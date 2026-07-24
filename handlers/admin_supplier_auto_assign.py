"""V24 — Supplier Auto Assignment admin panel.

Callback namespace:  ``acc:sas:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:sas``

Sub-actions:
    acc:sas:menu               → global settings + stats overview
    acc:sas:toggle             → enable/disable entire feature
    acc:sas:fallback:on|off    → toggle fallback-to-any-supplier
    acc:sas:list:<page>        → paginated supplier-product assignment list
    acc:sas:sup:<sup_id>       → per-supplier assignment detail
    acc:sas:addprod:<sup_id>   → begin "add product to supplier" conversation
    acc:sas:rmprod:<sp_id>     → remove a supplier-product assignment
    acc:sas:prio:<sp_id>:<n>   → set priority on a supplier-product assignment
    acc:sas:toggle_assign:<sp_id>  → toggle is_auto_assign on a supplier-product assignment
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ContextTypes, ConversationHandler, MessageHandler,
    CommandHandler, CallbackQueryHandler, filters,
)

from database import get_db_session
from database.models import Supplier, SupplierProduct, Product
from services import supplier_auto_assign as svc
from utils.audit import log_admin_action
from utils.bot_config import cfg
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send, fmt_money

logger = logging.getLogger(__name__)

# ConversationHandler states
SAS_PICK_SUPPLIER, SAS_PICK_PRODUCT, SAS_PICK_PRIORITY = range(9300, 9303)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    return cfg.get_bool("sas_enabled", True)


def _fallback() -> bool:
    return cfg.get_bool("sas_fallback_to_any", True)


def _back_sas() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Auto Assign", callback_data="acc:sas:menu")


# ─────────────────────────────────────────────────────────────────────────
# Global Settings Panel
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def sas_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled = _enabled()
    fallback = _fallback()

    with get_db_session() as s:
        total_assignments = s.query(SupplierProduct).filter(
            SupplierProduct.is_active.is_(True)
        ).count()
        active_suppliers = s.query(Supplier).filter(
            Supplier.is_active.is_(True)
        ).count()
        total_suppliers = s.query(Supplier).count()

    lines = [
        "🤖 <b>SUPPLIER AUTO ASSIGNMENT</b>  (V24)",
        "",
        f"<b>Feature:</b>  {'🟢 Enabled' if enabled else '🔴 Disabled'}",
        f"<b>Fallback to any supplier:</b>  {'✅ ON' if fallback else '🚫 OFF'}",
        "",
        "<b>Overview:</b>",
        f"  • Active suppliers:       <b>{active_suppliers}</b> / {total_suppliers}",
        f"  • Active assignments:     <b>{total_assignments}</b>",
        "",
        "Auto Assignment selects which supplier's keys to fulfill each order.",
        "Lower priority number = higher preference.",
        "Users never see supplier information.",
    ]

    kb = [
        [InlineKeyboardButton(
            "🔴 Disable" if enabled else "🟢 Enable",
            callback_data="acc:sas:toggle",
        )],
        [
            InlineKeyboardButton(
                f"{'✅' if fallback else '🚫'} Fallback to any supplier",
                callback_data=f"acc:sas:fallback:{'off' if fallback else 'on'}",
            ),
        ],
        [InlineKeyboardButton("📋 View All Assignments", callback_data="acc:sas:list:0")],
        [InlineKeyboardButton("🏭 Manage Suppliers", callback_data="acc:sec:suppliers")],
        [back_root()],
    ]

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Toggle & Settings
# ─────────────────────────────────────────────────────────────────────────

async def _toggle_feature(update, context):
    new_val = not _enabled()
    cfg.set("sas_enabled", new_val)
    try:
        log_admin_action(update.effective_user.id, "sas_toggle",
                         f"sas_enabled={new_val}")
    except Exception:
        pass
    await sas_menu(update, context)


async def _toggle_fallback(update, context, value: str):
    new_val = value == "on"
    cfg.set("sas_fallback_to_any", new_val)
    await sas_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Assignment List
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def sas_assignment_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    with get_db_session() as s:
        rows = (
            s.query(SupplierProduct)
            .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
            .join(Product, Product.id == SupplierProduct.product_id)
            .order_by(
                SupplierProduct.priority.asc(),
                Supplier.name.asc(),
                Product.name.asc(),
            )
            .all()
        )
        # Eagerly load names while session is open
        entries = []
        for sp in rows:
            sup_name = sp.supplier.name if sp.supplier else f"#{sp.supplier_id}"
            prod_name = sp.product.name if sp.product else f"#{sp.product_id}"
            entries.append((sp.id, sp.supplier_id, sp.priority,
                            sp.is_auto_assign, sp.is_active,
                            sup_name, prod_name))

    slice_, page, pages = paginate(entries, page, per_page=8)
    lines = [f"📋 <b>SUPPLIER ASSIGNMENTS</b>  ({len(entries)} total)", ""]
    kb: list = []
    for sp_id, sup_id, prio, auto, active, sup_name, prod_name in slice_:
        badges = []
        if not active:
            badges.append("⚪")
        elif auto:
            badges.append("🟢")
        else:
            badges.append("🟡")
        label = f"{''.join(badges)} P{prio} · {sup_name} → {prod_name[:25]}"
        kb.append([InlineKeyboardButton(label, callback_data=f"acc:sas:detail:{sp_id}")])

    if pages > 1:
        kb.append(nav_row("sas_list", page, pages))
    kb.append([_back_sas()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Assignment Detail (single SupplierProduct row)
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def sas_assignment_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, sp_id: int):
    with get_db_session() as s:
        sp = s.get(SupplierProduct, sp_id)
        if not sp:
            await send(update, "Assignment not found.", InlineKeyboardMarkup([[_back_sas()]]))
            return
        sup_name = sp.supplier.name if sp.supplier else f"#{sp.supplier_id}"
        prod_name = sp.product.name if sp.product else f"#{sp.product_id}"
        prio = sp.priority
        auto = sp.is_auto_assign
        active = sp.is_active
        sup_id = sp.supplier_id

        # Available stock for this supplier/product
        avail = svc.count_supplier_stock(sup_id, sp.product_id, sp.variant_id, s)

    lines = [
        f"🔗 <b>Assignment Detail</b>",
        f"Supplier:  <b>{sup_name}</b>",
        f"Product:   <b>{prod_name}</b>",
        f"Priority:  <b>{prio}</b>  (lower = first)",
        f"Auto Assign: <b>{'✅ ON' if auto else '🚫 OFF'}</b>",
        f"Status:    <b>{'🟢 Active' if active else '⚪ Inactive'}</b>",
        f"Available Stock: <b>{avail} keys</b>",
    ]

    prio_row = [
        InlineKeyboardButton(f"{'✅ ' if prio == v else ''}P{v}",
                             callback_data=f"acc:sas:prio:{sp_id}:{v}")
        for v in (1, 5, 10, 20, 50)
    ]

    kb = [
        prio_row,
        [InlineKeyboardButton(
            "🚫 Disable Auto Assign" if auto else "✅ Enable Auto Assign",
            callback_data=f"acc:sas:toggle_assign:{sp_id}",
        )],
        [InlineKeyboardButton(
            "⚪ Deactivate" if active else "🟢 Activate",
            callback_data=f"acc:sas:activate:{sp_id}:{0 if active else 1}",
        )],
        [InlineKeyboardButton("🗑 Remove Assignment", callback_data=f"acc:sas:rmprod:{sp_id}")],
        [InlineKeyboardButton("⬅️ Assignments", callback_data="acc:sas:list:0"),
         _back_sas()],
    ]
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Per-supplier Assignment View (from suppliers panel)
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def sas_supplier_view(update: Update, context: ContextTypes.DEFAULT_TYPE, sup_id: int):
    """Show all product assignments for a single supplier."""
    with get_db_session() as s:
        sup = s.get(Supplier, sup_id)
        if not sup:
            await send(update, "Supplier not found.", InlineKeyboardMarkup([[back_root()]]))
            return
        sup_name = sup.name

        assignments = (
            s.query(SupplierProduct)
            .filter(SupplierProduct.supplier_id == sup_id)
            .join(Product, Product.id == SupplierProduct.product_id)
            .order_by(SupplierProduct.priority.asc(), Product.name.asc())
            .all()
        )
        entries = []
        for sp in assignments:
            pname = sp.product.name if sp.product else f"#{sp.product_id}"
            entries.append((sp.id, sp.priority, sp.is_auto_assign, sp.is_active, pname))

        stats = svc.supplier_stats(sup_id, s)

    lines = [
        f"🏭 <b>{sup_name}</b> — Auto Assign",
        "",
        f"Global priority: <b>{stats.get('priority', 10)}</b>",
        f"Available stock: <b>{stats.get('available_stock', 0)} keys</b>",
        f"Delivered: <b>{stats.get('total_delivered', 0)}</b>  "
        f"Failed: <b>{stats.get('total_failed', 0)}</b>  "
        f"Rate: <b>{stats.get('success_rate', 100):.1f}%</b>",
        f"Product assignments: <b>{len(entries)}</b>",
    ]

    kb: list = []
    for sp_id, prio, auto, active, pname in entries:
        badge = "🟢" if (auto and active) else ("🟡" if active else "⚪")
        kb.append([InlineKeyboardButton(
            f"{badge} P{prio} · {pname[:30]}",
            callback_data=f"acc:sas:detail:{sp_id}",
        )])

    kb.append([InlineKeyboardButton(
        "➕ Assign Product",
        callback_data=f"acc:sas:addprod:{sup_id}",
    )])
    kb.append([InlineKeyboardButton("⬅️ Supplier", callback_data=f"acc:sup:view:{sup_id}"),
               _back_sas()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Remove assignment
# ─────────────────────────────────────────────────────────────────────────

async def _remove_assignment(update, context, sp_id: int):
    with get_db_session() as s:
        sp = s.get(SupplierProduct, sp_id)
        if not sp:
            await sas_menu(update, context)
            return
        sup_id = sp.supplier_id
        s.delete(sp)
        s.commit()
    try:
        log_admin_action(update.effective_user.id, "sas_assignment_removed",
                         f"supplier_product_id={sp_id}")
    except Exception:
        pass
    await sas_supplier_view(update, context, sup_id)


# ─────────────────────────────────────────────────────────────────────────
# Priority setter
# ─────────────────────────────────────────────────────────────────────────

async def _set_priority(update, context, sp_id: int, priority: int):
    with get_db_session() as s:
        sp = s.get(SupplierProduct, sp_id)
        if not sp:
            await sas_menu(update, context)
            return
        sp.priority = priority
        s.commit()
    await sas_assignment_detail(update, context, sp_id)


# ─────────────────────────────────────────────────────────────────────────
# Toggle auto-assign flag
# ─────────────────────────────────────────────────────────────────────────

async def _toggle_assignment(update, context, sp_id: int):
    with get_db_session() as s:
        sp = s.get(SupplierProduct, sp_id)
        if not sp:
            await sas_menu(update, context)
            return
        sp.is_auto_assign = not sp.is_auto_assign
        s.commit()
    await sas_assignment_detail(update, context, sp_id)


# ─────────────────────────────────────────────────────────────────────────
# Activate/deactivate assignment
# ─────────────────────────────────────────────────────────────────────────

async def _set_active(update, context, sp_id: int, active: bool):
    with get_db_session() as s:
        sp = s.get(SupplierProduct, sp_id)
        if not sp:
            await sas_menu(update, context)
            return
        sp.is_active = active
        s.commit()
    await sas_assignment_detail(update, context, sp_id)


# ─────────────────────────────────────────────────────────────────────────
# "Add Product to Supplier" conversation
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def addprod_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: user tapped 'Add Product' on a supplier's auto-assign view."""
    q = update.callback_query
    await q.answer()
    # Parse sup_id from callback_data "acc:sas:addprod:<sup_id>"
    parts = q.data.split(":")
    sup_id = int(parts[-1]) if parts[-1].isdigit() else None
    if not sup_id:
        await q.message.reply_text("Invalid supplier. Try again.")
        return ConversationHandler.END
    context.user_data["sas_sup_id"] = sup_id
    await q.message.reply_text(
        "Enter the <b>Product ID</b> to assign to this supplier "
        "(or /cancel to abort):",
        parse_mode="HTML",
    )
    return SAS_PICK_PRODUCT


async def addprod_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Please enter a numeric Product ID, or /cancel.")
        return SAS_PICK_PRODUCT
    product_id = int(text)
    with get_db_session() as s:
        prod = s.get(Product, product_id)
        if not prod:
            await update.message.reply_text(f"No product found with ID {product_id}. Try again:")
            return SAS_PICK_PRODUCT
        prod_name = prod.name
    context.user_data["sas_product_id"] = product_id
    context.user_data["sas_product_name"] = prod_name
    await update.message.reply_text(
        f"Product: <b>{prod_name}</b>\n\n"
        "Enter priority (1–99, lower = higher priority, default 10):",
        parse_mode="HTML",
    )
    return SAS_PICK_PRIORITY


async def addprod_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    priority = 10
    if text.isdigit():
        priority = max(1, min(99, int(text)))

    sup_id = context.user_data.pop("sas_sup_id", None)
    product_id = context.user_data.pop("sas_product_id", None)
    prod_name = context.user_data.pop("sas_product_name", "")

    if not sup_id or not product_id:
        await update.message.reply_text("Session expired. Please start over.")
        return ConversationHandler.END

    with get_db_session() as s:
        # Check for existing assignment
        existing = s.query(SupplierProduct).filter_by(
            supplier_id=sup_id, product_id=product_id, variant_id=None
        ).first()
        if existing:
            existing.priority = priority
            existing.is_active = True
            existing.is_auto_assign = True
            s.commit()
            await update.message.reply_text(
                f"✅ Updated existing assignment for <b>{prod_name}</b> with priority {priority}.",
                parse_mode="HTML",
            )
        else:
            sp = SupplierProduct(
                supplier_id=sup_id,
                product_id=product_id,
                variant_id=None,
                priority=priority,
                is_auto_assign=True,
                is_active=True,
            )
            s.add(sp)
            s.commit()
            await update.message.reply_text(
                f"✅ Assigned <b>{prod_name}</b> to this supplier with priority {priority}.",
                parse_mode="HTML",
            )
    try:
        log_admin_action(update.effective_user.id, "sas_assignment_created",
                         f"supplier_id={sup_id} product_id={product_id} priority={priority}")
    except Exception:
        pass
    return ConversationHandler.END


async def addprod_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("sas_sup_id", None)
    context.user_data.pop("sas_product_id", None)
    context.user_data.pop("sas_product_name", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_sas_addprod_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(addprod_start, pattern=r"^acc:sas:addprod:\d+$")],
        states={
            SAS_PICK_PRODUCT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addprod_product)],
            SAS_PICK_PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addprod_priority)],
        },
        fallbacks=[CommandHandler("cancel", addprod_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────
# Router — entry point from admin_control_center ``acc:sas:*``
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await sas_menu(update, context)
        return

    if action == "toggle":
        await _toggle_feature(update, context)
        return

    if action == "fallback" and rest:
        await _toggle_fallback(update, context, rest[0])
        return

    if action == "list":
        page = int(rest[0]) if rest and rest[0].isdigit() else 0
        await sas_assignment_list(update, context, page)
        return

    if action == "sup" and rest:
        await sas_supplier_view(update, context, int(rest[0]))
        return

    if action == "detail" and rest:
        await sas_assignment_detail(update, context, int(rest[0]))
        return

    if action == "rmprod" and rest:
        await _remove_assignment(update, context, int(rest[0]))
        return

    if action == "prio" and len(rest) >= 2:
        try:
            await _set_priority(update, context, int(rest[0]), int(rest[1]))
        except (ValueError, IndexError):
            await sas_menu(update, context)
        return

    if action == "toggle_assign" and rest:
        await _toggle_assignment(update, context, int(rest[0]))
        return

    if action == "activate" and len(rest) >= 2:
        await _set_active(update, context, int(rest[0]), rest[1] == "1")
        return

    # Paginated assignment list with sas_list nav prefix
    if action == "sas_list" and rest:
        page = int(rest[0]) if rest[0].isdigit() else 0
        await sas_assignment_list(update, context, page)
        return

    await sas_menu(update, context)
