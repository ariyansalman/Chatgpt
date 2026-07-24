"""Inventory Reservation UI helpers — V23.

Wraps the existing ``services.inventory`` engine with:
  - Feature-flag checks
  - Per-user reservation limits
  - Admin manager queries
  - Countdown formatting

The UI-level reservation (created from the product page "Reserve" button)
is a short-lived StockReservation whose purpose is to give the user a
countdown window before they commit to checkout. On checkout it is
released so the payment flow can create its own reservation normally.

BotConfig keys consumed:
    irs_enabled                — master bool toggle
    irs_status                 — "enabled" / "maintenance" / "disabled"
    irs_allow_manual_release   — bool (users may cancel their own reservation)
    irs_max_per_user           — int (0 = unlimited)
    inventory_reservation_ttl_minutes  — already in bot_config (shared with checkout)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from database import get_db_session
from database.models import (
    StockReservation, ReservationStatus,
    Product, User,
)
from services.inventory import reserve, release, ReservationError, KEY_BACKED_TYPES
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def feature_status() -> str:
    return cfg.get_str("irs_status", "enabled").lower()


def is_enabled() -> bool:
    if not cfg.get_bool("irs_enabled", True):
        return False
    return feature_status() == "enabled"


def allow_manual_release() -> bool:
    return cfg.get_bool("irs_allow_manual_release", True)


def max_per_user() -> int:
    """0 = unlimited."""
    return max(0, cfg.get_int("irs_max_per_user", 1))


def ttl_minutes() -> int:
    return max(1, cfg.get_int("inventory_reservation_ttl_minutes", 15))


# ─────────────────────────────────────────────────────────────────────────
# Countdown display
# ─────────────────────────────────────────────────────────────────────────

def format_countdown(expires_at: datetime) -> str:
    """Return a 'MM:SS' countdown string (or 'Expired' if past)."""
    now = datetime.utcnow()
    remaining = expires_at - now
    total_secs = int(remaining.total_seconds())
    if total_secs <= 0:
        return "Expired"
    mins, secs = divmod(total_secs, 60)
    return f"{mins:02d}:{secs:02d}"


def format_time_remaining(expires_at: datetime) -> str:
    """Human-readable form: '14 minutes 32 seconds remaining'."""
    now = datetime.utcnow()
    remaining = expires_at - now
    total_secs = int(remaining.total_seconds())
    if total_secs <= 0:
        return "Expired"
    mins, secs = divmod(total_secs, 60)
    if mins > 0:
        return f"{mins}m {secs}s remaining"
    return f"{secs}s remaining"


# ─────────────────────────────────────────────────────────────────────────
# Stock display helpers
# ─────────────────────────────────────────────────────────────────────────

def get_stock_summary(product_id: int) -> dict:
    """Return dict with available, reserved, remaining for a product."""
    try:
        with get_db_session() as s:
            p = s.query(Product).filter_by(id=product_id).first()
            if not p:
                return {"available": 0, "reserved": 0, "remaining": 0}

            if p.product_type in KEY_BACKED_TYPES:
                from database.models import ProductKey
                total_keys = (
                    s.query(ProductKey)
                     .filter_by(product_id=product_id, is_sold=False)
                     .count()
                )
                reserved = (
                    s.query(StockReservation)
                     .filter(
                         StockReservation.product_id == product_id,
                         StockReservation.status == ReservationStatus.ACTIVE,
                     )
                     .with_entities(
                         StockReservation.quantity
                     )
                     .all()
                )
                reserved_qty = sum(r.quantity for r in reserved)
                remaining = max(0, total_keys - reserved_qty)
                return {
                    "available": total_keys,
                    "reserved": reserved_qty,
                    "remaining": remaining,
                }
            else:
                # FILE / other types
                stock = p.stock_count or 0
                reserved_rows = (
                    s.query(StockReservation)
                     .filter(
                         StockReservation.product_id == product_id,
                         StockReservation.status == ReservationStatus.ACTIVE,
                     )
                     .all()
                )
                reserved_qty = sum(r.quantity for r in reserved_rows)
                remaining = max(0, stock - reserved_qty)
                return {
                    "available": stock,
                    "reserved": reserved_qty,
                    "remaining": remaining,
                }
    except Exception:
        logger.exception("irs: get_stock_summary failed")
        return {"available": 0, "reserved": 0, "remaining": 0}


# ─────────────────────────────────────────────────────────────────────────
# Per-user reservation helpers
# ─────────────────────────────────────────────────────────────────────────

def get_user_active_reservation(
    user_pk: int, product_id: int
) -> Optional[StockReservation]:
    """Return the user's ACTIVE StockReservation for a product, or None.

    Cleans up expired entries inline (sets status=EXPIRED).
    """
    try:
        with get_db_session() as s:
            rows = (
                s.query(StockReservation)
                 .filter(
                     StockReservation.user_id == user_pk,
                     StockReservation.product_id == product_id,
                     StockReservation.status == ReservationStatus.ACTIVE,
                 )
                 .order_by(StockReservation.created_at.desc())
                 .all()
            )
            now = datetime.utcnow()
            active = None
            for r in rows:
                if r.expires_at and r.expires_at < now:
                    r.status = ReservationStatus.EXPIRED
                    r.released_at = now
                else:
                    active = r
                    break
            if rows:
                s.commit()
            if active:
                # Detach-safe snapshot
                return s.query(StockReservation).filter_by(id=active.id).first()
            return None
    except Exception:
        logger.exception("irs: get_user_active_reservation failed")
        return None


def count_user_active_reservations(user_pk: int) -> int:
    """Count all ACTIVE reservations across all products for this user."""
    try:
        with get_db_session() as s:
            now = datetime.utcnow()
            return (
                s.query(StockReservation)
                 .filter(
                     StockReservation.user_id == user_pk,
                     StockReservation.status == ReservationStatus.ACTIVE,
                     StockReservation.expires_at > now,
                 )
                 .count()
            )
    except Exception:
        logger.exception("irs: count_user_active_reservations failed")
        return 0


def get_user_pk(telegram_id: int) -> Optional[int]:
    """Resolve telegram_id → user.id (PK). Returns None if user not found."""
    try:
        with get_db_session() as s:
            u = s.query(User).filter_by(telegram_id=telegram_id).first()
            return u.id if u else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────
# Create / cancel UI reservations
# ─────────────────────────────────────────────────────────────────────────

def create_ui_reservation(
    user_pk: int, product_id: int, quantity: int = 1
) -> tuple[Optional[StockReservation], Optional[str]]:
    """Create a UI-level reservation. Returns (reservation, None) or (None, error_msg)."""
    if not is_enabled():
        return None, "Inventory reservation is currently unavailable."

    # Check existing reservation for this product
    existing = get_user_active_reservation(user_pk, product_id)
    if existing:
        return existing, None   # already reserved — return it

    # Check max-per-user limit across all products
    cap = max_per_user()
    if cap > 0:
        total = count_user_active_reservations(user_pk)
        if total >= cap:
            return None, (
                f"You already have {total} active reservation(s). "
                f"Maximum is {cap}. Cancel one to reserve another product."
            )

    try:
        res = reserve(user_pk, product_id, quantity)
        return res, None
    except ReservationError as e:
        return None, str(e)
    except Exception as e:
        logger.exception("irs: create_ui_reservation failed")
        return None, "Could not create reservation. Please try again."


def cancel_ui_reservation(reservation_id: int, user_pk: int) -> tuple[bool, str]:
    """Cancel the user's own UI reservation. Returns (success, message)."""
    if not allow_manual_release():
        return False, "Manual cancellation is not enabled."
    try:
        with get_db_session() as s:
            r = s.query(StockReservation).filter(
                StockReservation.id == reservation_id,
                StockReservation.user_id == user_pk,
                StockReservation.status == ReservationStatus.ACTIVE,
            ).first()
            if not r:
                return False, "Reservation not found or already released."
            from services.inventory import release_locked
            release_locked(s, r)
            s.commit()
        return True, "Reservation cancelled. Stock has been released."
    except Exception:
        logger.exception("irs: cancel_ui_reservation failed")
        return False, "Could not cancel reservation. Please try again."


def admin_release_reservation(reservation_id: int) -> tuple[bool, str]:
    """Admin force-release a reservation."""
    ok = release(reservation_id)
    if ok:
        return True, "Reservation released."
    return False, "Reservation not found or already closed."


# ─────────────────────────────────────────────────────────────────────────
# Admin manager queries
# ─────────────────────────────────────────────────────────────────────────

_ADMIN_PAGE_SIZE = 10


def admin_get_active_reservations(page: int = 0) -> tuple[list, int]:
    """Paginated list of ACTIVE reservations for admin manager."""
    try:
        with get_db_session() as s:
            now = datetime.utcnow()
            q = (
                s.query(StockReservation)
                 .filter(
                     StockReservation.status == ReservationStatus.ACTIVE,
                     StockReservation.expires_at > now,
                 )
                 .order_by(StockReservation.expires_at.asc())
            )
            total = q.count()
            rows = q.offset(page * _ADMIN_PAGE_SIZE).limit(_ADMIN_PAGE_SIZE).all()
            result = []
            for r in rows:
                p = s.query(Product).filter_by(id=r.product_id).first()
                u = s.query(User).filter_by(id=r.user_id).first()
                result.append({
                    "id":           r.id,
                    "product_name": p.name if p else f"#{r.product_id}",
                    "product_id":   r.product_id,
                    "user_name":    (f"@{u.username}" if (u and u.username)
                                    else (str(u.telegram_id) if u else f"uid:{r.user_id}")),
                    "quantity":     r.quantity,
                    "expires_at":   r.expires_at,
                    "countdown":    format_countdown(r.expires_at),
                })
            return result, total
    except Exception:
        logger.exception("irs: admin_get_active_reservations failed")
        return [], 0


def get_stats() -> dict:
    """Admin dashboard statistics."""
    stats = {
        "active":   0,
        "expired":  0,
        "released": 0,
        "consumed": 0,
        "total":    0,
    }
    try:
        with get_db_session() as s:
            from sqlalchemy import func as _f
            rows = (
                s.query(StockReservation.status,
                        _f.count(StockReservation.id).label("cnt"))
                 .group_by(StockReservation.status)
                 .all()
            )
            for status, cnt in rows:
                key = status.value if hasattr(status, "value") else str(status)
                if key in stats:
                    stats[key] = cnt
                stats["total"] += cnt
    except Exception:
        logger.exception("irs: get_stats failed")
    return stats
