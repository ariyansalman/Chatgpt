"""Inventory reservation engine — the safety net that prevents overselling.

A reservation is created at the checkout / payment step. It puts the required
``ProductKey`` rows (for KEY products) into a locked state, or reserves a
quantity slot (for FILE products). It expires automatically after the TTL
configured via bot_config ``inventory_reservation_ttl_minutes``, and is
CONSUMED only when the order is successfully fulfilled.

All state mutations happen inside the caller's ``get_db_session`` transaction,
with row-level locks on PostgreSQL (``SELECT ... FOR UPDATE``). SQLite falls
back to the same logic without the lock (single-writer anyway).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from database import get_db_session
from database.models import (
    ProductKey, StockReservation, ReservationStatus,
    Product, ProductVariant, ProductType, InventoryBatch,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


def _ttl_minutes() -> int:
    val = cfg.get_int("inventory_reservation_ttl_minutes", 15)
    return max(1, val)  # never zero — that would insta-expire


def _lock_query(q, dialect_name: str):
    """Apply row-level lock on Postgres only. SQLite has no equivalent."""
    if dialect_name == "postgresql":
        return q.with_for_update(skip_locked=True)
    return q


class ReservationError(Exception):
    """Raised when a reservation cannot be created (insufficient stock, etc.)."""


# Product types whose inventory lives in the shared ``product_keys`` table
# (as opposed to the ``Product.stock_count``/``ProductVariant.stock_count``
# counters used by FILE-type products). All of these are reserved and
# consumed identically — a specific ``ProductKey`` row is locked to the
# reservation, then flipped to sold on consume.
KEY_BACKED_TYPES: Tuple[ProductType, ...] = (
    ProductType.KEY,
    ProductType.REDEEM_LINK,
    ProductType.ACCOUNT_LOGIN,
    ProductType.VOUCHER,
)


def reserve(user_id: int, product_id: int, quantity: int,
            variant_id: Optional[int] = None,
            order_id: Optional[int] = None) -> StockReservation:
    """Create a temporary hold on ``quantity`` units of the given product/variant.

    Returns the persisted :class:`StockReservation`. Raises
    :class:`ReservationError` when stock is insufficient.
    """
    if quantity <= 0:
        raise ReservationError("Quantity must be > 0")

    expires = datetime.utcnow() + timedelta(minutes=_ttl_minutes())

    with get_db_session() as s:
        dialect = s.bind.dialect.name if s.bind else "sqlite"

        product = s.query(Product).filter(Product.id == product_id).first()
        if not product or not product.is_active:
            raise ReservationError("Product unavailable")

        # Passive cleanup: expire stale reservations for this scope first.
        _expire_stale(s)

        reservation = StockReservation(
            user_id=user_id,
            product_id=product_id,
            variant_id=variant_id,
            order_id=order_id,
            quantity=quantity,
            status=ReservationStatus.ACTIVE,
            expires_at=expires,
        )
        s.add(reservation)
        s.flush()  # populate reservation.id

        if product.product_type in KEY_BACKED_TYPES:
            # V24 — Supplier Auto Assignment: prefer keys from higher-priority
            # suppliers. Falls back to any key when auto-assignment is disabled
            # or no assignment exists for this product.
            from services.supplier_auto_assign import get_preferred_batch_ids
            preferred_batch_ids = get_preferred_batch_ids(product_id, variant_id, s)

            def _build_key_query(batch_filter=None):
                q = (
                    s.query(ProductKey)
                    .filter(ProductKey.product_id == product_id)
                    .filter(ProductKey.is_sold == False)  # noqa: E712
                    .filter(ProductKey.reservation_id == None)  # noqa: E711
                )
                if variant_id is not None:
                    q = q.filter(ProductKey.variant_id == variant_id)
                if batch_filter is not None:
                    q = q.filter(batch_filter)
                return q

            if preferred_batch_ids:
                # Two-pass strategy: first fill from preferred supplier batches
                # (in priority order), then fall back to remaining keys if
                # sas_fallback_to_any is enabled.
                keys: List[ProductKey] = []
                remaining_needed = quantity
                for batch_id in preferred_batch_ids:
                    if remaining_needed <= 0:
                        break
                    batch_q = _build_key_query(ProductKey.batch_id == batch_id)
                    batch_q = _lock_query(batch_q, dialect).limit(remaining_needed)
                    batch_keys = batch_q.all()
                    keys.extend(batch_keys)
                    remaining_needed -= len(batch_keys)

                # Fallback to any remaining key if enabled and still short
                if remaining_needed > 0 and cfg.get_bool("sas_fallback_to_any", True):
                    exclude_batch_ids = preferred_batch_ids
                    fallback_q = _build_key_query(
                        ProductKey.batch_id.notin_(exclude_batch_ids)
                        if exclude_batch_ids else None
                    )
                    fallback_q = _lock_query(fallback_q, dialect).limit(remaining_needed)
                    keys.extend(fallback_q.all())
            else:
                # No supplier assignments — original behaviour (any available key)
                key_q = _build_key_query()
                key_q = _lock_query(key_q, dialect).limit(quantity)
                keys: List[ProductKey] = key_q.all()
            if len(keys) < quantity:
                # Insufficient stock — roll back the reservation row.
                s.delete(reservation)
                raise ReservationError(
                    f"Only {len(keys)} key(s) in stock (need {quantity})"
                )
            for k in keys:
                k.reservation_id = reservation.id
        else:  # FILE-type — check the counter
            # Row-lock the Product (and Variant, if any) BEFORE reading the
            # stock counter and the sum of active reservations below. Without
            # this lock, two concurrent reserve() calls on the same
            # product/variant can both read the same "available - already"
            # value under READ COMMITTED isolation and both pass the check,
            # oversubscribing stock (unlike the KEY_BACKED_TYPES branch above,
            # which already locks the actual ProductKey rows it selects).
            # SELECT ... FOR UPDATE on Postgres serializes concurrent callers
            # on this product/variant; SQLite is single-writer so the plain
            # query below is equivalent there.
            locked_product_q = _lock_query(
                s.query(Product).filter(Product.id == product_id), dialect
            )
            product = locked_product_q.first()
            available = product.stock_count or 0
            if variant_id is not None:
                locked_variant_q = _lock_query(
                    s.query(ProductVariant).filter(ProductVariant.id == variant_id),
                    dialect,
                )
                variant = locked_variant_q.first()
                if variant is None or not variant.is_active:
                    s.delete(reservation)
                    raise ReservationError("Variant unavailable")
                available = variant.stock_count or 0
            # Subtract quantities already reserved for this scope. Safe to
            # read now — the lock above holds until this transaction commits
            # or rolls back, so no other reserve() call on this product/
            # variant can slip in between this count and our own insert.
            reserved_q = s.query(StockReservation).filter(
                StockReservation.product_id == product_id,
                StockReservation.status == ReservationStatus.ACTIVE,
                StockReservation.id != reservation.id,
            )
            if variant_id is not None:
                reserved_q = reserved_q.filter(
                    StockReservation.variant_id == variant_id)
            already = sum(r.quantity for r in reserved_q.all())
            if available - already < quantity:
                s.delete(reservation)
                raise ReservationError(
                    f"Only {max(0, available - already)} unit(s) available"
                )

        s.commit()
        # Return a detached, safe copy — the ORM instance is bound to `s` which
        # exits scope. Caller only needs the id.
        return reservation


def reserve_bundle(
    user_id: int,
    bundle_product_id: int,
    quantity: int,
    order_id: Optional[int] = None,
) -> List[StockReservation]:
    """Reserve all key-backed child inventory for a BUNDLE product atomically.

    All child reservations are created inside a SINGLE database transaction.
    If any child has insufficient stock the entire transaction is rolled back,
    releasing every key lock created in that attempt, and
    :class:`ReservationError` is raised.

    Non-key-backed children (FILE, DOWNLOADABLE_FILE, AUTO_GENERATED, …)
    are skipped — they have no per-unit row to lock.

    Returns the list of child :class:`StockReservation` objects that were
    committed. The caller should attach ``order_id`` to them (via a bulk
    UPDATE filtered on the returned ids) after the order row is created, and
    must call ``release_for_order(order_id)`` on any failure path.
    """
    from database.models import BundleItem  # local import avoids circular at module load

    if quantity <= 0:
        raise ReservationError("Quantity must be > 0")

    expires = datetime.utcnow() + timedelta(minutes=_ttl_minutes())

    with get_db_session() as s:
        dialect = s.bind.dialect.name if s.bind else "sqlite"

        bundle = s.query(Product).filter(Product.id == bundle_product_id).first()
        if not bundle or not bundle.is_active:
            raise ReservationError("Bundle product unavailable")
        if bundle.product_type != ProductType.BUNDLE:
            raise ReservationError(
                f"Product {bundle_product_id} is not a BUNDLE type"
            )

        children = s.query(BundleItem).filter_by(
            parent_product_id=bundle_product_id
        ).all()
        if not children:
            raise ReservationError("Bundle has no child items configured")

        _expire_stale(s)

        created: List[StockReservation] = []
        try:
            for bi in children:
                child = s.query(Product).filter(
                    Product.id == bi.child_product_id
                ).first()
                if not child or not child.is_active:
                    raise ReservationError(
                        f"Bundle child product {bi.child_product_id} is unavailable"
                    )
                if child.product_type == ProductType.BUNDLE:
                    raise ReservationError("Nested bundles are not supported")
                if child.product_type not in KEY_BACKED_TYPES:
                    continue  # FILE / AUTO_GENERATED / etc. — no per-key reservation

                need = bi.quantity * quantity

                # Lock and pick unreserved, unsold keys for this child.
                key_q = (
                    s.query(ProductKey)
                    .filter(ProductKey.product_id == child.id)
                    .filter(ProductKey.is_sold == False)         # noqa: E712
                    .filter(ProductKey.reservation_id == None)   # noqa: E711
                )
                key_q = _lock_query(key_q, dialect).limit(need)
                locked_keys: List[ProductKey] = key_q.all()

                if len(locked_keys) < need:
                    raise ReservationError(
                        f"Insufficient stock for bundle child '{child.name}': "
                        f"need {need}, have {len(locked_keys)}"
                    )

                res = StockReservation(
                    user_id=user_id,
                    product_id=child.id,
                    order_id=order_id,
                    quantity=need,
                    status=ReservationStatus.ACTIVE,
                    expires_at=expires,
                )
                s.add(res)
                s.flush()  # populate res.id

                for k in locked_keys:
                    k.reservation_id = res.id

                created.append(res)

            s.commit()
            return created

        except ReservationError:
            s.rollback()
            raise
        except Exception as exc:
            s.rollback()
            raise ReservationError(f"Bundle reservation failed: {exc}") from exc


def release_locked(session, reservation: StockReservation) -> None:
    """Core release logic — operates on the CALLER's session/transaction.

    Does not commit or close the session; the caller controls the
    transaction boundary. Use this when already inside an open
    ``get_db_session()`` block (e.g. from ``services.delivery_service``)
    to avoid opening a nested session on the shared scoped-session.
    """
    session.query(ProductKey).filter(
        ProductKey.reservation_id == reservation.id,
        ProductKey.is_sold == False,  # noqa: E712
    ).update({"reservation_id": None}, synchronize_session=False)
    reservation.status = ReservationStatus.RELEASED
    reservation.released_at = datetime.utcnow()


def release(reservation_id: int, *, reason: str = "released") -> bool:
    """Release an ACTIVE reservation. Idempotent — no-op if already closed."""
    with get_db_session() as s:
        r = s.query(StockReservation).filter(
            StockReservation.id == reservation_id).first()
        if not r or r.status != ReservationStatus.ACTIVE:
            return False
        release_locked(s, r)
        s.commit()
        return True


def consume_locked(session, reservation_id: int, order_id: int) -> List[str]:
    """Core consume logic — operates on the CALLER's session/transaction.

    Does not commit or close the session; the caller controls the
    transaction boundary. Use this when already inside an open
    ``get_db_session()`` block (e.g. from ``services.delivery_service``)
    to avoid opening a nested session on the shared scoped-session, which
    would prematurely close/detach objects still in use by the outer scope.

    Returns the list of delivered key values. For FILE-type products (no
    keys attached to the reservation) returns an empty list — the caller
    uses the product's ``download_link`` instead.
    """
    r = session.query(StockReservation).filter(
        StockReservation.id == reservation_id).first()
    if not r:
        raise ReservationError("Reservation not found")
    if r.status == ReservationStatus.CONSUMED:
        # Idempotent — return whatever was previously delivered.
        keys = session.query(ProductKey).filter(
            ProductKey.reservation_id == r.id).all()
        return [k.key_value for k in keys]
    if r.status != ReservationStatus.ACTIVE:
        raise ReservationError(f"Reservation is {r.status.value}, cannot consume")

    now = datetime.utcnow()
    delivered: List[str] = []
    keys = session.query(ProductKey).filter(
        ProductKey.reservation_id == r.id,
        ProductKey.is_sold == False,  # noqa: E712
    ).all()
    for k in keys:
        k.is_sold = True
        k.sold_at = now
        k.order_id = order_id
        delivered.append(k.key_value)

    # For FILE-type variants, decrement the counter.
    if not keys:
        if r.variant_id is not None:
            v = session.query(ProductVariant).filter(
                ProductVariant.id == r.variant_id).first()
            if v is not None:
                v.stock_count = max(0, (v.stock_count or 0) - r.quantity)
        else:
            p = session.query(Product).filter(Product.id == r.product_id).first()
            if p is not None and p.product_type == ProductType.FILE:
                p.stock_count = max(0, (p.stock_count or 0) - r.quantity)

    r.status = ReservationStatus.CONSUMED
    r.order_id = order_id
    r.released_at = now

    # V24 — Record supplier delivery stats for each key's supplier.
    # Runs best-effort inside the same session; a failure here must not
    # block the consume (delivery already completed above).
    if keys:
        try:
            from services.supplier_auto_assign import get_supplier_for_key, record_supplier_delivery
            seen_suppliers: set = set()
            for k in keys:
                sup = get_supplier_for_key(k, session)
                if sup and sup.id not in seen_suppliers:
                    record_supplier_delivery(sup.id, success=True, session=session)
                    seen_suppliers.add(sup.id)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "consume_locked: supplier delivery tracking failed"
            )

    return delivered


def consume(reservation_id: int, order_id: int) -> List[str]:
    """Mark the reservation's keys as sold and attach them to ``order_id``.

    Opens its own session — use ``consume_locked`` instead when already
    inside an existing ``get_db_session()`` block.
    """
    with get_db_session() as s:
        delivered = consume_locked(s, reservation_id, order_id)
        s.commit()
        return delivered


def _expire_stale(session) -> int:
    """Mark expired ACTIVE reservations as EXPIRED and free their keys."""
    now = datetime.utcnow()
    stale = session.query(StockReservation).filter(
        StockReservation.status == ReservationStatus.ACTIVE,
        StockReservation.expires_at < now,
    ).all()
    count = 0
    for r in stale:
        session.query(ProductKey).filter(
            ProductKey.reservation_id == r.id,
            ProductKey.is_sold == False,  # noqa: E712
        ).update({"reservation_id": None}, synchronize_session=False)
        r.status = ReservationStatus.EXPIRED
        r.released_at = now
        count += 1
    if count:
        session.flush()
    return count


async def expire_reservations_job(context=None) -> None:
    """Background sweeper — run periodically from the JobQueue."""
    try:
        with get_db_session() as s:
            n = _expire_stale(s)
            s.commit()
            if n:
                logger.info("Expired %d stale reservation(s)", n)
    except SQLAlchemyError:
        logger.exception("expire_reservations_job failed")


def count_available(product_id: int,
                    variant_id: Optional[int] = None) -> int:
    """Return real available stock = physical stock − active reservations."""
    with get_db_session() as s:
        _expire_stale(s)
        product = s.query(Product).filter(Product.id == product_id).first()
        if not product:
            return 0
        # All KEY_BACKED_TYPES (KEY, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER) use
        # the product_keys table - count unreserved, unsold rows.
        if product.product_type in KEY_BACKED_TYPES:
            q = s.query(ProductKey).filter(
                ProductKey.product_id == product_id,
                ProductKey.is_sold == False,  # noqa: E712
                ProductKey.reservation_id == None,  # noqa: E711
            )
            if variant_id is not None:
                q = q.filter(ProductKey.variant_id == variant_id)
            return q.count()
        # FILE-type and others use stock_count
        base = product.stock_count or 0
        if variant_id is not None:
            v = s.query(ProductVariant).filter(
                ProductVariant.id == variant_id).first()
            base = (v.stock_count or 0) if v else 0
        reserved_q = s.query(StockReservation).filter(
            StockReservation.product_id == product_id,
            StockReservation.status == ReservationStatus.ACTIVE,
        )
        if variant_id is not None:
            reserved_q = reserved_q.filter(
                StockReservation.variant_id == variant_id)
        reserved = sum(r.quantity for r in reserved_q.all())
        return max(0, base - reserved)


def count_available_bulk(product_ids: List[int]) -> dict:
    """Bulk version of ``count_available`` for product-level (no variant)
    stock — returns ``{product_id: available_stock}``.

    Used by the flat "🛍 Products" catalog, which renders every active
    product in one screen and would otherwise issue one ``get_db_session``
    round-trip per row (N+1). Runs a handful of aggregate queries total
    instead, all inside a single session/transaction.
    """
    ids = [pid for pid in dict.fromkeys(product_ids)]  # de-dup, keep order
    if not ids:
        return {}

    result = {pid: 0 for pid in ids}

    with get_db_session() as s:
        _expire_stale(s)

        products = s.query(Product).filter(Product.id.in_(ids)).all()
        key_backed_ids = [p.id for p in products if p.product_type in KEY_BACKED_TYPES]
        counter_products = {p.id: p for p in products if p.product_type not in KEY_BACKED_TYPES}

        if key_backed_ids:
            from sqlalchemy import func
            rows = (
                s.query(ProductKey.product_id, func.count(ProductKey.id))
                .filter(
                    ProductKey.product_id.in_(key_backed_ids),
                    ProductKey.is_sold == False,  # noqa: E712
                    ProductKey.reservation_id == None,  # noqa: E711
                )
                .group_by(ProductKey.product_id)
                .all()
            )
            for pid, cnt in rows:
                result[pid] = cnt

        if counter_products:
            from sqlalchemy import func
            reserved_rows = (
                s.query(StockReservation.product_id, func.sum(StockReservation.quantity))
                .filter(
                    StockReservation.product_id.in_(counter_products.keys()),
                    StockReservation.status == ReservationStatus.ACTIVE,
                    StockReservation.variant_id == None,  # noqa: E711
                )
                .group_by(StockReservation.product_id)
                .all()
            )
            reserved_map = {pid: (qty or 0) for pid, qty in reserved_rows}
            for pid, product in counter_products.items():
                base = product.stock_count or 0
                reserved = reserved_map.get(pid, 0)
                result[pid] = max(0, base - reserved)

    return result


def low_stock_warning(product_id: int, variant_id: Optional[int] = None) -> str:
    """Return an urgency line like '⚠️ Only 3 left!' for the product detail
    view, or '' when nothing should be shown.

    Uses ``count_available()`` (real available stock — physical stock minus
    active reservations, and for KEY-backed types, unsold/unreserved
    ``ProductKey`` rows) rather than the raw ``stock_count`` column, so the
    number matches what checkout will actually let the buyer take.

    Threshold is admin-configurable via bot_config ``low_stock_threshold``
    (default 5, "Inventory" section of the admin panel) — the same knob
    already used for the admin low-stock alert, so both stay in sync.
    Shows nothing when available stock is above the threshold.
    """
    threshold = cfg.get_int("low_stock_threshold", 5)
    if threshold <= 0:
        return ""

    available = count_available(product_id, variant_id=variant_id)
    if available > threshold:
        return ""
    if available <= 0:
        return "❌ Out of stock!"
    return f"⚠️ Only {available} left!"


def release_for_order(order_id: int, *, reason: str = "order_cancelled") -> int:
    """Release every ACTIVE reservation attached to ``order_id``.

    Used by admin/user cancellation and payment-expiry paths so held keys
    return to inventory immediately. Idempotent.
    """
    released = 0
    with get_db_session() as s:
        rows = s.query(StockReservation).filter(
            StockReservation.order_id == order_id,
            StockReservation.status == ReservationStatus.ACTIVE,
        ).all()
        for r in rows:
            release_locked(s, r)
            released += 1
        s.commit()
    if released:
        logger.info("Released %d reservation(s) for order %s (%s)",
                    released, order_id, reason)
    return released
