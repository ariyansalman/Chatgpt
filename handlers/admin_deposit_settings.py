"""Admin panel: Global Minimum Deposit settings.

Accessible via: Payment Gateways → 💰 Deposit Settings → Minimum Deposit

Callback namespace: ``admin_deposit_*``

Conversation states:
    DEPOSIT_EDIT_AMOUNT = 701
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CallbackQueryHandler, MessageHandler, CommandHandler, filters,
)

from utils.permissions import has_permission
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# Conversation state
DEPOSIT_EDIT_AMOUNT = 701


# ─── Keyboard builders ────────────────────────────────────────────────────────

def _deposit_keyboard() -> InlineKeyboardMarkup:
    enabled = cfg.get_bool("minimum_deposit_enabled", False)
    enable_btn  = InlineKeyboardButton("✅ Enable",        callback_data="admin_deposit_enable")
    disable_btn = InlineKeyboardButton("❌ Disable",       callback_data="admin_deposit_disable")
    row_toggle = [enable_btn, disable_btn]
    row_amount = [InlineKeyboardButton("✏️ Change Amount",  callback_data="admin_deposit_edit_amount")]
    row_back   = [InlineKeyboardButton("⬅️ Back",           callback_data="admin_gateways")]
    return InlineKeyboardMarkup([row_toggle, row_amount, row_back])


def _deposit_text() -> str:
    enabled = cfg.get_bool("minimum_deposit_enabled", False)
    amount  = cfg.get_float("topup_min_amount", 1.0)
    status  = "✅ Enabled" if enabled else "❌ Disabled"
    note    = (
        "Users cannot deposit less than the configured amount."
        if enabled
        else "Any positive amount is accepted across all payment gateways."
    )
    return (
        "💰 <b>Minimum Deposit Settings</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Current Amount:</b> <code>${amount:.2f}</code>\n\n"
        f"{note}\n\n"
        "Use the buttons below to configure."
    )


# ─── Main view ───────────────────────────────────────────────────────────────

async def admin_deposit_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Deposit Settings panel (Minimum Deposit)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            _deposit_text(), reply_markup=_deposit_keyboard(), parse_mode="HTML"
        )
    except Exception:
        await query.message.reply_text(
            _deposit_text(), reply_markup=_deposit_keyboard(), parse_mode="HTML"
        )


# ─── Enable / Disable ────────────────────────────────────────────────────────

async def admin_deposit_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable the global minimum deposit."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    cfg.set("minimum_deposit_enabled", True)
    amount = cfg.get_float("topup_min_amount", 1.0)
    await query.answer(f"✅ Minimum deposit enabled (${amount:.2f}).", show_alert=False)
    try:
        await query.edit_message_text(
            _deposit_text(), reply_markup=_deposit_keyboard(), parse_mode="HTML"
        )
    except Exception:
        pass


async def admin_deposit_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable the global minimum deposit."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    cfg.set("minimum_deposit_enabled", False)
    await query.answer("❌ Minimum deposit disabled. All positive amounts accepted.", show_alert=False)
    try:
        await query.edit_message_text(
            _deposit_text(), reply_markup=_deposit_keyboard(), parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Edit Amount (ConversationHandler) ───────────────────────────────────────

async def admin_deposit_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: ask admin to type the new minimum deposit amount."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    current = cfg.get_float("topup_min_amount", 1.00)
    await query.edit_message_text(
        f"✏️ <b>Set Minimum Deposit Amount</b>\n\n"
        f"Current amount: <b>${current:.2f}</b>\n\n"
        "Please send the new minimum deposit amount in USD.\n"
        "Examples: <code>0.50</code>  <code>1</code>  <code>5.00</code>  <code>10</code>\n\n"
        "Must be a positive number greater than 0.\n"
        "Send /cancel to go back.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_deposit_view")]
        ]),
    )
    return DEPOSIT_EDIT_AMOUNT


async def admin_deposit_edit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and save the new minimum deposit amount."""
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    try:
        value = float(text)
        if value <= 0:
            raise ValueError("Must be positive")
        if value > 100_000:
            raise ValueError("Too large")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Please enter a positive number (e.g. <code>1.00</code>, <code>5</code>, <code>0.50</code>).\n\n"
            "Send /cancel to go back.",
            parse_mode="HTML",
        )
        return DEPOSIT_EDIT_AMOUNT

    cfg.set("topup_min_amount", round(value, 2))

    enabled = cfg.get_bool("minimum_deposit_enabled", False)
    status_note = (
        "\n\n<b>Note:</b> Minimum deposit is currently <b>disabled</b> — enable it to enforce this limit."
        if not enabled else ""
    )

    await update.message.reply_text(
        f"✅ <b>Minimum deposit amount set to ${value:.2f}</b>{status_note}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Deposit Settings", callback_data="admin_deposit_view")]
        ]),
    )
    return ConversationHandler.END


async def admin_deposit_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel amount editing — return to deposit settings view."""
    if update.callback_query:
        await update.callback_query.answer()
        return await admin_deposit_view(update, context)
    await update.message.reply_text(
        "Cancelled.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Deposit Settings", callback_data="admin_deposit_view")]
        ]),
    )
    return ConversationHandler.END


async def admin_deposit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel during the amount editing conversation."""
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Deposit Settings", callback_data="admin_deposit_view")]
        ]),
    )
    return ConversationHandler.END


# ─── ConversationHandler factory ─────────────────────────────────────────────

def build_admin_deposit_conv() -> ConversationHandler:
    """Return the ConversationHandler for the admin minimum-deposit amount edit flow."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_deposit_edit_start, pattern="^admin_deposit_edit_amount$"),
        ],
        states={
            DEPOSIT_EDIT_AMOUNT: [
                CommandHandler("cancel", admin_deposit_cancel_cmd),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_deposit_edit_amount),
                CallbackQueryHandler(admin_deposit_edit_cancel, pattern="^admin_deposit_view$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_deposit_cancel_cmd),
            CallbackQueryHandler(admin_deposit_edit_cancel, pattern="^admin_deposit_view$"),
        ],
        allow_reentry=True,
        per_message=False,
    )
