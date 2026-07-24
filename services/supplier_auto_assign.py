"""V24 — Supplier Auto Assignment engine.

Automatically selects the best supplier's keys when fulfilling an order
for KEY-backed product types. Users never see supplier information — all
selection happens transparently in the background.

Key features:
• Priority-based selection (lower priority number = selected first)
• Per-supplier per-product assignment configuration
• Race condition prevention via PostgreSQL row-level locks
• Delivery stat tracking (total_delivered, total_failed, last_activity)
• Graceful fallback to any available key when no assignment exists

Usage:
    from services.supplier_auto_assign import (
        get_preferred_batch_ids,
        record_supplier_delivery,
    )
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from database.models import (
    Supplier, SupplierProduct, InventoryBatch, ProductKey, ReservationStatus,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """Return True when the global auto-assignment feature is switched on."""
    return cfg.get_bool("sas_enabled", True)


def get_preferred_batch_ids(
    product_id: int,
    variant_id: Optional[int],
    session: Session,
) -> List[int]:
    """Return an ordered list of batch IDs to prefer when picking keys.

    The list is sorted by (SupplierProduct.priority ASC, Supplier.priority ASC)
    so lower numbers are tried first.  An empty list means no assignments exist
    and the caller should use any available key.

    Only considers assignments where:
        • SupplierProduct.is_active is True
        • SupplierProduct.is_auto_assign is True
        • Supplier.is_active is True
    """
    if not is_enabled():
        return []

    # Find active auto-assign entries for this product/variant, ordered by priority.
    q = (
        session.query(SupplierProduct)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .filter(
            SupplierProduct.product_id == product_id,
            SupplierProduct.is_active.is_(True),
            SupplierProduct.is_auto_assign.is_(True),
            Supplier.is_active.is_(True),
        )
    )
    if variant_id is not None:
        # Prefer variant-specific assignments; fall back to product-wide ones below.
        q = q.filter(
            (SupplierProduct.variant_id == variant_id) |
            (SupplierProduct.variant_id.is_(None))
        )
    else:
        q = q.filter(SupplierProduct.variant_id.is_(None))

    assignments: List[SupplierProduct] = (
        q.order_by(SupplierProduct.priority.asc(), Supplier.priority.asc()).all()
    )

    if not assignments:
        return []

    # Map each assignment → batch IDs for that supplier/product.
    batch_ids: List[int] = []
    seen: set = set()
    for sp in assignments:
        batches = (
            session.query(InventoryBatch.id)
            .filter(
                InventoryBatch.supplier_id == sp.supplier_id,
                InventoryBatch.product_id == product_id,
            )
        )
        if variant_id is not None:
            batches = batches.filter(
                (InventoryBatch.variant_id == variant_id) |
                (InventoryBatch.variant_id.is_(None))
            )
        for (bid,) in batches.all():
            if bid not in seen:
                batch_ids.append(bid)
                seen.add(bid)

    return batch_ids


def count_supplier_stock(
    supplier_id: int,
    product_id: int,
    variant_id: Optional[int],
    session: Session,
) -> int:
    """Count unreserved, unsold keys for a supplier/product combination."""
    q = (
        session.query(ProductKey)
        .join(InventoryBatch, ProductKey.batch_id == InventoryBatch.id)
        .filter(
            ProductKey.product_id == product_id,
            ProductKey.is_sold.is_(False),
            ProductKey.reservation_id.is_(None),
            InventoryBatch.supplier_id == supplier_id,
        )
    )
    if variant_id is not None:
        q = q.filter(ProductKey.variant_id == variant_id)
    return q.count()


def record_supplier_delivery(
    supplier_id: int,
    success: bool,
    session: Session,
) -> None:
    """Update delivered/failed counters and last_activity on the supplier row.

    Called by the inventory engine after a key is consumed. Does NOT commit —
    the caller's transaction covers this update.
    """
    try:
        sup = session.get(Supplier, supplier_id)
        if sup is None:
            return
        if success:
            sup.total_delivered = (sup.total_delivered or 0) + 1
        else:
            sup.total_failed = (sup.total_failed or 0) + 1
        sup.last_activity = datetime.utcnow()
    except Exception:
        logger.exception("record_supplier_delivery failed for supplier_id=%s", supplier_id)


def get_supplier_for_key(key: ProductKey, session: Session) -> Optional[Supplier]:
    """Return the Supplier associated with a ProductKey via its batch, or None."""
    if not key.batch_id:
        return None
    batch = session.get(InventoryBatch, key.batch_id)
    if not batch or not batch.supplier_id:
        return None
    return session.get(Supplier, batch.supplier_id)


def supplier_stats(supplier_id: int, session: Session) -> dict:
    """Return a stats dict for the auto-assign panel.

    Includes: available_stock (across all assigned products),
    total_delivered, total_failed, success_rate, last_activity.
    """
    sup = session.get(Supplier, supplier_id)
    if not sup:
        return {}

    # Count unreserved unsold keys across all batches for this supplier.
    available = (
        session.query(ProductKey)
        .join(InventoryBatch, ProductKey.batch_id == InventoryBatch.id)
        .filter(
            InventoryBatch.supplier_id == supplier_id,
            ProductKey.is_sold.is_(False),
            ProductKey.reservation_id.is_(None),
        )
        .count()
    )

    total = (sup.total_delivered or 0) + (sup.total_failed or 0)
    success_rate = (
        (sup.total_delivered / total * 100.0) if total else 100.0
    )

    # Count product assignments
    assignment_count = (
        session.query(SupplierProduct)
        .filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.is_active.is_(True),
        )
        .count()
    )

    return dict(
        available_stock=available,
        total_delivered=sup.total_delivered or 0,
        total_failed=sup.total_failed or 0,
        success_rate=success_rate,
        last_activity=sup.last_activity,
        priority=sup.priority,
        assignment_count=assignment_count,
    )
