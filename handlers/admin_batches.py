"""Inventory Batch management — list / view / import.

Batch import here is a batch-record-only helper (creates a batch row, does
not itself bulk-load ProductKey rows — that continues to use the existing
inventory import flow). To retro-link, admin can attach existing unlinked
ProductKey rows for a product to a batch.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler

from database import get_db_session
from database.models import (
    InventoryBatch, Supplier, Product, ProductKey, InventoryIssue, OrderItem,
)
from utils.audit import log_admin_action
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send, fmt_money

B_PRODUCT, B_QTY, B_COST, B_SUPPLIER, B_NOTES = range(9300, 9305)


def _gen_ref(product: Product) -> str:
    prefix = "".join(c for c in (product.name or "BAT").upper() if c.isalnum())[:3] or "BAT"
    d = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{d}"


@require_admin
async def batches_menu(update, context, page: int = 0):
    with get_db_session() as s:
        rows = s.query(InventoryBatch).order_by(InventoryBatch.created_at.desc()).all()
    slice_, page, pages = paginate(rows, page)
    kb = []
    lines = ["📦 <b>INVENTORY BATCHES</b>",
             f"Total: {len(rows)}", ""]
    for b in slice_:
        kb.append([InlineKeyboardButton(
            f"{b.reference} · qty {b.quantity_imported}",
            callback_data=f"acc:bat:view:{b.id}")])
    if pages > 1:
        kb.append(nav_row("bat", page, pages))
    kb.append([InlineKeyboardButton("➕ New batch", callback_data="acc:bat:add")])
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _view(update, bat_id: int):
    with get_db_session() as s:
        b = s.get(InventoryBatch, bat_id)
        if not b:
            await send(update, "Not found.", InlineKeyboardMarkup([[back_root()]]))
            return
        product = s.get(Product, b.product_id)
        supplier = s.get(Supplier, b.supplier_id) if b.supplier_id else None
        linked = s.query(func.count(ProductKey.id)).filter(ProductKey.batch_id == b.id).scalar() or 0
        sold = (s.query(func.count(ProductKey.id))
                 .filter(ProductKey.batch_id == b.id,
                         ProductKey.is_sold.is_(True)).scalar() or 0)
        available = linked - sold
        issues = (s.query(func.count(InventoryIssue.id))
                   .filter(InventoryIssue.batch_id == b.id).scalar() or 0)
        # Revenue from sold keys in this batch (uses OrderItem base_price snapshot when set)
        revenue = 0.0
        cogs = 0.0
        sold_keys = s.query(ProductKey).filter(
            ProductKey.batch_id == b.id, ProductKey.is_sold.is_(True)).all()
        for k in sold_keys:
            cogs += float(k.cost_per_unit_snapshot or b.cost_per_unit or 0)
            if k.order_id:
                oi = s.query(OrderItem).filter(OrderItem.order_id == k.order_id,
                                               OrderItem.product_id == k.product_id).first()
                if oi:
                    revenue += float(oi.price or 0) / max(1, int(oi.quantity or 1))
        gross = revenue - cogs

    t = [
        f"📦 <b>Batch {b.reference}</b>",
        f"Product: {product.name if product else b.product_id}",
        f"Supplier: {supplier.name if supplier else '—'}",
        f"Imported: <b>{b.quantity_imported}</b>",
        f"Linked ProductKeys: <b>{linked}</b>",
        f"Available: <b>{available}</b>  ·  Sold: <b>{sold}</b>",
        f"Issues: <b>{issues}</b>",
        f"Cost/unit: <b>{fmt_money(b.cost_per_unit)}</b>  ·  Total: <b>{fmt_money(b.total_cost)}</b>",
        f"Revenue (est): <b>{fmt_money(revenue)}</b>",
        f"COGS (est): <b>{fmt_money(cogs)}</b>",
        f"Gross profit (est): <b>{fmt_money(gross)}</b>",
    ]
    kb = [[InlineKeyboardButton("⬅️ Batches", callback_data="acc:bat:list:0"), back_root()]]
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


# ── New batch conversation ─────────────────────────────────────────────

@require_admin
async def add_start(update, context):
    q = update.callback_query; await q.answer()
    with get_db_session() as s:
        products = s.query(Product).order_by(Product.name.asc()).limit(50).all()
    if not products:
        await q.message.reply_text("No products exist yet.")
        return ConversationHandler.END
    lines = ["Reply with the product ID to receive this batch:", ""]
    for p in products[:30]:
        lines.append(f"  <code>{p.id}</code> — {p.name}")
    await q.message.reply_text("\n".join(lines), parse_mode="HTML")
    return B_PRODUCT


async def b_product(update, context):
    try:
        pid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a numeric product id.")
        return B_PRODUCT
    with get_db_session() as s:
        p = s.get(Product, pid)
    if not p:
        await update.message.reply_text("Not found.")
        return B_PRODUCT
    context.user_data["bat_new"] = {"product_id": pid, "product_name": p.name}
    await update.message.reply_text("Quantity imported (integer):")
    return B_QTY


async def b_qty(update, context):
    try:
        q = int(update.message.text.strip())
        if q <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a positive integer.")
        return B_QTY
    context.user_data["bat_new"]["qty"] = q
    await update.message.reply_text("Cost per unit (e.g. 1.25):")
    return B_COST


async def b_cost(update, context):
    try:
        c = float(update.message.text.strip())
        if c < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a non-negative number.")
        return B_COST
    context.user_data["bat_new"]["cost"] = c
    with get_db_session() as s:
        sups = s.query(Supplier).filter_by(is_active=True).order_by(Supplier.name.asc()).limit(30).all()
    if not sups:
        context.user_data["bat_new"]["supplier_id"] = None
        await update.message.reply_text("No active suppliers. Notes (or '-' to skip):")
        return B_NOTES
    lines = ["Supplier id (or '-' for none):"]
    for s_ in sups:
        lines.append(f"  <code>{s_.id}</code> — {s_.name}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return B_SUPPLIER


async def b_supplier(update, context):
    v = update.message.text.strip()
    if v == "-":
        context.user_data["bat_new"]["supplier_id"] = None
    else:
        try:
            context.user_data["bat_new"]["supplier_id"] = int(v)
        except ValueError:
            await update.message.reply_text("Enter numeric id or '-'.")
            return B_SUPPLIER
    await update.message.reply_text("Notes (or '-' to skip):")
    return B_NOTES


async def b_notes(update, context):
    v = update.message.text.strip()
    d = context.user_data.pop("bat_new")
    d["notes"] = None if v == "-" else v
    with get_db_session() as s:
        p = s.get(Product, d["product_id"])
        ref = _gen_ref(p) + f"-{int(datetime.utcnow().timestamp()) % 1000:03d}"
        bat = InventoryBatch(
            reference=ref, product_id=d["product_id"],
            supplier_id=d.get("supplier_id"),
            quantity_imported=d["qty"],
            cost_per_unit=d["cost"],
            total_cost=d["qty"] * d["cost"],
            import_source="manual", notes=d.get("notes"),
            created_by=update.effective_user.id,
            created_at=datetime.utcnow(),
        )
        s.add(bat); s.commit(); s.refresh(bat)
    try:
        log_admin_action(update.effective_user.id,
                         "batch_created",
                         f"batch_id={bat.id} ref={bat.reference} qty={bat.quantity_imported}")
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Batch {bat.reference} created.\n"
        f"Attach product keys via the existing inventory import — new keys can be "
        f"linked to this batch by setting their batch_id to {bat.id}."
    )
    return ConversationHandler.END


async def b_cancel(update, context):
    context.user_data.pop("bat_new", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def route(action, rest, update, context):
    if action == "list":
        page = int(rest[0]) if rest else 0
        await batches_menu(update, context, page=page)
    elif action == "view" and rest:
        await _view(update, int(rest[0]))


def build_batch_add_conv():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^acc:bat:add$")],
        states={
            B_PRODUCT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, b_product)],
            B_QTY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, b_qty)],
            B_COST:     [MessageHandler(filters.TEXT & ~filters.COMMAND, b_cost)],
            B_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, b_supplier)],
            B_NOTES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, b_notes)],
        },
        fallbacks=[CommandHandler("cancel", b_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
