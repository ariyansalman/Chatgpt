"""Admin handlers for managing manual payment methods (add / edit / toggle / delete)."""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, ManualPaymentMethod
from utils import (
    create_admin_payment_methods_menu_keyboard,
    create_admin_payment_method_detail_keyboard,
    create_admin_settings_menu_keyboard,
    create_admin_gateways_menu_keyboard,
    create_admin_gateway_detail_keyboard,
)
from utils.safe_conversation import safe_conversation
from utils.bot_config import cfg
from utils.permissions import has_permission
from services import gateway_manual_mode as gw_mode
from telegram.error import BadRequest

# Conversation states
(
    PM_ADD_NAME,
    PM_ADD_EMOJI,
    PM_ADD_INSTRUCTIONS,
    PM_ADD_MIN,
    PM_EDIT_VALUE,
    GW_EDIT_VALUE,
) = range(6)


# ==================== LIST / VIEW ====================

async def admin_payment_methods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all manual payment methods."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        methods = session.query(ManualPaymentMethod).order_by(
            ManualPaymentMethod.sort_order, ManualPaymentMethod.id
        ).all()
        # Detach: we only need id/name/emoji/is_active for the keyboard
        class _M:
            def __init__(self, m):
                self.id = m.id
                self.name = m.name
                self.emoji = m.emoji
                self.is_active = m.is_active
        methods_data = [_M(m) for m in methods]

    if not methods_data:
        text = (
            "💳 <b>Payment Methods</b>\n\n"
            "No manual payment methods configured yet.\n"
            "Tap ➕ to add your first method (USDT TRC20, bKash, Binance Pay, etc.)."
        )
    else:
        text = (
            "💳 <b>Payment Methods</b>\n\n"
            "These are shown to users during top-up. ✅ = active, 🚫 = disabled."
        )

    try:
        await query.edit_message_text(
            text,
            reply_markup=create_admin_payment_methods_menu_keyboard(methods_data),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_pm_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a single payment method with edit/toggle/delete controls."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    _override = context.user_data.pop("_cb_data_override", None)
    method_id = int(_override) if _override else int(query.data.split("_")[-1])
    with get_db_session() as session:
        m = session.query(ManualPaymentMethod).filter_by(id=method_id).first()
        if not m:
            try:
                await query.edit_message_text("❌ Payment method not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        max_line = (f"Max amount: ${m.max_amount:.2f}\n"
                    if m.max_amount and m.max_amount > 0 else "Max amount: (no limit)\n")
        label_line = (f"🏷 Label: {m.account_label}\n" if m.account_label else "")
        acct_line = (f"💳 Account: <code>{m.account_number}</code>\n"
                     if m.account_number else "")
        txid_flag = "ON" if m.require_txid else "OFF"
        proof_flag = "ON" if m.require_proof else "OFF"
        text = (
            f"{m.emoji or '💳'} <b>{m.name}</b>\n"
            f"Status: {'✅ Active' if m.is_active else '🚫 Disabled'}\n"
            f"Order: {m.sort_order or 0}\n"
            f"Min amount: ${(m.min_amount or 0):.2f}\n"
            f"{max_line}"
            f"TXID required: {txid_flag}  |  Proof required: {proof_flag}\n\n"
            f"{label_line}{acct_line}"
            f"<b>Instructions shown to user:</b>\n{m.instructions}"
        )
        keyboard = create_admin_payment_method_detail_keyboard(m)

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_pm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle active/disabled state."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    method_id = int(query.data.split("_")[-1])
    with get_db_session() as session:
        m = session.query(ManualPaymentMethod).filter_by(id=method_id).first()
        if not m:
            return
        m.is_active = not m.is_active
        session.commit()

    # Re-render detail view
    context.user_data["_cb_data_override"] = str(method_id)
    await admin_pm_view(update, context)


async def admin_pm_toggle_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle require_txid / require_proof on a payment method."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    # callback_data: admin_pm_tgl_<flag>_<id>
    parts = query.data.split("_")
    flag = parts[3]          # 'txid' or 'proof'
    method_id = int(parts[4])
    field = "require_txid" if flag == "txid" else "require_proof"

    with get_db_session() as session:
        m = session.query(ManualPaymentMethod).filter_by(id=method_id).first()
        if not m:
            return
        setattr(m, field, not getattr(m, field, True))
        session.commit()

    context.user_data["_cb_data_override"] = str(method_id)
    await admin_pm_view(update, context)


async def admin_pm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a payment method."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    method_id = int(query.data.split("_")[-1])
    with get_db_session() as session:
        m = session.query(ManualPaymentMethod).filter_by(id=method_id).first()
        if m:
            session.delete(m)
            session.commit()

    await query.answer("🗑 Deleted.", show_alert=False)
    await admin_payment_methods_menu(update, context)


async def admin_pm_delete_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before wiping every manual payment method."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    with get_db_session() as session:
        count = session.query(ManualPaymentMethod).count()

    if count == 0:
        await query.answer("No manual payment methods to delete.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 Yes, delete all {count}", callback_data="admin_pm_delete_all_go")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_payment_methods")],
    ])
    try:
        await query.edit_message_text(
            f"⚠️ This will permanently delete all {count} manual payment method(s) "
            f"(bKash, USDT, Binance Pay, etc. that you added here).\n\n"
            f"Customers won't see any manual payment option until you add new ones.\n\n"
            f"Are you sure?",
            reply_markup=keyboard,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_pm_delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete every manual payment method after confirmation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    with get_db_session() as session:
        count = session.query(ManualPaymentMethod).delete()
        session.commit()

    await query.answer(f"🗑 Deleted {count} method(s).", show_alert=True)
    await admin_payment_methods_menu(update, context)


# ==================== ADD (conversation) ====================

async def admin_pm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    context.user_data['pm_new'] = {}
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_payment_methods")]]
    try:
        await query.edit_message_text(
            "➕ <b>New Payment Method</b>\n\n"
            "Please enter a <b>name</b> (e.g. 'USDT TRC20', 'Binance Pay', 'bKash'):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return PM_ADD_NAME


async def admin_pm_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['pm_new']['name'] = update.message.text.strip()[:120]
    keyboard = [[InlineKeyboardButton("Skip", callback_data="pm_add_emoji_skip")]]
    await update.message.reply_text(
        "🎨 Send an <b>emoji</b> for this method (or tap Skip to use 💳):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
    )
    return PM_ADD_EMOJI


async def admin_pm_add_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data['pm_new']['emoji'] = "💳"
        target = update.callback_query.message
        sender = target.reply_text
    else:
        context.user_data['pm_new']['emoji'] = update.message.text.strip()[:12] or "💳"
        sender = update.message.reply_text

    await sender(
        "📝 Now send the <b>payment instructions</b> shown to users.\n\n"
        "Include address / account / phone number and any note (e.g. 'Send USDT TRC20 to TXxxx… and reply with TXID').",
        parse_mode='HTML',
    )
    return PM_ADD_INSTRUCTIONS


async def admin_pm_add_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['pm_new']['instructions'] = update.message.text.strip()
    await update.message.reply_text(
        "💵 Minimum accepted amount in USD (send a number, e.g. 5). Send 0 to allow any amount:"
    )
    return PM_ADD_MIN


@safe_conversation(cleanup_keys=('pm_new',))
async def admin_pm_add_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_amount = float(update.message.text.strip())
        if min_amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return PM_ADD_MIN

    data = context.user_data.pop('pm_new', {}) or {}
    name = (data.get('name') or 'Payment')[:120]
    emoji = (data.get('emoji') or '💳')[:12]
    instructions = data.get('instructions') or ''

    with get_db_session() as session:
        m = ManualPaymentMethod(
            name=name,
            emoji=emoji,
            instructions=instructions,
            min_amount=min_amount,
            is_active=True,
            require_txid=cfg.get_bool("manual_require_txid_default", True),
            require_proof=cfg.get_bool("manual_require_proof_default", True),
        )
        session.add(m)
        session.commit()
        method_name = m.name

    await update.message.reply_text(
        f"✅ Payment method '{method_name}' added.",
        reply_markup=create_admin_settings_menu_keyboard(),
    )
    return ConversationHandler.END


# ==================== EDIT (conversation) ====================

_FIELD_LABELS = {
    "name": ("name", "New name?"),
    "emoji": ("emoji", "New emoji?"),
    "instr": ("instructions", "New instructions?"),
    "min": ("min_amount", "New minimum amount (USD)?"),
    "max": ("max_amount", "New maximum amount (USD)? Send 0 for no limit."),
    "label": ("account_label", "New account label? (e.g. 'bKash Personal') Send '-' to clear."),
    "acct": ("account_number", "New account number / address / phone? Send '-' to clear."),
    "order": ("sort_order", "New display order (integer, lower = higher up)?"),
}


async def admin_pm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    # callback_data like admin_pm_edit_<field>_<id>
    parts = query.data.split("_")
    field_key = parts[3]
    method_id = int(parts[4])
    if field_key not in _FIELD_LABELS:
        return ConversationHandler.END

    field, prompt = _FIELD_LABELS[field_key]
    context.user_data['pm_edit'] = {'id': method_id, 'field': field}
    try:
        await query.edit_message_text(f"✏️ {prompt}")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return PM_EDIT_VALUE


async def admin_pm_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit = context.user_data.pop('pm_edit', None)
    if not edit:
        return ConversationHandler.END

    value = update.message.text.strip()
    field = edit['field']

    with get_db_session() as session:
        m = session.query(ManualPaymentMethod).filter_by(id=edit['id']).first()
        if not m:
            await update.message.reply_text("❌ Payment method not found.")
            return ConversationHandler.END

        if field == 'min_amount':
            try:
                m.min_amount = max(0.0, float(value))
            except ValueError:
                await update.message.reply_text("❌ Invalid number.")
                return ConversationHandler.END
        elif field == 'max_amount':
            try:
                v = float(value)
                if v < 0:
                    raise ValueError
                m.max_amount = v if v > 0 else None
            except ValueError:
                await update.message.reply_text("❌ Invalid number.")
                return ConversationHandler.END
        elif field == 'sort_order':
            try:
                m.sort_order = int(value)
            except ValueError:
                await update.message.reply_text("❌ Invalid integer.")
                return ConversationHandler.END
        elif field == 'account_label':
            m.account_label = None if value in ('-', '') else value[:120]
        elif field == 'account_number':
            m.account_number = None if value in ('-', '') else value[:255]
        elif field == 'name':
            m.name = value[:120]
        elif field == 'emoji':
            m.emoji = value[:12]
        elif field == 'instructions':
            m.instructions = value

        session.commit()
        method_id = m.id

    await update.message.reply_text(
        "✅ Updated.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to method", callback_data=f"admin_pm_view_{method_id}")
        ]]),
    )
    return ConversationHandler.END


async def admin_pm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pm_new', None)
    context.user_data.pop('pm_edit', None)
    if update.callback_query:
        await update.callback_query.answer()
        await admin_payment_methods_menu(update, context)
    return ConversationHandler.END


# ==================== PAYMENT GATEWAYS (bKash / Nagad) ====================
#
# Unlike ManualPaymentMethod (a DB table with unlimited rows), bKash and
# Nagad are exactly two fixed, code-integrated gateways (see
# services/bkash_payment.py / services/nagad_payment.py). Their enabled flag
# and credentials live in the generic `bot_config` key/value table (see
# utils/bot_config.py DEFAULTS, category="gateways") so no schema change is
# needed and the values are also visible/editable from the generic
# "🛠 Bot Configuration" screen if preferred.

# field_key -> (bot_config key template "{field}_{gw}", prompt, is_secret, storage)
# storage: "cfg" (default, generic bot_config key/value) or "pgc_number" /
# "pgc_instructions" (PaymentGatewayConfig.manual_* columns, via
# services/gateway_manual_mode.py) — the manual-mode merchant number /
# instructions live there, not in bot_config, alongside the new Auto/Manual
# `mode` column on PaymentGatewayConfig (database/models.py). NOTE: that new
# column is DIFFERENT from the "mode" row below, which is bKash/Nagad's own
# sandbox/live API mode and is unrelated to the Auto/Manual toggle.
_GATEWAY_FIELDS = {
    "mode":            ("mode", "New mode? Send 'sandbox' or 'live'.", False, "cfg"),
    "appkey":          ("app_key", "New bKash App Key?", False, "cfg"),
    "appsecret":       ("app_secret", "New bKash App Secret?", True, "cfg"),
    "username":        ("username", "New bKash API Username?", False, "cfg"),
    "password":        ("password", "New bKash API Password?", True, "cfg"),
    "merchantid":      ("merchant_id", "New Nagad Merchant ID?", False, "cfg"),
    "merchantnumber":  ("merchant_number", "New Nagad Merchant Number?", False, "cfg"),
    "pubkey":          ("public_key", "Send Nagad's Gateway Public Key (PEM, or just the base64 body).", True, "cfg"),
    "privkey":         ("private_key", "Send your Nagad Merchant Private Key (PEM, or just the base64 body).", True, "cfg"),
    "min":             ("min_amount", "New minimum top-up amount (USD)? Send a number.", False, "cfg"),
    "max":             ("max_amount", "New maximum top-up amount (USD)? Send 0 for no limit.", False, "cfg"),
    # Manual-mode-only fields (shown/edited when mode == "manual")
    "manualnumber":    (None, "New merchant bKash/Nagad number to show users (e.g. '01712345678')? Send '-' to clear.", False, "pgc_number"),
    "manualinstr":     (None, "New payment instructions shown to users in Manual mode? Send '-' to clear.", False, "pgc_instructions"),
}


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


async def admin_gateways_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List bKash / Nagad with their current enabled/disabled status."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.telegram_stars import telegram_stars_service
    from services.cryptomus_payment import CryptomusPaymentService
    from services.heleket_payment import HeleketPaymentService
    from services.nowpayments_payment import NowPaymentsService
    from services.zinipay_payment import ZiniPayService
    from services.binance_pay import BinancePayService
    from services.bybit_pay import BybitPayService
    status = {
        "bkash": cfg.get_bool("bkash_enabled", False),
        "nagad": cfg.get_bool("nagad_enabled", False),
        "stars": telegram_stars_service.is_enabled(),
        "cryptomus": CryptomusPaymentService().enabled,
        "heleket": HeleketPaymentService().enabled,
        "nowpayments": NowPaymentsService().enabled,
        "zinipay": ZiniPayService().enabled,
        "binance_pay": BinancePayService().enabled,
        "bybit_pay": BybitPayService().enabled,
    }
    text = (
        "🏦 <b>Payment Gateways — Auto (API)</b>\n\n"
        "One place for every automated gateway: bKash, Nagad, Telegram Stars, "
        "Cryptomus, Heleket, NOWPayments, ZiniPay, Binance Pay, Bybit Pay. ✅ = enabled and shown to "
        "users, 🚫 = disabled. Tap a gateway to configure it.\n\n"
        "💳 Manual payment methods (bank transfer, personal bKash/Nagad "
        "number, etc.) are managed separately — see <b>💳 Payment Methods</b> "
        "in the Admin Control Center.\n\n"
        "⚠️ Each gateway only accepts real payments once its credentials "
        "are filled in — enable it after configuring."
    )
    try:
        await query.edit_message_text(
            text, reply_markup=create_admin_gateways_menu_keyboard(status), parse_mode='HTML'
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_gw_disable_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before turning off every automated gateway."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, turn off all", callback_data="admin_gw_disable_all_go")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_gateways")],
    ])
    try:
        await query.edit_message_text(
            "⚠️ This will disable ALL automated payment gateways — bKash, Nagad, "
            "Telegram Stars, Cryptomus, Heleket, NOWPayments, ZiniPay, and Binance Pay.\n\n"
            "Their saved credentials/settings are kept (nothing configured is lost), "
            "they just stop being shown/usable to customers until you turn them back "
            "on and reconfigure them.\n\n"
            "Are you sure?",
            reply_markup=keyboard,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_gw_disable_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable every automated gateway after confirmation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    cfg.set("bkash_enabled", False)
    cfg.set("nagad_enabled", False)

    disabled_count = 2
    with get_db_session() as session:
        from database.models import PaymentGatewayConfig
        rows = session.query(PaymentGatewayConfig).filter_by(is_enabled=True).all()
        for row in rows:
            row.is_enabled = False
            disabled_count += 1
        session.commit()

    await query.answer("🚫 All gateways disabled.", show_alert=True)
    await admin_gateways_menu(update, context)


def _gateway_summary(gateway_key: str) -> str:
    enabled = cfg.get_bool(f"{gateway_key}_enabled", False)
    api_mode = cfg.get_str(f"{gateway_key}_mode", "sandbox")  # bKash/Nagad's own sandbox/live API mode
    auto_manual_mode = gw_mode.get_mode(gateway_key)          # NEW: "auto" | "manual"
    min_amt = cfg.get_float(f"{gateway_key}_min_amount", 0.0)
    max_amt = cfg.get_float(f"{gateway_key}_max_amount", 0.0)
    max_line = f"${max_amt:.2f}" if max_amt else "(no limit)"

    label = "📱 <b>bKash</b>" if gateway_key == "bkash" else "🟠 <b>Nagad</b>"
    lines = [
        f"{label} — Status: {'✅ Enabled' if enabled else '🚫 Disabled'}",
        f"Payment Mode: {'🤖 Auto (API)' if auto_manual_mode == 'auto' else '✍️ Manual (admin-reviewed)'}",
    ]

    if auto_manual_mode == "manual":
        # Manual mode: hide API credentials entirely, show merchant details instead.
        details = gw_mode.get_manual_details(gateway_key)
        lines.append(f"📞 Merchant Number: <code>{details['merchant_number'] or '(not set)'}</code>")
        lines.append(f"📝 Instructions: {details['instructions'] or '(not set)'}")
    else:
        lines.append(f"API Mode: <code>{api_mode}</code>")
        if gateway_key == "bkash":
            lines += [
                f"App Key: <code>{_mask(cfg.get_str('bkash_app_key', ''))}</code>",
                f"App Secret: <code>{_mask(cfg.get_str('bkash_app_secret', ''))}</code>",
                f"Username: <code>{_mask(cfg.get_str('bkash_username', ''))}</code>",
                f"Password: <code>{_mask(cfg.get_str('bkash_password', ''))}</code>",
            ]
        else:
            lines += [
                f"Merchant ID: <code>{_mask(cfg.get_str('nagad_merchant_id', ''))}</code>",
                f"Merchant Number: <code>{_mask(cfg.get_str('nagad_merchant_number', ''))}</code>",
                f"Public Key: <code>{_mask(cfg.get_str('nagad_public_key', ''))}</code>",
                f"Private Key: <code>{_mask(cfg.get_str('nagad_private_key', ''))}</code>",
            ]

    lines.append(f"Min amount: ${min_amt:.2f}  |  Max amount: {max_line}")
    return "\n".join(lines)


async def admin_gw_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a single gateway's config with edit/toggle controls."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    gateway_key = query.data.split("_")[-1]  # admin_gw_view_<bkash|nagad>
    if gateway_key not in ("bkash", "nagad"):
        return

    enabled = cfg.get_bool(f"{gateway_key}_enabled", False)
    auto_manual_mode = gw_mode.get_mode(gateway_key)
    text = _gateway_summary(gateway_key)
    try:
        await query.edit_message_text(
            text,
            reply_markup=create_admin_gateway_detail_keyboard(gateway_key, enabled, auto_manual_mode),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_gw_toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip a gateway between Auto (API) and Manual (admin-reviewed) mode."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    gateway_key = query.data.split("_")[-1]  # admin_gw_mode_toggle_<bkash|nagad>
    if gateway_key not in ("bkash", "nagad"):
        return

    new_mode = gw_mode.toggle_mode(gateway_key)
    await query.answer(
        "✍️ Switched to Manual mode." if new_mode == "manual" else "🤖 Switched to Auto mode.",
        show_alert=False,
    )
    await admin_gw_view(update, context)


async def admin_gw_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable/disable a gateway. Refuses to enable if credentials are missing."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    gateway_key = query.data.split("_")[-1]  # admin_gw_toggle_<bkash|nagad>
    if gateway_key not in ("bkash", "nagad"):
        return

    currently_enabled = cfg.get_bool(f"{gateway_key}_enabled", False)

    if not currently_enabled:
        # About to enable — sanity-check the right prerequisites are present
        # depending on Auto vs Manual mode.
        if gw_mode.get_mode(gateway_key) == "manual":
            details = gw_mode.get_manual_details(gateway_key)
            if not details["merchant_number"]:
                await query.answer(
                    "⚠️ Set a merchant number for Manual mode before enabling it.",
                    show_alert=True,
                )
                await admin_gw_view(update, context)
                return
        else:
            if gateway_key == "bkash":
                required = ["bkash_app_key", "bkash_app_secret", "bkash_username", "bkash_password"]
            else:
                required = ["nagad_merchant_id", "nagad_merchant_number", "nagad_public_key", "nagad_private_key"]
            missing = [k for k in required if not cfg.get_str(k, "")]
            if missing:
                await query.answer(
                    "⚠️ Set all API credentials for this gateway before enabling it.",
                    show_alert=True,
                )
                await admin_gw_view(update, context)
                return

    cfg.set(f"{gateway_key}_enabled", not currently_enabled)
    await admin_gw_view(update, context)


async def admin_gw_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for editing one credential/setting field of a gateway."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    # callback_data: admin_gw_edit_<field>_<bkash|nagad>
    parts = query.data.split("_")
    field_key = parts[3]
    gateway_key = parts[4]
    if field_key not in _GATEWAY_FIELDS or gateway_key not in ("bkash", "nagad"):
        return ConversationHandler.END

    suffix, prompt, is_secret, storage = _GATEWAY_FIELDS[field_key]
    context.user_data['gw_edit'] = {
        'gateway': gateway_key,
        'storage': storage,
        'config_key': f"{gateway_key}_{suffix}" if storage == "cfg" else None,
    }
    note = "\n\n🔒 This value is sensitive — it won't be echoed back after saving." if is_secret else ""
    try:
        await query.edit_message_text(f"✏️ {prompt}{note}")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GW_EDIT_VALUE


async def admin_gw_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit = context.user_data.pop('gw_edit', None)
    if not edit:
        return ConversationHandler.END

    value = update.message.text.strip()
    gateway_key = edit['gateway']
    storage = edit.get('storage', 'cfg')

    # Manual-mode-only fields — stored on PaymentGatewayConfig, not bot_config.
    if storage == "pgc_number":
        gw_mode.set_manual_merchant_number(gateway_key, None if value == "-" else value)
        await update.message.reply_text(
            "✅ Updated.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to gateway", callback_data=f"admin_gw_view_{gateway_key}")
            ]]),
        )
        return ConversationHandler.END
    if storage == "pgc_instructions":
        gw_mode.set_manual_instructions(gateway_key, None if value == "-" else value)
        await update.message.reply_text(
            "✅ Updated.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to gateway", callback_data=f"admin_gw_view_{gateway_key}")
            ]]),
        )
        return ConversationHandler.END

    config_key = edit['config_key']

    if config_key.endswith("_mode"):
        value = value.lower()
        if value not in ("sandbox", "live"):
            await update.message.reply_text("❌ Mode must be 'sandbox' or 'live'.")
            return ConversationHandler.END
    elif config_key.endswith("_min_amount") or config_key.endswith("_max_amount"):
        try:
            num = float(value)
            if num < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Please send a valid non-negative number.")
            return ConversationHandler.END
        value = num

    cfg.set(config_key, value)

    await update.message.reply_text(
        "✅ Updated.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to gateway", callback_data=f"admin_gw_view_{gateway_key}")
        ]]),
    )
    return ConversationHandler.END


async def admin_gw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('gw_edit', None)
    if update.callback_query:
        await update.callback_query.answer()
        await admin_gateways_menu(update, context)
    return ConversationHandler.END
