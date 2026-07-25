"""System Tools — health, activity logs, DB status, cache, backup, restore, jobs."""
from __future__ import annotations

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import inspect, text

from database import get_db_session
from database.db import engine
from database.models import Base
from utils.audit import log_admin_action
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# Placeholder items: callback action → (emoji, display title, description)
_PLACEHOLDERS: dict[str, tuple[str, str, str]] = {
    "logs":    ("📝", "Activity Logs",   "Admin action history and event stream."),
    "db":      ("🗄️", "Database Status", "Connection pool, table sizes, and query stats."),
    "cache":   ("🧹", "Cache Manager",   "View and flush in-memory and Redis caches."),
    "backup":  ("📤", "Backup",          "Export a full database snapshot."),
    "restore": ("📥", "Restore",         "Import a previously exported database snapshot."),
}


def _kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="acc:root")]])


def _kb_sys_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to System Tools", callback_data="acc:system")]])


async def system_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """System Tools sub-panel — diagnostics + future-expansion placeholders."""
    query = update.callback_query
    text_ = (
        "🖥️ <b>System Tools</b>\n\n"
        "Live diagnostics are marked 🟢. "
        "Items marked 🚧 are reserved for a future update."
    )
    kb = [
        # Live tools
        [InlineKeyboardButton("📈 System Health",    callback_data="acc:sys:health"),
         InlineKeyboardButton("🔄 Background Jobs",  callback_data="acc:sys:jobs")],
        [InlineKeyboardButton("📐 Schema Drift",     callback_data="acc:sys:drift"),
         InlineKeyboardButton("🔧 Maintenance",      callback_data="admin_maintenance_toggle")],
        # Placeholders (🚧 future)
        [InlineKeyboardButton("📝 Activity Logs 🚧", callback_data="acc:sys:logs"),
         InlineKeyboardButton("🗄️ Database Status 🚧", callback_data="acc:sys:db")],
        [InlineKeyboardButton("🧹 Cache Manager 🚧", callback_data="acc:sys:cache"),
         InlineKeyboardButton("📤 Backup 🚧",        callback_data="acc:sys:backup")],
        [InlineKeyboardButton("📥 Restore 🚧",       callback_data="acc:sys:restore")],
        # Navigation
        [InlineKeyboardButton("🔙 Back",             callback_data="acc:root")],
    ]
    try:
        try:
            await query.edit_message_text(text_, reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def _render_coming_soon(action: str, update, context) -> None:
    """Generic 'not yet available' screen for placeholder menu items."""
    emoji, title, desc = _PLACEHOLDERS.get(
        action, ("🚧", action.replace("_", " ").title(), "")
    )
    query = update.callback_query
    text_ = (
        f"{emoji} <b>{title}</b>\n\n"
        "🚧 <i>This feature is not yet available.</i>\n\n"
        f"{desc}\n\n"
        "<i>It will be implemented in a future update.</i>"
    )
    try:
        await query.answer()
        try:
            await query.edit_message_text(text_, reply_markup=_kb_sys_back(),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def route(action, rest, update, context):
    update.callback_query
    if action == "health":
        await _render_health(update, context)
        return
    if action == "drift":
        await _render_drift(update, context)
        return
    if action == "jobs":
        await _render_jobs(update, context)
        return
    # Placeholder routes — show coming-soon screen, no backend logic
    if action in _PLACEHOLDERS:
        await _render_coming_soon(action, update, context)
        return
    await system_menu(update, context)


async def _render_health(update, context):
    query = update.callback_query
    lines = ["📈 <b>System Health</b>", ""]
    try:
        with get_db_session() as s:
            s.execute(text("SELECT 1"))
        lines.append(f"• Engine: <code>{engine.url.drivername}</code>")
        lines.append(f"• SELECT 1: 🟢 ok")
        insp = inspect(engine)
        lines.append(f"• Live tables: <b>{len(insp.get_table_names())}</b>")
    except Exception as e:
        lines.append(f"❌ {e}")
    try:
        await query.answer()
        try:
            await query.edit_message_text("\n".join(lines),
                                          reply_markup=_kb_back(),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def _render_drift(update, context):
    query = update.callback_query
    lines = ["📐 <b>Schema drift report</b>", ""]
    try:
        insp = inspect(engine)
        live_tables = set(insp.get_table_names())
        missing_tables = []
        missing_columns = []
        for tname, table in Base.metadata.tables.items():
            if tname not in live_tables:
                missing_tables.append(tname)
                continue
            live_cols = {c["name"] for c in insp.get_columns(tname)}
            for col in table.columns:
                if col.name not in live_cols:
                    missing_columns.append(f"{tname}.{col.name}")
        if missing_tables:
            lines.append("Missing tables:")
            for t in missing_tables:
                lines.append(f"  • {t}")
        if missing_columns:
            lines.append("Missing columns:")
            for c in missing_columns[:40]:
                lines.append(f"  • {c}")
        if not missing_tables and not missing_columns:
            lines.append("🟢 In sync with ORM metadata.")
    except Exception as e:
        lines.append(f"❌ {e}")
    log_admin_action(update.effective_user.id, "system.drift_report")
    try:
        await query.answer()
        try:
            await query.edit_message_text("\n".join(lines),
                                          reply_markup=_kb_back(),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def _render_jobs(update, context):
    query = update.callback_query
    lines = ["🔄 <b>Background Jobs</b>", ""]
    jq = getattr(context, "job_queue", None)
    if jq is None:
        lines.append("(no job queue)")
    else:
        jobs = list(jq.jobs()) if hasattr(jq, "jobs") else []
        if not jobs:
            lines.append("— no jobs scheduled —")
        for j in jobs[:50]:
            nxt = getattr(j, "next_t", None)
            lines.append(f"• {j.name}  next={nxt}")
    try:
        await query.answer()
        try:
            await query.edit_message_text("\n".join(lines),
                                          reply_markup=_kb_back(),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass
