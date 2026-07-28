"""Admin panel controls for the ZiniPay payment gateway.

Covers every user-configurable field:
  • API Key
  • bKash / Nagad / Rocket / Upay merchant numbers
  • Per-provider enable / disable (⚪ Not Configured when no wallet number)
  • Default provider highlighted on the payment screen
  • USD → BDT exchange rate (manual, auto-refresh, refresh now, reset)
  • Per-provider payment instructions (bKash / Nagad / Rocket / Upay)
  • Global payment instructions (fallback)
  • Deposit bonus settings (%, enable/disable, min deposit, max bonus)
  • Enable / Disable toggle

All values are stored in PaymentGatewayConfig (gateway="zinipay").
None of the wallet numbers are ever hardcoded — they come exclusively from
this admin panel and are served to users in _finish_zinipay_payment().

Callback namespaces:
  admin_zinipay_*   — main view / provider management / field edits
  zpi:*             — per-provider payment instructions
  zper:*            — exchange rate panel
  zpb:*             — bonus settings panel
"""
from __future__ import annotations

import logging
from typing import Optional

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
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    ZINIPAY_EDIT_API_KEY,      # 0
    ZINIPAY_EDIT_BKASH,        # 1
    ZINIPAY_EDIT_NAGAD,        # 2
    ZINIPAY_EDIT_ROCKET,       # 3
    ZINIPAY_EDIT_UPAY,         # 4
    ZINIPAY_EDIT_RATE,         # 5
    ZINIPAY_EDIT_INSTRUCTIONS, # 6 — global instructions
    ZINIPAY_EDIT_PROV_INSTR,   # 7 — per-provider instruction text
    ZINIPAY_EDIT_BONUS_PCT,    # 8
    ZINIPAY_EDIT_BONUS_MIN,    # 9
    ZINIPAY_EDIT_BONUS_MAX,    # 10
) = range(11)

# Human-readable labels for each editable field
_FIELD_LABELS = {
    "api_key":                   "API Key",
    "zinipay_bkash_number":      "bKash Number",
    "zinipay_nagad_number":      "Nagad Number",
    "zinipay_rocket_number":     "Rocket Number",
    "zinipay_upay_number":       "Upay Number",
    "zinipay_usd_to_bdt_rate":   "USD → BDT Exchange Rate",
    "zinipay_instructions":      "Global Payment Instructions",
    "zinipay_bkash_instructions": "bKash Payment Instructions",
    "zinipay_nagad_instructions": "Nagad Payment Instructions",
    "zinipay_rocket_instructions":"Rocket Payment Instructions",
    "zinipay_upay_instructions":  "Upay Payment Instructions",
    "zinipay_bonus_percent":      "Deposit Bonus %",
    "zinipay_bonus_min_deposit":  "Minimum Deposit for Bonus",
    "zinipay_bonus_max_amount":   "Maximum Bonus Limit",
}

VALID_PROVIDERS = ("bkash", "nagad", "rocket", "upay")

# Provider display info: (emoji, display_name)
_PROVIDER_INFO = {
    "bkash":  ("💙", "bKash"),
    "nagad":  ("🧡", "Nagad"),
    "rocket": ("💜", "Rocket"),
    "upay":   ("🔵", "Upay"),
}


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
            "enabled":            bool(row.is_enabled),
            "api_key":            row.api_key or "",
            "bkash":              row.zinipay_bkash_number or "",
            "nagad":              row.zinipay_nagad_number or "",
            "rocket":             row.zinipay_rocket_number or "",
            "upay":               row.zinipay_upay_number or "",
            "default_provider":   row.zinipay_default_provider or "bkash",
            "rate":               row.zinipay_usd_to_bdt_rate,
            "auto_rate":          bool(row.zinipay_auto_rate),
            "instructions":       row.zinipay_instructions or "",
            # Per-provider instructions (column may not exist if not yet migrated)
            "bkash_instructions":  getattr(row, "zinipay_bkash_instructions",  None) or "",
            "nagad_instructions":  getattr(row, "zinipay_nagad_instructions",  None) or "",
            "rocket_instructions": getattr(row, "zinipay_rocket_instructions", None) or "",
            "upay_instructions":   getattr(row, "zinipay_upay_instructions",   None) or "",
            # Bonus settings
            "bonus_percent":       float(getattr(row, "zinipay_bonus_percent",     None) or 0.0),
            "bonus_enabled":       bool(getattr(row, "zinipay_bonus_enabled",      False)),
            "bonus_min_deposit":   getattr(row, "zinipay_bonus_min_deposit", None),
            "bonus_max_amount":    getattr(row, "zinipay_bonus_max_amount",  None),
        }


# ---------------------------------------------------------------------------
# Per-provider active state (stored in bot_config)
# ---------------------------------------------------------------------------

def _provider_is_active(provider: str) -> bool:
    """Return True if the provider is enabled by admin (default: True)."""
    return cfg.get_str(f"zinipay_prov_{provider}_active", "1") == "1"


def _set_provider_active(provider: str, active: bool) -> None:
    cfg.set(f"zinipay_prov_{provider}_active", "1" if active else "0")


def _provider_status(provider: str, wallet_number: str) -> str:
    if not wallet_number:
        return "⚪ Not Configured"
    if _provider_is_active(provider):
        return "✅ Enabled"
    return "🔴 Disabled"


def _provider_is_visible(provider: str, wallet_number: str) -> bool:
    """Return True if the provider should appear in the user payment menu."""
    return bool(wallet_number) and _provider_is_active(provider)


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


def _instr_preview(text: str, max_chars: int = 50) -> str:
    if not text:
        return "❌ <i>not set</i>"
    return (text[:max_chars] + "…") if len(text) > max_chars else text


def _summary(config: dict) -> str:
    status = "✅ Enabled" if config["enabled"] else "🚫 Disabled"
    if config["rate"]:
        rate_display = f"{config['rate']:.2f} BDT/USD"
        if config["auto_rate"]:
            rate_display += " (auto-refresh ✅)"
    else:
        rate_display = "🌐 Global rate"
        if config["auto_rate"]:
            rate_display += " (auto-refresh ✅)"

    instr_preview = _instr_preview(config["instructions"])

    provider_lines = ""
    for prov in VALID_PROVIDERS:
        emoji, name = _PROVIDER_INFO[prov]
        wallet = config[prov]
        status_badge = _provider_status(prov, wallet)
        if wallet:
            masked = wallet[:12] + ("…" if len(wallet) > 12 else "")
            provider_lines += f"  {emoji} <b>{name}:</b> {status_badge}  (<code>{masked}</code>)\n"
        else:
            provider_lines += f"  {emoji} <b>{name}:</b> {status_badge}\n"

    # Bonus display
    if config["bonus_enabled"] and config["bonus_percent"] > 0:
        bonus_line = f"🎁 Bonus: {config['bonus_percent']:.2f}% ✅"
    elif config["bonus_percent"] > 0:
        bonus_line = f"🎁 Bonus: {config['bonus_percent']:.2f}% 🚫 Disabled"
    else:
        bonus_line = "🎁 Bonus: Not configured"

    # Count visible providers
    visible = sum(
        1 for p in VALID_PROVIDERS
        if _provider_is_visible(config[p] != "", config[p])
    )

    return (
        "🇧🇩 <b>ZiniPay / Mobile Banking</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>API Key:</b> {_mask(config['api_key'])}\n\n"
        "<b>Providers:</b>\n"
        f"{provider_lines}\n"
        f"<b>Default Provider:</b> {config['default_provider'].title()}\n"
        f"<b>Exchange Rate:</b> {rate_display}\n"
        f"<b>Global Instructions:</b> {instr_preview}\n"
        f"{bonus_line}\n\n"
        "⚠️ API Key and at least one wallet number must be set before enabling."
    )


def _keyboard(config: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if config["enabled"] else "✅ Enable"

    # Provider rows
    provider_rows = []
    for prov in VALID_PROVIDERS:
        emoji, name = _PROVIDER_INFO[prov]
        wallet = config[prov]
        edit_field = f"admin_zinipay_edit_{prov}"

        if not wallet:
            provider_rows.append([
                InlineKeyboardButton(
                    f"{emoji} {name}: ⚪ Not Configured",
                    callback_data=f"admin_zinipay_provinfo_{prov}",
                ),
                InlineKeyboardButton("✏️ Set", callback_data=edit_field),
            ])
        else:
            is_active = _provider_is_active(prov)
            status_icon = "✅" if is_active else "🔴"
            toggle_cb = f"admin_zinipay_toggle_prov_{prov}"
            provider_rows.append([
                InlineKeyboardButton(
                    f"{emoji} {name}: {status_icon}",
                    callback_data=toggle_cb,
                ),
                InlineKeyboardButton("✏️ Wallet", callback_data=edit_field),
            ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 API Key", callback_data="admin_zinipay_edit_apikey")],
        *provider_rows,
        [InlineKeyboardButton("🏦 Default Provider", callback_data="admin_zinipay_provider_menu")],
        [InlineKeyboardButton("📋 Payment Instructions", callback_data="zpi:menu")],
        [InlineKeyboardButton("💱 Exchange Rate", callback_data="zper:menu")],
        [InlineKeyboardButton("🎁 Bonus Settings", callback_data="zpb:menu")],
        [InlineKeyboardButton("📱 Mobile Banking Manager", callback_data="mb:menu")],
        [InlineKeyboardButton("💳 Wallet Manager", callback_data="gww:list:zinipay")],
        [InlineKeyboardButton(toggle_label, callback_data="admin_zinipay_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")],
    ])


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------

async def admin_zinipay_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    config = _load_cfg()
    try:
        await query.edit_message_text(
            _summary(config), reply_markup=_keyboard(config), parse_mode="HTML"
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

    config = _load_cfg()
    if not config["enabled"]:
        missing = []
        if not config["api_key"]:
            missing.append("API Key")
        has_any_wallet = any([config["bkash"], config["nagad"], config["rocket"], config["upay"]])
        if not has_any_wallet:
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
# Per-provider toggle
# ---------------------------------------------------------------------------

async def admin_zinipay_toggle_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    provider = query.data.replace("admin_zinipay_toggle_prov_", "")
    if provider not in VALID_PROVIDERS:
        await query.answer("⚠️ Unknown provider.", show_alert=True)
        return

    config = _load_cfg()
    wallet = config.get(provider, "")
    if not wallet:
        await query.answer(
            "⚠️ This provider is not configured. Set a wallet number first.",
            show_alert=True,
        )
        return

    current = _provider_is_active(provider)
    _set_provider_active(provider, not current)

    emoji, name = _PROVIDER_INFO[provider]
    new_status = "✅ Enabled" if not current else "🔴 Disabled"
    await query.answer(f"{emoji} {name} is now {new_status}.", show_alert=False)
    await admin_zinipay_view(update, context)


# ---------------------------------------------------------------------------
# Provider info popup (unconfigured)
# ---------------------------------------------------------------------------

async def admin_zinipay_provider_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    provider = query.data.replace("admin_zinipay_provinfo_", "")
    emoji, name = _PROVIDER_INFO.get(provider, ("📱", provider.title()))
    await query.answer(
        f"{emoji} {name}: Not configured yet.\n"
        "Tap ✏️ Set to add a wallet number.",
        show_alert=True,
    )


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
# Default provider selection
# ---------------------------------------------------------------------------

async def admin_zinipay_provider_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    config = _load_cfg()
    current = config["default_provider"]
    buttons = []
    for p in VALID_PROVIDERS:
        emoji, name = _PROVIDER_INFO[p]
        wallet = config.get(p, "")
        selected = "✅ " if p == current else ""
        not_cfg = " (no wallet)" if not wallet else ""
        label = f"{selected}{emoji} {name}{not_cfg}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"admin_zinipay_setprovider_{p}"))

    keyboard = InlineKeyboardMarkup([
        buttons[:2], buttons[2:],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_zinipay_view")],
    ])
    try:
        await query.edit_message_text(
            "🏦 <b>Select Default Payment Provider</b>\n\n"
            "This provider is highlighted first on the user's payment screen.\n"
            "Only configured providers can become the default.",
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

    config = _load_cfg()
    wallet = config.get(provider, "")
    if not wallet:
        emoji, name = _PROVIDER_INFO.get(provider, ("📱", provider.title()))
        await query.answer(
            f"⚠️ {name} has no wallet number.\nConfigure a wallet first.",
            show_alert=True,
        )
        return

    with get_db_session() as session:
        row = _get_or_create(session)
        row.zinipay_default_provider = provider
        session.commit()

    await admin_zinipay_view(update, context)


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════
# SECTION A: Per-Provider Payment Instructions  (zpi:*)
# ═══════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

def _prov_instr_col(provider: str) -> str:
    return f"zinipay_{provider}_instructions"


def _load_prov_instr(provider: str) -> str:
    with get_db_session() as session:
        row = _get_or_create(session)
        return getattr(row, _prov_instr_col(provider), None) or ""


def _save_prov_instr(provider: str, text: Optional[str]) -> None:
    col = _prov_instr_col(provider)
    with get_db_session() as session:
        row = _get_or_create(session)
        setattr(row, col, text)
        session.commit()


def _zpi_menu_text(config: dict) -> str:
    lines = ["📋 <b>Payment Instructions Manager</b>\n"]
    for prov in VALID_PROVIDERS:
        emoji, name = _PROVIDER_INFO[prov]
        instr = config.get(f"{prov}_instructions", "")
        if instr:
            preview = (instr[:40] + "…") if len(instr) > 40 else instr
            lines.append(f"{emoji} <b>{name}:</b> {preview}")
        else:
            global_instr = config.get("instructions", "")
            if global_instr:
                lines.append(f"{emoji} <b>{name}:</b> <i>(using global)</i>")
            else:
                lines.append(f"{emoji} <b>{name}:</b> ❌ <i>not set</i>")
    lines.append("\n<i>Each provider can have its own instructions.\nFalls back to Global if not set.</i>")
    return "\n".join(lines)


def _zpi_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for prov in VALID_PROVIDERS:
        emoji, name = _PROVIDER_INFO[prov]
        rows.append([
            InlineKeyboardButton(f"{emoji} {name}", callback_data=f"zpi:view:{prov}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"zpi:edit:{prov}"),
        ])
    rows.append([InlineKeyboardButton("🌐 Global Instructions", callback_data="admin_zinipay_edit_instructions")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_zinipay_view")])
    return InlineKeyboardMarkup(rows)


async def zpi_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Instructions management menu  (zpi:menu)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zpi_menu_text(config),
            reply_markup=_zpi_menu_keyboard(),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zpi_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View instructions for one provider  (zpi:view:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    provider = query.data.split(":")[2]
    if provider not in VALID_PROVIDERS:
        return

    emoji, name = _PROVIDER_INFO[provider]
    instr = _load_prov_instr(provider)
    config = _load_cfg()
    global_instr = config.get("instructions", "")

    if instr:
        text = (
            f"{emoji} <b>{name} Payment Instructions</b>\n\n"
            f"{instr}"
        )
    elif global_instr:
        text = (
            f"{emoji} <b>{name} Payment Instructions</b>\n\n"
            f"<i>(No provider-specific instructions. Showing global:)</i>\n\n"
            f"{global_instr}"
        )
    else:
        text = (
            f"{emoji} <b>{name} Payment Instructions</b>\n\n"
            "❌ No instructions set for this provider.\n"
            "Tap ✏️ Edit to add instructions."
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"zpi:edit:{provider}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"zpi:del:{provider}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="zpi:menu")],
    ])
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zpi_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete instructions for a provider  (zpi:del:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    provider = query.data.split(":")[2]
    if provider not in VALID_PROVIDERS:
        return

    _save_prov_instr(provider, None)
    emoji, name = _PROVIDER_INFO[provider]
    await query.answer(f"🗑 {name} instructions cleared.", show_alert=False)

    # Return to the instructions menu
    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zpi_menu_text(config),
            reply_markup=_zpi_menu_keyboard(),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zpi_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start editing provider instructions  (zpi:edit:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    provider = query.data.split(":")[2]
    if provider not in VALID_PROVIDERS:
        return ConversationHandler.END

    emoji, name = _PROVIDER_INFO[provider]
    current = _load_prov_instr(provider)
    context.user_data["zpi_editing_provider"] = provider

    current_display = current or "(not set)"
    try:
        await query.edit_message_text(
            f"{emoji} <b>Edit {name} Payment Instructions</b>\n\n"
            f"Current:\n<i>{current_display}</i>\n\n"
            "Send the new instructions text.\n"
            "Example:\n"
            "<i>Open bKash app → Send Money → Enter exact amount → "
            "Copy the TrxID → Submit below.</i>\n\n"
            "Send <code>-</code> to clear (will use global instructions).\n"
            "/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="zpi:menu"),
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ZINIPAY_EDIT_PROV_INSTR


async def zpi_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new provider instructions text."""
    value = (update.message.text or "").strip()
    provider = context.user_data.pop("zpi_editing_provider", None)

    if not provider or provider not in VALID_PROVIDERS:
        await update.message.reply_text("❌ Session expired. Try again.")
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❌ Cannot be empty. Send instructions or /cancel.")
        context.user_data["zpi_editing_provider"] = provider
        return ZINIPAY_EDIT_PROV_INSTR

    clear = value == "-"
    _save_prov_instr(provider, None if clear else value[:2000])

    emoji, name = _PROVIDER_INFO[provider]
    saved = "cleared." if clear else "saved."
    await update.message.reply_text(
        f"✅ <b>{name} instructions</b> {saved}",
        parse_mode="HTML",
    )

    # Rebuild the instructions menu in a new message
    config = _load_cfg()
    await update.message.reply_text(
        _zpi_menu_text(config),
        reply_markup=_zpi_menu_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def zpi_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("zpi_editing_provider", None)
    if update.callback_query:
        await update.callback_query.answer()
        config = _load_cfg()
        try:
            await update.callback_query.edit_message_text(
                _zpi_menu_text(config),
                reply_markup=_zpi_menu_keyboard(),
                parse_mode="HTML",
            )
        except BadRequest:
            pass
    return ConversationHandler.END


async def zpi_edit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("zpi_editing_provider", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════
# SECTION B: Exchange Rate Panel  (zper:*)
# ═══════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

def _zper_text(config: dict) -> str:
    if config["rate"]:
        rate_str = f"<b>{config['rate']:.4f} BDT</b> per USD"
        mode = "Manual"
    else:
        try:
            from services.pricing import get_usd_to_bdt_rate
            live = get_usd_to_bdt_rate()
            rate_str = f"<b>{live:.4f} BDT</b> per USD (global/live)"
        except Exception:
            rate_str = "(global rate — check Settings)"
        mode = "Global"

    auto_status = "✅ Auto-refresh ON" if config["auto_rate"] else "🚫 Auto-refresh OFF"
    return (
        "💱 <b>Exchange Rate — ZiniPay</b>\n\n"
        f"Current Rate: {rate_str}\n"
        f"Mode: {mode}\n"
        f"Auto-Refresh: {auto_status}\n\n"
        "<i>Manual rate overrides the global Settings rate for ZiniPay only.\n"
        "Reset removes the override and uses the global rate again.</i>"
    )


def _zper_keyboard(config: dict) -> InlineKeyboardMarkup:
    auto_label = "⏹ Disable Auto-Refresh" if config["auto_rate"] else "🔄 Enable Auto-Refresh"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Manual Rate", callback_data="admin_zinipay_edit_rate")],
        [
            InlineKeyboardButton("🔄 Refresh Now", callback_data="zper:refresh"),
            InlineKeyboardButton("🗑 Reset to Global", callback_data="zper:reset"),
        ],
        [InlineKeyboardButton(auto_label, callback_data="admin_zinipay_toggle_autorate")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_zinipay_view")],
    ])


async def zper_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exchange rate management panel  (zper:menu)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zper_text(config),
            reply_markup=_zper_keyboard(config),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zper_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch the latest rate from the global API now  (zper:refresh)."""
    query = update.callback_query
    await query.answer("🔄 Fetching rate…")
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    try:
        from services.pricing import get_usd_to_bdt_rate
        rate = get_usd_to_bdt_rate(force_refresh=True)
        await query.answer(f"✅ Rate refreshed: {rate:.4f} BDT/USD", show_alert=True)
    except Exception as exc:
        await query.answer(f"❌ Refresh failed: {exc}", show_alert=True)

    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zper_text(config),
            reply_markup=_zper_keyboard(config),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zper_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the manual rate override  (zper:reset)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    with get_db_session() as session:
        row = _get_or_create(session)
        row.zinipay_usd_to_bdt_rate = None
        session.commit()

    await query.answer("✅ Rate reset to global.", show_alert=False)
    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zper_text(config),
            reply_markup=_zper_keyboard(config),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════
# SECTION C: Bonus Settings  (zpb:*)
# ═══════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

def _zpb_text(config: dict) -> str:
    pct = config["bonus_percent"]
    enabled = config["bonus_enabled"]
    min_dep = config["bonus_min_deposit"]
    max_bonus = config["bonus_max_amount"]

    status_icon = "✅ Enabled" if enabled else "🚫 Disabled"
    min_str = f"${min_dep:.2f}" if min_dep else "No minimum"
    max_str = f"${max_bonus:.2f}" if max_bonus else "No limit"

    return (
        "🎁 <b>Deposit Bonus Settings — ZiniPay</b>\n\n"
        f"Status: {status_icon}\n"
        f"Bonus Percent: <b>{pct:.2f}%</b>\n"
        f"Min Deposit for Bonus: <b>{min_str}</b>\n"
        f"Max Bonus Limit: <b>{max_str}</b>\n\n"
        "<i>Example: 5% bonus on deposits ≥ $10, capped at $50.\n"
        "Enable bonus and set percent to activate.</i>"
    )


def _zpb_keyboard(config: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable Bonus" if config["bonus_enabled"] else "✅ Enable Bonus"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="zpb:tog")],
        [InlineKeyboardButton("💰 Bonus % (edit)", callback_data="zpb:edit:pct")],
        [InlineKeyboardButton("💵 Min Deposit for Bonus", callback_data="zpb:edit:min")],
        [InlineKeyboardButton("💰 Max Bonus Limit", callback_data="zpb:edit:max")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_zinipay_view")],
    ])


async def zpb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bonus settings panel  (zpb:menu)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    config = _load_cfg()
    try:
        await query.edit_message_text(
            _zpb_text(config),
            reply_markup=_zpb_keyboard(config),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zpb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle bonus enabled/disabled  (zpb:tog)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    config = _load_cfg()
    if not config["bonus_enabled"] and config["bonus_percent"] <= 0:
        await query.answer(
            "⚠️ Set a bonus percent first before enabling the bonus.",
            show_alert=True,
        )
        return

    with get_db_session() as session:
        row = _get_or_create(session)
        current = getattr(row, "zinipay_bonus_enabled", False)
        setattr(row, "zinipay_bonus_enabled", not current)
        session.commit()

    config = _load_cfg()
    new_state = "✅ Enabled" if config["bonus_enabled"] else "🚫 Disabled"
    await query.answer(f"Bonus {new_state}.", show_alert=False)
    try:
        await query.edit_message_text(
            _zpb_text(config),
            reply_markup=_zpb_keyboard(config),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def zpb_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start editing a bonus field  (zpb:edit:{field})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    parts = query.data.split(":")
    field = parts[2] if len(parts) > 2 else ""
    if field not in ("pct", "min", "max"):
        return ConversationHandler.END

    config = _load_cfg()
    context.user_data["zpb_editing"] = field

    if field == "pct":
        current = f"{config['bonus_percent']:.2f}%"
        prompt = (
            "💰 <b>Edit Deposit Bonus %</b>\n\n"
            f"Current: <b>{current}</b>\n\n"
            "Send the new bonus percentage.\n"
            "Example: <code>5</code> for 5%, <code>2.5</code> for 2.5%\n"
            "Send <code>0</code> to disable the bonus.\n/cancel to abort."
        )
        state = ZINIPAY_EDIT_BONUS_PCT
    elif field == "min":
        current = f"${config['bonus_min_deposit']:.2f}" if config["bonus_min_deposit"] else "No minimum"
        prompt = (
            "💵 <b>Minimum Deposit for Bonus</b>\n\n"
            f"Current: <b>{current}</b>\n\n"
            "Send the minimum deposit amount in USD for users to receive the bonus.\n"
            "Example: <code>10</code> means users must deposit at least $10.\n"
            "Send <code>0</code> or <code>-</code> to remove the minimum.\n/cancel to abort."
        )
        state = ZINIPAY_EDIT_BONUS_MIN
    else:
        current = f"${config['bonus_max_amount']:.2f}" if config["bonus_max_amount"] else "No limit"
        prompt = (
            "💰 <b>Maximum Bonus Limit</b>\n\n"
            f"Current: <b>{current}</b>\n\n"
            "Send the maximum bonus amount in USD a user can receive per deposit.\n"
            "Example: <code>50</code> means the bonus will never exceed $50.\n"
            "Send <code>0</code> or <code>-</code> to remove the limit.\n/cancel to abort."
        )
        state = ZINIPAY_EDIT_BONUS_MAX

    try:
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="zpb:menu"),
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return state


async def zpb_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a bonus field value."""
    value = (update.message.text or "").strip()
    field = context.user_data.pop("zpb_editing", None)

    if not field:
        await update.message.reply_text("❌ Session expired. Try again.")
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❌ Cannot be empty. Send a value or /cancel.")
        context.user_data["zpb_editing"] = field
        return ZINIPAY_EDIT_BONUS_PCT if field == "pct" else (
            ZINIPAY_EDIT_BONUS_MIN if field == "min" else ZINIPAY_EDIT_BONUS_MAX
        )

    clear = value in ("-", "0")

    if field == "pct":
        if clear:
            parsed = 0.0
        else:
            try:
                parsed = float(value.replace(",", "."))
                if parsed < 0 or parsed > 100:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Please send a number between 0 and 100 (e.g. <code>5</code>).",
                    parse_mode="HTML",
                )
                context.user_data["zpb_editing"] = field
                return ZINIPAY_EDIT_BONUS_PCT

        with get_db_session() as session:
            row = _get_or_create(session)
            setattr(row, "zinipay_bonus_percent", parsed)
            session.commit()

        await update.message.reply_text(
            f"✅ <b>Bonus %</b> set to <b>{parsed:.2f}%</b>.", parse_mode="HTML"
        )

    elif field == "min":
        if clear:
            save_val = None
        else:
            try:
                parsed = float(value.replace(",", "."))
                if parsed < 0:
                    raise ValueError
                save_val = parsed
            except ValueError:
                await update.message.reply_text(
                    "❌ Please send a positive number (e.g. <code>10</code>).",
                    parse_mode="HTML",
                )
                context.user_data["zpb_editing"] = field
                return ZINIPAY_EDIT_BONUS_MIN

        with get_db_session() as session:
            row = _get_or_create(session)
            setattr(row, "zinipay_bonus_min_deposit", save_val)
            session.commit()

        label = f"${save_val:.2f}" if save_val else "removed"
        await update.message.reply_text(
            f"✅ <b>Min deposit for bonus</b> set to <b>{label}</b>.", parse_mode="HTML"
        )

    else:  # max
        if clear:
            save_val = None
        else:
            try:
                parsed = float(value.replace(",", "."))
                if parsed < 0:
                    raise ValueError
                save_val = parsed
            except ValueError:
                await update.message.reply_text(
                    "❌ Please send a positive number (e.g. <code>50</code>).",
                    parse_mode="HTML",
                )
                context.user_data["zpb_editing"] = field
                return ZINIPAY_EDIT_BONUS_MAX

        with get_db_session() as session:
            row = _get_or_create(session)
            setattr(row, "zinipay_bonus_max_amount", save_val)
            session.commit()

        label = f"${save_val:.2f}" if save_val else "removed"
        await update.message.reply_text(
            f"✅ <b>Max bonus limit</b> set to <b>{label}</b>.", parse_mode="HTML"
        )

    config = _load_cfg()
    await update.message.reply_text(
        _zpb_text(config),
        reply_markup=_zpb_keyboard(config),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def zpb_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("zpb_editing", None)
    if update.callback_query:
        await update.callback_query.answer()
        config = _load_cfg()
        try:
            await update.callback_query.edit_message_text(
                _zpb_text(config),
                reply_markup=_zpb_keyboard(config),
                parse_mode="HTML",
            )
        except BadRequest:
            pass
    return ConversationHandler.END


async def zpb_edit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("zpb_editing", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════
# SECTION D: Generic field editor  (existing functionality)
# ═══════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

async def _start_edit(query, context, field_key: str, prompt: str, state: int):
    context.user_data["zinipay_editing_field"] = field_key
    try:
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_zinipay_view")]
            ]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return state


async def admin_zinipay_edit_apikey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "api_key",
        "🔑 Send your ZiniPay <b>Brand / API Key</b>\n"
        "(ZiniPay dashboard → Brands → your brand → API Key).\n\n"
        "🔒 Sensitive — stored securely; not echoed back after saving.\n\n"
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
        "💙 Send the <b>bKash merchant number</b> users should send money to.\n\n"
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
        "🧡 Send the <b>Nagad merchant number</b> users should send money to.\n\n"
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
        "💜 Send the <b>Rocket (DBBL) merchant number</b> users should send money to.\n\n"
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
        "🔵 Send the <b>Upay merchant number</b> users should send money to.\n\n"
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
        "Send <code>-</code> or <code>0</code> to clear and use the global rate.",
        ZINIPAY_EDIT_RATE,
    )


async def admin_zinipay_edit_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    return await _start_edit(
        query, context, "zinipay_instructions",
        "📋 Send the <b>global payment instructions</b> shown on the payment screen.\n\n"
        "These apply to all providers unless a provider-specific instruction is set.\n\n"
        "Example:\n"
        "<i>Open your app → Send Money → Enter exact amount → "
        "Copy Transaction ID → Press Submit Transaction ID</i>\n\n"
        "Send <code>-</code> to clear.",
        ZINIPAY_EDIT_INSTRUCTIONS,
    )


async def admin_zinipay_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save whichever wallet/rate/instructions field is currently being edited."""
    value = (update.message.text or "").strip()
    field = context.user_data.get("zinipay_editing_field", "")

    if not field:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❌ Please send a value, or press Cancel.")
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

    clear = value == "-"
    save_value: object = None if clear else value

    # Special handling for rate
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
    config = _load_cfg()
    await update.message.reply_text(
        f"✅ <b>{label}</b> {saved_text}", parse_mode="HTML"
    )
    await update.message.reply_text(
        _summary(config), reply_markup=_keyboard(config), parse_mode="HTML"
    )
    return ConversationHandler.END


async def admin_zinipay_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any active edit and return to the ZiniPay view."""
    context.user_data.pop("zinipay_editing_field", None)
    if update.callback_query:
        await admin_zinipay_view(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factories
# ---------------------------------------------------------------------------

def build_zinipay_edit_conv() -> ConversationHandler:
    """ConversationHandler for all ZiniPay wallet/rate/instructions edits."""
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


def build_zpi_conv() -> ConversationHandler:
    """ConversationHandler for per-provider instructions editing."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(zpi_edit_start, pattern=r"^zpi:edit:[a-z]+$"),
        ],
        states={
            ZINIPAY_EDIT_PROV_INSTR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zpi_edit_receive),
                CallbackQueryHandler(zpi_edit_cancel, pattern=r"^zpi:menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", zpi_edit_cancel_cmd),
            CallbackQueryHandler(zpi_edit_cancel, pattern=r"^zpi:menu$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


def build_zpb_conv() -> ConversationHandler:
    """ConversationHandler for bonus settings editing."""
    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, zpb_edit_receive)
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(zpb_edit_start, pattern=r"^zpb:edit:(pct|min|max)$"),
        ],
        states={
            ZINIPAY_EDIT_BONUS_PCT: [
                text_handler,
                CallbackQueryHandler(zpb_edit_cancel, pattern=r"^zpb:menu$"),
            ],
            ZINIPAY_EDIT_BONUS_MIN: [
                text_handler,
                CallbackQueryHandler(zpb_edit_cancel, pattern=r"^zpb:menu$"),
            ],
            ZINIPAY_EDIT_BONUS_MAX: [
                text_handler,
                CallbackQueryHandler(zpb_edit_cancel, pattern=r"^zpb:menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", zpb_edit_cancel_cmd),
            CallbackQueryHandler(zpb_edit_cancel, pattern=r"^zpb:menu$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


def register_all_handlers(app) -> None:
    """Register ALL ZiniPay admin handlers.

    Call this from bot.py instead of the old individual
    application.add_handler() calls.  This function is ADDITIVE —
    it does NOT remove any existing handler registrations.
    """
    # Conversations (must be registered before simple callbacks)
    app.add_handler(build_zinipay_edit_conv())
    app.add_handler(build_zpi_conv())
    app.add_handler(build_zpb_conv())

    # ── Main view / toggle ──────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(admin_zinipay_view,     pattern="^admin_zinipay_view$"))
    app.add_handler(CallbackQueryHandler(admin_zinipay_toggle,   pattern="^admin_zinipay_toggle$"))
    # ── Provider management ─────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(admin_zinipay_toggle_autorate,  pattern="^admin_zinipay_toggle_autorate$"))
    app.add_handler(CallbackQueryHandler(admin_zinipay_provider_menu,    pattern="^admin_zinipay_provider_menu$"))
    app.add_handler(CallbackQueryHandler(admin_zinipay_set_provider,     pattern="^admin_zinipay_setprovider_"))
    app.add_handler(CallbackQueryHandler(admin_zinipay_toggle_provider,  pattern="^admin_zinipay_toggle_prov_"))
    app.add_handler(CallbackQueryHandler(admin_zinipay_provider_info,    pattern="^admin_zinipay_provinfo_"))
    # ── Per-provider instructions (zpi:) ────────────────────────────────
    app.add_handler(CallbackQueryHandler(zpi_menu,   pattern=r"^zpi:menu$"))
    app.add_handler(CallbackQueryHandler(zpi_view,   pattern=r"^zpi:view:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(zpi_delete, pattern=r"^zpi:del:[a-z]+$"))
    # ── Exchange rate panel (zper:) ─────────────────────────────────────
    app.add_handler(CallbackQueryHandler(zper_menu,    pattern=r"^zper:menu$"))
    app.add_handler(CallbackQueryHandler(zper_refresh, pattern=r"^zper:refresh$"))
    app.add_handler(CallbackQueryHandler(zper_reset,   pattern=r"^zper:reset$"))
    # ── Bonus settings (zpb:) ───────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(zpb_menu,   pattern=r"^zpb:menu$"))
    app.add_handler(CallbackQueryHandler(zpb_toggle, pattern=r"^zpb:tog$"))
