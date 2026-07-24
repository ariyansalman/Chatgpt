"""Admin Sales Forecast & Reports Handler — V40.

Callback namespace: asf:*

Sub-namespaces:
  asf:menu        — Forecast overview
  asf:reports     — Reports menu
  asf:report:<type>  — Generate and show a specific report
  asf:export:<type>:<fmt>  — Export report in CSV/JSON/XLSX/PDF
"""
from __future__ import annotations

import logging
import io
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.error import BadRequest

from services import sales_forecast as sf
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)


def _back_kb(cb: str = "asf:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _trend_icon(trend: str) -> str:
    return {"up": "📈", "down": "📉", "flat": "➡️"}.get(trend, "➡️")


# ─── Forecast Overview ────────────────────────────────────────────────────────

async def asf_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 Sales Forecast main view."""
    q = update.callback_query
    if q:
        await q.answer("⏳ Calculating…")

    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        fc = sf.get_sales_forecast()
    except Exception as e:
        logger.exception("asf_menu: get_sales_forecast failed")
        if q:
            await q.answer(f"❌ {e}", show_alert=True)
        return

    trend = fc.get("trend", "flat")
    icon  = _trend_icon(trend)
    g_pct = fc.get("expected_growth_pct")
    g_str = f"{g_pct:+.1f}%" if g_pct is not None else "N/A"
    conf  = fc.get("confidence_pct", 0)

    warn_line  = "\n⚠️ <b>LOW SALES WARNING</b> — Revenue trending below average." if fc.get("low_sales_warning") else ""
    trend_line = "\n🚀 <b>HIGH GROWTH TREND</b> — Revenue growing strongly!" if fc.get("high_sales_trend") else ""

    lines = [
        f"📈 <b>Sales Forecast</b>   {icon}\n",
        f"Expected Daily Revenue:   <b>${fc['expected_daily']:,.2f}</b>",
        f"Expected Weekly Revenue:  <b>${fc['expected_weekly']:,.2f}</b>",
        f"Expected Monthly Revenue: <b>${fc['expected_monthly']:,.2f}</b>",
        "",
        f"Expected Orders (30d): <b>{fc['expected_orders_30d']}</b>",
        f"Expected Growth:       <b>{g_str}</b>",
        f"Trend Direction:       <b>{trend.title()}</b> {icon}",
        f"Model Confidence:      <b>{conf:.0f}%</b>",
        warn_line,
        trend_line,
        "",
        "<i>Based on 30-day moving average (SMA-7).</i>",
    ]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📑 Reports",        callback_data="asf:reports"),
         InlineKeyboardButton("🔄 Refresh",        callback_data="asf:menu")],
        [InlineKeyboardButton("💰 Revenue Dash →", callback_data="abiz:dash"),
         InlineKeyboardButton("🔙 Back",           callback_data="abiz:menu")],
    ])
    text = "\n".join(l for l in lines if l is not None)
    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("asf_menu: %s", e)


# ─── Reports Menu ─────────────────────────────────────────────────────────────

async def asf_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports selection menu."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Daily Report",   callback_data="asf:report:daily"),
         InlineKeyboardButton("📅 Weekly Report",  callback_data="asf:report:weekly")],
        [InlineKeyboardButton("📅 Monthly Report", callback_data="asf:report:monthly"),
         InlineKeyboardButton("📅 Yearly Report",  callback_data="asf:report:yearly")],
        [InlineKeyboardButton("💵 Revenue Report", callback_data="asf:report:revenue"),
         InlineKeyboardButton("🛒 Orders Report",  callback_data="asf:report:orders")],
        [InlineKeyboardButton("👤 Customer Report",callback_data="asf:report:customer"),
         InlineKeyboardButton("👥 Referral Report",callback_data="asf:report:referral")],
        [InlineKeyboardButton("💳 Payment Report", callback_data="asf:report:payment")],
        [InlineKeyboardButton("🔙 Back",           callback_data="asf:menu")],
    ])
    try:
        await q.edit_message_text(
            "📑 <b>Reports</b>\n\nSelect a report to generate:",
            reply_markup=kb, parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("asf_reports: %s", e)


# ─── Single Report View ───────────────────────────────────────────────────────

async def asf_report_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and display a report summary with export options."""
    q = update.callback_query
    await q.answer("⏳ Generating report…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts       = (q.data or "").split(":")
    report_type = parts[2] if len(parts) >= 3 else "daily"

    try:
        data = sf.generate_report(report_type, admin_tg_id=update.effective_user.id)
    except Exception as e:
        logger.exception("asf_report_view failed")
        await q.answer(f"❌ {e}", show_alert=True)
        return

    # Store report data in context for export callbacks
    context.user_data["last_report_type"] = report_type
    context.user_data["last_report_data"]  = data

    lines = [
        f"📑 <b>{data.get('title', report_type.title() + ' Report')}</b>\n",
        f"Period: {data.get('period_start','')[:10]} → {data.get('period_end','')[:10]}",
        f"Revenue: <b>${data.get('revenue', 0):,.2f}</b>",
        f"Orders:  <b>{data.get('orders', 0)}</b>",
        f"Avg Order Value: <b>${data.get('avg_order_value', 0):,.2f}</b>",
        "",
    ]

    # Top products
    best = data.get("best_products", [])[:3]
    if best:
        lines.append("🏆 <b>Top Products:</b>")
        for p in best:
            lines.append(f"  • {p['name']} ({p['quantity_sold']} sold / ${p['revenue']:,.2f})")
        lines.append("")

    # Top customers
    top_c = data.get("top_customers", [])[:3]
    if top_c:
        lines.append("💸 <b>Top Customers:</b>")
        for u in top_c:
            lines.append(f"  • {u['username']} — ${u['total_spend']:,.2f}")
        lines.append("")

    # Payment methods
    pmts = data.get("payment_methods", [])[:3]
    if pmts:
        lines.append("💳 <b>Payment Methods:</b>")
        for p in pmts:
            lines.append(f"  • {p['method']}: {p['count']} ({p['pct']}%)")

    lines.append("\n<i>Use export buttons below to download the full report.</i>")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 CSV",  callback_data=f"asf:export:{report_type}:csv"),
         InlineKeyboardButton("📥 JSON", callback_data=f"asf:export:{report_type}:json"),
         InlineKeyboardButton("📥 XLSX", callback_data=f"asf:export:{report_type}:xlsx"),
         InlineKeyboardButton("📥 PDF",  callback_data=f"asf:export:{report_type}:pdf")],
        [InlineKeyboardButton("🔙 Reports", callback_data="asf:reports")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("asf_report_view: %s", e)


# ─── Export ───────────────────────────────────────────────────────────────────

async def asf_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export a report in the requested format and send as document."""
    q = update.callback_query
    await q.answer("⏳ Exporting…")
    if not has_permission(update.effective_user.id, "view_reports", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts       = (q.data or "").split(":")
    report_type = parts[2] if len(parts) >= 3 else "daily"
    fmt         = parts[3] if len(parts) >= 4 else "csv"

    # Retrieve or regenerate report data
    data = context.user_data.get("last_report_data")
    if not data or context.user_data.get("last_report_type") != report_type:
        try:
            data = sf.generate_report(report_type, admin_tg_id=update.effective_user.id)
        except Exception as e:
            await q.answer(f"❌ {e}", show_alert=True)
            return

    try:
        if fmt == "csv":
            content  = sf.export_csv(data)
            filename = f"report_{report_type}_{datetime.utcnow().strftime('%Y%m%d')}.csv"
            mime     = "text/csv"
        elif fmt == "json":
            content  = sf.export_json(data)
            filename = f"report_{report_type}_{datetime.utcnow().strftime('%Y%m%d')}.json"
            mime     = "application/json"
        elif fmt == "xlsx":
            content  = sf.export_excel(data)
            filename = f"report_{report_type}_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
            mime     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pdf":
            content  = sf.export_pdf(data)
            filename = f"report_{report_type}_{datetime.utcnow().strftime('%Y%m%d')}.pdf"
            mime     = "application/pdf"
        else:
            await q.answer("❌ Unknown format.", show_alert=True)
            return

        buf = io.BytesIO(content)
        buf.name = filename
        await update.effective_chat.send_document(
            document=InputFile(buf, filename=filename),
            caption=f"📑 {data.get('title', report_type.title() + ' Report')}",
        )
    except Exception as e:
        logger.exception("asf_export failed for fmt=%s", fmt)
        await q.answer(f"❌ Export failed: {e}", show_alert=True)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def asf_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all asf:* callbacks."""
    q = update.callback_query
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await asf_menu(update, context)
    elif action == "reports":
        await asf_reports(update, context)
    elif action == "report":
        await asf_report_view(update, context)
    elif action == "export":
        await asf_export(update, context)
    else:
        await q.answer()
        await asf_menu(update, context)


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(asf_dispatch, pattern=r"^asf:.+$"))
