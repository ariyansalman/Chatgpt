"""Admin panel controls for the Binance Pay payment gateway.

Extended from the original to support:
  - Setting API Key / Secret via the Telegram admin panel (stored in
    PaymentGatewayConfig; env vars are used as fallback).
  - Viewing and resolving pending manual verifications (cases where the
    Binance API could not automatically confirm a TXID).

Security note on DB-stored credentials:
  Access to the database is equivalent to access to any env var — both give
  full read access to the API key. Storing in DB adds admin panel convenience
  without reducing the effective security boundary. The key is NEVER
  displayed in full after being saved (masked to first-4/last-4 chars).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session
from database.models import (
    PaymentGatewayConfig, PendingManualVerification, Transaction,
    TransactionStatus, User,
)
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ─── Conversation states ───────────────────────────────────────────────────
(
    BINANCE_EDIT_PAY_ID,
    BINANCE_EDIT_MIN,
    BINANCE_EDIT_MAX,
    BINANCE_EDIT_EXPIRY,
    BINANCE_EDIT_BONUS,
    BINANCE_EDIT_INSTRUCTIONS,
    BINANCE_EDIT_API_KEY,
    BINANCE_EDIT_API_SECRET,
) = range(8)

ALL_CURRENCIES = ("USDT", "USDC")


# ─── Config helpers ────────────────────────────────────────────────────────

def _get_or_create_config(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway="binance_pay").first()
    if not row:
        row = PaymentGatewayConfig(
            gateway="binance_pay", is_enabled=False,
            binance_allowed_currencies="USDT,USDC",
            binance_order_expiry_minutes=30,
            binance_bonus_percent=0.0,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _get_config_dict() -> dict:
    with get_db_session() as session:
        row = _get_or_create_config(session)
        return {
            "enabled": bool(row.is_enabled),
            "pay_id": row.binance_pay_id or "",
            "allowed_currencies": [
                c.strip().upper()
                for c in (row.binance_allowed_currencies or "USDT,USDC").split(",")
                if c.strip()
            ],
            "min_amount": row.binance_min_amount or 0.0,
            "max_amount": row.binance_max_amount or 0.0,
            "order_expiry_minutes": row.binance_order_expiry_minutes or 30,
            "bonus_percent": row.binance_bonus_percent or 0.0,
            "instructions": row.binance_instructions or "",
            "has_db_api_key": bool(row.binance_api_key),
            "has_db_api_secret": bool(row.binance_api_secret),
            "api_key_masked": _mask(row.binance_api_key),
        }


def _mask(value: str | None) -> str:
    """Show first-4 / last-4, mask the middle. Empty → '(not set)'."""
    if not value or len(value) < 8:
        return "(not set)"
    return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"


# ─── Status / label helpers ────────────────────────────────────────────────

def _quick_status_label() -> str:
    from services.binance_pay import BinancePayService
    svc = BinancePayService()
    if not svc.is_configured():
        return "⚪ Not Configured"
    src = "DB" if svc.credentials_source == "db" else "env var"
    return f"⚙️ Key loaded from {src} — tap 🧪 Test to verify live"


async def _api_status_label() -> str:
    from services.binance_pay import BinancePayService
    svc = BinancePayService()
    if not svc.is_configured():
        return "⚪ Not Configured"
    ok, msg = await asyncio.to_thread(svc.test_connection)
    src = "DB" if svc.credentials_source == "db" else "env var"
    prefix = f"✅ Connected ({src})" if ok else f"❌ {msg} (source: {src})"
    return prefix


# ─── Keyboards ────────────────────────────────────────────────────────────

def _detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆔 Binance Pay ID", callback_data="admin_binance_edit_payid")],
        [
            InlineKeyboardButton(
                f"{'✅' if 'USDT' in cfg['allowed_currencies'] else '⬜'} USDT",
                callback_data="admin_binance_toggle_cur_USDT",
            ),
            InlineKeyboardButton(
                f"{'✅' if 'USDC' in cfg['allowed_currencies'] else '⬜'} USDC",
                callback_data="admin_binance_toggle_cur_USDC",
            ),
        ],
        [
            InlineKeyboardButton("💵 Min Amount", callback_data="admin_binance_edit_min"),
            InlineKeyboardButton("💰 Max Amount", callback_data="admin_binance_edit_max"),
        ],
        [InlineKeyboardButton("⏱ Order Expiry (min)", callback_data="admin_binance_edit_expiry")],
        [InlineKeyboardButton("🎁 Bonus %", callback_data="admin_binance_edit_bonus")],
        [InlineKeyboardButton("📝 Payment Instructions", callback_data="admin_binance_edit_instructions")],
        [
            InlineKeyboardButton("🔑 API Key", callback_data="admin_binance_edit_apikey"),
            InlineKeyboardButton("🔒 API Secret", callback_data="admin_binance_edit_apisecret"),
        ],
        [InlineKeyboardButton("📋 Pending Verifications", callback_data="admin_binance_pending")],
        [InlineKeyboardButton("🧪 Test Binance API", callback_data="admin_binance_test")],
        [InlineKeyboardButton(toggle_label, callback_data="admin_binance_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")],
    ])


def _summary_text(cfg: dict, api_status: str = "⚪ Not Configured") -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    currencies = ", ".join(cfg["allowed_currencies"]) or "(none selected)"
    key_line = f"API Key: {cfg['api_key_masked']}\n" if cfg["has_db_api_key"] else ""
    return (
        "🟡 <b>Binance Pay</b>\n\n"
        f"Status: {status}\n"
        f"API Status: {api_status}\n"
        f"Binance Pay ID: <code>{cfg['pay_id'] or '(not set)'}</code>\n"
        f"{key_line}"
        f"Allowed currencies: {currencies}\n"
        f"Min amount: ${cfg['min_amount']:.2f}\n"
        f"Max amount: {('$' + format(cfg['max_amount'], '.2f')) if cfg['max_amount'] else 'No limit'}\n"
        f"Order expiry: {cfg['order_expiry_minutes']} minutes\n"
        f"Bonus: {cfg['bonus_percent']:.2f}%\n\n"
        "Verified via the normal Binance HMAC API's transaction history "
        "(GET /sapi/v1/pay/transactions) — READ-ONLY, no Merchant API, no "
        "webhooks.\n\n"
        "⚠️ Binance Pay ID must be set, at least one currency selected, and "
        "API Status must be Connected before enabling.\n\n"
        "🔑 You can set API Key/Secret via the buttons below (stored in DB) "
        "OR via BINANCE_API_KEY / BINANCE_API_SECRET environment variables."
    )


# ─── Main view / toggle / test ─────────────────────────────────────────────

async def admin_binance_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    status = await _api_status_label()
    try:
        await query.edit_message_text(
            _summary_text(cfg, status),
            reply_markup=_detail_keyboard(cfg),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_binance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    if not cfg["enabled"]:
        from services.binance_pay import BinancePayService
        svc = BinancePayService()
        if not cfg["pay_id"]:
            await query.answer("⚠️ Set a Binance Pay ID before enabling.", show_alert=True)
            return
        if not cfg["allowed_currencies"]:
            await query.answer("⚠️ Select at least one currency before enabling.", show_alert=True)
            return
        if not svc.is_configured():
            await query.answer(
                "⚠️ Set BINANCE_API_KEY / BINANCE_API_SECRET (via panel or env var) before enabling.",
                show_alert=True,
            )
            return
        ok, _msg = await asyncio.to_thread(svc.test_connection)
        if not ok:
            await query.answer("⚠️ Binance API test failed — fix credentials before enabling.", show_alert=True)
            return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.is_enabled = not row.is_enabled
        session.commit()

    await admin_binance_view(update, context)


async def admin_binance_toggle_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    currency = query.data.rsplit("_", 1)[-1].upper()
    if currency not in ALL_CURRENCIES:
        return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        current = [c.strip().upper() for c in (row.binance_allowed_currencies or "").split(",") if c.strip()]
        if currency in current:
            current.remove(currency)
        else:
            current.append(currency)
        row.binance_allowed_currencies = ",".join(current)
        session.commit()

    await admin_binance_view(update, context)


async def admin_binance_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🧪 Testing…")
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.binance_pay import BinancePayService
    svc = BinancePayService()
    ok, msg = await asyncio.to_thread(svc.test_connection)
    await query.answer(f"{'✅' if ok else '❌'} {msg}", show_alert=True)
    await admin_binance_view(update, context)


# ─── Pending verifications view ────────────────────────────────────────────

async def admin_binance_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending Binance Pay manual verifications awaiting admin decision."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        rows = (
            session.query(PendingManualVerification)
            .filter_by(gateway="binance_pay", status="pending")
            .order_by(PendingManualVerification.created_at.desc())
            .limit(10)
            .all()
        )
        if not rows:
            try:
                await query.edit_message_text(
                    "✅ No pending Binance Pay verifications.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="admin_binance_view")]
                    ]),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        lines = ["📋 <b>Pending Binance Pay Verifications</b>\n"]
        keyboard_rows = []
        for pmv in rows:
            lines.append(
                f"• <b>#{pmv.id}</b> — Order #{pmv.internal_order_id}\n"
                f"  TXID: <code>{pmv.submitted_txid}</code>\n"
                f"  Amount: {pmv.amount} {pmv.currency}\n"
                f"  Outcome: {pmv.auto_outcome or 'unknown'}\n"
                f"  At: {pmv.created_at.strftime('%Y-%m-%d %H:%M') if pmv.created_at else '?'}\n"
            )
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"✅ Approve #{pmv.id}",
                    callback_data=f"admin_binance_approve_{pmv.internal_order_id}_{pmv.id}",
                ),
                InlineKeyboardButton(
                    f"❌ Reject #{pmv.id}",
                    callback_data=f"admin_binance_reject_{pmv.internal_order_id}_{pmv.id}",
                ),
            ])

    keyboard_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_binance_view")])
    try:
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─── Field editors ─────────────────────────────────────────────────────────

async def _edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, state):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    try:
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_binance_view")]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return state


async def admin_binance_edit_payid_start(update, context):
    return await _edit_start(update, context, "💬 Send the Binance Pay ID to show users (numeric ID from your Binance app → Pay → Receive).", BINANCE_EDIT_PAY_ID)


async def admin_binance_edit_payid_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return BINANCE_EDIT_PAY_ID
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_pay_id = value[:64]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_binance_edit_min_start(update, context):
    return await _edit_start(update, context, "💬 Send minimum top-up amount in USD (e.g. 5), or 0 for no minimum.", BINANCE_EDIT_MIN)


async def admin_binance_edit_min_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BINANCE_EDIT_MIN
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_min_amount = value
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_binance_edit_max_start(update, context):
    return await _edit_start(update, context, "💬 Send maximum top-up amount in USD (e.g. 500), or 0 for no maximum.", BINANCE_EDIT_MAX)


async def admin_binance_edit_max_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BINANCE_EDIT_MAX
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_max_amount = value if value > 0 else None
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_binance_edit_expiry_start(update, context):
    return await _edit_start(update, context, "💬 Send order expiry time in minutes (e.g. 30). Minimum: 5.", BINANCE_EDIT_EXPIRY)


async def admin_binance_edit_expiry_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int((update.message.text or "").strip())
        if value < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid integer of at least 5.")
        return BINANCE_EDIT_EXPIRY
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_order_expiry_minutes = value
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_binance_edit_bonus_start(update, context):
    return await _edit_start(update, context, "💬 Send bonus percentage (e.g. 5 for +5%), or 0 for no bonus.", BINANCE_EDIT_BONUS)


async def admin_binance_edit_bonus_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BINANCE_EDIT_BONUS
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_bonus_percent = value
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_binance_edit_instructions_start(update, context):
    return await _edit_start(update, context, "💬 Send payment instructions shown on the Binance Pay screen (or 'default' to reset).", BINANCE_EDIT_INSTRUCTIONS)


async def admin_binance_edit_instructions_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return BINANCE_EDIT_INSTRUCTIONS
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_instructions = "" if value.lower() == "default" else value[:2000]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


# ─── API Key / Secret editors (new) ───────────────────────────────────────

async def admin_binance_edit_apikey_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _edit_start(
        update, context,
        "🔑 <b>Set Binance API Key</b>\n\n"
        "Send your Binance API Key (the one with Spot &amp; Margin History read permission).\n\n"
        "⚠️ This is stored in the database and takes priority over the "
        "BINANCE_API_KEY environment variable.\n\n"
        "Send <code>clear</code> to remove the DB-stored key and fall back to the env var.",
        BINANCE_EDIT_API_KEY,
    )


async def admin_binance_edit_apikey_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send the API Key or 'clear'.")
        return BINANCE_EDIT_API_KEY
    # Try to delete the message to avoid the key lingering in chat
    try:
        await update.message.delete()
    except Exception:
        pass
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_api_key = None if value.lower() == "clear" else value
        session.commit()
    cfg = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Key {action}.\n\n"
             + _summary_text(cfg, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_binance_edit_apisecret_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _edit_start(
        update, context,
        "🔒 <b>Set Binance API Secret</b>\n\n"
        "Send your Binance API Secret.\n\n"
        "⚠️ Stored in the database, takes priority over BINANCE_API_SECRET env var.\n\n"
        "Send <code>clear</code> to remove the DB-stored secret.",
        BINANCE_EDIT_API_SECRET,
    )


async def admin_binance_edit_apisecret_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send the API Secret or 'clear'.")
        return BINANCE_EDIT_API_SECRET
    try:
        await update.message.delete()
    except Exception:
        pass
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.binance_api_secret = None if value.lower() == "clear" else value
        session.commit()
    cfg = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Secret {action}.\n\n" + _summary_text(cfg, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_binance_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_binance_view(update, context)
    return ConversationHandler.END


def build_binance_edit_conv():
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, CommandHandler
    from utils.safe_conversation import cancel_command

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_binance_edit_payid_start, pattern="^admin_binance_edit_payid$"),
            CallbackQueryHandler(admin_binance_edit_min_start, pattern="^admin_binance_edit_min$"),
            CallbackQueryHandler(admin_binance_edit_max_start, pattern="^admin_binance_edit_max$"),
            CallbackQueryHandler(admin_binance_edit_expiry_start, pattern="^admin_binance_edit_expiry$"),
            CallbackQueryHandler(admin_binance_edit_bonus_start, pattern="^admin_binance_edit_bonus$"),
            CallbackQueryHandler(admin_binance_edit_instructions_start, pattern="^admin_binance_edit_instructions$"),
            CallbackQueryHandler(admin_binance_edit_apikey_start, pattern="^admin_binance_edit_apikey$"),
            CallbackQueryHandler(admin_binance_edit_apisecret_start, pattern="^admin_binance_edit_apisecret$"),
        ],
        states={
            BINANCE_EDIT_PAY_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_payid_value)],
            BINANCE_EDIT_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_min_value)],
            BINANCE_EDIT_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_max_value)],
            BINANCE_EDIT_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_expiry_value)],
            BINANCE_EDIT_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_bonus_value)],
            BINANCE_EDIT_INSTRUCTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_instructions_value)],
            BINANCE_EDIT_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_apikey_value)],
            BINANCE_EDIT_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_binance_edit_apisecret_value)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_binance_cancel, pattern="^admin_binance_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
