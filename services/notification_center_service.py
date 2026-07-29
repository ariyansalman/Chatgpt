"""V37 — Notification Center Service.

Creates, stores, and manages AdminNotification records.
Called by services/notifications.py whenever an event fires.

Public API
──────────
create(event_type, title, body, category, severity, source_type, source_id) → AdminNotification|None
get_unread_count() → int
cleanup_old() → int   (respects retention settings)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from database import get_db_session
from database.models import AdminNotification
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# Maps event_type → category for auto-classification
_EVENT_CATEGORY = {
    "new_user":              "users",
    "new_order":             "orders",
    "payment_success":       "payments",
    "payment_failed":        "payments",
    "payment_pending":       "payments",
    "deposit":               "payments",
    "withdrawal_request":    "withdrawals",
    "withdrawal_approved":   "withdrawals",
    "withdrawal_rejected":   "withdrawals",
    "product_delivered":     "orders",
    "refund_request":        "payments",
    "support_ticket":        "support",
    "low_stock":             "products",
    "out_of_stock":          "products",
    "coupon_used":           "payments",
    "referral_reward":       "users",
    "broadcast_done":        "system",
    "fraud_alert":           "fraud",
    "api_failure":           "api",
    "webhook_failure":       "api",
    "db_error":              "system",
    "tg_api_error":          "api",
    "system_warning":        "system",
    # Legacy events from services/notifications.py
    "new_order":             "orders",
    "manual_payment":        "payments",
    "dispute":               "support",
    "low_stock":             "products",
    "refund":                "payments",
    "ticket_reply":          "support",
    "subscription":          "payments",
    "sla_warning":           "support",
    "sla_breach":            "support",
    # Enterprise Admin Notification System events
    "payment_expired":       "payments",
    "payment_reversed":      "payments",
    "order_delivered":       "orders",
}

# Maps event_type → BotConfig key for per-event enable check
_EVENT_ENABLE_KEY = {
    "new_user":              "notif_nc_new_user",
    "new_order":             "notif_nc_new_order",
    "payment_success":       "notif_nc_payment_success",
    "payment_failed":        "notif_nc_payment_failed",
    "payment_pending":       "notif_nc_payment_pending",
    "deposit":               "notif_nc_deposit",
    "withdrawal_request":    "notif_nc_withdrawal_request",
    "withdrawal_approved":   "notif_nc_withdrawal_approved",
    "withdrawal_rejected":   "notif_nc_withdrawal_rejected",
    "product_delivered":     "notif_nc_product_delivered",
    "refund_request":        "notif_nc_refund_request",
    "refund":                "notif_nc_refund_request",
    "support_ticket":        "notif_nc_support_ticket",
    "ticket_reply":          "notif_nc_support_ticket",
    "low_stock":             "notif_nc_low_stock",
    "out_of_stock":          "notif_nc_out_of_stock",
    "coupon_used":           "notif_nc_coupon_used",
    "referral_reward":       "notif_nc_referral_reward",
    "broadcast_done":        "notif_nc_broadcast_done",
    "fraud_alert":           "notif_nc_fraud_alert",
    "api_failure":           "notif_nc_api_failure",
    "webhook_failure":       "notif_nc_webhook_failure",
    "db_error":              "notif_nc_db_error",
    "tg_api_error":          "notif_nc_tg_api_error",
    "system_warning":        "notif_nc_system_warning",
    "manual_payment":        "notif_nc_payment_pending",
    "dispute":               "notif_nc_support_ticket",
    "subscription":          "notif_nc_payment_success",
    "sla_warning":           "notif_nc_support_ticket",
    "sla_breach":            "notif_nc_support_ticket",
    # Enterprise Admin Notification System events
    "payment_expired":       "notif_nc_payment_expired",
    "payment_reversed":      "notif_nc_payment_reversed",
    "order_delivered":       "notif_nc_order_delivered",
}


def _is_enabled() -> bool:
    status = cfg.get("notification_center_status", "enabled")
    return status == "enabled"


def _event_allowed(event_type: str) -> bool:
    """Return True if notifications for this event_type are enabled."""
    key = _EVENT_ENABLE_KEY.get(event_type)
    if key is None:
        return True  # unknown events always allowed
    return cfg.get_bool(key, True)


def create(
    event_type: str,
    title: str,
    body: str,
    category: Optional[str] = None,
    severity: str = "push",
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    admin_telegram_id: Optional[int] = None,
) -> Optional[AdminNotification]:
    """Create and store an AdminNotification record.

    Returns the created record, or None if the notification center is
    disabled, the feature is in maintenance, or the event is suppressed.
    Failures are swallowed — notifications must never break business flow.
    """
    try:
        if not _is_enabled():
            return None
        if not _event_allowed(event_type):
            return None

        cat = category or _EVENT_CATEGORY.get(event_type, "system")
        # Enforce max notifications cap
        max_count = cfg.get_int("notification_center_max", 1000)
        with get_db_session() as s:
            current = s.query(AdminNotification).count()
            if current >= max_count:
                # Delete oldest non-pinned notification to make room
                oldest = (s.query(AdminNotification)
                          .filter(AdminNotification.is_pinned == False)  # noqa: E712
                          .order_by(AdminNotification.created_at.asc())
                          .first())
                if oldest:
                    s.delete(oldest)
                    s.flush()

            notif = AdminNotification(
                event_type=event_type[:64],
                category=cat[:32],
                severity=severity[:16],
                title=title[:255],
                body=body,
                source_type=(source_type or None) and str(source_type)[:32],
                source_id=(source_id is not None) and str(source_id)[:64] or None,
                admin_telegram_id=admin_telegram_id,
                created_at=datetime.utcnow(),
            )
            s.add(notif)
            s.commit()
            s.refresh(notif)
            return notif
    except Exception:
        logger.exception("notification_center_service.create failed for event=%s", event_type)
        return None


def get_unread_count(admin_telegram_id: Optional[int] = None) -> int:
    """Return count of unread, non-archived notifications."""
    try:
        with get_db_session() as s:
            q = (s.query(AdminNotification)
                 .filter(
                     AdminNotification.is_read == False,      # noqa: E712
                     AdminNotification.is_archived == False,  # noqa: E712
                 ))
            if admin_telegram_id is not None:
                q = q.filter(
                    (AdminNotification.admin_telegram_id == admin_telegram_id) |
                    (AdminNotification.admin_telegram_id.is_(None))
                )
            return q.count()
    except Exception:
        return 0


def mark_read(notification_id: int) -> bool:
    """Mark a notification as read. Returns True on success."""
    try:
        with get_db_session() as s:
            n = s.query(AdminNotification).filter_by(id=notification_id).first()
            if n:
                n.is_read = True
                n.read_at = datetime.utcnow()
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("mark_read failed for id=%s", notification_id)
        return False


def mark_all_read() -> int:
    """Mark all unread notifications as read. Returns count updated."""
    try:
        with get_db_session() as s:
            now = datetime.utcnow()
            rows = (s.query(AdminNotification)
                    .filter(AdminNotification.is_read == False)  # noqa: E712
                    .all())
            for r in rows:
                r.is_read = True
                r.read_at = now
            s.commit()
            return len(rows)
    except Exception:
        logger.exception("mark_all_read failed")
        return 0


def delete_notification(notification_id: int) -> bool:
    """Permanently delete a notification. Returns True on success."""
    try:
        with get_db_session() as s:
            n = s.query(AdminNotification).filter_by(id=notification_id).first()
            if n:
                s.delete(n)
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("delete_notification failed for id=%s", notification_id)
        return False


def delete_all(category: Optional[str] = None) -> int:
    """Delete all notifications (optionally filtered by category). Returns count."""
    try:
        with get_db_session() as s:
            q = s.query(AdminNotification).filter(AdminNotification.is_pinned == False)  # noqa: E712
            if category:
                q = q.filter(AdminNotification.category == category)
            rows = q.all()
            count = len(rows)
            for r in rows:
                s.delete(r)
            s.commit()
            return count
    except Exception:
        logger.exception("delete_all failed")
        return 0


def toggle_pin(notification_id: int) -> Optional[bool]:
    """Toggle pin status. Returns new pin value or None on failure."""
    try:
        with get_db_session() as s:
            n = s.query(AdminNotification).filter_by(id=notification_id).first()
            if n:
                n.is_pinned = not n.is_pinned
                s.commit()
                return n.is_pinned
        return None
    except Exception:
        logger.exception("toggle_pin failed for id=%s", notification_id)
        return None


def archive_notification(notification_id: int) -> bool:
    """Archive a notification. Returns True on success."""
    try:
        with get_db_session() as s:
            n = s.query(AdminNotification).filter_by(id=notification_id).first()
            if n:
                n.is_archived = True
                n.archived_at = datetime.utcnow()
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("archive_notification failed for id=%s", notification_id)
        return False


def cleanup_old() -> int:
    """Delete notifications older than the retention period (if auto-delete is on).
    Returns count deleted.
    """
    try:
        if not cfg.get_bool("notification_center_auto_delete", False):
            return 0
        days = cfg.get_int("notification_center_retention_days", 30)
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db_session() as s:
            rows = (s.query(AdminNotification)
                    .filter(
                        AdminNotification.created_at < cutoff,
                        AdminNotification.is_pinned == False,  # noqa: E712
                    ).all())
            count = len(rows)
            for r in rows:
                s.delete(r)
            s.commit()
            return count
    except Exception:
        logger.exception("cleanup_old failed")
        return 0


def get_stats() -> dict:
    """Return notification center statistics."""
    try:
        with get_db_session() as s:
            total    = s.query(AdminNotification).count()
            unread   = s.query(AdminNotification).filter_by(is_read=False,  is_archived=False).count()
            pinned   = s.query(AdminNotification).filter_by(is_pinned=True).count()
            archived = s.query(AdminNotification).filter_by(is_archived=True).count()
            return {"total": total, "unread": unread, "pinned": pinned, "archived": archived}
    except Exception:
        return {"total": 0, "unread": 0, "pinned": 0, "archived": 0}
