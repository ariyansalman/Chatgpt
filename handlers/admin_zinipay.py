"""Admin panel controls for the ZiniPay payment gateway.

Covers every user-configurable field:
  • API Key
  • bKash / Nagad / Rocket / Upay merchant numbers
  • Default provider highlighted on the payment screen
  • USD → BDT exchange rate (per-gateway override or use global)
  • Auto-rate toggle (refresh from the global exchange-rate API)
  • Payment instructions shown below the wallet numbers
  • Enable / Disable toggle

All values are stored in PaymentGatewayConfig (gateway="zinipay").
None of the wallet numbers are ever hardcoded — they come exclusively from
this admin panel and are served to users in _finish_zinipay_payment().
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import PaymentGatewayConfig
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states — one per editable text field.
# ---------------------------------------------------------------------------
(
    ZINIPAY_EDIT_API_KEY,
    ZINIPAY_EDIT_BKASH,
    ZINIPAY_EDIT_NAGAD,
    ZINIPAY_EDIT_ROCKET,
    ZINIPAY_EDIT_UPAY,
    ZINIPAY_EDIT_RATE,
    ZINIPAY_EDIT_INSTRUCTIONS,
) = range(7)

# Human-readable labels for each editable field (used in prompts).
_FIELD_LABELS = {
    "api_key":                 "API Key",
    "zinipay_bkash_number":    "bKash Number",
    "zinipay_nagad_number":    "Nagad Number",
    "zinipay_rocket_number":   "Rocket Number",
    "zinipay_upay_number":     "Upay Number",
    "zinipay_usd_to_bdt_rate": "USD → BDT Exchange Rate",
    "zinipay_instructions":    "Payment Instructions",
}

VALID_PROVIDERS = ("bkash", "nagad", "rocket", "upay")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_or_create(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway="zinipay").first()
    if not row:
        row = PaymentGatewayConfig(gateway="zinipay", is_enabled=False)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _load_cfg() -> dict:
    with get_db_session() as session:
        row = _get_or_create(session)
        return {
            "enabled":           bool(row.is_enabled),
            "api_key":           row.api_key or "",
            "bkash":             row.zinipay_bkash_number or "",
            "nagad":             row.zinipay_nagad_number or "",
            "rocket":            row.zinipay_rocket_number or "",
            "upay":              row.zinipay_upay_number or "",
            "default_provider":  row.zinipay_default_provider or "bkash",
            "rate":              row.zinipay_usd_to_bdt_rate,
            "auto_rate":         bool(row.zinipay_auto_rate),
            "instructions":      row.zinipay_instructions or "",
        }


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def _mask(value: str) -> str:
    if not value:
        return "❌ <i>not set</i>"
    if len(value) <= 6:
        return "•" * len(value)
    return f"<code>{value[:3]}…{value[-3:]}</code> ({len(value)} chars)"


def _num(value: str) -> str:
    return f"<code>{value}</code>" if value else "❌ <i>not set</i>"


def _summary(cfg: dict) -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    rate_display = (
        f"{cfg['rate']:.2f} BDT/USD" if cfg["rate"] else "🌐 Global rate"
    )
    if cfg["auto_rate"]:
        rate_display += " (auto-refresh ✅)"
    instr_preview = cfg["instructions"][:60] + "…" if len(cfg["instructions"]) > 60 else (cfg["instructions"] or "❌ <i>not set</i>")
    return (
        "🇧🇩 <b>ZiniPay Configuration</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>API Key:</b> {_mask(cfg['api_key'])}\n\n"
        "<b>Wallet Numbers:</b>\n"
        f"  📱 bKash:  {_num(cfg['bkash'])}\n"
        f"  📱 Nagad:  {_num(cfg['nagad'])}\n"
        f"  📱 Rocket: {_num(cfg['rocket'])}\n"
        f"  📱 Upay:   {_num(cfg['upay'])}\n\n"
        f"<b>Default Provider:</b> {cfg['default_provider'].title()}\n"
        f"<b>Exchange Rate:</b> {rate_display}\n"
        f"<b>Instructions:</b> {instr_preview}\n\n"
        "⚠️ API Key and at least one wallet number must be set before enabling."
    )


def _keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    auto_label = "⏹ Disable Auto-Rate" if cfg["auto_rate"] else "🔄 Enable Auto-Rate"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 API Key",            callback_data="admin_zinipay_edit_apikey")],
        [
            InlineKeyboardButton("📱 bKash",   callback_data="admin_zinipay_edit_bkash"),
            InlineKeyboardButton("📱 Nagad",   callback_data="admin_zinipay_edit_nagad"),
        ],
        [
            InlineKeyboardButton("📱 Rocket",  callback_data="admin_zinipay_edit_rocket"),
            InlineKeyboardButton("📱 Upay",    callback_data="admin_zinipay_edit_upay"),
        ],
        [InlineKeyboardButton("🏦 Default Provider",   callback_data="admin_zinipay_provider_menu")],
        [InlineKeyboardButton("💱 Exchange Rate",       callback_data="admin_zinipay_edit_rate")],
        [InlineKeyboardButton(auto_label,               callback_data="admin_zinipay_toggle_autorate")],
        [InlineKeyboardButton("📋 Instructions",        callback_data="admin_zinipay_edit_instructions")],
        [InlineKeyboardButton(toggle_label,             callback_data="admin_zinipay_toggle")],
        [InlineKeyboardButton("🔙 Back",                callback_data="admin_gateways")],
    ])


# ---------------------------------------------------------------------------
# View entry point
# ---------------------------------------------------------------------------

async def admin_zinipay_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the ZiniPay configuration summary."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _load_cfg()
    try:
        await query.edit_message_text(
            _summary(cfg), reply_markup=_keyboard(cfg), parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ---------------------------------------------------------------------------
# Toggle enable / disable
# ---------------------------------------------------------------------------

async def admin_zinipay_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    cfg = _load_cfg()
    # Refuse to enable without an API key AND at least one wallet number.
    if not cfg["enabled"]:
        missing = []
        if not cfg["api_key"]:
            missing.append("API Key")
        if not any([cfg["bkash"], cfg["nagad"], cfg["rocket"], cfg["upay"]]):
            missing.append("at least one wallet number")
        if missing:
            await query.answer(
                f"⚠️ Set {' and '.join(missing)} before enabling.", show_alert=True
            )
            await admin_zinipay_view(update, context)
            return

    with get_db_session() as session:
        row = _get_or_create(session)
        row.is_enabled = not row.is_enabled
        session.commit()

    await admin_zinipay_view(update, context)


# ---------------------------------------------------------------------------
# Toggle auto-rate
# ---------------------------------------------------------------------------

async def admin_zinipay_toggle_autorate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    with get_db_session() as session:
        row = _get_or_create(session)
        row.zinipay_auto_rate = not bool(row.zinipay_auto_rate)
        session.commit()

    await admin_zinipay_view(update, context)


# ---------------------------------------------------------------------------
# Default provider inline selection (no conversation needed)
# ---------------------------------------------------------------------------

async def admin_zinipay_provider_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    cfg = _load_cfg()
    current = cfg["default_provider"]
    buttons = []
    for p in VALID_PROVIDERS:
        label = f"{'✅ ' if p == current else ''}{p.title()}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"admin_zinipay_setprovider_{p}"))
    keyboard = InlineKeyboardMarkup([
        buttons[:2], buttons[2:],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_zinipay_view")],
    ])
    try:
        await query.edit_message_text(
            "🏦 <b>Select the default payment provider</b>\n\n"
            "This provider will be highlighted first on the user's payment screen. "
            "All configured wallet numbers are always shown.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_zinipay_set_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    provider = query.data.replace("admin_zinipay_setprovider_", "")
    if provider not in VALID_PROVIDERS:
        return

    with get_db_session() as session:
        row = _get_or_create(session)
        row.zinipay_default_provider = provider
        session.commit()

    await admin_zinipay_view(update, context)


# ---------------------------------------------------------------------------
# Generic "start edit field" helper
# ---------------------------------------------------------------------------

async def _start_edit(query, context, field_key: str, prompt: str, state: int):
    context.user_data["zinipay_editing_field"] = field_key
    try:
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Cancel", callback_data="admin_zinipay_view")]
            ]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return state


# ---------------------------------------------------------------------------
# Entry points for each editable field
# ---------------------------------------------------------------------------

async def admin_zinipay_edit_apikey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "api_key",
        "🔑 Send your ZiniPay <b>Brand / API Key</b>\n"
        "(ZiniPay dashboard → Brands → your brand → API Key).\n\n"
        "🔒 This value is sensitive — send it and it will be stored securely. "
        "It won't be echoed back after saving.\n\n"
        "Send <code>-</code> to clear.",
        ZINIPAY_EDIT_API_KEY,
    )


async def admin_zinipay_edit_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_bkash_number",
        "📱 Send the <b>bKash merchant number</b> users should send money to.\n\n"
        "Example: <code>01712345678</code>\n\n"
        "Send <code>-</code> to clear (hides bKash from the payment screen).",
        ZINIPAY_EDIT_BKASH,
    )


async def admin_zinipay_edit_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_nagad_number",
        "📱 Send the <b>Nagad merchant number</b> users should send money to.\n\n"
        "Example: <code>01812345678</code>\n\n"
        "Send <code>-</code> to clear.",
        ZINIPAY_EDIT_NAGAD,
    )


async def admin_zinipay_edit_rocket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_rocket_number",
        "📱 Send the <b>Rocket (DBBL) merchant number</b> users should send money to.\n\n"
        "Example: <code>01912345678</code>\n\n"
        "Send <code>-</code> to clear.",
        ZINIPAY_EDIT_ROCKET,
    )


async def admin_zinipay_edit_upay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_upay_number",
        "📱 Send the <b>Upay merchant number</b> users should send money to.\n\n"
        "Example: <code>01512345678</code>\n\n"
        "Send <code>-</code> to clear.",
        ZINIPAY_EDIT_UPAY,
    )


async def admin_zinipay_edit_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_usd_to_bdt_rate",
        "💱 Send the <b>USD → BDT exchange rate</b> to use for ZiniPay payments.\n\n"
        "Example: <code>125</code> means $1.00 = ৳125.00\n\n"
        "Send <code>-</code> or <code>0</code> to clear and use the global rate "
        "configured in Settings.",
        ZINIPAY_EDIT_RATE,
    )


async def admin_zinipay_edit_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_instructions",
        "📋 Send the <b>payment instructions</b> shown below the wallet numbers "
        "on the payment screen.\n\n"
        "Example:\n"
        "<i>Open your app → Send exact amount → Copy Transaction ID → "
        "Press Submit Transaction ID</i>\n\n"
        "Send <code>-</code> to clear (default instructions will be shown).",
        ZINIPAY_EDIT_INSTRUCTIONS,
    )


# ---------------------------------------------------------------------------
# Generic "receive value" handler (handles all text field saves)
# ---------------------------------------------------------------------------

async def admin_zinipay_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save whichever field is currently being edited."""
    value = (update.message.text or "").strip()
    field = context.user_data.get("zinipay_editing_field", "")

    if not field:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❌ Please send a value, or press Cancel.")
        # Return the same state based on which field is being edited
        state_map = {
            "api_key":                 ZINIPAY_EDIT_API_KEY,
            "zinipay_bkash_number":    ZINIPAY_EDIT_BKASH,
            "zinipay_nagad_number":    ZINIPAY_EDIT_NAGAD,
            "zinipay_rocket_number":   ZINIPAY_EDIT_ROCKET,
            "zinipay_upay_number":     ZINIPAY_EDIT_UPAY,
            "zinipay_usd_to_bdt_rate": ZINIPAY_EDIT_RATE,
            "zinipay_instructions":    ZINIPAY_EDIT_INSTRUCTIONS,
        }
        return state_map.get(field, ConversationHandler.END)

    # "-" means "clear this field"
    clear = value == "-"
    save_value: object = None if clear else value

    # Special handling for the rate field: must be a positive float.
    if field == "zinipay_usd_to_bdt_rate":
        if clear or value == "0":
            save_value = None
        else:
            try:
                parsed = float(value.replace(",", "."))
                if parsed <= 0:
                    raise ValueError
                save_value = parsed
            except ValueError:
                await update.message.reply_text(
                    "❌ Please send a positive number (e.g. <code>125</code> or <code>125.50</code>).",
                    parse_mode="HTML",
                )
                return ZINIPAY_EDIT_RATE

    with get_db_session() as session:
        row = _get_or_create(session)
        if field == "api_key":
            row.api_key = None if clear else value[:255]
        elif field == "zinipay_bkash_number":
            row.zinipay_bkash_number = None if clear else value[:120]
        elif field == "zinipay_nagad_number":
            row.zinipay_nagad_number = None if clear else value[:120]
        elif field == "zinipay_rocket_number":
            row.zinipay_rocket_number = None if clear else value[:120]
        elif field == "zinipay_upay_number":
            row.zinipay_upay_number = None if clear else value[:120]
        elif field == "zinipay_usd_to_bdt_rate":
            row.zinipay_usd_to_bdt_rate = save_value
        elif field == "zinipay_instructions":
            row.zinipay_instructions = None if clear else value[:2000]
        session.commit()

    context.user_data.pop("zinipay_editing_field", None)
    label = _FIELD_LABELS.get(field, field)
    saved_text = "cleared." if clear else "saved."
    cfg = _load_cfg()
    await update.message.reply_text(
        f"✅ <b>{label}</b> {saved_text}",
        parse_mode="HTML",
    )
    await update.message.reply_text(
        _summary(cfg), reply_markup=_keyboard(cfg), parse_mode="HTML"
    )
    return ConversationHandler.END


async def admin_zinipay_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any active edit and return to the ZiniPay view."""
    context.user_data.pop("zinipay_editing_field", None)
    if update.callback_query:
        await admin_zinipay_view(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_zinipay_edit_conv() -> ConversationHandler:
    """Build the ConversationHandler for all ZiniPay admin edits."""
    from utils.safe_conversation import cancel_command

    entry_points = [
        CallbackQueryHandler(admin_zinipay_edit_apikey,       pattern="^admin_zinipay_edit_apikey$"),
        CallbackQueryHandler(admin_zinipay_edit_bkash,        pattern="^admin_zinipay_edit_bkash$"),
        CallbackQueryHandler(admin_zinipay_edit_nagad,        pattern="^admin_zinipay_edit_nagad$"),
        CallbackQueryHandler(admin_zinipay_edit_rocket,       pattern="^admin_zinipay_edit_rocket$"),
        CallbackQueryHandler(admin_zinipay_edit_upay,         pattern="^admin_zinipay_edit_upay$"),
        CallbackQueryHandler(admin_zinipay_edit_rate,         pattern="^admin_zinipay_edit_rate$"),
        CallbackQueryHandler(admin_zinipay_edit_instructions, pattern="^admin_zinipay_edit_instructions$"),
    ]

    # All text states share the same handler — the field being edited is
    # stored in context.user_data["zinipay_editing_field"].
    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zinipay_receive_value)

    return ConversationHandler(
        entry_points=entry_points,
        states={
            ZINIPAY_EDIT_API_KEY:      [text_handler],
            ZINIPAY_EDIT_BKASH:        [text_handler],
            ZINIPAY_EDIT_NAGAD:        [text_handler],
            ZINIPAY_EDIT_ROCKET:       [text_handler],
            ZINIPAY_EDIT_UPAY:         [text_handler],
            ZINIPAY_EDIT_RATE:         [text_handler],
            ZINIPAY_EDIT_INSTRUCTIONS: [text_handler],
        },
        fallbacks=[
            CallbackQueryHandler(admin_zinipay_cancel, pattern="^admin_zinipay_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
