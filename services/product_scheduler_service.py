"""V45 — Product Scheduler Service.

Manages scheduled product changes: publish, unpublish, price change, discount,
stock change. Executes pending schedules in a background job and keeps a full
history of executions.

Schedule types:
  publish        — set is_active=True at a given time
  unpublish      — set is_active=False at a given time
  price_change   — update price (and optionally sale_price)
  discount       — set sale_price for a duration
  stock_change   — set stock_count to a new value

Public API (sync — wrap in asyncio.to_thread from handlers):
  create_schedule(admin_id, product_id, schedule_type, execute_at,
                  payload, timezone_name, notes) -> ProductSchedule | None
  cancel_schedule(schedule_id, admin_id) -> bool
  get_schedule(schedule_id) -> dict | None
  list_schedules(product_id, status, page, per_page) -> dict
  get_upcoming(days, limit) -> list[dict]
  get_history(product_id, page, per_page) -> dict
  get_stats() -> dict
  process_due_schedules(context) -> int  (background job)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from database import get_db_session
from database.models import ProductSchedule, Product
from utils.audit import log_admin_action
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

VALID_TYPES = {"publish", "unpublish", "price_change", "discount", "stock_change"}


# ─── Create / Cancel ──────────────────────────────────────────────────────────

def create_schedule(admin_id: int, product_id: int, schedule_type: str,
                    execute_at: datetime, payload: Optional[dict] = None,
                    timezone_name: str = "UTC", notes: Optional[str] = None):
    """Create a new product schedule. Returns the created ProductSchedule or None."""
    if schedule_type not in VALID_TYPES:
        raise ValueError(f"Invalid schedule type: {schedule_type}")
    try:
        with get_db_session() as s:
            product = s.query(Product).get(product_id)
            if not product:
                return None
            sched = ProductSchedule(
                product_id=product_id,
                product_name_snapshot=product.name,
                admin_id=admin_id,
                schedule_type=schedule_type,
                execute_at=execute_at,
                payload_json=json.dumps(payload or {}),
                timezone_name=timezone_name,
                notes=notes,
                status="pending",
            )
            s.add(sched)
            s.commit()
            s.refresh(sched)
            log_admin_action(admin_id, "schedule_create",
                             target_type="product", target_id=product_id,
                             details=f"type={schedule_type} at={execute_at.isoformat()}",
                             module="product_scheduler")
            return sched
    except Exception:
        logger.exception("create_schedule failed pid=%s type=%s", product_id, schedule_type)
        return None


def cancel_schedule(schedule_id: int, admin_id: int) -> bool:
    """Cancel a pending schedule. Returns True if cancelled."""
    try:
        with get_db_session() as s:
            sched = s.query(ProductSchedule).get(schedule_id)
            if not sched or sched.status != "pending":
                return False
            sched.status = "cancelled"
            sched.cancelled_at = datetime.utcnow()
            s.commit()
            log_admin_action(admin_id, "schedule_cancel",
                             target_type="product_schedule", target_id=schedule_id,
                             module="product_scheduler")
            return True
    except Exception:
        logger.exception("cancel_schedule failed sid=%s", schedule_id)
        return False


def get_schedule(schedule_id: int) -> Optional[dict]:
    try:
        with get_db_session() as s:
            sched = s.query(ProductSchedule).get(schedule_id)
            if not sched:
                return None
            return _sched_dict(sched)
    except Exception:
        logger.exception("get_schedule failed sid=%s", schedule_id)
        return None


def _sched_dict(sched: ProductSchedule) -> dict:
    payload = {}
    try:
        payload = json.loads(sched.payload_json or "{}")
    except Exception:
        pass
    return {
        "id": sched.id,
        "product_id": sched.product_id,
        "product_name": sched.product_name_snapshot,
        "admin_id": sched.admin_id,
        "schedule_type": sched.schedule_type,
        "execute_at": sched.execute_at,
        "payload": payload,
        "timezone_name": sched.timezone_name,
        "notes": sched.notes,
        "status": sched.status,
        "executed_at": sched.executed_at,
        "cancelled_at": sched.cancelled_at,
        "result_message": sched.result_message,
        "created_at": sched.created_at,
    }


# ─── Query helpers ────────────────────────────────────────────────────────────

def list_schedules(product_id: Optional[int] = None,
                   status: Optional[str] = None,
                   page: int = 1, per_page: int = 20) -> dict:
    try:
        with get_db_session() as s:
            q = s.query(ProductSchedule).order_by(ProductSchedule.execute_at.asc())
            if product_id:
                q = q.filter(ProductSchedule.product_id == product_id)
            if status:
                q = q.filter(ProductSchedule.status == status)
            total = q.count()
            rows = q.offset((page - 1) * per_page).limit(per_page).all()
            return {
                "items": [_sched_dict(r) for r in rows],
                "total": total, "page": page, "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            }
    except Exception:
        logger.exception("list_schedules failed")
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 1}


def get_upcoming(days: int = 7, limit: int = 50) -> list[dict]:
    """Pending schedules due within the next N days."""
    try:
        cutoff = datetime.utcnow() + timedelta(days=days)
        with get_db_session() as s:
            rows = (s.query(ProductSchedule)
                    .filter(
                        ProductSchedule.status == "pending",
                        ProductSchedule.execute_at <= cutoff,
                        ProductSchedule.execute_at >= datetime.utcnow(),
                    )
                    .order_by(ProductSchedule.execute_at.asc())
                    .limit(limit).all())
            return [_sched_dict(r) for r in rows]
    except Exception:
        logger.exception("get_upcoming failed")
        return []


def get_history(product_id: Optional[int] = None,
                page: int = 1, per_page: int = 20) -> dict:
    try:
        with get_db_session() as s:
            q = (s.query(ProductSchedule)
                 .filter(ProductSchedule.status.in_(["executed", "failed", "cancelled"]))
                 .order_by(ProductSchedule.executed_at.desc().nullslast(),
                            ProductSchedule.created_at.desc()))
            if product_id:
                q = q.filter(ProductSchedule.product_id == product_id)
            total = q.count()
            rows = q.offset((page - 1) * per_page).limit(per_page).all()
            return {
                "items": [_sched_dict(r) for r in rows],
                "total": total, "page": page, "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            }
    except Exception:
        logger.exception("get_history failed")
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 1}


def get_stats() -> dict:
    try:
        with get_db_session() as s:
            pending = s.query(ProductSchedule).filter_by(status="pending").count()
            executed = s.query(ProductSchedule).filter_by(status="executed").count()
            failed = s.query(ProductSchedule).filter_by(status="failed").count()
            cancelled = s.query(ProductSchedule).filter_by(status="cancelled").count()
            # Due in next 24h
            due_soon = (s.query(ProductSchedule)
                        .filter(
                            ProductSchedule.status == "pending",
                            ProductSchedule.execute_at <= datetime.utcnow() + timedelta(hours=24),
                        ).count())
            # Overdue (past execute_at but still pending)
            overdue = (s.query(ProductSchedule)
                       .filter(
                           ProductSchedule.status == "pending",
                           ProductSchedule.execute_at < datetime.utcnow(),
                       ).count())
            return {
                "pending": pending, "executed": executed,
                "failed": failed, "cancelled": cancelled,
                "due_soon": due_soon, "overdue": overdue,
                "total": pending + executed + failed + cancelled,
            }
    except Exception:
        logger.exception("get_stats failed")
        return {"pending": 0, "executed": 0, "failed": 0, "cancelled": 0,
                "due_soon": 0, "overdue": 0, "total": 0}


# ─── Execution engine ─────────────────────────────────────────────────────────

def _execute_schedule(sched: ProductSchedule, s) -> tuple[bool, str]:
    """Apply the scheduled change. Returns (success, result_message)."""
    try:
        product = s.query(Product).get(sched.product_id)
        if not product:
            return False, "Product not found"
        payload = {}
        try:
            payload = json.loads(sched.payload_json or "{}")
        except Exception:
            pass

        t = sched.schedule_type
        if t == "publish":
            product.is_active = True
            return True, "Product published"
        elif t == "unpublish":
            product.is_active = False
            return True, "Product unpublished"
        elif t == "price_change":
            new_price = payload.get("price")
            if new_price is None:
                return False, "Missing 'price' in payload"
            old_price = product.price
            product.price = float(new_price)
            if "sale_price" in payload:
                product.sale_price = float(payload["sale_price"]) if payload["sale_price"] else None
            return True, f"Price changed {old_price:.2f} → {product.price:.2f}"
        elif t == "discount":
            sale_price = payload.get("sale_price")
            if sale_price is None:
                return False, "Missing 'sale_price' in payload"
            product.sale_price = float(sale_price)
            return True, f"Discount applied: sale_price={product.sale_price:.2f}"
        elif t == "stock_change":
            new_stock = payload.get("stock_count")
            if new_stock is None:
                return False, "Missing 'stock_count' in payload"
            product.stock_count = int(new_stock)
            return True, f"Stock updated to {product.stock_count}"
        else:
            return False, f"Unknown schedule type: {t}"
    except Exception as e:
        return False, str(e)


def _process_due_sync() -> int:
    """Synchronous core of the background job. Returns count executed."""
    now = datetime.utcnow()
    executed = 0
    try:
        with get_db_session() as s:
            due = (s.query(ProductSchedule)
                   .filter(ProductSchedule.status == "pending",
                           ProductSchedule.execute_at <= now)
                   .all())
            for sched in due:
                success, result = _execute_schedule(sched, s)
                sched.status = "executed" if success else "failed"
                sched.executed_at = datetime.utcnow()
                sched.result_message = result[:512]
                if success:
                    executed += 1
                    log_admin_action(
                        sched.admin_id, "schedule_executed",
                        target_type="product", target_id=sched.product_id,
                        details=f"type={sched.schedule_type} result={result}",
                        module="product_scheduler",
                    )
                else:
                    logger.warning("schedule %s failed: %s", sched.id, result)
            s.commit()
    except Exception:
        logger.exception("_process_due_sync failed")
    return executed


async def process_due_schedules(context) -> int:
    """Background job: execute all pending schedules whose execute_at has passed.

    Called by the job queue every 60 seconds.
    """
    return await asyncio.to_thread(_process_due_sync)
