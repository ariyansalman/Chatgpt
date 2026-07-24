"""Bulk Product Import & Export admin handler — V35.

Callback namespace: bpim:*

Features:
  • CSV / Excel / JSON import with validation and duplicate detection
  • Export: all / by category / selected products
  • Bulk actions: enable, disable, price edit, stock edit, category change,
    clone, delete, update delivery type
  • Feature status management: 🟢 enabled / 🟡 maintenance / 🔴 disabled
  • Statistics: import/export totals, failed imports, bulk action count
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────
BPIM_WAIT_FILE     = 0   # waiting for uploaded import file
BPIM_WAIT_PRICE    = 1   # waiting for new price input
BPIM_WAIT_STOCK    = 2   # waiting for new stock input

# ── Status helpers ────────────────────────────────────────────────────────
_STATUS_EMOJI = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}

def _mgr_status() -> str:
    return cfg.get("bulk_product_manager_status", "enabled")

def _is_enabled() -> bool:
    return _mgr_status() == "enabled"

def _is_active() -> bool:
    return _mgr_status() in ("enabled", "maintenance")

def _guard(uid: int) -> bool:
    return has_permission(uid, "manage_products")


# ── Safe edit helper ──────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_main() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data="bpim:menu")


def _back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_back_main()]])


# ═════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ═════════════════════════════════════════════════════════════════════════

async def bpim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — Bulk Product Manager main menu."""
    query = update.callback_query
    uid = update.effective_user.id
    if query:
        await query.answer()

    if not _guard(uid):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    mgr_status = _mgr_status()
    status_emoji = _STATUS_EMOJI.get(mgr_status, "⚪")

    from services.bulk_product_service import get_product_bulk_stats
    stats = get_product_bulk_stats()

    from services import payment_ui as pui
    text = (
        f"📦 <b>Bulk Product Manager</b>\n"
        f"{pui.DIVIDER}\n"
        f"Status: {status_emoji} {mgr_status.title()}\n\n"
        f"📊 <b>Statistics:</b>\n"
        f"  📥 Total Imports: <b>{stats['total_imports']}</b>\n"
        f"  📤 Total Exports: <b>{stats['total_exports']}</b>\n"
        f"  ✅ Imported Products: <b>{stats['imported_products']}</b>\n"
        f"  ❌ Failed Imports: <b>{stats['failed_imports']}</b>\n"
        f"  ⚡ Bulk Actions: <b>{stats['bulk_actions']}</b>\n\n"
        f"Choose an action:"
    )

    kb = [
        [InlineKeyboardButton("📥 Import Products", callback_data="bpim:import:menu"),
         InlineKeyboardButton("📤 Export Products", callback_data="bpim:export:menu")],
        [InlineKeyboardButton("⚡ Bulk Actions",    callback_data="bpim:bulk:menu")],
        [InlineKeyboardButton("📋 Import History",  callback_data="bpim:history:import:0"),
         InlineKeyboardButton("📋 Export History",  callback_data="bpim:history:export:0")],
        [InlineKeyboardButton(f"{status_emoji} Manager Settings", callback_data="bpim:settings")],
        [InlineKeyboardButton("🔙 Admin Panel",    callback_data="acc:root")],
    ]

    msg_text = text
    if query:
        await _safe_edit(query, msg_text, InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════════════
# IMPORT FLOW
# ═════════════════════════════════════════════════════════════════════════

async def bpim_import_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    if not _is_enabled():
        await _safe_edit(query, f"⛔ Bulk Product Manager is {_mgr_status()}.", _back_main_kb())
        return

    text = (
        "📥 <b>IMPORT PRODUCTS</b>\n\n"
        "Select the import format:\n\n"
        "• <b>CSV</b> — Comma-separated values (.csv)\n"
        "• <b>Excel</b> — Microsoft Excel (.xlsx)\n"
        "• <b>JSON</b> — JavaScript Object Notation (.json)\n\n"
        "Required fields: <code>name</code>, <code>price</code>, <code>product_type</code>\n"
        "Optional: category, description, stock_count, sale_price, currency, …"
    )
    kb = [
        [InlineKeyboardButton("📄 CSV",   callback_data="bpim:import:start:csv"),
         InlineKeyboardButton("📊 Excel", callback_data="bpim:import:start:xlsx"),
         InlineKeyboardButton("📋 JSON",  callback_data="bpim:import:start:json")],
        [InlineKeyboardButton("📄 Download CSV Template", callback_data="bpim:import:template")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_import_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a CSV template file to the admin."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    header = (
        "name,description,category,subcategory,price,sale_price,currency,"
        "product_type,stock_count,is_active,is_featured,delivery_note,"
        "warranty_info,min_quantity,max_quantity,bulk_purchase_enabled,"
        "reusable,product_emoji,sort_order\n"
    )
    example = (
        "Example Product,A sample product description,Electronics,Phones,"
        "9.99,,USD,KEY,100,true,false,Delivered instantly,,1,10,true,false,📱,1\n"
    )
    data = (header + example).encode("utf-8-sig")
    file_obj = InputFile(io.BytesIO(data), filename="product_import_template.csv")
    await query.message.reply_document(
        document=file_obj,
        caption="📄 CSV import template. Fill in your products and upload it back.",
    )


async def bpim_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask admin to upload file for import."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return
    if not _is_enabled():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    parts = (query.data or "").split(":")
    fmt = parts[3] if len(parts) > 3 else "csv"
    context.user_data["bpim_fmt"] = fmt

    fmt_labels = {"csv": "CSV (.csv)", "xlsx": "Excel (.xlsx)", "json": "JSON (.json)"}
    text = (
        f"📥 <b>IMPORT — {fmt_labels.get(fmt, fmt.upper())}</b>\n\n"
        f"Please upload your <b>{fmt_labels.get(fmt, fmt).split()[0]}</b> file now.\n\n"
        f"Max rows: {cfg.get_int('bulk_product_import_max_rows', 1000)}\n\n"
        f"Send /cancel to abort."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bpim:import:cancel")]])
    await _safe_edit(query, text, kb)
    return BPIM_WAIT_FILE


async def bpim_import_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the uploaded import file."""
    uid = update.effective_user.id
    if not _guard(uid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    fmt = context.user_data.get("bpim_fmt", "csv")

    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Please send a file document.")
        return BPIM_WAIT_FILE

    await update.message.reply_text("⏳ Processing your import file…")

    try:
        file_obj = await doc.get_file()
        file_bytes = await file_obj.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"❌ Could not download file: {e}")
        return ConversationHandler.END

    max_rows = cfg.get_int("bulk_product_import_max_rows", 1000)

    from services.bulk_product_service import import_products
    report = import_products(bytes(file_bytes), fmt, uid, max_rows=max_rows)

    log_admin_action(uid, "bulk_product_import",
                     target_type="bulk_import",
                     details=f"fmt={fmt} imported={report['imported']} failed={report['failed']}",
                     module="bulk_products")

    errors_text = ""
    if report["errors"]:
        shown = report["errors"][:10]
        errors_text = "\n".join(f"  • {e}" for e in shown)
        if len(report["errors"]) > 10:
            errors_text += f"\n  … and {len(report['errors']) - 10} more"

    text = (
        f"📥 <b>IMPORT COMPLETE</b>\n\n"
        f"📊 Total rows: <b>{report['total_rows']}</b>\n"
        f"✅ Imported: <b>{report['imported']}</b>\n"
        f"⚠️ Duplicates skipped: <b>{report['duplicates']}</b>\n"
        f"❌ Failed: <b>{report['failed']}</b>\n"
    )
    if errors_text:
        text += f"\n<b>Errors / warnings:</b>\n{errors_text}"

    kb = InlineKeyboardMarkup([[_back_main()]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def bpim_import_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await _safe_edit(query, "❌ Import cancelled.", _back_main_kb())
    return ConversationHandler.END


# ═════════════════════════════════════════════════════════════════════════
# EXPORT FLOW
# ═════════════════════════════════════════════════════════════════════════

async def bpim_export_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = (
        "📤 <b>EXPORT PRODUCTS</b>\n\n"
        "Select what to export:\n"
        "• <b>All Products</b> — entire catalog\n"
        "• <b>By Category</b> — products from a specific category\n"
        "• <b>Active Only</b> — only active products\n"
        "• <b>Inactive Only</b> — only disabled products\n"
    )
    kb = [
        [InlineKeyboardButton("🌐 All Products",    callback_data="bpim:export:scope:all"),
         InlineKeyboardButton("🗂 By Category",     callback_data="bpim:export:catsel")],
        [InlineKeyboardButton("🟢 Active Only",     callback_data="bpim:export:scope:active"),
         InlineKeyboardButton("🔴 Inactive Only",   callback_data="bpim:export:scope:inactive")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_export_catsel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show categories to choose from for category-scoped export."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    from database.models import Category
    with get_db_session() as s:
        cats = s.query(Category).order_by(Category.name).all()
        cat_list = [(c.id, c.name) for c in cats]

    if not cat_list:
        await _safe_edit(query, "⚠️ No categories found.", _back_main_kb()); return

    kb = []
    for cid, cname in cat_list:
        kb.append([InlineKeyboardButton(
            f"🗂 {cname}", callback_data=f"bpim:export:scope:category:{cid}"
        )])
    kb.append([_back_main()])
    await _safe_edit(query, "🗂 <b>Select a category to export:</b>", InlineKeyboardMarkup(kb))


async def bpim_export_scope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show format selection after scope is chosen."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    # bpim:export:scope:<scope>[:<scope_arg>]
    scope = parts[3] if len(parts) > 3 else "all"
    scope_arg = parts[4] if len(parts) > 4 else None

    context.user_data["bpim_export_scope"] = scope
    context.user_data["bpim_export_scope_arg"] = scope_arg

    scope_labels = {
        "all": "All Products",
        "active": "Active Products",
        "inactive": "Inactive Products",
        "category": f"Category #{scope_arg}",
    }
    text = (
        f"📤 <b>EXPORT — {scope_labels.get(scope, scope)}</b>\n\n"
        f"Select export format:"
    )
    kb = [
        [InlineKeyboardButton("📄 CSV",   callback_data="bpim:export:do:csv"),
         InlineKeyboardButton("📊 Excel", callback_data="bpim:export:do:xlsx"),
         InlineKeyboardButton("📋 JSON",  callback_data="bpim:export:do:json")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_export_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the export and send the file."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid): return
    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    parts = (query.data or "").split(":")
    fmt = parts[3] if len(parts) > 3 else "csv"
    scope = context.user_data.get("bpim_export_scope", "all")
    scope_arg = context.user_data.get("bpim_export_scope_arg")

    await _safe_edit(query, "⏳ Generating export file…", None)

    try:
        from services.bulk_product_service import export_products
        data, row_count = export_products(fmt, scope, scope_arg, admin_id=uid)
    except Exception as e:
        await query.message.reply_text(f"❌ Export failed: {e}")
        return

    ext_map = {"csv": "csv", "xlsx": "xlsx", "json": "json"}
    ext = ext_map.get(fmt, fmt)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"products_{scope}_{ts}.{ext}"
    mime = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "json": "application/json"}.get(fmt, "application/octet-stream")

    file_obj = InputFile(io.BytesIO(data), filename=filename)
    await query.message.reply_document(
        document=file_obj,
        caption=f"📤 Product export — {row_count} rows\nScope: {scope} | Format: {fmt.upper()}",
    )

    log_admin_action(uid, "bulk_product_export", target_type="export",
                     details=f"fmt={fmt} scope={scope} rows={row_count}",
                     module="bulk_products")

    # Show back button
    await query.message.reply_text(
        "✅ Export complete.",
        reply_markup=InlineKeyboardMarkup([[_back_main()]]),
    )


# ═════════════════════════════════════════════════════════════════════════
# BULK ACTIONS MENU
# ═════════════════════════════════════════════════════════════════════════

async def bpim_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    if not _is_enabled():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = "⚡ <b>BULK ACTIONS</b>\n\nSelect an action to apply to products:"
    kb = [
        [InlineKeyboardButton("🟢 Bulk Enable",          callback_data="bpim:bulk:enable:scope"),
         InlineKeyboardButton("🔴 Bulk Disable",         callback_data="bpim:bulk:disable:scope")],
        [InlineKeyboardButton("💰 Bulk Edit Price",       callback_data="bpim:bulk:price:scope"),
         InlineKeyboardButton("📦 Bulk Edit Stock",       callback_data="bpim:bulk:stock:scope")],
        [InlineKeyboardButton("🗂 Bulk Change Category",  callback_data="bpim:bulk:category:scope"),
         InlineKeyboardButton("🚚 Bulk Update Del. Type", callback_data="bpim:bulk:dtype:scope")],
        [InlineKeyboardButton("🔁 Bulk Clone",            callback_data="bpim:bulk:clone:scope"),
         InlineKeyboardButton("🗑 Bulk Delete",           callback_data="bpim:bulk:delete:scope")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_bulk_scope_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Select scope for a bulk action (all / active / inactive)."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    # bpim:bulk:<action>:scope
    action = parts[2] if len(parts) > 2 else "enable"
    context.user_data["bpim_bulk_action"] = action

    action_labels = {
        "enable": "Enable Products",
        "disable": "Disable Products",
        "price": "Edit Price",
        "stock": "Edit Stock",
        "category": "Change Category",
        "dtype": "Update Delivery Type",
        "clone": "Clone Products",
        "delete": "Delete Products",
    }
    text = (
        f"⚡ <b>BULK {action_labels.get(action, action).upper()}</b>\n\n"
        f"Select scope:\n"
        f"• <b>All Products</b> — affects every product\n"
        f"• <b>Active Only</b> — only active products\n"
        f"• <b>Inactive Only</b> — only disabled products"
    )
    kb = [
        [InlineKeyboardButton("🌐 All Products",  callback_data=f"bpim:bulk:{action}:all"),
         InlineKeyboardButton("🟢 Active Only",   callback_data=f"bpim:bulk:{action}:active")],
        [InlineKeyboardButton("🔴 Inactive Only", callback_data=f"bpim:bulk:{action}:inactive")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show confirmation before destructive bulk actions, or prompt for value."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid): return

    parts = (query.data or "").split(":")
    # bpim:bulk:<action>:<scope>
    action = parts[2] if len(parts) > 2 else "enable"
    scope = parts[3] if len(parts) > 3 else "all"
    context.user_data["bpim_bulk_action"] = action
    context.user_data["bpim_bulk_scope"] = scope

    # Actions that need a value input:
    if action == "price":
        await _safe_edit(
            query,
            "💰 <b>BULK PRICE EDIT</b>\n\nPlease reply with the new price (e.g. <code>19.99</code>).\n\nSend /cancel to abort.",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bpim:bulk:cancel")]]),
        )
        return BPIM_WAIT_PRICE

    if action == "stock":
        await _safe_edit(
            query,
            "📦 <b>BULK STOCK EDIT</b>\n\nPlease reply with the new stock count (e.g. <code>100</code>).\n\nSend /cancel to abort.",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bpim:bulk:cancel")]]),
        )
        return BPIM_WAIT_STOCK

    if action == "category":
        from database.models import Category
        with get_db_session() as s:
            cats = s.query(Category).order_by(Category.name).all()
            cat_list = [(c.id, c.name) for c in cats]
        if not cat_list:
            await _safe_edit(query, "⚠️ No categories found.", _back_main_kb()); return
        kb = []
        for cid, cname in cat_list:
            kb.append([InlineKeyboardButton(
                f"🗂 {cname}", callback_data=f"bpim:bulk:category:confirm:{scope}:{cid}"
            )])
        kb.append([_back_main()])
        await _safe_edit(query, "🗂 <b>Select target category:</b>", InlineKeyboardMarkup(kb))
        return

    if action == "dtype":
        from services.bulk_product_service import VALID_PRODUCT_TYPES
        types = sorted(VALID_PRODUCT_TYPES)
        kb = []
        row = []
        for t in types:
            row.append(InlineKeyboardButton(t, callback_data=f"bpim:bulk:dtype:confirm:{scope}:{t}"))
            if len(row) == 2:
                kb.append(row); row = []
        if row:
            kb.append(row)
        kb.append([_back_main()])
        await _safe_edit(query, "🚚 <b>Select new Delivery Type:</b>", InlineKeyboardMarkup(kb))
        return

    # Delete requires extra confirmation
    if action == "delete":
        need_confirm = cfg.get_bool("bulk_product_delete_confirm", True)
        if need_confirm:
            kb = [
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"bpim:bulk:delete:exec:{scope}"),
                 InlineKeyboardButton("❌ Cancel",       callback_data="bpim:menu")],
            ]
            await _safe_edit(
                query,
                f"⚠️ <b>CONFIRM BULK DELETE</b>\n\nYou are about to delete <b>{scope}</b> products.\n"
                f"This action cannot be undone. Are you sure?",
                InlineKeyboardMarkup(kb),
            )
            return

    # Immediate actions: enable, disable, clone
    await _run_bulk_action(query, uid, action, scope)


async def _run_bulk_action(query, uid: int, action: str, scope: str, scope_arg=None,
                            param: Optional[float] = None):
    """Execute a bulk action and report result."""
    from services.bulk_product_service import (
        bulk_enable_products, bulk_disable_products,
        bulk_delete_products, bulk_clone_products,
    )

    await _safe_edit(query, f"⏳ Running bulk {action}…")

    try:
        if action == "enable":
            result = bulk_enable_products(uid, scope, scope_arg)
        elif action == "disable":
            result = bulk_disable_products(uid, scope, scope_arg)
        elif action == "delete":
            result = bulk_delete_products(uid, scope, scope_arg)
        elif action == "clone":
            result = bulk_clone_products(uid, scope, scope_arg)
        else:
            result = {"success": 0, "failed": 0}
    except Exception as e:
        await query.message.reply_text(f"❌ Bulk action failed: {e}")
        return

    log_admin_action(uid, f"bulk_product_{action}", target_type="bulk_action",
                     details=f"scope={scope} success={result['success']} failed={result['failed']}",
                     module="bulk_products")

    text = (
        f"✅ <b>BULK {action.upper()} COMPLETE</b>\n\n"
        f"✅ Success: <b>{result['success']}</b>\n"
        f"❌ Failed:  <b>{result['failed']}</b>"
    )
    await query.message.reply_text(text, reply_markup=_back_main_kb(), parse_mode="HTML")


async def bpim_bulk_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute confirmed bulk actions that needed category/dtype/confirm."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid): return

    parts = (query.data or "").split(":")
    # bpim:bulk:<action>:exec:<scope>[:<arg>]
    # bpim:bulk:<action>:confirm:<scope>[:<arg>]
    action = parts[2] if len(parts) > 2 else "enable"
    # parts[3] = exec|confirm
    scope = parts[4] if len(parts) > 4 else "all"
    scope_arg = parts[5] if len(parts) > 5 else None

    await _safe_edit(query, f"⏳ Running bulk {action}…")

    from services.bulk_product_service import (
        bulk_enable_products, bulk_disable_products, bulk_delete_products,
        bulk_clone_products, bulk_change_category, bulk_update_tags,
    )

    try:
        if action == "delete":
            result = bulk_delete_products(uid, scope, None)
        elif action == "category" and scope_arg:
            result = bulk_change_category(uid, scope, int(scope_arg), None)
        elif action == "dtype" and scope_arg:
            result = bulk_update_tags(uid, scope, scope_arg, None)
        else:
            result = {"success": 0, "failed": 0}
    except Exception as e:
        await query.message.reply_text(f"❌ Bulk action failed: {e}")
        return

    log_admin_action(uid, f"bulk_product_{action}", target_type="bulk_action",
                     details=f"scope={scope} arg={scope_arg} success={result['success']} failed={result['failed']}",
                     module="bulk_products")

    text = (
        f"✅ <b>BULK {action.upper()} COMPLETE</b>\n\n"
        f"✅ Success: <b>{result['success']}</b>\n"
        f"❌ Failed:  <b>{result['failed']}</b>"
    )
    await query.message.reply_text(text, reply_markup=_back_main_kb(), parse_mode="HTML")


# ── Price input (ConversationHandler continuation) ───────────────────────

async def bpim_bulk_price_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    scope = context.user_data.get("bpim_bulk_scope", "all")
    text = update.message.text.strip()
    try:
        new_price = float(text.replace(",", "."))
        if new_price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a positive number (e.g. 19.99).")
        return BPIM_WAIT_PRICE

    from services.bulk_product_service import bulk_edit_price
    result = bulk_edit_price(uid, scope, new_price)
    log_admin_action(uid, "bulk_product_price_edit", target_type="bulk_action",
                     details=f"scope={scope} new_price={new_price} success={result['success']}",
                     module="bulk_products")
    await update.message.reply_text(
        f"✅ Price updated to <b>${new_price:.2f}</b>\n"
        f"✅ Success: <b>{result['success']}</b>  ❌ Failed: <b>{result['failed']}</b>",
        reply_markup=_back_main_kb(), parse_mode="HTML",
    )
    return ConversationHandler.END


async def bpim_bulk_stock_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    scope = context.user_data.get("bpim_bulk_scope", "all")
    text = update.message.text.strip()
    try:
        new_stock = int(text)
        if new_stock < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid stock. Enter a non-negative integer (e.g. 100).")
        return BPIM_WAIT_STOCK

    from services.bulk_product_service import bulk_edit_stock
    result = bulk_edit_stock(uid, scope, new_stock)
    log_admin_action(uid, "bulk_product_stock_edit", target_type="bulk_action",
                     details=f"scope={scope} new_stock={new_stock} success={result['success']}",
                     module="bulk_products")
    await update.message.reply_text(
        f"✅ Stock updated to <b>{new_stock}</b>\n"
        f"✅ Success: <b>{result['success']}</b>  ❌ Failed: <b>{result['failed']}</b>",
        reply_markup=_back_main_kb(), parse_mode="HTML",
    )
    return ConversationHandler.END


# ═════════════════════════════════════════════════════════════════════════
# HISTORY VIEWS
# ═════════════════════════════════════════════════════════════════════════

async def bpim_history_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    page = int(parts[4]) if len(parts) > 4 else 0
    per_page = 5

    from database.models import BulkImportRecord
    with get_db_session() as s:
        total = s.query(BulkImportRecord).count()
        records = (
            s.query(BulkImportRecord)
            .order_by(BulkImportRecord.started_at.desc())
            .limit(per_page).offset(page * per_page)
            .all()
        )
        rows = [
            (r.id, r.file_format, r.status, r.imported, r.failed, r.duplicates,
             r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "—")
            for r in records
        ]

    text = "📋 <b>IMPORT HISTORY</b>\n\n"
    if not rows:
        text += "No import records yet."
    else:
        for rid, fmt, status, imp, fail, dup, ts in rows:
            emoji = "✅" if status == "COMPLETED" else ("❌" if status == "FAILED" else "⏳")
            text += (
                f"{emoji} <b>#{rid}</b> [{fmt.upper()}] — {ts}\n"
                f"   ✅ {imp} imported · ❌ {fail} failed · ⚠️ {dup} dups\n\n"
            )

    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bpim:history:import:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bpim:history:import:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_history_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    page = int(parts[4]) if len(parts) > 4 else 0
    per_page = 5

    from database.models import BulkExportRecord
    with get_db_session() as s:
        total = s.query(BulkExportRecord).filter_by(export_type="products").count()
        records = (
            s.query(BulkExportRecord)
            .filter_by(export_type="products")
            .order_by(BulkExportRecord.started_at.desc())
            .limit(per_page).offset(page * per_page)
            .all()
        )
        rows = [
            (r.id, r.file_format, r.scope, r.row_count,
             r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "—")
            for r in records
        ]

    text = "📋 <b>EXPORT HISTORY</b>\n\n"
    if not rows:
        text += "No export records yet."
    else:
        for rid, fmt, scope, count, ts in rows:
            text += f"📤 <b>#{rid}</b> [{fmt.upper()}] scope={scope} — {ts}\n   {count} rows\n\n"

    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bpim:history:export:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bpim:history:export:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ═════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═════════════════════════════════════════════════════════════════════════

async def bpim_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    status = _mgr_status()
    max_import = cfg.get_int("bulk_product_import_max_rows", 1000)
    del_confirm = cfg.get_bool("bulk_product_delete_confirm", True)
    action_log = cfg.get_bool("bulk_product_action_log_enabled", True)

    text = (
        f"⚙️ <b>BULK PRODUCT MANAGER — SETTINGS</b>\n\n"
        f"Status: {_STATUS_EMOJI.get(status, '⚪')} {status.title()}\n"
        f"Max Import Rows: <b>{max_import}</b>\n"
        f"Delete Confirmation: {'✅' if del_confirm else '⚪'}\n"
        f"Action Logging: {'✅' if action_log else '⚪'}\n"
    )
    kb = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="bpim:set:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="bpim:set:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="bpim:set:status:disabled")],
        [InlineKeyboardButton(
            "✅ Del. Confirm: ON" if del_confirm else "⚪ Del. Confirm: OFF",
            callback_data="bpim:set:toggle:bulk_product_delete_confirm",
        )],
        [InlineKeyboardButton(
            "✅ Action Log: ON" if action_log else "⚪ Action Log: OFF",
            callback_data="bpim:set:toggle:bulk_product_action_log_enabled",
        )],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bpim_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    parts = (query.data or "").split(":")
    new_status = parts[3] if len(parts) > 3 else "enabled"
    cfg.set("bulk_product_manager_status", new_status)
    log_admin_action(uid, "bulk_product_manager_status_change", target_type="config",
                     new_value=new_status, module="bulk_products")
    await bpim_settings(update, context)


async def bpim_set_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    parts = (query.data or "").split(":")
    key = parts[3] if len(parts) > 3 else ""
    if key:
        current = cfg.get_bool(key, True)
        cfg.set(key, "false" if current else "true")
        log_admin_action(uid, "bulk_product_config_toggle", target_type="config",
                         target_id=key, new_value=str(not current), module="bulk_products")
    await bpim_settings(update, context)


# ═════════════════════════════════════════════════════════════════════════
# CONVERSATIONHANDLER BUILDER
# ═════════════════════════════════════════════════════════════════════════

def build_bpim_import_conv() -> ConversationHandler:
    """Build the import ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bpim_import_start, pattern=r"^bpim:import:start:"),
        ],
        states={
            BPIM_WAIT_FILE: [
                MessageHandler(filters.Document.ALL, bpim_import_receive_file),
                CallbackQueryHandler(bpim_import_cancel, pattern=r"^bpim:import:cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bpim_import_cancel, pattern=r"^bpim:import:cancel$"),
        ],
        per_chat=True,
        per_user=True,
        name="bpim_import_conv",
        persistent=False,
    )


def build_bpim_bulk_conv() -> ConversationHandler:
    """Build the bulk action ConversationHandler (for price/stock input)."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bpim_bulk_confirm, pattern=r"^bpim:bulk:(price|stock):"),
        ],
        states={
            BPIM_WAIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bpim_bulk_price_receive),
                CallbackQueryHandler(bpim_import_cancel, pattern=r"^bpim:bulk:cancel$"),
            ],
            BPIM_WAIT_STOCK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bpim_bulk_stock_receive),
                CallbackQueryHandler(bpim_import_cancel, pattern=r"^bpim:bulk:cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bpim_import_cancel, pattern=r"^bpim:bulk:cancel$"),
        ],
        per_chat=True,
        per_user=True,
        name="bpim_bulk_conv",
        persistent=False,
    )


def register_handlers(application) -> None:
    """Register all bpim:* handlers on the Application."""
    from telegram.ext import CallbackQueryHandler as CQH

    # ConversationHandlers (must come first)
    application.add_handler(build_bpim_import_conv())
    application.add_handler(build_bpim_bulk_conv())

    # Plain callback handlers
    application.add_handler(CQH(bpim_menu,             pattern=r"^bpim:menu$"))
    application.add_handler(CQH(bpim_import_menu,      pattern=r"^bpim:import:menu$"))
    application.add_handler(CQH(bpim_import_template,  pattern=r"^bpim:import:template$"))
    application.add_handler(CQH(bpim_export_menu,      pattern=r"^bpim:export:menu$"))
    application.add_handler(CQH(bpim_export_catsel,    pattern=r"^bpim:export:catsel$"))
    application.add_handler(CQH(bpim_export_scope,     pattern=r"^bpim:export:scope:"))
    application.add_handler(CQH(bpim_export_do,        pattern=r"^bpim:export:do:"))
    application.add_handler(CQH(bpim_bulk_menu,        pattern=r"^bpim:bulk:menu$"))
    application.add_handler(CQH(bpim_bulk_scope_select,pattern=r"^bpim:bulk:(enable|disable|clone|delete|category|dtype):scope$"))
    application.add_handler(CQH(bpim_bulk_confirm,     pattern=r"^bpim:bulk:(enable|disable|clone):"))
    application.add_handler(CQH(bpim_bulk_exec,        pattern=r"^bpim:bulk:(delete|category|dtype):(exec|confirm):"))
    application.add_handler(CQH(bpim_history_import,   pattern=r"^bpim:history:import:"))
    application.add_handler(CQH(bpim_history_export,   pattern=r"^bpim:history:export:"))
    application.add_handler(CQH(bpim_settings,         pattern=r"^bpim:settings$"))
    application.add_handler(CQH(bpim_set_status,       pattern=r"^bpim:set:status:"))
    application.add_handler(CQH(bpim_set_toggle,       pattern=r"^bpim:set:toggle:"))
