"""V37 — File & License Key Manager admin handler.

Callback namespace: ``flm:*``

Sub-namespaces
──────────────
flm:menu                          — Main hub (stats + nav)
flm:files:list:PAGE               — Paginated file list
flm:files:view:ID                 — File detail
flm:files:arch:ID                 — Archive file
flm:files:del:ID                  — Delete file
flm:files:upload_prompt           — Prompt admin to upload a file (via ConversationHandler)
flm:keys:menu                     — Key Manager hub
flm:keys:list:TYPE:STATUS:PAGE    — Paginated key list
flm:keys:view:ID                  — Key detail
flm:keys:use:ID                   — Mark key as used manually
flm:keys:recycle:ID               — Recycle (reset) a used key
flm:keys:reserve:ID               — Reserve key for manual delivery
flm:keys:del:ID                   — Delete key
flm:keys:del_bulk:confirm:TYPE:STATUS — Confirm bulk delete
flm:keys:del_bulk:go:TYPE:STATUS  — Execute bulk delete
flm:keys:export:TYPE:STATUS       — Export keys as text file
flm:keys:import_prompt            — Prompt admin to paste keys (ConversationHandler)
flm:keys:generate_prompt          — Prompt admin to generate keys (ConversationHandler)
flm:stats                         — Combined statistics
flm:settings                      — Settings menu
flm:settings:status:VAL           — Set enabled/maintenance/disabled
flm:settings:toggle:KEY           — Flip a bool config key
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import List, Optional

from telegram import (
    Document, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from database import get_db_session
from database.models import ManagedFile, ManagedKey, ManagedKeyDelivery
from services import file_license_service as fls
from utils.helpers import is_admin
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8

# ConversationHandler states
_FILE_UPLOAD_NAME  = 20
_FILE_UPLOAD_FILE  = 21
_KEY_IMPORT_TYPE   = 30
_KEY_IMPORT_TEXT   = 31
_KEY_GEN_TYPE      = 40
_KEY_GEN_COUNT     = 41
_KEY_GEN_PREFIX    = 42

_KEY_TYPES = [
    ("product_key",     "🔑 Product Key"),
    ("license_key",     "🪪 License Key"),
    ("account",         "👤 Account"),
    ("serial_number",   "🔢 Serial Number"),
    ("gift_code",       "🎁 Gift Code"),
    ("activation_code", "✅ Activation Code"),
]

_KEY_TYPE_LABELS = {k: v for k, v in _KEY_TYPES}

_STATUS_EMOJI = {
    "unused":   "🟢",
    "reserved": "🟡",
    "used":     "🔴",
    "expired":  "⌛",
    "recycled": "♻️",
}

_FILE_TYPE_EMOJI = {
    "pdf":      "📄",
    "zip":      "🗜",
    "rar":      "🗜",
    "txt":      "📝",
    "docx":     "📝",
    "image":    "🖼",
    "video":    "🎬",
    "software": "💿",
    "other":    "📁",
}

_SETTINGS_BOOL_KEYS = [
    ("flm_auto_delete_expired",    "🗑 Auto Delete Expired Files"),
    ("flm_auto_archive_used_keys", "♻️ Auto Archive Used Keys"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await q.message.reply_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
    else:
        msg = getattr(update, "message", None)
        if msg:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main menu
# ─────────────────────────────────────────────────────────────────────────────

async def flm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    status     = cfg.get("file_license_manager_status", "enabled")
    status_emoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")
    fstats = fls.get_file_stats()
    kstats = fls.get_key_stats()

    text = (
        f"📂 <b>File & License Key Manager</b>\n\n"
        f"{status_emoji} Status: <b>{status.title()}</b>\n\n"
        f"<b>📁 Files</b>\n"
        f"• Total: <b>{fstats['total']}</b>  |  Active: <b>{fstats['active']}</b>  "
        f"|  Archived: <b>{fstats['archived']}</b>\n"
        f"• Downloads: <b>{fstats['total_downloads']}</b>\n\n"
        f"<b>🔑 Keys</b>\n"
        f"• Total: <b>{kstats['total']}</b>\n"
        f"• 🟢 Unused: <b>{kstats['unused']}</b>  "
        f"🟡 Reserved: <b>{kstats['reserved']}</b>  "
        f"🔴 Used: <b>{kstats['used']}</b>\n"
        f"• ⌛ Expired: <b>{kstats['expired']}</b>  "
        f"♻️ Recycled: <b>{kstats['recycled']}</b>\n"
        f"• Deliveries: <b>{kstats['total_deliveries']}</b>\n"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 File Manager", callback_data="flm:files:list:0"),
         InlineKeyboardButton("🔑 Key Manager", callback_data="flm:keys:menu")],
        [InlineKeyboardButton("📊 Statistics",  callback_data="flm:stats"),
         InlineKeyboardButton("⚙️ Settings",    callback_data="flm:settings")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    await _send(update, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# File Manager
# ─────────────────────────────────────────────────────────────────────────────

async def flm_files_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts = q.data.split(":")  # flm:files:list:PAGE
    page = int(parts[3]) if len(parts) > 3 else 0

    with get_db_session() as s:
        base_q  = s.query(ManagedFile).order_by(ManagedFile.created_at.desc())
        total   = base_q.count()
        rows    = base_q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

    if not rows:
        text = "📁 <b>File Manager</b>\n\nNo files uploaded yet."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Upload File", callback_data="flm:files:upload_prompt")],
            [InlineKeyboardButton("🔙 Back", callback_data="flm:menu")],
        ])
        await _send(update, text, kb); return

    lines = [f"📁 <b>File Manager</b>  (page {page+1})\n"]
    btns  = []
    for f in rows:
        emoji  = _FILE_TYPE_EMOJI.get(f.file_type, "📁")
        status = {"active": "🟢", "archived": "🗄", "expired": "⌛"}.get(f.status, "📁")
        label  = f"{status} {emoji} {f.filename[:30]}"
        btns.append([InlineKeyboardButton(label, callback_data=f"flm:files:view:{f.id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"flm:files:list:{page-1}"))
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="flm:menu"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"flm:files:list:{page+1}"))
    if nav:
        btns.append(nav)
    btns.append([
        InlineKeyboardButton("📤 Upload File", callback_data="flm:files:upload_prompt"),
        InlineKeyboardButton("🔙 Back",        callback_data="flm:menu"),
    ])
    await _send(update, "\n".join(lines), InlineKeyboardMarkup(btns))


async def flm_files_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    fid = int(q.data.split(":")[3])
    with get_db_session() as s:
        f = s.query(ManagedFile).filter_by(id=fid).first()
        if not f:
            await q.answer("File not found.", show_alert=True); return

        emoji  = _FILE_TYPE_EMOJI.get(f.file_type, "📁")
        status = {"active": "🟢 Active", "archived": "🗄 Archived", "expired": "⌛ Expired"}.get(f.status, f.status)
        ts     = f.created_at.strftime("%Y-%m-%d %H:%M UTC")
        size   = f"{f.file_size // 1024} KB" if f.file_size else "Unknown"
        max_dl = str(f.max_downloads) if f.max_downloads else "Unlimited"
        linked = f"Product #{f.product_id}" if f.product_id else "Not linked"

        text = (
            f"{emoji} <b>{f.filename}</b>\n\n"
            f"📊 Status: {status}\n"
            f"🗂 Type: <code>{f.file_type.upper()}</code>\n"
            f"📦 Size: {size}\n"
            f"⬇️ Downloads: <b>{f.download_count}</b> / {max_dl}\n"
            f"🔗 Linked: {linked}\n"
            f"🕐 Created: {ts}\n"
        )
        if f.description:
            text += f"\n📝 {f.description}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗄 Archive", callback_data=f"flm:files:arch:{fid}"),
         InlineKeyboardButton("🗑 Delete",  callback_data=f"flm:files:del:{fid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="flm:files:list:0")],
    ])
    await _send(update, text, kb)


async def flm_files_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[3])
    ok = fls.archive_file(fid)
    if ok:
        log_admin_action(update.effective_user.id, "file_manager.archive", target_type="managed_file", target_id=str(fid))
        await q.answer("🗄 File archived.")
    else:
        await q.answer("File not found.", show_alert=True)
    await flm_files_list(with_data(update, "flm:files:list:0"), context)


async def flm_files_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[3])
    ok = fls.delete_file(fid)
    if ok:
        log_admin_action(update.effective_user.id, "file_manager.delete", target_type="managed_file", target_id=str(fid))
        await q.answer("🗑 File deleted.")
    else:
        await q.answer("File not found.", show_alert=True)
    await flm_files_list(with_data(update, "flm:files:list:0"), context)


# ── File upload conversation ──────────────────────────────────────────────────

async def flm_files_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return ConversationHandler.END

    await q.message.reply_text(
        "📤 <b>Upload File</b>\n\n"
        "Send me the filename / description (as plain text), then I'll ask for the file.\n\n"
        "Format: <code>filename | description (optional)</code>\n\n"
        "Example: <code>Product Manual.pdf | User guide for Premium plan</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _FILE_UPLOAD_NAME


async def flm_files_upload_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    text = update.message.text.strip()
    parts = text.split("|", 1)
    context.user_data["flm_file_name"] = parts[0].strip()
    context.user_data["flm_file_desc"] = parts[1].strip() if len(parts) > 1 else ""
    await update.message.reply_text(
        "📎 Now send the file (document, image, or video).\n\nSend /cancel to abort.",
    )
    return _FILE_UPLOAD_FILE


async def flm_files_upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END

    msg = update.message
    doc = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video or msg.audio
    if not doc:
        await msg.reply_text("❌ Please send a file (document, photo, or video).")
        return _FILE_UPLOAD_FILE

    file_id = doc.file_id
    filename = context.user_data.get("flm_file_name", "Unnamed File")
    desc     = context.user_data.get("flm_file_desc", "")

    # Determine file type
    if msg.document:
        mime = getattr(msg.document, "mime_type", "") or ""
        if "pdf"   in mime: ft = "pdf"
        elif "zip" in mime: ft = "zip"
        elif "rar" in mime: ft = "rar"
        elif "word" in mime or "docx" in mime: ft = "docx"
        elif "text" in mime: ft = "txt"
        elif "image" in mime: ft = "image"
        elif "video" in mime: ft = "video"
        else: ft = "software"
        size = getattr(msg.document, "file_size", None)
    elif msg.photo:
        ft, size = "image", None
    elif msg.video:
        ft = "video"
        size = getattr(msg.video, "file_size", None)
    else:
        ft, size = "other", None

    mf = fls.create_file(
        filename=filename,
        file_type=ft,
        telegram_file_id=file_id,
        file_size=size,
        description=desc or None,
        created_by=update.effective_user.id,
    )
    if mf:
        log_admin_action(update.effective_user.id, "file_manager.upload",
                         target_type="managed_file", target_id=str(mf.id),
                         details=f"Uploaded: {filename}")
        await msg.reply_text(
            f"✅ <b>File Uploaded</b>\n\n"
            f"📁 {filename}\n"
            f"🗂 Type: {ft.upper()}\n"
            f"🆔 ID: #{mf.id}",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text("❌ Failed to save file. Please try again.")

    context.user_data.pop("flm_file_name", None)
    context.user_data.pop("flm_file_desc", None)
    return ConversationHandler.END


async def flm_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("flm_file_name", None)
    context.user_data.pop("flm_file_desc", None)
    await update.message.reply_text("❌ Upload cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Key Manager
# ─────────────────────────────────────────────────────────────────────────────

async def flm_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    kstats = fls.get_key_stats()
    text = (
        "🔑 <b>License Key Manager</b>\n\n"
        f"• Total: <b>{kstats['total']}</b>\n"
        f"• 🟢 Unused: <b>{kstats['unused']}</b>\n"
        f"• 🟡 Reserved: <b>{kstats['reserved']}</b>\n"
        f"• 🔴 Used: <b>{kstats['used']}</b>\n"
        f"• ⌛ Expired: <b>{kstats['expired']}</b>\n"
        f"• ♻️ Recycled: <b>{kstats['recycled']}</b>\n"
        f"• Deliveries: <b>{kstats['total_deliveries']}</b>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Unused",   callback_data="flm:keys:list:all:unused:0"),
         InlineKeyboardButton("📋 View Used",     callback_data="flm:keys:list:all:used:0")],
        [InlineKeyboardButton("📋 View Reserved", callback_data="flm:keys:list:all:reserved:0"),
         InlineKeyboardButton("📋 View Expired",  callback_data="flm:keys:list:all:expired:0")],
        [InlineKeyboardButton("📤 Import Keys",   callback_data="flm:keys:import_prompt"),
         InlineKeyboardButton("⚙️ Generate Keys", callback_data="flm:keys:generate_prompt")],
        [InlineKeyboardButton("📥 Export Keys",   callback_data="flm:keys:export_menu"),
         InlineKeyboardButton("🗑 Bulk Delete",   callback_data="flm:keys:del_bulk:menu")],
        [InlineKeyboardButton("🔎 Browse by Type", callback_data="flm:keys:type_menu")],
        [InlineKeyboardButton("🔙 Back", callback_data="flm:menu")],
    ])
    await _send(update, text, kb)


async def flm_keys_type_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    text = "🔑 <b>Browse Keys by Type</b>\n\nSelect a key type:"
    btns = []
    for kt, label in _KEY_TYPES:
        btns.append([InlineKeyboardButton(label, callback_data=f"flm:keys:list:{kt}:all:0")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="flm:keys:menu")])
    await _send(update, text, InlineKeyboardMarkup(btns))


async def flm_keys_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts = q.data.split(":")  # flm:keys:list:TYPE:STATUS:PAGE
    kt     = parts[3] if len(parts) > 3 else "all"
    status = parts[4] if len(parts) > 4 else "all"
    page   = int(parts[5]) if len(parts) > 5 else 0

    with get_db_session() as s:
        bq = s.query(ManagedKey).order_by(ManagedKey.created_at.desc())
        if kt != "all":
            bq = bq.filter(ManagedKey.key_type == kt)
        if status != "all":
            bq = bq.filter(ManagedKey.status == status)
        total = bq.count()
        rows  = bq.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

    type_label  = _KEY_TYPE_LABELS.get(kt, kt.replace("_", " ").title()) if kt != "all" else "All Types"
    status_label = status.title() if status != "all" else "All"
    heading = f"🔑 <b>Keys — {type_label} / {status_label}</b>  (page {page+1})\n"

    if not rows:
        text = heading + "\nNo keys found."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="flm:keys:menu")]])
        await _send(update, text, kb); return

    btns = []
    for mk in rows:
        st_emoji = _STATUS_EMOJI.get(mk.status, "•")
        preview  = mk.key_value[:20] + ("…" if len(mk.key_value) > 20 else "")
        label    = f"{st_emoji} {preview}"
        btns.append([InlineKeyboardButton(label, callback_data=f"flm:keys:view:{mk.id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"flm:keys:list:{kt}:{status}:{page-1}"))
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="flm:keys:menu"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"flm:keys:list:{kt}:{status}:{page+1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="flm:keys:menu")])
    await _send(update, heading, InlineKeyboardMarkup(btns))


async def flm_keys_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    kid = int(q.data.split(":")[3])
    with get_db_session() as s:
        mk = s.query(ManagedKey).filter_by(id=kid).first()
        if not mk:
            await q.answer("Key not found.", show_alert=True); return

        st_emoji = _STATUS_EMOJI.get(mk.status, "•")
        ts       = mk.created_at.strftime("%Y-%m-%d %H:%M UTC")
        linked   = f"Product #{mk.product_id}" if mk.product_id else "Not linked"
        used_info = ""
        if mk.used_at:
            used_info = f"\n👤 Used by User #{mk.used_by_user_id} at {mk.used_at.strftime('%Y-%m-%d %H:%M UTC')}"

        text = (
            f"🔑 <b>Key Detail</b>\n\n"
            f"Type: <b>{_KEY_TYPE_LABELS.get(mk.key_type, mk.key_type)}</b>\n"
            f"Value: <code>{mk.key_value}</code>\n"
            f"{st_emoji} Status: <b>{mk.status.title()}</b>\n"
            f"🔗 Linked: {linked}\n"
            f"🕐 Created: {ts}"
            f"{used_info}\n"
        )
        if mk.notes:
            text += f"\n📝 Notes: {mk.notes}\n"

    btns = [[InlineKeyboardButton("🔙 Back", callback_data="flm:keys:menu")]]
    if mk.status == "unused":
        btns.insert(0, [
            InlineKeyboardButton("🟡 Reserve",  callback_data=f"flm:keys:reserve:{kid}"),
            InlineKeyboardButton("🗑 Delete",   callback_data=f"flm:keys:del:{kid}"),
        ])
    elif mk.status in ("used", "reserved"):
        btns.insert(0, [
            InlineKeyboardButton("♻️ Recycle", callback_data=f"flm:keys:recycle:{kid}"),
            InlineKeyboardButton("🗑 Delete",  callback_data=f"flm:keys:del:{kid}"),
        ])
    else:
        btns.insert(0, [InlineKeyboardButton("🗑 Delete", callback_data=f"flm:keys:del:{kid}")])

    await _send(update, text, InlineKeyboardMarkup(btns))


async def flm_keys_reserve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    kid = int(q.data.split(":")[3])
    ok = fls.reserve_key(kid, update.effective_user.id)
    if ok:
        log_admin_action(update.effective_user.id, "key_manager.reserve", target_id=str(kid))
        await q.answer("🟡 Key reserved.")
    else:
        await q.answer("Key not available for reservation.", show_alert=True)
    await flm_keys_view(with_data(update, f"flm:keys:view:{kid}"), context)


async def flm_keys_recycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    kid = int(q.data.split(":")[3])
    ok = fls.recycle_key(kid)
    if ok:
        log_admin_action(update.effective_user.id, "key_manager.recycle", target_id=str(kid))
        await q.answer("♻️ Key recycled (reset to unused).")
    else:
        await q.answer("Failed to recycle key.", show_alert=True)
    await flm_keys_view(with_data(update, f"flm:keys:view:{kid}"), context)


async def flm_keys_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    kid = int(q.data.split(":")[3])
    ok = fls.delete_key(kid)
    if ok:
        log_admin_action(update.effective_user.id, "key_manager.delete", target_id=str(kid))
        await q.answer("🗑 Key deleted.")
    else:
        await q.answer("Key not found.", show_alert=True)
    await flm_keys_menu(with_data(update, "flm:keys:menu"), context)


# ── Bulk delete ───────────────────────────────────────────────────────────────

async def flm_keys_del_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    text = "🗑 <b>Bulk Delete Keys</b>\n\nSelect scope:"
    btns = [
        [InlineKeyboardButton("🔴 Delete All USED",     callback_data="flm:keys:del_bulk:confirm:all:used")],
        [InlineKeyboardButton("⌛ Delete All EXPIRED",  callback_data="flm:keys:del_bulk:confirm:all:expired")],
        [InlineKeyboardButton("♻️ Delete All RECYCLED", callback_data="flm:keys:del_bulk:confirm:all:recycled")],
        [InlineKeyboardButton("🗑 Delete ALL Keys",     callback_data="flm:keys:del_bulk:confirm:all:all")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="flm:keys:menu")],
    ]
    await _send(update, text, InlineKeyboardMarkup(btns))


async def flm_keys_del_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts = q.data.split(":")  # flm:keys:del_bulk:confirm:TYPE:STATUS
    kt     = parts[4] if len(parts) > 4 else "all"
    status = parts[5] if len(parts) > 5 else "all"

    text = (
        f"⚠️ <b>Confirm Bulk Delete</b>\n\n"
        f"Type: <b>{kt}</b>   Status: <b>{status}</b>\n\n"
        f"This will permanently delete all matching keys.\n"
        f"This action cannot be undone!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Delete", callback_data=f"flm:keys:del_bulk:go:{kt}:{status}")],
        [InlineKeyboardButton("🔙 Cancel",         callback_data="flm:keys:menu")],
    ])
    await _send(update, text, kb)


async def flm_keys_del_bulk_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts  = q.data.split(":")  # flm:keys:del_bulk:go:TYPE:STATUS
    kt     = parts[4] if len(parts) > 4 else "all"
    status = parts[5] if len(parts) > 5 else "all"
    count  = fls.bulk_delete_keys(
        key_type=None if kt == "all" else kt,
        status=None if status == "all" else status,
    )
    log_admin_action(update.effective_user.id, "key_manager.bulk_delete",
                     details=f"Deleted {count} keys (type={kt}, status={status})")
    await q.answer(f"🗑 {count} keys deleted.")
    await flm_keys_menu(with_data(update, "flm:keys:menu"), context)


# ── Export keys ───────────────────────────────────────────────────────────────

async def flm_keys_export_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    text = "📥 <b>Export Keys</b>\n\nSelect scope:"
    btns = [
        [InlineKeyboardButton("🟢 Export Unused",   callback_data="flm:keys:export:all:unused")],
        [InlineKeyboardButton("🔴 Export Used",     callback_data="flm:keys:export:all:used")],
        [InlineKeyboardButton("📋 Export All Keys", callback_data="flm:keys:export:all:all")],
        [InlineKeyboardButton("🔙 Back", callback_data="flm:keys:menu")],
    ]
    await _send(update, text, InlineKeyboardMarkup(btns))


async def flm_keys_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return

    parts  = q.data.split(":")  # flm:keys:export:TYPE:STATUS
    kt     = parts[3] if len(parts) > 3 else "all"
    status = parts[4] if len(parts) > 4 else "all"

    await q.answer("⏳ Exporting…")
    keys = fls.export_keys(
        key_type=None if kt == "all" else kt,
        status=None if status == "all" else status,
    )
    if not keys:
        await q.answer("No keys match that filter.", show_alert=True); return

    content = "\n".join(keys)
    bio = io.BytesIO(content.encode())
    bio.name = f"keys_{kt}_{status}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"

    log_admin_action(update.effective_user.id, "key_manager.export",
                     details=f"Exported {len(keys)} keys (type={kt}, status={status})")
    await q.message.reply_document(
        document=bio,
        caption=f"🔑 Key export: <b>{len(keys)}</b> keys ({kt} / {status})",
        parse_mode="HTML",
    )


# ── Import keys conversation ──────────────────────────────────────────────────

async def flm_keys_import_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return ConversationHandler.END

    text = "📤 <b>Import Keys</b>\n\nStep 1/2: Select the key type:"
    btns = []
    for kt, label in _KEY_TYPES:
        btns.append([InlineKeyboardButton(label, callback_data=f"flm_import_type:{kt}")])
    btns.append([InlineKeyboardButton("🔙 Cancel", callback_data="flm:keys:menu")])
    await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    return _KEY_IMPORT_TYPE


async def flm_keys_import_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kt = q.data.split(":")[1]
    context.user_data["flm_import_key_type"] = kt
    await q.message.reply_text(
        f"📤 <b>Import Keys</b> — {_KEY_TYPE_LABELS.get(kt, kt)}\n\n"
        "Step 2/2: Paste your keys below.\n"
        "One key per line, or comma-separated.\n\n"
        "Duplicates are automatically skipped.\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _KEY_IMPORT_TEXT


async def flm_keys_import_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    kt       = context.user_data.pop("flm_import_key_type", "product_key")
    raw_text = update.message.text.strip()
    added, dupes, errors = fls.bulk_import_keys(kt, raw_text, created_by=update.effective_user.id)
    log_admin_action(update.effective_user.id, "key_manager.bulk_import",
                     details=f"Imported {added} {kt} keys (dupes={dupes}, errors={errors})")
    await update.message.reply_text(
        f"✅ <b>Import Complete</b>\n\n"
        f"• Added: <b>{added}</b>\n"
        f"• Duplicates skipped: <b>{dupes}</b>\n"
        f"• Errors: <b>{errors}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def flm_import_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("flm_import_key_type", None)
    await update.message.reply_text("❌ Import cancelled.")
    return ConversationHandler.END


# ── Generate keys conversation ────────────────────────────────────────────────

async def flm_keys_generate_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return ConversationHandler.END

    text = "⚙️ <b>Generate Keys</b>\n\nStep 1/3: Select key type:"
    btns = []
    for kt, label in _KEY_TYPES:
        btns.append([InlineKeyboardButton(label, callback_data=f"flm_gen_type:{kt}")])
    await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    return _KEY_GEN_TYPE


async def flm_keys_gen_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kt = q.data.split(":")[1]
    context.user_data["flm_gen_key_type"] = kt
    await q.message.reply_text(
        f"⚙️ <b>Generate Keys</b> — {_KEY_TYPE_LABELS.get(kt, kt)}\n\n"
        "Step 2/3: How many keys? (1–500)\n\nSend /cancel to abort.",
    )
    return _KEY_GEN_COUNT


async def flm_keys_gen_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        count = int(update.message.text.strip())
        if not 1 <= count <= 500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a number between 1 and 500.")
        return _KEY_GEN_COUNT
    context.user_data["flm_gen_count"] = count
    await update.message.reply_text(
        "Step 3/3: Enter a key prefix (optional).\n\n"
        "Example: <code>PRO-</code> → <code>PRO-XXXX-XXXX-XXXX</code>\n\n"
        "Send a dash <code>-</code> for no prefix.",
        parse_mode="HTML",
    )
    return _KEY_GEN_PREFIX


async def flm_keys_gen_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    prefix = update.message.text.strip()
    if prefix == "-":
        prefix = ""
    kt    = context.user_data.pop("flm_gen_key_type", "license_key")
    count = context.user_data.pop("flm_gen_count", 10)
    added = fls.generate_keys(kt, count, prefix=prefix, created_by=update.effective_user.id)
    log_admin_action(update.effective_user.id, "key_manager.generate",
                     details=f"Generated {added} {kt} keys (prefix='{prefix}')")
    await update.message.reply_text(
        f"✅ <b>Generated {added} keys</b>\n\n"
        f"Type: {_KEY_TYPE_LABELS.get(kt, kt)}\n"
        f"Prefix: <code>{prefix or '(none)'}</code>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def flm_gen_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("flm_gen_key_type", None)
    context.user_data.pop("flm_gen_count", None)
    await update.message.reply_text("❌ Key generation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

async def flm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    fstats = fls.get_file_stats()
    kstats = fls.get_key_stats()

    text = (
        "📊 <b>File & License Manager Statistics</b>\n\n"
        "<b>📁 Files</b>\n"
        f"• Total files: <b>{fstats['total']}</b>\n"
        f"• Active: <b>{fstats['active']}</b>\n"
        f"• Archived: <b>{fstats['archived']}</b>\n"
        f"• Expired: <b>{fstats['expired']}</b>\n"
        f"• Total downloads: <b>{fstats['total_downloads']}</b>\n\n"
        "<b>🔑 Keys</b>\n"
        f"• Total keys: <b>{kstats['total']}</b>\n"
        f"• 🟢 Unused: <b>{kstats['unused']}</b>\n"
        f"• 🟡 Reserved: <b>{kstats['reserved']}</b>\n"
        f"• 🔴 Used: <b>{kstats['used']}</b>\n"
        f"• ⌛ Expired: <b>{kstats['expired']}</b>\n"
        f"• ♻️ Recycled: <b>{kstats['recycled']}</b>\n"
        f"• Total deliveries: <b>{kstats['total_deliveries']}</b>\n"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="flm:menu")]])
    await _send(update, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

async def flm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    status    = cfg.get("file_license_manager_status", "enabled")
    max_size  = cfg.get_int("flm_max_upload_size_mb", 50)
    allowed   = cfg.get("flm_allowed_types", "pdf,zip,rar,txt,docx,image,video,software")

    lines = [
        "⚙️ <b>File & License Manager Settings</b>\n",
        f"Status: <b>{status.title()}</b>",
        f"Max upload size: <b>{max_size} MB</b>",
        f"Allowed types: <code>{allowed}</code>\n",
    ]
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        lines.append(f"{'✅' if val else '❌'} {label}")

    text = "\n".join(lines)
    btns = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="flm:settings:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="flm:settings:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="flm:settings:status:disabled")],
    ]
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        btns.append([InlineKeyboardButton(
            f"{'✅' if val else '❌'} {label}",
            callback_data=f"flm:settings:toggle:{key}",
        )])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="flm:menu")])
    await _send(update, text, InlineKeyboardMarkup(btns))


async def flm_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # flm:settings:status:VAL
    val = parts[3] if len(parts) > 3 else "enabled"
    cfg.set("file_license_manager_status", val)
    log_admin_action(update.effective_user.id, "file_license_manager.set_status", new_value=val)
    await q.answer(f"Status set to {val}.")
    await flm_settings(with_data(update, "flm:settings"), context)


async def flm_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # flm:settings:toggle:KEY
    key = ":".join(parts[3:])
    new_val = not cfg.get_bool(key, True)
    cfg.set(key, new_val)
    log_admin_action(update.effective_user.id, "file_license_manager.toggle", target_id=key, new_value=str(new_val))
    await q.answer(f"{'✅ Enabled' if new_val else '❌ Disabled'}")
    await flm_settings(with_data(update, "flm:settings"), context)


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def build_flm_file_upload_conv():
    """ConversationHandler for file upload."""
    from config.settings import settings as _s
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(flm_files_upload_prompt, pattern=r"^flm:files:upload_prompt$")],
        states={
            _FILE_UPLOAD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, flm_files_upload_name)],
            _FILE_UPLOAD_FILE: [MessageHandler(
                (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO) & ~filters.COMMAND,
                flm_files_upload_file,
            )],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r"^/cancel$"), flm_upload_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_flm_key_import_conv():
    """ConversationHandler for bulk key import."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(flm_keys_import_prompt, pattern=r"^flm:keys:import_prompt$")],
        states={
            _KEY_IMPORT_TYPE: [CallbackQueryHandler(flm_keys_import_type, pattern=r"^flm_import_type:.+$")],
            _KEY_IMPORT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, flm_keys_import_text)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r"^/cancel$"), flm_import_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_flm_key_gen_conv():
    """ConversationHandler for key generation."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(flm_keys_generate_prompt, pattern=r"^flm:keys:generate_prompt$")],
        states={
            _KEY_GEN_TYPE:   [CallbackQueryHandler(flm_keys_gen_type, pattern=r"^flm_gen_type:.+$")],
            _KEY_GEN_COUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, flm_keys_gen_count)],
            _KEY_GEN_PREFIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, flm_keys_gen_prefix)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r"^/cancel$"), flm_gen_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def register_handlers(application) -> None:
    """Register all flm:* callback handlers and conversations."""
    # Conversations (must be registered before plain CallbackQueryHandlers)
    application.add_handler(build_flm_file_upload_conv())
    application.add_handler(build_flm_key_import_conv())
    application.add_handler(build_flm_key_gen_conv())

    # Plain callbacks
    application.add_handler(CallbackQueryHandler(flm_menu,                pattern=r"^flm:menu$"))
    application.add_handler(CallbackQueryHandler(flm_files_list,          pattern=r"^flm:files:list:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_files_view,          pattern=r"^flm:files:view:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_files_archive,       pattern=r"^flm:files:arch:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_files_delete,        pattern=r"^flm:files:del:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_menu,           pattern=r"^flm:keys:menu$"))
    application.add_handler(CallbackQueryHandler(flm_keys_type_menu,      pattern=r"^flm:keys:type_menu$"))
    application.add_handler(CallbackQueryHandler(flm_keys_list,           pattern=r"^flm:keys:list:.+:.+:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_view,           pattern=r"^flm:keys:view:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_reserve,        pattern=r"^flm:keys:reserve:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_recycle,        pattern=r"^flm:keys:recycle:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_delete,         pattern=r"^flm:keys:del:\d+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_del_bulk_menu,  pattern=r"^flm:keys:del_bulk:menu$"))
    application.add_handler(CallbackQueryHandler(flm_keys_del_bulk_confirm, pattern=r"^flm:keys:del_bulk:confirm:.+:.+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_del_bulk_go,    pattern=r"^flm:keys:del_bulk:go:.+:.+$"))
    application.add_handler(CallbackQueryHandler(flm_keys_export_menu,    pattern=r"^flm:keys:export_menu$"))
    application.add_handler(CallbackQueryHandler(flm_keys_export,         pattern=r"^flm:keys:export:.+:.+$"))
    application.add_handler(CallbackQueryHandler(flm_stats,               pattern=r"^flm:stats$"))
    application.add_handler(CallbackQueryHandler(flm_settings,            pattern=r"^flm:settings$"))
    application.add_handler(CallbackQueryHandler(flm_settings_status,     pattern=r"^flm:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(flm_settings_toggle,     pattern=r"^flm:settings:toggle:.+$"))
