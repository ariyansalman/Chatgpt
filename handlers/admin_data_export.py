"""V43 — Data Export Center admin handler.

Callback namespace: dec:*
All admin actions require the 'admin' permission.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from services.data_export_service import (
    EXPORT_TYPES, EXPORT_FORMATS,
    create_job, get_job, list_jobs, count_jobs, delete_job, start_job,
    get_stats,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─── ConversationHandler states ───────────────────────────────────────────────
DEC_DATE_FROM = 200
DEC_DATE_TO   = 201
DEC_SCHED_DT  = 202

# ─── Pagination ───────────────────────────────────────────────────────────────
PAGE_SIZE = 8

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_size(b: Optional[int]) -> str:
    if not b:
        return "—"
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b/1024:.1f} KB"
    return f"{b/1024**2:.1f} MB"


def _status_emoji(status: str) -> str:
    return {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌",
            "scheduled": "📅"}.get(status, "❓")


async def _check_perm(update: Update) -> bool:
    uid = update.effective_user.id
    if not has_permission(uid, "admin"):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


def _back_btn(to: str = "dec:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


def _enabled() -> bool:
    return cfg.get("dec_status", "enabled") != "disabled"


# ─── Main menu ────────────────────────────────────────────────────────────────

async def dec_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    status = cfg.get("dec_status", "enabled")
    if status == "maintenance":
        await _edit(update,
                    "📤 <b>Data Export Center</b>\n\n🟡 <b>System under maintenance.</b>",
                    InlineKeyboardMarkup([[_back_btn("acc:root")]]))
        return

    from services import payment_ui as pui
    stats = get_stats()
    text = (
        "📤 <b>Data Export Center</b>\n"
        f"{pui.DIVIDER}\n"
        f"📊 Today: <b>{stats['today']}</b>   "
        f"📅 Week: <b>{stats['weekly']}</b>   "
        f"🗓 Month: <b>{stats['monthly']}</b>\n"
        f"✅ Total done: <b>{stats['total'] - stats['failed'] - stats['pending']}</b>   "
        f"❌ Failed: <b>{stats['failed']}</b>   "
        f"⏳ Pending: <b>{stats['pending']}</b>\n"
    )
    if stats["largest_size"]:
        text += f"📦 Largest: <b>{_fmt_size(stats['largest_size'])}</b> ({stats['largest_type']})\n"

    kb = [
        [InlineKeyboardButton("📦 Export Data",     callback_data="dec:pick_type:0"),
         InlineKeyboardButton("📜 History",         callback_data="dec:history:0")],
        [InlineKeyboardButton("📊 Statistics",      callback_data="dec:stats"),
         InlineKeyboardButton("⚙️ Settings",        callback_data="dec:settings")],
        [_back_btn("acc:root")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Type picker (paginated) ──────────────────────────────────────────────────

async def dec_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:pick_type:{page}
    page = int(parts[2]) if len(parts) > 2 else 0
    types_list = list(EXPORT_TYPES.items())
    pages = math.ceil(len(types_list) / PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    slice_ = types_list[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    kb = []
    row = []
    for slug, meta in slice_:
        row.append(InlineKeyboardButton(meta["label"], callback_data=f"dec:pick_fmt:{slug}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"dec:pick_type:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="dec:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"dec:pick_type:{page+1}"))
    kb.append(nav)
    kb.append([_back_btn("dec:menu")])

    await _edit(update,
                "📤 <b>SELECT EXPORT TYPE</b>\n\nChoose the data set to export:",
                InlineKeyboardMarkup(kb))


# ─── Format picker ────────────────────────────────────────────────────────────

async def dec_pick_fmt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:pick_fmt:{type}
    export_type = parts[2]
    meta = EXPORT_TYPES.get(export_type, {"label": export_type})

    kb = []
    for fmt, label in EXPORT_FORMATS.items():
        kb.append([InlineKeyboardButton(label, callback_data=f"dec:confirm:{export_type}:{fmt}")])
    kb.append([_back_btn("dec:pick_type:0")])

    await _edit(update,
                f"📤 <b>{meta['label']} — Choose Format</b>\n\nSelect the output format:",
                InlineKeyboardMarkup(kb))


# ─── Confirm & launch ─────────────────────────────────────────────────────────

async def dec_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:confirm:{type}:{fmt}
    export_type = parts[2]
    fmt = parts[3]
    type_meta = EXPORT_TYPES.get(export_type, {"label": export_type})
    fmt_label = EXPORT_FORMATS.get(fmt, fmt)

    kb = [
        [InlineKeyboardButton("🚀 Export All (Now)",
                              callback_data=f"dec:run:{export_type}:{fmt}:all")],
        [InlineKeyboardButton("📅 Filter by Date Range",
                              callback_data=f"dec:filter_dates:{export_type}:{fmt}")],
        [InlineKeyboardButton("⏰ Schedule Export",
                              callback_data=f"dec:sched_init:{export_type}:{fmt}")],
        [_back_btn(f"dec:pick_fmt:{export_type}")],
    ]
    await _edit(update,
                f"📤 <b>{type_meta['label']}</b>\n"
                f"Format: {fmt_label}\n\n"
                "Choose export options:",
                InlineKeyboardMarkup(kb))


# ─── Run immediately ──────────────────────────────────────────────────────────

async def dec_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⏳ Starting export…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:run:{type}:{fmt}:{filter_key}
    export_type = parts[2]
    fmt = parts[3]
    filter_key = parts[4] if len(parts) > 4 else "all"

    filters_data = {}
    if filter_key != "all" and context.user_data.get("dec_filters"):
        filters_data = context.user_data.pop("dec_filters", {})

    admin_id = update.effective_user.id
    job_id = create_job(admin_id, export_type, fmt, filters=filters_data)
    if not job_id:
        await _edit(update, "❌ Failed to create export job. Please try again.",
                    InlineKeyboardMarkup([[_back_btn("dec:menu")]]))
        return

    start_job(job_id)

    kb = [
        [InlineKeyboardButton("🔄 Check Status", callback_data=f"dec:job:{job_id}")],
        [_back_btn("dec:menu")],
    ]
    type_meta = EXPORT_TYPES.get(export_type, {"label": export_type})
    await _edit(update,
                f"✅ <b>Export Started</b>\n\n"
                f"Type: {type_meta['label']}\n"
                f"Format: {EXPORT_FORMATS.get(fmt, fmt)}\n"
                f"Job ID: <code>#{job_id}</code>\n\n"
                "The export is running in the background.\n"
                "Use «Check Status» to see when it's ready.",
                InlineKeyboardMarkup(kb))


# ─── Date filter conversation ─────────────────────────────────────────────────

async def dec_filter_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return ConversationHandler.END

    parts = q.data.split(":")          # dec:filter_dates:{type}:{fmt}
    context.user_data["dec_pending_type"] = parts[2]
    context.user_data["dec_pending_fmt"] = parts[3]

    await _edit(update,
                "📅 <b>Date Filter — Step 1/2</b>\n\n"
                "Enter the <b>start date</b> (YYYY-MM-DD) or send /skip to skip:",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",
                                                             callback_data="dec:cancel_conv")]]))
    return DEC_DATE_FROM


async def dec_date_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "/skip":
        context.user_data.setdefault("dec_filters", {})["date_from"] = None
    else:
        try:
            datetime.fromisoformat(text)
            context.user_data.setdefault("dec_filters", {})["date_from"] = text
        except ValueError:
            await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD format or /skip:")
            return DEC_DATE_FROM

    await update.message.reply_text(
        "📅 <b>Date Filter — Step 2/2</b>\n\nEnter the <b>end date</b> (YYYY-MM-DD) or /skip:",
        parse_mode="HTML")
    return DEC_DATE_TO


async def dec_date_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "/skip":
        context.user_data.setdefault("dec_filters", {})["date_to"] = None
    else:
        try:
            datetime.fromisoformat(text)
            context.user_data.setdefault("dec_filters", {})["date_to"] = text
        except ValueError:
            await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD or /skip:")
            return DEC_DATE_TO

    export_type = context.user_data.get("dec_pending_type", "")
    fmt = context.user_data.get("dec_pending_fmt", "")
    filters_data = context.user_data.get("dec_filters", {})
    admin_id = update.effective_user.id

    job_id = create_job(admin_id, export_type, fmt, filters=filters_data)
    if not job_id:
        await update.message.reply_text("❌ Failed to create export job.")
        return ConversationHandler.END

    start_job(job_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Check Status", callback_data=f"dec:job:{job_id}")],
        [InlineKeyboardButton("📜 History", callback_data="dec:history:0")],
        [InlineKeyboardButton("🏠 DEC Menu", callback_data="dec:menu")],
    ])
    await update.message.reply_text(
        f"✅ <b>Export started!</b>\nJob ID: <code>#{job_id}</code>\n"
        "Filters applied. Check status when ready.",
        parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


async def dec_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("Cancelled")
    context.user_data.pop("dec_filters", None)
    context.user_data.pop("dec_pending_type", None)
    context.user_data.pop("dec_pending_fmt", None)
    await _edit(update, "❌ Export cancelled.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 DEC Menu",
                                                             callback_data="dec:menu")]]))
    return ConversationHandler.END


# ─── Schedule conversation ────────────────────────────────────────────────────

async def dec_sched_init(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return ConversationHandler.END

    parts = q.data.split(":")          # dec:sched_init:{type}:{fmt}
    context.user_data["dec_sched_type"] = parts[2]
    context.user_data["dec_sched_fmt"] = parts[3]

    await _edit(update,
                "⏰ <b>Schedule Export</b>\n\n"
                "Enter the datetime to run the export (UTC):\n"
                "<code>YYYY-MM-DD HH:MM</code>",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",
                                                             callback_data="dec:cancel_conv")]]))
    return DEC_SCHED_DT


async def dec_sched_dt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        sched_dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Use <code>YYYY-MM-DD HH:MM</code> (UTC):",
            parse_mode="HTML")
        return DEC_SCHED_DT

    if sched_dt <= datetime.utcnow():
        await update.message.reply_text("❌ Scheduled time must be in the future.")
        return DEC_SCHED_DT

    export_type = context.user_data.get("dec_sched_type", "")
    fmt = context.user_data.get("dec_sched_fmt", "")
    admin_id = update.effective_user.id

    job_id = create_job(admin_id, export_type, fmt, scheduled_at=sched_dt)
    if not job_id:
        await update.message.reply_text("❌ Failed to schedule export.")
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 History", callback_data="dec:history:0")],
        [InlineKeyboardButton("🏠 DEC Menu", callback_data="dec:menu")],
    ])
    await update.message.reply_text(
        f"📅 <b>Export scheduled!</b>\nJob ID: <code>#{job_id}</code>\n"
        f"Runs at: <code>{sched_dt.strftime('%Y-%m-%d %H:%M UTC')}</code>",
        parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


# ─── Job detail ──────────────────────────────────────────────────────────────

async def dec_job_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:job:{job_id}
    job_id = int(parts[2])
    job = get_job(job_id)
    if not job:
        await _edit(update, "❌ Job not found.",
                    InlineKeyboardMarkup([[_back_btn("dec:history:0")]]))
        return

    type_meta = EXPORT_TYPES.get(job["export_type"], {"label": job["export_type"]})
    st = _status_emoji(job["status"])
    created = job["created_at"].strftime("%Y-%m-%d %H:%M") if job["created_at"] else "—"
    completed = job["completed_at"].strftime("%Y-%m-%d %H:%M") if job["completed_at"] else "—"
    scheduled = job["scheduled_at"].strftime("%Y-%m-%d %H:%M") if job["scheduled_at"] else "—"

    text = (
        f"📤 <b>Export Job #{job_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Type: {type_meta['label']}\n"
        f"Format: {EXPORT_FORMATS.get(job['format'], job['format'])}\n"
        f"Status: {st} <b>{job['status'].upper()}</b>\n"
        f"Rows: <b>{job['row_count']:,}</b>\n" if job["row_count"] else
        f"Status: {st} <b>{job['status'].upper()}</b>\n"
    )
    text = (
        f"📤 <b>Export Job #{job_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Type: {type_meta['label']}\n"
        f"📄 Format: {EXPORT_FORMATS.get(job['format'], job['format'])}\n"
        f"Status: {st} <b>{job['status'].upper()}</b>\n"
        f"📊 Rows: <b>{job['row_count']:,}</b>\n"
        f"💾 Size: <b>{_fmt_size(job['file_size'])}</b>\n"
        f"🕐 Created: {created}\n"
        f"✅ Completed: {completed}\n"
    )
    if scheduled != "—":
        text += f"📅 Scheduled: {scheduled}\n"
    if job["error_message"]:
        text += f"\n❌ Error:\n<code>{job['error_message'][:300]}</code>"

    kb = []
    if job["status"] == "done" and job["file_path"]:
        kb.append([InlineKeyboardButton("⬇️ Download File", callback_data=f"dec:dl:{job_id}")])
    if job["status"] in ("pending", "running"):
        kb.append([InlineKeyboardButton("🔄 Refresh Status", callback_data=f"dec:job:{job_id}")])
    kb.append([InlineKeyboardButton("🗑 Delete Job", callback_data=f"dec:del:{job_id}")])
    kb.append([_back_btn("dec:history:0")])

    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Download ────────────────────────────────────────────────────────────────

async def dec_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⬇️ Sending file…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:dl:{job_id}
    job_id = int(parts[2])
    job = get_job(job_id)

    if not job or job["status"] != "done" or not job["file_path"]:
        await q.answer("❌ File not available.", show_alert=True)
        return

    path = Path(job["file_path"])
    if not path.exists():
        await q.answer("❌ File has been deleted.", show_alert=True)
        return

    admin_id = update.effective_user.id
    type_meta = EXPORT_TYPES.get(job["export_type"], {"label": job["export_type"]})
    caption = (f"📤 {type_meta['label']} export\n"
               f"Format: {EXPORT_FORMATS.get(job['format'], job['format'])}\n"
               f"Rows: {job['row_count']:,} | Size: {_fmt_size(job['file_size'])}\n"
               f"Job #{job_id}")
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=path.name,
                caption=caption,
            )
        try:
            log_admin_action(admin_id, "dec_download", details=f"job_id={job_id}")
        except Exception:
            pass
    except Exception as e:
        logger.error("dec_download: %s", e)
        await q.answer("❌ Failed to send file.", show_alert=True)


# ─── Delete job ───────────────────────────────────────────────────────────────

async def dec_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:del:{job_id}
    job_id = int(parts[2])
    ok = delete_job(job_id)
    text = f"✅ Job #{job_id} deleted." if ok else f"❌ Could not delete job #{job_id}."
    await _edit(update, text,
                InlineKeyboardMarkup([[InlineKeyboardButton("📜 History",
                                                             callback_data="dec:history:0")],
                                      [_back_btn("dec:menu")]]))


# ─── History ──────────────────────────────────────────────────────────────────

async def dec_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:history:{page}
    page = int(parts[2]) if len(parts) > 2 else 0
    admin_id = update.effective_user.id

    total = count_jobs()
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    jobs = list_jobs(offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    if not jobs:
        await _edit(update, "📜 <b>Export History</b>\n\nNo export jobs yet.",
                    InlineKeyboardMarkup([[_back_btn("dec:menu")]]))
        return

    text = f"📜 <b>Export History</b> — Page {page+1}/{pages}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    for j in jobs:
        st = _status_emoji(j["status"])
        size_str = f" {_fmt_size(j['file_size'])}" if j["file_size"] else ""
        date_str = j["created_at"].strftime("%m-%d %H:%M") if j["created_at"] else ""
        label_short = (j["label"] or j["export_type"])[:28]
        kb.append([InlineKeyboardButton(
            f"{st} {label_short}{size_str} — {date_str}",
            callback_data=f"dec:job:{j['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"dec:history:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="dec:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"dec:history:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_btn("dec:menu")])

    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Statistics ───────────────────────────────────────────────────────────────

async def dec_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    s = get_stats()
    recent_at = s["recent_at"].strftime("%Y-%m-%d %H:%M") if s["recent_at"] else "—"
    text = (
        "📊 <b>EXPORT STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Today's exports:   <b>{s['today']}</b>\n"
        f"📆 Weekly exports:   <b>{s['weekly']}</b>\n"
        f"🗓 Monthly exports:  <b>{s['monthly']}</b>\n"
        f"✅ Total completed:  <b>{s['total'] - s['failed'] - s['pending']}</b>\n"
        f"❌ Failed:           <b>{s['failed']}</b>\n"
        f"⏳ In queue:         <b>{s['pending']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Largest export:   <b>{_fmt_size(s['largest_size'])}</b>"
        f"{' (' + s['largest_type'] + ')' if s['largest_type'] else ''}\n"
        f"🕐 Last completed:   {recent_at}\n"
        f"{('📄 ' + s['recent_label']) if s['recent_label'] else ''}"
    )
    await _edit(update, text,
                InlineKeyboardMarkup([[_back_btn("dec:menu")]]))


# ─── Settings ────────────────────────────────────────────────────────────────

async def dec_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    status = cfg.get("dec_status", "enabled")
    auto_cleanup_days = cfg.get_int("dec_auto_cleanup_days", 30)
    max_file_mb = cfg.get_int("dec_max_file_mb", 50)

    text = (
        "⚙️ <b>DEC Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: <b>{status.upper()}</b>\n"
        f"Auto-cleanup after: <b>{auto_cleanup_days} days</b>\n"
        f"Max file size: <b>{max_file_mb} MB</b>\n"
    )
    def _s(k: str, v: str) -> str:
        return "✅" if status == v else "○"

    kb = [
        [InlineKeyboardButton(f"{_s('dec_status','enabled')} 🟢 Enable",
                              callback_data="dec:set:dec_status:enabled"),
         InlineKeyboardButton(f"{_s('dec_status','maintenance')} 🟡 Maintenance",
                              callback_data="dec:set:dec_status:maintenance"),
         InlineKeyboardButton(f"{_s('dec_status','disabled')} 🔴 Disable",
                              callback_data="dec:set:dec_status:disabled")],
        [_back_btn("dec:menu")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def dec_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # dec:set:{key}:{value}
    key, value = parts[2], parts[3]
    cfg.set(key, value)
    try:
        log_admin_action(update.effective_user.id, "dec_settings",
                         details=f"{key}={value}")
    except Exception:
        pass
    await dec_settings(update, context)


# ─── No-op ───────────────────────────────────────────────────────────────────

async def dec_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ─── Handler registration ─────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all Data Export Center handlers. Called from bot.py main()."""

    # Date-filter conversation
    date_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dec_filter_dates, pattern=r"^dec:filter_dates:")],
        states={
            DEC_DATE_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, dec_date_from),
                            MessageHandler(filters.Regex(r"^/skip$"), dec_date_from)],
            DEC_DATE_TO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, dec_date_to),
                            MessageHandler(filters.Regex(r"^/skip$"), dec_date_to)],
        },
        fallbacks=[
            CallbackQueryHandler(dec_cancel_conv, pattern=r"^dec:cancel_conv$"),
            CommandHandler("cancel", dec_cancel_conv),
        ],
        per_message=False,
        allow_reentry=True,
        name="dec_date_filter",
    )

    # Schedule conversation
    sched_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dec_sched_init, pattern=r"^dec:sched_init:")],
        states={
            DEC_SCHED_DT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dec_sched_dt)],
        },
        fallbacks=[
            CallbackQueryHandler(dec_cancel_conv, pattern=r"^dec:cancel_conv$"),
            CommandHandler("cancel", dec_cancel_conv),
        ],
        per_message=False,
        allow_reentry=True,
        name="dec_schedule",
    )

    application.add_handler(date_conv)
    application.add_handler(sched_conv)

    # Plain callback handlers
    application.add_handler(CallbackQueryHandler(dec_menu,       pattern=r"^dec:menu$"))
    application.add_handler(CallbackQueryHandler(dec_pick_type,  pattern=r"^dec:pick_type:"))
    application.add_handler(CallbackQueryHandler(dec_pick_fmt,   pattern=r"^dec:pick_fmt:"))
    application.add_handler(CallbackQueryHandler(dec_confirm,    pattern=r"^dec:confirm:"))
    application.add_handler(CallbackQueryHandler(dec_run,        pattern=r"^dec:run:"))
    application.add_handler(CallbackQueryHandler(dec_job_detail, pattern=r"^dec:job:"))
    application.add_handler(CallbackQueryHandler(dec_download,   pattern=r"^dec:dl:"))
    application.add_handler(CallbackQueryHandler(dec_delete,     pattern=r"^dec:del:"))
    application.add_handler(CallbackQueryHandler(dec_history,    pattern=r"^dec:history:"))
    application.add_handler(CallbackQueryHandler(dec_stats,      pattern=r"^dec:stats$"))
    application.add_handler(CallbackQueryHandler(dec_settings,   pattern=r"^dec:settings$"))
    application.add_handler(CallbackQueryHandler(dec_set,        pattern=r"^dec:set:"))
    application.add_handler(CallbackQueryHandler(dec_noop,       pattern=r"^dec:noop$"))

    logger.info("V43: Data Export Center handlers registered.")
