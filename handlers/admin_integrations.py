"""Admin Integrations panel — read-only health snapshot for connected services.

Never prints secrets. Only presence / basic ping status.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from telegram.error import BadRequest


def _status(present: bool) -> str:
    return "🟢 configured" if present else "⚪ not set"


async def integrations_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lines = [
        "🔌 <b>Integrations</b>",
        "",
        f"• CryptoBot API key: {_status(bool(settings.CRYPTO_BOT_API_KEY))}",
        f"• Telegram Payments (card): {_status(bool(settings.TELEGRAM_PROVIDER_TOKEN))}",
        f"• Payment currency: <b>{settings.PAYMENT_CURRENCY}</b>",
        "",
        f"• Runtime mode: <b>{settings.RUN_MODE}</b>",
        f"• Webhook URL: {_status(bool(settings.WEBHOOK_URL))}",
        f"• Webhook secret: {_status(bool(settings.WEBHOOK_SECRET))}",
        "",
        "<i>Secrets are never displayed. Edit values in the .env file "
        "and restart the bot to change them.</i>",
    ]
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="acc:root")]]
    try:
        try:
            await query.edit_message_text("\n".join(lines),
                                          reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def route(action, rest, update, context):
    await integrations_menu(update, context)
