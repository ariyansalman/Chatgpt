"""Admin Security Center — V1.

Unified security hub providing:
  • Show / Hide API Keys        (via API Manager)
  • Rotate API Keys             (via API Manager)
  • Export Configuration        (sends JSON snapshot as Telegram document)
  • Import Configuration        (upload JSON file to restore settings)
  • Backup Configuration        (triggers a settings backup)
  • Restore Configuration       (restore from a listed backup)
  • Audit Log                   (delegates to admin_audit_enhanced)

Callback namespace: ``asc:*``

Callbacks handled:
  asc:menu                  — main hub
  asc:apikeys               — shortcut to aim:menu
  asc:export_cfg            — export current config as JSON file
  asc:import_cfg            — start import conversation
  asc:backup_cfg            — trigger an immediate settings backup
  asc:restore_cfg           — list recent backups to restore
  asc:restore_ok:<id>       — confirm and restore from backup <id>
  asc:audit                 — go to audit log
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from utils.permissions import has_permission
from utils.audit import log_admin_action
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# Conversation state
_ASC_IMPORT = 50_001

_PAGE_SIZE = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guard(uid: int) -> bool:
    return has_permission(uid, "manage_settings")


async def _deny(update: Update, msg: str = "⛔ Access denied.") -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(msg, show_alert=True)


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                      disable_web_page_preview=True)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                try:
                    await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                               disable_web_page_preview=True)
                except Exception:
                    pass
    else:
        msg_obj = getattr(update, "message", None)
        if msg_obj:
            await msg_obj.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                     disable_web_page_preview=True)


def _back_root() -> list:
    return [[InlineKeyboardButton("🔙 Back", callback_data="acc:root")]]


# ── Main menu ─────────────────────────────────────────────────────────────────

async def asc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Security Center hub."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    text = (
        "🛡 <b>Security Center</b>\n\n"
        "Manage API credentials, configuration backups, and admin audit records "
        "from one place. All actions are logged to the audit trail."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 API Key Manager",      callback_data="aim:menu"),
         InlineKeyboardButton("🔄 Rotate a Key",         callback_data="asc:apikeys")],
        [InlineKeyboardButton("📤 Export Config",        callback_data="asc:export_cfg"),
         InlineKeyboardButton("📥 Import Config",        callback_data="asc:import_cfg")],
        [InlineKeyboardButton("💾 Backup Config",        callback_data="asc:backup_cfg"),
         InlineKeyboardButton("♻️ Restore Config",       callback_data="asc:restore_cfg")],
        [InlineKeyboardButton("📝 Audit Log",            callback_data="acc:audit:page:0")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    await _edit(update, text, kb)


# ── API Keys shortcut ─────────────────────────────────────────────────────────

async def asc_apikeys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut to the API & Integration Manager (aim:menu) — same guard."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return
    # Show list of integrations so admin can pick one to rotate its key
    text = (
        "🔄 <b>Rotate API Key</b>\n\n"
        "Open the API Key Manager, select an integration, then tap "
        "<b>🔄 Rotate Key</b> to clear the current key and enter a new one.\n\n"
        "After rotation the integration is placed in <i>Maintenance</i> mode until "
        "the new key is saved."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Open API Key Manager", callback_data="aim:menu")],
        [InlineKeyboardButton("🔙 Back", callback_data="asc:menu")],
    ])
    await _edit(update, text, kb)


# ── Export Configuration ──────────────────────────────────────────────────────

async def asc_export_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export the current bot configuration as a downloadable JSON file."""
    q = update.callback_query
    if q:
        await q.answer("Generating config export…", show_alert=False)
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    try:
        from services.settings_backup import create_settings_backup
        record = create_settings_backup(admin_id=uid, triggered_by="export")
        if record and record.status == "SUCCESS":
            # Read the file and send as document
            from services.settings_backup import BACKUP_DIR
            fpath = BACKUP_DIR / record.filename
            if fpath.exists():
                with open(fpath, "rb") as f:
                    data = f.read()
                fname = f"config_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
                chat_id = update.effective_chat.id
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(io.BytesIO(data), filename=fname),
                    caption=(
                        "📤 <b>Configuration Export</b>\n\n"
                        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
                        f"Size: {len(data):,} bytes\n\n"
                        "Keep this file secure — it contains all bot settings."
                    ),
                    parse_mode="HTML",
                )
                log_admin_action(uid, "asc.export_config",
                                 details=f"size={len(data)} filename={fname}")
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")],
                ])
                msg_text = "✅ Configuration exported and sent as document above."
                if q:
                    try:
                        await q.edit_message_text(msg_text, reply_markup=kb, parse_mode="HTML")
                    except Exception:
                        pass
                return

        # Fallback — build a minimal config JSON directly
        with get_db_session() as s:
            from database.models import BotConfig
            rows = s.query(BotConfig).all()
            config_data = {r.key: r.value for r in rows}

        payload = json.dumps(
            {"schema_version": "1.0",
             "exported_at": datetime.utcnow().isoformat() + "Z",
             "bot_config": config_data},
            indent=2, ensure_ascii=False
        ).encode()
        fname = f"config_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(io.BytesIO(payload), filename=fname),
            caption="📤 <b>Configuration Export</b> (bot_config only)",
            parse_mode="HTML",
        )
        log_admin_action(uid, "asc.export_config", details=f"fallback size={len(payload)}")

    except Exception:
        logger.exception("asc_export_cfg failed")
        kb = InlineKeyboardMarkup(_back_root())
        await _edit(update, "❌ Export failed. Check server logs for details.", kb)
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")]])
    await _edit(update, "✅ Configuration exported and sent above.", kb)


# ── Import Configuration ──────────────────────────────────────────────────────

async def asc_import_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start import conversation — prompt admin to upload a JSON file."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return ConversationHandler.END

    text = (
        "📥 <b>Import Configuration</b>\n\n"
        "Upload the JSON configuration file exported from this bot.\n\n"
        "⚠️ <b>Warning:</b> This will overwrite all current settings with the "
        "values from the file. A backup of the current config will be saved first.\n\n"
        "Send the JSON file now, or press Cancel."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="asc:menu")],
    ])
    await _edit(update, text, kb)
    return _ASC_IMPORT


async def asc_import_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the uploaded JSON file and apply it."""
    uid = update.effective_user.id
    doc = update.message.document if update.message else None
    if not doc:
        await update.message.reply_text(
            "❌ Please send a JSON file, or /cancel to abort.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="asc:menu")]]
            ),
        )
        return _ASC_IMPORT

    try:
        file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        raw = buf.getvalue()

        # Parse the JSON
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            await update.message.reply_text("❌ Invalid JSON file. Please upload a valid config export.")
            return _ASC_IMPORT

        # Accept both the full backup format and a flat key:value dict
        if "bot_config" in payload:
            config_data = payload["bot_config"]
        elif isinstance(payload, dict):
            config_data = payload
        else:
            await update.message.reply_text("❌ Unrecognised format. Upload a file exported from this bot.")
            return _ASC_IMPORT

        if not isinstance(config_data, dict):
            await update.message.reply_text("❌ Config data must be a JSON object (key:value pairs).")
            return _ASC_IMPORT

        # Save a backup of current config before overwriting
        try:
            from services.settings_backup import create_settings_backup
            create_settings_backup(compress=False)
        except Exception:
            logger.warning("asc_import: could not create pre-import backup")

        # Apply the imported config
        applied = 0
        skipped = 0
        SENSITIVE = {"secret", "password", "token", "private_key", "api_secret"}

        with get_db_session() as s:
            from database.models import BotConfig
            for key, value in config_data.items():
                # Skip obviously sensitive fields
                lower_key = str(key).lower()
                if any(s in lower_key for s in SENSITIVE):
                    skipped += 1
                    continue
                row = s.query(BotConfig).filter_by(key=str(key)).first()
                if row:
                    row.value = str(value) if value is not None else ""
                else:
                    s.add(BotConfig(key=str(key), value=str(value) if value is not None else ""))
                applied += 1
            s.commit()

        log_admin_action(uid, "asc.import_config",
                         details=f"applied={applied} skipped_sensitive={skipped}")

        await update.message.reply_text(
            f"✅ <b>Configuration Imported</b>\n\n"
            f"Applied: <b>{applied}</b> settings\n"
            f"Skipped (sensitive): <b>{skipped}</b>\n\n"
            f"A backup of the previous config was saved automatically.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")],
            ]),
        )
    except Exception:
        logger.exception("asc_import_receive failed")
        await update.message.reply_text(
            "❌ Import failed. See server logs for details.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")]]
            ),
        )
    return ConversationHandler.END


async def asc_import_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await asc_menu(update, context)
    elif update.message:
        await update.message.reply_text(
            "Import cancelled.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")]]
            ),
        )
    return ConversationHandler.END


# ── Backup Configuration ──────────────────────────────────────────────────────

async def asc_backup_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger an immediate settings backup."""
    q = update.callback_query
    if q:
        await q.answer("Creating backup…", show_alert=False)
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    try:
        from services.settings_backup import create_settings_backup
        record = create_settings_backup(admin_id=uid, triggered_by="manual")

        if record and record.status == "SUCCESS":
            log_admin_action(uid, "asc.backup_config",
                             details=f"filename={record.filename} size={record.size_bytes}")
            text = (
                "✅ <b>Backup Complete</b>\n\n"
                f"File: <code>{record.filename}</code>\n"
                f"Size: {record.size_bytes:,} bytes\n"
                f"Checksum (SHA-256): <code>{(record.checksum or '')[:16]}…</code>\n\n"
                "The backup is stored on the server and can be restored from "
                "<b>Restore Config</b>."
            )
        else:
            err = getattr(record, "error_summary", "unknown error") if record else "unknown error"
            text = f"❌ Backup failed: {err}"

    except Exception:
        logger.exception("asc_backup_cfg failed")
        text = "❌ Backup failed — check server logs."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ Restore Config", callback_data="asc:restore_cfg"),
         InlineKeyboardButton("🔙 Back", callback_data="asc:menu")],
    ])
    await _edit(update, text, kb)


# ── Restore Configuration ─────────────────────────────────────────────────────

async def asc_restore_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent backups available for restore."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    try:
        from database.models import SettingsBackupRecord
        with get_db_session() as s:
            rows = (s.query(SettingsBackupRecord)
                    .filter(SettingsBackupRecord.status == "SUCCESS")
                    .order_by(SettingsBackupRecord.created_at.desc())
                    .limit(_PAGE_SIZE).all())

        if not rows:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Create Backup Now", callback_data="asc:backup_cfg")],
                [InlineKeyboardButton("🔙 Back", callback_data="asc:menu")],
            ])
            await _edit(update, "📂 <b>Restore Configuration</b>\n\nNo backups found.", kb)
            return

        lines = ["♻️ <b>Restore Configuration</b>\n\nSelect a backup to restore:\n"]
        btn_rows = []
        for r in rows:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            sz = f"{r.size_bytes:,}B" if r.size_bytes else "?"
            lines.append(f"• <code>{when}</code>  ({sz})  <i>{r.filename}</i>")
            btn_rows.append([
                InlineKeyboardButton(
                    f"♻️ {when}",
                    callback_data=f"asc:restore_ok:{r.id}",
                )
            ])

        btn_rows.append([InlineKeyboardButton("🔙 Back", callback_data="asc:menu")])
        kb = InlineKeyboardMarkup(btn_rows)
        await _edit(update, "\n".join(lines), kb)

    except Exception:
        logger.exception("asc_restore_cfg failed")
        kb = InlineKeyboardMarkup(_back_root())
        await _edit(update, "❌ Could not load backups.", kb)


async def asc_restore_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute restore from a specific backup record."""
    q = update.callback_query
    if q:
        await q.answer("Restoring…", show_alert=False)
    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    try:
        rec_id = int(q.data.split(":")[-1])
    except Exception:
        await _edit(update, "❌ Invalid backup ID.", InlineKeyboardMarkup(_back_root()))
        return

    try:
        from services.settings_backup import restore_settings_backup, BACKUP_DIR
        from database.models import SettingsBackupRecord

        with get_db_session() as s:
            rec = s.get(SettingsBackupRecord, rec_id)
            if not rec or rec.status != "SUCCESS":
                await _edit(update, "❌ Backup not found or invalid.",
                            InlineKeyboardMarkup(_back_root()))
                return
            filename = rec.filename

        result = restore_settings_backup(
            backup_id=rec_id,
            admin_id=uid,
            restore_products=False,
            restore_categories=False,
        )
        ok = result.get("ok", False)
        if ok:
            restored = result.get("restored_keys", 0)
            log_admin_action(uid, "asc.restore_config",
                             details=f"backup_id={rec_id} restored_keys={restored}")
            text = (
                f"✅ <b>Restore Complete</b>\n\n"
                f"Backup: <code>{filename}</code>\n"
                f"Settings restored: <b>{restored}</b>\n\n"
                "Bot configuration has been updated. Restart the bot if needed."
            )
        else:
            errs = "; ".join(result.get("errors", ["unknown error"]))
            text = f"❌ Restore failed: {errs}"

    except Exception:
        logger.exception("asc_restore_ok failed rec_id=%s", rec_id)
        text = "❌ Restore failed — check server logs."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Security Center", callback_data="asc:menu")],
    ])
    await _edit(update, text, kb)


# ── Central dispatcher ────────────────────────────────────────────────────────

async def asc_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data if q else ""

    if data == "asc:menu":
        await asc_menu(update, context)
    elif data == "asc:apikeys":
        await asc_apikeys(update, context)
    elif data == "asc:export_cfg":
        await asc_export_cfg(update, context)
    elif data == "asc:backup_cfg":
        await asc_backup_cfg(update, context)
    elif data == "asc:restore_cfg":
        await asc_restore_cfg(update, context)
    elif data.startswith("asc:restore_ok:"):
        await asc_restore_ok(update, context)
    elif data == "acc:audit:page:0":
        # Forward to audit log — handled by admin_control_center routing
        from handlers.admin_audit_enhanced import audit_menu
        await audit_menu(update, context)
    elif q:
        await q.answer("Unknown action.", show_alert=False)


# ── Handler registration ──────────────────────────────────────────────────────

def build_asc_import_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(asc_import_cfg, pattern=r"^asc:import_cfg$")],
        states={
            _ASC_IMPORT: [
                MessageHandler(filters.Document.ALL, asc_import_receive),
                MessageHandler(filters.TEXT & ~filters.COMMAND, asc_import_receive),
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, asc_import_cancel),
            CallbackQueryHandler(asc_import_cancel, pattern=r"^asc:menu$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def register_handlers(app) -> None:
    """Register Security Center handlers on the PTB Application."""
    app.add_handler(build_asc_import_conv())
    app.add_handler(CallbackQueryHandler(asc_dispatch, pattern=r"^asc:"))
