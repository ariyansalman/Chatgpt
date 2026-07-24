"""Admin CRUD for product variants (V8 Premium Core).

Callback prefixes (all guarded by ``is_admin``):
  admin_variants               -> pick a product to manage
  admin_variants_p_<prod_id>   -> variant list for product
  var_add_<prod_id>            -> add-variant conversation entry
  var_toggle_<var_id>          -> flip is_active
  var_edit_<var_id>            -> edit sub-menu (price / sale / stock / name)
  var_ep_<var_id>              -> edit price
  var_es_<var_id>              -> edit sale price
  var_ek_<var_id>              -> edit stock
  var_en_<var_id>              -> edit name
  var_del_<var_id>             -> delete (only if empty)
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from database import get_db_session
from database.models import Product, ProductVariant, ProductKey
from utils import is_admin
from utils.audit import log_admin_action
from utils.safe_conversation import safe_conversation
from telegram.error import BadRequest
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# Conversation states
V_ADD_NAME, V_ADD_PRICE, V_EDIT_VALUE = range(3)


def _fmt_variant_row(v: ProductVariant) -> str:
    on = "✅" if v.is_active else "🚫"
    sale = f" (sale ${v.sale_price:.2f})" if v.sale_price else ""
    return f"{on} {v.name} — ${v.price:.2f}{sale} · stock {v.stock_count or 0}"


# ── entry: product picker ──────────────────────────────────────────
async def admin_variants_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as s:
        products = s.query(Product).filter(Product.is_active == True).order_by(  # noqa: E712
            Product.name).limit(50).all()
        rows = [(p.id, p.name, len(p.variants)) for p in products]
    if not rows:
        try:
            await q.edit_message_text("No active products found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_products")]]))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return
    kb = [[InlineKeyboardButton(f"{name} ({nvar})", callback_data=f"admin_variants_p_{pid}")]
          for pid, name, nvar in rows]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])
    try:
        await q.edit_message_text("🎛️ Product Variants\n\nPick a product:",
                                  reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── variant list for a product ─────────────────────────────────────
async def admin_variants_for_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    pid = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        product = s.query(Product).filter(Product.id == pid).first()
        if not product:
            try:
                await q.edit_message_text("Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        variants = list(product.variants)
        pname = product.name
    body = [f"📦 <b>{pname}</b> — variants:\n"]
    kb = []
    if not variants:
        body.append("<i>No variants yet. Product sells as-is.</i>")
    for v in variants:
        body.append(_fmt_variant_row(v))
        kb.append([
            InlineKeyboardButton(("🚫" if v.is_active else "✅") + " toggle",
                                 callback_data=f"var_toggle_{v.id}"),
            InlineKeyboardButton("✏️ edit", callback_data=f"var_edit_{v.id}"),
            InlineKeyboardButton("🗑️ del", callback_data=f"var_del_{v.id}"),
        ])
    kb.append([InlineKeyboardButton("➕ Add variant", callback_data=f"var_add_{pid}")])
    kb.append([InlineKeyboardButton("🔙", callback_data="admin_variants")])
    try:
        await q.edit_message_text("\n".join(body),
                                  reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_variant_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    vid = int(q.data.rsplit("_", 1)[1])
    pid = None
    with get_db_session() as s:
        v = s.query(ProductVariant).filter(ProductVariant.id == vid).first()
        if not v:
            await q.answer("Not found", show_alert=True); return
        v.is_active = not v.is_active
        pid = v.product_id
        s.commit()
    log_admin_action(update.effective_user.id, "variant.toggle",
                     target_type="variant", target_id=str(vid))
    await admin_variants_for_product(with_data(update, f"admin_variants_p_{pid}"), context)


async def admin_variant_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    vid = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        v = s.query(ProductVariant).filter(ProductVariant.id == vid).first()
        if not v:
            await q.answer("Not found", show_alert=True); return
        pid = v.product_id
        row = _fmt_variant_row(v)
    kb = [
        [InlineKeyboardButton("💵 Price",     callback_data=f"var_ep_{vid}")],
        [InlineKeyboardButton("🏷️ Sale price", callback_data=f"var_es_{vid}")],
        [InlineKeyboardButton("📦 Stock",     callback_data=f"var_ek_{vid}")],
        [InlineKeyboardButton("📝 Name",      callback_data=f"var_en_{vid}")],
        [InlineKeyboardButton("🔙", callback_data=f"admin_variants_p_{pid}")],
    ]
    try:
        await q.edit_message_text(f"Editing:\n{row}\n\nPick a field:",
                                  reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_variant_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    vid = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        v = s.query(ProductVariant).filter(ProductVariant.id == vid).first()
        if not v:
            await q.answer("Not found", show_alert=True); return
        pid = v.product_id
        n_keys = s.query(ProductKey).filter(ProductKey.variant_id == vid).count()
        if n_keys > 0 or (v.stock_count or 0) > 0:
            await q.answer("Variant has stock — set to 0 first", show_alert=True); return
        s.delete(v); s.commit()
    log_admin_action(update.effective_user.id, "variant.delete",
                     target_type="variant", target_id=str(vid))
    await admin_variants_for_product(with_data(update, f"admin_variants_p_{pid}"), context)


# ── add-variant conversation ───────────────────────────────────────
async def variant_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    pid = int(q.data.rsplit("_", 1)[1])
    context.user_data["_var_add_pid"] = pid
    try:
        await q.edit_message_text("Send the variant NAME (e.g. '1 Month', 'Family'):")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return V_ADD_NAME


@safe_conversation(cleanup_keys=("_var_add_pid", "_var_add_name"))
async def variant_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name or len(name) > 120:
        await update.message.reply_text("Name required (1-120 chars). Send again:")
        return V_ADD_NAME
    context.user_data["_var_add_name"] = name
    await update.message.reply_text("Now send the PRICE (USD, e.g. 4.99):")
    return V_ADD_PRICE


@safe_conversation(cleanup_keys=("_var_add_pid", "_var_add_name"))
async def variant_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float((update.message.text or "").strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a positive number, e.g. 4.99:")
        return V_ADD_PRICE
    pid = context.user_data.pop("_var_add_pid", None)
    name = context.user_data.pop("_var_add_name", None)
    if not pid or not name:
        await update.message.reply_text("Session lost. /cancel and retry.")
        return ConversationHandler.END
    with get_db_session() as s:
        n_existing = s.query(ProductVariant).filter(
            ProductVariant.product_id == pid).count()
        v = ProductVariant(product_id=pid, name=name, price=price,
                           display_order=n_existing, is_active=True)
        s.add(v); s.commit()
        vid = v.id
    log_admin_action(update.effective_user.id, "variant.create",
                     target_type="variant", target_id=str(vid),
                     details=f"{name} @ ${price:.2f}")
    kb = [[InlineKeyboardButton("← back to variants",
                                callback_data=f"admin_variants_p_{pid}")]]
    await update.message.reply_text(f"✅ Variant '{name}' created (${price:.2f}).",
                                    reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# ── edit-value conversation (shared for price/sale/stock/name) ─────
async def variant_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    prefix, vid = q.data.rsplit("_", 1)
    field_map = {"var_ep": "price", "var_es": "sale_price",
                 "var_ek": "stock_count", "var_en": "name"}
    field = field_map.get(prefix)
    if not field:
        return ConversationHandler.END
    context.user_data["_var_edit_id"] = int(vid)
    context.user_data["_var_edit_field"] = field
    hint = {"price": "new price (USD, e.g. 4.99)",
            "sale_price": "new sale price (0 to clear)",
            "stock_count": "new stock quantity (integer)",
            "name": "new name (1-120 chars)"}[field]
    try:
        await q.edit_message_text(f"Send the {hint}:")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return V_EDIT_VALUE


@safe_conversation(cleanup_keys=("_var_edit_id", "_var_edit_field"))
async def variant_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vid = context.user_data.pop("_var_edit_id", None)
    field = context.user_data.pop("_var_edit_field", None)
    if not vid or not field:
        await update.message.reply_text("Session lost. /cancel and retry.")
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    with get_db_session() as s:
        v = s.query(ProductVariant).filter(ProductVariant.id == vid).first()
        if not v:
            await update.message.reply_text("Variant vanished.")
            return ConversationHandler.END
        stock_before = v.stock_count or 0
        try:
            if field == "name":
                if not raw or len(raw) > 120:
                    raise ValueError("length")
                v.name = raw
            elif field == "price":
                val = float(raw)
                if val <= 0: raise ValueError("positive")
                v.price = val
            elif field == "sale_price":
                val = float(raw)
                v.sale_price = None if val <= 0 else val
            elif field == "stock_count":
                v.stock_count = max(0, int(raw))
        except ValueError:
            await update.message.reply_text("Invalid value. /cancel and retry.")
            return ConversationHandler.END
        pid = v.product_id
        stock_after = v.stock_count or 0
        s.commit()
    log_admin_action(update.effective_user.id, f"variant.edit.{field}",
                     target_type="variant", target_id=str(vid),
                     details=raw[:120])
    # Automatic Restock Broadcast: a variant stock edit that takes the
    # variant from 0 to >0 counts as a restock for that product too.
    if field == "stock_count" and stock_before == 0 and stock_after > 0:
        try:
            from handlers.admin_broadcast_center import send_restock_broadcast
            await send_restock_broadcast(context.bot, pid, variant_id=vid)
        except Exception:
            logging.getLogger(__name__).exception(
                "restock broadcast trigger failed for product_id=%s (variant %s)", pid, vid)

        # Channel Auto-Post (V18): best-effort restock post to the
        # configured channel, mirroring the eligible-users broadcast above.
        try:
            from services.channel_poster import post_restock
            await post_restock(context.bot, pid, variant_id=vid, available=stock_after)
        except Exception:
            logging.getLogger(__name__).exception(
                "channel auto-post (restock) failed for product_id=%s (variant %s)", pid, vid)
    kb = [[InlineKeyboardButton("← back to variants",
                                callback_data=f"admin_variants_p_{pid}")]]
    await update.message.reply_text(f"✅ Updated {field}.",
                                    reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


def register(application):
    """Wire all variant callbacks + conversations onto the bot."""
    application.add_handler(CallbackQueryHandler(admin_variants_menu,
                                                 pattern="^admin_variants$"))
    application.add_handler(CallbackQueryHandler(admin_variants_for_product,
                                                 pattern=r"^admin_variants_p_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_variant_toggle,
                                                 pattern=r"^var_toggle_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_variant_edit_menu,
                                                 pattern=r"^var_edit_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_variant_delete,
                                                 pattern=r"^var_del_\d+$"))

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(variant_add_start,
                                           pattern=r"^var_add_\d+$")],
        states={
            V_ADD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, variant_add_name)],
            V_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, variant_add_price)],
        },
        fallbacks=[CallbackQueryHandler(admin_variants_menu,
                                        pattern="^admin_variants$")],
        allow_reentry=True,
    )
    application.add_handler(add_conv)

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(variant_edit_start,
                                           pattern=r"^var_(ep|es|ek|en)_\d+$")],
        states={
            V_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                          variant_edit_value)],
        },
        fallbacks=[CallbackQueryHandler(admin_variants_menu,
                                        pattern="^admin_variants$")],
        allow_reentry=True,
    )
    application.add_handler(edit_conv)