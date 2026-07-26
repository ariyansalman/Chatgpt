"""Single, standardized layout for every admin-facing notification.

Every admin alert in the project — orders, payments, users, coupons,
inventory, support, system — renders through :func:`render` so they all
look and read the same way:

    {icon} <b>{title}</b>

    {Label}: {value}
    {Label}: {value}

    🕒 {timestamp}

Rules baked into this module (see the admin-notifications redesign spec):
  • One layout, no per-event variations.
  • No dashed/line separators.
  • No repeated section headers — a flat list of labeled fields.
  • Fields with no value are dropped automatically, so callers never end
    up printing "Reason: —" style noise or duplicate info.
  • Only one timestamp line, never both UTC and local.

This module is presentation-only. It never touches business logic,
database state, or decides *whether* a notification is sent — callers
keep doing that exactly as before and just hand values to ``render()``
to get back the text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

FieldList = Iterable[Tuple[str, object]]

# ── Order / delivery notifications ─────────────────────────────────────────
#
# Orders get their own, richer layout (render_order_notification below)
# instead of the flat generic ``render()`` above. Every order-related admin
# alert — new order, completed order, instant/pending/manual/failed delivery
# — shares this exact structure so admins can scan them all the same way:
#
#   {icon} <b>{title}</b>
#
#   🆔 <b>Order ID</b>
#   <code>{order id}</code>
#
#   👤 <b>Customer</b>
#   {name} (@{username})
#
#   🆔 <b>Telegram ID</b>
#   <code>{telegram id}</code>
#
#   ━━━━━━━━━━━━━━━━━━
#
#   📦 <b>Product</b> / 🔢 <b>Quantity</b> / 💵 <b>Unit Price</b> /
#   💰 <b>Total Paid</b> / 💳 <b>Payment Method</b> / 🚀 <b>Delivery</b>
#   (each shown only when the caller has a value for it)
#
#   ━━━━━━━━━━━━━━━━━━     (only when there's a coupon or referral to show)
#
#   🏷 <b>Coupon</b> / 🤝 <b>Referral Commission</b>
#
#   ━━━━━━━━━━━━━━━━━━
#
#   🕒 <b>Order Time</b>
#   {timestamp, always Asia/Dhaka — never UTC}
#
# This is presentation-only, same as the rest of this module — it never
# decides whether/when a notification fires, only how it's formatted.

_DHAKA_TZ = ZoneInfo("Asia/Dhaka")
_SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━"

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
) -> str:
    """Build one standardized order/delivery admin notification.

    Args:
        status: one of "new_order", "completed", "pending", "manual",
            "failed" — picks the header icon/title. Unknown values fall
            back to a generic "🛒 Order Update" header so new statuses
            never crash the caller.
        order_id: the customer-facing order id (e.g. "ORD-20260726-000051"),
            never the raw internal database primary key.
        customer_name, telegram_id: always shown.
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
    """
    icon, title = _ORDER_HEADERS.get(status, ("🛒", "Order Update"))

    customer_line = (
        f"{customer_name} (@{customer_username})" if customer_username else customer_name
    )

    header = f"{icon} <b>{title}</b>"
    identity = "\n\n".join([
        f"🆔 <b>Order ID</b>\n<code>{order_id}</code>",
        f"👤 <b>Customer</b>\n{customer_line}",
        f"🆔 <b>Telegram ID</b>\n<code>{telegram_id}</code>",
    ])

    order_fields = []
    if product_name:
        order_fields.append(f"📦 <b>Product</b>\n{product_name}")
    if quantity is not None and quantity != "":
        order_fields.append(f"🔢 <b>Quantity</b>\n{quantity}")
    if unit_price:
        order_fields.append(f"💵 <b>Unit Price</b>\n{unit_price}")
    if total_paid:
        order_fields.append(f"💰 <b>Total Paid</b>\n{total_paid}")
    if payment_method:
        order_fields.append(f"💳 <b>Payment Method</b>\n{payment_method}")
    if delivery_status:
        d_icon, d_label = _DELIVERY_LABELS.get(
            delivery_status, ("🚀", str(delivery_status).title())
        )
        order_fields.append(f"{d_icon} <b>Delivery</b>\n{d_label}")
    if reason:
        order_fields.append(f"❗️ <b>Reason</b>\n{reason}")

    extra_fields = []
    if coupon_code:
        coupon_value = (
            f"{coupon_code} ({coupon_discount_label})" if coupon_discount_label else coupon_code
        )
        extra_fields.append(f"🏷 <b>Coupon</b>\n{coupon_value}")
    if referral_commission:
        extra_fields.append(f"🤝 <b>Referral Commission</b>\n{referral_commission}")

    time_block = f"🕒 <b>Order Time</b>\n{order_time or dhaka_time_str()}"

    sections = [order_fields and "\n\n".join(order_fields),
                extra_fields and "\n\n".join(extra_fields),
                time_block]

    message = f"{header}\n\n{identity}"
    for section in sections:
        if section:
            message += f"\n\n{_SECTION_DIVIDER}\n\n{section}"
    return message


def render(icon: str, title: str, fields: FieldList,
           timestamp: Optional[str] = None) -> str:
    """Build one standardized admin notification message.

    Args:
        icon: a single leading emoji for the event category.
        title: short event title, e.g. "New Order".
        fields: ordered (label, value) pairs. Entries whose value is
            ``None`` or ``""`` are skipped automatically.
        timestamp: optional pre-formatted timestamp string. Omit to leave
            the timestamp off entirely (e.g. when an admin has disabled it).
    """
    lines = [f"{icon} <b>{title}</b>", ""]
    for label, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"{label}: {value}")
    if timestamp:
        lines.append("")
        lines.append(f"🕒 {timestamp}")
    return "\n".join(lines)


def utc_now_str() -> str:
    """Single canonical timestamp format used across all admin notifications."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
