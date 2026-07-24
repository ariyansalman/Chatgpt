"""V25 — Order Timeline service.

Central module for rendering timelines, tracking progress, sending user
notifications, and recording admin notes. The underlying data lives entirely
in the existing ``order_status_history`` table — no new table required.

Admin notes are stored as ``OrderStatusHistory`` rows with
``actor_type='admin_note'`` and ``from_status == to_status`` (no actual
state change). This keeps the history table as the single source of truth.

Public API:
    render_user_timeline(order_id)   → str   Pretty timeline for users
    render_admin_timeline(order_id)  → str   Detailed timeline for admins
    progress_bar(lifecycle_status)   → str   Visual progress bar
    add_admin_note(order_id, note, admin_id)   → None
    dispatch_user_notify(order_id, new_status, bot)  → coroutine (fire-and-forget)
    get_timeline_rows(order_id, limit)  → list[OrderStatusHistory]
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Any

from database import get_db_session
from database.models import (
    Order, OrderStatusHistory, OrderLifecycleStatus, User,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Status presentation maps
# ─────────────────────────────────────────────────────────────────────────

# Human-readable label for each lifecycle status value (stored as .name)
_STATUS_EMOJI: dict[str, str] = {
    "PENDING":          "🆕",
    "AWAITING_PAYMENT": "💳",
    "PAID":             "💰",
    "PROCESSING":       "⚙️",
    "DELIVERED":        "📦",
    "COMPLETED":        "🎉",
    "CANCELLED":        "❌",
    "FAILED":           "❌",
    "REFUNDED":         "💸",
}

_STATUS_LABEL: dict[str, str] = {
    "PENDING":          "Order Created",
    "AWAITING_PAYMENT": "Payment Pending",
    "PAID":             "Payment Received",
    "PROCESSING":       "Processing",
    "DELIVERED":        "Product Delivered",
    "COMPLETED":        "Completed",
    "CANCELLED":        "Cancelled",
    "FAILED":           "Failed",
    "REFUNDED":         "Refunded",
}

# Notification messages sent to users on status change
_NOTIFY_MESSAGES: dict[str, str] = {
    "PENDING":          "Your order has been created and is now awaiting payment.",
    "AWAITING_PAYMENT": "Payment is pending — please complete it to continue.",
    "PAID":             "Payment received. We're now verifying your order.",
    "PROCESSING":       "Your order is being processed and will be delivered shortly.",
    "DELIVERED":        "Your product has been delivered. Check your order for full details.",
    "COMPLETED":        "Order complete — thank you for shopping with us!",
    "CANCELLED":        "This order has been cancelled.",
    "FAILED":           "We couldn't process this order. Contact support if you were charged.",
    "REFUNDED":         "Your payment has been refunded to your wallet balance.",
}

# The standard forward-progress sequence (for the progress bar)
_FORWARD_STAGES = [
    "PENDING",
    "AWAITING_PAYMENT",
    "PAID",
    "PROCESSING",
    "DELIVERED",
    "COMPLETED",
]


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    status = cfg.get_str("ots_status", "enabled")
    return status == "enabled"


def show_to_users() -> bool:
    return cfg.get_bool("ots_show_to_users", True)


def show_processing_time() -> bool:
    return cfg.get_bool("ots_show_processing_time", True)


def notify_users() -> bool:
    return cfg.get_bool("ots_notify_users", True)


# ─────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────

def get_timeline_rows(order_id: int, limit: int = 50) -> List[OrderStatusHistory]:
    """Return all history rows for ``order_id``, oldest first."""
    with get_db_session() as s:
        rows = (
            s.query(OrderStatusHistory)
            .filter(OrderStatusHistory.order_id == order_id)
            .order_by(OrderStatusHistory.created_at.asc())
            .limit(limit)
            .all()
        )
        # Detach from session — caller only needs scalars
        result = []
        for r in rows:
            result.append({
                "id": r.id,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "actor_type": r.actor_type,
                "admin_id": r.admin_id,
                "reason": r.reason,
                "created_at": r.created_at,
            })
        return result


# ─────────────────────────────────────────────────────────────────────────
# Progress bar
# ─────────────────────────────────────────────────────────────────────────

def progress_bar(current_status_name: Optional[str]) -> str:
    """Return a multi-line visual progress bar for the order's forward stages.

    Terminal statuses (CANCELLED / FAILED / REFUNDED) are handled separately
    — they render a single red line instead of the forward-progress chart.

    Example output:
        🟢 Order Created
        🟢 Payment Received
        🟡 Processing
        ⚪ Delivered
        ⚪ Completed
    """
    if current_status_name in ("CANCELLED", "FAILED", "REFUNDED"):
        emoji = _STATUS_EMOJI.get(current_status_name, "❌")
        label = _STATUS_LABEL.get(current_status_name, current_status_name)
        return f"❌ <b>{label}</b>"

    try:
        cur_idx = _FORWARD_STAGES.index(current_status_name) if current_status_name else -1
    except ValueError:
        cur_idx = -1

    lines = []
    for i, stage in enumerate(_FORWARD_STAGES):
        label = _STATUS_LABEL.get(stage, stage)
        if i < cur_idx:
            dot = "🟢"
        elif i == cur_idx:
            dot = "🟡"
        else:
            dot = "⚪"
        lines.append(f"{dot} {label}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# User-facing timeline renderer
# ─────────────────────────────────────────────────────────────────────────

def render_user_timeline(order_id: int) -> str:
    """Return a clean user-facing timeline for ``order_id``.

    - Omits admin IDs and internal actor labels.
    - Admin notes are shown without the author.
    - Each stage shows date, time, status label, and an optional description.
    """
    rows = get_timeline_rows(order_id, limit=30)
    if not rows:
        return "No timeline entries yet."

    current_status = rows[-1]["to_status"]

    lines = [
        "📋 <b>ORDER TIMELINE</b>",
        "",
        progress_bar(current_status),
        "",
        "<b>History:</b>",
    ]

    for r in rows:
        ts = r["created_at"]
        when = ts.strftime("%d %b %Y  %H:%M") if ts else "?"
        to_st = r["to_status"] or ""
        actor = r["actor_type"] or "system"
        reason = r.get("reason") or ""

        if actor == "admin_note":
            # Show note without "admin" label
            emoji = "📝"
            label = "Admin Note"
            desc = reason
        else:
            emoji = _STATUS_EMOJI.get(to_st, "•")
            label = _STATUS_LABEL.get(to_st, to_st.replace("_", " ").title())
            desc = reason

        entry = f"{emoji} <b>{label}</b>\n   🕐 {when}"
        if desc:
            entry += f"\n   💬 {desc}"
        lines.append(entry)

    if show_processing_time():
        first_ts = rows[0]["created_at"]
        last_ts = rows[-1]["created_at"]
        if first_ts and last_ts:
            delta = last_ts - first_ts
            mins = int(delta.total_seconds() // 60)
            if mins < 60:
                elapsed = f"{mins}m"
            else:
                hrs = mins // 60
                rem = mins % 60
                elapsed = f"{hrs}h {rem}m" if rem else f"{hrs}h"
            lines.append(f"\n⏱ Processing time: <b>{elapsed}</b>")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Admin timeline renderer
# ─────────────────────────────────────────────────────────────────────────

def render_admin_timeline(order_id: int) -> str:
    """Return a detailed admin-facing timeline with actor info."""
    rows = get_timeline_rows(order_id, limit=50)
    if not rows:
        return "No timeline entries yet."

    current_status = rows[-1]["to_status"]

    lines = [
        f"📋 <b>ORDER #{order_id} — TIMELINE</b>",
        "",
        progress_bar(current_status),
        "",
        "<b>Full History:</b>",
    ]

    for r in rows:
        ts = r["created_at"]
        when = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
        from_st = r["from_status"] or "—"
        to_st = r["to_status"] or ""
        actor = r["actor_type"] or "system"
        admin_id = r.get("admin_id")
        reason = r.get("reason") or ""

        emoji = _STATUS_EMOJI.get(to_st, "•")
        label = _STATUS_LABEL.get(to_st, to_st.replace("_", " ").title())

        if actor == "admin_note":
            actor_label = f"admin [{admin_id}]" if admin_id else "admin"
            entry = f"📝 <b>Note</b>  [{actor_label}]  {when}"
        else:
            actor_label = f"{actor}" + (f" [{admin_id}]" if admin_id and actor == "admin" else "")
            entry = f"{emoji} <b>{label}</b>  [{actor_label}]  {when}"
            if from_st != to_st and from_st != "—":
                entry += f"\n   {_STATUS_LABEL.get(from_st, from_st)} → {label}"

        if reason:
            entry += f"\n   💬 {reason}"
        lines.append(entry)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Admin note
# ─────────────────────────────────────────────────────────────────────────

def add_admin_note(order_id: int, note: str, admin_id: Optional[int] = None) -> None:
    """Append an admin note to the order's timeline.

    Stored as a special ``OrderStatusHistory`` row with
    ``actor_type='admin_note'`` and ``from_status == to_status`` (no state
    change). The note text goes into ``reason``.
    """
    with get_db_session() as s:
        order = s.query(Order).filter(Order.id == order_id).first()
        if not order:
            raise ValueError(f"Order {order_id} not found")
        current = (order.lifecycle_status.name
                   if order.lifecycle_status else "PENDING")
        s.add(OrderStatusHistory(
            order_id=order_id,
            from_status=current,
            to_status=current,
            actor_type="admin_note",
            admin_id=admin_id,
            reason=(note or "")[:2000],
        ))
        s.commit()


# ─────────────────────────────────────────────────────────────────────────
# User notifications
# ─────────────────────────────────────────────────────────────────────────

def _build_notify_text(order_id: int, new_status_name: str, created_at=None) -> str:
    from utils.helpers import format_order_id
    emoji = _STATUS_EMOJI.get(new_status_name, "ℹ️")
    label = _STATUS_LABEL.get(new_status_name, new_status_name)
    msg_body = _NOTIFY_MESSAGES.get(new_status_name, "Your order status has been updated.")
    return (
        f"{emoji} <b>Order Update</b>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n"
        f"📝 Order   <code>{format_order_id(order_id, created_at)}</code>\n"
        f"📌 Status  <b>{label}</b>\n\n"
        f"{msg_body}"
    )


async def _async_notify_user(order_id: int, new_status_name: str, bot: Any) -> None:
    """Send status change DM to the user who placed the order."""
    try:
        with get_db_session() as s:
            order = s.query(Order).filter(Order.id == order_id).first()
            if not order:
                return
            user = s.query(User).filter(User.id == order.user_id).first()
            if not user or not user.telegram_id:
                return
            telegram_id = user.telegram_id
            order_created_at = order.created_at

        text = _build_notify_text(order_id, new_status_name, order_created_at)
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("OTS user notify failed for order %s status %s",
                     order_id, new_status_name, exc_info=True)


def dispatch_user_notify(
    order_id: int,
    new_status: "OrderLifecycleStatus",
    bot: Optional[Any],
) -> None:
    """Fire-and-forget: notify the buyer of a status change.

    Does nothing when ``ots_notify_users`` is OFF or ``bot`` is None.
    Runs as a background task in the running PTB event loop (if available),
    or synchronously in a short-lived loop (background job context).
    """
    if not notify_users() or bot is None:
        return
    status_name = new_status.name if new_status else ""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_async_notify_user(order_id, status_name, bot))
    except RuntimeError:
        try:
            asyncio.run(_async_notify_user(order_id, status_name, bot))
        except Exception:
            logger.debug("OTS sync notify failed for order %s", order_id)
    except Exception:
        logger.debug("OTS dispatch_user_notify failed for order %s", order_id)
