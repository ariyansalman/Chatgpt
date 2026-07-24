"""V36 — Delivery Management System service layer.

Pure Python — no Telegram dependencies. All functions accept an open
SQLAlchemy session so they can participate in a caller's transaction or
be called from a standalone ``with get_db_session() as s:`` block.

Public API
──────────
create_record(...)         Create a new DeliveryRecord.
get_record(s, id)          Fetch one record by int PK or secure_id UUID.
list_records(...)          Paginated, filtered list.
get_dashboard_stats(s)     Counts/rates for the admin dashboard.
search_records(...)        Full-text search across order/user/product.
retry_delivery(...)        Reset a failed record back to pending.
resend_delivery(...)       Re-send the stored content to the user via bot.
cancel_delivery(...)       Cancel a pending/preparing/failed record.
replace_content(...)       Overwrite delivered_content and re-deliver.
export_logs(...)           Return CSV or JSON bytes of delivery records.
generate_secure_token(...) Attach a signed download token to a record.
consume_download(...)      Validate token + decrement download_count.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_PENDING     = "pending"
STATUS_PREPARING   = "preparing"
STATUS_PROCESSING  = "processing"
STATUS_DELIVERED   = "delivered"
STATUS_COMPLETED   = "completed"
STATUS_FAILED      = "failed"
STATUS_CANCELLED   = "cancelled"
STATUS_EXPIRED     = "expired"
STATUS_REFUNDED    = "refunded"

ALL_STATUSES = [
    STATUS_PENDING, STATUS_PREPARING, STATUS_PROCESSING,
    STATUS_DELIVERED, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_CANCELLED, STATUS_EXPIRED, STATUS_REFUNDED,
]

# Delivery types
TYPE_PRODUCT_KEY   = "product_key"
TYPE_ACCOUNT       = "account"
TYPE_GIFT_CARD     = "gift_card"
TYPE_LICENSE_KEY   = "license_key"
TYPE_DIGITAL_FILE  = "digital_file"
TYPE_DOWNLOAD_LINK = "download_link"
TYPE_CUSTOM_TEXT   = "custom_text"
TYPE_API           = "api"
TYPE_MANUAL        = "manual"

# Delivery methods
METHOD_AUTOMATIC  = "automatic"
METHOD_MANUAL     = "manual"
METHOD_SCHEDULED  = "scheduled"
METHOD_BULK       = "bulk"
METHOD_RANDOM     = "random"
METHOD_API        = "api"

PAGE_SIZE = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_token(length: int = 48) -> str:
    """Cryptographically-secure URL-safe token."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _apply_template(template: str, variables: Dict[str, str]) -> str:
    """Replace {variable} placeholders; unknown placeholders are left intact."""
    for k, v in variables.items():
        template = template.replace(f"{{{k}}}", str(v))
    return template


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_record(
    session,
    *,
    user_id: int,
    delivery_type: str,
    delivery_method: str = METHOD_AUTOMATIC,
    order_id: Optional[int] = None,
    order_item_id: Optional[int] = None,
    product_id: Optional[int] = None,
    delivered_content: Optional[str] = None,
    template_snapshot: Optional[str] = None,
    admin_id: Optional[int] = None,
    admin_note: Optional[str] = None,
    status: str = STATUS_PENDING,
    max_retries: int = 3,
    is_one_time: bool = False,
    download_limit: Optional[int] = None,
    link_expires_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> "DeliveryRecord":
    from database.models import DeliveryRecord
    now = datetime.utcnow()
    rec = DeliveryRecord(
        user_id=user_id,
        delivery_type=delivery_type,
        delivery_method=delivery_method,
        order_id=order_id,
        order_item_id=order_item_id,
        product_id=product_id,
        delivered_content=delivered_content,
        template_snapshot=template_snapshot,
        admin_id=admin_id,
        admin_note=admin_note,
        status=status,
        max_retries=max_retries,
        is_one_time=is_one_time,
        download_limit=download_limit,
        link_expires_at=link_expires_at,
        expires_at=expires_at,
        created_at=now,
    )
    if status == STATUS_DELIVERED:
        rec.delivered_at = now
    session.add(rec)
    session.flush()
    return rec


def get_record(session, record_id) -> Optional["DeliveryRecord"]:
    """Fetch by integer PK or secure UUID string."""
    from database.models import DeliveryRecord
    if isinstance(record_id, int):
        return session.get(DeliveryRecord, record_id)
    return (session.query(DeliveryRecord)
            .filter(DeliveryRecord.secure_id == str(record_id))
            .first())


def list_records(
    session,
    *,
    status: Optional[str] = None,
    delivery_type: Optional[str] = None,
    delivery_method: Optional[str] = None,
    user_id: Optional[int] = None,
    order_id: Optional[int] = None,
    product_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> Tuple[List["DeliveryRecord"], int, int]:
    """Returns (records_page, page, total_pages)."""
    from database.models import DeliveryRecord
    q = session.query(DeliveryRecord)
    if status:
        q = q.filter(DeliveryRecord.status == status)
    if delivery_type:
        q = q.filter(DeliveryRecord.delivery_type == delivery_type)
    if delivery_method:
        q = q.filter(DeliveryRecord.delivery_method == delivery_method)
    if user_id:
        q = q.filter(DeliveryRecord.user_id == user_id)
    if order_id:
        q = q.filter(DeliveryRecord.order_id == order_id)
    if product_id:
        q = q.filter(DeliveryRecord.product_id == product_id)
    if date_from:
        q = q.filter(DeliveryRecord.created_at >= date_from)
    if date_to:
        q = q.filter(DeliveryRecord.created_at <= date_to)
    q = q.order_by(DeliveryRecord.created_at.desc())

    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    records = q.offset(page * page_size).limit(page_size).all()
    return records, page, total_pages


def search_records(
    session,
    query: str,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> Tuple[List["DeliveryRecord"], int, int]:
    """Search by order_id, user_id, secure_id, product_id, or delivery_type prefix."""
    from database.models import DeliveryRecord
    query = query.strip()
    q = session.query(DeliveryRecord)

    if query.isdigit():
        num = int(query)
        q = q.filter(
            (DeliveryRecord.order_id == num) |
            (DeliveryRecord.user_id == num) |
            (DeliveryRecord.product_id == num) |
            (DeliveryRecord.id == num)
        )
    else:
        like = f"%{query.lower()}%"
        q = q.filter(
            (DeliveryRecord.secure_id.ilike(like)) |
            (DeliveryRecord.delivery_type.ilike(like)) |
            (DeliveryRecord.status.ilike(like))
        )
    q = q.order_by(DeliveryRecord.created_at.desc())

    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    records = q.offset(page * page_size).limit(page_size).all()
    return records, page, total_pages


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

def get_dashboard_stats(session) -> Dict[str, Any]:
    """Return counts and rates for the admin dashboard widget."""
    from database.models import DeliveryRecord
    from sqlalchemy import func

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    rows = (session.query(DeliveryRecord.status, func.count(DeliveryRecord.id))
            .group_by(DeliveryRecord.status).all())
    by_status = {s: c for s, c in rows}

    total          = sum(by_status.values())
    delivered      = by_status.get(STATUS_DELIVERED, 0) + by_status.get(STATUS_COMPLETED, 0)
    failed         = by_status.get(STATUS_FAILED, 0)
    pending        = by_status.get(STATUS_PENDING, 0) + by_status.get(STATUS_PREPARING, 0) + by_status.get(STATUS_PROCESSING, 0)
    success_rate   = round(delivered / total * 100, 1) if total else 0.0

    today_count = (session.query(func.count(DeliveryRecord.id))
                   .filter(DeliveryRecord.created_at >= today_start).scalar() or 0)

    retry_queue = (session.query(func.count(DeliveryRecord.id))
                   .filter(DeliveryRecord.status == STATUS_FAILED,
                           DeliveryRecord.retry_count < DeliveryRecord.max_retries).scalar() or 0)

    # Average delivery time (created_at → delivered_at) in seconds
    avg_seconds: Optional[float] = None
    try:
        from sqlalchemy import extract
        result = (session.query(
                      func.avg(
                          extract("epoch", DeliveryRecord.delivered_at) -
                          extract("epoch", DeliveryRecord.created_at)
                      )
                  )
                  .filter(DeliveryRecord.delivered_at.isnot(None),
                          DeliveryRecord.created_at.isnot(None))
                  .scalar())
        avg_seconds = float(result) if result is not None else None
    except Exception:
        avg_seconds = None

    avg_time_str = "—"
    if avg_seconds is not None:
        if avg_seconds < 60:
            avg_time_str = f"{avg_seconds:.0f}s"
        elif avg_seconds < 3600:
            avg_time_str = f"{avg_seconds / 60:.1f}m"
        else:
            avg_time_str = f"{avg_seconds / 3600:.1f}h"

    return {
        "total":        total,
        "delivered":    delivered,
        "pending":      pending,
        "failed":       failed,
        "cancelled":    by_status.get(STATUS_CANCELLED, 0),
        "expired":      by_status.get(STATUS_EXPIRED, 0),
        "refunded":     by_status.get(STATUS_REFUNDED, 0),
        "retry_queue":  retry_queue,
        "today":        today_count,
        "success_rate": success_rate,
        "avg_time":     avg_time_str,
        "by_status":    by_status,
    }


# ---------------------------------------------------------------------------
# Delivery actions
# ---------------------------------------------------------------------------

def retry_delivery(session, record_id, admin_id: int) -> Tuple[bool, str]:
    """Reset a failed/expired record to pending for the runner to re-attempt.

    Returns (success, message).
    """
    rec = get_record(session, record_id)
    if not rec:
        return False, "Delivery record not found."
    if rec.status not in (STATUS_FAILED, STATUS_EXPIRED, STATUS_CANCELLED):
        return False, f"Cannot retry a delivery with status '{rec.status}'."
    if rec.retry_count >= rec.max_retries:
        return False, f"Maximum retry count ({rec.max_retries}) already reached."

    rec.status      = STATUS_PENDING
    rec.retry_count += 1
    rec.admin_id    = admin_id
    rec.last_error  = None
    session.commit()

    try:
        from utils.audit import log_admin_action
        log_admin_action(admin_id, "delivery_retry",
                         f"record_id={rec.id} attempt={rec.retry_count}",
                         module="delivery_manager")
    except Exception:
        pass
    return True, f"Delivery #{rec.id} reset to pending (attempt {rec.retry_count}/{rec.max_retries})."


async def resend_delivery(session, record_id, admin_id: int, bot) -> Tuple[bool, str]:
    """Re-send the stored delivered_content to the user via Telegram.

    Does NOT consume new inventory — only re-sends what was already delivered.
    """
    rec = get_record(session, record_id)
    if not rec:
        return False, "Delivery record not found."
    if not rec.delivered_content:
        return False, "No delivered content stored — nothing to resend."

    try:
        from utils.helpers import sanitize_message as _sanitize
        await bot.send_message(
            chat_id=rec.user_id,
            text=_sanitize(
                f"📦 <b>Redelivery — Order #{rec.order_id or rec.id}</b>\n\n"
                f"{rec.delivered_content}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("resend_delivery: telegram error: %s", exc)
        return False, f"Telegram error: {exc}"

    rec.status      = STATUS_DELIVERED
    rec.delivered_at = datetime.utcnow()
    rec.admin_id    = admin_id
    session.commit()

    try:
        from utils.audit import log_admin_action
        log_admin_action(admin_id, "delivery_resent",
                         f"record_id={rec.id} user={rec.user_id}",
                         module="delivery_manager")
    except Exception:
        pass
    return True, f"Delivery #{rec.id} resent to user {rec.user_id}."


def cancel_delivery(session, record_id, admin_id: int) -> Tuple[bool, str]:
    """Cancel a delivery that has not yet completed."""
    rec = get_record(session, record_id)
    if not rec:
        return False, "Delivery record not found."
    if rec.status in (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_REFUNDED):
        return False, f"Cannot cancel a delivery with status '{rec.status}'."

    rec.status    = STATUS_CANCELLED
    rec.admin_id  = admin_id
    session.commit()

    try:
        from utils.audit import log_admin_action
        log_admin_action(admin_id, "delivery_cancelled",
                         f"record_id={rec.id}",
                         module="delivery_manager")
    except Exception:
        pass
    return True, f"Delivery #{rec.id} cancelled."


async def replace_content(
    session,
    record_id,
    new_content: str,
    admin_id: int,
    bot,
) -> Tuple[bool, str]:
    """Replace the delivered item with new content and re-send to the user."""
    rec = get_record(session, record_id)
    if not rec:
        return False, "Delivery record not found."

    rec.delivered_content = new_content
    rec.admin_id          = admin_id
    rec.status            = STATUS_PROCESSING
    session.commit()

    try:
        from utils.helpers import sanitize_message as _sanitize
        await bot.send_message(
            chat_id=rec.user_id,
            text=_sanitize(
                f"🔄 <b>Replacement Delivery — Order #{rec.order_id or rec.id}</b>\n\n"
                f"{new_content}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        rec.status     = STATUS_FAILED
        rec.last_error = str(exc)
        session.commit()
        return False, f"Telegram error while sending replacement: {exc}"

    rec.status       = STATUS_DELIVERED
    rec.delivered_at = datetime.utcnow()
    session.commit()

    try:
        from utils.audit import log_admin_action
        log_admin_action(admin_id, "delivery_replaced",
                         f"record_id={rec.id} user={rec.user_id}",
                         module="delivery_manager")
    except Exception:
        pass
    return True, f"Replacement delivered for record #{rec.id}."


# ---------------------------------------------------------------------------
# Secure download links
# ---------------------------------------------------------------------------

def generate_secure_token(
    session,
    record_id,
    *,
    one_time: bool = False,
    download_limit: Optional[int] = None,
    expiry_hours: int = 24,
) -> str:
    """Attach a fresh download token to an existing DeliveryRecord.

    Returns the token string. Raises ValueError if record not found.
    """
    rec = get_record(session, record_id)
    if not rec:
        raise ValueError(f"DeliveryRecord {record_id} not found.")

    token = _gen_token(48)
    rec.download_token  = token
    rec.is_one_time     = one_time
    rec.download_limit  = download_limit
    rec.download_count  = 0
    if expiry_hours > 0:
        rec.link_expires_at = datetime.utcnow() + timedelta(hours=expiry_hours)
    else:
        rec.link_expires_at = None
    session.commit()
    return token


def consume_download(session, token: str) -> Tuple[bool, str, Optional["DeliveryRecord"]]:
    """Validate and consume one download credit from a secure token.

    Returns (allowed, reason, record).
    """
    from database.models import DeliveryRecord
    rec = (session.query(DeliveryRecord)
           .filter(DeliveryRecord.download_token == token)
           .first())
    if not rec:
        return False, "Invalid or expired download link.", None

    now = datetime.utcnow()
    if rec.link_expires_at and now > rec.link_expires_at:
        rec.status = STATUS_EXPIRED
        session.commit()
        return False, "Download link has expired.", rec

    if rec.download_limit is not None and rec.download_count >= rec.download_limit:
        return False, "Download limit reached.", rec

    rec.download_count += 1
    if rec.is_one_time or (rec.download_limit and rec.download_count >= rec.download_limit):
        rec.download_token = None   # Invalidate after use
    session.commit()
    return True, "ok", rec


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_logs(
    session,
    fmt: str = "csv",
    *,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 5000,
) -> bytes:
    """Export delivery records as CSV or JSON bytes."""
    from database.models import DeliveryRecord

    q = session.query(DeliveryRecord)
    if status:
        q = q.filter(DeliveryRecord.status == status)
    if date_from:
        q = q.filter(DeliveryRecord.created_at >= date_from)
    if date_to:
        q = q.filter(DeliveryRecord.created_at <= date_to)
    records = q.order_by(DeliveryRecord.created_at.desc()).limit(limit).all()

    rows = []
    for r in records:
        rows.append({
            "id":               r.id,
            "secure_id":        r.secure_id,
            "order_id":         r.order_id or "",
            "order_item_id":    r.order_item_id or "",
            "user_id":          r.user_id,
            "product_id":       r.product_id or "",
            "delivery_type":    r.delivery_type,
            "delivery_method":  r.delivery_method,
            "status":           r.status,
            "admin_id":         r.admin_id or "",
            "retry_count":      r.retry_count,
            "last_error":       r.last_error or "",
            "download_count":   r.download_count,
            "created_at":       _fmt_dt(r.created_at),
            "delivered_at":     _fmt_dt(r.delivered_at),
            "completed_at":     _fmt_dt(r.completed_at),
        })

    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")

    # CSV
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue().encode("utf-8")
