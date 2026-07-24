"""Admin Audit Log — Enhanced V21.

Extends the minimal existing viewer with:
  - All module types (module column)
  - Old value / new value diff display
  - IP address display
  - Search by admin, action, module, target
  - Filter by date range
  - CSV export
  - Replaces the acc:audit:* route handler

Callback namespace: acc:audit:* (replaces admin_audit.py route)
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import get_db_session, AdminAuditLog
from utils.bot_config import cfg
from utils.permissions import has_permission
from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 12

# Conversation states
AUDIT_SEARCH_INPUT = 0


def _is_admin(uid: int) -> bool:
    return uid == settings.ADMIN_TELEGRAM_ID or has_permission(uid, "view_analytics")


async def _safe_edit(query, text: str, kb=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _kb_nav(page: int, has_next: bool, extra_rows: list = None) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"acc:audit:page:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"acc:audit:page:{page+1}"))
    kb = []
    if nav:
        kb.append(nav)
    if extra_rows:
        kb.extend(extra_rows)
    kb.append([
        InlineKeyboardButton("🔍 Search", callback_data="acc:audit:search"),
        InlineKeyboardButton("📥 Export CSV", callback_data="acc:audit:export"),
    ])
    kb.append([
        InlineKeyboardButton("📂 By Module", callback_data="acc:audit:filter_module"),
        InlineKeyboardButton("🕐 Last 24h", callback_data="acc:audit:filter_1d"),
    ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    return InlineKeyboardMarkup(kb)


# ── Main audit menu ───────────────────────────────────────────────────────

async def audit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — renders page 0."""
    await _render_page(update, context, 0)


async def route(action, rest, update, context):
    """Dispatcher called from admin_control_center acc:audit:* routing."""
    if action == "page" and rest:
        try:
            page = max(0, int(rest[0]))
        except Exception:
            page = 0
        await _render_page(update, context, page)
    elif action == "search":
        await _start_search(update, context)
    elif action == "export":
        await _export_csv(update, context)
    elif action == "filter_module":
        await _filter_by_module(update, context)
    elif action == "filter_1d":
        await _filter_period(update, context, hours=24)
    elif action == "detail" and rest:
        try:
            aid = int(rest[0])
        except Exception:
            aid = 0
        await _render_detail(update, context, aid)
    else:
        await _render_page(update, context, 0)


async def _render_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int,
                       search: str = None, module_filter: str = None,
                       since: datetime = None):
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    offset = page * PAGE_SIZE
    with get_db_session() as s:
        q = s.query(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
        if search:
            q = q.filter(or_(
                AdminAuditLog.action.ilike(f"%{search}%"),
                AdminAuditLog.details.ilike(f"%{search}%"),
                AdminAuditLog.target_type.ilike(f"%{search}%"),
            ))
        if module_filter:
            q = q.filter(AdminAuditLog.module == module_filter)
        if since:
            q = q.filter(AdminAuditLog.created_at >= since)
        total = q.count()
        rows = q.offset(offset).limit(PAGE_SIZE + 1).all()
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

        filter_desc = ""
        if search:
            filter_desc = f"  🔍 search: {search}"
        if module_filter:
            filter_desc += f"  📂 module: {module_filter}"
        if since:
            filter_desc += f"  🕐 since {since.strftime('%m/%d %H:%M')}"

        lines = [f"📝 <b>Audit Log</b>  page {page+1}  ({total} total){filter_desc}\n"]
        for r in rows:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            tgt = f" {r.target_type}#{r.target_id}" if r.target_type else ""
            mod = f" [{r.module}]" if getattr(r, 'module', None) else ""
            det = f"\n    ↳ {r.details[:80]}" if r.details else ""
            ip = f" 🌐{r.ip_address}" if getattr(r, 'ip_address', None) else ""
            lines.append(
                f"⏰ <code>{when}</code>  👤<code>{r.admin_telegram_id}</code>\n"
                f"   {r.action}{tgt}{mod}{ip}{det}"
            )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(truncated)"

    extra_rows = []
    if search or module_filter or since:
        extra_rows.append([InlineKeyboardButton("✖️ Clear Filters", callback_data="acc:audit:page:0")])
    kb = _kb_nav(page, has_next, extra_rows)

    if query:
        try:
            await _safe_edit(query, text, kb)
        except Exception:
            try:
                await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _render_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, aid: int):
    query = update.callback_query
    with get_db_session() as s:
        r = s.get(AdminAuditLog, aid)
        if not r:
            await query.answer("❌ Not found.", show_alert=True)
            return
        when = r.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if r.created_at else "?"
        old_v = getattr(r, 'old_value', None) or "—"
        new_v = getattr(r, 'new_value', None) or "—"
        ip = getattr(r, 'ip_address', None) or "—"
        mod = getattr(r, 'module', None) or "—"

        text = (
            f"📝 <b>Audit Entry #{aid}</b>\n\n"
            f"<b>When:</b> {when}\n"
            f"<b>Admin:</b> <code>{r.admin_telegram_id}</code>\n"
            f"<b>Action:</b> <code>{r.action}</code>\n"
            f"<b>Module:</b> {mod}\n"
            f"<b>Target:</b> {r.target_type or '—'} #{r.target_id or '—'}\n"
            f"<b>IP:</b> {ip}\n\n"
            f"<b>Details:</b>\n{r.details or '—'}\n\n"
            f"<b>Old value:</b>\n<pre>{old_v[:500]}</pre>\n"
            f"<b>New value:</b>\n<pre>{new_v[:500]}</pre>"
        )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="acc:audit:page:0")]])
    try:
        await _safe_edit(query, text, kb)
    except Exception:
        pass


# ── Search conversation ───────────────────────────────────────────────────

async def _start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await _safe_edit(query,
            "🔍 <b>Audit Log Search</b>\n\nSend a search term (action, module, details):",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="acc:audit:page:0")]]))
    context.user_data["_audit_search"] = True
    return AUDIT_SEARCH_INPUT


async def _receive_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.pop("_audit_search", None)
    term = (update.message.text or "").strip()
    await _render_page(update, context, 0, search=term)
    return ConversationHandler.END


def build_audit_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(_start_search, pattern=r"^acc:audit:search$")],
        states={
            AUDIT_SEARCH_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_search),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern=r"^acc:audit:page:0$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ── Module filter ─────────────────────────────────────────────────────────

async def _filter_by_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with get_db_session() as s:
        try:
            modules = (s.query(AdminAuditLog.module, func.count(AdminAuditLog.id))
                       .filter(AdminAuditLog.module.isnot(None))
                       .group_by(AdminAuditLog.module)
                       .order_by(func.count(AdminAuditLog.id).desc())
                       .limit(20).all())
        except Exception:
            modules = []

    kb = []
    for mod, cnt in modules:
        if mod:
            kb.append([InlineKeyboardButton(f"📂 {mod} ({cnt})",
                                            callback_data=f"acc:audit:mod:{mod}")])
    if not kb:
        kb.append([InlineKeyboardButton("(no module data yet)", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:audit:page:0")])
    await _safe_edit(query, "📂 <b>Filter by Module</b>\n\nSelect a module:", InlineKeyboardMarkup(kb))


async def _filter_period(update: Update, context: ContextTypes.DEFAULT_TYPE, hours: int):
    since = datetime.utcnow() - timedelta(hours=hours)
    await _render_page(update, context, 0, since=since)


# ── CSV export ────────────────────────────────────────────────────────────

async def _export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Exporting…")
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        rows = (s.query(AdminAuditLog)
                .order_by(AdminAuditLog.created_at.desc())
                .limit(5000).all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "admin_telegram_id", "action",
                     "module", "target_type", "target_id", "details",
                     "old_value", "new_value", "ip_address"])
    for r in rows:
        writer.writerow([
            r.id,
            str(r.created_at) if r.created_at else "",
            r.admin_telegram_id,
            r.action,
            getattr(r, 'module', "") or "",
            r.target_type or "",
            r.target_id or "",
            (r.details or "")[:500],
            (getattr(r, 'old_value', "") or "")[:200],
            (getattr(r, 'new_value', "") or "")[:200],
            getattr(r, 'ip_address', "") or "",
        ])
    buf.seek(0)
    fname = f"audit_log_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    try:
        await query.message.reply_document(
            InputFile(io.BytesIO(buf.getvalue().encode()), filename=fname),
            caption=f"📥 Audit log export — {len(rows)} entries",
        )
    except Exception as e:
        logger.warning("audit CSV export failed: %s", e)
