"""Admin panel controls for the NOWPayments payment gateway.

Kept separate from ``admin_payment_methods.py``'s bKash/Nagad gateway flow
(same reasoning as handlers/admin_cryptomus.py) because NOWPayments'
credentials live in their own dedicated ``PaymentGatewayConfig`` row
(gateway="nowpayments" — see database/models.py) instead of the generic
bot_config key/value store used for bKash/Nagad.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session
from database.models import PaymentGatewayConfig
from utils.permissions import has_permission
from telegram.error import BadRequest

# Conversation states
NOWPAYMENTS_EDIT_API_KEY, NOWPAYMENTS_EDIT_IPN_SECRET = range(2)


def _get_or_create_config(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway="nowpayments").first()
    if not row:
        row = PaymentGatewayConfig(gateway="nowpayments", is_enabled=False)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


def _get_config_dict() -> dict:
    with get_db_session() as session:
        row = _get_or_create_config(session)
        return {
            "enabled": bool(row.is_enabled),
            "api_key": row.api_key or "",
            "ipn_secret": row.secondary_key or "",
        }


def _detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    keyboard = [
        [InlineKeyboardButton("🔑 API Key", callback_data="admin_nowpayments_edit_apikey")],
        [InlineKeyboardButton("🔒 IPN Secret", callback_data="admin_nowpayments_edit_ipnsecret")],
        [InlineKeyboardButton(toggle_label, callback_data="admin_nowpayments_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _summary_text(cfg: dict) -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    return (
        "🌐 <b>NOWPayments (Crypto)</b>\n\n"
        f"Status: {status}\n"
        f"API Key: <code>{_mask(cfg['api_key'])}</code>\n"
        f"IPN Secret: <code>{_mask(cfg['ipn_secret'])}</code>\n\n"
        "Accepts 300+ cryptocurrencies via a hosted invoice, courtesy of "
        "https://nowpayments.io\n\n"
        "⚠️ API Key must be set before enabling. IPN Secret is optional but "
        "strongly recommended — without it, instant webhook confirmation is "
        "skipped and payments fall back to polling only."
    )


async def admin_nowpayments_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the NOWPayments gateway status + credentials."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    try:
        await query.edit_message_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_nowpayments_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable/disable NOWPayments top-ups. Refuses to enable without an API key."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    if not cfg["enabled"] and not cfg["api_key"]:
        await query.answer("⚠️ Set an API Key before enabling.", show_alert=True)
        await admin_nowpayments_view(update, context)
        return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.is_enabled = not row.is_enabled
        session.commit()

    await admin_nowpayments_view(update, context)


async def admin_nowpayments_edit_api_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send your NOWPayments <b>API Key</b> "
            "(NOWPayments dashboard → Store Settings → API Keys).\n\n"
            "🔒 This value is sensitive — it won't be echoed back after saving.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_nowpayments_view")]]
            ),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return NOWPAYMENTS_EDIT_API_KEY


async def admin_nowpayments_edit_api_key_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return NOWPAYMENTS_EDIT_API_KEY

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.api_key = value[:255]
        session.commit()

    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_nowpayments_edit_ipn_secret_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send your NOWPayments <b>IPN Secret Key</b> "
            "(NOWPayments dashboard → Store Settings → Instant Payment Notifications).\n\n"
            "🔒 This value is sensitive — it won't be echoed back after saving. "
            "Send <code>-</code> to clear it.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_nowpayments_view")]]
            ),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return NOWPAYMENTS_EDIT_IPN_SECRET


async def admin_nowpayments_edit_ipn_secret_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return NOWPAYMENTS_EDIT_IPN_SECRET

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.secondary_key = None if value == "-" else value[:255]
        session.commit()

    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_nowpayments_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: bail out of the edit conversation back to the NOWPayments detail view."""
    await admin_nowpayments_view(update, context)
    return ConversationHandler.END


def build_nowpayments_edit_conv():
    """Conversation for editing the NOWPayments api_key / secondary_key (IPN secret) fields."""
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, CommandHandler
    from utils.safe_conversation import cancel_command

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_nowpayments_edit_api_key_start,
                                  pattern="^admin_nowpayments_edit_apikey$"),
            CallbackQueryHandler(admin_nowpayments_edit_ipn_secret_start,
                                  pattern="^admin_nowpayments_edit_ipnsecret$"),
        ],
        states={
            NOWPAYMENTS_EDIT_API_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_nowpayments_edit_api_key_value),
            ],
            NOWPAYMENTS_EDIT_IPN_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_nowpayments_edit_ipn_secret_value),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_nowpayments_cancel, pattern="^admin_nowpayments_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
