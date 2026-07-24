"""Enterprise Broadcast Analytics — Admin Handler.

Callback namespace: ``bca:*``
Conversation key:   ``_bca`` in context.user_data

Provides:
  • Analytics Dashboard     (bca:menu)
  • Per-Broadcast Analytics (bca:analytics:<id>)
  • Broadcast History       (bca:history[:<page>[:<filter>]])
  • History Search          (bca:history:search  — conversation)
  • Broadcast Reports       (bca:reports:<id>)
  • Report View             (bca:report:<type>:<id>)
  • Export                  (bca:export:<id>:<format>)
  • Period Export           (bca:export_period:<period>:<format>)
  • Error Management        (bca:errors:<id>)
  • Retry Manager           (bca:retry_menu:<id>)
  • Archive / Delete        (bca:archive:<id>  bca:del:<id>)
  • Settings                (bca:settings)
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import get_db_session
from database.models import ScheduledBroadcast
from utils.bot_config import cfg
from utils.audit import log_admin_action
from utils.permissions import has_permission
from config.settings import settings
from utils.update_proxy import with_data

from services.broadcast_analytics_service import (
    get_analytics_dashboard,
    get_broadcast_analytics,
    search_broadcast_history,
    get_error_breakdown,
    get_error_detail,
    generate_delivery_report,
    generate_failure_report,
    generate_blocked_report,
    generate_skipped_report,
    generate_success_report,
    generate_retry_report,
    generate_period_report,
    export_csv,
    export_excel,
    export_json,
    export_pdf,
    retry_failed_deliveries,
    clear_retry_queue,
    archive_broadcast,
    delete_broadcast_history,
    log_export,
)

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
BCA_SEARCH       = 0   # receiving free-text search query
BCA_SETTINGS_NUM = 1   # receiving numeric settings value (retention / max history)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return has_permission(uid, "manage_broadcasts")


async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML") -> None:
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            try:
                await query.message.reply_text(text, parse_mode=parse_mode, reply_markup=kb)
            except Exception:
                pass


def _back_kb(cb: str = "bca:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _status_ok() -> bool:
    return cfg.get("broadcast_analytics_status", "enabled") == "enabled"


def _fmt_s(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m < 60 else f"{m // 60}h {m % 60}m"


STATUS_ICONS = {
    "sending":   "📤",
    "scheduled": "⏰",
    "draft":     "📝",
    "sent":      "✅",
    "paused":    "⏸",
    "cancelled": "❌",
    "failed":    "🔴",
}

REPORT_TYPES = {
    "delivery": "📊 Delivery Report",
    "failure":  "🔴 Failure Report",
    "blocked":  "🚫 Blocked Users Report",
    "skipped":  "⏭ Skipped Users Report",
    "success":  "✅ Success Report",
    "retry":    "🔄 Retry Report",
}

EXPORT_FORMATS = {
    "csv":   "📄 CSV",
    "excel": "📊 Excel",
    "json":  "📋 JSON",
    "pdf":   "📑 PDF",
}

PERIOD_LABELS = {
    "daily":   "📅 Today",
    "weekly":  "📅 This Week",
    "monthly": "📅 This Month",
}


# ── Guard ─────────────────────────────────────────────────────────────────────

def _guard(query, uid: int) -> bool:
    """Return True if access is denied (caller should return early)."""
    if not _is_admin(uid):
        return True
    if not _status_ok():
        return True
    return False


# ── Main dashboard ────────────────────────────────────────────────────────────

async def bca_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analytics Dashboard hub (bca:menu)."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _is_admin(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    status = cfg.get("broadcast_analytics_status", "enabled")
    if status == "disabled":
        await _safe_edit(query,
            "📊 <b>Broadcast Analytics</b>\n\n🔴 Feature is currently disabled.",
            _back_kb("acc:sec:broadcast"))
        return

    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")

    import asyncio
    try:
        stats = await asyncio.to_thread(get_analytics_dashboard)
    except Exception:
        logger.exception("bca_menu: dashboard error")
        stats = {}

    sr   = stats.get("success_rate", 0.0)
    fr   = stats.get("failure_rate", 0.0)
    spd  = stats.get("avg_speed_mps")
    spd_str = f"{spd:.1f} msg/s" if spd else "—"
    avg_ms  = stats.get("avg_delivery_ms")
    avg_str = f"{avg_ms:.0f} ms" if avg_ms else "—"

    text = (
        f"📊 <b>Broadcast Analytics</b>  {status_icon}\n\n"
        f"<b>Today's Broadcasts:</b>   {stats.get('today', 0)}\n"
        f"<b>Running:</b>             {stats.get('running', 0)}  "
        f"<b>Scheduled:</b>           {stats.get('scheduled', 0)}\n"
        f"<b>Completed:</b>           {stats.get('completed', 0)}  "
        f"<b>Failed:</b>              {stats.get('failed', 0)}\n"
        f"<b>Paused:</b>              {stats.get('paused', 0)}  "
        f"<b>Cancelled:</b>           {stats.get('cancelled', 0)}\n\n"
        f"<b>Total Sent:</b>          {stats.get('total_sent', 0):,}\n"
        f"<b>Total Delivered:</b>     {stats.get('total_delivered', 0):,}\n"
        f"<b>Success Rate:</b>        {sr:.1f}%  "
        f"<b>Failure Rate:</b>        {fr:.1f}%\n"
        f"<b>Delivery Speed:</b>      {spd_str}\n"
        f"<b>Avg Delivery Time:</b>   {avg_str}\n"
        f"<b>Retry Queue:</b>         {stats.get('retry_pending', 0)}\n"
        f"<b>Blocked Users:</b>       {stats.get('blocked_total', 0)}\n"
    )
    if status == "maintenance":
        text += "\n⚠️ <i>Maintenance mode active.</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Analytics by Broadcast", callback_data="bca:history:filter:all")],
        [
            InlineKeyboardButton("📜 History",       callback_data="bca:history"),
            InlineKeyboardButton("🔍 Search",        callback_data="bca:history:search"),
        ],
        [
            InlineKeyboardButton("📈 Period Reports", callback_data="bca:period_reports"),
            InlineKeyboardButton("📤 Export",         callback_data="bca:export_hub"),
        ],
        [InlineKeyboardButton("⚠️ Failed Deliveries", callback_data="bca:history:filter:failed")],
        [
            InlineKeyboardButton("⚙️ Settings",       callback_data="bca:settings"),
            InlineKeyboardButton("🔙 Back",           callback_data="acc:sec:broadcast"),
        ],
    ])
    await _safe_edit(query, text, kb)


# ── Per-broadcast analytics ───────────────────────────────────────────────────

async def bca_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Real-time analytics for one broadcast (bca:analytics:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    try:
        a = await asyncio.to_thread(get_broadcast_analytics, bid)
    except Exception:
        logger.exception("bca_analytics: error bid=%d", bid)
        await query.answer("❌ Failed to load analytics.", show_alert=True)
        return

    if not a:
        await _safe_edit(query, f"❌ Broadcast #{bid} not found.", _back_kb("bca:history"))
        return

    st_icon = STATUS_ICONS.get(a.get("status", ""), "•")
    eta_str = _fmt_s(a.get("eta_s"))
    ela_str = _fmt_s(a.get("elapsed_s"))
    spd     = a.get("avg_speed_mps")
    spd_str = f"{spd:.1f} msg/s" if spd else "—"
    btype   = a.get("broadcast_type") or a.get("media_type", "—")

    text = (
        f"📊 <b>Analytics — {a.get('title')} (#{bid})</b>\n\n"
        f"<b>Type:</b>      {btype}  "
        f"<b>Status:</b>    {st_icon} {a.get('status')}\n"
        f"<b>Segment:</b>   {a.get('target_segment', '—')}\n"
        f"<b>Created By:</b> {a.get('created_by', '—')}\n"
        f"<b>Created:</b>   {(a.get('created_at') or '—')[:19]}\n"
        f"<b>Started:</b>   {(a.get('started_at') or '—')[:19]}\n"
        f"<b>Finished:</b>  {(a.get('finished_at') or '—')[:19]}\n"
        f"<b>Elapsed:</b>   {ela_str}  "
        f"<b>ETA:</b>       {eta_str}\n\n"
        f"<b>👥 Total Users:</b>    {a.get('total_recipients', 0):,}\n"
        f"<b>📤 Sent:</b>           {a.get('sent_count', 0):,}\n"
        f"<b>✅ Delivered:</b>      {a.get('delivered_count', 0):,}  "
        f"({a.get('success_rate', 0):.1f}%)\n"
        f"<b>❌ Failed:</b>         {a.get('failed_count', 0):,}  "
        f"({a.get('failure_rate', 0):.1f}%)\n"
        f"<b>🚫 Blocked:</b>        {a.get('blocked_count', 0):,}\n"
        f"<b>⏭ Skipped:</b>        {a.get('skipped_count', 0):,}\n"
        f"<b>⏳ Remaining:</b>      {a.get('remaining', 0):,}\n\n"
        f"<b>⚡ Speed:</b>          {spd_str}\n"
        f"<b>🔄 Retry Pending:</b>  {a.get('retry_pending', 0)}  "
        f"<b>Retry Done:</b>        {a.get('retry_sent', 0)}\n"
        f"<b>Runs:</b>              {a.get('run_count', 0)}\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Reports",      callback_data=f"bca:reports:{bid}"),
            InlineKeyboardButton("⚠️ Errors",       callback_data=f"bca:errors:{bid}"),
        ],
        [
            InlineKeyboardButton("🔄 Retry Mgr",   callback_data=f"bca:retry_menu:{bid}"),
            InlineKeyboardButton("📤 Export",       callback_data=f"bca:export_menu:{bid}"),
        ],
        [
            InlineKeyboardButton("📋 View Broadcast", callback_data=f"asb:view:{bid}"),
            InlineKeyboardButton("📋 Logs",           callback_data=f"asb:logs:{bid}"),
        ],
        [InlineKeyboardButton("🔙 History", callback_data="bca:history")],
    ])
    await _safe_edit(query, text, kb)


# ── History list ───────────────────────────────────────────────────────────────

async def bca_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast history browser (bca:history[:<page>[:<filter>]])."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    parts         = query.data.split(":")
    filter_status = "all"
    page          = 0

    # bca:history  /  bca:history:filter:<status>  /  bca:history:page:<n>:<filter>
    if len(parts) >= 4 and parts[2] == "filter":
        filter_status = parts[3]
    elif len(parts) >= 4 and parts[2] == "page":
        page = int(parts[3]) if parts[3].isdigit() else 0
        filter_status = parts[4] if len(parts) > 4 else "all"

    # Check for stored search
    search_q = context.user_data.get("_bca_search")

    import asyncio
    records, total = await asyncio.to_thread(
        search_broadcast_history,
        search_q, filter_status, None, page, 8,
    )

    pages = max(1, (total + 7) // 8)
    filter_label = filter_status.capitalize() if filter_status != "all" else "All"
    title = (f"📜 <b>Broadcast History</b> — {filter_label}"
             + (f' 🔍 "<i>{search_q}</i>"' if search_q else "")
             + f"\nPage {page+1}/{pages}  ({total} records)\n")

    kb = []
    for r in records:
        st_icon = STATUS_ICONS.get(r["status"], "•")
        date_s  = (r["created_at"] or "")[:10]
        kb.append([InlineKeyboardButton(
            f"{st_icon} #{r['id']} {r['title'][:28]} ({date_s})",
            callback_data=f"bca:analytics:{r['id']}")])

    # Navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"bca:history:page:{page-1}:{filter_status}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"bca:history:page:{page+1}:{filter_status}"))
    if nav:
        kb.append(nav)

    # Filter bar
    filters_row = []
    for st in ("all", "sent", "sending", "failed", "scheduled", "paused", "cancelled"):
        icon = STATUS_ICONS.get(st, "📋") if st != "all" else "📋"
        filters_row.append(InlineKeyboardButton(
            f"{icon} {st.title()}", callback_data=f"bca:history:filter:{st}"))
    # Break into rows of 3
    for i in range(0, len(filters_row), 3):
        kb.append(filters_row[i:i+3])

    kb.append([
        InlineKeyboardButton("🔍 Search",         callback_data="bca:history:search"),
        InlineKeyboardButton("🗑 Clear Search",    callback_data="bca:history:clear_search"),
    ])
    kb.append([InlineKeyboardButton("🔙 Dashboard", callback_data="bca:menu")])
    await _safe_edit(query, title, InlineKeyboardMarkup(kb))


async def bca_history_clear_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear active search filter (bca:history:clear_search)."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("_bca_search", None)
    return await bca_history(with_data(update, "bca:history"), context)


# ── History search conversation ────────────────────────────────────────────────

async def bca_history_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for search query (bca:history:search)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return ConversationHandler.END

    await _safe_edit(query,
        "🔍 <b>Search Broadcast History</b>\n\n"
        "Send a search term (broadcast title, ID, segment, or type).\n\n"
        "Or /cancel to go back.",
        _back_kb("bca:history"))
    return BCA_SEARCH


async def bca_history_search_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive search query and show results."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    query_text = (update.message.text or "").strip()
    if not query_text:
        await update.message.reply_text("❌ Empty search. Try again or /cancel.")
        return BCA_SEARCH

    context.user_data["_bca_search"] = query_text
    records, total = search_broadcast_history(query_text, "all", None, 0, 8)

    if not records:
        await update.message.reply_text(
            f"🔍 No results for <i>{query_text}</i>.",
            parse_mode="HTML",
            reply_markup=_back_kb("bca:history"))
        return ConversationHandler.END

    lines = [f"🔍 <b>Results for «{query_text}»</b> ({total} found)\n"]
    kb    = []
    for r in records:
        st_icon = STATUS_ICONS.get(r["status"], "•")
        date_s  = (r["created_at"] or "")[:10]
        lines.append(f"{st_icon} #{r['id']} <b>{r['title'][:30]}</b> ({date_s})")
        kb.append([InlineKeyboardButton(
            f"{st_icon} #{r['id']} {r['title'][:28]}",
            callback_data=f"bca:analytics:{r['id']}")])
    kb.append([InlineKeyboardButton("🔙 History", callback_data="bca:history")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# ── Reports hub ────────────────────────────────────────────────────────────────

async def bca_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show report type menu for a broadcast (bca:reports:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_analytics_enabled", True):
        await query.answer("📈 Reports disabled in settings.", show_alert=True)
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await _safe_edit(query, "❌ Not found.", _back_kb("bca:history"))
            return
        title = br.title

    kb = [[InlineKeyboardButton(label, callback_data=f"bca:report:{rtype}:{bid}")]
          for rtype, label in REPORT_TYPES.items()]
    kb.append([InlineKeyboardButton("📤 Export All", callback_data=f"bca:export_menu:{bid}")])
    kb.append([InlineKeyboardButton("🔙 Analytics", callback_data=f"bca:analytics:{bid}")])

    await _safe_edit(query,
        f"📈 <b>Reports — {title} (#{bid})</b>\n\n"
        "Select a report type to generate:",
        InlineKeyboardMarkup(kb))


async def bca_report_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and display a typed report (bca:report:<type>:<id>)."""
    query = update.callback_query
    await query.answer("Generating report…")
    if _guard(query, update.effective_user.id):
        return

    parts = query.data.split(":")
    try:
        rtype = parts[2]
        bid   = int(parts[3])
    except (IndexError, ValueError):
        return

    generators = {
        "delivery": generate_delivery_report,
        "failure":  generate_failure_report,
        "blocked":  generate_blocked_report,
        "skipped":  generate_skipped_report,
        "success":  generate_success_report,
        "retry":    generate_retry_report,
    }
    gen_fn = generators.get(rtype, generate_delivery_report)

    import asyncio
    try:
        rep = await asyncio.to_thread(gen_fn, bid)
    except Exception:
        logger.exception("bca_report_view: bid=%d type=%s", bid, rtype)
        await query.answer("❌ Report generation failed.", show_alert=True)
        return

    if not rep:
        await _safe_edit(query, "❌ No data found.", _back_kb(f"bca:reports:{bid}"))
        return

    title_label = REPORT_TYPES.get(rtype, rtype.title())

    # Build human-readable summary
    lines = [
        f"📈 <b>{title_label}</b>\n",
        f"<b>Broadcast:</b> {rep.get('title')} (#{bid})\n",
        f"<b>Status:</b>    {rep.get('status')}  "
        f"<b>Segment:</b>   {rep.get('target_segment', '—')}\n",
        f"\n<b>📊 Delivery Summary</b>",
        f"Total: {rep.get('total_recipients', 0):,}  Sent: {rep.get('sent_count', 0):,}",
        f"✅ Delivered: {rep.get('delivered_count', 0):,}  ({rep.get('success_rate', 0):.1f}%)",
        f"❌ Failed: {rep.get('failed_count', 0):,}  ({rep.get('failure_rate', 0):.1f}%)",
        f"🚫 Blocked: {rep.get('blocked_count', 0):,}  ⏭ Skipped: {rep.get('skipped_count', 0):,}",
    ]

    if "error_breakdown" in rep and rep["error_breakdown"]:
        lines.append("\n<b>⚠️ Error Breakdown</b>")
        for cat, cnt in rep["error_breakdown"].items():
            lines.append(f"  • {cat}: <b>{cnt}</b>")

    # Row previews (up to 5 rows)
    for row_key, row_label in [
        ("failure_rows", "❌ Sample Failures"),
        ("blocked_rows", "🚫 Sample Blocked"),
        ("retry_rows",   "🔄 Sample Retry Queue"),
    ]:
        rows = rep.get(row_key, [])
        if rows:
            lines.append(f"\n<b>{row_label}</b> (showing first 5)")
            for r in rows[:5]:
                err = (r.get("error_msg") or "")[:60]
                lines.append(f"  • <code>{r.get('telegram_id')}</code>  {err}")

    text = "\n".join(lines)[:4000]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Export CSV",   callback_data=f"bca:export:{bid}:csv:{rtype}"),
            InlineKeyboardButton("📊 Export Excel", callback_data=f"bca:export:{bid}:excel:{rtype}"),
        ],
        [
            InlineKeyboardButton("📋 Export JSON",  callback_data=f"bca:export:{bid}:json:{rtype}"),
            InlineKeyboardButton("📑 Export PDF",   callback_data=f"bca:export:{bid}:pdf:{rtype}"),
        ],
        [InlineKeyboardButton("🔙 Reports", callback_data=f"bca:reports:{bid}")],
    ])
    await _safe_edit(query, text, kb)


# ── Period reports ─────────────────────────────────────────────────────────────

async def bca_period_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Period report hub (bca:period_reports)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    kb = InlineKeyboardMarkup([
        *[[InlineKeyboardButton(label, callback_data=f"bca:period_view:{period}")]
          for period, label in PERIOD_LABELS.items()],
        [
            InlineKeyboardButton("📄 CSV",   callback_data="bca:period_export:daily:csv"),
            InlineKeyboardButton("📊 Excel", callback_data="bca:period_export:daily:excel"),
            InlineKeyboardButton("📋 JSON",  callback_data="bca:period_export:daily:json"),
            InlineKeyboardButton("📑 PDF",   callback_data="bca:period_export:daily:pdf"),
        ],
        [InlineKeyboardButton("🔙 Dashboard", callback_data="bca:menu")],
    ])
    await _safe_edit(query,
        "📅 <b>Period Reports</b>\n\n"
        "View and export aggregate summaries for today, this week, or this month.\n\n"
        "Select a period to view its report, then choose an export format:",
        kb)


async def bca_period_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display a period report (bca:period_view:<period>)."""
    query = update.callback_query
    await query.answer("Generating period report…")
    if _guard(query, update.effective_user.id):
        return

    parts  = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "daily"

    import asyncio
    try:
        rep = await asyncio.to_thread(generate_period_report, period)
    except Exception:
        logger.exception("bca_period_view: period=%s", period)
        await query.answer("❌ Period report failed.", show_alert=True)
        return

    label = rep.get("label", period.title())
    sc    = rep.get("status_counts", {})
    sc_str = "  ".join(f"{k}: {v}" for k, v in sc.items()) or "None"

    text = (
        f"📅 <b>{label}</b>\n\n"
        f"<b>From:</b> {(rep.get('from') or '')[:16]}  "
        f"<b>To:</b>   {(rep.get('to')   or '')[:16]}\n\n"
        f"<b>Broadcasts:</b>    {rep.get('broadcast_count', 0)}\n"
        f"<b>Status:</b>        {sc_str}\n\n"
        f"<b>Total Sent:</b>    {rep.get('total_sent', 0):,}\n"
        f"<b>Delivered:</b>     {rep.get('total_delivered', 0):,}  "
        f"({rep.get('success_rate', 0):.1f}%)\n"
        f"<b>Failed:</b>        {rep.get('total_failed', 0):,}  "
        f"({rep.get('failure_rate', 0):.1f}%)\n"
        f"<b>Blocked:</b>       {rep.get('total_blocked', 0):,}\n"
        f"<b>Skipped:</b>       {rep.get('total_skipped', 0):,}\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 CSV",     callback_data=f"bca:period_export:{period}:csv"),
            InlineKeyboardButton("📊 Excel",   callback_data=f"bca:period_export:{period}:excel"),
        ],
        [
            InlineKeyboardButton("📋 JSON",    callback_data=f"bca:period_export:{period}:json"),
            InlineKeyboardButton("📑 PDF",     callback_data=f"bca:period_export:{period}:pdf"),
        ],
        *[[InlineKeyboardButton(lbl, callback_data=f"bca:period_view:{p}")]
          for p, lbl in PERIOD_LABELS.items()],
        [InlineKeyboardButton("🔙 Period Reports", callback_data="bca:period_reports")],
    ])
    await _safe_edit(query, text, kb)


async def bca_period_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export a period report (bca:period_export:<period>:<format>)."""
    query = update.callback_query
    await query.answer("Generating export…")
    if _guard(query, update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_export_enabled", True):
        await query.answer("📤 Export is disabled.", show_alert=True)
        return

    parts  = query.data.split(":")
    try:
        period = parts[2]
        fmt    = parts[3]
    except IndexError:
        return

    import asyncio
    try:
        rep  = await asyncio.to_thread(generate_period_report, period)
        data = await asyncio.to_thread(_do_export, rep, fmt,
                                        f"period_{period}_report", period)
    except Exception:
        logger.exception("bca_period_export: period=%s fmt=%s", period, fmt)
        await query.message.reply_text("❌ Export failed.")
        return

    file_bytes, filename, mime = data
    log_export(None, fmt, "period", period, update.effective_user.id, len(file_bytes), filename)
    await _send_file(query.message, file_bytes, filename, mime,
                     f"📅 {PERIOD_LABELS.get(period, period)} — {fmt.upper()} Report")


# ── Export per-broadcast ────────────────────────────────────────────────────────

async def bca_export_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export format picker for a broadcast (bca:export_menu:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    kb_rows = []
    for rtype, rlabel in REPORT_TYPES.items():
        row = [InlineKeyboardButton(
            f"{fmt_label} — {rlabel}",
            callback_data=f"bca:export:{bid}:{fmt}:{rtype}")
               for fmt, fmt_label in [("csv", "📄"), ("excel", "📊"),
                                       ("json", "📋"), ("pdf", "📑")]]
        # 4 per row
        kb_rows.append(row)

    kb_rows.append([InlineKeyboardButton("🔙 Analytics", callback_data=f"bca:analytics:{bid}")])
    await _safe_edit(query,
        f"📤 <b>Export — Broadcast #{bid}</b>\n\n"
        "Each row is a report type. Tap a format icon to export it.",
        InlineKeyboardMarkup(kb_rows))


async def bca_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send export file (bca:export:<id>:<format>[:<report_type>])."""
    query = update.callback_query
    await query.answer("Generating export…")
    if _guard(query, update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_export_enabled", True):
        await query.answer("📤 Export is disabled in settings.", show_alert=True)
        return

    parts = query.data.split(":")
    try:
        bid   = int(parts[2])
        fmt   = parts[3]
        rtype = parts[4] if len(parts) > 4 else "delivery"
    except (IndexError, ValueError):
        return

    generators = {
        "delivery": generate_delivery_report,
        "failure":  generate_failure_report,
        "blocked":  generate_blocked_report,
        "skipped":  generate_skipped_report,
        "success":  generate_success_report,
        "retry":    generate_retry_report,
    }
    gen_fn = generators.get(rtype, generate_delivery_report)

    import asyncio
    try:
        rep  = await asyncio.to_thread(gen_fn, bid)
        data = await asyncio.to_thread(
            _do_export, rep, fmt, f"broadcast_{bid}_{rtype}_report", None)
    except Exception:
        logger.exception("bca_export: bid=%d fmt=%s type=%s", bid, fmt, rtype)
        await query.message.reply_text("❌ Export failed. Check logs.")
        return

    file_bytes, filename, mime = data
    log_export(bid, fmt, rtype, None, update.effective_user.id, len(file_bytes), filename)
    log_admin_action(update.effective_user.id, "bca.export",
                     "scheduled_broadcast", bid,
                     f"format={fmt} type={rtype}",
                     module="admin_broadcast_analytics")

    await _send_file(query.message, file_bytes, filename, mime,
                     f"📤 Broadcast #{bid} — {REPORT_TYPES.get(rtype, rtype)} — {fmt.upper()}")


def _do_export(data: dict, fmt: str, base_name: str,
               period: Optional[str]) -> tuple:
    """Synchronous export helper — returns (bytes, filename, mime_type)."""
    title_map = {k: v for k, v in REPORT_TYPES.items()}
    title = title_map.get(data.get("report_type", ""), "Broadcast Report")
    if fmt == "csv":
        content  = export_csv(data)
        filename = f"{base_name}.csv"
        mime     = "text/csv"
    elif fmt == "excel":
        content  = export_excel(data, title)
        filename = f"{base_name}.xlsx"
        mime     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif fmt == "json":
        content  = export_json(data)
        filename = f"{base_name}.json"
        mime     = "application/json"
    elif fmt == "pdf":
        content  = export_pdf(data, title)
        filename = f"{base_name}.pdf"
        mime     = "application/pdf"
    else:
        content  = export_csv(data)
        filename = f"{base_name}.csv"
        mime     = "text/csv"
    return content, filename, mime


async def _send_file(message, file_bytes: bytes, filename: str, mime: str, caption: str):
    """Send a document to the chat."""
    buf       = io.BytesIO(file_bytes)
    buf.name  = filename
    size_kb   = len(file_bytes) // 1024
    now_str   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    await message.reply_document(
        document=buf,
        filename=filename,
        caption=f"{caption}\n📁 {size_kb} KB  ⏱ {now_str}",
    )


# ── Export hub ─────────────────────────────────────────────────────────────────

async def bca_export_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick export hub from the dashboard (bca:export_hub)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Export Today (CSV)",    callback_data="bca:period_export:daily:csv"),
         InlineKeyboardButton("📅 Export Today (Excel)",  callback_data="bca:period_export:daily:excel")],
        [InlineKeyboardButton("📅 Export Week (CSV)",     callback_data="bca:period_export:weekly:csv"),
         InlineKeyboardButton("📅 Export Week (Excel)",   callback_data="bca:period_export:weekly:excel")],
        [InlineKeyboardButton("📅 Export Month (CSV)",    callback_data="bca:period_export:monthly:csv"),
         InlineKeyboardButton("📅 Export Month (Excel)",  callback_data="bca:period_export:monthly:excel")],
        [InlineKeyboardButton("📊 Export Month (PDF)",    callback_data="bca:period_export:monthly:pdf"),
         InlineKeyboardButton("📋 Export Month (JSON)",   callback_data="bca:period_export:monthly:json")],
        [InlineKeyboardButton("🔙 Dashboard",             callback_data="bca:menu")],
    ])
    await _safe_edit(query,
        "📤 <b>Export Reports</b>\n\n"
        "Quick export period summaries, or navigate to a specific broadcast for per-broadcast exports.",
        kb)


# ── Error management ──────────────────────────────────────────────────────────

async def bca_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Error breakdown for a broadcast (bca:errors:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    try:
        breakdown = await asyncio.to_thread(get_error_breakdown, bid)
    except Exception:
        logger.exception("bca_errors bid=%d", bid)
        breakdown = {}

    if not breakdown:
        text = (f"⚠️ <b>Error Management — Broadcast #{bid}</b>\n\n"
                f"✅ No errors found in the retry queue for this broadcast.")
    else:
        total_errors = sum(breakdown.values())
        lines = [f"⚠️ <b>Error Management — Broadcast #{bid}</b>\n",
                 f"Total error entries: <b>{total_errors}</b>\n"]
        for cat, cnt in sorted(breakdown.items(), key=lambda x: -x[1]):
            pct = cnt / total_errors * 100
            lines.append(f"  • {cat}: <b>{cnt}</b>  ({pct:.1f}%)")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry Manager",     callback_data=f"bca:retry_menu:{bid}")],
        [
            InlineKeyboardButton("📄 Export Failures", callback_data=f"bca:export:{bid}:csv:failure"),
            InlineKeyboardButton("📑 PDF",             callback_data=f"bca:export:{bid}:pdf:failure"),
        ],
        [InlineKeyboardButton("🔙 Analytics",          callback_data=f"bca:analytics:{bid}")],
    ])
    await _safe_edit(query, text, kb)


# ── Retry manager ─────────────────────────────────────────────────────────────

async def bca_retry_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry manager for a broadcast (bca:retry_menu:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_retry_manager_enabled", True):
        await query.answer("🔄 Retry Manager is disabled.", show_alert=True)
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        pending = s.query(BroadcastRetryQueue).filter_by(
            broadcast_id=bid, status="pending").count()
        failed  = s.query(BroadcastRetryQueue).filter_by(
            broadcast_id=bid, status="failed").count()
        done    = s.query(BroadcastRetryQueue).filter_by(
            broadcast_id=bid, status="sent").count()

    from database.models import BroadcastRetryQueue

    text = (
        f"🔄 <b>Retry Manager — Broadcast #{bid}</b>\n\n"
        f"<b>Pending:</b>  {pending}\n"
        f"<b>Failed:</b>   {failed}\n"
        f"<b>Sent:</b>     {done}\n\n"
        "Use the buttons below to manage retries:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry All Failed",  callback_data=f"bca:retry_all:{bid}")],
        [InlineKeyboardButton("🗑 Clear Pending",     callback_data=f"bca:retry_clear:{bid}")],
        [
            InlineKeyboardButton("📄 Export Retry Log", callback_data=f"bca:export:{bid}:csv:retry"),
        ],
        [InlineKeyboardButton("🔙 Analytics",          callback_data=f"bca:analytics:{bid}")],
    ])
    await _safe_edit(query, text, kb)


async def bca_retry_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-queue all failed retry entries (bca:retry_all:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    count = await asyncio.to_thread(retry_failed_deliveries, bid)
    log_admin_action(update.effective_user.id, "bca.retry_all",
                     "scheduled_broadcast", bid, f"re-queued={count}",
                     module="admin_broadcast_analytics")
    await query.answer(f"✅ {count} failed entries re-queued.", show_alert=True)
    return await bca_retry_menu(with_data(update, f"bca:retry_menu:{bid}"), context)


async def bca_retry_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear pending retry queue (bca:retry_clear:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    count = await asyncio.to_thread(clear_retry_queue, bid)
    log_admin_action(update.effective_user.id, "bca.retry_clear",
                     "scheduled_broadcast", bid, f"cleared={count}",
                     module="admin_broadcast_analytics")
    await query.answer(f"🗑 {count} pending entries cleared.", show_alert=True)
    return await bca_retry_menu(with_data(update, f"bca:retry_menu:{bid}"), context)


# ── Archive / Delete ──────────────────────────────────────────────────────────

async def bca_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Archive a broadcast (bca:archive:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    ok = await asyncio.to_thread(archive_broadcast, bid)
    log_admin_action(update.effective_user.id, "bca.archive",
                     "scheduled_broadcast", bid, "",
                     module="admin_broadcast_analytics")
    if ok:
        await query.answer("🗂 Broadcast archived.", show_alert=True)
    else:
        await query.answer("❌ Not found.", show_alert=True)
    return await bca_history(with_data(update, "bca:history"), context)


async def bca_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm before deleting a broadcast from history (bca:del_ask:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        title = br.title if br else f"#{bid}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"bca:del_ok:{bid}"),
        InlineKeyboardButton("❌ Cancel",       callback_data=f"bca:analytics:{bid}"),
    ]])
    await _safe_edit(query,
        f"🗑 <b>Delete Broadcast #{bid}: {title}</b>\n\n"
        "⚠️ This permanently deletes the broadcast record and all its logs.\n"
        "This action cannot be undone.\n\nAre you sure?",
        kb)


async def bca_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute broadcast history deletion (bca:del_ok:<id>)."""
    query = update.callback_query
    await query.answer()
    if _guard(query, update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    import asyncio
    ok = await asyncio.to_thread(delete_broadcast_history, bid)
    log_admin_action(update.effective_user.id, "bca.delete",
                     "scheduled_broadcast", bid, "",
                     module="admin_broadcast_analytics")
    if ok:
        await query.answer(f"🗑 Broadcast #{bid} deleted.", show_alert=True)
    else:
        await query.answer("❌ Not found or already deleted.", show_alert=True)
    return await bca_history(with_data(update, "bca:history"), context)


# ── Settings ───────────────────────────────────────────────────────────────────

async def bca_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analytics settings page (bca:settings)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    status     = cfg.get("broadcast_analytics_status", "enabled")
    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")

    def tf(v: bool) -> str:
        return "✅ ON" if v else "❌ OFF"

    analytics_on = cfg.get_bool("broadcast_analytics_enabled", True)
    reports_on   = cfg.get_bool("broadcast_reports_enabled",   True)
    export_on    = cfg.get_bool("broadcast_export_enabled",    True)
    retry_on     = cfg.get_bool("broadcast_retry_manager_enabled", True)
    retention    = cfg.get_int("broadcast_log_retention_days", 90)
    max_hist     = cfg.get_int("broadcast_max_history", 500)

    text = (
        f"⚙️ <b>Broadcast Analytics Settings</b>\n\n"
        f"<b>Feature Status:</b> {status_icon} {status.capitalize()}\n\n"
        f"<b>Analytics:</b>      {tf(analytics_on)}\n"
        f"<b>Reports:</b>        {tf(reports_on)}\n"
        f"<b>Export:</b>         {tf(export_on)}\n"
        f"<b>Retry Manager:</b>  {tf(retry_on)}\n\n"
        f"<b>Log Retention:</b>  {retention} days (0 = forever)\n"
        f"<b>Max History:</b>    {max_hist} records\n"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="bca:settings:status:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="bca:settings:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="bca:settings:status:disabled"),
        ],
        [
            InlineKeyboardButton(f"Analytics: {'ON ✅' if analytics_on else 'OFF ❌'}",
                                  callback_data="bca:settings:toggle:broadcast_analytics_enabled"),
            InlineKeyboardButton(f"Reports: {'ON ✅' if reports_on else 'OFF ❌'}",
                                  callback_data="bca:settings:toggle:broadcast_reports_enabled"),
        ],
        [
            InlineKeyboardButton(f"Export: {'ON ✅' if export_on else 'OFF ❌'}",
                                  callback_data="bca:settings:toggle:broadcast_export_enabled"),
            InlineKeyboardButton(f"Retry Mgr: {'ON ✅' if retry_on else 'OFF ❌'}",
                                  callback_data="bca:settings:toggle:broadcast_retry_manager_enabled"),
        ],
        [
            InlineKeyboardButton(f"Retention: {retention}d [−7]",
                                  callback_data="bca:settings:adj:broadcast_log_retention_days:-7"),
            InlineKeyboardButton(f"[+7]",
                                  callback_data="bca:settings:adj:broadcast_log_retention_days:7"),
            InlineKeyboardButton(f"Max Hist: {max_hist} [−50]",
                                  callback_data="bca:settings:adj:broadcast_max_history:-50"),
            InlineKeyboardButton(f"[+50]",
                                  callback_data="bca:settings:adj:broadcast_max_history:50"),
        ],
        [InlineKeyboardButton("🔙 Dashboard", callback_data="bca:menu")],
    ])
    await _safe_edit(query, text, kb)


async def bca_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set feature status (bca:settings:status:<val>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    status = query.data.split(":")[-1]
    if status in ("enabled", "maintenance", "disabled"):
        cfg.set("broadcast_analytics_status", status)
        log_admin_action(update.effective_user.id, "bca.settings.status",
                         "analytics", 0, f"status={status}",
                         module="admin_broadcast_analytics")
    await bca_settings(update, context)


async def bca_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a boolean setting (bca:settings:toggle:<key>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    key     = query.data.split(":")[-1]
    current = cfg.get_bool(key, True)
    cfg.set(key, "false" if current else "true")
    log_admin_action(update.effective_user.id, "bca.settings.toggle",
                     "analytics", 0, f"{key}: {current} → {not current}",
                     module="admin_broadcast_analytics")
    await bca_settings(update, context)


async def bca_settings_adj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adjust numeric settings (bca:settings:adj:<key>:<delta>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    try:
        key   = parts[3]
        delta = int(parts[4])
    except (IndexError, ValueError):
        return
    allowed = {"broadcast_log_retention_days", "broadcast_max_history"}
    if key not in allowed:
        return
    current = cfg.get_int(key, 90)
    new_val = max(0, current + delta)
    cfg.set(key, str(new_val))
    await bca_settings(update, context)


# ── Cancel conversation ────────────────────────────────────────────────────────

async def bca_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the analytics search conversation."""
    if update.message:
        await update.message.reply_text(
            "❌ Cancelled.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="bca:history")
            ]]))
    return ConversationHandler.END


# ── Conversation handler ───────────────────────────────────────────────────────

def build_bca_conv() -> ConversationHandler:
    """Build the analytics search conversation handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bca_history_search_start, pattern=r"^bca:history:search$"),
        ],
        states={
            BCA_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bca_history_search_recv),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bca_cancel, pattern=r"^bca:cancel$"),
            CommandHandler("cancel", bca_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
