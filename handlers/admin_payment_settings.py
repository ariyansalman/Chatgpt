"""Admin panel: Global Payment Settings.

Accessible via: Payment Gateways → ⚙️ Payment Settings

Callback namespace: ``ps:``

All values live in ``bot_config`` (key/value store) — zero database schema
changes.  Payment processing logic in ``payment_handlers.py`` / ``services/``
is NOT modified.

Settings managed here
─────────────────────
 1. Minimum Deposit          topup_min_amount          float  (USD)
    Enable/Disable toggle    minimum_deposit_enabled   bool
 2. Maximum Deposit          topup_max_amount          float  (USD, 0 = unlimited)
 3. Exchange Rate            ps_manual_exchange_rate   float  (local currency per 1 USD)
    Currency label           ps_rate_currency          str    (e.g. BDT, INR, PKR)
 4. Auto Exchange Rate       erm_auto_update_enabled   bool
 5. Deposit Expiry           payment_expiry_minutes    int    (minutes)
 6. Pending Timeout          ps_pending_timeout_min    int    (minutes, 0 = no timeout)
 7. Auto Cancel Pending      ps_auto_cancel_pending    bool
 8. Maximum Pending Deposits ps_max_pending_deposits   int    (per user, 0 = unlimited)
 9. Payment Instructions     ps_payment_instructions   text
10. Gateway Status           ps_gateway_status         str    (enabled/maintenance/disabled)
11. Maintenance Mode         maintenance_mode          bool

Conversation states:   PS_EDIT_VAL = 9500
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation state ─────────────────────────────────────────────────────
PS_EDIT_VAL = 9500

# ── Config key map  (shorthand -> actual bot_config key) ──────────────────
_KEY_MAP: dict[str, str] = {
    "min_amount":    "topup_min_amount",
    "max_amount":    "topup_max_amount",
    "rate":          "ps_manual_exchange_rate",
    "currency":      "ps_rate_currency",
    "expiry":        "payment_expiry_minutes",
    "timeout":       "ps_pending_timeout_min",
    "max_pending":   "ps_max_pending_deposits",
    "instructions":  "ps_payment_instructions",
}

_BOOL_KEY_MAP: dict[str, str] = {
    "min_enabled":   "minimum_deposit_enabled",
    "auto_rate":     "erm_auto_update_enabled",
    "auto_cancel":   "ps_auto_cancel_pending",
    "maintenance":   "maintenance_mode",
}

# The gateway-status key cycles: enabled → maintenance → disabled → enabled
_GW_STATUS_KEY = "ps_gateway_status"
_GW_CYCLE = ["enabled", "maintenance", "disabled"]

# ── Defaults (used when key has never been set) ────────────────────────────
_DEFAULTS: dict[str, object] = {
    "topup_min_amount":          1.0,
    "topup_max_amount":          0.0,
    "ps_manual_exchange_rate":   0.0,
    "ps_rate_currency":          "USD",
    "payment_expiry_minutes":    30,
    "ps_pending_timeout_min":    60,
    "ps_max_pending_deposits":   0,
    "ps_payment_instructions":   "",
    "minimum_deposit_enabled":   False,
    "erm_auto_update_enabled":   True,
    "ps_auto_cancel_pending":    False,
    "maintenance_mode":          False,
    "ps_gateway_status":         "enabled",
}


def _get(key: str) -> object:
    default = _DEFAULTS.get(key, "")
    if isinstance(default, bool):
        return cfg.get_bool(key, default)
    if isinstance(default, int):
        return cfg.get_int(key, default)
    if isinstance(default, float):
        return cfg.get_float(key, default)
    return cfg.get_str(key, str(default))


# ── Display helpers ────────────────────────────────────────────────────────

def _bool_icon(key: str) -> str:
    return "🟢" if _get(key) else "🔴"


def _gw_status_icon(status: str) -> str:
    return {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "❓")


def _fmt_usd(v: float) -> str:
    return f"${v:.2f}" if v else "(no limit)"


def _fmt_rate(rate: float, currency: str) -> str:
    if not rate:
        return f"Not set ({currency}/USD)"
    return f"{rate:.4f} {currency}/USD"


def _settings_text() -> str:
    min_amt      = _get("topup_min_amount")
    max_amt      = _get("topup_max_amount")
    min_enabled  = _get("minimum_deposit_enabled")
    rate         = _get("ps_manual_exchange_rate")
    currency     = _get("ps_rate_currency") or "USD"
    auto_rate    = _get("erm_auto_update_enabled")
    expiry       = _get("payment_expiry_minutes")
    timeout      = _get("ps_pending_timeout_min")
    auto_cancel  = _get("ps_auto_cancel_pending")
    max_pending  = _get("ps_max_pending_deposits")
    instructions = (_get("ps_payment_instructions") or "(not set)")
    gw_status    = _get("ps_gateway_status") or "enabled"
    maintenance  = _get("maintenance_mode")

    min_icon  = "🟢" if min_enabled else "🔴"
    rate_icon = "🟢" if auto_rate else "🔴"
    ac_icon   = "🟢" if auto_cancel else "🔴"
    mt_icon   = "🟢" if maintenance else "🔴"
    gw_icon   = _gw_status_icon(gw_status)

    min_line  = f"{min_icon} Enabled  •  Amount: <b>{_fmt_usd(min_amt)}</b>" if min_enabled else f"{min_icon} Disabled  •  Amount: <b>{_fmt_usd(min_amt)}</b>"
    max_line  = _fmt_usd(max_amt) if max_amt else "No limit"
    rate_line = f"{rate_icon} Auto  •  Manual: <b>{_fmt_rate(rate, currency)}</b>"
    timeout_s = f"{timeout} min" if timeout else "No timeout"
    pending_s = f"{max_pending} per user" if max_pending else "Unlimited"
    instr_pre = (instructions[:60] + "…") if len(str(instructions)) > 60 else instructions

    return (
        "⚙️ <b>Payment Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>1. Minimum Deposit</b>  {min_line}\n"
        f"<b>2. Maximum Deposit</b>  {max_line}\n\n"
        f"<b>3. Exchange Rate</b>  {rate_line}\n\n"
        f"<b>4. Deposit Expiry</b>  {expiry} min\n"
        f"<b>5. Pending Timeout</b>  {timeout_s}\n"
        f"<b>6. Auto Cancel Pending</b>  {ac_icon} {'On' if auto_cancel else 'Off'}\n"
        f"<b>7. Max Pending Deposits</b>  {pending_s}\n\n"
        f"<b>8. Payment Instructions</b>\n"
        f"<code>{instr_pre}</code>\n\n"
        f"<b>9. Gateway Status</b>  {gw_icon} {gw_status.title()}\n"
        f"<b>10. Maintenance Mode</b>  {mt_icon} {'On' if maintenance else 'Off'}\n"
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    min_enabled = bool(_get("minimum_deposit_enabled"))
    auto_rate   = bool(_get("erm_auto_update_enabled"))
    auto_cancel = bool(_get("ps_auto_cancel_pending"))
    maintenance = bool(_get("maintenance_mode"))
    gw_status   = str(_get("ps_gateway_status") or "enabled")

    min_toggle  = "🔴 Disable Min" if min_enabled else "🟢 Enable Min"
    rate_toggle = "🔴 Disable Auto Rate" if auto_rate else "🟢 Enable Auto Rate"
    ac_toggle   = "🔴 Disable Auto Cancel" if auto_cancel else "🟢 Enable Auto Cancel"
    mt_toggle   = "🔴 Turn Off Maintenance" if maintenance else "🟢 Turn On Maintenance"
    gw_icon     = _gw_status_icon(gw_status)
    next_status = _GW_CYCLE[(_GW_CYCLE.index(gw_status) + 1) % 3] if gw_status in _GW_CYCLE else "enabled"

    return InlineKeyboardMarkup([
        # ── Minimum Deposit ──────────────────────────────────────────────
        [
            InlineKeyboardButton("✏️ Min Amount",   callback_data="ps:edit:min_amount"),
            InlineKeyboardButton(min_toggle,         callback_data="ps:tog:min_enabled"),
        ],
        # ── Maximum Deposit ──────────────────────────────────────────────
        [InlineKeyboardButton("✏️ Max Deposit Amount", callback_data="ps:edit:max_amount")],
        # ── Exchange Rate ────────────────────────────────────────────────
        [
            InlineKeyboardButton("✏️ Manual Rate",  callback_data="ps:edit:rate"),
            InlineKeyboardButton("✏️ Currency",     callback_data="ps:edit:currency"),
        ],
        [InlineKeyboardButton(rate_toggle,           callback_data="ps:tog:auto_rate")],
        # ── Deposit Expiry ───────────────────────────────────────────────
        [InlineKeyboardButton("✏️ Deposit Expiry (min)", callback_data="ps:edit:expiry")],
        # ── Pending Timeout ──────────────────────────────────────────────
        [InlineKeyboardButton("✏️ Pending Timeout (min)", callback_data="ps:edit:timeout")],
        # ── Auto Cancel ──────────────────────────────────────────────────
        [InlineKeyboardButton(ac_toggle,             callback_data="ps:tog:auto_cancel")],
        # ── Max Pending Deposits ─────────────────────────────────────────
        [InlineKeyboardButton("✏️ Max Pending Deposits", callback_data="ps:edit:max_pending")],
        # ── Payment Instructions ─────────────────────────────────────────
        [InlineKeyboardButton("✏️ Payment Instructions", callback_data="ps:edit:instructions")],
        # ── Gateway Status ───────────────────────────────────────────────
        [InlineKeyboardButton(
            f"{gw_icon} Gateway Status → {next_status.title()}",
            callback_data="ps:tri:gw_status",
        )],
        # ── Maintenance Mode ─────────────────────────────────────────────
        [InlineKeyboardButton(mt_toggle,             callback_data="ps:tog:maintenance")],
        # ── Navigation ───────────────────────────────────────────────────
        [InlineKeyboardButton("🔙 Back",             callback_data="admin_gateways")],
    ])


# ── Main view ──────────────────────────────────────────────────────────────

async def ps_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Payment Settings dashboard."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            _settings_text(),
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def _refresh(query, context) -> None:
    """Helper: re-render the dashboard after a change."""
    try:
        await query.edit_message_text(
            _settings_text(),
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Boolean toggles ────────────────────────────────────────────────────────

async def ps_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a boolean setting.  Callback: ``ps:tog:{shorthand}``"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    shorthand = query.data.split(":", 2)[2]   # e.g. "min_enabled"
    config_key = _BOOL_KEY_MAP.get(shorthand)
    if not config_key:
        await query.answer("⚠️ Unknown setting.", show_alert=True)
        return

    current = cfg.get_bool(config_key, bool(_DEFAULTS.get(config_key, False)))
    cfg.set(config_key, not current)
    state_label = "ON" if not current else "OFF"
    await query.answer(f"{'🟢' if not current else '🔴'} Turned {state_label}.", show_alert=False)
    await _refresh(query, context)


# ── Tri-state cycle ────────────────────────────────────────────────────────

async def ps_tristate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle a tri-state setting (enabled → maintenance → disabled).
    Callback: ``ps:tri:gw_status``
    """
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    current = cfg.get_str(_GW_STATUS_KEY, "enabled")
    if current not in _GW_CYCLE:
        current = "enabled"
    next_val = _GW_CYCLE[(_GW_CYCLE.index(current) + 1) % 3]
    cfg.set(_GW_STATUS_KEY, next_val)
    icon = _gw_status_icon(next_val)
    await query.answer(f"{icon} Gateway Status → {next_val.title()}", show_alert=False)
    await _refresh(query, context)


# ── Edit conversation ──────────────────────────────────────────────────────

# Per-field prompts and validation hints shown to the admin
_EDIT_META: dict[str, dict] = {
    "min_amount": {
        "label": "Minimum Deposit Amount",
        "prompt": "Send the new minimum deposit amount in <b>USD</b>.\nExample: <code>1.00</code>  <code>5</code>  <code>0.50</code>",
        "type": "float_positive",
        "unit": "USD",
    },
    "max_amount": {
        "label": "Maximum Deposit Amount",
        "prompt": "Send the new maximum deposit amount in <b>USD</b>.\nSend <code>0</code> for no limit.\nExample: <code>500</code>  <code>1000</code>",
        "type": "float_nonneg",
        "unit": "USD",
    },
    "rate": {
        "label": "Manual Exchange Rate",
        "prompt": (
            "Send the exchange rate — how many units of your local currency equal <b>1 USD</b>.\n"
            "Send <code>0</code> to clear (no manual rate).\n"
            "Example: <code>110.50</code> (BDT/USD)  <code>83.20</code> (INR/USD)"
        ),
        "type": "float_nonneg",
        "unit": "/USD",
    },
    "currency": {
        "label": "Rate Currency Code",
        "prompt": (
            "Send the 3-letter currency code for your local currency.\n"
            "Example: <code>BDT</code>  <code>INR</code>  <code>PKR</code>  <code>NGN</code>"
        ),
        "type": "str_upper",
        "unit": "",
    },
    "expiry": {
        "label": "Deposit Expiry",
        "prompt": "Send the number of <b>minutes</b> a payment link stays valid before expiring.\nExample: <code>30</code>  <code>60</code>  <code>120</code>",
        "type": "int_positive",
        "unit": "min",
    },
    "timeout": {
        "label": "Pending Timeout",
        "prompt": (
            "Send the number of <b>minutes</b> before a pending deposit is considered timed out.\n"
            "Send <code>0</code> for no timeout.\n"
            "Example: <code>60</code>  <code>120</code>"
        ),
        "type": "int_nonneg",
        "unit": "min",
    },
    "max_pending": {
        "label": "Max Pending Deposits",
        "prompt": (
            "Send the maximum number of pending deposits allowed per user at once.\n"
            "Send <code>0</code> for unlimited.\n"
            "Example: <code>3</code>  <code>5</code>"
        ),
        "type": "int_nonneg",
        "unit": "per user",
    },
    "instructions": {
        "label": "Payment Instructions",
        "prompt": (
            "Send the global payment instructions shown to users during deposit.\n"
            "You can use HTML: <code>&lt;b&gt;bold&lt;/b&gt;</code>, <code>&lt;i&gt;italic&lt;/i&gt;</code>.\n"
            "Send <code>-</code> to clear."
        ),
        "type": "text",
        "unit": "",
    },
}


async def ps_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: ask admin for the new value.  Callback: ``ps:edit:{shorthand}``"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    shorthand = query.data.split(":", 2)[2]
    meta = _EDIT_META.get(shorthand)
    if not meta:
        await query.answer("⚠️ Unknown setting.", show_alert=True)
        return ConversationHandler.END

    config_key = _KEY_MAP.get(shorthand)
    if not config_key:
        return ConversationHandler.END

    # Current value for display
    default = _DEFAULTS.get(config_key, "")
    if isinstance(default, float):
        current = str(cfg.get_float(config_key, default))
    elif isinstance(default, int):
        current = str(cfg.get_int(config_key, default))
    else:
        current = cfg.get_str(config_key, str(default)) or "(not set)"

    context.user_data["ps_edit"] = {"shorthand": shorthand, "config_key": config_key, "meta": meta}

    try:
        await query.edit_message_text(
            f"✏️ <b>Edit {meta['label']}</b>\n\n"
            f"Current: <code>{current}</code>{' ' + meta['unit'] if meta['unit'] else ''}\n\n"
            f"{meta['prompt']}\n\n"
            "Send /cancel to go back.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="ps:view")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return PS_EDIT_VAL


async def ps_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and validate the new value, save it, return to dashboard."""
    edit = context.user_data.pop("ps_edit", None)
    if not edit:
        return ConversationHandler.END

    raw        = (update.message.text or "").strip()
    shorthand  = edit["shorthand"]
    config_key = edit["config_key"]
    meta       = edit["meta"]
    vtype      = meta["type"]

    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Payment Settings", callback_data="ps:view")
    ]])

    # ── Validation ────────────────────────────────────────────────────────
    value: object
    if vtype == "float_positive":
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Send a positive number (e.g. <code>1.00</code>).",
                parse_mode="HTML",
            )
            context.user_data["ps_edit"] = edit
            return PS_EDIT_VAL

    elif vtype == "float_nonneg":
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Send a non-negative number (e.g. <code>0</code> or <code>100</code>).",
                parse_mode="HTML",
            )
            context.user_data["ps_edit"] = edit
            return PS_EDIT_VAL
        value = round(value, 6)

    elif vtype == "int_positive":
        try:
            value = int(raw)
            if value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Send a positive integer (e.g. <code>30</code>).",
                parse_mode="HTML",
            )
            context.user_data["ps_edit"] = edit
            return PS_EDIT_VAL

    elif vtype == "int_nonneg":
        try:
            value = int(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Send 0 or a positive integer.",
                parse_mode="HTML",
            )
            context.user_data["ps_edit"] = edit
            return PS_EDIT_VAL

    elif vtype == "str_upper":
        value = raw.upper()[:10]
        if not value.isalpha():
            await update.message.reply_text(
                "❌ Currency code must be letters only (e.g. <code>BDT</code>).",
                parse_mode="HTML",
            )
            context.user_data["ps_edit"] = edit
            return PS_EDIT_VAL

    elif vtype == "text":
        value = "" if raw == "-" else raw

    else:
        value = raw

    # ── Save ──────────────────────────────────────────────────────────────
    cfg.set(config_key, value)

    unit_str = f" {meta['unit']}" if meta.get("unit") else ""
    display  = str(value) if value != "" else "(cleared)"
    await update.message.reply_text(
        f"✅ <b>{meta['label']}</b> updated to <code>{display}</code>{unit_str}",
        reply_markup=back_btn,
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def ps_edit_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel via inline button (ps:view callback during conversation)."""
    context.user_data.pop("ps_edit", None)
    await ps_view(update, context)
    return ConversationHandler.END


async def ps_edit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel via /cancel command during conversation."""
    context.user_data.pop("ps_edit", None)
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Payment Settings", callback_data="ps:view")
        ]]),
    )
    return ConversationHandler.END


# ── ConversationHandler factory ────────────────────────────────────────────

def build_ps_edit_conv() -> ConversationHandler:
    """Return the ConversationHandler for editing any single payment setting."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ps_edit_start, pattern=r"^ps:edit:[a-z_]+$"),
        ],
        states={
            PS_EDIT_VAL: [
                CommandHandler("cancel", ps_edit_cancel_cmd),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ps_edit_value),
                CallbackQueryHandler(ps_edit_cancel_cb, pattern=r"^ps:view$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", ps_edit_cancel_cmd),
            CallbackQueryHandler(ps_edit_cancel_cb, pattern=r"^ps:view$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


# ── Registration helper ────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all Payment Settings handlers with the Application."""
    # Conversation first (higher priority than plain callbacks)
    app.add_handler(build_ps_edit_conv())

    # Dashboard view
    app.add_handler(CallbackQueryHandler(ps_view,      pattern=r"^ps:view$"))
    # Boolean toggles
    app.add_handler(CallbackQueryHandler(ps_toggle,    pattern=r"^ps:tog:[a-z_]+$"))
    # Tri-state cycle
    app.add_handler(CallbackQueryHandler(ps_tristate,  pattern=r"^ps:tri:gw_status$"))
