"""Single, standardized layout for every admin-facing notification.

Every admin alert in the project — orders, payments, deposits, refunds,
manual payments, users, coupons, inventory, support, system — renders
through :func:`render` or :func:`render_order_notification` so they all
look and read the same way: compact, no divider lines, at most one blank
line between sections, and a consistent emoji per field so admins can
scan any notification type the same way.

Order / delivery notifications (new order, completed, pending, manual,
failed) use the richer, fixed layout below:

    {icon} <b>{title}</b>

    🆔 {order id}
    👤 {name} (@{username})

    📦 {product}
    🔢 Qty: {quantity}
    💰 Paid: {total}
    💳 {payment method}
    🚀 {delivery label}

    🕒 {timestamp}

    🆔 Telegram ID: {telegram id}      ← only when the admin has turned
                                          on "Show Telegram ID" in
                                          Notification Settings

Every other admin notification (deposits, refunds, manual payments,
disputes, tickets, system alerts, etc.) goes through the flat ``render()``
helper below, which prefixes each (label, value) field with a consistent
emoji looked up from its label.

Rules baked into this module (see the admin-notifications redesign spec):
  • One layout family, no per-event variations.
  • No dashed/line separators.
  • At most one blank line between sections.
  • Fields with no value are dropped automatically, so callers never end
    up printing "Reason: —" style noise or duplicate info.
  • Only one timestamp line, never both UTC and local.
  • A customer's Telegram ID is only ever shown when the admin has
    enabled "Show Telegram ID" in Notification Settings.

This module is presentation-only. It never touches business logic,
database state, or decides *whether* a notification is sent — callers
keep doing that exactly as before and just hand values to ``render()`` /
``render_order_notification()`` to get back the text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

FieldList = Iterable[Tuple[str, object]]

_DHAKA_TZ = ZoneInfo("Asia/Dhaka")

# status -> (header icon, header title)
_ORDER_HEADERS = {
    "new_order": ("🆕", "New Order"),
    "completed": ("✅", "New Order Completed"),
    "pending": ("⏳", "Order Pending"),
    "manual": ("🛠", "Manual Delivery Required"),
    "failed": ("❌", "Order Delivery Failed"),
}

# delivery_status -> (icon, label) for the "Delivery" field itself.
_DELIVERY_LABELS = {
    "instant": ("🚀", "Instant"),
    "pending": ("⏳", "Pending"),
    "processing": ("🔄", "Processing"),
    "manual": ("🛠", "Manual"),
    "failed": ("❌", "Failed"),
    "file": ("📎", "File"),
}

# label (normalized: lowercased, underscores -> spaces) -> emoji.
# Used by render() so every flat notification (deposits, refunds, manual
# payments, disputes, tickets, system alerts, ...) gets the same
# consistent emoji per field without every call site having to know
# about it.
_FIELD_EMOJI = {
    "order id": "🆔", "deposit id": "🆔", "transaction id": "🆔",
    "triggered by order id": "🆔",
    "customer": "👤", "user": "👤", "name": "👤", "username": "👤",
    "referred by": "🤝",
    "admin": "🛠", "admin_id": "🛠", "approved by": "🛠", "rejected by": "🛠",
    "target": "🎯",
    "product": "📦", "product_name": "📦", "plan": "📦", "subscription": "📦",
    "quantity": "🔢", "units added": "🔢",
    "amount": "💰", "reward amount": "💰",
    "discount": "🏷", "coupon code": "🏷",
    "payment method": "💳", "method": "💳",
    "delivery_type": "🚀", "delivery mode": "🚀",
    "reason": "❗️", "error": "❗️",
    "status": "🔄", "previous status": "🔄", "outcome": "🔄",
    "priority": "⚠️", "risk": "⚠️", "flags": "⚠️",
    "subject": "📝", "category": "📂", "source": "🔗",
    "time left": "⏳", "rating": "⭐",
    "action": "⚙️", "actions": "⚙️",
    "type": "🗄", "message": "💬", "update": "🔍",
}
_DEFAULT_FIELD_EMOJI = "▫️"

# Labels (normalized the same way) that carry a customer's Telegram ID.
# Gated behind the "Show Telegram ID" admin setting everywhere they
# appear, not just on order notifications.
_TELEGRAM_ID_LABELS = {
    "telegram id", "referrer telegram id",
    "customer_telegram_id", "referrer_telegram_id", "telegram_id",
}


def _show_telegram_id_setting() -> bool:
    """Whether admins opted in to showing customers' Telegram IDs.

    Controlled from Admin Panel → Notification Settings → "Show Telegram
    ID". Defensive: any failure (e.g. no DB session yet during boot)
    just hides the field rather than raising, since this only affects
    display.
    """
    try:
        from utils.bot_config import cfg
        return cfg.get_bool("notif_show_telegram_id", False)
    except Exception:
        return False


def _field_emoji(label: str) -> str:
    return _FIELD_EMOJI.get(label.strip().lower(), _DEFAULT_FIELD_EMOJI)


def dhaka_time_str(dt: Optional[datetime] = None) -> str:
    """Format a timestamp in Asia/Dhaka time as ``YYYY-MM-DD HH:mm BST``.

    Order notifications never show UTC. ``dt`` is treated as UTC when it
    has no tzinfo, matching how timestamps are already stored in the
    database (e.g. ``Order.created_at``). Defaults to "now" when omitted.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DHAKA_TZ).strftime("%Y-%m-%d %H:%M BST")


def render_order_notification(
    *,
    status: str,
    order_id: object,
    customer_name: str,
    telegram_id: object,
    customer_username: Optional[str] = None,
    product_name: Optional[str] = None,
    quantity: Optional[object] = None,
    unit_price: Optional[str] = None,
    total_paid: Optional[str] = None,
    payment_method: Optional[str] = None,
    delivery_status: Optional[str] = None,
    coupon_code: Optional[str] = None,
    coupon_discount_label: Optional[str] = None,
    referral_commission: Optional[str] = None,
    reason: Optional[str] = None,
    order_time: Optional[str] = None,
    show_telegram_id: Optional[bool] = None,
) -> str:
    """Build one compact order/delivery admin notification.

    Args:
        status: one of "new_order", "completed", "pending", "manual",
            "failed" — picks the header icon/title. Unknown values fall
            back to a generic "🛒 Order Update" header so new statuses
            never crash the caller.
        order_id: the customer-facing order id (e.g. "ORD-20260726-000051"),
            never the raw internal database primary key.
        customer_name, telegram_id: customer_name is always shown;
            telegram_id is only appended when enabled (see
            show_telegram_id below).
        customer_username: shown inline with the name, never on its own
            line, and omitted (name only) when the customer has no
            username.
        product_name, quantity, unit_price, total_paid, payment_method,
            delivery_status: each optional — omitted fields are dropped
            automatically rather than printed empty.
        coupon_code, coupon_discount_label, referral_commission: optional,
            only shown when the order actually used them.
        reason: optional short failure reason, shown only for failed
            deliveries.
        order_time: pre-formatted timestamp string; defaults to "now" in
            Asia/Dhaka time when omitted.
        show_telegram_id: whether to append the Telegram ID line at the
            end. Defaults to the admin's "Show Telegram ID" Notification
            Settings toggle when omitted — callers don't need to know
            about the setting themselves.
    """
    icon, title = _ORDER_HEADERS.get(status, ("🛒", "Order Update"))

    customer_line = (
        f"{customer_name} (@{customer_username})" if customer_username else customer_name
    )

    lines = [f"{icon} <b>{title}</b>", "", f"🆔 <code>{order_id}</code>", f"👤 {customer_line}"]

    order_fields = []
    if product_name:
        order_fields.append(f"📦 {product_name}")
    if quantity is not None and quantity != "":
        order_fields.append(f"🔢 Qty: {quantity}")
    if unit_price:
        order_fields.append(f"💵 Unit: {unit_price}")
    if total_paid:
        order_fields.append(f"💰 Paid: {total_paid}")
    if payment_method:
        order_fields.append(f"💳 {payment_method}")
    if delivery_status:
        d_icon, d_label = _DELIVERY_LABELS.get(
            delivery_status, ("🚀", str(delivery_status).title())
        )
        order_fields.append(f"{d_icon} {d_label}")
    if reason:
        order_fields.append(f"❗️ {reason}")
    if order_fields:
        lines.append("")
        lines.extend(order_fields)

    extra_fields = []
    if coupon_code:
        coupon_value = (
            f"{coupon_code} ({coupon_discount_label})" if coupon_discount_label else coupon_code
        )
        extra_fields.append(f"🏷 Coupon: {coupon_value}")
    if referral_commission:
        extra_fields.append(f"🤝 Referral: {referral_commission}")
    if extra_fields:
        lines.append("")
        lines.extend(extra_fields)

    lines.append("")
    lines.append(f"🕒 {order_time or dhaka_time_str()}")

    if show_telegram_id is None:
        show_telegram_id = _show_telegram_id_setting()
    if show_telegram_id and telegram_id:
        lines.append("")
        lines.append(f"🆔 Telegram ID: <code>{telegram_id}</code>")

    return "\n".join(lines)


def render(icon: str, title: str, fields: FieldList,
           timestamp: Optional[str] = None) -> str:
    """Build one compact, emoji-consistent admin notification message.

    Args:
        icon: a single leading emoji for the event category.
        title: short event title, e.g. "Deposit Approved".
        fields: ordered (label, value) pairs. Entries whose value is
            ``None`` or ``""`` are skipped automatically. Each field is
            prefixed with an emoji looked up from its label so every
            notification type reads the same way. A field carrying a
            customer's Telegram ID is only shown when the admin has
            enabled "Show Telegram ID" in Notification Settings.
        timestamp: optional pre-formatted timestamp string. Omit to leave
            the timestamp off entirely (e.g. when an admin has disabled it).
    """
    lines = [f"{icon} <b>{title}</b>", ""]
    show_tid: Optional[bool] = None
    for label, value in fields:
        if value is None or value == "":
            continue
        if label.strip().lower() in _TELEGRAM_ID_LABELS:
            if show_tid is None:
                show_tid = _show_telegram_id_setting()
            if not show_tid:
                continue
        lines.append(f"{_field_emoji(label)} {label}: {value}")
    if len(lines) == 2:
        lines.pop()  # no fields at all — drop the lone blank line
    if timestamp:
        lines.append("")
        lines.append(f"🕒 {timestamp}")
    return "\n".join(lines)


def utc_now_str() -> str:
    """Single canonical timestamp format used across all admin notifications."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
