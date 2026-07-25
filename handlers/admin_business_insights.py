"""Admin Business Insights Handler — V40.

Callback namespace: abiz:*

Sub-namespaces:
  abiz:dash      — Revenue dashboard
  abiz:insights  — Business insights (best sellers, top customers, etc.)
  abiz:products  — Product insights (stock, trending, etc.)
  abiz:settings  — Business analytics settings
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.error import BadRequest

from services import sales_forecast as sf
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)


def _back_kb(cb: str = "abiz:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _check_enabled() -> bool:
    return cfg.get("biz_analytics_status", "enabled") == "enabled"


def _fmt(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount/1_000:.2f}K"
    return f"${amount:,.2f}"


# ─── Main menu ────────────────────────────────────────────────────────────────

async def abiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📈 Business Insights root menu."""
    q = update.callback_query
    if q:
        await q.answer()

    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    status_val = cfg.get("biz_analytics_status", "enabled")
    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status_val, "⚪")

    text = (
        f"📊 <b>Business Analytics Center</b>\n\n"
        f"Status: {status_icon} <b>{status_val.title()}</b>\n\n"
        f"Select a section:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Revenue Dashboard",   callback_data="abiz:dash"),
         InlineKeyboardButton("📈 Sales Forecast",      callback_data="asf:menu")],
        [InlineKeyboardButton("🏆 Business Insights",   callback_data="abiz:insights"),
         InlineKeyboardButton("📦 Product Insights",    callback_data="abiz:products")],
        [InlineKeyboardButton("📑 Reports",             callback_data="asf:reports"),
         InlineKeyboardButton("⚙️ Settings",            callback_data="abiz:settings")],
        [InlineKeyboardButton("🔙 Back",               callback_data="acc:root")],
    ])
    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_menu: %s", e)


# ─── Revenue Dashboard ────────────────────────────────────────────────────────

async def abiz_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the revenue dashboard."""
    q = update.callback_query
    await q.answer("⏳ Loading…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        rev = sf.get_revenue_summary()
    except Exception as e:
        logger.exception("abiz_dashboard: get_revenue_summary failed")
        await q.answer("❌ Failed to load data.", show_alert=True)
        return

    trend_today = "📈" if rev["today"] >= rev["yesterday"] else "📉"
    lines = [
        "💰 <b>Revenue Dashboard</b>\n",
        f"📅 Today:      <b>{_fmt(rev['today'])}</b>   {trend_today}",
        f"📅 Yesterday:  <b>{_fmt(rev['yesterday'])}</b>",
        f"📅 Weekly:     <b>{_fmt(rev['weekly'])}</b>",
        f"📅 Monthly:    <b>{_fmt(rev['monthly'])}</b>",
        f"📅 Yearly:     <b>{_fmt(rev['yearly'])}</b>",
        f"💵 Total:      <b>{_fmt(rev['total'])}</b>",
        "",
        f"📊 Gross Profit:  <b>{_fmt(rev['gross_profit'])}</b>",
        f"📊 Net Profit:    <b>{_fmt(rev['net_profit'])}</b>",
        "",
        f"🛒 Avg Order Value:    <b>{_fmt(rev['avg_order_value'])}</b>",
        f"👤 Avg Customer Value: <b>{_fmt(rev['avg_customer_value'])}</b>",
        "",
        f"📦 Today's Orders:  <b>{rev['today_orders']}</b>",
        f"📦 Total Orders:    <b>{rev['total_orders']}</b>",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",        callback_data="abiz:dash"),
         InlineKeyboardButton("📈 Forecast →",     callback_data="asf:menu")],
        [InlineKeyboardButton("🔙 Back",           callback_data="abiz:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_dashboard: %s", e)


# ─── Business Insights ────────────────────────────────────────────────────────

async def abiz_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display business insights menu."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Best Sellers",       callback_data="abiz:ins:bestsell"),
         InlineKeyboardButton("📉 Worst Sellers",      callback_data="abiz:ins:worstsell")],
        [InlineKeyboardButton("🔥 Most Active Users",  callback_data="abiz:ins:active"),
         InlineKeyboardButton("💸 Top Spenders",       callback_data="abiz:ins:topspend")],
        [InlineKeyboardButton("👥 Top Referrers",      callback_data="abiz:ins:referrers"),
         InlineKeyboardButton("💳 Payment Methods",    callback_data="abiz:ins:payments")],
        [InlineKeyboardButton("🗂 Top Categories",     callback_data="abiz:ins:categories"),
         InlineKeyboardButton("🚀 Fastest Growing",    callback_data="abiz:ins:fastest")],
        [InlineKeyboardButton("🔙 Back",               callback_data="abiz:menu")],
    ])
    try:
        await q.edit_message_text(
            "🏆 <b>Business Insights</b>\n\nSelect a view:",
            reply_markup=kb, parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_insights: %s", e)


async def abiz_insights_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render a specific insights view."""
    q = update.callback_query
    await q.answer("⏳ Loading…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts  = (q.data or "").split(":")
    view   = parts[2] if len(parts) >= 3 else ""

    try:
        lines = _render_insights_view(view)
    except Exception as e:
        logger.exception("abiz_insights_view %s", view)
        lines = [f"❌ Failed to load: {e}"]

    kb = _back_kb("abiz:insights")
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_insights_view: %s", e)


def _render_insights_view(view: str) -> list:
    if view == "bestsell":
        rows = sf.get_best_selling_products(8)
        lines = ["🏆 <b>Best Selling Products</b>\n"]
        for i, p in enumerate(rows, 1):
            lines.append(f"{i}. <b>{p['name']}</b> — {p['quantity_sold']} sold / {_fmt(p['revenue'])}")
        return lines or ["<i>No data available.</i>"]

    if view == "worstsell":
        rows = sf.get_worst_selling_products(8)
        lines = ["📉 <b>Worst / Slow Selling Products</b>\n"]
        for i, p in enumerate(rows, 1):
            lines.append(f"{i}. <b>{p['name']}</b> — {p['quantity_sold']} sold / {_fmt(p['revenue'])}")
        return lines or ["<i>No data available.</i>"]

    if view == "active":
        rows = sf.get_most_active_customers(8)
        lines = ["🔥 <b>Most Active Customers</b>\n"]
        for i, u in enumerate(rows, 1):
            lines.append(f"{i}. <b>{u['username']}</b> — {u['order_count']} orders / {_fmt(u['total_spend'])}")
        return lines or ["<i>No data available.</i>"]

    if view == "topspend":
        rows = sf.get_top_spending_users(8)
        lines = ["💸 <b>Top Spending Users</b>\n"]
        for i, u in enumerate(rows, 1):
            lines.append(f"{i}. <b>{u['username']}</b> — {_fmt(u['total_spend'])} / {u['order_count']} orders")
        return lines or ["<i>No data available.</i>"]

    if view == "referrers":
        rows = sf.get_top_referral_users(8)
        lines = ["👥 <b>Top Referral Users</b>\n"]
        for i, u in enumerate(rows, 1):
            lines.append(f"{i}. <b>{u['username']}</b> — {u['referral_count']} refs / {_fmt(u['total_earned'])} earned")
        return lines or ["<i>No data available.</i>"]

    if view == "payments":
        rows = sf.get_payment_method_stats()
        lines = ["💳 <b>Payment Methods</b>\n"]
        for p in rows:
            lines.append(f"• <b>{p['method']}</b>: {p['count']} orders ({p['pct']}%) — {_fmt(p['revenue'])}")
        return lines or ["<i>No data available.</i>"]

    if view == "categories":
        rows = sf.get_category_stats()
        lines = ["🗂 <b>Most Popular Categories</b>\n"]
        for i, c in enumerate(rows, 1):
            lines.append(f"{i}. <b>{c['name']}</b> — {c['order_items']} items / {_fmt(c['revenue'])}")
        return lines or ["<i>No data available.</i>"]

    if view == "fastest":
        p = sf.get_fastest_growing_product()
        if not p:
            return ["🚀 <b>Fastest Growing Product</b>\n\n<i>No data available.</i>"]
        return [
            "🚀 <b>Fastest Growing Product</b>\n",
            f"<b>{p['name']}</b>",
            f"Growth: <b>{p['growth_pct']:+.1f}%</b>",
            f"Units this week: <b>{p['units_this_period']}</b>",
        ]

    return [f"❓ Unknown view: {view}"]


# ─── Product Insights ─────────────────────────────────────────────────────────

async def abiz_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Product insights overview."""
    q = update.callback_query
    await q.answer("⏳ Loading…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        insights = sf.get_product_insights()
    except Exception as e:
        logger.exception("abiz_products failed")
        await q.answer(f"❌ {e}", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"❌ Out of Stock ({len(insights['out_of_stock'])})",
                               callback_data="abiz:prod:oos"),
         InlineKeyboardButton(f"⚠️ Low Stock ({len(insights['low_stock'])})",
                               callback_data="abiz:prod:low")],
        [InlineKeyboardButton(f"🐢 Slow Selling ({len(insights['slow_selling'])})",
                               callback_data="abiz:prod:slow"),
         InlineKeyboardButton(f"⚡ Fast Selling ({len(insights['fast_selling'])})",
                               callback_data="abiz:prod:fast")],
        [InlineKeyboardButton(f"🔥 Trending ({len(insights['trending'])})",
                               callback_data="abiz:prod:trend"),
         InlineKeyboardButton(f"🕳 No Sales ({len(insights['no_sales'])})",
                               callback_data="abiz:prod:nosale")],
        [InlineKeyboardButton("🔙 Back", callback_data="abiz:menu")],
    ])
    text = (
        "📦 <b>Product Insights</b>\n\n"
        f"❌ Out of stock:  <b>{len(insights['out_of_stock'])}</b>\n"
        f"⚠️ Low stock:     <b>{len(insights['low_stock'])}</b>\n"
        f"🐢 Slow selling:  <b>{len(insights['slow_selling'])}</b>\n"
        f"⚡ Fast selling:  <b>{len(insights['fast_selling'])}</b>\n"
        f"🔥 Trending:      <b>{len(insights['trending'])}</b>\n"
        f"🕳 No sales ever: <b>{len(insights['no_sales'])}</b>"
    )
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_products: %s", e)


async def abiz_products_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a specific product insight category."""
    q = update.callback_query
    await q.answer("⏳ Loading…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = (q.data or "").split(":")
    view  = parts[2] if len(parts) >= 3 else ""
    labels = {
        "oos":    ("❌ Out of Stock",    "out_of_stock",  "stock"),
        "low":    ("⚠️ Low Stock",       "low_stock",     "stock"),
        "slow":   ("🐢 Slow Selling",    "slow_selling",  "week_sales"),
        "fast":   ("⚡ Fast Selling",    "fast_selling",  "week_sales"),
        "trend":  ("🔥 Trending",        "trending",      "week_sales"),
        "nosale": ("🕳 No Sales Ever",   "no_sales",      None),
    }
    label, key, metric = labels.get(view, ("?", "out_of_stock", None))

    try:
        insights = sf.get_product_insights()
        rows = insights.get(key, [])
    except Exception as e:
        logger.exception("abiz_products_view failed")
        rows = []

    lines = [f"{label}\n"]
    if not rows:
        lines.append("<i>None found.</i>")
    else:
        for p in rows:
            extra = f" ({metric}: {p.get(metric, 0)})" if metric else ""
            lines.append(f"• <b>{p['name']}</b>{extra}")

    kb = _back_kb("abiz:products")
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_products_view: %s", e)


# ─── Settings ─────────────────────────────────────────────────────────────────

async def abiz_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Business analytics settings."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    status = cfg.get("biz_analytics_status", "enabled")
    daily  = cfg.get_bool("biz_auto_daily_report", False)
    weekly = cfg.get_bool("biz_auto_weekly_report", False)
    monthly= cfg.get_bool("biz_auto_monthly_report", False)
    period = cfg.get_int("biz_forecast_period_days", 30)
    retain = cfg.get_int("biz_report_retention_days", 90)

    on = "✅"
    off = "☐"

    text = (
        f"⚙️ <b>Business Analytics Settings</b>\n\n"
        f"Status: <b>{status.title()}</b>\n"
        f"Forecast Period: <b>{period} days</b>\n"
        f"Report Retention: <b>{retain} days</b>\n\n"
        f"Auto Reports:\n"
        f"  {on if daily else off} Daily Summary\n"
        f"  {on if weekly else off} Weekly Summary\n"
        f"  {on if monthly else off} Monthly Summary"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Enable",      callback_data="abiz:set:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="abiz:set:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="abiz:set:status:disabled")],
        [InlineKeyboardButton(f"{'✅' if daily else '☐'} Daily Auto-Report",
                               callback_data="abiz:set:daily"),
         InlineKeyboardButton(f"{'✅' if weekly else '☐'} Weekly Auto-Report",
                               callback_data="abiz:set:weekly")],
        [InlineKeyboardButton(f"{'✅' if monthly else '☐'} Monthly Auto-Report",
                               callback_data="abiz:set:monthly")],
        [InlineKeyboardButton("🔙 Back", callback_data="abiz:menu")],
    ])
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("abiz_settings: %s", e)


async def abiz_settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings toggle/set actions."""
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts  = (q.data or "").split(":")
    action = parts[2] if len(parts) >= 3 else ""
    val    = parts[3] if len(parts) >= 4 else ""

    try:
        from database import get_db_session
        from database.models import BotConfig
        with get_db_session() as s:
            if action == "status" and val:
                row = s.query(BotConfig).filter_by(key="biz_analytics_status").first()
                if row:
                    row.value = val
                s.commit()
                cfg._invalidate() if hasattr(cfg, "_invalidate") else None
                await q.answer(f"✅ Status set to {val}.")
            elif action in ("daily", "weekly", "monthly"):
                key = f"biz_auto_{action}_report"
                row = s.query(BotConfig).filter_by(key=key).first()
                if row:
                    row.value = "false" if row.value in ("true", "1", True) else "true"
                    s.commit()
                    await q.answer(f"✅ Toggled {action} report.")
            else:
                await q.answer("❓ Unknown setting.")
        log_admin_action(update.effective_user.id, f"biz_settings.{action}",
                         details=val)
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return

    await abiz_settings(update, context)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def abiz_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all abiz:* callbacks."""
    q = update.callback_query
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await abiz_menu(update, context)
    elif action == "dash":
        await abiz_dashboard(update, context)
    elif action == "insights":
        await abiz_insights(update, context)
    elif action == "ins":
        await abiz_insights_view(update, context)
    elif action == "products":
        await abiz_products(update, context)
    elif action == "prod":
        await abiz_products_view(update, context)
    elif action == "settings":
        await abiz_settings(update, context)
    elif action == "set":
        await abiz_settings_action(update, context)
    else:
        await q.answer()
        await abiz_menu(update, context)


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(abiz_dispatch, pattern=r"^abiz:.+$"))
