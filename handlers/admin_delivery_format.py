"""V17 — Admin UI for "📄 Formatted Account" delivery templates.

Lets an admin attach a ``delivery_format_template`` to any key-backed
product (KEY, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER) so that customer
delivery messages are rendered nicely instead of as a raw string, e.g.::

    📄 Your Account Details
    ━━━━━━━━━━━━━━
    📧 Email: {email}
    🔑 Password: {password}
    🔐 Recovery Email: {recovery}
    📅 Valid Until: {expiry}
    ━━━━━━━━━━━━━━
    ⚠️ Please change the password after first login.

Entry point: the "📄 Set Delivery Format" button on the inventory
product-detail screen (``handlers/admin_handlers.py::admin_inv_product_callback``),
callback data ``delivery_fmt_{product_id}``.

This is entirely additive — a product with no template keeps delivering
raw text exactly as before (see ``services/structured_delivery.py`` and
``services/delivery_service.py``).
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, Product
from utils.permissions import has_permission
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# Conversation state
WAITING_FOR_DELIVERY_TEMPLATE = 100

_EXAMPLE_TEMPLATE = (
    "📄 Your Account Details\n"
    "━━━━━━━━━━━━━━\n"
    "📧 Email: {email}\n"
    "🔑 Password: {password}\n"
    "🔐 Recovery Email: {recovery}\n"
    "📅 Valid Until: {expiry}\n"
    "━━━━━━━━━━━━━━\n"
    "⚠️ Please change the password after first login."
)


def _product_detail_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Product", callback_data=f"inv_prod_{product_id}")]
    ])


async def admin_delivery_format_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: prompt the admin to type a new delivery-format template."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    product_id = int(query.data.split("_")[-1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        p_name = product.name
        current_template = product.delivery_format_template

    context.user_data['delivery_fmt_product_id'] = product_id

    lines = [
        f"📄 Set Delivery Format — {p_name}",
        "━" * 30,
        "Type the message template that customers will receive when this "
        "product is delivered. Use `{placeholder}` for any field you want "
        "filled in from the account/key data (e.g. `{email}`, `{password}`, "
        "`{recovery}`, `{expiry}` — you can name placeholders anything you "
        "like).",
        "",
        "Example:",
        "```",
        _EXAMPLE_TEMPLATE,
        "```",
    ]
    if current_template:
        lines.append("")
        lines.append("Current template is shown below — send a new one to replace it:")
        lines.append("```")
        lines.append(current_template)
        lines.append("```")
    lines.append("")
    lines.append("Type 'cancel' or press Cancel to abort.")

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="delivery_fmt_cancel")]]
    if current_template:
        keyboard.insert(0, [InlineKeyboardButton(
            "🗑️ Clear Template", callback_data=f"delivery_fmt_clear_{product_id}"
        )])

    try:
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return WAITING_FOR_DELIVERY_TEMPLATE


async def handle_delivery_format_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the template text, save it, and offer a preview."""
    if not has_permission(update.effective_user.id, "manage_products"):
        return WAITING_FOR_DELIVERY_TEMPLATE

    text = (update.message.text or "").strip()
    if text.lower() in ("cancel", "/cancel"):
        return await cancel_delivery_format(update, context)

    product_id = context.user_data.get('delivery_fmt_product_id')
    if not product_id:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END

    from services.structured_delivery import extract_placeholders

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            await update.message.reply_text("❌ Product not found.")
            context.user_data.pop('delivery_fmt_product_id', None)
            return ConversationHandler.END
        product.delivery_format_template = text
        session.commit()
        p_name = product.name

    placeholders = extract_placeholders(text)
    ph_line = (
        "Detected placeholders: " + ", ".join(f"`{{{p}}}`" for p in placeholders)
        if placeholders else
        "⚠️ No `{placeholder}` tokens detected — every delivery will send this exact text."
    )

    keyboard = [
        [InlineKeyboardButton("🔍 Preview with sample data", callback_data=f"delivery_fmt_preview_{product_id}")],
        [InlineKeyboardButton("✏️ Edit Again", callback_data=f"delivery_fmt_{product_id}")],
        [InlineKeyboardButton("🔙 Back to Product", callback_data=f"inv_prod_{product_id}")],
    ]
    await update.message.reply_text(
        f"✅ Delivery format template saved for *{p_name}*.\n\n{ph_line}\n\n"
        "New stock uploaded with matching `field1|field2|...` values (in "
        "placeholder order) will automatically be structured to fit this "
        "template. Existing raw-text stock keeps working too.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data.pop('delivery_fmt_product_id', None)
    return ConversationHandler.END


async def admin_delivery_format_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the buyer-facing message rendered with sample data."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    product_id = int(query.data.split("_")[-1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        template = product.delivery_format_template
        p_name = product.name

    if not template:
        await query.message.reply_text(
            f"ℹ️ *{p_name}* has no delivery format template configured yet.",
            parse_mode="Markdown",
            reply_markup=_product_detail_keyboard(product_id),
        )
        return

    from services.structured_delivery import render_preview
    rendered = render_preview(template)

    await query.message.reply_text(
        "🔍 Preview (sample data — this is exactly what the customer will see):\n\n"
        + rendered,
        reply_markup=_product_detail_keyboard(product_id),
    )


async def admin_delivery_format_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a product's delivery format template (revert to raw-text delivery)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    product_id = int(query.data.split("_")[-1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        product.delivery_format_template = None
        session.commit()
        p_name = product.name

    try:
        await query.edit_message_text(
            f"🗑️ Delivery format template cleared for *{p_name}*.\n"
            "This product now delivers as plain raw text again.",
            parse_mode="Markdown",
            reply_markup=_product_detail_keyboard(product_id),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def cancel_delivery_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the delivery-format conversation (button or text 'cancel')."""
    product_id = context.user_data.get('delivery_fmt_product_id')
    query = update.callback_query
    kb = _product_detail_keyboard(product_id) if product_id else None
    if query:
        await query.answer()
        try:
            try:
                await query.edit_message_text("❌ Delivery format setup cancelled.", reply_markup=kb)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            pass
    else:
        await update.message.reply_text("❌ Delivery format setup cancelled.", reply_markup=kb)
    context.user_data.pop('delivery_fmt_product_id', None)
    return ConversationHandler.END
