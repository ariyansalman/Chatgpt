"""Admin Recently Viewed Settings panel — V23.

Callback namespace:  ``acc:rvw:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:rvw``

Sub-actions:
    acc:rvw:menu              → main panel
    acc:rvw:status:<s>        → set 3-state status (enable/maint/disable)
    acc:rvw:max:<n>           → set max history size (10/20/50/100/0)
    acc:rvw:clean:on|off      → toggle auto-clean deleted/inactive products
    acc:rvw:clearall:on|off   → toggle allow-clear-all for users

Panel layout
────────────
🕒 RECENTLY VIEWED SETTINGS

Feature Status: [🟢 Enable] [🟡 Maintenance] [🔴 Disable]

Settings:
  Max History:         10 / 20 / 50 / 100 / ∞
  Auto-Clean Deleted:  ON / OFF
  Allow Clear All:     ON / OFF

Statistics:
  Total Records:          1,248
  Today:                  12
  This Week:              87
  This Month:             340
  Most Viewed:            Product A (45), ...
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
    return cfg.get_str("recently_viewed_status", "enabled").lower()


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
async def recently_viewed_admin_menu(update, context):
    """Render the Recently Viewed Settings admin panel."""
    from services import recently_viewed_service as svc
    stats = svc.get_stats()

    cur_max   = cfg.get_int("feature_recently_viewed_max", 20)
    clean_on  = cfg.get_bool("feature_recently_viewed_clean_deleted", True)
    clear_on  = cfg.get_bool("recently_viewed_allow_clear_all", True)

    def _max_label(val):
        return "∞" if val == 0 else str(val)

    most_str = ", ".join(
        f"{n} ({c})" for n, c in stats["most_viewed"][:3]
    ) or "—"
    top_str = ", ".join(
        f"tg:{uid} ({c})" for uid, c in stats["top_users"][:3]
    ) or "—"

    lines = [
        "🕒 <b>RECENTLY VIEWED SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {_status_label()}",
        "",
        "<b>Settings:</b>",
        f"  • Max History:           <b>{'∞ Unlimited' if cur_max == 0 else cur_max}</b>",
        f"  • Auto-Clean Deleted:    <b>{'✅ ON' if clean_on else '🚫 OFF'}</b>",
        f"  • Allow Clear All:       <b>{'✅ ON' if clear_on else '🚫 OFF'}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Total Records:          <b>{stats['total']:,}</b>",
        f"  • Today:                  <b>{stats['daily']}</b>",
        f"  • This Week:              <b>{stats['weekly']}</b>",
        f"  • This Month:             <b>{stats['monthly']}</b>",
        f"  • Most Viewed:            <b>{most_str}</b>",
        f"  • Top Users:              <b>{top_str}</b>",
    ]

    kb = []

    # Status row
    kb.append([
        InlineKeyboardButton(label, callback_data=f"acc:rvw:status:{key}")
        for key, label, _ in _STATUS_OPTS
    ])

    # Max history (split into 2 rows: [10 20 50] [100 ∞])
    max_row1, max_row2 = [], []
    for key, val, label in _MAX_OPTS[:3]:
        mark = "✅ " if cur_max == val else ""
        max_row1.append(InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"acc:rvw:max:{key}"
        ))
    for key, val, label in _MAX_OPTS[3:]:
        mark = "✅ " if cur_max == val else ""
        max_row2.append(InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"acc:rvw:max:{key}"
        ))
    kb.append(max_row1)
    kb.append(max_row2)

    # Toggle rows
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if clean_on else '🚫'} Auto-Clean Deleted",
            callback_data=f"acc:rvw:clean:{'off' if clean_on else 'on'}",
        )
    ])
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if clear_on else '🚫'} Allow Clear All",
            callback_data=f"acc:rvw:clearall:{'off' if clear_on else 'on'}",
        )
    ])

    # Back
    kb.append([InlineKeyboardButton("⬅️ Control Center", callback_data="acc:root")])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    mapping = {opt[0]: opt[2] for opt in _STATUS_OPTS}
    val = mapping.get(key, "enabled")
    cfg.set("recently_viewed_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        label = next((lbl for k, lbl, v in _STATUS_OPTS if v == val), val)
        await q.answer(f"Recently Viewed status set to: {label}", show_alert=False)
    await recently_viewed_admin_menu(update, context)


@require_admin
async def _set_max(update, context, key: str):
    try:
        val = int(key)
    except (ValueError, TypeError):
        val = 20
    cfg.set("feature_recently_viewed_max", val)
    q = getattr(update, "callback_query", None)
    if q:
        lbl = "Unlimited" if val == 0 else str(val)
        await q.answer(f"Max history set to {lbl}.", show_alert=False)
    await recently_viewed_admin_menu(update, context)


@require_admin
async def _set_toggle(update, context, key_cfg: str, value_str: str, label: str):
    cfg.set(key_cfg, value_str == "on")
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(
            f"{label}: {'ON' if value_str == 'on' else 'OFF'}", show_alert=False
        )
    await recently_viewed_admin_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("rvw", action, rest, ...)``."""
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await recently_viewed_admin_menu(update, context)
        return
    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return
    if action == "max" and rest:
        await _set_max(update, context, rest[0])
        return
    if action == "clean" and rest:
        await _set_toggle(
            update, context,
            "feature_recently_viewed_clean_deleted",
            rest[0],
            "Auto-Clean Deleted",
        )
        return
    if action == "clearall" and rest:
        await _set_toggle(
            update, context,
            "recently_viewed_allow_clear_all",
            rest[0],
            "Allow Clear All",
        )
        return

    await recently_viewed_admin_menu(update, context)
