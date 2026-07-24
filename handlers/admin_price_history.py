"""Admin Price History Settings + Manager panel — V23.

Callback namespace:  ``acc:ph:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:ph``

Sub-actions:
    acc:ph:menu              → main panel (settings + stats)
    acc:ph:status:<s>        → set 3-state status (enable/maint/disable)
    acc:ph:max:<n>           → set max records per product
    acc:ph:users:on|off      → toggle allow-users
    acc:ph:diff:on|off       → toggle show-difference
    acc:ph:pct:on|off        → toggle show-pct-change
    acc:ph:name:on|off       → toggle record-admin-name
    acc:ph:mgr:<page>        → paginated history manager
    acc:ph:prod:<pid>:<pg>   → history for a specific product

Panel layout
────────────
📈 PRICE HISTORY SETTINGS

Feature Status: [🟢 Enable] [🟡 Maintenance] [🔴 Disable]

Settings:
  Max Records / Product:  10 / 20 / 50 / 100 / ∞
  Allow Users to View:    ON / OFF
  Show Price Difference:  ON / OFF
  Show % Change:          ON / OFF
  Record Admin Name:      ON / OFF

Statistics:
  Total Records:      1,248
  Today:              12
  This Week:          87
  This Month:         340
  Most Changed:       ...

[📋 History Manager]  [⬅️ Control Center]
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

_MGR_PAGE_SIZE = 10   # records per manager page


def _status() -> str:
    return cfg.get_str("price_history_status", "enabled").lower()


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
async def price_history_admin_menu(update, context):
    """Render the Price History Settings + Stats panel."""
    from services import price_history_service as svc
    stats = svc.get_stats()

    cur_max    = cfg.get_int("price_history_max_records", 50)
    users_on   = cfg.get_bool("price_history_allow_users", True)
    diff_on    = cfg.get_bool("price_history_show_difference", True)
    pct_on     = cfg.get_bool("price_history_show_pct_change", True)
    name_on    = cfg.get_bool("price_history_record_admin_name", True)

    most_str = ", ".join(
        f"{n} ({c})" for n, c in stats["most_changed"][:3]
    ) or "—"

    lines = [
        "📈 <b>PRICE HISTORY SETTINGS</b>",
        "",
        f"<b>Feature Status:</b>  {_status_label()}",
        "",
        "<b>Settings:</b>",
        f"  • Max Records / Product:  <b>{'∞ Unlimited' if cur_max == 0 else cur_max}</b>",
        f"  • Allow Users to View:    <b>{'✅ ON' if users_on else '🚫 OFF'}</b>",
        f"  • Show Price Difference:  <b>{'✅ ON' if diff_on else '🚫 OFF'}</b>",
        f"  • Show % Change:          <b>{'✅ ON' if pct_on else '🚫 OFF'}</b>",
        f"  • Record Admin Name:      <b>{'✅ ON' if name_on else '🚫 OFF'}</b>",
        "",
        "<b>Statistics:</b>",
        f"  • Total Records:           <b>{stats['total']:,}</b>",
        f"  • Today:                   <b>{stats['daily']}</b>",
        f"  • This Week:               <b>{stats['weekly']}</b>",
        f"  • This Month:              <b>{stats['monthly']}</b>",
        f"  • Most Changed:            <b>{most_str}</b>",
    ]

    kb = []

    # Status row
    kb.append([
        InlineKeyboardButton(label, callback_data=f"acc:ph:status:{key}")
        for key, label, _ in _STATUS_OPTS
    ])

    # Max records (split 2 rows: [10 20 50] [100 ∞])
    row1, row2 = [], []
    for key, val, label in _MAX_OPTS[:3]:
        mark = "✅ " if cur_max == val else ""
        row1.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"acc:ph:max:{key}"))
    for key, val, label in _MAX_OPTS[3:]:
        mark = "✅ " if cur_max == val else ""
        row2.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"acc:ph:max:{key}"))
    kb.append(row1)
    kb.append(row2)

    # Toggle rows
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if users_on else '🚫'} Allow Users to View",
            callback_data=f"acc:ph:users:{'off' if users_on else 'on'}",
        ),
    ])
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if diff_on else '🚫'} Show Price Difference",
            callback_data=f"acc:ph:diff:{'off' if diff_on else 'on'}",
        ),
        InlineKeyboardButton(
            f"{'✅' if pct_on else '🚫'} Show % Change",
            callback_data=f"acc:ph:pct:{'off' if pct_on else 'on'}",
        ),
    ])
    kb.append([
        InlineKeyboardButton(
            f"{'✅' if name_on else '🚫'} Record Admin Name",
            callback_data=f"acc:ph:name:{'off' if name_on else 'on'}",
        ),
    ])

    # Manager + Back
    kb.append([
        InlineKeyboardButton("📋 History Manager", callback_data="acc:ph:mgr:0"),
        InlineKeyboardButton("⬅️ Control Center",   callback_data="acc:root"),
    ])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# History Manager (all products, paginated)
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def price_history_manager(update, context, page: int = 0):
    """Show paginated price history records across all products."""
    from services import price_history_service as svc

    search = context.user_data.get("ph_search", "")
    records, total = svc.admin_get_history(search_name=search, page=page)
    pages = svc.total_pages(total, _MGR_PAGE_SIZE)

    header = "📋 <b>Price History Manager</b>"
    if search:
        header += f"  🔍 <i>\"{search}\"</i>"
    header += f"\n<i>{total} record(s)"
    if pages > 1:
        header += f"  •  Page {page + 1}/{pages}"
    header += "</i>"

    if not records:
        lines = [header, "", "No price history records found."]
    else:
        lines = [header, ""]
        for r in records:
            old_p = f"${r['old_price']:.2f}" if r["old_price"] else "—"
            new_p = f"${r['new_price']:.2f}"
            diff  = r["difference"]
            arrow = "📈" if diff > 0 else "📉"
            when  = r["changed_at"].strftime("%b %d %H:%M") if r["changed_at"] else "—"
            name_suffix = f" by {r['changed_by_name']}" if r.get("changed_by_name") else ""
            lines.append(
                f"{arrow} <b>{r['product_name']}</b>  {old_p} → <b>{new_p}</b>"
                f"  <i>{when}{name_suffix}</i>"
            )

    text = "\n".join(lines)
    kb   = []

    # Pagination
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"acc:ph:mgr:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="acc:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"acc:ph:mgr:{page + 1}"))
        kb.append(nav)

    kb.append([
        InlineKeyboardButton("⬅️ Settings", callback_data="acc:ph:menu"),
    ])

    await send(update, text, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def _set_status(update, context, key: str):
    mapping = {opt[0]: opt[2] for opt in _STATUS_OPTS}
    val = mapping.get(key, "enabled")
    cfg.set("price_history_status", val)
    q = getattr(update, "callback_query", None)
    if q:
        label = next((lbl for k, lbl, v in _STATUS_OPTS if v == val), val)
        await q.answer(f"Price History status: {label}", show_alert=False)
    await price_history_admin_menu(update, context)


@require_admin
async def _set_max(update, context, key: str):
    try:
        val = int(key)
    except (ValueError, TypeError):
        val = 50
    cfg.set("price_history_max_records", val)
    q = getattr(update, "callback_query", None)
    if q:
        lbl = "Unlimited" if val == 0 else str(val)
        await q.answer(f"Max records set to {lbl}.", show_alert=False)
    await price_history_admin_menu(update, context)


@require_admin
async def _set_toggle(update, context, key_cfg: str, value_str: str, label: str):
    cfg.set(key_cfg, value_str == "on")
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(
            f"{label}: {'ON' if value_str == 'on' else 'OFF'}", show_alert=False
        )
    await price_history_admin_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    """Entry point from ``_route_section_action("ph", action, rest, ...)``."""
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await price_history_admin_menu(update, context)
        return

    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return

    if action == "max" and rest:
        await _set_max(update, context, rest[0])
        return

    if action == "users" and rest:
        await _set_toggle(update, context, "price_history_allow_users", rest[0], "Allow Users")
        return

    if action == "diff" and rest:
        await _set_toggle(update, context, "price_history_show_difference", rest[0], "Show Difference")
        return

    if action == "pct" and rest:
        await _set_toggle(update, context, "price_history_show_pct_change", rest[0], "Show % Change")
        return

    if action == "name" and rest:
        await _set_toggle(update, context, "price_history_record_admin_name", rest[0], "Record Admin Name")
        return

    if action == "mgr":
        page = int(rest[0]) if rest and rest[0].isdigit() else 0
        await price_history_manager(update, context, page)
        return

    if action == "prod" and rest:
        try:
            pid  = int(rest[0])
            page = int(rest[1]) if len(rest) > 1 else 0
        except (ValueError, IndexError):
            await price_history_admin_menu(update, context)
            return
        # Show admin-level product history (reuses user view, no auth check needed here)
        from services import price_history_service as svc
        from database import get_db_session, Product as _Product
        with get_db_session() as s:
            p = s.query(_Product).filter_by(id=pid).first()
            product_name = p.name if p else f"#{pid}"
        summary = svc.get_product_summary(pid)
        records, total = svc.get_product_history(pid, page, 5)
        from handlers.price_history_handlers import (
            _build_summary_text, _build_timeline_text, _history_keyboard,
        )
        text = _build_summary_text(product_name, summary, pid)
        text += _build_timeline_text(records, page, total)
        kb_rows = []
        pages = svc.total_pages(total, 5)
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"acc:ph:prod:{pid}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="acc:noop"))
            if page < pages - 1:
                nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"acc:ph:prod:{pid}:{page + 1}"))
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("⬅️ Manager", callback_data="acc:ph:mgr:0")])
        await send(update, text, InlineKeyboardMarkup(kb_rows))
        return

    await price_history_admin_menu(update, context)
