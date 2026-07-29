"""Database & business integrity scanner (read-only detection).

Scans never modify data. Repairs are separate, admin-confirmed actions
in handlers/admin_integrity.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List

from sqlalchemy import func

from database import get_db_session
from database.models import (
    Order, OrderLifecycleStatus, OrderStatus, ProductKey,
    StockReservation, ReservationStatus, Transaction,
    DeliveryJob, InventoryBatch,
    IntegrityScan, IntegrityScanResult,
)

logger = logging.getLogger(__name__)

INFO, WARNING, CRITICAL = "INFO", "WARNING", "CRITICAL"


@dataclass
class CheckResult:
    name: str
    severity: str
    count: int
    explanation: str
    sample_ids: List[int]


# ── individual checks ────────────────────────────────────────────────────

def _check_paid_not_delivered(s) -> CheckResult:
    rows = (s.query(Order.id)
             .filter(Order.status == OrderStatus.PROCESSING,
                     Order.lifecycle_status == OrderLifecycleStatus.PAID)
             .limit(200).all())
    return CheckResult(
        "paid_not_delivered", WARNING, len(rows),
        "Orders marked PAID but never advanced to DELIVERED.",
        [r[0] for r in rows[:20]],
    )


def _check_key_multi_order(s) -> CheckResult:
    # ProductKey belonging to more than one order — should be impossible
    dup = (s.query(ProductKey.key_value, func.count(ProductKey.id).label("c"))
            .filter(ProductKey.order_id.isnot(None))
            .group_by(ProductKey.key_value)
            .having(func.count(ProductKey.id) > 1)
            .limit(200).all())
    return CheckResult(
        "duplicate_key_values_assigned", CRITICAL, len(dup),
        "Identical key_value assigned to multiple orders. Investigate manually.",
        [],
    )


def _check_sold_without_order(s) -> CheckResult:
    rows = (s.query(ProductKey.id)
             .filter(ProductKey.is_sold.is_(True),
                     ProductKey.order_id.is_(None))
             .limit(200).all())
    return CheckResult(
        "sold_without_order", WARNING, len(rows),
        "ProductKey rows flagged sold but not linked to an order.",
        [r[0] for r in rows[:20]],
    )


def _check_expired_active_reservations(s) -> CheckResult:
    rows = (s.query(StockReservation.id)
             .filter(StockReservation.status == ReservationStatus.ACTIVE,
                     StockReservation.expires_at < datetime.utcnow())
             .limit(200).all())
    return CheckResult(
        "expired_active_reservations", WARNING, len(rows),
        "Reservations still ACTIVE past their expiry — expiry job may be stalled.",
        [r[0] for r in rows[:20]],
    )


def _check_duplicate_txids(s) -> CheckResult:
    dup = (s.query(Transaction.txid, func.count(Transaction.id))
            .filter(Transaction.txid.isnot(None), Transaction.txid != "")
            .group_by(Transaction.txid)
            .having(func.count(Transaction.id) > 1)
            .limit(200).all())
    return CheckResult(
        "duplicate_transaction_txids", CRITICAL, len(dup),
        "Same provider TXID recorded on multiple transactions — possible double credit.",
        [],
    )


def _check_orphan_delivery_jobs(s) -> CheckResult:
    # Delivery jobs without a matching order row
    rows = (s.query(DeliveryJob.id)
             .outerjoin(Order, Order.id == DeliveryJob.order_id)
             .filter(Order.id.is_(None))
             .limit(200).all())
    return CheckResult(
        "orphan_delivery_jobs", WARNING, len(rows),
        "DeliveryJob rows referencing missing orders.",
        [r[0] for r in rows[:20]],
    )


def _check_stuck_processing(s) -> CheckResult:
    rows = (s.query(DeliveryJob.id)
             .filter(DeliveryJob.status == "PROCESSING",
                     DeliveryJob.started_at.isnot(None))
             .limit(200).all())
    return CheckResult(
        "delivery_jobs_processing", INFO, len(rows),
        "Delivery jobs currently in PROCESSING (informational).",
        [r[0] for r in rows[:20]],
    )


def _check_batch_quantity_drift(s) -> CheckResult:
    """Batch.quantity_imported vs actual count of ProductKey rows referencing it."""
    bad = []
    for b in s.query(InventoryBatch).limit(500).all():
        cnt = s.query(ProductKey).filter(ProductKey.batch_id == b.id).count()
        if cnt != int(b.quantity_imported or 0):
            bad.append(b.id)
            if len(bad) >= 20:
                break
    return CheckResult(
        "batch_quantity_drift", WARNING, len(bad),
        "Batch.quantity_imported does not equal actual linked ProductKey count.",
        bad,
    )


ALL_CHECKS: List[Callable] = [
    _check_paid_not_delivered,
    _check_key_multi_order,
    _check_sold_without_order,
    _check_expired_active_reservations,
    _check_duplicate_txids,
    _check_orphan_delivery_jobs,
    _check_stuck_processing,
    _check_batch_quantity_drift,
]


def run_scan(triggered_by: str = "manual", admin_id: int = None) -> IntegrityScan:
    with get_db_session() as s:
        scan = IntegrityScan(
            triggered_by=triggered_by, admin_id=admin_id,
            started_at=datetime.utcnow(),
        )
        s.add(scan); s.commit(); s.refresh(scan)
        scan_id = scan.id

    results: List[CheckResult] = []
    with get_db_session() as s:
        for fn in ALL_CHECKS:
            try:
                r = fn(s)
            except Exception as e:
                logger.exception("integrity check failed: %s", fn.__name__)
                r = CheckResult(fn.__name__, WARNING, 0,
                                f"check crashed: {e}", [])
            results.append(r)

    with get_db_session() as s:
        scan = s.get(IntegrityScan, scan_id)
        scan.total_checks = len(results)
        scan.total_issues = sum(1 for r in results if r.count > 0)
        scan.critical_count = sum(1 for r in results if r.severity == CRITICAL and r.count > 0)
        scan.warning_count = sum(1 for r in results if r.severity == WARNING and r.count > 0)
        scan.info_count = sum(1 for r in results if r.severity == INFO and r.count > 0)
        for r in results:
            s.add(IntegrityScanResult(
                scan_id=scan.id, check_name=r.name, severity=r.severity,
                count=r.count, explanation=r.explanation,
                sample_ids=json.dumps(r.sample_ids),
            ))
        scan.completed_at = datetime.utcnow()
        s.commit()
        s.refresh(scan)
        return scan
