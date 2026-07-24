"""Admin notification fan-out helper.

Reads per-admin preferences from ``AdminNotificationPref`` (falls back to the
global ``BotConfig`` ``notif_*`` toggles) and sends the message via the bot.
Failures are swallowed — notifications must never break the business flow.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from config.settings import settings
from database import get_db_session
from database.models import AdminNotificationPref, TicketPriority
from utils.bot_config import cfg

try:
    from utils.permissions import list_admins as _list_admins
except Exception:  # pragma: no cover — defensive only
    def _list_admins(include_inactive: bool = False):
        return []

logger = logging.getLogger(__name__)


# event key -> (pref-field | None, BotConfig fallback key)
#
# ``pref-field`` is a column on ``AdminNotificationPref`` when the event
# supports a per-admin override; it is ``None`` for events that are
# global-only (no schema change needed — they live entirely in the
# BotConfig key/value store, same pattern already used by the
# Notification Settings module for mode/channel).
_EVENTS = {
    "new_order":        ("new_order",        "notif_new_order"),
    "manual_payment":   ("manual_payment",   "notif_manual_payment"),
    "dispute":          ("dispute",          "notif_dispute"),
    "low_stock":        ("low_stock",        "notif_low_stock"),
    "refund":           ("refund",           "notif_refund"),
    "ticket_reply":     ("ticket_reply",     "notif_ticket_reply"),
    "subscription":     ("subscription",     "notif_subscription"),
    "sla_warning":      ("sla_warning",      "notif_sla_warning"),
    "sla_breach":       ("sla_breach",       "notif_sla_breach"),
    # ── Enterprise Admin Notification System ──────────────────────────────
    "new_user":         ("new_user",         "notif_new_user"),
    "deposit":          ("deposit",          "notif_deposit"),
    "payment_failed":   ("payment_failed",   "notif_payment_failed"),
    "payment_expired":  ("payment_expired",  "notif_payment_expired"),
    "payment_reversed": ("payment_reversed", "notif_payment_reversed"),
    "order_delivered":  ("order_delivered",  "notif_order_delivered"),
    # ── Fix: this event was fired by services/fraud_detection.py but was
    # never registered here, so ``_wants()`` returned False for every
    # admin and the alert was silently dropped before ``bot.send_message``
    # was ever called. ──────────────────────────────────────────────────
    "fraud_alert":      (None,               "notif_fraud_alert"),
    # ── New: Orders category — placeholders for event types the example
    # spec asks for that no code path currently emits. Kept global-only
    # (no DB column) and clearly marked in the settings UI as "not yet
    # wired to a live event" so toggling them is honest about doing
    # nothing until a future change fires them. ─────────────────────────
    "order_failed":     (None,               "notif_order_failed"),
    "manual_delivery":  (None,               "notif_manual_delivery"),
    "delivery_failed":  (None,               "notif_delivery_failed"),
    # ── New: Coupons category — no coupon-notification call site exists
    # anywhere in this codebase today; same "not yet wired" caveat. ─────
    "coupon_created":   (None,               "notif_coupon_created"),
    "coupon_used":      (None,               "notif_coupon_used"),
    "coupon_expired":   (None,               "notif_coupon_expired"),
    # ── New: System category — wired to services/health_monitor.py,
    # which used to send straight to ``settings.ADMIN_TELEGRAM_ID`` and
    # bypass every preference/mode/channel setting. ─────────────────────
    "system_alert":     (None,               "notif_system_alert"),
}


# Category → ordered list of event keys, for the Notification Settings UI.
# ``live=True`` events are actually emitted by a code path today;
# ``live=False`` events are additive placeholders (see comments above).
NOTIFICATION_CATALOG = [
    ("orders", "🛒 Orders", [
        ("new_order",       "New Order",        False),
        ("order_delivered", "Order Completed",  True),
        ("order_failed",    "Order Failed",     False),
        ("refund",          "Refunded",         False),
        ("manual_delivery", "Manual Delivery",  False),
        ("delivery_failed", "Delivery Failed",  False),
    ]),
    ("payments", "💳 Payments", [
        ("deposit",          "Deposit Completed",         True),
        ("manual_payment",   "Manual Payment Submitted",  True),
        ("payment_failed",   "Payment Failed",            True),
        ("payment_expired",  "Payment Expired",           True),
        ("payment_reversed", "Payment Reversed",          True),
        ("subscription",     "Subscription Renewal Failed", True),
    ]),
    ("users", "👤 Users", [
        ("new_user", "New User Registration", True),
    ]),
    ("coupons", "🎟 Coupons", [
        ("coupon_created", "Coupon Created", False),
        ("coupon_used",    "Coupon Used",    False),
        ("coupon_expired", "Coupon Expired", False),
    ]),
    ("inventory", "📦 Inventory", [
        ("low_stock", "Low Stock", True),
    ]),
    ("support", "💬 Support", [
        ("ticket_reply", "Ticket Reply",  False),
        ("dispute",      "New Dispute",   False),
        ("sla_warning",  "SLA Warning",   True),
        ("sla_breach",   "SLA Breached",  True),
    ]),
    ("system", "⚙️ System", [
        ("fraud_alert",   "Fraud Alert",        True),
        ("system_alert",  "API / Health Alert", True),
    ]),
]


# ─────────────────────────────────────────────────────────────────────────
# V16: Priority-Based Ticketing — SLA deadlines & auto-reminders
# ─────────────────────────────────────────────────────────────────────────
# Default hours-to-respond per priority. All admin-tunable via BotConfig
# keys ``sla_hours_<priority>`` so store owners can adjust without a
# deploy (see admin_config_handlers.py's generic cfg editor, or
# utils/bot_config.py directly).
_DEFAULT_SLA_HOURS = {
    TicketPriority.URGENT: 2,
    TicketPriority.HIGH: 8,
    TicketPriority.MEDIUM: 24,
    TicketPriority.LOW: 72,
}


def sla_hours_for(priority: TicketPriority) -> float:
    """Hours support has to respond/resolve before SLA is breached."""
    key = f"sla_hours_{priority.value}"
    default = _DEFAULT_SLA_HOURS.get(priority, 24)
    val = cfg.get_float(key, float(default))
    return val if val > 0 else float(default)


def sla_reminder_lead_minutes() -> int:
    """How long before the deadline the admin gets the 'about to miss SLA' nudge."""
    return max(1, cfg.get_int("sla_reminder_lead_minutes", 30))


def compute_sla_deadline(priority: TicketPriority, from_time: Optional[datetime] = None) -> datetime:
    """Return the SLA deadline for a ticket/dispute opened at ``from_time`` (default: now)."""
    start = from_time or datetime.utcnow()
    return start + timedelta(hours=sla_hours_for(priority))


async def _sla_scan_support_tickets(bot: Bot) -> tuple:
    """Check open support tickets for SLA warnings/breaches. Returns (warned, breached)."""
    from database.models import SupportTicket, TicketStatus

    warned = breached = 0
    now = datetime.utcnow()
    lead = timedelta(minutes=sla_reminder_lead_minutes())

    with get_db_session() as s:
        tickets = (s.query(SupportTicket)
                   .filter(SupportTicket.status == TicketStatus.OPEN,
                           SupportTicket.sla_deadline.isnot(None))
                   .all())
        to_warn, to_breach = [], []
        for tk in tickets:
            if tk.sla_breached:
                continue
            if tk.sla_deadline <= now:
                tk.sla_breached = True
                to_breach.append((tk.id, tk.subject, tk.priority.value))
            elif not tk.sla_reminder_sent and (tk.sla_deadline - now) <= lead:
                tk.sla_reminder_sent = True
                to_warn.append((tk.id, tk.subject, tk.priority.value,
                                int((tk.sla_deadline - now).total_seconds() // 60)))
        s.commit()

    from utils.notify_format import render as _render, utc_now_str as _ts
    for tid, subject, prio, mins_left in to_warn:
        await notify_admins(
            bot, "sla_warning",
            _render("⏰", f"SLA Warning — Ticket #{tid}", [
                ("Priority", prio.upper()),
                ("Subject", subject[:80]),
                ("Time left", f"{mins_left} min"),
            ], _ts()),
        )
        warned += 1
    for tid, subject, prio in to_breach:
        await notify_admins(
            bot, "sla_breach",
            _render("🚨", f"SLA Breached — Ticket #{tid}", [
                ("Priority", prio.upper()),
                ("Subject", subject[:80]),
            ], _ts()),
        )
        breached += 1
    return warned, breached


async def _sla_scan_disputes(bot: Bot) -> tuple:
    """Check open disputes for SLA warnings/breaches. Returns (warned, breached)."""
    from database.models import Dispute, DisputeStatus

    warned = breached = 0
    now = datetime.utcnow()
    lead = timedelta(minutes=sla_reminder_lead_minutes())

    with get_db_session() as s:
        disputes = (s.query(Dispute)
                    .filter(Dispute.status == DisputeStatus.OPENED,
                            Dispute.sla_deadline.isnot(None))
                    .all())
        to_warn, to_breach = [], []
        for d in disputes:
            if d.sla_breached:
                continue
            if d.sla_deadline <= now:
                d.sla_breached = True
                to_breach.append((d.id, d.order_id, d.priority.value))
            elif not d.sla_reminder_sent and (d.sla_deadline - now) <= lead:
                d.sla_reminder_sent = True
                to_warn.append((d.id, d.order_id, d.priority.value,
                                int((d.sla_deadline - now).total_seconds() // 60)))
        s.commit()

    from utils.notify_format import render as _render, utc_now_str as _ts
    for did, order_id, prio, mins_left in to_warn:
        await notify_admins(
            bot, "sla_warning",
            _render("⏰", f"SLA Warning — Dispute #{did}", [
                ("Order", f"#{order_id}"),
                ("Priority", prio.upper()),
                ("Time left", f"{mins_left} min"),
            ], _ts()),
        )
        warned += 1
    for did, order_id, prio in to_breach:
        await notify_admins(
            bot, "sla_breach",
            _render("🚨", f"SLA Breached — Dispute #{did}", [
                ("Order", f"#{order_id}"),
                ("Priority", prio.upper()),
            ], _ts()),
        )
        breached += 1
    return warned, breached


async def sla_reminder_job(context) -> None:
    """JobQueue entrypoint — run periodically (see ``bot.py``).

    Scans open support tickets and disputes and fires a one-time
    "about to miss SLA" reminder shortly before the deadline, then a
    one-time "SLA breached" alert once it's actually missed. Both are
    idempotent (guarded by ``sla_reminder_sent`` / ``sla_breached``
    flags) so re-running the job never spams the admin.
    """
    try:
        tw, tb = await _sla_scan_support_tickets(context.bot)
        dw, db_ = await _sla_scan_disputes(context.bot)
        if tw or tb or dw or db_:
            logger.info("SLA scan: tickets(warned=%d, breached=%d) disputes(warned=%d, breached=%d)",
                       tw, tb, dw, db_)
    except Exception:
        logger.exception("sla_reminder_job failed")


def _admin_ids() -> list:
    """Return every admin Telegram ID that should be considered for a DM.

    Bugfix: this used to return only ``settings.ADMIN_TELEGRAM_ID``, so any
    additional admin configured via ``utils.permissions.list_admins()`` —
    which is exactly what ``AdminNotificationPref`` is per-admin *for* —
    could set their own preferences and still never receive anything.
    """
    ids = set()
    if settings.ADMIN_TELEGRAM_ID:
        ids.add(int(settings.ADMIN_TELEGRAM_ID))
    try:
        for row in _list_admins():
            tid = row.get("telegram_id")
            if tid:
                ids.add(int(tid))
    except Exception:
        logger.exception("notifications: list_admins failed, falling back to owner only")
    return list(ids)


def _wants(admin_id: int, event: str) -> bool:
    if event not in _EVENTS:
        return False
    pref_field, cfg_key = _EVENTS[event]
    if pref_field is None:
        # Global-only event — no per-admin override exists, everyone
        # shares the single BotConfig toggle.
        return cfg.get_bool(cfg_key, True)
    with get_db_session() as s:
        row = (s.query(AdminNotificationPref)
               .filter(AdminNotificationPref.admin_telegram_id == admin_id)
               .first())
        if row is not None:
            return bool(getattr(row, pref_field, True))
    return cfg.get_bool(cfg_key, True)


def _delivery_targets() -> tuple:
    """Read the Notification Settings (``nsm``) mode/channel config.

    Returns ``(send_to_admins, channel_id_or_None)``. Falls back to
    "admin only" if the log channel isn't configured/verified, so a
    half-finished channel setup never silently blackholes notifications.
    """
    mode = cfg.get_str("notif_settings_mode", "admin").strip().lower()
    chan_id = cfg.get_str("notif_settings_log_channel_id", "").strip()
    verified = cfg.get_bool("notif_settings_log_channel_verified", False)

    channel = chan_id if (chan_id and verified) else None
    if mode == "log_channel" and channel:
        return False, channel
    if mode == "both" and channel:
        return True, channel
    # "admin" mode, or "log_channel"/"both" requested but not configured yet.
    return True, None


async def notify_admins(bot: Bot, event: str, text: str,
                        parse_mode: Optional[str] = ParseMode.HTML) -> int:
    """Fan out `text` per the admin's Notification Settings. Returns count sent.

    Honors the delivery mode configured in Notification Settings
    (Admin Only / Log Channel Only / Admin + Log Channel) — previously
    this always DM'd the admin(s) directly and ignored that setting
    entirely, so "Log Channel" and "Admin + Log Channel" modes appeared
    to work in the settings UI but never actually delivered to the
    channel for real events.

    V37: Also records the notification in the centralized Notification Center.
    """
    # ── V37: Store in Notification Center ────────────────────────────────────
    try:
        from services import notification_center_service as _ncs
        import re as _re
        _title = event.replace("_", " ").title()
        _body_clean = _re.sub(r"<[^>]+>", "", text)[:500]
        _ncs.create(event_type=event, title=_title, body=_body_clean)
    except Exception:
        logger.exception("notify_admins: notification_center_service.create failed for event=%s", event)

    send_to_admins, channel_id = _delivery_targets()
    sent = 0

    if send_to_admins:
        for admin_id in _admin_ids():
            try:
                if not _wants(admin_id, event):
                    continue
                await bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode)
                sent += 1
            except Exception:
                logger.exception("notify_admins failed for %s / %s", admin_id, event)

    if channel_id:
        try:
            # The per-event toggle still governs the channel copy too —
            # an event an admin disabled shouldn't appear in the log
            # channel just because the delivery mode includes it.
            if event not in _EVENTS or cfg.get_bool(_EVENTS[event][1], True):
                dest = int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
                await bot.send_message(chat_id=dest, text=text, parse_mode=parse_mode)
                sent += 1
        except Exception:
            logger.exception("notify_admins failed for channel %s / %s", channel_id, event)

    return sent


def notify_admins_sync(bot: Bot, event: str, text: str,
                       parse_mode: Optional[str] = ParseMode.HTML) -> None:
    """Fire-and-forget wrapper for sync call sites."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(notify_admins(bot, event, text, parse_mode))
        else:
            loop.run_until_complete(notify_admins(bot, event, text, parse_mode))
    except Exception:
        logger.exception("notify_admins_sync failed")


def get_prefs(admin_id: int) -> dict:
    """Fetch effective prefs for an admin (row/global-config fallback)."""
    out = {}
    with get_db_session() as s:
        row = (s.query(AdminNotificationPref)
               .filter(AdminNotificationPref.admin_telegram_id == admin_id)
               .first())
        for event, (field, cfg_key) in _EVENTS.items():
            if field is None:
                # Global-only event — same value for every admin.
                out[event] = cfg.get_bool(cfg_key, True)
            elif row is not None:
                out[event] = bool(getattr(row, field, True))
            else:
                out[event] = cfg.get_bool(cfg_key, True)
    return out


def toggle_pref(admin_id: int, event: str) -> bool:
    """Toggle a preference and return the new value.

    Per-admin events flip the admin's own ``AdminNotificationPref`` row.
    Global-only events (no DB column — see ``_EVENTS``) flip the shared
    BotConfig key instead, same as the rest of Notification Settings.
    """
    if event not in _EVENTS:
        return False
    field, cfg_key = _EVENTS[event]
    if field is None:
        new_val = not cfg.get_bool(cfg_key, True)
        cfg.set(cfg_key, new_val)
        return new_val
    with get_db_session() as s:
        row = (s.query(AdminNotificationPref)
               .filter(AdminNotificationPref.admin_telegram_id == admin_id)
               .first())
        if row is None:
            row = AdminNotificationPref(admin_telegram_id=admin_id)
            s.add(row)
            s.flush()
        new_val = not bool(getattr(row, field, True))
        setattr(row, field, new_val)
        s.commit()
        return new_val
