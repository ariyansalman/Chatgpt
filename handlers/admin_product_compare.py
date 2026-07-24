"""Admin Product Compare Settings panel — V22.

Callback namespace:  ``acc:pcmp:*``  (routed through admin_control_center)
Section entry:       ``acc:sec:pcmp``

Sub-actions:
    acc:pcmp:menu              → main settings panel
    acc:pcmp:status:<s>        → set 3-state status (enable/maint/disable)
    acc:pcmp:max:<n>           → set max compared products (2/3/4)
    acc:pcmp:counter:on|off    → toggle compare counter on product buttons
    acc:pcmp:best:on|off       → toggle best-value highlighting
    acc:pcmp:unavail:on|off    → toggle show-unavailable-products

Panel layout
────────────
⚖️ PRODUCT COMPARE SETTINGS

Feature Status:  [🟢 Enable]  [🟡 Maintenance]  [🔴 Disable]

Settings:
  Max Products:    [2]  [3]  [4]
  Compare Counter: [ON] [OFF]
  Best Value:      [ON] [OFF]
  Show Unavailable:[ON] [OFF]

Statistics:
  Total Comparisons:      142
  Most Compared Products: Product A (15), Product B (12)
  Avg Products/Comparison: 2.8
  Purchased After Compare: 34

[⬅️ Control Center]
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.bot_config import cfg
from utils.permissions import has_permission
from ._acc_helpers import require_admin, back_root, send

logger = logging.getLogger(__name__)

_STATUS_OPTS = [
    ("enable",  "🟢 Enable",      "enabled"),
    ("maint",   "🟡 Maintenance", "maintenance"),
    ("disable", "🔴 Disable",     "disabled"),
]


def _status() -> str:
    return cfg.get_str("product_compare_status", "enabled").lower()


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
async def product_compare_menu(update, context):
    """Render the Product Compare Settings panel."""
    from services import product_compare as svc
    stats = svc.get_stats()

    cur_max = cfg.get_int("product_compare_max", 4)
    counter_on = cfg.get_bool("product_compare_counter", True)
    best_on = cfg.get_bool("product_compare_best_value", True)
    unavail_on = cfg.get_bool("product_compare_show_unavailable", True)

    # Top-5 most-compared
    most_cmp_lines = ""
    if stats["most_compared"]:
        top = [f"{name} ({cnt})" for name, cnt in stats["most_compared"][:3]]
        most_cmp_lines = ", ".join(top)
    else:
        most_cmp_lines = "—"

    lines = [
        "⚖️ <b>PRODUCT COMPARE SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {_status_label()}",
        "",
        "<b>Settings:</b>",
        f"  • Max Products:          <b>{cur_max}</b>",
        f"  • Compare Counter:       <b>{'✅ ON' if counter_on else '🚫 OFF'}</b>",
        f"  • Highlight Best Value:  <b>{'✅ ON' if best_on else '🚫 OFF'}</b>",
        f"  • Show Unavailable:      <b>{'✅ ON' if unavail_on else '🚫 OFF'}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Total Comparisons:            <b>{stats['total_comparisons']}</b>",
        f"  • Most Compared:                <b>{most_cmp_lines}</b>",
        f"  • Avg Products / Comparison:    <b>{stats['avg_compare_count']}</b>",
        f"  • Purchased After Comparison:   <b>{stats['purchased_after']}</b>",
    ]

    kb = []

    # ── Status row ─────────────────────────────────────────────────────────
    kb.append([
        InlineKeyboardButton(label, callback_data=f"acc:pcmp:status:{key}")
        for key, label, _ in _STATUS_OPTS
    ])

    # ── Max products ───────────────────────────────────────────────────────
    kb.append([
        InlineKeyboardButton(
            f"{'✅ ' if cur_max == n else ''}{n} Max",
            callback_data=f"acc:pcmp:max:{n}",
        )
        for n in (2, 3, 4)
    ])

    # ── Toggle rows ────────────────────────────────────────────────────────
    def _toggle_row(label: str, cur_val: bool, on_cb: str, off_cb: str):
        return [
            InlineKeyboardButton(f"{'✅' if cur_val else '☑️'} ON",    callback_data=on_cb),
            InlineKeyboardButton(f"{'✅' if not cur_val else '☑️'} OFF", callback_data=off_cb),
            InlineKeyboardButton(f"  {label}", callback_data="noop"),
        ]

    kb.append(_toggle_row("Counter",   counter_on,
                          "acc:pcmp:counter:on", "acc:pcmp:counter:off"))
    kb.append(_toggle_row("Best Value", best_on,
                          "acc:pcmp:best:on", "acc:pcmp:best:off"))
    kb.append(_toggle_row("Show Unavailable", unavail_on,
                          "acc:pcmp:unavail:on", "acc:pcmp:unavail:off"))

    kb.append([back_root()])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    val_map = {"enable": "enabled", "maint": "maintenance", "disable": "disabled"}
    val = val_map.get(key, "enabled")
    cfg.set("product_compare_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        labels = {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance",
                  "disabled": "🔴 Disabled"}
        await q.answer(f"Status → {labels.get(val, val)}", show_alert=False)
    await product_compare_menu(update, context)


@require_admin
async def _set_max(update, context, n: str):
    try:
        val = max(2, min(4, int(n)))
    except (ValueError, TypeError):
        val = 4
    cfg.set("product_compare_max", val)
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"Max products set to {val}.", show_alert=False)
    await product_compare_menu(update, context)


@require_admin
async def _set_toggle(update, context, key_cfg: str, value_str: str, label: str):
    cfg.set(key_cfg, value_str == "on")
    q = getattr(update, "callback_query", None)
    if q:
        state = "ON" if value_str == "on" else "OFF"
        await q.answer(f"{label}: {state}", show_alert=False)
    await product_compare_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Router (called by admin_control_center._route_section_action)
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("pcmp", action, rest, ...)``."""
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await product_compare_menu(update, context)
        return

    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return

    if action == "max" and rest:
        await _set_max(update, context, rest[0])
        return

    if action == "counter" and rest:
        await _set_toggle(update, context,
                          "product_compare_counter", rest[0], "Compare Counter")
        return

    if action == "best" and rest:
        await _set_toggle(update, context,
                          "product_compare_best_value", rest[0], "Best Value Highlight")
        return

    if action == "unavail" and rest:
        await _set_toggle(update, context,
                          "product_compare_show_unavailable", rest[0], "Show Unavailable")
        return

    # Fallback
    await product_compare_menu(update, context)
