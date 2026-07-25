"""Admin Favorites Settings panel — V22.

Callback namespace:  ``acc:favs:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:favs``

Sub-actions:
    acc:favs:menu              → main panel
    acc:favs:status:<s>        → set 3-state status (enable/maint/disable)
    acc:favs:max:<n>           → set max favorites (10/20/50/100/0)
    acc:favs:counter:on|off    → toggle favorite counter
    acc:favs:clearall:on|off   → toggle allow-clear-all for users

Panel layout
────────────
❤️ FAVORITES SETTINGS

Feature Status: [🟢 Enable] [🟡 Maintenance] [🔴 Disable]

Settings:
  Max Favorites: [10] [20] [50] [100] [∞]
  Show Counter:  [ON] [OFF]
  Allow Clear All: [ON] [OFF]

Statistics:
  Total Favorites:        1,248
  Today:                  12
  This Week:              87
  This Month:             340
  Most Favorited:         Product A (45), ...
  Top Users:              ...

[⬅️ Control Center]
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.bot_config import cfg
from ._acc_helpers import require_admin, back_root, send

logger = logging.getLogger(__name__)

_STATUS_OPTS = [
    ("enable",  "🟢 Enable",      "enabled"),
    ("maint",   "🟡 Maintenance", "maintenance"),
    ("disable", "🔴 Disable",     "disabled"),
]

_MAX_OPTS = [
    ("10",   10,   "10"),
    ("20",   20,   "20"),
    ("50",   50,   "50"),
    ("100",  100,  "100"),
    ("0",    0,    "∞ Unlimited"),
]


def _status() -> str:
    return cfg.get_str("favorites_status", "enabled").lower()


def _status_label() -> str:
    s = _status()
    for _, label, val in _STATUS_OPTS:
        if val == s:
            return label
    return "🟢 Enable"


# ─────────────────────────────────────────────────────────────────────────
# Main panel
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def favorites_menu(update, context):
    """Render the Favorites Settings admin panel."""
    from services import favorites_service as svc
    stats = svc.get_stats()

    cur_max = cfg.get_int("favorites_max", 50)
    counter_on = cfg.get_bool("favorites_counter", True)
    clear_on   = cfg.get_bool("favorites_allow_clear_all", True)

    def _max_label(val):
        return "∞" if val == 0 else str(val)

    most_str = ", ".join(
        f"{n} ({c})" for n, c in stats["most_favorited"][:3]
    ) or "—"
    top_str = ", ".join(
        f"tg:{uid} ({c})" for uid, c in stats["top_users"][:3]
    ) or "—"

    lines = [
        "❤️ <b>FAVORITES SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {_status_label()}",
        "",
        "<b>Settings:</b>",
        f"  • Max Favorites:    <b>{'∞ Unlimited' if cur_max == 0 else cur_max}</b>",
        f"  • Show Counter:     <b>{'✅ ON' if counter_on else '🚫 OFF'}</b>",
        f"  • Allow Clear All:  <b>{'✅ ON' if clear_on else '🚫 OFF'}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Total Favorites:   <b>{stats['total']:,}</b>",
        f"  • Today:             <b>{stats['daily']}</b>",
        f"  • This Week:         <b>{stats['weekly']}</b>",
        f"  • This Month:        <b>{stats['monthly']}</b>",
        f"  • Most Favorited:    <b>{most_str}</b>",
        f"  • Top Users:         <b>{top_str}</b>",
    ]

    kb = []

    # Status row
    kb.append([
        InlineKeyboardButton(label, callback_data=f"acc:favs:status:{key}")
        for key, label, _ in _STATUS_OPTS
    ])

    # Max favorites (split into 2 rows: [10 20 50] [100 ∞])
    max_row1, max_row2 = [], []
    for key, val, label in _MAX_OPTS[:3]:
        mark = "✅ " if cur_max == val else ""
        max_row1.append(InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"acc:favs:max:{key}"
        ))
    for key, val, label in _MAX_OPTS[3:]:
        mark = "✅ " if cur_max == val else ""
        max_row2.append(InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"acc:favs:max:{key}"
        ))
    kb.append(max_row1)
    kb.append(max_row2)

    # Toggle rows
    def _toggle(label, cur, on_cb, off_cb):
        return [
            InlineKeyboardButton(f"{'✅' if cur else '☑️'} ON",  callback_data=on_cb),
            InlineKeyboardButton(f"{'✅' if not cur else '☑️'} OFF", callback_data=off_cb),
            InlineKeyboardButton(f"  {label}", callback_data="noop"),
        ]

    kb.append(_toggle("Counter",   counter_on,
                      "acc:favs:counter:on",  "acc:favs:counter:off"))
    kb.append(_toggle("Clear All", clear_on,
                      "acc:favs:clearall:on", "acc:favs:clearall:off"))

    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    val_map = {"enable": "enabled", "maint": "maintenance", "disable": "disabled"}
    val = val_map.get(key, "enabled")
    cfg.set("favorites_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        labels = {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance",
                  "disabled": "🔴 Disabled"}
        await q.answer(f"Status → {labels.get(val, val)}", show_alert=False)
    await favorites_menu(update, context)


@require_admin
async def _set_max(update, context, key: str):
    try:
        val = int(key)
    except (ValueError, TypeError):
        val = 50
    cfg.set("favorites_max", val)
    q = getattr(update, "callback_query", None)
    if q:
        lbl = "Unlimited" if val == 0 else str(val)
        await q.answer(f"Max favorites set to {lbl}.", show_alert=False)
    await favorites_menu(update, context)


@require_admin
async def _set_toggle(update, context, key_cfg: str, value_str: str, label: str):
    cfg.set(key_cfg, value_str == "on")
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"{label}: {'ON' if value_str == 'on' else 'OFF'}", show_alert=False)
    await favorites_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("favs", action, rest, ...)``."""
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await favorites_menu(update, context)
        return
    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return
    if action == "max" and rest:
        await _set_max(update, context, rest[0])
        return
    if action == "counter" and rest:
        await _set_toggle(update, context, "favorites_counter", rest[0], "Counter")
        return
    if action == "clearall" and rest:
        await _set_toggle(update, context, "favorites_allow_clear_all", rest[0], "Allow Clear All")
        return

    await favorites_menu(update, context)
