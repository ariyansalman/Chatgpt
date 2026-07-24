"""Admin panel controls for the Telegram Stars payment gateway.

Kept separate from ``admin_payment_methods.py``'s bKash/Nagad gateway flow
because Stars settings live in their own dedicated ``PaymentGatewayConfig``
row (see database/models.py) instead of the generic bot_config key/value
store used for bKash/Nagad credentials.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from utils.permissions import has_permission
from services.telegram_stars import telegram_stars_service
from telegram.error import BadRequest

# Conversation states
STARS_EDIT_RATE, STARS_EDIT_MIN, STARS_EDIT_MAX = range(3)


def _detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    keyboard = [
        [InlineKeyboardButton(f"💱 Rate: ${cfg['rate']:.4f} / ⭐", callback_data="admin_stars_edit_rate")],
        [
            InlineKeyboardButton(f"🔽 Min: {cfg['min_stars']} ⭐", callback_data="admin_stars_edit_min"),
            InlineKeyboardButton(f"🔼 Max: {cfg['max_stars']} ⭐", callback_data="admin_stars_edit_max"),
        ],
        [InlineKeyboardButton(toggle_label, callback_data="admin_stars_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _summary_text(cfg: dict) -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    example_stars = telegram_stars_service.stars_for_usd(5.0)
    return (
        "⭐ <b>Telegram Stars</b>\n\n"
        f"Status: {status}\n"
        f"Rate: <b>${cfg['rate']:.4f}</b> credited to the wallet per 1 ⭐ Star paid\n"
        f"Allowed range: {cfg['min_stars']}–{cfg['max_stars']} ⭐ per top-up\n\n"
        f"Example: a $5.00 top-up currently costs <b>{example_stars} ⭐</b>.\n\n"
        "No provider token is needed — Telegram settles Stars payments "
        "directly with your bot (currency code XTR)."
    )


async def admin_stars_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Telegram Stars gateway status + settings."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = telegram_stars_service.get_config()
    try:
        await query.edit_message_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_stars_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable/disable Telegram Stars top-ups."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = telegram_stars_service.get_config()
    telegram_stars_service.set_enabled(not cfg["enabled"])
    await admin_stars_view(update, context)


async def admin_stars_edit_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    cfg = telegram_stars_service.get_config()
    try:
        await query.edit_message_text(
            "💬 Send the USD value credited to the wallet per 1 ⭐ Star paid.\n\n"
            f"Current: ${cfg['rate']:.4f}\nExample: 0.013\n\n"
            "Tip: check Telegram's current Stars terms before setting this, so "
            "you don't under- or over-credit users relative to what you can "
            "actually withdraw.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_stars_view")]]
            ),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return STARS_EDIT_RATE


async def admin_stars_edit_rate_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        rate = float(text)
        if rate <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a positive number, e.g. 0.013")
        return STARS_EDIT_RATE

    telegram_stars_service.set_rate(rate)
    cfg = telegram_stars_service.get_config()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_stars_edit_min_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send the minimum number of ⭐ Stars allowed per top-up (whole number).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_stars_view")]]
            ),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return STARS_EDIT_MIN


async def admin_stars_edit_min_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        val = int(text)
        if val < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a whole number ≥ 1.")
        return STARS_EDIT_MIN

    telegram_stars_service.set_star_limits(min_stars=val)
    cfg = telegram_stars_service.get_config()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_stars_edit_max_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "💬 Send the maximum number of ⭐ Stars allowed per top-up (whole number).\n"
            "Telegram also enforces its own server-side ceiling regardless of this value.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_stars_view")]]
            ),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return STARS_EDIT_MAX


async def admin_stars_edit_max_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        val = int(text)
        if val < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a whole number ≥ 1.")
        return STARS_EDIT_MAX

    telegram_stars_service.set_star_limits(max_stars=val)
    cfg = telegram_stars_service.get_config()
    await update.message.reply_text(_summary_text(cfg), reply_markup=_detail_keyboard(cfg), parse_mode='HTML')
    return ConversationHandler.END


async def admin_stars_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: bail out of the edit conversation back to the Stars detail view."""
    await admin_stars_view(update, context)
    return ConversationHandler.END


def build_stars_edit_conv() -> ConversationHandler:
    """Conversation for editing the Stars rate / min / max fields.

    Kept as its own small ConversationHandler (mirrors gw_edit_conv in
    bot.py) so text replies are only captured while an admin is actually
    mid-edit.
    """
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, CommandHandler
    from utils.safe_conversation import cancel_command

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_stars_edit_rate_start, pattern="^admin_stars_edit_rate$"),
            CallbackQueryHandler(admin_stars_edit_min_start, pattern="^admin_stars_edit_min$"),
            CallbackQueryHandler(admin_stars_edit_max_start, pattern="^admin_stars_edit_max$"),
        ],
        states={
            STARS_EDIT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_stars_edit_rate_value)],
            STARS_EDIT_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_stars_edit_min_value)],
            STARS_EDIT_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_stars_edit_max_value)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_stars_cancel, pattern="^admin_stars_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
