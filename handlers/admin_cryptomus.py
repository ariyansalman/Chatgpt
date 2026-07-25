"""Admin panel controls for the Cryptomus payment gateway.

Kept separate from ``admin_payment_methods.py``'s bKash/Nagad gateway flow
(same reasoning as handlers/admin_stars.py) because Cryptomus's two
credential fields live in their own dedicated ``PaymentGatewayConfig`` row
(gateway="cryptomus" — see database/models.py) instead of the generic
bot_config key/value store used for bKash/Nagad.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session
from database.models import PaymentGatewayConfig
from utils.permissions import has_permission
from telegram.error import BadRequest

# Conversation states
CRYPTOMUS_EDIT_MERCHANT_UUID, CRYPTOMUS_EDIT_API_KEY = range(2)


def _get_or_create_config(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway="cryptomus").first()
    if not row:
        row = PaymentGatewayConfig(gateway="cryptomus", is_enabled=False)
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
            "merchant_uuid": row.merchant_uuid or "",
            "api_key": row.api_key or "",
        }


def _detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    keyboard = [
        [InlineKeyboardButton("🆔 Merchant UUID", callback_data="admin_cryptomus_edit_merchantuuid")],
        [InlineKeyboardButton("🔑 API Key", callback_data="admin_cryptomus_edit_apikey")],
        [InlineKeyboardButton(toggle_label, callback_data="admin_cryptomus_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _summary_text(cfg: dict) -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    return (
        "💠 <b>Cryptomus (USDT/Crypto)</b>\n\n"
        f"Status: {status}\n"
        f"Merchant UUID: <code>{_mask(cfg['merchant_uuid'])}</code>\n"
        f"API Key: <code>{_mask(cfg['api_key'])}</code>\n\n"
        "Used for USDT/crypto top-ups via the Cryptomus payment gateway "
        "(https://cryptomus.com) — an alternative to @CryptoBot for regions "
        "where it isn't available, e.g. Bangladesh.\n\n"
        "⚠️ Both fields must be set before enabling."
    )


async def admin_cryptomus_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Cryptomus gateway status + credentials."""
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


async def admin_cryptomus_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable/disable Cryptomus top-ups. Refuses to enable without credentials."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    if not cfg["enabled"] and not (cfg["merchant_uuid"] and cfg["api_key"]):
        await query.answer(
            "⚠️ Set both Merchant UUID and API Key before enabling.",
            show_alert=True,
        )
        await admin_cryptomus_view(update, context)
        return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.is_enabled = not row.is_enabled
        session.commit()

    await admin_cryptomus_view(update, context)


async def admin_cryptomus_edit_merchant_uuid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send your Cryptomus <b>Merchant UUID</b> "
            "(Cryptomus dashboard → Settings → API).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_cryptomus_view")]]
            ),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return CRYPTOMUS_EDIT_MERCHANT_UUID


async def admin_cryptomus_edit_merchant_uuid_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return CRYPTOMUS_EDIT_MERCHANT_UUID

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.merchant_uuid = value[:120]
        session.commit()

    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_cryptomus_edit_api_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send your Cryptomus <b>Payment API Key</b> "
            "(Cryptomus dashboard → Settings → API).\n\n"
            "🔒 This value is sensitive — it won't be echoed back after saving.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_cryptomus_view")]]
            ),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return CRYPTOMUS_EDIT_API_KEY


async def admin_cryptomus_edit_api_key_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return CRYPTOMUS_EDIT_API_KEY

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.api_key = value[:255]
        session.commit()

    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_cryptomus_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: bail out of the edit conversation back to the Cryptomus detail view."""
    await admin_cryptomus_view(update, context)
    return ConversationHandler.END


def build_cryptomus_edit_conv():
    """Conversation for editing the Cryptomus merchant_uuid / api_key fields.

    Kept as its own small ConversationHandler (mirrors build_stars_edit_conv
    in handlers/admin_stars.py) so text replies are only captured while an
    admin is actually mid-edit.
    """
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, CommandHandler
    from utils.safe_conversation import cancel_command

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_cryptomus_edit_merchant_uuid_start,
                                  pattern="^admin_cryptomus_edit_merchantuuid$"),
            CallbackQueryHandler(admin_cryptomus_edit_api_key_start,
                                  pattern="^admin_cryptomus_edit_apikey$"),
        ],
        states={
            CRYPTOMUS_EDIT_MERCHANT_UUID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cryptomus_edit_merchant_uuid_value),
            ],
            CRYPTOMUS_EDIT_API_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cryptomus_edit_api_key_value),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_cryptomus_cancel, pattern="^admin_cryptomus_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
