"""Backup Manager admin panel — V34.

Callback namespace: acc:bak:*

Provides:
  • pg_dump backups (existing, unchanged)
  • Settings Backup & Restore (JSON-based: products, categories, bot_config,
    payment gateways, feature settings)
  • Manual / Auto / Scheduled backups
  • Backup History (paginated)
  • Download Backup (send as Telegram document)
  • Import Backup (via ConversationHandler: acc:bak:import -> document upload)
  • Delete Backup (with confirmation)
  • Backup Verification (SHA-256 checksum)
  • Backup Manager Settings (Enable / Maintenance / Disable, auto backup,
    interval, max count, compression, restore confirmation)
"""
from __future__ import annotations

import io
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
from database.models import BackupRecord
from services import backup as pg_backup
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.permissions import has_permission
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send

logger = logging.getLogger(__name__)

# ── Conversation state for import ─────────────────────────────────────────
BAK_IMPORT_FILE = 0

# ── Status helpers ────────────────────────────────────────────────────────
_STATUS_EMOJI = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}


def _mgr_status() -> str:
    return cfg.get("backup_manager_status", "enabled")


def _is_active() -> bool:
    return _mgr_status() in ("enabled", "maintenance")


def _guard(uid: int) -> bool:
    return has_permission(uid, "manage_settings")


def _fmt_size(n) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ──────────────────────────────────────────────────────────────────────────
# Root / pg_dump section (existing + extended)
# ──────────────────────────────────────────────────────────────────────────

@require_admin
async def backups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       page: int = 0):
    """Main Backup Manager menu (acc:sec:backups → backups_menu)."""
    mgr_status = _mgr_status()
    status_emoji = _STATUS_EMOJI.get(mgr_status, "⚪")

    # pg_dump stats
    with get_db_session() as s:
        pg_rows = (s.query(BackupRecord)
                   .order_by(BackupRecord.created_at.desc()).limit(100).all())
        last_ok = (s.query(BackupRecord)
                   .filter(BackupRecord.status == "SUCCESS")
                   .order_by(BackupRecord.created_at.desc()).first())
        last_fail = (s.query(BackupRecord)
                     .filter(BackupRecord.status == "FAILED")
                     .order_by(BackupRecord.created_at.desc()).first())

    # Settings backup stats
    from database.models import SettingsBackupRecord
    with get_db_session() as s:
        sbk_total = s.query(SettingsBackupRecord).count()
        sbk_last = (s.query(SettingsBackupRecord)
                    .filter(SettingsBackupRecord.status == "SUCCESS")
                    .order_by(SettingsBackupRecord.created_at.desc()).first())

    pg_enabled = cfg.get_bool("backup_enabled", False)
    auto_settings = cfg.get_bool("backup_auto_settings_enabled", False)
    max_count = cfg.get_int("backup_max_count", 30)
    compress = cfg.get_bool("backup_compression", True)
    restore_confirm = cfg.get_bool("backup_restore_confirm", True)

    lines = [
        f"💾 <b>BACKUP MANAGER</b>",
        f"Status: {status_emoji} {mgr_status.title()}",
        "",
        "<b>📦 DB Dump (pg_dump):</b>",
        f"  Scheduled: {'🟢 ON' if pg_enabled else '⚪ OFF'}",
        f"  Last SUCCESS: {last_ok.created_at.strftime('%Y-%m-%d %H:%M') if last_ok else '—'}",
        f"  Last FAILED:  {last_fail.created_at.strftime('%Y-%m-%d %H:%M') if last_fail else '—'}",
        f"  Total records: {len(pg_rows)}",
        "",
        "<b>⚙️ Settings Backup (JSON):</b>",
        f"  Auto Backup: {'🟢 ON' if auto_settings else '⚪ OFF'}",
        f"  Last Backup: {sbk_last.created_at.strftime('%Y-%m-%d %H:%M') if sbk_last else '—'}",
        f"  Total: {sbk_total}  ·  Max keep: {max_count}",
        f"  Compression: {'✅' if compress else '⚪'}  ·  Restore Confirm: {'✅' if restore_confirm else '⚪'}",
    ]

    kb = [
        [
            InlineKeyboardButton("⚙️ Settings Backups", callback_data="acc:bak:sbak"),
            InlineKeyboardButton("📦 DB Dump History", callback_data="acc:bak:pglist:0"),
        ],
        [
            InlineKeyboardButton("▶️ DB Dump Now", callback_data="acc:bak:confirm"),
            InlineKeyboardButton("🧹 Prune DB Dumps", callback_data="acc:bak:prune"),
        ],
        [
            InlineKeyboardButton(
                "🟢 Sched Dump: ON" if pg_enabled else "⚪ Sched Dump: OFF",
                callback_data="acc:bak:toggle",
            ),
        ],
        [InlineKeyboardButton("⚙️ Manager Settings", callback_data="acc:bak:mgr")],
        [back_root()],
    ]

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# pg_dump actions (preserved from existing implementation)
# ──────────────────────────────────────────────────────────────────────────

async def _pg_confirm(update: Update):
    kb = [
        [InlineKeyboardButton("✅ Run DB Dump Now", callback_data="acc:bak:run"),
         InlineKeyboardButton("Cancel", callback_data="acc:sec:backups")],
    ]
    await send(update,
               "⚠️ <b>Trigger a manual pg_dump backup?</b>\n\n"
               "This runs pg_dump against DATABASE_URL and stores a gzipped SQL file.",
               InlineKeyboardMarkup(kb))


async def _pg_run(update: Update):
    admin = update.effective_user.id
    rec = pg_backup.run_backup(triggered_by="manual", admin_id=admin)
    try:
        log_admin_action(admin, "backup_manual",
                         f"record_id={rec.id} status={rec.status}")
    except Exception:
        pass
    msg = ("✅ DB Dump completed" if rec.status == "SUCCESS"
           else f"❌ DB Dump failed: {rec.error_summary or ''}")
    await send(update, msg + f"\nFile: {rec.filename or '—'}",
               InlineKeyboardMarkup([[InlineKeyboardButton(
                   "⬅️ Back", callback_data="acc:sec:backups")]]))


async def _pg_prune(update: Update):
    n = pg_backup.cleanup_retention()
    try:
        log_admin_action(update.effective_user.id, "backup_retention_cleanup",
                         f"removed={n}")
    except Exception:
        pass
    await send(update, f"🧹 DB Dump retention cleanup: pruned {n} old file(s).",
               InlineKeyboardMarkup([[InlineKeyboardButton(
                   "⬅️ Back", callback_data="acc:sec:backups")]]))


async def _pg_toggle(update: Update):
    now = not cfg.get_bool("backup_enabled", False)
    cfg.set("backup_enabled", "true" if now else "false")
    try:
        log_admin_action(update.effective_user.id,
                         "backup_setting_changed", f"pg_enabled={now}")
    except Exception:
        pass
    await backups_menu(update, None)


async def _pg_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    with get_db_session() as s:
        rows = (s.query(BackupRecord)
                .order_by(BackupRecord.created_at.desc()).limit(100).all())
    slice_, page, pages = paginate(rows, page)

    lines = ["📦 <b>DB Dump History</b>", ""]
    if not slice_:
        lines.append("  (no records)")
    for r in slice_:
        badge = "✅" if r.status == "SUCCESS" else ("❌" if r.status == "FAILED" else "•")
        lines.append(
            f"  {badge} <b>#{r.id}</b> · {r.filename or '—'} · "
            f"{_fmt_size(r.size_bytes)} · {r.status} · "
            f"{r.created_at.strftime('%m/%d %H:%M') if r.created_at else '—'}"
        )

    kb = []
    if pages > 1:
        kb.append(nav_row("bak:pglist", page, pages))
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:sec:backups")])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# Settings Backup section
# ──────────────────────────────────────────────────────────────────────────

async def _sbak_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     page: int = 0):
    """Settings Backup list & actions menu."""
    from database.models import SettingsBackupRecord
    with get_db_session() as s:
        rows = (s.query(SettingsBackupRecord)
                .order_by(SettingsBackupRecord.created_at.desc()).limit(200).all())
    slice_, page, pages = paginate(rows, page, page_size=6)

    auto = cfg.get_bool("backup_auto_settings_enabled", False)
    compress = cfg.get_bool("backup_compression", True)

    lines = [
        "⚙️ <b>Settings Backups</b>",
        f"Auto: {'🟢 ON' if auto else '⚪ OFF'}  ·  Compression: {'✅' if compress else '⚪'}",
        f"Total: {len(rows)}",
        "",
        "<b>Recent backups:</b>",
    ]
    if not slice_:
        lines.append("  (no backups yet)")
    for r in slice_:
        badge = "✅" if r.status == "SUCCESS" else ("❌" if r.status == "FAILED" else "⏳")
        lines.append(
            f"  {badge} <b>#{r.id}</b> · {_fmt_size(r.size_bytes)} · "
            f"{r.status} · {r.created_at.strftime('%m/%d %H:%M') if r.created_at else '—'}"
            + (f" · {r.note}" if r.note else "")
        )

    kb = [
        [
            InlineKeyboardButton("▶️ Create Backup Now", callback_data="acc:bak:sconfirm"),
            InlineKeyboardButton("📥 Import Backup", callback_data="acc:bak:import_prompt"),
        ],
    ]
    if pages > 1:
        kb.append(nav_row("bak:slist", page, pages))
    # Detail buttons for each item on current page
    for r in slice_:
        kb.append([
            InlineKeyboardButton(f"#{r.id} {r.status[:4]}",
                                 callback_data=f"acc:bak:sdetail:{r.id}"),
        ])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:sec:backups")])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _sbak_confirm(update: Update):
    """Confirm before running settings backup."""
    kb = [
        [InlineKeyboardButton("✅ Create Settings Backup", callback_data="acc:bak:srun"),
         InlineKeyboardButton("Cancel", callback_data="acc:bak:sbak")],
    ]
    await send(update,
               "⚙️ <b>Create a Settings Backup?</b>\n\n"
               "This exports all bot_config values, products, categories, and "
               "payment gateway settings to a JSON file.",
               InlineKeyboardMarkup(kb))


async def _sbak_run(update: Update):
    """Run a settings backup."""
    admin = update.effective_user.id
    from services.settings_backup import create_settings_backup
    rec = create_settings_backup(admin_id=admin, triggered_by="manual")
    try:
        log_admin_action(admin, "settings_backup_created",
                         f"id={rec.id} status={rec.status}")
    except Exception:
        pass
    if rec.status == "SUCCESS":
        msg = (f"✅ <b>Settings Backup created!</b>\n"
               f"ID: #{rec.id}\n"
               f"Size: {_fmt_size(rec.size_bytes)}\n"
               f"File: <code>{rec.filename}</code>")
    else:
        msg = f"❌ Backup failed: {rec.error_summary or '—'}"
    kb = [
        [InlineKeyboardButton("⬅️ Settings Backups", callback_data="acc:bak:sbak")],
    ]
    await send(update, msg, InlineKeyboardMarkup(kb))


async def _sbak_detail(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       backup_id: int):
    """Show detail for a settings backup record."""
    from database.models import SettingsBackupRecord
    with get_db_session() as s:
        rec = s.get(SettingsBackupRecord, backup_id)
        if not rec:
            await send(update, "⚠️ Record not found.",
                       InlineKeyboardMarkup([[InlineKeyboardButton(
                           "⬅️ Back", callback_data="acc:bak:sbak")]]))
            return
        rec_id = rec.id
        status = rec.status
        fname = rec.filename
        size = rec.size_bytes
        checksum = rec.checksum
        note = rec.note
        created_at = rec.created_at
        created_by = rec.created_by
        restore_count = rec.restore_count
        last_restored = rec.last_restored_at

    lines = [
        f"💾 <b>Settings Backup #{rec_id}</b>",
        f"Status: {status}",
        f"File: <code>{fname}</code>",
        f"Size: {_fmt_size(size)}",
        f"Checksum: <code>{(checksum or '—')[:20]}…</code>",
        f"Note: {note or '—'}",
        f"Created: {created_at.strftime('%Y-%m-%d %H:%M') if created_at else '—'}",
        f"Created by: {created_by or 'auto'}",
        f"Restore count: {restore_count}",
        f"Last restored: {last_restored.strftime('%Y-%m-%d %H:%M') if last_restored else '—'}",
    ]

    kb = []
    if status == "SUCCESS":
        kb.append([
            InlineKeyboardButton("✅ Verify", callback_data=f"acc:bak:sverify:{rec_id}"),
            InlineKeyboardButton("📥 Download", callback_data=f"acc:bak:sdownload:{rec_id}"),
        ])
        restore_confirm_req = cfg.get_bool("backup_restore_confirm", True)
        if restore_confirm_req:
            kb.append([
                InlineKeyboardButton("🔄 Restore Settings", callback_data=f"acc:bak:srestore:{rec_id}"),
            ])
        else:
            kb.append([
                InlineKeyboardButton("🔄 Restore Settings", callback_data=f"acc:bak:srconfirm:{rec_id}"),
            ])
        kb.append([
            InlineKeyboardButton("🗑 Delete", callback_data=f"acc:bak:sdconfirm:{rec_id}"),
        ])
    else:
        kb.append([
            InlineKeyboardButton("🗑 Delete Record", callback_data=f"acc:bak:sdconfirm:{rec_id}"),
        ])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:bak:sbak")])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _sbak_verify(update: Update, backup_id: int):
    from services.settings_backup import verify_backup
    result = verify_backup(backup_id)
    status = "✅ Verification PASSED" if result["ok"] else "❌ Verification FAILED"
    msg = f"<b>{status}</b>\n\n{result['reason']}"
    try:
        log_admin_action(update.effective_user.id, "settings_backup_verify",
                         f"id={backup_id} ok={result['ok']}")
    except Exception:
        pass
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=f"acc:bak:sdetail:{backup_id}")],
    ])
    await send(update, msg, kb)


async def _sbak_download(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         backup_id: int):
    """Send the backup file as a Telegram document."""
    from services.settings_backup import get_backup_file_path
    admin_id = update.effective_user.id
    fpath = get_backup_file_path(backup_id)
    if fpath is None:
        await send(update, "❌ Backup file not found on disk.",
                   InlineKeyboardMarkup([[InlineKeyboardButton(
                       "⬅️ Back", callback_data=f"acc:bak:sdetail:{backup_id}")]]))
        return

    try:
        q = update.callback_query
        await q.edit_message_text("⏳ Preparing download…", parse_mode="HTML")
    except Exception:
        pass

    try:
        with open(fpath, "rb") as f:
            doc = InputFile(f, filename=fpath.name)
            await context.bot.send_document(
                chat_id=admin_id,
                document=doc,
                caption=f"💾 Settings Backup #{backup_id}\n{fpath.name}",
            )
        msg = "✅ Backup file sent!"
    except Exception as e:
        msg = f"❌ Failed to send file: {str(e)[:80]}"

    try:
        log_admin_action(admin_id, "settings_backup_download", f"id={backup_id}")
    except Exception:
        pass

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=f"acc:bak:sdetail:{backup_id}")],
    ])
    await send(update, msg, kb)


async def _sbak_restore_confirm(update: Update, backup_id: int):
    """Show restore confirmation screen."""
    kb = [
        [
            InlineKeyboardButton("🔄 Restore Settings Only",
                                 callback_data=f"acc:bak:srconfirm:{backup_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Full Restore (+ Products/Categories)",
                                 callback_data=f"acc:bak:srfull:{backup_id}"),
        ],
        [InlineKeyboardButton("Cancel", callback_data=f"acc:bak:sdetail:{backup_id}")],
    ]
    await send(update,
               f"⚠️ <b>Restore Backup #{backup_id}?</b>\n\n"
               "<b>Settings Only</b> — restores all bot_config values (safe).\n"
               "<b>Full Restore</b> — also updates product prices/stock and category names.\n\n"
               "⚠️ This cannot be undone. Consider creating a new backup first.",
               InlineKeyboardMarkup(kb))


async def _sbak_do_restore(update: Update, backup_id: int,
                            restore_products: bool = False,
                            restore_categories: bool = False):
    """Execute the restore."""
    from services.settings_backup import restore_settings_backup
    admin_id = update.effective_user.id
    result = restore_settings_backup(
        backup_id=backup_id,
        admin_id=admin_id,
        restore_products=restore_products,
        restore_categories=restore_categories,
    )
    try:
        log_admin_action(admin_id, "settings_backup_restore",
                         f"id={backup_id} ok={result['ok']} keys={result['restored_keys']}")
    except Exception:
        pass

    if result["ok"]:
        msg = (f"✅ <b>Restore Successful</b>\n\n"
               f"Settings keys restored: {result['restored_keys']}\n"
               f"Products updated: {'Yes' if restore_products else 'No'}\n"
               f"Categories updated: {'Yes' if restore_categories else 'No'}")
    else:
        errors = "\n".join(result["errors"][:5])
        msg = (f"⚠️ <b>Restore Completed with Errors</b>\n\n"
               f"Keys restored: {result['restored_keys']}\n"
               f"Errors:\n{errors}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Settings Backups", callback_data="acc:bak:sbak")],
    ])
    await send(update, msg, kb)


async def _sbak_delete_confirm(update: Update, backup_id: int):
    kb = [
        [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"acc:bak:sdel:{backup_id}"),
         InlineKeyboardButton("Cancel", callback_data=f"acc:bak:sdetail:{backup_id}")],
    ]
    await send(update,
               f"⚠️ <b>Delete Settings Backup #{backup_id}?</b>\n\nThis is permanent.",
               InlineKeyboardMarkup(kb))


async def _sbak_do_delete(update: Update, backup_id: int):
    from services.settings_backup import delete_backup
    result = delete_backup(backup_id)
    try:
        log_admin_action(update.effective_user.id, "settings_backup_delete",
                         f"id={backup_id} ok={result['ok']}")
    except Exception:
        pass
    msg = f"✅ Backup #{backup_id} deleted." if result["ok"] else f"❌ {result['reason']}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Settings Backups", callback_data="acc:bak:sbak")],
    ])
    await send(update, msg, kb)


# ──────────────────────────────────────────────────────────────────────────
# Import (via document upload — ConversationHandler)
# ──────────────────────────────────────────────────────────────────────────

async def _import_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show import instructions and enter conversation state."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data="acc:bak:sbak")],
    ])
    await send(update,
               "📥 <b>Import Settings Backup</b>\n\n"
               "Send a <b>.json</b> or <b>.json.gz</b> backup file as a document in this chat.\n\n"
               "The file must be a valid settings backup created by this system.",
               kb)
    return BAK_IMPORT_FILE


async def _import_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the uploaded backup file."""
    admin_id = update.effective_user.id
    doc = update.message.document

    if not doc:
        await update.message.reply_text(
            "⚠️ Please send a .json or .json.gz file as a document.",
        )
        return BAK_IMPORT_FILE

    fname = doc.file_name or ""
    if not (fname.endswith(".json") or fname.endswith(".json.gz")):
        await update.message.reply_text(
            "⚠️ Invalid file type. Please send a .json or .json.gz file.",
        )
        return BAK_IMPORT_FILE

    await update.message.reply_text("⏳ Importing backup…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        data = buf.getvalue()

        from services.settings_backup import import_backup_from_bytes
        rec = import_backup_from_bytes(data=data, admin_id=admin_id)

        try:
            log_admin_action(admin_id, "settings_backup_import",
                             f"id={rec.id} status={rec.status}")
        except Exception:
            pass

        if rec.status == "SUCCESS":
            msg = (f"✅ <b>Backup Imported!</b>\n"
                   f"ID: #{rec.id}\n"
                   f"Size: {_fmt_size(rec.size_bytes)}\n"
                   f"Use the Settings Backups menu to restore it.")
        else:
            msg = f"❌ Import failed: {rec.error_summary or '—'}"
    except Exception as e:
        msg = f"❌ Import error: {str(e)[:100]}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings Backups", callback_data="acc:bak:sbak")],
    ])
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def _import_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Import cancelled.")
    return ConversationHandler.END


def build_bak_import_conv() -> ConversationHandler:
    """Build and return the backup import ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_import_prompt, pattern=r"^acc:bak:import_prompt$"),
        ],
        states={
            BAK_IMPORT_FILE: [
                MessageHandler(filters.Document.ALL, _import_receive_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _import_cancel),
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, _import_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


# ──────────────────────────────────────────────────────────────────────────
# Manager Settings
# ──────────────────────────────────────────────────────────────────────────

async def _mgr_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mgr_status = _mgr_status()
    auto = cfg.get_bool("backup_auto_settings_enabled", False)
    interval = cfg.get_int("backup_settings_interval_hours", 24)
    max_count = cfg.get_int("backup_max_count", 30)
    compress = cfg.get_bool("backup_compression", True)
    restore_confirm = cfg.get_bool("backup_restore_confirm", True)
    pg_enabled = cfg.get_bool("backup_enabled", False)

    lines = [
        "⚙️ <b>Backup Manager Settings</b>",
        "",
        f"Status: {_STATUS_EMOJI.get(mgr_status, '⚪')} {mgr_status.title()}",
        f"Auto Settings Backup: {'✅ ON' if auto else '⚪ OFF'}",
        f"Settings Backup Interval: {interval}h",
        f"Max Backups to Keep: {max_count}",
        f"Compression: {'✅ ON' if compress else '⚪ OFF'}",
        f"Restore Confirmation: {'✅ ON' if restore_confirm else '⚪ OFF'}",
        "",
        f"DB Dump (pg_dump) Scheduled: {'✅ ON' if pg_enabled else '⚪ OFF'}",
        f"DB Dump Interval: {cfg.get_int('backup_interval_hours', 24)}h",
        f"DB Dump Retention: {cfg.get_int('backup_retention_count', 14)} files",
    ]

    kb = [
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="acc:bak:set_status:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="acc:bak:set_status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="acc:bak:set_status:disabled"),
        ],
        [
            InlineKeyboardButton(
                "🔄 Auto Backup: " + ("ON ✅" if auto else "OFF ⚪"),
                callback_data="acc:bak:toggle_auto",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗜 Compression: " + ("ON ✅" if compress else "OFF ⚪"),
                callback_data="acc:bak:toggle_compress",
            ),
            InlineKeyboardButton(
                "🔒 Restore Confirm: " + ("ON ✅" if restore_confirm else "OFF ⚪"),
                callback_data="acc:bak:toggle_restore_confirm",
            ),
        ],
    ]
    # Interval presets for settings backup
    kb.append([
        InlineKeyboardButton(f"{'✔ ' if interval == 6 else ''}6h",
                             callback_data="acc:bak:set_interval:6"),
        InlineKeyboardButton(f"{'✔ ' if interval == 12 else ''}12h",
                             callback_data="acc:bak:set_interval:12"),
        InlineKeyboardButton(f"{'✔ ' if interval == 24 else ''}24h",
                             callback_data="acc:bak:set_interval:24"),
        InlineKeyboardButton(f"{'✔ ' if interval == 48 else ''}48h",
                             callback_data="acc:bak:set_interval:48"),
        InlineKeyboardButton(f"{'✔ ' if interval == 168 else ''}7d",
                             callback_data="acc:bak:set_interval:168"),
    ])
    # Max count presets
    kb.append([
        InlineKeyboardButton(f"{'✔ ' if max_count == 7 else ''}7",
                             callback_data="acc:bak:set_max:7"),
        InlineKeyboardButton(f"{'✔ ' if max_count == 14 else ''}14",
                             callback_data="acc:bak:set_max:14"),
        InlineKeyboardButton(f"{'✔ ' if max_count == 30 else ''}30",
                             callback_data="acc:bak:set_max:30"),
        InlineKeyboardButton(f"{'✔ ' if max_count == 60 else ''}60",
                             callback_data="acc:bak:set_max:60"),
        InlineKeyboardButton(f"{'✔ ' if max_count == 90 else ''}90",
                             callback_data="acc:bak:set_max:90"),
    ])
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# Auto backup job (called by scheduler)
# ──────────────────────────────────────────────────────────────────────────

async def settings_backup_auto_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduler job: create a settings backup if auto is enabled."""
    try:
        if not cfg.get_bool("backup_auto_settings_enabled", False):
            return
        if _mgr_status() not in ("enabled",):
            return
        from services.settings_backup import create_settings_backup, cleanup_old_backups
        rec = create_settings_backup(triggered_by="auto")
        if rec.status == "SUCCESS":
            cleanup_old_backups()
        logger.info("settings_backup_auto_job: id=%s status=%s", rec.id, rec.status)
    except Exception:
        logger.exception("settings_backup_auto_job failed")


# ──────────────────────────────────────────────────────────────────────────
# Unified route dispatcher
# ──────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update: Update,
                context: ContextTypes.DEFAULT_TYPE):
    """Route acc:bak:<action>[:<arg>] callbacks."""
    uid = update.effective_user.id if update.effective_user else 0
    if not _guard(uid):
        q = update.callback_query
        if q:
            await q.answer("⛔ Access denied.", show_alert=True)
        return

    # ── pg_dump actions (preserved) ───────────────────────────────────────
    if action == "confirm":
        await _pg_confirm(update)
        return
    if action == "run":
        await _pg_run(update)
        return
    if action == "prune":
        await _pg_prune(update)
        return
    if action == "toggle":
        await _pg_toggle(update)
        return
    if action == "list":
        # Legacy: acc:bak:list:<page>
        page = int(rest[0]) if rest else 0
        await backups_menu(update, context, page=page)
        return
    if action == "pglist":
        page = int(rest[0]) if rest else 0
        await _pg_list(update, context, page=page)
        return

    # ── Settings backup actions ───────────────────────────────────────────
    if action == "sbak":
        page = int(rest[0]) if rest else 0
        await _sbak_menu(update, context, page=page)
        return
    if action == "slist":
        page = int(rest[0]) if rest else 0
        await _sbak_menu(update, context, page=page)
        return
    if action == "sconfirm":
        await _sbak_confirm(update)
        return
    if action == "srun":
        await _sbak_run(update)
        return
    if action == "sdetail":
        bid = int(rest[0]) if rest else 0
        await _sbak_detail(update, context, bid)
        return
    if action == "sverify":
        bid = int(rest[0]) if rest else 0
        await _sbak_verify(update, bid)
        return
    if action == "sdownload":
        bid = int(rest[0]) if rest else 0
        await _sbak_download(update, context, bid)
        return
    if action == "srestore":
        bid = int(rest[0]) if rest else 0
        await _sbak_restore_confirm(update, bid)
        return
    if action == "srconfirm":
        bid = int(rest[0]) if rest else 0
        await _sbak_do_restore(update, bid, restore_products=False, restore_categories=False)
        return
    if action == "srfull":
        bid = int(rest[0]) if rest else 0
        # Show full-restore confirmation
        kb = [
            [InlineKeyboardButton("✅ Confirm Full Restore",
                                  callback_data=f"acc:bak:srfull_confirm:{bid}"),
             InlineKeyboardButton("Cancel", callback_data=f"acc:bak:sdetail:{bid}")],
        ]
        await send(update,
                   f"⚠️ <b>Full Restore Backup #{bid}?</b>\n\n"
                   "This will update product prices, stock, and category names.\n"
                   "<b>This cannot be undone.</b>",
                   InlineKeyboardMarkup(kb))
        return
    if action == "srfull_confirm":
        bid = int(rest[0]) if rest else 0
        await _sbak_do_restore(update, bid, restore_products=True, restore_categories=True)
        return
    if action == "sdconfirm":
        bid = int(rest[0]) if rest else 0
        await _sbak_delete_confirm(update, bid)
        return
    if action == "sdel":
        bid = int(rest[0]) if rest else 0
        await _sbak_do_delete(update, bid)
        return
    if action == "import_prompt":
        # This is handled by the ConversationHandler entry point
        # but we add a fallback here in case it's routed via acc_dispatch
        await _import_prompt(update, context)
        return

    # ── Manager settings ──────────────────────────────────────────────────
    if action == "mgr":
        await _mgr_settings(update, context)
        return
    if action == "set_status":
        new_status = rest[0] if rest else "enabled"
        if new_status in ("enabled", "maintenance", "disabled"):
            cfg.set("backup_manager_status", new_status)
            try:
                log_admin_action(uid, "backup_manager_status_changed",
                                 f"status={new_status}")
            except Exception:
                pass
        await _mgr_settings(update, context)
        return
    if action == "toggle_auto":
        new_val = not cfg.get_bool("backup_auto_settings_enabled", False)
        cfg.set("backup_auto_settings_enabled", "true" if new_val else "false")
        await _mgr_settings(update, context)
        return
    if action == "toggle_compress":
        new_val = not cfg.get_bool("backup_compression", True)
        cfg.set("backup_compression", "true" if new_val else "false")
        await _mgr_settings(update, context)
        return
    if action == "toggle_restore_confirm":
        new_val = not cfg.get_bool("backup_restore_confirm", True)
        cfg.set("backup_restore_confirm", "true" if new_val else "false")
        await _mgr_settings(update, context)
        return
    if action == "set_interval":
        try:
            hours = max(1, min(720, int(rest[0])))
        except (ValueError, IndexError):
            hours = 24
        cfg.set("backup_settings_interval_hours", str(hours))
        await _mgr_settings(update, context)
        return
    if action == "set_max":
        try:
            n = max(1, min(365, int(rest[0])))
        except (ValueError, IndexError):
            n = 30
        cfg.set("backup_max_count", str(n))
        await _mgr_settings(update, context)
        return

    # ── Fallback ──────────────────────────────────────────────────────────
    await backups_menu(update, context)
