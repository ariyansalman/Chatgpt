"""V42 — Admin Global Activity Timeline.

Callback namespace: ``gat:*``

Callbacks:
  gat:menu                      — Main dashboard with stats
  gat:view:PAGE                 — Timeline list (paginated)
  gat:filter:CAT:PAGE           — Filter by category
  gat:entry:ID                  — Entry detail
  gat:date:PRESET               — Date preset filter (today|yesterday|week|month)
  gat:search                    — Enter search mode (ConversationHandler)
  gat:export:FORMAT             — Export (csv | json)
  gat:delete:confirm            — Confirm delete old entries
  gat:delete:go:DAYS            — Execute delete
  gat:stats                     — Statistics panel
  gat:settings                  — Settings panel
  gat:settings:toggle:KEY       — Toggle a setting
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from math import ceil
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from telegram.error import BadRequest

from services.global_timeline import (
    get_timeline, get_stats, export_csv, export_json, delete_old_entries,
    CATEGORY_LABELS,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 15
_SEARCH_STATE = 9200   # unique ConversationHandler state


# ─── Keyboard helpers ─────────────────────────────────────────────────────────

def _back_btn(cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("🔙 Back", callback_data=cb)


def _main_menu_kb() -> InlineKeyboardMarkup:
    cats = list(CATEGORY_LABELS.items())
    # Group categories two per row
    cat_rows = []
    for i in range(0, len(cats), 2):
        row = []
        for slug, label in cats[i:i+2]:
            row.append(InlineKeyboardButton(label, callback_data=f"gat:filter:{slug}:1"))
        cat_rows.append(row)

    kb = [
        [InlineKeyboardButton("📜 All Activity",   callback_data="gat:view:1"),
         InlineKeyboardButton("📊 Statistics",     callback_data="gat:stats")],
        [InlineKeyboardButton("🔍 Search",         callback_data="gat:search"),
         InlineKeyboardButton("📅 Today",          callback_data="gat:date:today:1")],
        [InlineKeyboardButton("📅 Yesterday",      callback_data="gat:date:yesterday:1"),
         InlineKeyboardButton("📅 Last 7 Days",    callback_data="gat:date:week:1")],
        [InlineKeyboardButton("📅 Last 30 Days",   callback_data="gat:date:month:1"),
         InlineKeyboardButton("⚙️ Settings",       callback_data="gat:settings")],
        *cat_rows,
        [InlineKeyboardButton("📤 Export CSV",     callback_data="gat:export:csv"),
         InlineKeyboardButton("📤 Export JSON",    callback_data="gat:export:json")],
        [InlineKeyboardButton("🗑 Delete Old Logs", callback_data="gat:delete:confirm")],
        [_back_btn("acc:root")],
    ]
    return InlineKeyboardMarkup(kb)


def _timeline_kb(entries, page: int, total_pages: int, back_cb: str) -> InlineKeyboardMarkup:
    rows = []
    for e in entries:
        ts = e.created_at.strftime("%m/%d %H:%M") if e.created_at else "?"
        label = f"[{ts}] {e.action[:20]}: {(e.description or '')[:25]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"gat:entry:{e.id}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{back_cb}:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"{back_cb}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_back_btn("gat:menu")])
    return InlineKeyboardMarkup(rows)


def _delete_confirm_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🗑 Delete >30 days",  callback_data="gat:delete:go:30"),
         InlineKeyboardButton("🗑 Delete >60 days",  callback_data="gat:delete:go:60")],
        [InlineKeyboardButton("🗑 Delete >90 days",  callback_data="gat:delete:go:90"),
         InlineKeyboardButton("🗑 Delete >180 days", callback_data="gat:delete:go:180")],
        [_back_btn("gat:menu")],
    ]
    return InlineKeyboardMarkup(kb)


def _settings_kb() -> InlineKeyboardMarkup:
    enabled = cfg.get_bool("gat_enabled", True)
    archive = cfg.get_bool("gat_auto_archive", False)

    def _tb(label: str, key: str, val: bool) -> InlineKeyboardButton:
        icon = "✅" if val else "☑️"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"gat:settings:toggle:{key}")

    kb = [
        [_tb("Timeline Enabled", "gat_enabled", enabled)],
        [_tb("Auto Archive",     "gat_auto_archive", archive)],
        [_back_btn("gat:menu")],
    ]
    return InlineKeyboardMarkup(kb)


# ─── Date preset helpers ──────────────────────────────────────────────────────

def _resolve_date_preset(preset: str) -> tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset == "today":
        return today, None
    if preset == "yesterday":
        yest = today - timedelta(days=1)
        return yest, today
    if preset == "week":
        return today - timedelta(days=7), None
    if preset == "month":
        return today - timedelta(days=30), None
    return None, None


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def gat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    stats = get_stats()
    text = (
        "📜 <b>Global Activity Timeline</b>\n\n"
        f"📊 Total Events:     <b>{stats.get('total', 0):,}</b>\n"
        f"📅 Today:            <b>{stats.get('today', 0):,}</b>\n"
        f"📅 This Week:        <b>{stats.get('week', 0):,}</b>\n"
        f"📅 This Month:       <b>{stats.get('month', 0):,}</b>\n\n"
        "Use the buttons below to browse, search, filter, or export."
    )
    try:
        await query.edit_message_text(text, reply_markup=_main_menu_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paginated full timeline view."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = query.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    entries, total = get_timeline(page=page, page_size=_PAGE_SIZE)
    total_pages = max(1, ceil(total / _PAGE_SIZE))
    page = max(1, min(page, total_pages))

    text = (
        f"📜 <b>All Activity</b>  (page {page}/{total_pages}, {total:,} total)\n\n"
        "Tap an entry for details."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_timeline_kb(entries, page, total_pages, "gat:view"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paginated timeline filtered by category."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # gat:filter:CAT:PAGE
    parts = query.data.split(":")
    cat  = parts[2] if len(parts) > 2 else ""
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    entries, total = get_timeline(page=page, page_size=_PAGE_SIZE, category=cat)
    total_pages = max(1, ceil(total / _PAGE_SIZE))
    page = max(1, min(page, total_pages))

    cat_label = CATEGORY_LABELS.get(cat, cat)
    text = (
        f"📜 <b>Timeline — {cat_label}</b>  (page {page}/{total_pages}, {total:,} total)\n\n"
        "Tap an entry for details."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_timeline_kb(entries, page, total_pages, f"gat:filter:{cat}"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Timeline filtered by date preset."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # gat:date:PRESET:PAGE
    parts = query.data.split(":")
    preset = parts[2] if len(parts) > 2 else "today"
    page   = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    date_from, date_to = _resolve_date_preset(preset)
    entries, total = get_timeline(page=page, page_size=_PAGE_SIZE,
                                  date_from=date_from, date_to=date_to)
    total_pages = max(1, ceil(total / _PAGE_SIZE))
    page = max(1, min(page, total_pages))

    preset_labels = {
        "today": "Today", "yesterday": "Yesterday",
        "week": "Last 7 Days", "month": "Last 30 Days",
    }
    label = preset_labels.get(preset, preset)
    text = (
        f"📅 <b>Timeline — {label}</b>  (page {page}/{total_pages}, {total:,} entries)\n\n"
        "Tap an entry for details."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_timeline_kb(entries, page, total_pages, f"gat:date:{preset}"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detail view of a single timeline entry."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    entry_id = int(query.data.split(":")[2])
    try:
        from database import get_db_session
        from database.models import GlobalActivityEntry
        with get_db_session() as s:
            entry = s.query(GlobalActivityEntry).get(entry_id)
            if entry:
                s.expunge(entry)
    except Exception:
        entry = None

    if entry is None:
        await query.answer("Entry not found.", show_alert=True)
        return

    cat_label = CATEGORY_LABELS.get(entry.category, entry.category or "—")
    ts = entry.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if entry.created_at else "—"

    text = (
        f"📋 <b>Activity Entry #{entry.id}</b>\n\n"
        f"<b>Action:</b>      {entry.action}\n"
        f"<b>Category:</b>    {cat_label}\n"
        f"<b>Status:</b>      {entry.status or '—'}\n"
        f"<b>Time:</b>        {ts}\n"
        f"<b>User ID:</b>     {entry.user_id or '—'}\n"
        f"<b>Username:</b>    @{entry.username or '—'}\n"
        f"<b>Admin TG ID:</b> {entry.admin_telegram_id or '—'}\n"
        f"<b>IP Address:</b>  {entry.ip_address or '—'}\n"
        f"<b>Ref:</b>         {entry.ref_type or '—'} #{entry.ref_id or '—'}\n\n"
        f"<b>Description:</b>\n{entry.description or '—'}"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_back_btn("gat:view:1"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    stats = get_stats()

    top_users_str = "\n".join(
        f"  @{u} — {cnt:,}" for u, cnt in stats.get("top_users", [])
    ) or "  (none)"
    top_admins_str = "\n".join(
        f"  #{a} — {cnt:,}" for a, cnt in stats.get("top_admins", [])
    ) or "  (none)"
    top_actions_str = "\n".join(
        f"  {a}: {cnt:,}" for a, cnt in stats.get("top_actions", [])
    ) or "  (none)"

    text = (
        "📊 <b>Timeline Statistics</b>\n\n"
        f"Total entries:  <b>{stats.get('total', 0):,}</b>\n"
        f"Today:          <b>{stats.get('today', 0):,}</b>\n"
        f"This Week:      <b>{stats.get('week', 0):,}</b>\n"
        f"This Month:     <b>{stats.get('month', 0):,}</b>\n\n"
        f"<b>Most Active Users:</b>\n{top_users_str}\n\n"
        f"<b>Most Active Admins:</b>\n{top_admins_str}\n\n"
        f"<b>Most Common Actions:</b>\n{top_actions_str}"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_back_btn("gat:menu"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export timeline as CSV or JSON file."""
    query = update.callback_query
    await query.answer("⏳ Generating export…")
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    fmt = query.data.split(":")[2]   # csv | json
    try:
        if fmt == "csv":
            content = export_csv(max_rows=10_000)
            fname = f"timeline_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
            bio = io.BytesIO(content.encode("utf-8"))
        else:
            content = export_json(max_rows=10_000)
            fname = f"timeline_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            bio = io.BytesIO(content.encode("utf-8"))

        bio.name = fname
        await query.message.reply_document(
            document=bio,
            caption=f"📤 Global Activity Timeline export — {fmt.upper()}\n"
                    f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        )
        log_admin_action(update.effective_user.id, f"gat.export.{fmt}",
                         details=f"Exported timeline as {fmt.upper()}", module="global_activity_timeline")
    except Exception:
        logger.exception("gat_export: failed")
        await query.message.reply_text("❌ Export failed. Please try again.")


async def gat_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    text = (
        "🗑 <b>Delete Old Timeline Entries</b>\n\n"
        "Choose a retention period. Entries <b>older</b> than the selected "
        "number of days will be permanently deleted.\n\n"
        "⚠️ This action cannot be undone."
    )
    try:
        await query.edit_message_text(text, reply_markup=_delete_confirm_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_delete_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Deleting…")
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    days = int(query.data.split(":")[3])
    count = delete_old_entries(days=days)

    log_admin_action(update.effective_user.id, "gat.delete_old",
                     details=f"Deleted {count} entries older than {days} days",
                     module="global_activity_timeline")

    text = (
        f"🗑 <b>Deletion Complete</b>\n\n"
        f"Deleted <b>{count:,}</b> entries older than <b>{days} days</b>."
    )
    try:
        await query.edit_message_text(text, reply_markup=_back_btn("gat:menu"), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    text = (
        "⚙️ <b>Activity Timeline Settings</b>\n\n"
        "Configure the Global Activity Timeline behaviour."
    )
    try:
        await query.edit_message_text(text, reply_markup=_settings_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gat_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    key = query.data.split(":", 3)[3]
    allowed = {"gat_enabled", "gat_auto_archive"}
    if key not in allowed:
        await query.answer("Unknown key.", show_alert=True)
        return

    old = cfg.get_bool(key, True)
    new_val = "false" if old else "true"
    try:
        with __import__("database").get_db_session() as s:
            from database.models import BotConfig
            row = s.query(BotConfig).filter_by(key=key).first()
            if row:
                row.value = new_val
            else:
                s.add(BotConfig(key=key, value=new_val, value_type="bool",
                                category="global_activity_timeline", label=key))
            s.commit()
    except Exception:
        logger.exception("gat_settings_toggle: failed key=%s", key)
        await query.answer("❌ Failed to save.", show_alert=True)
        return

    log_admin_action(update.effective_user.id, "gat.settings.toggle", "config", key,
                     details=f"{old} → {not old}", module="global_activity_timeline")
    await gat_settings(with_data(update, "gat:settings"), context)


# ─── Search conversation ──────────────────────────────────────────────────────

async def gat_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        "🔍 <b>Search Timeline</b>\n\n"
        "Send a keyword, username, action, or order/product ID to search:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="gat:menu")
        ]]),
        parse_mode="HTML",
    )
    return _SEARCH_STATE


async def gat_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search_text = (update.message.text or "").strip()
    await update.message.delete()

    entries, total = get_timeline(page=1, page_size=_PAGE_SIZE, search=search_text)

    if not entries:
        msg_text = f"🔍 No results for <b>{search_text}</b>."
        kb = InlineKeyboardMarkup([[_back_btn("gat:menu")]])
    else:
        total_pages = max(1, ceil(total / _PAGE_SIZE))
        msg_text = (
            f"🔍 <b>Search: {search_text}</b>  ({total:,} results, page 1/{total_pages})\n\n"
            "Tap an entry for details."
        )
        kb = _timeline_kb(entries, 1, total_pages, "gat:view")

    await update.effective_chat.send_message(msg_text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all gat:* handlers."""
    application.add_handler(CallbackQueryHandler(gat_menu,            pattern=r"^gat:menu$"))
    application.add_handler(CallbackQueryHandler(gat_view,            pattern=r"^gat:view:\d+$"))
    application.add_handler(CallbackQueryHandler(gat_filter,          pattern=r"^gat:filter:.+:\d+$"))
    application.add_handler(CallbackQueryHandler(gat_date,            pattern=r"^gat:date:.+:\d+$"))
    application.add_handler(CallbackQueryHandler(gat_entry,           pattern=r"^gat:entry:\d+$"))
    application.add_handler(CallbackQueryHandler(gat_stats,           pattern=r"^gat:stats$"))
    application.add_handler(CallbackQueryHandler(gat_export,          pattern=r"^gat:export:(csv|json)$"))
    application.add_handler(CallbackQueryHandler(gat_delete_confirm,  pattern=r"^gat:delete:confirm$"))
    application.add_handler(CallbackQueryHandler(gat_delete_go,       pattern=r"^gat:delete:go:\d+$"))
    application.add_handler(CallbackQueryHandler(gat_settings,        pattern=r"^gat:settings$"))
    application.add_handler(CallbackQueryHandler(gat_settings_toggle, pattern=r"^gat:settings:toggle:.+$"))

    # Search conversation
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(gat_search_start, pattern=r"^gat:search$")],
        states={
            _SEARCH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, gat_search_query)],
        },
        fallbacks=[CallbackQueryHandler(gat_menu, pattern=r"^gat:menu$")],
        per_message=False,
    )
    application.add_handler(search_conv)

    logger.info("admin_global_timeline: handlers registered (gat:*)")
