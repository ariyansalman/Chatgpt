"""Admin Dashboard Widget System — V30 (namespace: adw:*).

Entry points:
  adw:main          — main dashboard (page 1)
  adw:main:2        — page 2
  adw:refresh:<pg>  — manual refresh, same page
  adw:period:<key>  — change time period (today|yday|7d|30d|90d)
  adw:manage        — widget manager (enable/disable/reorder/collapse/pin)
  adw:toggle:<id>   — toggle widget visibility
  adw:up:<id>       — move widget up
  adw:dn:<id>       — move widget down
  adw:collapse:<id> — collapse / expand widget
  adw:pin:<id>      — pin / unpin widget
  adw:reset         — reset layout to defaults
  adw:qa            — quick actions panel
  adw:stats         — detailed statistics view
  adw:stats:<period>— stats with specific period
  adw:settings      — dashboard settings panel
  adw:setstatus:<v> — set feature status (enabled|maintenance|disabled)
  adw:toggle_ar     — toggle auto-refresh on/off
  adw:interval:<v>  — set refresh interval (30|60|300|600)
  adw:ar:start      — activate auto-refresh for this session
  adw:ar:stop       — deactivate auto-refresh for this session
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CallbackQueryHandler

from utils.permissions import has_permission
from utils.bot_config import cfg
from utils import format_price
from services.dashboard_widgets import (
    WIDGET_LABELS, WIDGET_IDS, WIDGET_DEFS,
    collect_stats, get_layout, save_layout,
    toggle_widget, move_widget, toggle_collapse, toggle_pin, reset_layout,
)
from services import payment_ui as pui

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

PERIOD_LABELS: dict[str, str] = {
    "today": "Today",
    "yday":  "Yesterday",
    "7d":    "Last 7 Days",
    "30d":   "Last 30 Days",
    "90d":   "Last 90 Days",
}

INTERVAL_LABELS: dict[str, str] = {
    "30":  "30 Seconds",
    "60":  "1 Minute",
    "300": "5 Minutes",
    "600": "10 Minutes",
}

# How many visible widgets to show per dashboard page
_WIDGETS_PER_PAGE = 8
# Job name prefix
_JOB_PREFIX = "adw_autorefresh_"

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _fp(v: float) -> str:
    """Format a monetary value."""
    return format_price(v)


def _check_status() -> str:
    """Return the feature status string."""
    return cfg.get("adw_status", "enabled")


async def _safe_edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _get_period(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("adw_period", "today")


def _ordered_visible(layout: dict[str, Any]) -> list[str]:
    """Return widget IDs in display order: pinned first, then rest, hidden excluded."""
    pinned = [w for w in layout["order"] if w in layout.get("pinned", [])
              and w not in layout.get("hidden", [])]
    rest = [w for w in layout["order"] if w not in layout.get("pinned", [])
            and w not in layout.get("hidden", [])]
    return pinned + rest


# ─── Widget Renderers ─────────────────────────────────────────────────────────

def _render_widget(widget_id: str, stats: dict[str, Any], collapsed: bool) -> str:
    """Return the text block for one widget (3–10 lines)."""
    label = WIDGET_LABELS.get(widget_id, widget_id)
    pin_mark = "📌 " if widget_id in stats.get("_pinned", []) else ""
    header = f"{pin_mark}<b>{label}</b>"

    if collapsed:
        return header + " <i>(collapsed)</i>"

    if widget_id == "revenue_today":
        sparkline = stats.get("revenue_sparkline", "")
        trend = stats.get("revenue_trend", "")
        return (
            f"{header}\n"
            f"  Amount: <b>{_fp(stats.get('revenue_today', 0))}</b>{trend}\n"
            f"  Trend: <code>{sparkline}</code> (7d)"
        )
    if widget_id == "wallet_balance":
        return f"{header}\n  Balance: <b>{_fp(stats.get('wallet_balance', 0))}</b>"
    if widget_id == "revenue_weekly":
        return f"{header}\n  7-Day Total: <b>{_fp(stats.get('revenue_weekly', 0))}</b>"
    if widget_id == "revenue_monthly":
        return f"{header}\n  30-Day Total: <b>{_fp(stats.get('revenue_monthly', 0))}</b>"
    if widget_id == "orders_today":
        trend = stats.get("orders_trend", "")
        return (
            f"{header}\n"
            f"  New: <b>{stats.get('orders_today', 0)}</b>{trend}  "
            f"Total: <b>{stats.get('orders_total', 0)}</b>"
        )
    if widget_id == "orders_pending":
        cnt = stats.get("orders_pending", 0)
        warn = " ⚠️" if cnt > 10 else ""
        return f"{header}\n  Count: <b>{cnt}</b>{warn}"
    if widget_id == "orders_completed":
        return f"{header}\n  In Period: <b>{stats.get('orders_completed', 0)}</b>"
    if widget_id == "orders_cancelled":
        return f"{header}\n  In Period: <b>{stats.get('orders_cancelled', 0)}</b>"
    if widget_id == "users_total":
        trend = stats.get("users_trend", "")
        return (
            f"{header}\n"
            f"  Total: <b>{stats.get('users_total', 0):,}</b>  "
            f"New: <b>{stats.get('users_new_today', 0)}</b>{trend}"
        )
    if widget_id == "users_online":
        return f"{header}\n  Active (24 h): <b>{stats.get('users_online', 0):,}</b>"
    if widget_id == "users_new_today":
        trend = stats.get("users_trend", "")
        return f"{header}\n  Count: <b>{stats.get('users_new_today', 0)}</b>{trend}"
    if widget_id == "deposits_today":
        trend = stats.get("deposits_trend", "")
        return f"{header}\n  Amount: <b>{_fp(stats.get('deposits_today', 0))}</b>{trend}"
    if widget_id == "withdrawals_today":
        return f"{header}\n  Amount: <b>{_fp(stats.get('withdrawals_today', 0))}</b>"
    if widget_id == "referral_earnings":
        return f"{header}\n  In Period: <b>{_fp(stats.get('referral_earnings', 0))}</b>"
    if widget_id == "failed_payments":
        cnt = stats.get("failed_payments", 0)
        warn = " 🚨" if cnt > 0 else ""
        return f"{header}\n  Count: <b>{cnt}</b>{warn}"
    if widget_id == "best_products":
        products = stats.get("best_products", [])
        if not products:
            return f"{header}\n  <i>No data for this period.</i>"
        lines = [header]
        for i, (name, cnt) in enumerate(products[:3], 1):
            lines.append(f"  {i}. {name[:28]} — <b>{cnt}</b> sold")
        return "\n".join(lines)
    if widget_id == "low_stock":
        low_list = stats.get("low_stock_list", [])
        if not low_list:
            return f"{header}\n  <i>All products well-stocked ✅</i>"
        lines = [header]
        for name, qty in low_list[:3]:
            lines.append(f"  • {name[:28]} — <b>{qty}</b> left")
        return "\n".join(lines)
    if widget_id == "active_coupons":
        return f"{header}\n  Active: <b>{stats.get('active_coupons', 0)}</b>"
    if widget_id == "top_customers":
        customers = stats.get("top_customers", [])
        if not customers:
            return f"{header}\n  <i>No data for this period.</i>"
        lines = [header]
        for i, (name, spent) in enumerate(customers[:3], 1):
            lines.append(f"  {i}. @{name} — <b>{_fp(spent)}</b>")
        return "\n".join(lines)
    if widget_id == "system_alerts":
        alerts = stats.get("system_alerts", [])
        if not alerts:
            return f"{header}\n  <i>No active alerts ✅</i>"
        lines = [header] + [f"  {a}" for a in alerts[:4]]
        return "\n".join(lines)
    return header


# ─── Dashboard Pages ──────────────────────────────────────────────────────────

def _build_dashboard_text(stats: dict[str, Any], layout: dict[str, Any],
                           page: int) -> tuple[str, int]:
    """Return (message_text, total_pages)."""
    period = stats.get("period", "today")
    visible = _ordered_visible(layout)
    total_pages = max(1, (len(visible) + _WIDGETS_PER_PAGE - 1) // _WIDGETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    slice_ = visible[page * _WIDGETS_PER_PAGE: (page + 1) * _WIDGETS_PER_PAGE]
    collapsed_set = set(layout.get("collapsed", []))
    pinned_set = set(layout.get("pinned", []))

    # Inject pinned list into stats so renderer can mark them
    stats = dict(stats, _pinned=list(pinned_set))

    now_str = datetime.utcnow().strftime("%H:%M UTC")
    period_label = PERIOD_LABELS.get(period, period)
    lines = [
        "📊 <b>Admin Dashboard</b>",
        pui.DIVIDER,
        f"<i>Period: {period_label}  ·  Updated: {now_str}</i>",
        "",
    ]

    if not slice_:
        lines.append("<i>All widgets hidden. Open Widget Manager to restore.</i>")
    else:
        for wid in slice_:
            lines.append(_render_widget(wid, stats, wid in collapsed_set))
            lines.append("")   # blank line between widgets

    if lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines), total_pages


def _build_dashboard_keyboard(page: int, total_pages: int,
                               period: str, layout: dict[str, Any],
                               admin_tg_id: int, context: ContextTypes.DEFAULT_TYPE,
                               ) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []

    # Period filter row
    period_row = []
    for pk in ("today", "yday", "7d", "30d", "90d"):
        label = ("●" if pk == period else "") + PERIOD_LABELS[pk]
        period_row.append(InlineKeyboardButton(label, callback_data=f"adw:period:{pk}"))
    # Split into two rows of 2-3
    kb.append(period_row[:3])
    kb.append(period_row[3:])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"adw:main:{page - 1}"))
    nav.append(InlineKeyboardButton(f"🔄 Refresh", callback_data=f"adw:refresh:{page}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"adw:main:{page + 1}"))
    kb.append(nav)

    # Feature rows
    kb.append([
        InlineKeyboardButton("🧩 Widgets",     callback_data="adw:manage"),
        InlineKeyboardButton("⚡ Quick Actions", callback_data="adw:qa"),
    ])
    kb.append([
        InlineKeyboardButton("📈 Statistics",  callback_data="adw:stats"),
        InlineKeyboardButton("⚙️ Settings",    callback_data="adw:settings"),
    ])

    # Auto-refresh toggle
    ar_on = bool(context.user_data.get("adw_ar_active"))
    ar_label = "⏹ Stop Auto-Refresh" if ar_on else "▶ Start Auto-Refresh"
    ar_cb = "adw:ar:stop" if ar_on else "adw:ar:start"
    kb.append([InlineKeyboardButton(ar_label, callback_data=ar_cb)])

    kb.append([InlineKeyboardButton("🔙 Back to Control Center", callback_data="acc:root")])
    return InlineKeyboardMarkup(kb)


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def _show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          page: int = 0) -> None:
    query = update.callback_query
    admin_tg_id = update.effective_user.id
    period = _get_period(context)
    layout = get_layout(admin_tg_id)
    stats = collect_stats(period)
    text, total_pages = _build_dashboard_text(stats, layout, page)
    kb = _build_dashboard_keyboard(page, total_pages, period, layout,
                                   admin_tg_id, context)
    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True)


# ─── Widget Manager ───────────────────────────────────────────────────────────

def _build_manager_text_kb(admin_tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    layout = get_layout(admin_tg_id)
    hidden = set(layout.get("hidden", []))
    collapsed = set(layout.get("collapsed", []))
    pinned = set(layout.get("pinned", []))
    order = layout["order"]

    lines = [
        "🧩 <b>Widget Manager</b>",
        "<i>Toggle visibility, reorder, collapse, or pin each widget.</i>",
        "",
    ]
    kb: list[list[InlineKeyboardButton]] = []

    for i, wid in enumerate(order):
        label = WIDGET_LABELS.get(wid, wid)
        is_hidden = wid in hidden
        is_col = wid in collapsed
        is_pin = wid in pinned

        # State indicators in the list text
        flags = []
        if is_pin:
            flags.append("📌")
        if is_col:
            flags.append("⏫")
        if is_hidden:
            flags.append("🚫")
        else:
            flags.append("✅")
        lines.append(f"{''.join(flags)} {label}")

        # Button row for this widget: vis-toggle | up | dn | collapse | pin
        vis_btn = InlineKeyboardButton(
            "👁 Show" if is_hidden else "🚫 Hide",
            callback_data=f"adw:toggle:{wid}",
        )
        up_btn = InlineKeyboardButton("⬆", callback_data=f"adw:up:{wid}")
        dn_btn = InlineKeyboardButton("⬇", callback_data=f"adw:dn:{wid}")
        col_btn = InlineKeyboardButton(
            "⤵ Expand" if is_col else "⤴ Collapse",
            callback_data=f"adw:collapse:{wid}",
        )
        pin_btn = InlineKeyboardButton(
            "📌 Unpin" if is_pin else "📌 Pin",
            callback_data=f"adw:pin:{wid}",
        )
        kb.append([vis_btn, up_btn, dn_btn, col_btn, pin_btn])

    kb.append([InlineKeyboardButton("🔄 Reset Layout", callback_data="adw:reset")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adw:main:0")])

    return "\n".join(lines), InlineKeyboardMarkup(kb)


# ─── Quick Actions ────────────────────────────────────────────────────────────

def _build_qa_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product",    callback_data="admin_create_product"),
         InlineKeyboardButton("👤 Add User",       callback_data="admin_users")],
        [InlineKeyboardButton("📢 Broadcast",      callback_data="admin_broadcast"),
         InlineKeyboardButton("💰 Add Balance",    callback_data="admin_users")],
        [InlineKeyboardButton("🎟 Create Coupon",  callback_data="admin_coupons"),
         InlineKeyboardButton("🔥 Flash Sale",     callback_data="acc:sec:promotions")],
        [InlineKeyboardButton("📦 View Orders",   callback_data="admin_orders"),
         InlineKeyboardButton("💳 View Deposits",  callback_data="admin_confirm_order")],
        [InlineKeyboardButton("⚙️ Settings",       callback_data="admin_settings")],
        [InlineKeyboardButton("🔙 Back",           callback_data="adw:main:0")],
    ])


# ─── Statistics View ──────────────────────────────────────────────────────────

def _build_stats_text(stats: dict[str, Any]) -> str:
    period = PERIOD_LABELS.get(stats.get("period", "today"), "Period")
    rev = stats.get("revenue_today", 0.0)
    wk = stats.get("revenue_weekly", 0.0)
    mo = stats.get("revenue_monthly", 0.0)
    aov = stats.get("avg_order_value", 0.0)
    orders_new = stats.get("orders_today", 0)
    orders_done = stats.get("orders_completed", 0)
    orders_cancel = stats.get("orders_cancelled", 0)
    deps = stats.get("deposits_today", 0.0)
    with_ = stats.get("withdrawals_today", 0.0)
    ref = stats.get("referral_earnings", 0.0)
    users_new = stats.get("users_new_today", 0)
    users_total = stats.get("users_total", 0)
    fail = stats.get("failed_payments", 0)
    spark = stats.get("revenue_sparkline", "")

    conversion = (orders_done / users_new * 100) if users_new > 0 else 0.0

    return (
        "📈 <b>Statistics</b>\n"
        f"{pui.DIVIDER}\n"
        f"<i>Period: {period}</i>\n"
        "\n"
        "💰 <b>Revenue</b>\n"
        f"  Period Sales:  <b>{_fp(rev)}</b>{stats.get('revenue_trend','')}\n"
        f"  Weekly Sales:  <b>{_fp(wk)}</b>\n"
        f"  Monthly Sales: <b>{_fp(mo)}</b>\n"
        f"  Avg Order:     <b>{_fp(aov)}</b>\n"
        f"  7d Trend: <code>{spark}</code>\n"
        "\n"
        "🛒 <b>Orders</b>\n"
        f"  New:       <b>{orders_new}</b>\n"
        f"  Completed: <b>{orders_done}</b>\n"
        f"  Cancelled: <b>{orders_cancel}</b>\n"
        f"  Pending:   <b>{stats.get('orders_pending',0)}</b>\n"
        "\n"
        "💳 <b>Finance</b>\n"
        f"  Deposits:     <b>{_fp(deps)}</b>{stats.get('deposits_trend','')}\n"
        f"  Withdrawals:  <b>{_fp(with_)}</b>\n"
        f"  Referral Earn:<b>{_fp(ref)}</b>\n"
        f"  Failed Pmts:  <b>{fail}</b>\n"
        "\n"
        "👤 <b>Users</b>\n"
        f"  Total:      <b>{users_total:,}</b>\n"
        f"  New Today:  <b>{users_new}</b>{stats.get('users_trend','')}\n"
        f"  Active 24h: <b>{stats.get('users_online',0):,}</b>\n"
        f"  Conversion: <b>{conversion:.1f}%</b>\n"
        "\n"
        "🏪 <b>Store</b>\n"
        f"  Coupons:   <b>{stats.get('active_coupons',0)}</b> active\n"
        f"  Low Stock: <b>{stats.get('low_stock',0)}</b> item(s)\n"
        f"  Tickets:   <b>{stats.get('open_tickets',0)}</b> open\n"
    )


def _build_stats_kb(period: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    period_row1 = [
        InlineKeyboardButton("Today" if period != "today" else "●Today",
                             callback_data="adw:stats:today"),
        InlineKeyboardButton("Yesterday" if period != "yday" else "●Yesterday",
                             callback_data="adw:stats:yday"),
        InlineKeyboardButton("7 Days" if period != "7d" else "●7 Days",
                             callback_data="adw:stats:7d"),
    ]
    period_row2 = [
        InlineKeyboardButton("30 Days" if period != "30d" else "●30 Days",
                             callback_data="adw:stats:30d"),
        InlineKeyboardButton("90 Days" if period != "90d" else "●90 Days",
                             callback_data="adw:stats:90d"),
    ]
    rows.append(period_row1)
    rows.append(period_row2)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="adw:main:0")])
    return InlineKeyboardMarkup(rows)


# ─── Settings Panel ───────────────────────────────────────────────────────────

def _build_settings_text() -> str:
    status = cfg.get("adw_status", "enabled")
    ar = cfg.get_bool("adw_auto_refresh", False)
    interval = cfg.get("adw_refresh_interval", "60")
    charts = cfg.get_bool("adw_charts_enabled", True)
    qa = cfg.get_bool("adw_quick_actions", True)
    stats_on = cfg.get_bool("adw_statistics", True)

    status_emoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "❓")
    return (
        "⚙️ <b>Dashboard Settings</b>\n"
        f"{pui.DIVIDER}\n\n"
        f"Status:           {status_emoji} {status.title()}\n"
        f"Auto-Refresh:     {'✅ ON' if ar else '❌ OFF'}\n"
        f"Refresh Interval: <b>{INTERVAL_LABELS.get(str(interval), str(interval) + 's')}</b>\n"
        f"Charts:           {'✅ ON' if charts else '❌ OFF'}\n"
        f"Quick Actions:    {'✅ ON' if qa else '❌ OFF'}\n"
        f"Statistics:       {'✅ ON' if stats_on else '❌ OFF'}\n"
    )


def _build_settings_kb() -> InlineKeyboardMarkup:
    ar = cfg.get_bool("adw_auto_refresh", False)
    charts = cfg.get_bool("adw_charts_enabled", True)
    qa = cfg.get_bool("adw_quick_actions", True)
    stats_on = cfg.get_bool("adw_statistics", True)

    return InlineKeyboardMarkup([
        # Status
        [InlineKeyboardButton("🟢 Enable",      callback_data="adw:setstatus:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="adw:setstatus:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="adw:setstatus:disabled")],
        # Auto-refresh global toggle
        [InlineKeyboardButton(
            "⏹ Disable Auto-Refresh" if ar else "▶ Enable Auto-Refresh",
            callback_data="adw:toggle_ar",
        )],
        # Interval
        [InlineKeyboardButton("⏱ 30s",  callback_data="adw:interval:30"),
         InlineKeyboardButton("⏱ 1min", callback_data="adw:interval:60"),
         InlineKeyboardButton("⏱ 5min", callback_data="adw:interval:300"),
         InlineKeyboardButton("⏱ 10min", callback_data="adw:interval:600")],
        # Feature toggles
        [InlineKeyboardButton(
            "📊 Charts: OFF" if charts else "📊 Charts: ON",
            callback_data="adw:toggle_charts",
        )],
        [InlineKeyboardButton(
            "⚡ Quick Actions: OFF" if qa else "⚡ Quick Actions: ON",
            callback_data="adw:toggle_qa",
        )],
        [InlineKeyboardButton(
            "📈 Statistics: OFF" if stats_on else "📈 Statistics: ON",
            callback_data="adw:toggle_stats",
        )],
        [InlineKeyboardButton("🔙 Back", callback_data="adw:main:0")],
    ])


# ─── Auto-Refresh Job ─────────────────────────────────────────────────────────

async def _auto_refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: edit the dashboard message in place with fresh data."""
    data = context.job.data  # type: ignore[attr-defined]
    if not data:
        return
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    admin_tg_id = data.get("admin_tg_id")
    page = data.get("page", 0)
    period = data.get("period", "today")

    if not chat_id or not message_id:
        return

    try:
        layout = get_layout(admin_tg_id)
        stats = collect_stats(period)
        text, total_pages = _build_dashboard_text(stats, layout, page)
        # Build a minimal keyboard for the auto-refreshed message
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Stop Auto-Refresh", callback_data="adw:ar:stop"),
            InlineKeyboardButton("🔙 Dashboard", callback_data="adw:main:0"),
        ]])
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug("auto_refresh_job failed (non-fatal): %s", exc)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def adw_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: C901
    """Single dispatcher for all adw:* callbacks."""
    query = update.callback_query
    if query is None:
        return

    # ── Feature status check ──────────────────────────────────────────────────
    status = _check_status()
    if status == "disabled" and not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("🔴 Dashboard is currently disabled.", show_alert=True)
        return
    if status == "maintenance" and not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("🟡 Dashboard is under maintenance.", show_alert=True)
        return

    # ── Permission check ──────────────────────────────────────────────────────
    if not has_permission(update.effective_user.id, "view_analytics"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":")          # ["adw", <action>, ...]
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    rest = parts[3:] if len(parts) > 3 else []
    admin_tg_id = update.effective_user.id

    # ── adw:main[:<page>] ─────────────────────────────────────────────────────
    if action == "main" or action == "":
        try:
            page = int(arg) if arg else 0
        except ValueError:
            page = 0
        await _show_dashboard(update, context, page)
        return

    # ── adw:refresh:<page> ────────────────────────────────────────────────────
    if action == "refresh":
        try:
            page = int(arg)
        except ValueError:
            page = 0
        await _show_dashboard(update, context, page)
        return

    # ── adw:period:<key> ──────────────────────────────────────────────────────
    if action == "period":
        if arg in PERIOD_LABELS:
            context.user_data["adw_period"] = arg
        await _show_dashboard(update, context, 0)
        return

    # ── adw:manage ────────────────────────────────────────────────────────────
    if action == "manage":
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:toggle:<widget_id> ────────────────────────────────────────────────
    if action == "toggle" and arg:
        toggle_widget(admin_tg_id, arg)
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:up / adw:dn ───────────────────────────────────────────────────────
    if action in ("up", "dn") and arg:
        move_widget(admin_tg_id, arg, action)
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:collapse:<widget_id> ──────────────────────────────────────────────
    if action == "collapse" and arg:
        toggle_collapse(admin_tg_id, arg)
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:pin:<widget_id> ───────────────────────────────────────────────────
    if action == "pin" and arg:
        toggle_pin(admin_tg_id, arg)
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:reset ─────────────────────────────────────────────────────────────
    if action == "reset":
        reset_layout(admin_tg_id)
        await query.answer("Layout reset to defaults.", show_alert=True)
        text, kb = _build_manager_text_kb(admin_tg_id)
        await _safe_edit(query, text, kb)
        return

    # ── adw:qa ────────────────────────────────────────────────────────────────
    if action == "qa":
        qa_enabled = cfg.get_bool("adw_quick_actions", True)
        if not qa_enabled:
            await query.answer("Quick Actions are disabled.", show_alert=True)
            return
        kb = _build_qa_kb()
        await _safe_edit(
            query,
            "⚡ <b>Quick Actions</b>\n<i>Tap an action to jump straight to that admin section.</i>",
            kb,
        )
        return

    # ── adw:stats[:<period>] ──────────────────────────────────────────────────
    if action == "stats":
        stats_enabled = cfg.get_bool("adw_statistics", True)
        if not stats_enabled:
            await query.answer("Statistics are disabled.", show_alert=True)
            return
        period = arg if arg in PERIOD_LABELS else _get_period(context)
        if arg in PERIOD_LABELS:
            context.user_data["adw_period"] = period
        stats = collect_stats(period)
        await _safe_edit(query, _build_stats_text(stats), _build_stats_kb(period))
        return

    # ── adw:settings ──────────────────────────────────────────────────────────
    if action == "settings":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ You need manage_settings permission.", show_alert=True)
            return
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── adw:setstatus:<value> ─────────────────────────────────────────────────
    if action == "setstatus":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        if arg in ("enabled", "maintenance", "disabled"):
            cfg.set("adw_status", arg)
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── adw:toggle_ar ─────────────────────────────────────────────────────────
    if action == "toggle_ar":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        current = cfg.get_bool("adw_auto_refresh", False)
        cfg.set("adw_auto_refresh", not current)
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── adw:toggle_charts / toggle_qa / toggle_stats ──────────────────────────
    if action == "toggle_charts":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        cfg.set("adw_charts_enabled", not cfg.get_bool("adw_charts_enabled", True))
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    if action == "toggle_qa":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        cfg.set("adw_quick_actions", not cfg.get_bool("adw_quick_actions", True))
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    if action == "toggle_stats":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        cfg.set("adw_statistics", not cfg.get_bool("adw_statistics", True))
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── adw:interval:<seconds> ────────────────────────────────────────────────
    if action == "interval":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        if arg in INTERVAL_LABELS:
            cfg.set("adw_refresh_interval", arg)
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── adw:ar:start ──────────────────────────────────────────────────────────
    if action == "ar":
        if arg == "start":
            if not cfg.get_bool("adw_auto_refresh", False):
                await query.answer(
                    "Auto-Refresh is disabled in Settings. Enable it first.",
                    show_alert=True,
                )
                return
            interval_sec = int(cfg.get("adw_refresh_interval", "60"))
            page = context.user_data.get("adw_page", 0)
            period = _get_period(context)
            job_name = f"{_JOB_PREFIX}{admin_tg_id}"
            # Cancel any existing job for this admin
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()
            msg = query.message
            context.user_data["adw_ar_active"] = True
            context.user_data["adw_ar_msg_id"] = msg.message_id
            context.job_queue.run_repeating(
                _auto_refresh_job,
                interval=interval_sec,
                first=interval_sec,
                name=job_name,
                data={
                    "chat_id":      msg.chat_id,
                    "message_id":   msg.message_id,
                    "admin_tg_id":  admin_tg_id,
                    "page":         page,
                    "period":       period,
                },
            )
            await query.answer(
                f"✅ Auto-refresh started every {INTERVAL_LABELS.get(str(interval_sec), str(interval_sec) + 's')}.",
                show_alert=True,
            )
            await _show_dashboard(update, context, page)
            return

        if arg == "stop":
            job_name = f"{_JOB_PREFIX}{admin_tg_id}"
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()
            context.user_data["adw_ar_active"] = False
            await query.answer("⏹ Auto-refresh stopped.", show_alert=True)
            await _show_dashboard(update, context, 0)
            return

    # Unknown action — fall back to dashboard
    await _show_dashboard(update, context, 0)


# ─── Entry point for acc:sec:dashboard redirect ────────────────────────────────

async def show_widget_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called from admin_control_center when user taps 📊 Dashboard."""
    await _show_dashboard(update, context, 0)


# ─── Handler Registration ─────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all adw:* callback handlers."""
    application.add_handler(
        CallbackQueryHandler(adw_dispatch, pattern=r"^adw:")
    )
