"""V40 — Global Delivery Message Builder admin handler.

Namespace: ``dmb:*``

Allows administrators to fully customise the customer delivery message
template without touching source code.

Navigation
──────────
  dmb:menu           — Main menu (show current template)
  dmb:edit           — Start ConversationHandler to type new template
  dmb:preview        — Show rendered preview with sample data
  dmb:restore        — Confirm-restore to DEFAULT_TEMPLATE
  dmb:restore_confirm — Actually restore the default template
  dmb:back           — Return to Store Settings

Entry point: callback_data ``dmb:menu``
Added to admin Store Settings menu via menu_builder registration.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── ConversationHandler state ─────────────────────────────────────────────────
_WAITING_TEMPLATE = 42


# ── Helpers ───────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Template",       callback_data="dmb:edit")],
        [InlineKeyboardButton("👁 Preview",              callback_data="dmb:preview")],
        [InlineKeyboardButton("🔄 Restore Default",     callback_data="dmb:restore")],
        [InlineKeyboardButton("🔙 Back",                callback_data="admin_settings")],
    ])


def _edit_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="dmb:menu")]
    ])


def _restore_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Restore Default", callback_data="dmb:restore_confirm")],
        [InlineKeyboardButton("❌ Cancel",                callback_data="dmb:menu")],
    ])


def _truncate(text: str, n: int = 3000) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "\n…[truncated]"


def _template_display(template: str) -> str:
    """Escape backticks so Markdown code blocks don't break."""
    return template.replace("`", "'")


# ── Menu ─────────────────────────────────────────────────────────────────────

async def dmb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Delivery Message Builder main menu with the current template."""
    query = update.callback_query
    if query:
        await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.delivery_message_renderer import get_global_template, DEFAULT_TEMPLATE
    current = get_global_template()
    is_default = (current.strip() == DEFAULT_TEMPLATE.strip())

    text = (
        "📐 Delivery Message Builder\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Customise the message customers receive when their order is delivered.\n"
        "Use ``{placeholders}`` for dynamic values.\n\n"
        "Supported placeholders:\n"
        "  {order_id} · {product_name} · {quantity}\n"
        "  {amount} · {purchase_time}\n"
        "  {email} · {password} · {twofa}\n\n"
        f"{'📋 Using: *Default Template*' if is_default else '📋 Using: *Custom Template*'}\n\n"
        "Current template:\n"
        "```\n"
        f"{_template_display(_truncate(current, 800))}\n"
        "```"
    )

    kb = _main_kb()
    try:
        if query:
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        logger.exception("dmb_menu: failed to render")


# ── Preview ───────────────────────────────────────────────────────────────────

async def dmb_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a rendered preview using sample data."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.delivery_message_renderer import render_preview
    preview_text = render_preview()

    text = (
        "👁 *Message Preview* (sample data)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + _truncate(preview_text, 3200)
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="dmb:menu")]
    ])
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            # Fallback: send as plain text if Markdown fails
            try:
                await query.edit_message_text(
                    "👁 Message Preview (sample data)\n\n" + _truncate(preview_text, 3200),
                    reply_markup=kb,
                )
            except Exception:
                logger.exception("dmb_preview: fallback also failed")


# ── Edit (ConversationHandler) ────────────────────────────────────────────────

async def dmb_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt admin to type the new template."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    from services.delivery_message_renderer import DEFAULT_TEMPLATE

    text = (
        "✏️ *Edit Delivery Message Template*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Type or paste your new template below.\n\n"
        "*Available placeholders:*\n"
        "  `{order_id}` — Order / receipt number\n"
        "  `{product_name}` — Product name\n"
        "  `{quantity}` — Quantity purchased\n"
        "  `{amount}` — Amount paid\n"
        "  `{purchase_time}` — Purchase date/time\n"
        "  `{email}` — Email (hidden if empty)\n"
        "  `{password}` — Password (hidden if empty)\n"
        "  `{twofa}` — 2FA code (hidden if empty)\n\n"
        "*Tips:*\n"
        "• Lines whose placeholder has no value are automatically hidden.\n"
        "• Use `━━━━━━━━━━━━━━` for section separators.\n"
        "• Send *cancel* or press Cancel to abort.\n\n"
        "Default template for reference:\n"
        "```\n"
        f"{_template_display(DEFAULT_TEMPLATE)}\n"
        "```"
    )
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_edit_cancel_kb())
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return _WAITING_TEMPLATE


async def dmb_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and save the new template text."""
    text = (update.message.text or "").strip()

    if text.lower() in ("cancel", "/cancel"):
        await update.message.reply_text("❌ Template edit cancelled.", reply_markup=_main_kb())
        return ConversationHandler.END

    if not text:
        await update.message.reply_text(
            "⚠️ Template cannot be empty. Send a valid template or type *cancel*.",
            parse_mode="Markdown",
        )
        return _WAITING_TEMPLATE

    if not has_permission(update.effective_user.id, "manage_settings"):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    from services.delivery_message_renderer import set_global_template, render_template, _SAMPLE_VALUES
    set_global_template(text)

    # Show a preview of the saved template
    preview = render_template(text, dict(_SAMPLE_VALUES))

    await update.message.reply_text(
        "✅ *Template saved!*\n\n"
        "Here's a preview with sample data:\n\n"
        + _truncate(preview, 3000),
        parse_mode="Markdown",
        reply_markup=_main_kb(),
    )
    return ConversationHandler.END


async def dmb_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the edit conversation via the Cancel button."""
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text("❌ Template edit cancelled.", reply_markup=_main_kb())
        except BadRequest:
            pass
    return ConversationHandler.END


# ── Restore Default ───────────────────────────────────────────────────────────

async def dmb_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before restoring the default template."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    text = (
        "🔄 *Restore Default Template?*\n\n"
        "This will replace your current custom template with the built-in default.\n\n"
        "Are you sure?"
    )
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_restore_kb())
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def dmb_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Actually restore the default template."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.delivery_message_renderer import set_global_template
    set_global_template(None)  # NULL → renderer falls back to DEFAULT_TEMPLATE

    await query.answer("✅ Default template restored.", show_alert=True)
    await dmb_menu(update, context)


# ── Handler registration ──────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all dmb:* callback and conversation handlers."""
    from telegram.ext import ConversationHandler as CH

    # Edit conversation
    edit_conv = CH(
        entry_points=[CallbackQueryHandler(dmb_edit_start, pattern=r"^dmb:edit$")],
        states={
            _WAITING_TEMPLATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dmb_edit_receive),
                CallbackQueryHandler(dmb_edit_cancel, pattern=r"^dmb:menu$"),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(dmb_edit_cancel, pattern=r"^dmb:menu$"),
        ],
        per_message=False,
    )
    application.add_handler(edit_conv)

    # Simple callbacks
    application.add_handler(CallbackQueryHandler(dmb_menu,            pattern=r"^dmb:menu$"))
    application.add_handler(CallbackQueryHandler(dmb_preview,         pattern=r"^dmb:preview$"))
    application.add_handler(CallbackQueryHandler(dmb_restore,         pattern=r"^dmb:restore$"))
    application.add_handler(CallbackQueryHandler(dmb_restore_confirm, pattern=r"^dmb:restore_confirm$"))
