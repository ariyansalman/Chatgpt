"""services/activity_feed.py — Enterprise Activity Feed System V21.

Two independent feeds:
  1. 🔒 Private Admin Feed  — detailed structured logs → private channel/group
  2. 🌍 Public Purchase Feed — privacy-safe purchase announcements → public channel

Design principles:
  - All posts are fire-and-forget via asyncio.create_task() — never blocks
  - All failures are swallowed and logged — never interrupts purchase flow
  - Bot-config driven: every toggle reads from the af_* key namespace
  - Multi-destination: comma-separated channel ID lists
  - Auto-retry on Telegram errors (3 attempts with exponential back-off)
  - Auto-delete and pin support

Usage from any async handler:
    import asyncio
    from services.activity_feed import post_event, EVENT_NEW_ORDER
    try:
        asyncio.create_task(post_event(context.bot, EVENT_NEW_ORDER, {
            "customer_telegram_id": tg_id,
            "customer_name": "John",
            ...
        }))
    except Exception:
        pass
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.notify_format import render as _render

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

EVENT_NEW_ORDER         = "new_order"
EVENT_WALLET_TOPUP      = "wallet_topup"
EVENT_REFUND            = "refund"
EVENT_DELIVERY          = "delivery_completed"
EVENT_ORDER_CANCELLED   = "order_cancelled"
EVENT_COUPON_USED       = "coupon_used"
EVENT_REFERRAL_REWARD   = "referral_reward"
EVENT_REVIEW_SUBMITTED  = "review_submitted"
EVENT_PRODUCT_RESTOCKED = "product_restocked"
EVENT_OUT_OF_STOCK      = "product_out_of_stock"
EVENT_INVOICE_GENERATED = "invoice_generated"
EVENT_USER_REGISTERED   = "user_registered"
EVENT_LOGIN_ALERT       = "login_alert"
EVENT_FAILED_PAYMENT    = "failed_payment"
EVENT_FRAUD_DETECTED    = "fraud_detected"
EVENT_SUPPORT_TICKET    = "support_ticket"
EVENT_ADMIN_ACTION      = "admin_action"

# Canonical config key suffixes per event
_EVENT_KEYS: Dict[str, str] = {
    EVENT_NEW_ORDER:         "new_order",
    EVENT_WALLET_TOPUP:      "wallet_topup",
    EVENT_REFUND:            "refund",
    EVENT_DELIVERY:          "delivery_completed",
    EVENT_ORDER_CANCELLED:   "order_cancelled",
    EVENT_COUPON_USED:       "coupon_used",
    EVENT_REFERRAL_REWARD:   "referral_reward",
    EVENT_REVIEW_SUBMITTED:  "review_submitted",
    EVENT_PRODUCT_RESTOCKED: "product_restocked",
    EVENT_OUT_OF_STOCK:      "product_out_of_stock",
    EVENT_INVOICE_GENERATED: "invoice_generated",
    EVENT_USER_REGISTERED:   "user_registered",
    EVENT_LOGIN_ALERT:       "login_alert",
    EVENT_FAILED_PAYMENT:    "failed_payment",
    EVENT_FRAUD_DETECTED:    "fraud_detected",
    EVENT_SUPPORT_TICKET:    "support_ticket",
    EVENT_ADMIN_ACTION:      "admin_action",
}

# Events eligible for the public purchase feed
_PUBLIC_ELIGIBLE = {EVENT_NEW_ORDER, EVENT_DELIVERY}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _c():
    from utils.bot_config import cfg
    return cfg


def _ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mask(name: str) -> str:
    """Privacy-mask a name: 'Arjun Sharma' → 'Arj***ma'."""
    if not name:
        return "***"
    clean = name.strip()
    if len(clean) <= 4:
        return clean[0] + "***"
    return clean[:3] + "***" + clean[-2:]


def _parse_channels(raw: str) -> List[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _fmt_price(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return str(v) if v else "—"


# ---------------------------------------------------------------------------
# Private feed formatters — one per event type
# ---------------------------------------------------------------------------

def _private_new_order(d: Dict[str, Any], cfg) -> str:
    hide_pm  = cfg.get_bool("af_hide_payment_method", False)
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    name     = d.get("customer_name") or "—"
    uname    = ("@" + d["customer_username"]) if d.get("customer_username") else None
    tg_id    = d.get("customer_telegram_id", "—")
    customer = f"{name} ({uname})" if uname else name
    product  = d.get("product_name", "—")
    variant  = d.get("variant")
    qty      = d.get("quantity", 1)
    price    = d.get("price", 0.0)
    currency = d.get("currency", "USD")
    amount   = f"{_fmt_price(price)} {currency}" if currency and currency != "USD" else _fmt_price(price)
    method   = (d.get("payment_method") or "Wallet Balance") if not hide_pm else None
    gateway  = (d.get("gateway")) if not hide_pm else None
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else "—"
    tx_id    = d.get("transaction_id")
    status   = d.get("order_status", "Completed")
    del_type = d.get("delivery_type", "Instant")
    fields = [
        ("Order ID", order_disp),
        ("Customer", customer),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Product", f"{product}" + (f" ({variant})" if variant else "")),
        ("Quantity", qty),
        ("Amount", amount),
        ("Payment Method", method),
        ("Gateway", gateway),
        ("Transaction ID", tx_id),
        ("Status", status),
        ("Delivery", del_type),
    ]
    return _render("🛒", "New Order", fields, None if hide_ts else _ts_utc())


def _private_wallet_topup(d: Dict[str, Any], cfg) -> str:
    hide_pm = cfg.get_bool("af_hide_payment_method", False)
    hide_ts = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_deposit_id as _fmt_did
    name    = d.get("customer_name") or "—"
    tg_id   = d.get("customer_telegram_id", "—")
    amount  = d.get("amount", 0.0)
    method  = (d.get("payment_method") or "—") if not hide_pm else None
    dep_id  = d.get("transaction_id")
    dep_disp = _fmt_did(int(dep_id)) if isinstance(dep_id, int) or (isinstance(dep_id, str) and dep_id.isdigit()) else dep_id
    fields = [
        ("Deposit ID", dep_disp or "—"),
        ("Customer", name),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Amount", _fmt_price(amount)),
        ("Payment Method", method),
    ]
    return _render("💰", "Deposit Approved", fields, None if hide_ts else _ts_utc())


def _private_failed_payment(d: Dict[str, Any], cfg) -> str:
    hide_pm = cfg.get_bool("af_hide_payment_method", False)
    hide_ts = cfg.get_bool("af_hide_time", False)
    tg_id   = d.get("customer_telegram_id", "—")
    amount  = d.get("amount", 0.0)
    method  = (d.get("payment_method") or "—") if not hide_pm else None
    tx_id   = d.get("transaction_id")
    reason  = d.get("reason") or "Rejected"
    fields = [
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Amount", _fmt_price(amount)),
        ("Payment Method", method),
        ("Transaction ID", tx_id),
        ("Reason", reason),
    ]
    return _render("⚠️", "Payment Failed", fields, None if hide_ts else _ts_utc())


def _private_refund(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    name     = d.get("customer_name") or "—"
    tg_id    = d.get("customer_telegram_id", "—")
    amount   = d.get("amount", 0.0)
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else "—"
    reason   = d.get("reason")
    fields = [
        ("Order ID", order_disp),
        ("Customer", name),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Refund Amount", _fmt_price(amount)),
        ("Reason", reason),
    ]
    return _render("💸", "Order Refunded", fields, None if hide_ts else _ts_utc())


def _private_delivery(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    name     = d.get("customer_name") or "—"
    tg_id    = d.get("customer_telegram_id", "—")
    product  = d.get("product_name", "—")
    qty      = d.get("quantity", 1)
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else "—"
    del_type = d.get("delivery_type", "Instant")
    icon, title = ("👤", "Manual Delivery") if del_type and "manual" in str(del_type).lower() else ("✅", "Order Completed")
    fields = [
        ("Order ID", order_disp),
        ("Customer", name),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Product", product),
        ("Quantity", qty),
        ("Delivery", del_type),
    ]
    return _render(icon, title, fields, None if hide_ts else _ts_utc())


def _private_order_cancelled(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    tg_id    = d.get("customer_telegram_id", "—")
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else "—"
    reason   = d.get("reason")
    fields = [
        ("Order ID", order_disp),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Reason", reason),
    ]
    return _render("❌", "Order Failed", fields, None if hide_ts else _ts_utc())


def _private_coupon_used(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    tg_id    = d.get("customer_telegram_id", "—")
    code     = d.get("coupon_code") or "—"
    discount = d.get("discount", 0.0)
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else None
    product  = d.get("product_name")
    fields = [
        ("Coupon Code", code),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Discount", _fmt_price(discount)),
        ("Order ID", order_disp),
        ("Product", product),
    ]
    return _render("🎟", "Coupon Redeemed", fields, None if hide_ts else _ts_utc())


def _private_referral_reward(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    ref_id   = d.get("referrer_telegram_id", "—")
    amount   = d.get("amount", 0.0)
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else None
    fields = [
        ("Referrer Telegram ID", f"<code>{ref_id}</code>"),
        ("Reward Amount", _fmt_price(amount)),
        ("Triggered By Order ID", order_disp),
    ]
    return _render("🎟", "Referral Reward Paid", fields, None if hide_ts else _ts_utc())


def _private_review(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    tg_id    = d.get("customer_telegram_id", "—")
    product  = d.get("product_name") or "—"
    rating   = d.get("rating", "—")
    stars    = "⭐" * int(rating) if str(rating).isdigit() else str(rating)
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else None
    fields = [
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Product", product),
        ("Rating", f"{stars} ({rating}/5)"),
        ("Order ID", order_disp),
    ]
    return _render("⭐", "Review Submitted", fields, None if hide_ts else _ts_utc())


def _private_restocked(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    product  = d.get("product_name") or "—"
    qty_add  = d.get("quantity_added")
    fields = [
        ("Product", product),
        ("Units Added", qty_add),
    ]
    return _render("📦", "Restocked", fields, None if hide_ts else _ts_utc())


def _private_out_of_stock(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    product  = d.get("product_name") or "—"
    fields = [
        ("Product", product),
    ]
    return _render("📦", "Out of Stock", fields, None if hide_ts else _ts_utc())


def _private_user_registered(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    name     = d.get("name")
    uname    = ("@" + d["username"]) if d.get("username") else None
    tg_id    = d.get("telegram_id", "—")
    ref_by   = d.get("referred_by") or "Organic"
    fields = [
        ("Name", name),
        ("Username", uname),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Referred By", ref_by),
    ]
    return _render("👤", "New Registration", fields, None if hide_ts else _ts_utc())


def _private_login_alert(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    tg_id    = d.get("telegram_id", "—")
    uname    = d.get("username")
    fields = [
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Username", uname),
    ]
    return _render("🔐", "Login Alert", fields, None if hide_ts else _ts_utc())


def _private_invoice(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    from utils.helpers import format_order_id as _fmt_oid
    tg_id    = d.get("customer_telegram_id", "—")
    order_id = d.get("order_id")
    order_disp = _fmt_oid(int(order_id)) if order_id not in (None, "—") else "—"
    amount   = d.get("amount", 0.0)
    fields = [
        ("Order ID", order_disp),
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Amount", _fmt_price(amount)),
    ]
    return _render("🧾", "Invoice Generated", fields, None if hide_ts else _ts_utc())


def _private_fraud(d: Dict[str, Any], cfg) -> str:
    hide_ts  = cfg.get_bool("af_hide_time", False)
    tg_id    = d.get("telegram_id", "—")
    reason   = d.get("reason")
    fields = [
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Reason", reason),
    ]
    return _render("🚫", "Fraud Detected", fields, None if hide_ts else _ts_utc())


def _private_support_ticket(d: Dict[str, Any], cfg) -> str:
    hide_ts   = cfg.get_bool("af_hide_time", False)
    tg_id     = d.get("customer_telegram_id", "—")
    subject   = d.get("subject") or "—"
    category  = d.get("category") or "General"
    fields = [
        ("Telegram ID", f"<code>{tg_id}</code>"),
        ("Subject", subject),
        ("Category", category),
    ]
    return _render("💬", "New Support Ticket", fields, None if hide_ts else _ts_utc())


def _private_admin_action(d: Dict[str, Any], cfg) -> str:
    hide_ts = cfg.get_bool("af_hide_time", False)
    admin   = d.get("admin_id", "—")
    action  = d.get("action") or "—"
    target  = d.get("target")
    fields = [
        ("Admin", f"<code>{admin}</code>"),
        ("Action", action),
        ("Target", target),
    ]
    return _render("🛠", "Admin Action", fields, None if hide_ts else _ts_utc())

_PRIVATE_FORMATTERS = {
    EVENT_NEW_ORDER:         _private_new_order,
    EVENT_WALLET_TOPUP:      _private_wallet_topup,
    EVENT_FAILED_PAYMENT:    _private_failed_payment,
    EVENT_REFUND:            _private_refund,
    EVENT_DELIVERY:          _private_delivery,
    EVENT_ORDER_CANCELLED:   _private_order_cancelled,
    EVENT_COUPON_USED:       _private_coupon_used,
    EVENT_REFERRAL_REWARD:   _private_referral_reward,
    EVENT_REVIEW_SUBMITTED:  _private_review,
    EVENT_PRODUCT_RESTOCKED: _private_restocked,
    EVENT_OUT_OF_STOCK:      _private_out_of_stock,
    EVENT_USER_REGISTERED:   _private_user_registered,
    EVENT_LOGIN_ALERT:       _private_login_alert,
    EVENT_INVOICE_GENERATED: _private_invoice,
    EVENT_FRAUD_DETECTED:    _private_fraud,
    EVENT_SUPPORT_TICKET:    _private_support_ticket,
    EVENT_ADMIN_ACTION:      _private_admin_action,
}


def _format_private(event_type: str, data: Dict[str, Any], cfg) -> str:
    formatter = _PRIVATE_FORMATTERS.get(event_type)
    if formatter:
        try:
            return formatter(data, cfg)
        except Exception:
            logger.exception("ActivityFeed: private formatter error for %s", event_type)
    # Fallback for any event without a dedicated formatter
    fields = [(k.replace("_", " ").title(), v) for k, v in data.items()]
    return _render("📡", f"Activity: {event_type}", fields)


# ---------------------------------------------------------------------------
# Public feed formatter — privacy-safe purchase announcements
# ---------------------------------------------------------------------------

def _format_public(event_type: str, data: Dict[str, Any], cfg) -> Optional[str]:
    """Return public announcement text or None to skip."""
    if event_type not in _PUBLIC_ELIGIBLE:
        return None

    anon       = cfg.get_bool("af_anonymous_names", False)
    hide_price = cfg.get_bool("af_hide_prices", False)
    hide_qty   = cfg.get_bool("af_hide_quantity", False)
    hide_prod  = cfg.get_bool("af_hide_product_name", False)
    hide_ts    = cfg.get_bool("af_hide_time", False)

    raw_name = (data.get("customer_name") or
                data.get("customer_username") or "Someone")
    display  = "Someone" if anon else _mask(raw_name)

    product  = "a product" if hide_prod else (data.get("product_name") or "a product")
    price    = data.get("price", 0.0)
    qty      = data.get("quantity", 1)
    del_type = data.get("delivery_type", "Instant Delivery")

    lines = ["🎉 <b>New Purchase!</b>", ""]
    lines.append(f"👤 {display}")
    if not hide_prod:
        lines.append(f"📦 {product}")
    if not hide_price:
        lines.append(f"💰 {_fmt_price(price)}")
    if not hide_qty and int(qty) > 1:
        lines.append(f"📦 Qty: {qty}")
    lines.append(f"⚡ {del_type}")
    if not hide_ts:
        lines.append("🕒 Just Now")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Channel delivery with retry
# ---------------------------------------------------------------------------

async def _send(bot, channel_id: str, text: str, pin: bool = False) -> bool:
    """Send text to a channel. Retries up to 3 times. Returns True on success."""
    for attempt in range(3):
        try:
            msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            # Optional auto-delete
            try:
                from utils.bot_config import cfg as _cfg2
                delay = _cfg2.get_int("af_auto_delete_seconds", 0)
                if delay > 0 and msg:
                    async def _del(b, cid, mid, secs):
                        await asyncio.sleep(secs)
                        try:
                            await b.delete_message(chat_id=cid, message_id=mid)
                        except Exception:
                            pass
                    asyncio.create_task(_del(bot, channel_id, msg.message_id, delay))
            except Exception:
                pass
            # Optional pin
            if pin and msg:
                try:
                    await bot.pin_chat_message(
                        chat_id=channel_id,
                        message_id=msg.message_id,
                        disable_notification=True,
                    )
                except Exception:
                    pass
            return True
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.warning(
                    "ActivityFeed: send to %s failed after 3 attempts: %s", channel_id, e
                )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def post_event(bot, event_type: str, data: Dict[str, Any]) -> None:
    """Post an activity event to all configured feed channels.

    Best-effort — swallows all exceptions. Always call via
    asyncio.create_task() to stay non-blocking.

    Args:
        bot:        Telegram Bot instance (context.bot)
        event_type: One of the EVENT_* constants in this module
        data:       Event payload — plain serialisable values (str/int/float)
    """
    try:
        cfg = _c()

        # Master status gate
        if cfg.get_str("af_status", "enabled") != "enabled":
            return

        # Per-event filter
        ev_key = _EVENT_KEYS.get(event_type, event_type)
        if not cfg.get_bool(f"af_event_{ev_key}", True):
            return

        pin = cfg.get_bool("af_pin_important", False) and event_type in (
            EVENT_NEW_ORDER, EVENT_FRAUD_DETECTED
        )

        # ── Private feed ─────────────────────────────────────────────────────
        if cfg.get_bool("af_private_enabled", False):
            primary = cfg.get_str("af_private_channel_id", "").strip()
            extras  = _parse_channels(cfg.get_str("af_private_extra_channels", ""))
            targets = ([primary] if primary else []) + extras
            if targets:
                text = _format_private(event_type, data, cfg)
                for ch in targets:
                    asyncio.create_task(_send(bot, ch, text, pin=pin))

        # ── Public feed ──────────────────────────────────────────────────────
        if cfg.get_bool("af_public_enabled", False) and event_type in _PUBLIC_ELIGIBLE:
            primary = cfg.get_str("af_public_channel_id", "").strip()
            extras  = _parse_channels(cfg.get_str("af_public_extra_channels", ""))
            targets = ([primary] if primary else []) + extras
            if targets:
                text = _format_public(event_type, data, cfg)
                if text:
                    for ch in targets:
                        asyncio.create_task(_send(bot, ch, text, pin=False))

    except Exception:
        logger.exception("ActivityFeed: unhandled error in post_event(%s)", event_type)
