"""V11 — Delivery dispatcher for all product types.

This module centralises delivery logic. Legacy KEY/FILE delivery in
``handlers/payment_handlers.py`` still runs unchanged; the dispatcher is
invoked *before* the legacy branch and takes over only for the 10 new
product types (returning ``DeliveryResult(handled=True, ...)``). If the
product is a legacy KEY or FILE (or an unknown type) it returns
``handled=False`` so the caller falls through to the existing code paths.

Every deliverer is idempotent. Calling ``dispatch(order_id)`` twice for
the same completed order will NOT consume extra inventory, generate a
second code, call an external provider twice, or duplicate a bundle.
"""
from __future__ import annotations

import json
import logging
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from database import (
    get_db_session, Product, ProductType, Order, OrderItem, ProductKey,
    Subscription, SubscriptionPlan, BundleItem, Preorder, ServiceOrder,
    ManualDeliveryTask, ExternalIntegration, GeneratedValue,
    ExternalDeliveryLog, StockReservation, ReservationStatus,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Result payload
# ─────────────────────────────────────────────────────────────────────
@dataclass
class DeliveryResult:
    handled: bool = False           # False → caller should run legacy path
    success: bool = False           # True → user notified with real assets
    queued: bool = False            # True → awaits admin fulfilment
    user_message: str = ""          # Text to send the user (may be empty)
    assets: List[str] = field(default_factory=list)  # keys/links/codes/etc.
    admin_notice: str = ""          # Text to send to admin channel
    error: Optional[str] = None
    idempotent_replay: bool = False # True → we've already delivered before


# ─────────────────────────────────────────────────────────────────────
# Type config helpers
# ─────────────────────────────────────────────────────────────────────
def load_type_config(product: Product) -> Dict[str, Any]:
    if not product.type_config:
        return {}
    try:
        return json.loads(product.type_config)
    except (ValueError, TypeError):
        return {}


def save_type_config(product: Product, cfg: Dict[str, Any]) -> None:
    product.type_config = json.dumps(cfg, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
def dispatch(order_id: int, session=None,
             order_item_id: Optional[int] = None) -> DeliveryResult:
    """Route ``order_id`` to the appropriate deliverer based on product type.

    Returns ``handled=False`` for legacy KEY/FILE types so the existing
    ``handlers/payment_handlers.py`` flow stays authoritative for them.
    Any raised exception is caught and returned as ``error``.

    Pass ``session`` when calling from within an existing ``get_db_session``
    block to avoid nested session close (which detaches ORM objects in the
    outer scope due to scoped_session sharing the same thread-local session).

    Pass ``order_item_id`` to dispatch a specific ``OrderItem`` by primary-key
    rather than always falling back to ``items[0]``. This is required for
    multi-item cart checkouts where each item in the cart loop must be
    delivered independently; without it, every iteration would re-deliver the
    first item.
    """
    if session is not None:
        try:
            return _dispatch_in_session(session, order_id, order_item_id=order_item_id)
        except Exception as e:
            logger.exception("delivery.dispatch failed for order %s", order_id)
            return DeliveryResult(handled=True, success=False, error=str(e))
    try:
        with get_db_session() as s:
            return _dispatch_in_session(s, order_id, order_item_id=order_item_id)
    except Exception as e:
        logger.exception("delivery.dispatch failed for order %s", order_id)
        return DeliveryResult(handled=True, success=False, error=str(e))


def _dispatch_in_session(s, order_id: int,
                         order_item_id: Optional[int] = None) -> DeliveryResult:
    order = s.query(Order).filter_by(id=order_id).first()
    if not order:
        return DeliveryResult(error="Order not found")

    if order_item_id is not None:
        # Caller specified the exact OrderItem to deliver — used by cart checkout
        # to process each item in the loop independently rather than always
        # processing items[0].
        item = s.query(OrderItem).filter_by(
            id=order_item_id, order_id=order_id
        ).first()
        if not item:
            return DeliveryResult(
                error=f"OrderItem {order_item_id} not found for order {order_id}"
            )
    else:
        # Single-item path (direct purchase / legacy callers).
        items = s.query(OrderItem).filter_by(order_id=order_id).all()
        if not items:
            return DeliveryResult(error="Order has no items")
        item = items[0]

    product = s.query(Product).filter_by(id=item.product_id).first()
    if not product:
        return DeliveryResult(error="Product not found")

    ptype = product.product_type
    deliverer = _DISPATCH_TABLE.get(ptype)
    if deliverer is None:
        # Legacy KEY / FILE — let caller run existing path.
        return DeliveryResult(handled=False)

    return deliverer(s, order, item, product)


# ─────────────────────────────────────────────────────────────────────
# Reusable primitives
# ─────────────────────────────────────────────────────────────────────
def _find_active_reservation(session, order_id: int, product_id: int,
                             variant_id: Optional[int] = None
                             ) -> Optional[StockReservation]:
    """Find the StockReservation that was created for this order/product
    BEFORE payment (see ``services.inventory.reserve``), so delivery can
    consume the exact keys that were already locked instead of pulling
    fresh unreserved rows.
    """
    q = session.query(StockReservation).filter(
        StockReservation.order_id == order_id,
        StockReservation.product_id == product_id,
        StockReservation.status.in_(
            [ReservationStatus.ACTIVE, ReservationStatus.CONSUMED]
        ),
    )
    if variant_id is not None:
        q = q.filter(StockReservation.variant_id == variant_id)
    return q.order_by(StockReservation.id.desc()).first()


def _consume_keys(session, product_id: int, quantity: int, order_id: int,
                  reservation_id: Optional[int] = None) -> List[str]:
    """Atomically mark ``quantity`` unsold keys as sold for this order.

    When ``reservation_id`` is given, this consumes the EXISTING reservation
    (the keys already locked for this order by ``services.inventory.reserve``)
    instead of querying fresh unreserved rows — this is required so
    dispatch-handled types (REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER) that reserved
    inventory before payment actually deliver the reserved keys, not an
    arbitrary different set.

    Falls back to the legacy "pull fresh unreserved stock" behaviour only
    when there is no reservation on record (e.g. admin-created orders that
    bypassed the reservation step).

    Uses SELECT ... FOR UPDATE on PostgreSQL to prevent double-delivery.
    Idempotent: if the order already has assigned keys FOR THIS PRODUCT/RESERVATION,
    those are returned.  The idempotency check is scoped to the specific
    reservation or product so that a multi-item cart order (which contains one
    ``order_id`` shared across all items) does not return a previously-delivered
    item's keys when the second item is being delivered.
    """
    # Scope idempotency to this reservation (most precise) or to this product
    # within the order (legacy fallback).  Never to the whole order — that would
    # return a different product's already-sold rows.
    if reservation_id is not None:
        # Keys permanently attached to THIS reservation after consume_locked().
        existing = session.query(ProductKey).filter(
            ProductKey.reservation_id == reservation_id,
            ProductKey.is_sold == True,     # noqa: E712
            ProductKey.order_id == order_id,
        ).all()
    else:
        # Legacy path — scope by product_id + order_id (not order-wide).
        existing = session.query(ProductKey).filter(
            ProductKey.product_id == product_id,
            ProductKey.order_id == order_id,
        ).all()
    if existing:
        return [k.key_value for k in existing]

    if reservation_id is not None:
        from services.inventory import consume_locked, ReservationError
        try:
            values = consume_locked(session, reservation_id, order_id)
        except ReservationError as e:
            raise RuntimeError(str(e))
        if len(values) < quantity:
            raise RuntimeError(
                f"reservation {reservation_id} yielded insufficient inventory: "
                f"{len(values)}/{quantity}"
            )
        return values

    # No reservation on record — legacy fallback: pull fresh unreserved
    # stock, explicitly excluding keys locked by someone else's reservation.
    q = session.query(ProductKey).filter_by(
        product_id=product_id, is_sold=False, order_id=None, reservation_id=None
    ).limit(quantity)
    if session.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    keys = q.all()
    if len(keys) < quantity:
        raise RuntimeError(
            f"insufficient inventory: {len(keys)}/{quantity}"
        )
    now = datetime.utcnow()
    values: List[str] = []
    for k in keys:
        k.is_sold = True
        k.order_id = order_id
        k.sold_at = now
        values.append(k.key_value)
    session.flush()
    return values


def _generate_value(cfg: Dict[str, Any]) -> str:
    """Cryptographically-secure value generator for AUTO_GENERATED."""
    mode = (cfg.get("mode") or "code").lower()
    prefix = cfg.get("prefix") or ""
    suffix = cfg.get("suffix") or ""
    length = int(cfg.get("length") or 16)
    length = max(4, min(length, 128))

    if mode == "uuid":
        body = uuid.uuid4().hex
    elif mode == "pin":
        body = "".join(secrets.choice(string.digits) for _ in range(length))
    elif mode == "token":
        body = secrets.token_urlsafe(length)[:length]
    else:  # random code — alphanumeric
        alphabet = cfg.get("charset") or (string.ascii_uppercase + string.digits)
        body = "".join(secrets.choice(alphabet) for _ in range(length))

    return f"{prefix}{body}{suffix}"


# ─────────────────────────────────────────────────────────────────────
# Per-type deliverers
# ─────────────────────────────────────────────────────────────────────
def _render_assets_message(product: Product, values: List[str]) -> str:
    """Render delivered values for the buyer.

    If the admin configured ``product.delivery_format_template`` (V17 —
    Formatted Account Delivery), each value is parsed and rendered through
    that template. Otherwise falls back to the exact legacy behaviour
    (plain newline-joined raw values) so untouched products never change.
    """
    template = getattr(product, "delivery_format_template", None)
    if template:
        from services.structured_delivery import render_delivery_message
        return "\n\n".join(render_delivery_message(template, v) for v in values)
    return "\n".join(values)


def _deliver_inventory_list(session, order: Order, item: OrderItem,
                            product: Product, label: str) -> DeliveryResult:
    """Deliver from the shared ``product_keys`` inventory table.

    Used by REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER (and could serve KEY too,
    but legacy KEY still uses ``assign_product_keys`` for backward compat).
    """
    if item.delivered_asset:
        stored_values = item.delivered_asset.split("\n")
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            assets=stored_values,
            user_message=f"✅ {label} — already delivered:\n\n"
                         + _render_assets_message(product, stored_values),
        )
    reservation = _find_active_reservation(
        session, order.id, product.id,
        variant_id=getattr(item, "variant_id", None),
    )
    try:
        values = _consume_keys(
            session, product.id, item.quantity, order.id,
            reservation_id=(reservation.id if reservation else None),
        )
    except RuntimeError as e:
        return DeliveryResult(handled=True, success=False, error=str(e))
    item.delivered_asset = "\n".join(values)
    session.commit()
    return DeliveryResult(
        handled=True, success=True, assets=values,
        user_message=f"✅ {label} delivered:\n\n" + _render_assets_message(product, values),
    )


def deliver_redeem_link(session, order, item, product):
    return _deliver_inventory_list(session, order, item, product, "🔗 Redeem link(s)")


def deliver_account_login(session, order, item, product):
    from services.inventory_import import format_account_delivery
    result = _deliver_inventory_list(session, order, item, product, "📧 Account(s)")
    # _deliver_inventory_list already applied product.delivery_format_template
    # (V17 — Formatted Account Delivery) when one is configured. Only fall
    # back to the historical hardcoded email/password/2FA formatting when the
    # admin hasn't set a custom template, so existing products keep working
    # exactly as before.
    if (result.success and result.assets
            and not getattr(product, "delivery_format_template", None)):
        result.user_message = "✅ 📧 Account(s) delivered:\n\n" + "\n\n".join(
            format_account_delivery(value) for value in result.assets
        )
    return result


def deliver_voucher(session, order, item, product):
    return _deliver_inventory_list(session, order, item, product, "🎟️ Voucher(s)")


def deliver_downloadable_file(session, order, item, product):
    if not product.telegram_file_id and not product.download_link:
        return DeliveryResult(handled=True, success=False,
                              error="no telegram_file_id or download_link configured")
    payload = product.telegram_file_id or product.download_link
    if not product.reusable and item.delivered_asset:
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            assets=[payload],
            user_message="✅ File already delivered.",
        )
    item.delivered_asset = payload
    session.commit()
    kind = product.telegram_file_type or "document"
    return DeliveryResult(
        handled=True, success=True, assets=[payload],
        user_message=(
            f"📁 {product.name}\n\nTelegram {kind} attached above."
            if product.telegram_file_id
            else f"📁 {product.name}\n🔗 Download: {payload}"
        ),
    )


def deliver_auto_generated(session, order, item, product):
    if item.delivered_asset:
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            assets=item.delivered_asset.split("\n"),
            user_message=f"✅ Already delivered:\n\n{item.delivered_asset}",
        )
    cfg = load_type_config(product)
    values: List[str] = []
    for _ in range(item.quantity):
        # Retry on unique-collision — extremely unlikely for 16+ char values
        for attempt in range(5):
            v = _generate_value(cfg)
            try:
                session.add(GeneratedValue(
                    product_id=product.id, order_id=order.id,
                    user_id=order.user_id, value=v,
                    expires_at=(
                        datetime.utcnow() + timedelta(days=int(cfg["expiry_days"]))
                        if cfg.get("expiry_days") else None
                    ),
                ))
                session.flush()
                values.append(v)
                break
            except IntegrityError:
                session.rollback()
                continue
        else:
            return DeliveryResult(handled=True, success=False,
                                  error="could not generate unique value")
    item.delivered_asset = "\n".join(values)
    session.commit()
    return DeliveryResult(
        handled=True, success=True, assets=values,
        user_message=f"🤖 Generated:\n\n" + "\n".join(values),
    )


def deliver_manual_delivery(session, order, item, product):
    existing = session.query(ManualDeliveryTask).filter_by(order_id=order.id).first()
    if existing:
        return DeliveryResult(
            handled=True, queued=True, idempotent_replay=True,
            user_message="👤 Your order is in the manual-delivery queue. "
                         "You will be notified once an admin completes it.",
        )
    task = ManualDeliveryTask(
        order_id=order.id, user_id=order.user_id, product_id=product.id,
        quantity=item.quantity, status="pending",
    )
    session.add(task)
    session.commit()
    return DeliveryResult(
        handled=True, queued=True,
        user_message="👤 Your order has been queued for manual delivery. "
                     "An admin will process it shortly.",
        admin_notice=f"🆕 Manual delivery task #{task.id} for order #{order.id} "
                     f"({product.name} x{item.quantity})",
    )


def deliver_preorder(session, order, item, product):
    existing = session.query(Preorder).filter_by(order_id=order.id).first()
    if existing:
        return DeliveryResult(
            handled=True, queued=True, idempotent_replay=True,
            user_message="⏳ Pre-order already recorded.",
        )
    cfg = load_type_config(product)
    est = cfg.get("estimated_delivery") or "TBA"
    pre = Preorder(
        order_id=order.id, user_id=order.user_id, product_id=product.id,
        quantity=item.quantity, status="pending", estimated_delivery=est,
    )
    session.add(pre)
    session.commit()
    return DeliveryResult(
        handled=True, queued=True,
        user_message=f"⏳ Pre-order confirmed.\n\n"
                     f"Estimated delivery: {est}\n\n"
                     f"You will be notified once your order is ready.",
        admin_notice=f"🆕 Pre-order #{pre.id} for order #{order.id} "
                     f"({product.name} x{item.quantity})",
    )


def deliver_subscription(session, order, item, product):
    existing = session.query(Subscription).filter_by(order_id=order.id).first()
    if existing:
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            user_message=f"♻️ Subscription active until {existing.expires_at:%Y-%m-%d}",
        )
    plan = session.query(SubscriptionPlan).filter_by(
        product_id=product.id, is_active=True
    ).order_by(SubscriptionPlan.display_order).first()
    if not plan:
        # Fall back to type_config default duration.
        cfg = load_type_config(product)
        duration = int(cfg.get("duration_days") or 30)
    else:
        duration = plan.duration_days
    now = datetime.utcnow()
    billing_amount = plan.price if plan else float(product.price or 0.0)
    sub = Subscription(
        user_id=order.user_id, product_id=product.id,
        plan_id=(plan.id if plan else None), order_id=order.id,
        starts_at=now, expires_at=now + timedelta(days=duration),
        status="active",
        # V13: recurring billing — bill again one cycle from now.
        next_billing_date=now + timedelta(days=duration),
        billing_cycle_days=duration,
        billing_amount=billing_amount,
        auto_renew=True,
    )
    session.add(sub)
    session.commit()
    return DeliveryResult(
        handled=True, success=True,
        user_message=(
            f"♻️ Subscription activated.\n\n"
            f"Plan: {plan.name if plan else 'default'}\n"
            f"Duration: {duration} day(s)\n"
            f"Expires: {sub.expires_at:%Y-%m-%d}"
        ),
    )


def deliver_bundle(session, order, item, product):
    """Deliver a BUNDLE product by consuming each key-backed child's reservation.

    Two-pass algorithm:
      Pass 1 — Verify that every key-backed child either has an ACTIVE
               reservation with enough locked keys (created by
               ``inventory.reserve_bundle()`` at cart-checkout time) OR
               has sufficient unreserved free stock (legacy / admin-created
               orders that bypassed the reservation step).
               Returns an error immediately if either condition is not met
               for any child — no inventory is consumed in this pass.
      Pass 2 — Consume, using the existing reservation where one exists, or
               pulling fresh unreserved stock as the legacy fallback.
               The child-level ``_find_active_reservation`` + ``_consume_keys``
               path guarantees we deliver the SAME keys that were locked at
               reservation time, not an arbitrary other set.

    Idempotent: if ``item.delivered_asset`` is already set, return it immediately
    without touching inventory.
    """
    bundle_children = session.query(BundleItem).filter_by(parent_product_id=product.id).all()
    if not bundle_children:
        return DeliveryResult(handled=True, success=False,
                              error="bundle has no child items configured")

    # Prevent recursion — reject bundle-in-bundle chains.
    for bi in bundle_children:
        child = session.query(Product).filter_by(id=bi.child_product_id).first()
        if not child:
            return DeliveryResult(handled=True, success=False,
                                  error=f"child product {bi.child_product_id} missing")
        if child.product_type == ProductType.BUNDLE:
            return DeliveryResult(handled=True, success=False,
                                  error="nested bundles are not supported")

    # Idempotent replay.
    if item.delivered_asset:
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            user_message="✅ Bundle already delivered:\n\n" + item.delivered_asset,
        )

    # ── Pass 1: verify stock (no mutations) ───────────────────────────
    _KEY_BACKED = (ProductType.KEY, ProductType.REDEEM_LINK,
                   ProductType.ACCOUNT_LOGIN, ProductType.VOUCHER)
    for bi in bundle_children:
        need = bi.quantity * item.quantity
        child = session.query(Product).filter_by(id=bi.child_product_id).first()
        if child.product_type not in _KEY_BACKED:
            continue

        child_res = _find_active_reservation(session, order.id, child.id)
        if child_res:
            # Reservation exists — verify it still holds enough locked keys.
            locked = session.query(ProductKey).filter_by(
                reservation_id=child_res.id, is_sold=False
            ).count()
            if locked < need:
                return DeliveryResult(
                    handled=True, success=False,
                    error=(
                        f"reserved inventory for '{child.name}' is insufficient: "
                        f"locked {locked}, need {need}"
                    ),
                )
        else:
            # No reservation — check fresh available unreserved stock (legacy fallback).
            available = session.query(ProductKey).filter_by(
                product_id=child.id, is_sold=False, order_id=None, reservation_id=None
            ).count()
            if available < need:
                return DeliveryResult(
                    handled=True, success=False,
                    error=(
                        f"insufficient stock of '{child.name}': "
                        f"need {need}, have {available}"
                    ),
                )

    # ── Pass 2: consume atomically using reserved inventory ───────────
    delivered_lines: List[str] = []
    for bi in bundle_children:
        need = bi.quantity * item.quantity
        child = session.query(Product).filter_by(id=bi.child_product_id).first()
        if child.product_type in _KEY_BACKED:
            # Resolve the child reservation so we consume the SAME keys that
            # were locked at reservation time (not an arbitrary other set).
            child_res = _find_active_reservation(session, order.id, child.id)
            values = _consume_keys(
                session, child.id, need, order.id,
                reservation_id=(child_res.id if child_res else None),
            )
            delivered_lines.append(f"— {child.name} x{need}:\n" + "\n".join(values))
        elif child.product_type in (ProductType.DOWNLOADABLE_FILE, ProductType.FILE):
            payload = child.telegram_file_id or child.download_link or ""
            delivered_lines.append(f"— {child.name}: {payload}")
        else:
            delivered_lines.append(f"— {child.name} x{need} (manual fulfilment)")

    item.delivered_asset = "\n\n".join(delivered_lines)
    session.commit()
    return DeliveryResult(
        handled=True, success=True,
        user_message="📦 Bundle delivered:\n\n" + item.delivered_asset,
    )


def deliver_service(session, order, item, product):
    existing = session.query(ServiceOrder).filter_by(order_id=order.id).first()
    if existing:
        return DeliveryResult(
            handled=True, queued=True, idempotent_replay=True,
            user_message="🛠️ Service order already queued.",
        )
    # Pull captured customer info off the order metadata if the checkout
    # flow stored it there (see handlers/admin_product_types.py).
    cfg = load_type_config(product)
    so = ServiceOrder(
        order_id=order.id, user_id=order.user_id, product_id=product.id,
        submitted_fields=json.dumps(cfg.get("_pending_customer_info") or {}),
        status="pending",
    )
    session.add(so)
    session.commit()
    return DeliveryResult(
        handled=True, queued=True,
        user_message="🛠️ Service order received. An admin will contact you shortly.",
        admin_notice=f"🆕 Service task #{so.id} for order #{order.id} ({product.name})",
    )


def deliver_external(session, order, item, product):
    """External-delivery deliverer.

    NOTE: this issues a single attempt synchronously with a timeout. The
    admin queue keeps track of failed jobs; a proper retry worker can be
    plugged in later using ``ExternalDeliveryLog``.
    """
    import os, urllib.parse, urllib.request

    cfg = load_type_config(product)
    integration_id = cfg.get("integration_id")
    if not integration_id:
        return DeliveryResult(handled=True, success=False,
                              error="no integration_id configured")
    integ = session.query(ExternalIntegration).filter_by(
        id=integration_id, is_active=True
    ).first()
    if not integ:
        return DeliveryResult(handled=True, success=False,
                              error="integration inactive or missing")

    # Idempotency — one row per order.
    idem = f"order:{order.id}"
    existing = session.query(ExternalDeliveryLog).filter_by(
        idempotency_key=idem
    ).first()
    if existing and existing.status == "success":
        return DeliveryResult(
            handled=True, success=True, idempotent_replay=True,
            assets=[existing.delivered_value or ""],
            user_message=f"✅ Already delivered:\n\n{existing.delivered_value or ''}",
        )

    # SECURITY: block private/loopback URLs.
    parsed = urllib.parse.urlparse(integ.endpoint_url)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return DeliveryResult(handled=True, success=False,
                              error="private network target blocked")

    headers = {"Content-Type": "application/json"}
    if integ.auth_type == "bearer" and integ.credential_env_name:
        tok = os.environ.get(integ.credential_env_name, "")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"

    body = {}
    try:
        if integ.request_template:
            body = json.loads(integ.request_template)
    except (ValueError, TypeError):
        body = {}
    body.setdefault("order_id", order.id)
    body.setdefault("product_id", product.id)
    body.setdefault("quantity", item.quantity)
    body.setdefault("idempotency_key", idem)

    log = existing or ExternalDeliveryLog(
        order_id=order.id, integration_id=integ.id,
        idempotency_key=idem, attempt=1, status="pending",
    )
    if existing:
        log.attempt += 1
    session.add(log)
    session.commit()

    try:
        req = urllib.request.Request(
            integ.endpoint_url, method=integ.http_method or "POST",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=integ.timeout_seconds) as resp:
            raw = resp.read(65536)  # cap at 64 KB
            log.http_status = resp.status
            log.response_summary = raw[:2000].decode("utf-8", errors="replace")
    except Exception as e:
        log.status = "failed"
        log.error_summary = str(e)[:500]
        log.completed_at = datetime.utcnow()
        session.commit()
        return DeliveryResult(
            handled=True, success=False,
            error=f"external call failed: {e}",
            admin_notice=f"❌ External delivery FAILED for order #{order.id} "
                         f"({integ.name}): {e}",
        )

    # Extract mapped delivery field.
    delivered = ""
    try:
        parsed_resp = json.loads(log.response_summary or "{}")
        mapping = json.loads(integ.response_mapping or "{}")
        key_path = mapping.get("delivered_value_path") or "value"
        cur: Any = parsed_resp
        for p in str(key_path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(p)
        delivered = str(cur) if cur is not None else ""
    except Exception:
        delivered = log.response_summary or ""

    log.delivered_value = delivered
    log.status = "success" if log.http_status and log.http_status < 400 else "failed"
    log.completed_at = datetime.utcnow()
    item.delivered_asset = delivered
    session.commit()

    if log.status != "success":
        return DeliveryResult(
            handled=True, success=False,
            error=f"external HTTP {log.http_status}",
            admin_notice=f"❌ External delivery HTTP {log.http_status} "
                         f"for order #{order.id}",
        )
    return DeliveryResult(
        handled=True, success=True, assets=[delivered],
        user_message=f"🌐 Delivered:\n\n{delivered}",
    )


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────
_DISPATCH_TABLE = {
    ProductType.REDEEM_LINK:       deliver_redeem_link,
    ProductType.ACCOUNT_LOGIN:     deliver_account_login,
    ProductType.VOUCHER:           deliver_voucher,
    ProductType.DOWNLOADABLE_FILE: deliver_downloadable_file,
    ProductType.AUTO_GENERATED:    deliver_auto_generated,
    ProductType.MANUAL_DELIVERY:   deliver_manual_delivery,
    ProductType.PREORDER:          deliver_preorder,
    ProductType.SUBSCRIPTION:      deliver_subscription,
    ProductType.BUNDLE:            deliver_bundle,
    ProductType.SERVICE:           deliver_service,
    ProductType.EXTERNAL_DELIVERY: deliver_external,
    # KEY and FILE deliberately absent — legacy code paths keep handling them.
}
