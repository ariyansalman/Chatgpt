"""Supplier management sub-panel — list / view / add / toggle."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler

from database import get_db_session
from database.models import (
    Supplier, InventoryBatch, ProductKey, InventoryIssue,
)
from utils.audit import log_admin_action
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send, fmt_money

ADD_NAME, ADD_CONTACT, ADD_NOTES = range(9200, 9203)
EDIT_FIELD, EDIT_VALUE = range(9210, 9212)


@require_admin
async def suppliers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    with get_db_session() as s:
        rows = s.query(Supplier).order_by(Supplier.is_active.desc(),
                                          Supplier.name.asc()).all()
    slice_, page, pages = paginate(rows, page)
    lines = ["🏭 <b>SUPPLIERS</b>", f"Total: {len(rows)}", ""]
    kb: list = []
    for sup in slice_:
        badge = "🟢" if sup.is_active else "⚪"
        kb.append([InlineKeyboardButton(f"{badge} {sup.name}",
                                        callback_data=f"acc:sup:view:{sup.id}")])
    if pages > 1:
        kb.append(nav_row("sup", page, pages))
    kb.append([InlineKeyboardButton("➕ Add supplier", callback_data="acc:sup:add")])
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


def _sup_stats(s, sup_id: int) -> dict:
    batches = s.query(InventoryBatch).filter_by(supplier_id=sup_id).all()
    batch_ids = [b.id for b in batches]
    total_qty = sum(b.quantity_imported or 0 for b in batches)
    total_cost = sum(b.total_cost or 0 for b in batches)
    if batch_ids:
        sold = (s.query(func.count(ProductKey.id))
                 .filter(ProductKey.batch_id.in_(batch_ids),
                         ProductKey.is_sold.is_(True)).scalar() or 0)
    else:
        sold = 0
    invalid = (s.query(func.count(InventoryIssue.id))
                .filter(InventoryIssue.supplier_id == sup_id,
                        InventoryIssue.issue_type.in_(
                            ("INVALID", "DUPLICATE", "EXPIRED", "DELIVERY_FAILED"))
                        ).scalar() or 0)
    replaced = (s.query(func.count(InventoryIssue.id))
                 .filter(InventoryIssue.supplier_id == sup_id,
                         InventoryIssue.issue_type == "REPLACED").scalar() or 0)
    fail_rate = (invalid / total_qty * 100.0) if total_qty else 0.0
    return dict(batches=len(batches), total_qty=total_qty, total_cost=total_cost,
                sold=sold, invalid=invalid, replaced=replaced, fail_rate=fail_rate)


async def _view(update, sup_id: int):
    with get_db_session() as s:
        sup = s.get(Supplier, sup_id)
        if not sup:
            await send(update, "Not found.", InlineKeyboardMarkup([[back_root()]]))
            return
        st = _sup_stats(s, sup.id)
        # V24 — Auto Assignment stats
        from services.supplier_auto_assign import supplier_stats as sas_stats
        sas = sas_stats(sup.id, s)
        last_act = sas.get("last_activity")
        last_act_str = last_act.strftime("%Y-%m-%d %H:%M") if last_act else "—"
    t = [
        f"🏭 <b>{sup.name}</b>",
        f"Status: {'🟢 Active' if sup.is_active else '⚪ Disabled'}",
        f"Contact: {sup.contact or '—'}",
        f"Telegram: @{sup.telegram_username}" if sup.telegram_username else "Telegram: —",
        f"Notes: {sup.notes or '—'}",
        "",
        f"Batches: <b>{st['batches']}</b>",
        f"Units imported: <b>{st['total_qty']}</b>",
        f"Total cost: <b>{fmt_money(st['total_cost'])}</b>",
        f"Sold: <b>{st['sold']}</b>",
        f"Invalid/failed: <b>{st['invalid']}</b>",
        f"Replacements: <b>{st['replaced']}</b>",
        f"Failure rate: <b>{st['fail_rate']:.2f}%</b>",
        "",
        "── <b>Auto Assignment</b> ──",
        f"Priority: <b>{sas.get('priority', 10)}</b>  (lower = first)",
        f"Product Assignments: <b>{sas.get('assignment_count', 0)}</b>",
        f"Available Stock (auto): <b>{sas.get('available_stock', 0)} keys</b>",
        f"Delivered: <b>{sas.get('total_delivered', 0)}</b>  "
        f"Failed: <b>{sas.get('total_failed', 0)}</b>  "
        f"Success: <b>{sas.get('success_rate', 100.0):.1f}%</b>",
        f"Last Activity: <b>{last_act_str}</b>",
    ]
    kb = [
        [InlineKeyboardButton(
            "🚫 Disable" if sup.is_active else "✅ Enable",
            callback_data=f"acc:sup:toggle:{sup.id}")],
        [InlineKeyboardButton("🤖 Auto Assign Settings",
                              callback_data=f"acc:sas:sup:{sup.id}")],
        [InlineKeyboardButton("⬅️ Suppliers", callback_data="acc:sup:list:0"),
         back_root()],
    ]
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


async def _toggle(update, sup_id: int):
    with get_db_session() as s:
        sup = s.get(Supplier, sup_id)
        if not sup:
            return
        sup.is_active = not sup.is_active
        s.commit()
    try:
        log_admin_action(update.effective_user.id,
                         "supplier_toggled", f"supplier_id={sup_id}")
    except Exception:
        pass
    await _view(update, sup_id)


# ── Add supplier conversation ──────────────────────────────────────────

@require_admin
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Enter supplier name (or /cancel):")
    return ADD_NAME


async def add_name(update, context):
    context.user_data["sup_new"] = {"name": update.message.text.strip()}
    await update.message.reply_text("Contact info (email/phone/URL, or '-' to skip):")
    return ADD_CONTACT


async def add_contact(update, context):
    v = update.message.text.strip()
    context.user_data["sup_new"]["contact"] = None if v == "-" else v
    await update.message.reply_text("Notes (or '-' to skip):")
    return ADD_NOTES


async def add_notes(update, context):
    v = update.message.text.strip()
    d = context.user_data.pop("sup_new")
    d["notes"] = None if v == "-" else v
    with get_db_session() as s:
        sup = Supplier(name=d["name"], contact=d.get("contact"),
                       notes=d.get("notes"), is_active=True,
                       created_at=datetime.utcnow(),
                       updated_at=datetime.utcnow())
        s.add(sup); s.commit(); s.refresh(sup)
    try:
        log_admin_action(update.effective_user.id,
                         "supplier_created", f"supplier_id={sup.id} name={d['name']}")
    except Exception:
        pass
    await update.message.reply_text(f"✅ Supplier '{d['name']}' created (id={sup.id}).")
    return ConversationHandler.END


async def add_cancel(update, context):
    context.user_data.pop("sup_new", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# Router entry from acc dispatcher
async def route(action, rest, update, context):
    if action == "list":
        page = int(rest[0]) if rest else 0
        await suppliers_menu(update, context, page=page)
    elif action == "view" and rest:
        await _view(update, int(rest[0]))
    elif action == "toggle" and rest:
        await _toggle(update, int(rest[0]))


def build_supplier_add_conv():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^acc:sup:add$")],
        states={
            ADD_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_contact)],
            ADD_NOTES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
