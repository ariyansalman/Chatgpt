"""Admin Inventory Reservation Settings + Manager panel — V23.

Callback namespace:  ``acc:irs:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:irs``

Sub-actions:
    acc:irs:menu              → main settings + stats panel
    acc:irs:status:<s>        → 3-state status (enable/maint/disable)
    acc:irs:time:<minutes>    → set reservation TTL (5/10/15/30/60)
    acc:irs:max:<n>           → set max reservations per user
    acc:irs:autorel:on|off    → toggle auto-release job
    acc:irs:manrel:on|off     → toggle allow manual release
    acc:irs:mgr:<page>        → paginated active-reservation manager
    acc:irs:rel:<res_id>      → admin force-release a reservation
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.bot_config import cfg
from ._acc_helpers import require_admin, send

logger = logging.getLogger(__name__)

_STATUS_OPTS = [
    ("enable",  "🟢 Enable",      "enabled"),
    ("maint",   "🟡 Maintenance", "maintenance"),
    ("disable", "🔴 Disable",     "disabled"),
]

_TIME_OPTS = [
    ("5",  5,  "5 min"),
    ("10", 10, "10 min"),
    ("15", 15, "15 min"),
    ("30", 30, "30 min"),
    ("60", 60, "60 min"),
]

_MAX_OPTS = [
    ("1", 1, "1"),
    ("2", 2, "2"),
    ("3", 3, "3"),
    ("0", 0, "∞ Unlimited"),
]


def _status() -> str:
    return cfg.get_str("irs_status", "enabled").lower()


def _status_label() -> str:
    s = _status()
    for _, label, val in _STATUS_OPTS:
        if val == s:
            return label
    return "🟢 Enable"


# ─────────────────────────────────────────────────────────────────────────
# Main Settings Panel
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def irs_admin_menu(update, context):
    """Render the Inventory Reservation Settings + Stats panel."""
    from services import inventory_reservation_ui as svc
    stats = svc.get_stats()

    cur_ttl   = cfg.get_int("inventory_reservation_ttl_minutes", 15)
    cur_max   = cfg.get_int("irs_max_per_user", 1)
    auto_rel  = cfg.get_bool("irs_auto_release", True)
    man_rel   = cfg.get_bool("irs_allow_manual_release", True)

    lines = [
        "⏳ <b>INVENTORY RESERVATION SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {_status_label()}",
        "",
        "<b>Settings:</b>",
        f"  • Reservation Time:         <b>{cur_ttl} minutes</b>",
        f"  • Max Reservations / User:  <b>{'∞ Unlimited' if cur_max == 0 else cur_max}</b>",
        f"  • Auto Release (on expiry): <b>{'✅ ON' if auto_rel else '🚫 OFF'}</b>",
        f"  • Allow Manual Release:     <b>{'✅ ON' if man_rel else '🚫 OFF'}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Active Reservations:   <b>{stats['active']}</b>",
        f"  • Released:              <b>{stats['released']}</b>",
        f"  • Consumed (completed):  <b>{stats['consumed']}</b>",
        f"  • Expired:               <b>{stats['expired']}</b>",
        f"  • Total (all-time):      <b>{stats['total']}</b>",
    ]

    kb = []

    # Status row
    kb.append([
        InlineKeyboardButton(label, callback_data=f"acc:irs:status:{key}")
        for key, label, _ in _STATUS_OPTS
    ])

    # Reservation time (5 / 10 / 15 / 30 / 60)
    time_row = []
    for key, val, label in _TIME_OPTS:
        mark = "✅ " if cur_ttl == val else ""
        time_row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"acc:irs:time:{key}"))
    kb.append(time_row)

    # Max per user
    max_row = []
    for key, val, label in _MAX_OPTS:
        mark = "✅ " if cur_max == val else ""
        max_row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"acc:irs:max:{key}"))
    kb.append(max_row)

    # Toggles
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if auto_rel else '🚫'} Auto Release",
            callback_data=f"acc:irs:autorel:{'off' if auto_rel else 'on'}",
        ),
        InlineKeyboardButton(
            f"{'✅' if man_rel else '🚫'} Manual Release",
            callback_data=f"acc:irs:manrel:{'off' if man_rel else 'on'}",
        ),
    ])

    kb.append([
        InlineKeyboardButton("📋 Active Reservations", callback_data="acc:irs:mgr:0"),
        InlineKeyboardButton("⬅️ Control Center",       callback_data="acc:root"),
    ])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Reservation Manager (active reservations, paginated)
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def irs_manager(update, context, page: int = 0):
    """Show paginated active reservations."""
    from services import inventory_reservation_ui as svc

    records, total = svc.admin_get_active_reservations(page)
    pages = max(1, (total + svc._ADMIN_PAGE_SIZE - 1) // svc._ADMIN_PAGE_SIZE)

    header = f"📋 <b>Active Reservations</b>  ({total} total"
    if pages > 1:
        header += f"  •  Page {page + 1}/{pages}"
    header += ")"

    if not records:
        text = header + "\n\nNo active reservations at this time."
    else:
        lines = [header, ""]
        for r in records:
            lines.append(
                f"⏳ <b>{r['product_name']}</b>"
                f"  qty: {r['quantity']}"
                f"  by {r['user_name']}"
                f"  expires in <b>{r['countdown']}</b>"
            )
        text = "\n".join(lines)

    kb = []

    # Per-record release buttons
    for r in records:
        kb.append([InlineKeyboardButton(
            f"🔓 Release — {r['product_name'][:28]} ({r['user_name']})",
            callback_data=f"acc:irs:rel:{r['id']}",
        )])

    # Pagination
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"acc:irs:mgr:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="acc:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"acc:irs:mgr:{page + 1}"))
        kb.append(nav)

    kb.append([InlineKeyboardButton("⬅️ Settings", callback_data="acc:irs:menu")])
    await send(update, text, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    mapping = {opt[0]: opt[2] for opt in _STATUS_OPTS}
    val = mapping.get(key, "enabled")
    cfg.set("irs_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        label = next((lbl for k, lbl, v in _STATUS_OPTS if v == val), val)
        await q.answer(f"Reservation status: {label}", show_alert=False)
    await irs_admin_menu(update, context)


@require_admin
async def _set_time(update, context, key: str):
    try:
        val = int(key)
    except (ValueError, TypeError):
        val = 15
    cfg.set("inventory_reservation_ttl_minutes", val)
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"Reservation time set to {val} minutes.", show_alert=False)
    await irs_admin_menu(update, context)


@require_admin
async def _set_max(update, context, key: str):
    try:
        val = int(key)
    except (ValueError, TypeError):
        val = 1
    cfg.set("irs_max_per_user", val)
    q = getattr(update, "callback_query", None)
    if q:
        lbl = "Unlimited" if val == 0 else str(val)
        await q.answer(f"Max per user set to {lbl}.", show_alert=False)
    await irs_admin_menu(update, context)


@require_admin
async def _set_toggle(update, context, cfg_key: str, value_str: str, label: str):
    cfg.set(cfg_key, value_str == "on")
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"{label}: {'ON' if value_str == 'on' else 'OFF'}", show_alert=False)
    await irs_admin_menu(update, context)


@require_admin
async def _force_release(update, context, res_id: int):
    from services import inventory_reservation_ui as svc
    ok, msg = svc.admin_release_reservation(res_id)
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(msg, show_alert=not ok)
    await irs_manager(update, context, page=0)


# ─────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("irs", action, rest, ...)``."""
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await irs_admin_menu(update, context)
        return

    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return

    if action == "time" and rest:
        await _set_time(update, context, rest[0])
        return

    if action == "max" and rest:
        await _set_max(update, context, rest[0])
        return

    if action == "autorel" and rest:
        await _set_toggle(update, context, "irs_auto_release", rest[0], "Auto Release")
        return

    if action == "manrel" and rest:
        await _set_toggle(update, context, "irs_allow_manual_release", rest[0], "Manual Release")
        return

    if action == "mgr":
        page = int(rest[0]) if rest and rest[0].isdigit() else 0
        await irs_manager(update, context, page)
        return

    if action == "rel" and rest:
        try:
            res_id = int(rest[0])
        except (ValueError, IndexError):
            await irs_admin_menu(update, context)
            return
        await _force_release(update, context, res_id)
        return

    await irs_admin_menu(update, context)
