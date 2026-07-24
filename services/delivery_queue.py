"""Delivery queue with idempotent inventory assignment + retry.

The queue centralises fulfilment so that:
- one order == one active delivery job at any time
  (Postgres partial unique index enforces this)
- inventory is assigned INSIDE a locked DB transaction, then Telegram
  network calls happen OUTSIDE that transaction
- a duplicate job execution never allocates new inventory when the order
  already has assigned ProductKey rows
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlalchemy.exc import IntegrityError

from database import get_db_session
from database.models import (
    DeliveryJob, OrderItem, ProductKey, OrderLifecycleStatus,
)
from services import order_lifecycle

logger = logging.getLogger(__name__)

RETRYABLE = {"telegram_timeout", "telegram_network", "temporary"}
BACKOFF_MIN = [1, 5, 15, 60, 240]   # minutes; index by attempts


def enqueue(order_id: int, max_attempts: int = 5) -> DeliveryJob:
    """Create a PENDING job for an order, or return the existing active one."""
    with get_db_session() as s:
        active = (
            s.query(DeliveryJob)
             .filter(DeliveryJob.order_id == order_id,
                     DeliveryJob.status.in_(("PENDING", "PROCESSING",
                                             "RETRY_SCHEDULED")))
             .first()
        )
        if active:
            return active
        job = DeliveryJob(
            order_id=order_id,
            status="PENDING",
            max_attempts=max_attempts,
            attempts=0,
            created_at=datetime.utcnow(),
        )
        s.add(job)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return (s.query(DeliveryJob)
                     .filter_by(order_id=order_id)
                     .order_by(DeliveryJob.id.desc()).first())
        s.refresh(job)
        return job


def _lock_job(s, job_id: int) -> Optional[DeliveryJob]:
    q = s.query(DeliveryJob).filter(DeliveryJob.id == job_id)
    if s.bind and s.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    return q.first()


def assign_inventory(job_id: int) -> Tuple[bool, List[int]]:
    """Idempotently assign ProductKey rows to the job's order.

    Returns (assigned_now, key_ids). If inventory is already assigned,
    returns (False, existing_key_ids) — no new allocation happens.
    """
    with get_db_session() as s:
        job = _lock_job(s, job_id)
        if job is None:
            return False, []
        # Already assigned? reuse.
        existing = (s.query(ProductKey.id)
                     .filter(ProductKey.order_id == job.order_id).all())
        if existing:
            job.inventory_assigned = True
            s.commit()
            return False, [r[0] for r in existing]

        assigned: List[int] = []
        for item in s.query(OrderItem).filter_by(order_id=job.order_id).all():
            need = int(item.quantity or 0)
            if need <= 0:
                continue
            q = (s.query(ProductKey)
                  .filter(ProductKey.product_id == item.product_id,
                          ProductKey.variant_id == item.variant_id,
                          ProductKey.is_sold.is_(False),
                          ProductKey.order_id.is_(None)))
            if s.bind and s.bind.dialect.name == "postgresql":
                q = q.with_for_update(skip_locked=True)
            rows = q.limit(need).all()
            if len(rows) < need:
                s.rollback()
                raise RuntimeError(
                    f"insufficient stock for product {item.product_id}: "
                    f"need {need}, got {len(rows)}"
                )
            for k in rows:
                k.order_id = job.order_id
                k.is_sold = True
                k.sold_at = datetime.utcnow()
                # Cost snapshot for profit tracking
                if k.cost_per_unit_snapshot is None and k.batch_id:
                    from database.models import InventoryBatch
                    b = s.get(InventoryBatch, k.batch_id)
                    if b:
                        k.cost_per_unit_snapshot = b.cost_per_unit
                assigned.append(k.id)
        # Persist per-item cost snapshots
        for item in s.query(OrderItem).filter_by(order_id=job.order_id).all():
            keys = [k for k in s.query(ProductKey)
                          .filter_by(order_id=job.order_id,
                                     product_id=item.product_id,
                                     variant_id=item.variant_id).all()]
            if keys and item.unit_cost_snapshot is None:
                costs = [k.cost_per_unit_snapshot or 0.0 for k in keys]
                item.unit_cost_snapshot = (sum(costs) / len(costs)) if costs else 0.0
                item.total_cost_snapshot = sum(costs)
        job.inventory_assigned = True
        job.status = "PROCESSING"
        job.started_at = datetime.utcnow()
        job.attempts += 1
        s.commit()
        return True, assigned


def mark_delivered(job_id: int) -> None:
    with get_db_session() as s:
        job = _lock_job(s, job_id)
        if job is None:
            return
        job.status = "DELIVERED"
        job.completed_at = datetime.utcnow()
        s.commit()
        try:
            order_lifecycle.transition(
                job.order_id,
                OrderLifecycleStatus.DELIVERED,
                actor_type="system",
                reason="delivery_queue_success",
            )
        except Exception:
            logger.exception("lifecycle transition failed for order %s", job.order_id)


def mark_failed(job_id: int, category: str, summary: str) -> None:
    """Record a failure; either schedule retry or mark FAILED permanently."""
    with get_db_session() as s:
        job = _lock_job(s, job_id)
        if job is None:
            return
        job.last_error_category = category[:48]
        job.last_error_summary = (summary or "")[:500]
        if category in RETRYABLE and job.attempts < job.max_attempts:
            mins = BACKOFF_MIN[min(job.attempts, len(BACKOFF_MIN) - 1)]
            job.status = "RETRY_SCHEDULED"
            job.next_retry_at = datetime.utcnow() + timedelta(minutes=mins)
        else:
            job.status = "FAILED"
        s.commit()


def due_retries(limit: int = 25) -> List[int]:
    with get_db_session() as s:
        rows = (s.query(DeliveryJob.id)
                 .filter(DeliveryJob.status == "RETRY_SCHEDULED",
                         DeliveryJob.next_retry_at <= datetime.utcnow())
                 .order_by(DeliveryJob.next_retry_at.asc())
                 .limit(limit).all())
        return [r[0] for r in rows]


def cancel(job_id: int, admin_id: int) -> bool:
    with get_db_session() as s:
        job = _lock_job(s, job_id)
        if job is None or job.status in ("DELIVERED", "CANCELLED"):
            return False
        job.status = "CANCELLED"
        job.completed_at = datetime.utcnow()
        s.commit()
        return True
