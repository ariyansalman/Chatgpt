"""Admin Bundle Manager.

Callback namespace: abn:*

Manages products of type BUNDLE and their child products (BundleItem rows).
Admin can:
  • View all bundle products
  • Set bundle price, discount percent, description
  • Add / remove child products
  • Set bundle stock quantity

Conversation states:
    ABN_BUNDLE_PRICE    (5500) — entering bundle price override
    ABN_BUNDLE_DISCOUNT (5501) — entering bundle discount percent
    ABN_CHILD_ID        (5502) — entering child product ID to add
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, Product, ProductType
from database.models import BundleItem
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

ABN_BUNDLE_PRICE    = 5500
ABN_BUNDLE_DISCOUNT = 5501
ABN_CHILD_ID        = 5502

_PER_PAGE = 8


# ─────────────────────────────────────────────────────────────────────────────
# Main bundle list
# ─────────────────────────────────────────────────────────────────────────────

async def bundle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all bundle products: abn:menu or acc:bundles:menu"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    with get_db_session() as s:
        bundles = (
            s.query(Product)
            .filter_by(product_type=ProductType.BUNDLE, is_active=True)
            .order_by(Product.name)
            .all()
        )
        rows = []
        for b in bundles:
            child_count = s.query(BundleItem).filter_by(parent_product_id=b.id).count()
            rows.append({
                "id":          b.id,
                "name":        b.name,
                "price":       b.price,
                "bundle_price": b.bundle_price,
                "bundle_discount": b.bundle_discount_percent,
                "stock":       b.stock_count,
                "children":    child_count,
            })

    if not rows:
        text = (
            "📦 <b>Bundle Manager</b>\n\n"
            "No bundle products found.\n"
            "Create a product with type <b>Bundle</b> from the Products panel first."
        )
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="acc:root")]]
    else:
        text = f"📦 <b>Bundle Manager</b> ({len(rows)} bundles)\n\n"
        kb = []
        for r in rows:
            price_str = f"${r['bundle_price']:.2f}" if r["bundle_price"] else f"${r['price']:.2f}"
            disc_str  = f" ({r['bundle_discount']:.0f}% off)" if r["bundle_discount"] else ""
            kb.append([InlineKeyboardButton(
                f"📦 {r['name']} — {price_str}{disc_str} | {r['children']} items",
                callback_data=f"abn:view:{r['id']}"
            )])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# View single bundle
# ─────────────────────────────────────────────────────────────────────────────

async def bundle_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View bundle details and child products: abn:view:<bundle_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        bundle_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await bundle_menu(update, context)
        return

    with get_db_session() as s:
        bundle = s.query(Product).filter_by(id=bundle_id).first()
        if not bundle:
            await query.answer("❌ Bundle not found.", show_alert=True)
            return

        items = (
            s.query(BundleItem)
            .filter_by(parent_product_id=bundle_id)
            .order_by(BundleItem.display_order, BundleItem.id)
            .all()
        )
        children = []
        individual_total = 0.0
        for item in items:
            child = s.query(Product).filter_by(id=item.child_product_id).first()
            if child:
                children.append({
                    "item_id": item.id,
                    "child_id": child.id,
                    "name":  child.name,
                    "price": child.price,
                    "qty":   item.quantity,
                })
                individual_total += child.price * item.quantity

        info = {
            "id":         bundle.id,
            "name":       bundle.name,
            "desc":       bundle.description or "—",
            "price":      bundle.price,
            "bundle_price": bundle.bundle_price,
            "bundle_discount": bundle.bundle_discount_percent,
            "stock":      bundle.stock_count,
            "children":   children,
            "individual_total": individual_total,
        }

    effective_price = info["bundle_price"] if info["bundle_price"] else info["price"]
    savings = info["individual_total"] - effective_price if info["individual_total"] > 0 else 0

    lines = [
        f"📦 <b>{info['name']}</b>\n",
        f"Description: {info['desc'][:200]}",
        f"Individual total: ${info['individual_total']:.2f}",
        f"Bundle price: ${effective_price:.2f}",
    ]
    if info["bundle_discount"]:
        lines.append(f"Discount: {info['bundle_discount']:.1f}%")
    if savings > 0:
        lines.append(f"Customer saves: ${savings:.2f}")
    lines.append(f"Stock: {info['stock']}")
    lines.append("\n<b>Included Products:</b>")

    for c in info["children"]:
        lines.append(f"  • {c['name']} × {c['qty']} (${c['price']:.2f} each)")

    text = "\n".join(lines)

    kb = [
        [InlineKeyboardButton("Set Bundle Price",    callback_data=f"abn:setprice:{bundle_id}")],
        [InlineKeyboardButton("🏷 Set Discount %",      callback_data=f"abn:setdisc:{bundle_id}")],
        [InlineKeyboardButton("➕ Add Child Product",   callback_data=f"abn:addchild:{bundle_id}")],
    ]
    for c in info["children"]:
        kb.append([InlineKeyboardButton(
            f"🗑 Remove: {c['name']}", callback_data=f"abn:rmchild:{c['item_id']}"
        )])
    kb.append([InlineKeyboardButton("🔙 Back to Bundles", callback_data="abn:menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Set bundle price (conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def set_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start set bundle price conversation: abn:setprice:<bundle_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        bundle_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["abn_bundle_id"] = bundle_id
    try:
        await query.edit_message_text(
            "📦 <b>Set Bundle Price</b>\n\n"
            "Enter the bundle price in USD (e.g., 14.99).\n"
            "This overrides the base product price for bundle display.\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ABN_BUNDLE_PRICE


async def set_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive bundle price."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("abn_bundle_id", None)
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    try:
        price = float(text)
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid non-negative price. Send /cancel to abort.")
        return ABN_BUNDLE_PRICE

    bundle_id = context.user_data.pop("abn_bundle_id", None)
    if not bundle_id:
        return ConversationHandler.END

    with get_db_session() as s:
        bundle = s.query(Product).filter_by(id=bundle_id).first()
        if bundle:
            bundle.bundle_price = price if price > 0 else None
            s.commit()

    log_admin_action(update.effective_user.id, "bundle.set_price",
                     target_type="product", target_id=bundle_id,
                     details=f"bundle_price={price}")
    await update.message.reply_text(f"✅ Bundle price set to ${price:.2f}.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Set bundle discount (conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def set_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start set bundle discount conversation: abn:setdisc:<bundle_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        bundle_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["abn_bundle_id"] = bundle_id
    try:
        await query.edit_message_text(
            "📦 <b>Set Bundle Discount</b>\n\n"
            "Enter the discount percentage (0–100).\n"
            "Example: 20 = 20% off the sum of individual prices.\n"
            "Enter 0 to remove the discount.\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ABN_BUNDLE_DISCOUNT


async def set_discount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive bundle discount percent."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("abn_bundle_id", None)
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    try:
        pct = float(text)
        if not (0 <= pct <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a number between 0 and 100. Send /cancel to abort.")
        return ABN_BUNDLE_DISCOUNT

    bundle_id = context.user_data.pop("abn_bundle_id", None)
    if not bundle_id:
        return ConversationHandler.END

    with get_db_session() as s:
        bundle = s.query(Product).filter_by(id=bundle_id).first()
        if bundle:
            bundle.bundle_discount_percent = pct if pct > 0 else None
            s.commit()

    log_admin_action(update.effective_user.id, "bundle.set_discount",
                     target_type="product", target_id=bundle_id,
                     details=f"discount={pct}%")
    await update.message.reply_text(
        f"✅ Bundle discount set to {pct:.1f}%." if pct > 0 else "✅ Bundle discount removed."
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Add child product (conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def add_child_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add child product conversation: abn:addchild:<bundle_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        bundle_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["abn_bundle_id"] = bundle_id
    try:
        await query.edit_message_text(
            "📦 <b>Add Child Product</b>\n\n"
            "Enter the <b>Product ID</b> to add to this bundle.\n"
            "(You can find product IDs in the Products admin panel.)\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ABN_CHILD_ID


async def add_child_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive child product ID."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("abn_bundle_id", None)
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    try:
        child_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numeric product ID. Send /cancel to abort.")
        return ABN_CHILD_ID

    bundle_id = context.user_data.pop("abn_bundle_id", None)
    if not bundle_id:
        return ConversationHandler.END

    with get_db_session() as s:
        bundle = s.query(Product).filter_by(id=bundle_id).first()
        child  = s.query(Product).filter_by(id=child_id, is_active=True).first()

        if not bundle:
            await update.message.reply_text("❌ Bundle not found.")
            return ConversationHandler.END
        if not child:
            await update.message.reply_text(f"❌ Product #{child_id} not found or inactive.")
            return ABN_CHILD_ID
        if child_id == bundle_id:
            await update.message.reply_text("❌ A bundle cannot contain itself.")
            return ABN_CHILD_ID

        # Check if already in bundle
        existing = s.query(BundleItem).filter_by(
            parent_product_id=bundle_id, child_product_id=child_id
        ).first()
        if existing:
            await update.message.reply_text(f"ℹ️ {child.name} is already in this bundle.")
            return ConversationHandler.END

        # Add child
        max_order = s.query(BundleItem).filter_by(parent_product_id=bundle_id).count()
        s.add(BundleItem(
            parent_product_id=bundle_id,
            child_product_id=child_id,
            quantity=1,
            display_order=max_order,
            created_at=datetime.utcnow(),
        ))
        s.commit()
        child_name = child.name

    log_admin_action(update.effective_user.id, "bundle.add_child",
                     target_type="product", target_id=bundle_id,
                     details=f"child_id={child_id}")
    await update.message.reply_text(f"✅ Added <b>{child_name}</b> to the bundle.", parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Remove child product
# ─────────────────────────────────────────────────────────────────────────────

async def remove_child(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove child product from bundle: abn:rmchild:<item_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        item_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        item = s.query(BundleItem).filter_by(id=item_id).first()
        if item:
            bundle_id = item.parent_product_id
            s.delete(item)
            s.commit()
        else:
            bundle_id = None

    if bundle_id:
        log_admin_action(update.effective_user.id, "bundle.remove_child",
                         target_type="bundle_item", target_id=item_id)
        context.user_data["_cb_data_override"] = str(bundle_id)
        await bundle_view(update, context)
    else:
        await bundle_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Route dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route acc:bundles:<action> calls."""
    if action == "menu":
        await bundle_menu(update, context)
    elif action == "view" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await bundle_view(update, context)
    elif action == "setprice" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await set_price_start(update, context)
    elif action == "setdisc" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await set_discount_start(update, context)
    elif action == "addchild" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await add_child_start(update, context)
    elif action == "rmchild" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await remove_child(update, context)
    else:
        await bundle_menu(update, context)
