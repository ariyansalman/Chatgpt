"""Admin Subscription Reminder Settings — V22.

Callback namespace: ``acc:srm:*`` (routed through admin_control_center's
``_route_section_action("srm", action, rest, update, context)``).

Entry via:
    acc:sec:subrem         → subscription_reminders_menu (main panel)

Sub-actions:
    acc:srm:menu           → main panel
    acc:srm:status:<s>     → set feature status (enable/maint/disable)
    acc:srm:days:<preset>  → set reminder-days preset
    acc:srm:tmpl:<n>       → set notification template (1/2/3)
    acc:srm:time:<t>       → set send-time window
    acc:srm:retry:on|off   → toggle retry-failed-notifications
    acc:srm:remind:all     → trigger manual scan NOW for all eligible subs
    acc:srm:remind:<id>    → trigger manual reminder for specific subscription

Admin Panel layout
──────────────────
🔔 SUBSCRIPTION REMINDER SETTINGS

Feature Status:
  [🟢 Enable]   [🟡 Maintenance]   [🔴 Disable]

Settings:
  • Reminder Days:            30,15,7,3,1  [change presets]
  • Notification Template:    Template 1   [cycle]
  • Send Time:                Any time     [change]
  • Retry Failed:             ON/OFF       [toggle]

Statistics:
  • Total Active Subscriptions: 42
  • Expiring Today:             3
  • Expiring This Week:         8
  • Expired:                    12
  • Reminders Sent:             67

[📤 Send All Pending Reminders]   [⬅️ Control Center]
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.bot_config import cfg
from utils.permissions import has_permission
from ._acc_helpers import require_admin, back_root, send

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

_STATUS_OPTIONS = [
    ("enable",  "🟢 Enable",      "enabled"),
    ("maint",   "🟡 Maintenance", "maintenance"),
    ("disable", "🔴 Disable",     "disabled"),
]

_DAY_PRESETS = [
    ("full",     "30,15,7,3,1", "Full (30/15/7/3/1d)"),
    ("standard", "15,7,3,1",    "Standard (15/7/3/1d)"),
    ("compact",  "7,3,1",       "Compact (7/3/1d)"),
    ("minimal",  "3,1",         "Minimal (3/1d)"),
]

_TEMPLATE_OPTIONS = [
    ("1", "Template 1 (Standard)"),
    ("2", "Template 2 (Detailed)"),
    ("3", "Template 3 (Friendly)"),
]

_TIME_OPTIONS = [
    ("any", "⏱ Any time"),
    ("8",   "🌅 Morning  (08:00 UTC)"),
    ("12",  "☀️ Noon     (12:00 UTC)"),
    ("18",  "🌇 Evening  (18:00 UTC)"),
    ("20",  "🌙 Night    (20:00 UTC)"),
]


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _status() -> str:
    return cfg.get_str("sub_expiry_reminder_status", "enabled").lower()


def _status_label() -> str:
    s = _status()
    for _, label, val in _STATUS_OPTIONS:
        if val == s:
            return label
    return "🟢 Enable"


def _days_label() -> str:
    val = cfg.get_str("sub_expiry_reminder_days", "30,15,7,3,1")
    for _, days, label in _DAY_PRESETS:
        if days == val:
            return label
    return val  # custom value


def _template_label() -> str:
    val = cfg.get_str("sub_expiry_reminder_template", "1")
    for k, label in _TEMPLATE_OPTIONS:
        if k == val:
            return label
    return "Template 1 (Standard)"


def _send_time_label() -> str:
    val = cfg.get_str("sub_expiry_reminder_send_time", "any")
    for k, label in _TIME_OPTIONS:
        if k == val:
            return label
    return "⏱ Any time"


def _retry_label() -> str:
    return "✅ ON" if cfg.get_bool("sub_expiry_reminder_retry_failed", True) else "🚫 OFF"


# ─────────────────────────────────────────────────────────────────────────
# Main panel
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def subscription_reminders_menu(update, context):
    """Render the Subscription Reminder Settings admin panel."""
    from services import subscription_reminder as _svc

    if not has_permission(update.effective_user.id, "manage_settings"):
        q = getattr(update, "callback_query", None)
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    stats = _svc.get_stats()
    status_str = _status_label()
    days_str = _days_label()
    tmpl_str = _template_label()
    time_str = _send_time_label()
    retry_str = _retry_label()

    lines = [
        "🔔 <b>SUBSCRIPTION REMINDER SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {status_str}",
        "",
        "<b>Settings:</b>",
        f"  • Reminder Days: <b>{days_str}</b>",
        f"  • Template:      <b>{tmpl_str}</b>",
        f"  • Send Time:     <b>{time_str}</b>",
        f"  • Retry Failed:  <b>{retry_str}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Total Active Subscriptions: <b>{stats['total_active']}</b>",
        f"  • Expiring Today:             <b>{stats['expiring_today']}</b>",
        f"  • Expiring This Week:         <b>{stats['expiring_this_week']}</b>",
        f"  • Expired:                    <b>{stats['expired']}</b>",
        f"  • Reminders Sent:             <b>{stats['reminders_sent']}</b>",
    ]

    kb = []

    # ── Status row ────────────────────────────────────────────────────────
    status_row = [
        InlineKeyboardButton(label, callback_data=f"acc:srm:status:{key}")
        for key, label, _ in _STATUS_OPTIONS
    ]
    kb.append(status_row)

    kb.append([InlineKeyboardButton("─── Reminder Days ───", callback_data="acc:noop")])
    days_row = []
    for preset_key, _, label in _DAY_PRESETS:
        cur = cfg.get_str("sub_expiry_reminder_days", "30,15,7,3,1")
        # find the days string for this preset
        for k, days, lbl in _DAY_PRESETS:
            if k == preset_key:
                mark = "✅ " if cur == days else ""
                days_row.append(InlineKeyboardButton(
                    f"{mark}{lbl}",
                    callback_data=f"acc:srm:days:{preset_key}",
                ))
                break
    kb.append(days_row[:2])
    if len(days_row) > 2:
        kb.append(days_row[2:])

    kb.append([InlineKeyboardButton("─── Notification Template ───", callback_data="acc:noop")])
    tmpl_row = []
    cur_tmpl = cfg.get_str("sub_expiry_reminder_template", "1")
    for k, lbl in _TEMPLATE_OPTIONS:
        mark = "✅ " if cur_tmpl == k else ""
        tmpl_row.append(InlineKeyboardButton(
            f"{mark}{lbl}",
            callback_data=f"acc:srm:tmpl:{k}",
        ))
    kb.append(tmpl_row)

    kb.append([InlineKeyboardButton("─── Send Time ───", callback_data="acc:noop")])
    cur_time = cfg.get_str("sub_expiry_reminder_send_time", "any")
    time_rows = []
    row = []
    for k, lbl in _TIME_OPTIONS:
        mark = "✅ " if cur_time == k else ""
        row.append(InlineKeyboardButton(f"{mark}{lbl}", callback_data=f"acc:srm:time:{k}"))
        if len(row) == 2:
            time_rows.append(row)
            row = []
    if row:
        time_rows.append(row)
    kb.extend(time_rows)

    kb.append([InlineKeyboardButton("─── Retry Failed Notifications ───", callback_data="acc:noop")])
    retry_on = cfg.get_bool("sub_expiry_reminder_retry_failed", True)
    kb.append([
        InlineKeyboardButton(
            f"{'✅ ON' if retry_on else '☑️ ON'}",
            callback_data="acc:srm:retry:on",
        ),
        InlineKeyboardButton(
            f"{'🚫 OFF' if not retry_on else '☑️ OFF'}",
            callback_data="acc:srm:retry:off",
        ),
    ])

    kb.append([InlineKeyboardButton(
        "📤 Send All Pending Reminders Now",
        callback_data="acc:srm:remind:all",
    )])
    kb.append([back_root()])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    val_map = {"enable": "enabled", "maint": "maintenance", "disable": "disabled"}
    val = val_map.get(key, "enabled")
    cfg.set("sub_expiry_reminder_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        label = {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance", "disabled": "🔴 Disabled"}.get(val, val)
        await q.answer(f"Status set to {label}.", show_alert=False)
    await subscription_reminders_menu(update, context)


@require_admin
async def _set_days(update, context, preset_key: str):
    for k, days, label in _DAY_PRESETS:
        if k == preset_key:
            cfg.set("sub_expiry_reminder_days", days)
            q = getattr(update, "callback_query", None)
            if q:
                await q.answer(f"Reminder days set: {days}", show_alert=False)
            break
    await subscription_reminders_menu(update, context)


@require_admin
async def _set_template(update, context, tmpl_key: str):
    if tmpl_key in ("1", "2", "3"):
        cfg.set("sub_expiry_reminder_template", tmpl_key)
        q = getattr(update, "callback_query", None)
        if q:
            await q.answer(f"Template {tmpl_key} selected.", show_alert=False)
    await subscription_reminders_menu(update, context)


@require_admin
async def _set_time(update, context, time_key: str):
    valid = {k for k, _ in _TIME_OPTIONS}
    if time_key in valid:
        cfg.set("sub_expiry_reminder_send_time", time_key)
        q = getattr(update, "callback_query", None)
        if q:
            await q.answer("Send time updated.", show_alert=False)
    await subscription_reminders_menu(update, context)


@require_admin
async def _set_retry(update, context, value: str):
    cfg.set("sub_expiry_reminder_retry_failed", value == "on")
    q = getattr(update, "callback_query", None)
    if q:
        label = "ON" if value == "on" else "OFF"
        await q.answer(f"Retry failed notifications: {label}", show_alert=False)
    await subscription_reminders_menu(update, context)


@require_admin
async def _trigger_remind_all(update, context):
    """Immediately trigger the expiry reminder scan."""
    from services import subscription_reminder as _svc
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⏳ Running reminder scan...", show_alert=False)
    try:
        n = await _svc.send_expiry_reminders(context.bot)
        msg = f"✅ Scan complete — sent {n} reminder(s)."
    except Exception as exc:
        msg = f"❌ Scan failed: {exc}"
        logger.exception("admin manual remind-all failed")

    from telegram import InlineKeyboardMarkup as IKM
    kb = IKM([[InlineKeyboardButton("🔙 Back", callback_data="acc:srm:menu")], [back_root()]])
    await send(update, msg, kb)


@require_admin
async def _trigger_remind_sub(update, context, sub_id: int):
    """Immediately send reminder for a specific subscription."""
    from services import subscription_reminder as _svc
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⏳ Sending reminder...", show_alert=False)
    ok, msg_txt = await _svc.manual_remind(context.bot, sub_id)
    status_emoji = "✅" if ok else "❌"

    from telegram import InlineKeyboardMarkup as IKM
    kb = IKM([
        [InlineKeyboardButton("🔙 Back to Subscription",
                               callback_data=f"acc:subs:view:{sub_id}")],
        [back_root()],
    ])
    await send(update, f"{status_emoji} {msg_txt}", kb)


# ─────────────────────────────────────────────────────────────────────────
# Router (called by admin_control_center._route_section_action)
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("srm", action, rest, ...)``.

    Possible patterns:
        srm : menu              → main panel
        srm : status : enable   → set status
        srm : days   : full     → set days preset
        srm : tmpl   : 1        → set template
        srm : time   : any      → set time
        srm : retry  : on|off   → toggle retry
        srm : remind : all      → scan all
        srm : remind : <id>     → remind specific sub
    """
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if action == "menu" or not action:
        await subscription_reminders_menu(update, context)
        return

    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return

    if action == "days" and rest:
        await _set_days(update, context, rest[0])
        return

    if action == "tmpl" and rest:
        await _set_template(update, context, rest[0])
        return

    if action == "time" and rest:
        await _set_time(update, context, rest[0])
        return

    if action == "retry" and rest:
        await _set_retry(update, context, rest[0])
        return

    if action == "remind":
        if rest and rest[0] == "all":
            await _trigger_remind_all(update, context)
            return
        if rest and rest[0].isdigit():
            await _trigger_remind_sub(update, context, int(rest[0]))
            return

    # Fallback
    await subscription_reminders_menu(update, context)
