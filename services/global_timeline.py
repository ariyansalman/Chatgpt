"""V42 — Global Activity Timeline service.

Records every important system-wide action into ``global_activity_entries``.

This is a separate, dedicated audit table from the existing ``activity_logs``
(which is user-centric account history) and ``admin_audit_logs`` (which only
records admin actions).  The Global Activity Timeline captures everything:
user events, admin events, system events.

All writes are best-effort — they MUST NOT raise exceptions to callers.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from database import get_db_session
from database.models import GlobalActivityEntry

logger = logging.getLogger(__name__)

# ─── Category constants ───────────────────────────────────────────────────────
CAT_USER     = "user"
CAT_WALLET   = "wallet"
CAT_ORDER    = "order"
CAT_PRODUCT  = "product"
CAT_COUPON   = "coupon"
CAT_BROADCAST= "broadcast"
CAT_FLASH    = "flash_sale"
CAT_REFERRAL = "referral"
CAT_ADMIN    = "admin"
CAT_API      = "api"
CAT_SETTINGS = "settings"
CAT_MODULE   = "module"
CAT_SYSTEM   = "system"

CATEGORY_LABELS = {
    CAT_USER:     "👤 User",
    CAT_WALLET:   "💰 Wallet",
    CAT_ORDER:    "🧾 Order",
    CAT_PRODUCT:  "📦 Product",
    CAT_COUPON:   "🎟 Coupon",
    CAT_BROADCAST:"📢 Broadcast",
    CAT_FLASH:    "⚡ Flash Sale",
    CAT_REFERRAL: "👥 Referral",
    CAT_ADMIN:    "🔐 Admin",
    CAT_API:      "🔌 API",
    CAT_SETTINGS: "⚙️ Settings",
    CAT_MODULE:   "🧩 Module",
    CAT_SYSTEM:   "🖥 System",
}


# ─── Record helper ────────────────────────────────────────────────────────────

def record(
    action: str,
    category: str,
    description: str,
    *,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    admin_telegram_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    status: str = "success",
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Best-effort write of a global activity entry.  Never raises."""
    try:
        with get_db_session() as s:
            entry = GlobalActivityEntry(
                user_id=user_id,
                username=(username or "")[:64] if username else None,
                admin_telegram_id=admin_telegram_id,
                action=action[:64],
                category=category[:32],
                description=description[:2000],
                ip_address=(ip_address or "")[:45] if ip_address else None,
                status=status[:16],
                ref_type=(ref_type or "")[:32] if ref_type else None,
                ref_id=(str(ref_id) or "")[:64] if ref_id else None,
                extra=json.dumps(extra) if extra else None,
            )
            s.add(entry)
            s.commit()
    except Exception:
        logger.debug("global_timeline: record failed", exc_info=True)


# ─── Convenience helpers (call from other handlers) ──────────────────────────

def record_user_registration(user_id: int, username: Optional[str]) -> None:
    record("user_registration", CAT_USER,
           f"New user registered: @{username or user_id}",
           user_id=user_id, username=username)


def record_user_login(user_id: int, username: Optional[str]) -> None:
    record("user_login", CAT_USER,
           f"User session started: @{username or user_id}",
           user_id=user_id, username=username)


def record_wallet_deposit(user_id: int, username: Optional[str],
                          amount: float, currency: str = "USD") -> None:
    record("wallet_deposit", CAT_WALLET,
           f"Deposit {amount:.2f} {currency} by @{username or user_id}",
           user_id=user_id, username=username)


def record_wallet_withdrawal(user_id: int, username: Optional[str],
                             amount: float, currency: str = "USD") -> None:
    record("wallet_withdrawal", CAT_WALLET,
           f"Withdrawal {amount:.2f} {currency} by @{username or user_id}",
           user_id=user_id, username=username)


def record_purchase(user_id: int, username: Optional[str],
                    order_id: int, amount: float) -> None:
    record("purchase", CAT_ORDER,
           f"Purchase ${amount:.2f} — order #{order_id} by @{username or user_id}",
           user_id=user_id, username=username,
           ref_type="order", ref_id=str(order_id))


def record_refund(user_id: int, username: Optional[str],
                  order_id: int, amount: float) -> None:
    record("refund", CAT_ORDER,
           f"Refund ${amount:.2f} — order #{order_id} by @{username or user_id}",
           user_id=user_id, username=username,
           ref_type="order", ref_id=str(order_id))


def record_order_created(user_id: int, username: Optional[str], order_id: int) -> None:
    record("order_created", CAT_ORDER,
           f"Order #{order_id} created by @{username or user_id}",
           user_id=user_id, username=username,
           ref_type="order", ref_id=str(order_id))


def record_order_completed(user_id: int, username: Optional[str], order_id: int) -> None:
    record("order_completed", CAT_ORDER,
           f"Order #{order_id} completed for @{username or user_id}",
           user_id=user_id, username=username,
           ref_type="order", ref_id=str(order_id))


def record_product_created(admin_tg_id: int, product_id: int, name: str) -> None:
    record("product_created", CAT_PRODUCT,
           f"Product '{name}' (#{product_id}) created",
           admin_telegram_id=admin_tg_id,
           ref_type="product", ref_id=str(product_id))


def record_product_edited(admin_tg_id: int, product_id: int, name: str) -> None:
    record("product_edited", CAT_PRODUCT,
           f"Product '{name}' (#{product_id}) edited",
           admin_telegram_id=admin_tg_id,
           ref_type="product", ref_id=str(product_id))


def record_product_deleted(admin_tg_id: int, product_id: int, name: str) -> None:
    record("product_deleted", CAT_PRODUCT,
           f"Product '{name}' (#{product_id}) deleted",
           admin_telegram_id=admin_tg_id,
           ref_type="product", ref_id=str(product_id))


def record_coupon_created(admin_tg_id: int, code: str) -> None:
    record("coupon_created", CAT_COUPON,
           f"Coupon '{code}' created",
           admin_telegram_id=admin_tg_id)


def record_coupon_used(user_id: int, username: Optional[str], code: str) -> None:
    record("coupon_used", CAT_COUPON,
           f"Coupon '{code}' used by @{username or user_id}",
           user_id=user_id, username=username)


def record_broadcast_sent(admin_tg_id: int, recipient_count: int) -> None:
    record("broadcast_sent", CAT_BROADCAST,
           f"Broadcast sent to {recipient_count} users",
           admin_telegram_id=admin_tg_id)


def record_flash_sale_started(admin_tg_id: int, sale_id: int, name: str) -> None:
    record("flash_sale_started", CAT_FLASH,
           f"Flash sale '{name}' (#{sale_id}) started",
           admin_telegram_id=admin_tg_id,
           ref_type="flash_sale", ref_id=str(sale_id))


def record_flash_sale_ended(sale_id: int, name: str) -> None:
    record("flash_sale_ended", CAT_FLASH,
           f"Flash sale '{name}' (#{sale_id}) ended",
           ref_type="flash_sale", ref_id=str(sale_id))


def record_referral_reward(user_id: int, username: Optional[str], amount: float) -> None:
    record("referral_reward", CAT_REFERRAL,
           f"Referral reward ${amount:.2f} to @{username or user_id}",
           user_id=user_id, username=username)


def record_admin_login(admin_tg_id: int) -> None:
    record("admin_login", CAT_ADMIN,
           f"Admin #{admin_tg_id} authenticated",
           admin_telegram_id=admin_tg_id)


def record_admin_action(admin_tg_id: int, action: str, details: str) -> None:
    record("admin_action", CAT_ADMIN,
           f"Admin #{admin_tg_id}: {details}",
           admin_telegram_id=admin_tg_id,
           extra={"action_key": action})


def record_api_change(admin_tg_id: int, details: str) -> None:
    record("api_change", CAT_API,
           details,
           admin_telegram_id=admin_tg_id)


def record_settings_change(admin_tg_id: int, key: str, old_val: str, new_val: str) -> None:
    record("settings_change", CAT_SETTINGS,
           f"Setting '{key}' changed",
           admin_telegram_id=admin_tg_id,
           extra={"key": key, "old": old_val, "new": new_val})


def record_module_change(admin_tg_id: int, slug: str, old_status: str, new_status: str) -> None:
    record("module_change", CAT_MODULE,
           f"Module '{slug}' status changed: {old_status} → {new_status}",
           admin_telegram_id=admin_tg_id,
           extra={"slug": slug, "old": old_status, "new": new_status})


def record_database_backup(admin_tg_id: int, details: str = "") -> None:
    record("database_backup", CAT_SYSTEM,
           f"Database backup created. {details}".strip(),
           admin_telegram_id=admin_tg_id)


def record_restore(admin_tg_id: int, details: str = "") -> None:
    record("restore", CAT_SYSTEM,
           f"Database restore performed. {details}".strip(),
           admin_telegram_id=admin_tg_id)


def record_system_error(details: str, extra: Optional[dict] = None) -> None:
    record("system_error", CAT_SYSTEM,
           details[:2000],
           status="failed",
           extra=extra)


# ─── Query helpers ────────────────────────────────────────────────────────────

def get_timeline(
    *,
    page: int = 1,
    page_size: int = 20,
    category: Optional[str] = None,
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    admin_tg_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    search: Optional[str] = None,
) -> tuple[list[GlobalActivityEntry], int]:
    """Return (entries, total_count) with filters."""
    try:
        with get_db_session() as s:
            q = s.query(GlobalActivityEntry)
            if category:
                q = q.filter(GlobalActivityEntry.category == category)
            if action:
                q = q.filter(GlobalActivityEntry.action == action)
            if user_id is not None:
                q = q.filter(GlobalActivityEntry.user_id == user_id)
            if admin_tg_id is not None:
                q = q.filter(GlobalActivityEntry.admin_telegram_id == admin_tg_id)
            if date_from:
                q = q.filter(GlobalActivityEntry.created_at >= date_from)
            if date_to:
                q = q.filter(GlobalActivityEntry.created_at <= date_to)
            if search:
                like = f"%{search}%"
                from sqlalchemy import or_
                q = q.filter(or_(
                    GlobalActivityEntry.description.ilike(like),
                    GlobalActivityEntry.username.ilike(like),
                    GlobalActivityEntry.action.ilike(like),
                    GlobalActivityEntry.ref_id.ilike(like),
                ))
            total = q.count()
            rows = (
                q.order_by(GlobalActivityEntry.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            s.expunge_all()
            return rows, total
    except Exception:
        logger.exception("global_timeline: get_timeline failed")
        return [], 0


def get_stats() -> dict:
    """Return activity statistics."""
    from sqlalchemy import func
    stats: dict = {}
    try:
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)

        with get_db_session() as s:
            stats["total"] = s.query(GlobalActivityEntry).count()
            stats["today"] = s.query(GlobalActivityEntry).filter(
                GlobalActivityEntry.created_at >= today).count()
            stats["week"] = s.query(GlobalActivityEntry).filter(
                GlobalActivityEntry.created_at >= week_ago).count()
            stats["month"] = s.query(GlobalActivityEntry).filter(
                GlobalActivityEntry.created_at >= month_ago).count()

            # Most active users (by user_id presence)
            top_users = (
                s.query(GlobalActivityEntry.username,
                        func.count(GlobalActivityEntry.id).label("cnt"))
                .filter(GlobalActivityEntry.user_id.isnot(None))
                .filter(GlobalActivityEntry.username.isnot(None))
                .group_by(GlobalActivityEntry.username)
                .order_by(func.count(GlobalActivityEntry.id).desc())
                .limit(5)
                .all()
            )
            stats["top_users"] = [(r.username, r.cnt) for r in top_users]

            # Most active admins
            top_admins = (
                s.query(GlobalActivityEntry.admin_telegram_id,
                        func.count(GlobalActivityEntry.id).label("cnt"))
                .filter(GlobalActivityEntry.admin_telegram_id.isnot(None))
                .group_by(GlobalActivityEntry.admin_telegram_id)
                .order_by(func.count(GlobalActivityEntry.id).desc())
                .limit(5)
                .all()
            )
            stats["top_admins"] = [(r.admin_telegram_id, r.cnt) for r in top_admins]

            # Most common actions
            top_actions = (
                s.query(GlobalActivityEntry.action,
                        func.count(GlobalActivityEntry.id).label("cnt"))
                .group_by(GlobalActivityEntry.action)
                .order_by(func.count(GlobalActivityEntry.id).desc())
                .limit(10)
                .all()
            )
            stats["top_actions"] = [(r.action, r.cnt) for r in top_actions]
    except Exception:
        logger.exception("global_timeline: get_stats failed")
    return stats


def export_csv(
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    category: Optional[str] = None,
    max_rows: int = 10_000,
) -> str:
    """Export timeline entries to CSV string."""
    rows, _ = get_timeline(
        page=1, page_size=max_rows,
        category=category, date_from=date_from, date_to=date_to,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "user_id", "username", "admin_telegram_id",
                     "action", "category", "description", "ip_address",
                     "status", "ref_type", "ref_id", "created_at"])
    for r in rows:
        writer.writerow([
            r.id, r.user_id, r.username, r.admin_telegram_id,
            r.action, r.category, r.description, r.ip_address,
            r.status, r.ref_type, r.ref_id,
            r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        ])
    return buf.getvalue()


def export_json(
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    category: Optional[str] = None,
    max_rows: int = 10_000,
) -> str:
    """Export timeline entries to JSON string."""
    rows, _ = get_timeline(
        page=1, page_size=max_rows,
        category=category, date_from=date_from, date_to=date_to,
    )
    data = []
    for r in rows:
        data.append({
            "id":                 r.id,
            "user_id":            r.user_id,
            "username":           r.username,
            "admin_telegram_id":  r.admin_telegram_id,
            "action":             r.action,
            "category":           r.category,
            "description":        r.description,
            "ip_address":         r.ip_address,
            "status":             r.status,
            "ref_type":           r.ref_type,
            "ref_id":             r.ref_id,
            "created_at":         r.created_at.isoformat() if r.created_at else None,
        })
    return json.dumps(data, ensure_ascii=False, indent=2)


def delete_old_entries(days: int = 90) -> int:
    """Delete entries older than `days` days. Returns count deleted."""
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db_session() as s:
            count = s.query(GlobalActivityEntry).filter(
                GlobalActivityEntry.created_at < cutoff
            ).delete(synchronize_session=False)
            s.commit()
        logger.info("global_timeline: deleted %d entries older than %d days", count, days)
        return count
    except Exception:
        logger.exception("global_timeline: delete_old_entries failed")
        return 0
