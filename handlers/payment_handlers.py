"""Payment and wallet management handlers."""

import os
import logging
import tempfile
import asyncio
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputFile
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy.exc import IntegrityError
from database import (
    get_db_session, User, Transaction, Order, OrderItem, Product,
    ProductKey, TransactionStatus, OrderStatus, PaymentMethod, ProductType,
    ManualPaymentMethod, BinancePayTransaction, BybitPayTransaction,
    ZiniPayUsedTransaction, PendingManualVerification, VerificationAttemptLog,
    AdminAuditLog,
)
from database.models import (
    OrderLifecycleStatus, Coupon, DiscountType, CouponRedemption,
    StockReservation,
)
from utils import (
    format_price, validate_amount, create_cancel_keyboard,
    create_payment_method_keyboard, create_quantity_keyboard,
    create_main_menu_keyboard, calculate_expiry_time,
    notify_admin, check_user_banned, is_admin, sanitize_message,
)
from config.settings import settings as app_settings
from services.crypto_bot import CryptoBotService
from services.bkash_payment import BkashPaymentService
from services.nagad_payment import NagadPaymentService
from services.cryptomus_payment import CryptomusPaymentService
from services.heleket_payment import HeleketPaymentService, SUPPORTED_ASSETS
from services.nowpayments_payment import NowPaymentsService
from services.zinipay_payment import ZiniPayService
from services.binance_pay import BinancePayService, VerificationOutcome, is_rate_limited, get_order_lock, is_valid_txid_format
from services import ltc_rate as _ltc_rate_svc
from services.bybit_pay import (
    BybitPayService, PaymentType as BybitPaymentType, VerificationOutcome as BybitVerificationOutcome,
    is_rate_limited as bybit_is_rate_limited, get_order_lock as bybit_get_order_lock,
    is_valid_uid_txid_format, is_valid_onchain_txid_format,
)
from services.telegram_stars import telegram_stars_service
from services import gateway_manual_mode as gw_mode
from services.pricing import convert_currency
from services import payment_ui as pui
from utils.bot_config import cfg
from utils.perf import perf_track
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def _gateway_admin_recipient_ids() -> list:
    """Telegram IDs that should receive manual-review notifications for any
    manually-verified crypto gateway (Binance Pay, Bybit Pay, ...): the
    store owner (always implicit super_admin) plus every active admin with
    the ``manage_payments`` permission. Deduplicated, owner-first."""
    ids = []
    owner_id = getattr(app_settings, "ADMIN_TELEGRAM_ID", None)
    if owner_id:
        ids.append(int(owner_id))
    try:
        from database import AdminRole
        with get_db_session() as session:
            rows = session.query(AdminRole).filter_by(is_active=True, manage_payments=True).all()
            for r in rows:
                if r.telegram_id and r.telegram_id not in ids:
                    ids.append(int(r.telegram_id))
    except Exception:
        logger.exception("Failed to load admin list for payment-gateway notification — falling back to owner only")
    return ids


def _auto_cancel_expired_pending(session, user_id: int, payment_method=None) -> int:
    """Reconcile stale PENDING transactions to CANCELLED *inline*, at the
    moment they'd otherwise block a new order — rather than waiting on the
    ``check_expired_payments`` background job's next tick.

    An order whose ``expires_at`` has already passed must never block the
    user from creating a new payment order, even if the periodic job hasn't
    run yet. Call this immediately before any "does the user already have a
    pending order?" check.

    Returns the number of rows flipped.
    """
    query = session.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.status == TransactionStatus.PENDING,
        Transaction.expires_at.isnot(None),
        Transaction.expires_at < datetime.utcnow(),
    )
    if payment_method is not None:
        query = query.filter(Transaction.payment_method == payment_method)

    flipped = query.update(
        {Transaction.status: TransactionStatus.CANCELLED},
        synchronize_session=False,
    )
    if flipped:
        session.commit()
    return flipped


def _cancel_user_pending_transactions(session, user_id: int, payment_method=None) -> int:
    """Explicitly cancel a user's still-PENDING transaction(s) — used when the
    user taps "Cancel" on a payment/order page, or an admin cancels an order.
    Never blocks: a cancelled order frees the user to start a new one right away.

    Returns the number of rows flipped.
    """
    query = session.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.status == TransactionStatus.PENDING,
    )
    if payment_method is not None:
        query = query.filter(Transaction.payment_method == payment_method)

    flipped = query.update(
        {Transaction.status: TransactionStatus.CANCELLED},
        synchronize_session=False,
    )
    if flipped:
        session.commit()
    return flipped


# Conversation states for top-up
AMOUNT, METHOD, MANUAL_PROOF, MANUAL_TXID = range(4)

# Separate conversation state for the Binance Pay "Submit Transaction ID" flow
# (kept out of the main topup_conv_handler states since it's entered from its
# own button, potentially long after the top-up conversation already ended —
# see bot.py's binance_submit_conv).
BINANCE_TXID = 100

# Separate conversation state for the Bybit Pay "Submit Transaction ID" flow —
# same rationale as BINANCE_TXID above (see bot.py's bybit_submit_conv).
BYBIT_TXID = 101

# Separate conversation state for the ZiniPay "Submit Transaction ID" flow —
# same rationale as BINANCE_TXID/BYBIT_TXID above (see bot.py's zinipay_submit_conv).
ZINIPAY_TXID = 103

# Conversation states for direct purchase
PURCHASE_QUANTITY = 10

# Legacy fallback; the live value is read from bot_config at call time.
BULK_DELIVERY_THRESHOLD = 10



def _build_topup_method_screen(amount: float = None):
    """Build the "choose a payment method" screen content (text + keyboard).

    Shared by ``topup_start`` (the normal entry point) and the Cancel
    handlers (``cancel_topup`` / ``cancel_payment_page``), which now behave
    like a Back tap straight to this screen instead of showing a dead-end
    "Payment Cancelled" card.

    Pass ``amount`` to include the confirmed amount in the header text.

    Returns ``(text, keyboard, is_empty)`` — ``is_empty`` is True when no
    gateway or manual payment method is configured at all, in which case
    the caller should end any in-progress conversation.

    Gateway order: Payment Providers → USDT Networks → Other Crypto →
    Local Payment. The keyboard groups them visually via
    ``create_payment_method_keyboard``.
    """
    gateways = []

    # ── 1. Payment Providers ─────────────────────────────────────────────────
    bybit = BybitPayService()
    if bybit.enabled and bybit.is_configured() and bybit.uid:
        gateways.append({"key": "bybit_pay", "label": "Bybit Pay", "emoji": "⭐"})
    binance = BinancePayService()
    if binance.enabled and binance.is_configured():
        gateways.append({"key": "binance_pay", "label": "Binance Pay", "emoji": "🟡"})

    # ── 2. USDT Networks (Bybit on-chain) ────────────────────────────────────
    if bybit.enabled and bybit.is_configured():
        if bybit.wallet_for_network("TRC20"):
            gateways.append({"key": "bybit_trc20", "label": "USDT (TRC20)", "emoji": "💵"})
        if bybit.wallet_for_network("BEP20"):
            gateways.append({"key": "bybit_bep20", "label": "USDT (BEP20)", "emoji": "🟢"})
        if bybit.wallet_for_network("ERC20"):
            gateways.append({"key": "bybit_erc20", "label": "USDT (ERC20)", "emoji": "🔵"})
        if bybit.wallet_for_network("TON"):
            gateways.append({"key": "bybit_ton", "label": "USDT (TON)", "emoji": "⚫"})
        if bybit.wallet_for_network("SOL"):
            gateways.append({"key": "bybit_sol", "label": "USDT (Solana)", "emoji": "🟣"})
        if bybit.wallet_for_network("AVAXC"):
            gateways.append({"key": "bybit_avaxc", "label": "USDT (Avalanche C-Chain)", "emoji": "🔺"})
        if bybit.wallet_for_network("BASE"):
            gateways.append({"key": "bybit_base", "label": "USDT (Base)", "emoji": "🔷"})
        if bybit.wallet_for_network("ARBONE"):
            gateways.append({"key": "bybit_arb", "label": "USDT (Arbitrum)", "emoji": "🔵"})
        if bybit.wallet_for_network("OP"):
            gateways.append({"key": "bybit_op", "label": "USDT (Optimism)", "emoji": "🔴"})
        if bybit.wallet_for_network("MATIC"):
            gateways.append({"key": "bybit_matic", "label": "USDT (Polygon)", "emoji": "🟣"})

    # ── 3. Other Crypto ──────────────────────────────────────────────────────
    if bybit.enabled and bybit.is_configured():
        if bybit.wallet_for_network("LTC"):
            gateways.append({"key": "bybit_ltc", "label": "Litecoin (LTC)", "emoji": "🪙"})
    if CryptomusPaymentService().enabled:
        gateways.append({"key": "cryptomus", "label": "Cryptomus (USDT/Crypto)", "emoji": "💠"})
    heleket = HeleketPaymentService()
    if heleket.enabled and heleket.is_configured():
        gateways.append({"key": "heleket", "label": "Crypto Deposit (Address)", "emoji": "🪙"})
    nowpayments = NowPaymentsService()
    if nowpayments.enabled and nowpayments.is_configured():
        gateways.append({"key": "nowpayments", "label": "NOWPayments (Crypto)", "emoji": "🌐"})

    # ── 4. Local Payment ─────────────────────────────────────────────────────
    if cfg.get_bool("bkash_enabled", False):
        gateways.append({"key": "bkash", "label": "bKash", "emoji": "📱"})
    if cfg.get_bool("nagad_enabled", False):
        gateways.append({"key": "nagad", "label": "Nagad", "emoji": "🟠"})
    zinipay = ZiniPayService()
    if zinipay.enabled and zinipay.is_configured():
        gateways.append({"key": "zinipay", "label": "BKash • Nagad • Rocket", "emoji": "🇧🇩"})
    stars_cfg = telegram_stars_service.get_config()
    if stars_cfg["enabled"]:
        gateways.append({"key": "stars", "label": "Telegram Stars", "emoji": "⭐"})

    with get_db_session() as session:
        methods = session.query(ManualPaymentMethod).filter_by(
            is_active=True
        ).order_by(ManualPaymentMethod.sort_order, ManualPaymentMethod.id).all()
        methods_data = [(m.id, m.emoji, m.name, m.min_amount) for m in methods]

    class _M:
        __slots__ = ('id', 'emoji', 'name', 'min_amount')
        def __init__(self, i, e, n, mn):
            self.id, self.emoji, self.name, self.min_amount = i, e, n, mn

    method_objs = [_M(*d) for d in methods_data]

    if not method_objs and not gateways:
        text = (
            "❌ No payment methods are available right now.\n\n"
            "Please contact support — the admin needs to configure at least one payment method."
        )
        return text, create_cancel_keyboard(), True

    if amount is not None:
        text = f"💰 <b>Top Up Wallet</b>\n\nAmount: <code>${amount:.2f}</code>\n\nSelect your preferred payment method below."
    else:
        text = "💰 <b>Top Up Wallet</b>\n\nSelect your preferred payment method below."
    keyboard = create_payment_method_keyboard(method_objs, gateways)
    return text, keyboard, False


async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the wallet top-up flow: show all available payment methods up front,
    before asking for an amount (amount is collected after a method is chosen)."""
    query = update.callback_query
    await query.answer()

    # Fresh start — clear any leftover state from a previous attempt.
    context.user_data.pop('topup_amount', None)
    context.user_data.pop('topup_method', None)

    text, keyboard, is_empty = _build_topup_method_screen()

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END if is_empty else METHOD


async def topup_amount_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy entry point kept for old in-flight conversations/links.
    Falls back to the classic 'type an amount first' flow."""
    query = update.callback_query; await query.answer()
    context.user_data.pop('topup_method', None)
    try:
        await query.edit_message_text("💬 How much would you like to add to your wallet, in USD?\nExample: 10", reply_markup=create_cancel_keyboard())
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT


def _amount_range_hint(gmin: float, gmax: float) -> str:
    """Small helper: build a '(Accepted range: ...)' hint line for amount prompts."""
    if gmin and gmax:
        return f"\n(Accepted range: ${gmin:.2f} – ${gmax:.2f})"
    if gmin:
        return f"\n(Minimum: ${gmin:.2f})"
    if gmax:
        return f"\n(Maximum: ${gmax:.2f})"
    return ""


async def _ask_amount_for_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   gateway_key: str, label: str, emoji: str,
                                   gmin: float = 0.0, gmax: float = 0.0):
    """Shared step: user picked an automated gateway — now ask for the amount."""
    query = update.callback_query
    await query.answer()
    context.user_data['topup_method'] = ('gateway', gateway_key)
    hint = _amount_range_hint(gmin, gmax)
    try:
        await query.edit_message_text(
            f"{emoji} {label} selected.\n\n💬 How much would you like to add to your wallet, in USD?{hint}\nExample: 10",
            reply_markup=create_cancel_keyboard(),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT

# ==================== UNIVERSAL CRYPTO DEPOSIT MESSAGE TEMPLATE ====================
# ONE template for every crypto asset/network the bot can show a deposit
# address for (USDT TRC20/BEP20/ERC20, BTC, LTC, DOGE, SOL, ETH, BNB, and
# any future coin — see services/heleket_payment.py's SUPPORTED_ASSETS).
# Every field is dynamic; nothing here is asset-specific. No payment
# provider name is ever shown to the customer — see CRYPTO_NETWORK_LABELS
# and build_crypto_deposit_message() below.

CRYPTO_NETWORK_LABELS = {
    "tron": "TRON (TRC20)", "trc20": "TRON (TRC20)",
    "bsc": "BNB Smart Chain (BEP20)", "bep20": "BNB Smart Chain (BEP20)",
    "eth": "Ethereum (ERC20)", "erc20": "Ethereum (ERC20)", "ethereum": "Ethereum (ERC20)",
    "btc": "Bitcoin", "ltc": "Litecoin", "doge": "Dogecoin",
    "sol": "Solana", "solana": "Solana", "bnb": "BNB Smart Chain",
    "base": "Base (Coinbase L2)",
    "arbone": "Arbitrum One",
    "op": "Optimism",
    "matic": "Polygon (MATIC)",
    "sol": "Solana",
}


def crypto_network_label(network: str) -> str:
    """Human-friendly network name for any network code, with a sane
    fallback for networks not yet in CRYPTO_NETWORK_LABELS (future coins)."""
    return CRYPTO_NETWORK_LABELS.get((network or "").strip().lower(), (network or "").upper())


def build_crypto_deposit_message(
    *, asset: str, network_label: str, address: str,
    amount: Optional[str] = None,
    min_deposit: Optional[str] = None,
    confirmations: Optional[str] = None,
) -> str:
    """The one universal deposit screen used for every supported
    cryptocurrency and network — asset, network, address, amount, minimum
    deposit and required confirmations are all dynamic parameters. Never
    mentions any payment provider by name."""
    lines = [
        "💳 <b>Crypto Deposit</b>",
        "",
        f"Asset:\n<b>{asset}</b>",
        "",
        f"Network:\n<b>{network_label}</b>",
        "",
        f"Deposit Address:\n<code>{address}</code>",
    ]
    if amount:
        lines += ["", f"Amount:\n<code>{amount}</code>"]
    if min_deposit:
        lines += ["", f"Minimum Deposit:\n<b>{min_deposit}</b>"]
    if confirmations:
        lines += ["", f"Required Confirmations:\n<b>{confirmations}</b>"]
    lines += [
        "",
        "⚠️ <b>Important</b>",
        f"• Send only the selected asset ({asset}) using the selected blockchain network ({network_label}).",
        "• Transfers made using a different asset or network cannot be recovered.",
        "• Send the exact payment amount displayed by the bot.",
        "",
        "🔄 <b>Automatic Verification</b>",
        "Your deposit will be monitored and verified automatically after the required blockchain confirmations.",
        "✅ Once confirmed, your wallet balance will be credited instantly.",
        "⏱ No transaction ID submission or manual verification is required.",
    ]
    return "\n".join(lines)


def generate_deposit_qr_bytes(data: str):
    """Best-effort QR PNG for any deposit address. Returns None (and logs
    once) if the optional `qrcode` package isn't installed — the address is
    always shown as tap-to-copy text regardless, so this never blocks the
    deposit screen from being usable."""
    try:
        import qrcode
        import io
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        logger.info("QR code generation unavailable (qrcode package missing or failed) for crypto deposit address")
        return None


async def payment_method_heleket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    svc = HeleketPaymentService()
    if not svc.enabled or not svc.is_configured():
        try:
            await query.edit_message_text("❌ Crypto deposits are not available right now.", reply_markup=create_cancel_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END
    rows=[]
    for key, (_, _, label) in SUPPORTED_ASSETS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"heleket_asset:{key}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    try:
        await query.edit_message_text("🪙 Select coin and network:", reply_markup=InlineKeyboardMarkup(rows))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD

async def heleket_asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    key=query.data.split(":",1)[1]
    asset=SUPPORTED_ASSETS.get(key)
    if not asset:
        await query.answer("Unsupported asset",show_alert=True); return METHOD
    currency, network, label=asset
    svc=HeleketPaymentService()
    wallet=await asyncio.to_thread(svc.create_or_get_static_wallet, update.effective_user.id, currency, network)
    if not wallet:
        reason = ""
        if not svc.is_configured():
            reason = " (Merchant ID or Payment API Key is missing.)"
        elif not svc.callback_url:
            reason = " (WEBHOOK_URL is not set — ask an admin to set it under Bot Configuration → Webhook Base URL, or the WEBHOOK_URL env var.)"
        text = "❌ Could not prepare a deposit address. Please try another method or contact support."
        if reason and is_admin(update.effective_user.id):
            text += reason
        try:
            await query.edit_message_text(text, reply_markup=create_cancel_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END
    address=wallet["address"]
    network_label = crypto_network_label(network)
    # Minimum deposit / required confirmations aren't available from the
    # current wallet API response, so the universal template simply omits
    # them (both parameters are optional-by-design — see
    # build_crypto_deposit_message()) rather than showing fabricated values.
    text = build_crypto_deposit_message(
        asset=currency, network_label=network_label, address=address,
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Choose another coin", callback_data="topup")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
    ])
    qr_buf = await asyncio.to_thread(generate_deposit_qr_bytes, address)
    try:
        if qr_buf:
            # Editing a text message into a photo in-place isn't supported by
            # Telegram, so replace it with a fresh photo message instead.
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, photo=qr_buf, caption=text,
                reply_markup=keyboard, parse_mode="HTML",
            )
        else:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END


async def topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle amount input for wallet top-up.

    Normally a payment method was already chosen (context.user_data['topup_method'])
    before we get here, so this validates the amount against that method's
    limits and creates the payment. If no method was pre-selected (old
    'Top Up by Amount' legacy entry point), falls back to the classic
    amount-then-eligible-methods list.
    """
    amount_str = update.message.text

    # Validate amount
    is_valid, amount, error_msg = validate_amount(amount_str)

    if not is_valid:
        await update.message.reply_text(
            f"❌ {error_msg}\n\nPlease enter a valid amount:",
            reply_markup=create_cancel_keyboard()
        )
        return AMOUNT

    # Global min/max top-up (from admin bot_config)
    try:
        from utils.bot_config import cfg
        _min_enabled = cfg.get_bool("minimum_deposit_enabled", False)
        gmin = cfg.get_float("topup_min_amount", 1.0) if _min_enabled else 0.0
        gmax = cfg.get_float("topup_max_amount", 0.0)
    except Exception:
        _min_enabled = False
        gmin, gmax = 0.0, 0.0
    if _min_enabled and amount < gmin:
        await update.message.reply_text(
            f"❌ Minimum top-up is ${gmin:.2f}.",
            reply_markup=create_cancel_keyboard(),
        )
        return AMOUNT
    if gmax and amount > gmax:
        await update.message.reply_text(
            f"❌ Maximum single top-up is ${gmax:.2f}.",
            reply_markup=create_cancel_keyboard(),
        )
        return AMOUNT

    # Store amount in context
    context.user_data['topup_amount'] = amount

    method = context.user_data.get('topup_method')

    if not method:
        # Legacy fallback: no method chosen yet (old amount-first entry
        # point) — show the amount-eligible method list with the new
        # ordered/labelled gateway groups, exactly as _build_topup_method_screen.
        gateways = []

        # ── Payment Providers ────────────────────────────────────────────────
        bybit_svc = BybitPayService()
        if bybit_svc.enabled and bybit_svc.is_configured():
            if amount >= bybit_svc.min_amount and (not bybit_svc.max_amount or amount <= bybit_svc.max_amount):
                if bybit_svc.uid:
                    gateways.append({"key": "bybit_pay", "label": "Bybit Pay", "emoji": "⭐"})
        binance_svc = BinancePayService()
        if binance_svc.enabled and binance_svc.is_configured():
            if amount >= binance_svc.min_amount and (not binance_svc.max_amount or amount <= binance_svc.max_amount):
                gateways.append({"key": "binance_pay", "label": "Binance Pay", "emoji": "🟡"})

        # ── USDT Networks ────────────────────────────────────────────────────
        if bybit_svc.enabled and bybit_svc.is_configured():
            if amount >= bybit_svc.min_amount and (not bybit_svc.max_amount or amount <= bybit_svc.max_amount):
                if bybit_svc.wallet_for_network("TRC20"):
                    gateways.append({"key": "bybit_trc20", "label": "USDT (TRC20)", "emoji": "💵"})
                if bybit_svc.wallet_for_network("BEP20"):
                    gateways.append({"key": "bybit_bep20", "label": "USDT (BEP20)", "emoji": "🟢"})
                if bybit_svc.wallet_for_network("ERC20"):
                    gateways.append({"key": "bybit_erc20", "label": "USDT (ERC20)", "emoji": "🔵"})
                if bybit_svc.wallet_for_network("TON"):
                    gateways.append({"key": "bybit_ton", "label": "USDT (TON)", "emoji": "⚫"})
                if bybit_svc.wallet_for_network("SOL"):
                    gateways.append({"key": "bybit_sol", "label": "USDT (Solana)", "emoji": "🟣"})
                if bybit_svc.wallet_for_network("AVAXC"):
                    gateways.append({"key": "bybit_avaxc", "label": "USDT (Avalanche C-Chain)", "emoji": "🔺"})
                if bybit_svc.wallet_for_network("BASE"):
                    gateways.append({"key": "bybit_base", "label": "USDT (Base)", "emoji": "🔷"})
                if bybit_svc.wallet_for_network("ARBONE"):
                    gateways.append({"key": "bybit_arb", "label": "USDT (Arbitrum)", "emoji": "🔵"})
                if bybit_svc.wallet_for_network("OP"):
                    gateways.append({"key": "bybit_op", "label": "USDT (Optimism)", "emoji": "🔴"})
                if bybit_svc.wallet_for_network("MATIC"):
                    gateways.append({"key": "bybit_matic", "label": "USDT (Polygon)", "emoji": "🟣"})

        # ── Other Crypto ─────────────────────────────────────────────────────
        if bybit_svc.enabled and bybit_svc.is_configured():
            if amount >= bybit_svc.min_amount and (not bybit_svc.max_amount or amount <= bybit_svc.max_amount):
                if bybit_svc.wallet_for_network("LTC"):
                    gateways.append({"key": "bybit_ltc", "label": "Litecoin (LTC)", "emoji": "🪙"})
        if CryptomusPaymentService().enabled:
            gateways.append({"key": "cryptomus", "label": "Cryptomus (USDT/Crypto)", "emoji": "💠"})
        if NowPaymentsService().enabled:
            gateways.append({"key": "nowpayments", "label": "NOWPayments (Crypto)", "emoji": "🌐"})

        # ── Local Payment ────────────────────────────────────────────────────
        if cfg.get_bool("bkash_enabled", False):
            bmin = cfg.get_float("bkash_min_amount", 0.0)
            bmax = cfg.get_float("bkash_max_amount", 0.0)
            if amount >= bmin and (not bmax or amount <= bmax):
                gateways.append({"key": "bkash", "label": "bKash", "emoji": "📱"})
        if cfg.get_bool("nagad_enabled", False):
            nmin = cfg.get_float("nagad_min_amount", 0.0)
            nmax = cfg.get_float("nagad_max_amount", 0.0)
            if amount >= nmin and (not nmax or amount <= nmax):
                gateways.append({"key": "nagad", "label": "Nagad", "emoji": "🟠"})
        if ZiniPayService().enabled:
            gateways.append({"key": "zinipay", "label": "BKash • Nagad • Rocket", "emoji": "🇧🇩"})
        stars_cfg = telegram_stars_service.get_config()
        if stars_cfg["enabled"]:
            stars_needed = telegram_stars_service.stars_for_usd(amount)
            if stars_cfg["min_stars"] <= stars_needed <= stars_cfg["max_stars"]:
                gateways.append({"key": "stars", "label": "Telegram Stars", "emoji": "⭐"})

        with get_db_session() as session:
            methods = session.query(ManualPaymentMethod).filter_by(
                is_active=True
            ).order_by(ManualPaymentMethod.sort_order, ManualPaymentMethod.id).all()

            eligible = [
                m for m in methods
                if amount >= (m.min_amount or 0)
                and (not m.max_amount or amount <= m.max_amount)
            ]

            if not eligible and not gateways:
                if not methods:
                    msg = (
                        "❌ No payment methods are available right now.\n\n"
                        "Please contact support — the admin needs to configure at least one payment method."
                    )
                else:
                    min_needed = min(m.min_amount or 0 for m in methods)
                    msg = (
                        f"❌ Amount too low for available methods.\n"
                        f"Minimum accepted: ${min_needed:.2f}\n\nPlease start again with a larger amount."
                    )
                await update.message.reply_text(msg, reply_markup=create_cancel_keyboard())
                return ConversationHandler.END

            methods_data = [(m.id, m.emoji, m.name, m.min_amount) for m in eligible]

        class _M:
            __slots__ = ('id', 'emoji', 'name', 'min_amount')
            def __init__(self, i, e, n, mn):
                self.id, self.emoji, self.name, self.min_amount = i, e, n, mn

        method_objs = [_M(*d) for d in methods_data]

        message = f"💰 <b>Top Up Wallet</b>\n\nAmount: <code>${amount:.2f}</code>\n\nSelect your preferred payment method below."
        await update.message.reply_text(
            message,
            reply_markup=create_payment_method_keyboard(method_objs, gateways),
            parse_mode="HTML",
        )
        return METHOD

    # A payment method was already chosen — validate the amount against
    # THAT method's limits, then create the payment.
    kind, key = method

    if kind == 'manual':
        return await _finish_manual_payment(update, context, key, amount)

    if kind == 'gateway':
        if key == 'bkash':
            bmin = cfg.get_float("bkash_min_amount", 0.0)
            bmax = cfg.get_float("bkash_max_amount", 0.0)
            if amount < bmin or (bmax and amount > bmax):
                await update.message.reply_text(
                    f"❌ Amount outside bKash limits.{_amount_range_hint(bmin, bmax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_gateway_payment(
                update, context, amount,
                payment_method=PaymentMethod.BKASH,
                service_cls=BkashPaymentService,
                gateway_key="bkash", gateway_label="bKash", emoji="📱",
                pay_button_label="📱 Pay with bKash",
            )
        if key == 'nagad':
            nmin = cfg.get_float("nagad_min_amount", 0.0)
            nmax = cfg.get_float("nagad_max_amount", 0.0)
            if amount < nmin or (nmax and amount > nmax):
                await update.message.reply_text(
                    f"❌ Amount outside Nagad limits.{_amount_range_hint(nmin, nmax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_gateway_payment(
                update, context, amount,
                payment_method=PaymentMethod.NAGAD,
                service_cls=NagadPaymentService,
                gateway_key="nagad", gateway_label="Nagad", emoji="🟠",
                pay_button_label="🟠 Pay with Nagad",
            )
        if key == 'cryptomus':
            return await _finish_gateway_payment(
                update, context, amount,
                payment_method=PaymentMethod.CRYPTOMUS,
                service_cls=CryptomusPaymentService,
                gateway_key="cryptomus", gateway_label="Cryptomus", emoji="💠",
                pay_button_label="💠 Pay with Cryptomus",
            )
        if key == 'nowpayments':
            return await _finish_gateway_payment(
                update, context, amount,
                payment_method=PaymentMethod.NOWPAYMENTS,
                service_cls=NowPaymentsService,
                gateway_key="nowpayments", gateway_label="NOWPayments", emoji="🌐",
                pay_button_label="🌐 Pay with NOWPayments",
            )
        if key == 'zinipay':
            return await _finish_zinipay_payment(update, context, amount)
        if key == 'binance_pay':
            bp_svc = BinancePayService()
            bmin, bmax = bp_svc.min_amount, bp_svc.max_amount
            if amount < bmin or (bmax and amount > bmax):
                await update.message.reply_text(
                    f"❌ Amount outside Binance Pay limits.{_amount_range_hint(bmin, bmax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_binance_payment(update, context, amount)
        if key == 'bybit_pay':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside Bybit Pay limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_payment(update, context, amount)
        if key in ('bybit_trc20', 'bybit_bep20', 'bybit_erc20'):
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            network = key.split("_")[1].upper()   # bybit_trc20 → TRC20
            return await _finish_bybit_onchain_direct(update, context, amount, network)
        if key == 'bybit_ltc':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "LTC")
        if key == 'bybit_avaxc':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "AVAXC")
        if key == 'bybit_ton':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "TON")
        if key == 'bybit_base':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "BASE")
        if key == 'bybit_arb':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "ARBONE")
        if key == 'bybit_op':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "OP")
        if key == 'bybit_matic':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "MATIC")
        if key == 'bybit_sol':
            by_svc = BybitPayService()
            bymin, bymax = by_svc.min_amount, by_svc.max_amount
            if amount < bymin or (bymax and amount > bymax):
                await update.message.reply_text(
                    f"❌ Amount outside deposit limits.{_amount_range_hint(bymin, bymax)}",
                    reply_markup=create_cancel_keyboard(),
                )
                return AMOUNT
            return await _finish_bybit_onchain_direct(update, context, amount, "SOL")
        if key == 'stars':
            return await _finish_stars_payment(update, context, amount)

    # Unknown/expired method selection — ask the user to start over rather
    # than silently guessing which method they meant.
    await update.message.reply_text(
        "❌ Session expired. Please start the top-up again.",
        reply_markup=create_cancel_keyboard(),
    )
    return ConversationHandler.END



# ==================== MANUAL PAYMENT FLOW ====================

async def payment_method_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked an admin-managed manual payment method — ask for the amount next."""
    query = update.callback_query
    await query.answer()

    try:
        method_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        try:
            await query.edit_message_text("❌ Invalid payment method.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    with get_db_session() as session:
        method = session.query(ManualPaymentMethod).filter_by(
            id=method_id, is_active=True
        ).first()
        if not method:
            try:
                await query.edit_message_text("❌ Payment method is no longer available.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        method_name = method.name
        method_emoji = method.emoji or "💳"
        mmin = method.min_amount or 0
        mmax = method.max_amount or 0

    context.user_data['topup_method'] = ('manual', method_id)
    hint = _amount_range_hint(mmin, mmax)
    try:
        await query.edit_message_text(
            f"{method_emoji} {method_name} selected.\n\n💬 How much would you like to add to your wallet, in USD?{hint}\nExample: 10",
            reply_markup=create_cancel_keyboard(),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT


async def _finish_manual_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, method_id: int, usd_amount: float):
    """Create the transaction for a previously-chosen manual payment method,
    once the amount has been collected. Mirrors the old payment_method_manual
    body, but replies to a text message instead of editing a callback-query message."""
    telegram_id = update.effective_user.id

    with get_db_session() as session:
        method = session.query(ManualPaymentMethod).filter_by(
            id=method_id, is_active=True
        ).first()
        if not method:
            await update.message.reply_text("❌ Payment method is no longer available. Please start again.")
            return ConversationHandler.END

        if usd_amount < (method.min_amount or 0):
            await update.message.reply_text(
                f"❌ Amount below minimum for {method.name} (min ${method.min_amount:.2f}).",
                reply_markup=create_cancel_keyboard(),
            )
            return AMOUNT
        if method.max_amount and usd_amount > method.max_amount:
            await update.message.reply_text(
                f"❌ Amount above maximum for {method.name} (max ${method.max_amount:.2f}).",
                reply_markup=create_cancel_keyboard(),
            )
            return AMOUNT

        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.MANUAL,
            manual_method_id=method.id,
            status=TransactionStatus.PENDING,
            expires_at=None,  # Manual payments don't auto-expire
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)

        transaction_id = transaction.id
        transaction_created_at = transaction.created_at
        method_name = method.name
        method_emoji = method.emoji or "💳"
        instructions = method.instructions
        acct_label = method.account_label or None
        acct_number = method.account_number or None
        req_txid = bool(method.require_txid)
        req_proof = bool(method.require_proof)

    context.user_data['manual_tx_id'] = transaction_id
    context.user_data['manual_req_txid'] = req_txid
    context.user_data['manual_req_proof'] = req_proof
    context.user_data['manual_method_id'] = method_id

    # Every payment method — this admin-added one included — renders through
    # the exact same PaymentMethodView contract. No gateway-specific code
    # here: just populate the fields this method actually has.
    view = pui.PaymentMethodView(
        name=method_name,
        emoji=method_emoji,
        stage="waiting",
        amount=f"${usd_amount:.2f}",
        deposit_id=transaction_id,
        created_at=transaction_created_at,
        account_label=acct_label,
        account_number=acct_number,
        instructions=instructions,
        requires_txid=req_txid,
        requires_proof=req_proof,
        cancel_cb="cancel",
    )

    await update.message.reply_text(
        view.render(),
        reply_markup=view.keyboard(),
        parse_mode='HTML',
    )
    # Ask TXID first when required; otherwise go straight to proof/note.
    return MANUAL_TXID if req_txid else MANUAL_PROOF


async def payment_manual_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive TXID from user for a manual payment, then request proof if configured."""
    transaction_id = context.user_data.get('manual_tx_id')
    method_id = context.user_data.get('manual_method_id')
    if not transaction_id:
        await update.message.reply_text("❌ Session expired. Please start the top-up again.")
        return ConversationHandler.END

    if not update.message.text:
        await update.message.reply_text("❌ Please send your Transaction ID as text.")
        return MANUAL_TXID

    txid = update.message.text.strip()[:128]
    if len(txid) < 4:
        await update.message.reply_text("❌ TXID looks too short. Please send a valid transaction ID.")
        return MANUAL_TXID

    with get_db_session() as session:
        # Reject reused TXID for the SAME method (per-method uniqueness).
        clash = session.query(Transaction).filter(
            Transaction.txid == txid,
            Transaction.manual_method_id == method_id,
            Transaction.id != transaction_id,
            Transaction.status.in_([
                TransactionStatus.AWAITING_CONFIRMATION,
                TransactionStatus.COMPLETED,
            ]),
        ).first()
        if clash:
            await update.message.reply_text(
                "❌ This Transaction ID was already submitted. Please double-check and send the correct TXID."
            )
            return MANUAL_TXID

        tx = session.query(Transaction).filter_by(id=transaction_id).first()
        if not tx:
            await update.message.reply_text("❌ Transaction not found.")
            return ConversationHandler.END
        tx.txid = txid
        session.commit()

    if context.user_data.get('manual_req_proof'):
        await update.message.reply_text(
            "✅ TXID recorded.\n\n"
            "📸 Now please send a <b>screenshot</b> of your payment as proof.",
            parse_mode='HTML',
        )
        return MANUAL_PROOF

    # No proof required — finalize as if proof step passed with the TXID as note.
    update.message.text = f"TXID: {txid}"
    return await payment_manual_proof(update, context)


async def payment_manual_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive proof (text or photo) from user for a manual payment."""
    transaction_id = context.user_data.get('manual_tx_id')
    if not transaction_id:
        await update.message.reply_text("❌ Session expired. Please start the top-up again.")
        return ConversationHandler.END

    require_proof = context.user_data.get('manual_req_proof', True)

    proof_text = None
    photo_file_id = None
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        proof_text = (update.message.caption or "").strip() or "(screenshot attached)"
    elif update.message.text:
        proof_text = update.message.text.strip()
    else:
        await update.message.reply_text("❌ Please send a text (TXID / note) or a screenshot.")
        return MANUAL_PROOF

    if require_proof and not photo_file_id:
        await update.message.reply_text(
            "❌ A screenshot is required for this payment method. Please attach a photo."
        )
        return MANUAL_PROOF

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=transaction_id).first()
        if not tx:
            await update.message.reply_text("❌ Transaction not found.")
            return ConversationHandler.END

        tx.proof = proof_text
        if photo_file_id:
            tx.proof_file_id = photo_file_id
            # Legacy mirror for older admin panels reading crypto_address.
            tx.crypto_address = f"photo:{photo_file_id}"
        tx.status = TransactionStatus.AWAITING_CONFIRMATION
        session.commit()

        user = session.query(User).filter_by(id=tx.user_id).first()
        # bKash/Nagad Manual mode has no ManualPaymentMethod row (it's a
        # gateway-level toggle, see services/gateway_manual_mode.py) — label
        # it by gateway instead.
        if tx.payment_method == PaymentMethod.BKASH:
            method_name = "bKash (Manual)"
        elif tx.payment_method == PaymentMethod.NAGAD:
            method_name = "Nagad (Manual)"
        else:
            method = session.query(ManualPaymentMethod).filter_by(id=tx.manual_method_id).first()
            method_name = method.name if method else "Manual"
        is_gateway_manual = tx.payment_method in (PaymentMethod.BKASH, PaymentMethod.NAGAD)
        amount = tx.amount
        tg_id = user.telegram_id if user else None
        stored_txid = tx.txid

    # bKash/Nagad manual transactions record `amount` in BDT (the real money
    # the user sent) — see _payment_method_gateway_manual / admin_manual_approve
    # for the BDT->USD conversion applied when the admin approves.
    amount_line = f"৳{amount:.2f} BDT" if is_gateway_manual else f"${amount:.2f}"

    await update.message.reply_text(
        pui.user_payment_card(
            gateway_key="manual",
            gateway_label_override=method_name,
            stage="pending_review",
            amount=amount_line,
            order_id=transaction_id,
            txn_id=stored_txid,
            note="Our team will verify your payment shortly. You'll be notified the "
                 "moment your balance is updated.",
        ),
        reply_markup=create_main_menu_keyboard(user_id=update.effective_user.id),
        parse_mode='HTML',
    )

    # Notify admin with the standardized review card + action buttons.
    # Per-order dedup: only the submission that flips review_notified
    # False→True actually alerts admins — if the user resends proof while
    # still in this conversation step, later attempts are silently skipped.
    review_claimed = False
    try:
        with get_db_session() as _rsess:
            review_claimed = _rsess.query(Transaction).filter(
                Transaction.id == transaction_id,
                Transaction.review_notified.is_(False),
            ).update(
                {Transaction.review_notified: True},
                synchronize_session=False,
            ) == 1
            _rsess.commit()
    except Exception:
        logger.exception("Failed to claim review_notified for tx %s (manual proof)", transaction_id)
        review_claimed = False

    if not review_claimed:
        for k in ('manual_tx_id', 'manual_req_txid', 'manual_req_proof', 'manual_method_id'):
            context.user_data.pop(k, None)
        return ConversationHandler.END

    admin_msg = pui.admin_review_card(
        gateway_key="manual",
        gateway_label_override=method_name,
        amount=amount_line,
        order_id=transaction_id,
        txn_id=stored_txid,
        customer_name=pui.customer_display(update.effective_user.username, tg_id),
        user_id=tg_id,
        status_key="pending_review",
        note=f"📝 <b>Proof:</b> {proof_text}",
    )
    keyboard = pui.admin_review_keyboard(
        verify_cb=f"mp_verify_{transaction_id}",
        approve_cb=f"mp_approve_{transaction_id}",
        reject_cb=f"mp_reject_{transaction_id}",
        view_user_cb=f"admin_view_user_pmv_{tg_id}",
    )
    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=app_settings.ADMIN_TELEGRAM_ID,
                photo=photo_file_id,
                caption=admin_msg,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        else:
            await context.bot.send_message(
                chat_id=app_settings.ADMIN_TELEGRAM_ID,
                text=admin_msg,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
    except Exception as e:
        logger.warning("[manual-payment] admin notify failed: %s", e)

    for k in ('manual_tx_id', 'manual_req_txid', 'manual_req_proof', 'manual_method_id'):
        context.user_data.pop(k, None)
    return ConversationHandler.END


async def admin_manual_verify_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mp_verify_{tx_id} — manual (proof-based) submissions have no
    gateway API to re-query, so this simply prompts the admin to review the
    attached proof again rather than pretending to re-run automated checks.
    Kept for UI consistency: every admin review card always shows the same
    four buttons in the same order."""
    query = update.callback_query
    if update.effective_user.id != app_settings.ADMIN_TELEGRAM_ID and not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    await query.answer(
        "ℹ️ This is a manual submission — there's no gateway API to re-verify. "
        "Please review the proof/screenshot above before approving or rejecting.",
        show_alert=True,
    )


async def admin_manual_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves a pending manual payment — credit user's wallet."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != app_settings.ADMIN_TELEGRAM_ID:
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        tx_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    # Idempotency guard — stable reference is the transaction's own DB id
    # (never the Telegram update_id, which changes on redelivery/retry).
    # Defense-in-depth alongside the atomic conditional UPDATE below: if the
    # claim call itself raises, fail CLOSED (no credit).
    try:
        from services.idempotency import claim as _idem_claim
        with _idem_claim("manual_approve", f"tx:{tx_id}") as _ok:
            if not _ok:
                logger.info("admin_manual_approve: duplicate approval for tx %s", tx_id)
                return
    except Exception:
        logger.error(
            "idempotency.claim raised for manual_approve tx %s — refusing to "
            "credit wallet (fail closed)", tx_id, exc_info=True,
        )
        return

    user_tg_id = None
    amount = 0.0
    credited_usd = 0.0
    new_balance = 0.0
    is_gateway_manual = False
    with get_db_session() as session:
        # Atomically flip PENDING/AWAITING → COMPLETED (idempotent).
        # Covers both the generic ManualPaymentMethod flow (payment_method ==
        # MANUAL) and the bKash/Nagad Manual-mode flow (payment_method ==
        # BKASH/NAGAD, see services/gateway_manual_mode.py — only reachable
        # here via the "mp_approve_<id>" button sent from
        # payment_manual_proof, so no auto/API transaction is ever at risk).
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_([
                PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD,
            ]),
            Transaction.status.in_([
                TransactionStatus.PENDING,
                TransactionStatus.AWAITING_CONFIRMATION,
            ]),
        ).update(
            {
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
        if flipped == 0:
            return  # Already processed or invalid — idempotent no-op

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            return

        is_gateway_manual = tx.payment_method in (PaymentMethod.BKASH, PaymentMethod.NAGAD)
        if is_gateway_manual:
            # bKash/Nagad Manual mode stores `amount` in BDT (the real money
            # the user was asked to send) — convert to USD with the store's
            # deposit rate before crediting the wallet (wallet_balance is
            # always USD). See services/pricing.py convert_currency /
            # get_usd_to_bdt_rate for the admin-configurable rate.
            credited_usd = convert_currency(tx.amount, "BDT", "USD")
            tx.admin_note = (
                f"Manual {tx.payment_method.value} deposit: ৳{tx.amount:.2f} BDT "
                f"→ ${credited_usd:.2f} USD credited (deposit rate applied)."
            )
        else:
            credited_usd = tx.amount

        # Atomic wallet credit (always USD)
        session.query(User).filter(User.id == tx.user_id).update(
            {User.wallet_balance: User.wallet_balance + credited_usd},
            synchronize_session=False,
        )
        session.commit()

        user = session.query(User).filter_by(id=tx.user_id).first()
        if user:
            user_tg_id = user.telegram_id
            amount = tx.amount
            new_balance = user.wallet_balance
            _dep_pm_label = pui.gateway_meta(tx.payment_method.value)[0]
        # Activity Feed: wallet top-up approved (best-effort, non-blocking)
        try:
            import asyncio as _asyncio
            from services.activity_feed import post_event as _af_post, EVENT_WALLET_TOPUP
            _af_uname = user.username if user else ""
            _af_name = user.username or str(user.telegram_id) if user else str(tx.user_id)
            _asyncio.create_task(_af_post(context.bot, EVENT_WALLET_TOPUP, {
                "customer_telegram_id": user.telegram_id if user else "—",
                "customer_name": _af_name,
                "amount": credited_usd,
                "payment_method": tx.payment_method.value if tx else "—",
                "transaction_id": tx_id,
            }))
        except Exception:
            pass
        # V19 — deposit receipt + activity log (best-effort)
        try:
            from handlers.account_features import create_receipt_record, log_activity
            create_receipt_record(
                order_id=None, transaction_id=tx_id,
                user_id_db=tx.user_id, receipt_type="deposit",
            )
            log_activity(
                user_id_db=tx.user_id, action="deposit", status="success",
                details=f"${credited_usd:.2f} deposited (manual approval)",
                ref_type="transaction", ref_id=str(tx_id),
            )
        except Exception:
            pass

        # Enterprise Admin Notification: deposit completed (best-effort)
        try:
            import asyncio as _asyncio
            from services.notifications import notify_admins as _notify_admins
            from utils.notify_format import render as _render_notif, utc_now_str as _ts
            from utils.helpers import format_deposit_id as _fmt_did
            _dep_amt_str = (
                f"৳{amount:.2f} BDT → ${credited_usd:.2f} USD"
                if is_gateway_manual else f"${credited_usd:.2f}"
            )
            _dep_method = tx.payment_method.value if tx else "Manual"
            _asyncio.create_task(_notify_admins(
                context.bot,
                "deposit",
                _render_notif("💰", "Deposit Approved", [
                    ("Deposit ID", _fmt_did(tx_id)),
                    ("Amount", _dep_amt_str),
                    ("Method", _dep_method),
                    ("Customer", f"<code>{user_tg_id}</code>" if user_tg_id else "—"),
                ], _ts()),
            ))
        except Exception:
            pass

    if is_gateway_manual:
        caption = pui.build_card(
            title="Payment Review", title_emoji="🛎️",
            fields=[("🧾", "Deposit ID", pui.format_deposit_id(tx_id)), ("💰", "Amount", f"৳{amount:.2f} BDT → ${credited_usd:.2f}")],
            status_key="approved",
        )
    else:
        caption = pui.build_card(
            title="Payment Review", title_emoji="🛎️",
            fields=[("🧾", "Deposit ID", pui.format_deposit_id(tx_id)), ("💰", "Amount", f"${credited_usd:.2f}")],
            status_key="approved",
        )
    try:
        if query.message.photo:
            try:
                await query.edit_message_caption(caption, parse_mode='HTML')
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            try:
                await query.edit_message_text(caption, parse_mode='HTML')
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
    except Exception:
        logger.warning('Ignored Telegram/API error', exc_info=True)

    if user_tg_id:
        try:
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.deposit_success_card(
                        amount=f"${credited_usd:.2f} USD",
                        payment_method=_dep_pm_label if '_dep_pm_label' in dir() else "Manual Payment",
                        deposit_id=pui.format_deposit_id(tx_id),
                    )
                ),
                reply_markup=pui.deposit_success_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            logger.warning('Ignored Telegram/API error', exc_info=True)


async def admin_manual_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejects a pending manual payment."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != app_settings.ADMIN_TELEGRAM_ID:
        return

    try:
        tx_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    user_tg_id = None
    amount = 0.0
    is_gateway_manual = False
    with get_db_session() as session:
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_([
                PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD,
            ]),
            Transaction.status.in_([
                TransactionStatus.PENDING,
                TransactionStatus.AWAITING_CONFIRMATION,
            ]),
        ).update(
            {Transaction.status: TransactionStatus.REJECTED},
            synchronize_session=False,
        )
        if flipped == 0:
            return  # Already processed
        session.commit()

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        user = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        if tx:
            is_gateway_manual = tx.payment_method in (PaymentMethod.BKASH, PaymentMethod.NAGAD)
        if user:
            user_tg_id = user.telegram_id
            amount = tx.amount

    caption = pui.build_card(
        title="Payment Review", title_emoji="🛎️",
        fields=[("🧾", "Deposit ID", pui.format_deposit_id(tx_id))],
        status_key="rejected",
    )
    try:
        if query.message.photo:
            try:
                await query.edit_message_caption(caption, parse_mode='HTML')
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            try:
                await query.edit_message_text(caption, parse_mode='HTML')
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
    except Exception:
        logger.warning('Ignored Telegram/API error', exc_info=True)

    amount_str = f"৳{amount:.2f} BDT" if is_gateway_manual else f"${amount:.2f}"
    if user_tg_id:
        try:
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.user_payment_card(
                        gateway_key="manual",
                        stage="rejected",
                        amount=amount_str,
                        order_id=tx_id,
                        note="If you believe this is a mistake, please contact support with your proof.",
                    )
                ),
                parse_mode='HTML',
            )
        except Exception:
            logger.warning('Ignored Telegram/API error', exc_info=True)

    # Activity Feed: failed payment (best-effort, non-blocking)
    try:
        import asyncio as _asyncio
        from services.activity_feed import post_event as _af_post, EVENT_FAILED_PAYMENT
        _asyncio.create_task(_af_post(context.bot, EVENT_FAILED_PAYMENT, {
            "customer_telegram_id": user_tg_id or "—",
            "amount": amount,
            "payment_method": "BDT Manual" if is_gateway_manual else "Manual",
            "transaction_id": tx_id,
            "reason": "Rejected by admin",
        }))
    except Exception:
        pass

    # Enterprise Admin Notification: payment reversed (best-effort)
    try:
        import asyncio as _asyncio
        from services.notifications import notify_admins as _notify_admins
        from utils.notify_format import render as _render_notif, utc_now_str as _ts
        from utils.helpers import format_deposit_id as _fmt_did
        _amt_str = f"৳{amount:.2f} BDT" if is_gateway_manual else f"${amount:.2f}"
        _asyncio.create_task(_notify_admins(
            context.bot,
            "payment_reversed",
            _render_notif("❌", "Deposit Rejected", [
                ("Deposit ID", _fmt_did(tx_id)),
                ("Amount", _amt_str),
                ("Customer", f"<code>{user_tg_id}</code>" if user_tg_id else "—"),
                ("Reason", "Rejected by admin"),
            ], _ts()),
        ))
    except Exception:
        pass



async def payment_method_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Crypto Wallet payment method selection."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Expired orders must never block a new one — reconcile first.
        _auto_cancel_expired_pending(session, user.id, PaymentMethod.CRYPTO_WALLET)

        # Check if user already has a pending CryptoBot transaction
        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id,
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING
        ).first()

        if existing_pending:
            # Show full payment details for the existing pending order
            # Extract pay_url from crypto_address (format: "invoice_id|pay_url")
            if existing_pending.crypto_address and "|" in existing_pending.crypto_address:
                invoice_id, pay_url = existing_pending.crypto_address.split("|", 1)
            else:
                pay_url = existing_pending.crypto_address if existing_pending.crypto_address else "#"

            expires_str = (
                existing_pending.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')
                if existing_pending.expires_at else 'N/A'
            )
            message = pui.user_payment_card(
                gateway_key="cryptobot",
                stage="waiting",
                amount=pui.copy_code(format_price(existing_pending.amount)),
                order_id=existing_pending.id,
                created_at=existing_pending.created_at,
                extra=[("🪙", "Accepted Assets", "BTC · TON · USDT · USDC · ETH · LTC · BNB · TRX and more"),
                       ("⏰", "Expires", expires_str)],
                note="⚠️ You already have a pending CryptoBot payment — tap below to finish it. "
                     "You can't start a new deposit until this one is completed or expired.",
            )

            # Create keyboard with payment button
            keyboard = [
                [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(
                    message,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Create transaction record
        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(cfg.get_int("payment_expiry_minutes", 30) / 60.0)
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)

        # Generate payment invoice in USD (accepts any cryptocurrency)
        crypto_service = CryptoBotService()
        payment_address = crypto_service.generate_payment_address(
            usd_amount,
            transaction.id
        )

        if not payment_address:
            transaction.status = TransactionStatus.FAILED
            session.commit()
            # Enterprise Admin Notification: payment failed (best-effort)
            try:
                import asyncio as _asyncio
                from services.notifications import notify_admins as _notify_admins
                from utils.notify_format import render as _render_notif, utc_now_str as _ts
                from utils.helpers import format_deposit_id as _fmt_did
                _asyncio.create_task(_notify_admins(
                    context.bot,
                    "payment_failed",
                    _render_notif("⚙️", "Payment Gateway Error", [
                        ("Deposit ID", _fmt_did(transaction.id)),
                        ("Amount", format_price(usd_amount)),
                        ("Gateway", "CryptoBot"),
                        ("Customer", f"<code>{user_id}</code>"),
                        ("Reason", "Failed to generate payment invoice"),
                    ], _ts()),
                ))
            except Exception:
                pass
            fail_text = pui.build_card(
                title="Payment Failed",
                title_emoji="❌",
                fields=[("💳", "Gateway", "CryptoBot"), ("💰", "Amount", format_price(usd_amount))],
                status_key="failed",
                note="We couldn't generate your payment invoice. No balance was deducted — "
                     "please try again or choose a different payment method.",
            )
            try:
                await query.edit_message_text(
                    fail_text, reply_markup=pui.payment_failed_keyboard(), parse_mode='HTML',
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Update transaction with crypto address (format: "invoice_id|pay_url")
        transaction.crypto_address = payment_address
        session.commit()

        # Extract pay_url from payment_address
        if "|" in payment_address:
            invoice_id, pay_url = payment_address.split("|", 1)
            logger.debug("Invoice created: ID=%s, URL=%s", invoice_id, pay_url)
        else:
            # Fallback for unexpected format
            pay_url = payment_address

        # Show payment instructions
        message = pui.user_payment_card(
            gateway_key="cryptobot",
            stage="created",
            amount=pui.copy_code(format_price(usd_amount)),
            order_id=transaction.id,
            created_at=transaction.created_at,
            extra=[("🪙", "Accepted Assets", "BTC · TON · USDT · USDC · ETH · LTC · BNB · TRX and more"),
                   ("⏱", "Expires in", "30 minutes")],
            note="👉 Tap below to open the payment page and pay with any supported cryptocurrency. "
                 "Your balance will be credited automatically the moment payment is confirmed.",
        )

        # Create keyboard with payment button
        keyboard = [
            [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    return ConversationHandler.END


def _extract_pay_url(address: str) -> str | None:
    """Shared helper: pull the "...|pay_url" half out of a stored gateway
    reference (crypto_address column), used by bKash/Nagad/CryptoBot alike.
    Returns None (not a placeholder) when there's no valid http(s) URL,
    since Telegram rejects inline URL buttons that aren't a real absolute URL."""
    candidate = address
    if address and "|" in address:
        candidate = address.split("|", 1)[1]
    if candidate and candidate.startswith(("http://", "https://")):
        return candidate
    return None


@perf_track("payment_creation")
async def _finish_gateway_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float,
    *, payment_method, service_cls, gateway_key: str, gateway_label: str,
    emoji: str, pay_button_label: str,
):
    """Dispatcher: routes bKash/Nagad to their manual-mode flow if the admin
    has enabled it (see services/gateway_manual_mode.py), otherwise creates
    the payment via the automated gateway. Cryptomus always goes automated."""
    if gateway_key in ("bkash", "nagad") and gw_mode.is_manual(gateway_key):
        return await _finish_gateway_manual_payment(
            update, context, usd_amount,
            payment_method=payment_method, gateway_key=gateway_key,
            gateway_label=gateway_label, emoji=emoji,
        )
    return await _finish_gateway_automated_payment(
        update, context, usd_amount,
        payment_method=payment_method, service_cls=service_cls,
        gateway_key=gateway_key, gateway_label=gateway_label, pay_button_label=pay_button_label,
    )


async def _finish_gateway_automated_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float,
    *, payment_method, service_cls, gateway_label: str, pay_button_label: str,
    gateway_key: str = None,
):
    """Shared flow for automated gateways (bKash / Nagad / Cryptomus), once
    the amount has been collected. Mirrors payment_method_crypto: reuse an
    existing pending transaction for this gateway if present, otherwise
    create one and call the gateway's create_payment(). Both gateways store
    their reference in the same `crypto_address` column using the
    "id|pay_url" convention already used by CryptoBotService, so
    check_pending_payments / check_expired_payments keep working unchanged.
    """
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        # Expired orders must never block a new one — reconcile first.
        _auto_cancel_expired_pending(session, user.id, payment_method)

        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id,
            payment_method=payment_method,
            status=TransactionStatus.PENDING,
        ).first()

        if existing_pending:
            pay_url = _extract_pay_url(existing_pending.crypto_address)

            # ---- Recover orphaned PENDING transactions ----
            # If crypto_address is None/empty (e.g. the gateway API call failed
            # during a previous session but the status-flip to FAILED was lost),
            # try to regenerate the payment reference now so the user can proceed.
            if not existing_pending.crypto_address:
                logger.warning(
                    "Existing pending %s transaction #%s has no crypto_address — "
                    "attempting payment regeneration",
                    gateway_label, existing_pending.id,
                )
                _recovery_svc = service_cls()
                _recovery_ref = _recovery_svc.create_payment(
                    float(existing_pending.amount), existing_pending.id
                )
                if _recovery_ref:
                    existing_pending.crypto_address = _recovery_ref
                    session.commit()
                    pay_url = _extract_pay_url(_recovery_ref)
                    logger.info(
                        "Recovered %s transaction #%s with new reference=%r",
                        gateway_label, existing_pending.id, _recovery_ref,
                    )
                else:
                    # Still couldn't create one — cancel the orphan so the user
                    # can start fresh on their next attempt.
                    logger.error(
                        "Could not recover %s transaction #%s (error=%r) — marking FAILED",
                        gateway_label, existing_pending.id,
                        getattr(_recovery_svc, "last_error", ""),
                    )
                    existing_pending.status = TransactionStatus.FAILED
                    session.commit()
                    await update.message.reply_text(
                        f"⚠️ Your previous {gateway_label} order could not be recovered and has been "
                        f"cancelled.  Please try again."
                    )
                    return ConversationHandler.END

            expires_str = (
                existing_pending.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')
                if existing_pending.expires_at else 'N/A'
            )
            message = pui.user_payment_card(
                gateway_key=gateway_key,
                gateway_label_override=gateway_label,
                stage="waiting",
                amount=format_price(existing_pending.amount),
                order_id=existing_pending.id,
                extra=[("⏰", "Expires", expires_str)],
                note="⚠️ You already have a pending payment for this gateway — tap below "
                     "to finish it. You can't start a new order until this one is "
                     "completed or expired.",
            )
            keyboard = []
            if pay_url:
                keyboard.append([InlineKeyboardButton(pay_button_label, url=pay_url)])
            else:
                message += "\n\n⚠️ The payment link is missing — please contact support with your Deposit ID above."
                logger.warning(
                    "Existing pending %s transaction #%s has no valid pay_url (crypto_address=%r)",
                    gateway_label, existing_pending.id, existing_pending.crypto_address,
                )
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            await update.message.reply_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
            )
            return ConversationHandler.END

        # ---- Create a new PENDING transaction row first, then call the gateway. ----
        # The two-step commit (PENDING → then update crypto_address) means that if
        # the gateway call fails, we explicitly mark it FAILED in a separate
        # session so the status flip is durable even if the original session
        # encounters a connection error during commit.
        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=payment_method,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(cfg.get_int("payment_expiry_minutes", 30) / 60.0),
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        new_tx_id = transaction.id
        # End the outer session before the gateway HTTP call so we don't hold
        # a DB connection open during a potentially slow network request.

    # ---- Gateway API call (outside the session) ----
    service = service_cls()
    reference = service.create_payment(usd_amount, new_tx_id)

    if not reference:
        # Use a fresh session to mark FAILED so it's durable regardless of
        # any connection error on the original session.
        with get_db_session() as _fail_session:
            _fail_session.query(Transaction).filter(
                Transaction.id == new_tx_id,
                Transaction.status == TransactionStatus.PENDING,
            ).update(
                {Transaction.status: TransactionStatus.FAILED},
                synchronize_session=False,
            )
            _fail_session.commit()
        text = pui.build_card(
            title="Payment Failed",
            title_emoji="❌",
            fields=[("💳", "Gateway", gateway_label), ("💰", "Amount", format_price(usd_amount))],
            status_key="failed",
            note=f"We couldn't start your {gateway_label} payment. No balance was deducted — "
                 f"please try again or choose a different payment method.",
        )
        last_error = getattr(service, "last_error", "")
        if last_error and is_admin(update.effective_user.id):
            text += f"\n\n🔧 Admin detail: {last_error}"
        await update.message.reply_text(text, reply_markup=pui.payment_failed_keyboard(), parse_mode='HTML')
        return ConversationHandler.END

    # ---- Persist the gateway reference and show the user their payment link ----
    with get_db_session() as session:
        session.query(Transaction).filter(Transaction.id == new_tx_id).update(
            {Transaction.crypto_address: reference},
            synchronize_session=False,
        )
        session.commit()
        # Reload for the rest of the function (amounts etc.)
        transaction = session.query(Transaction).filter_by(id=new_tx_id).first()

        pay_url = _extract_pay_url(reference)

        message = pui.user_payment_card(
            gateway_key=gateway_key,
            gateway_label_override=gateway_label,
            stage="created",
            amount=format_price(usd_amount),
            order_id=transaction.id,
            extra=[("⏱", "Expires in", "30 minutes")],
            note=f"👉 Tap below to pay via {gateway_label}. Your balance will be "
                 f"credited automatically the moment payment is confirmed.",
        )
        keyboard = []
        if pay_url:
            keyboard.append([InlineKeyboardButton(pay_button_label, url=pay_url)])
        else:
            message += "\n\n⚠️ The payment link is missing — please contact support with your Deposit ID above."
            logger.warning(
                "New %s transaction #%s has no valid pay_url (reference=%r)",
                gateway_label, transaction.id, reference,
            )
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await update.message.reply_text(
            message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
        )

    return ConversationHandler.END


async def _finish_gateway_manual_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float,
    *, payment_method, gateway_key: str, gateway_label: str, emoji: str,
):
    """Manual-mode flow for bKash/Nagad (see services/gateway_manual_mode.py),
    once the amount has been collected. Mirrors _finish_manual_payment, but
    the merchant number / instructions come from the gateway's own
    PaymentGatewayConfig row instead of a ManualPaymentMethod DB row. Feeds
    into the SAME MANUAL_TXID / MANUAL_PROOF conversation states used by
    admin-managed manual payment methods, so TrxID/screenshot verification
    and admin notification are unchanged.
    """
    telegram_id = update.effective_user.id
    details = gw_mode.get_manual_details(gateway_key)
    merchant_number = details["merchant_number"]
    instructions = details["instructions"]

    if not merchant_number:
        await update.message.reply_text(
            f"❌ {gateway_label} manual payment isn't fully configured yet "
            f"(missing merchant number). Please choose another method or contact support."
        )
        return ConversationHandler.END

    # bKash/Nagad are BDT mobile-money rails — quote the BDT amount to send
    # using the store's admin-configurable USD<->BDT deposit rate (the same
    # rate used elsewhere for wallet/display conversions — see
    # services/pricing.py get_usd_to_bdt_rate / convert_currency). The
    # inverse conversion runs again when the admin approves the payment.
    bdt_amount = convert_currency(usd_amount, "USD", "BDT")

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=bdt_amount,
            payment_method=payment_method,
            manual_method_id=None,
            status=TransactionStatus.PENDING,
            expires_at=None,  # Manual payments don't auto-expire
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        transaction_id = transaction.id

    context.user_data['manual_tx_id'] = transaction_id
    context.user_data['manual_req_txid'] = True
    context.user_data['manual_req_proof'] = True
    context.user_data['manual_method_id'] = None

    message = pui.user_payment_card(
        gateway_key=gateway_key,
        gateway_label_override=f"{gateway_label} (Manual)",
        stage="waiting",
        amount=f"৳{bdt_amount:.2f} BDT (≈ ${usd_amount:.2f})",
        order_id=transaction_id,
        extra=[("📞", "Send to", f"<code>{merchant_number}</code>")],
        note=(
            f"📝 <b>Payment Instructions</b>\n"
            f"{instructions or f'Send the amount above via {gateway_label} to the number shown.'}\n\n"
            f"👉 After sending, reply here with your <b>Transaction ID (TrxID)</b> to continue."
        ),
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    await update.message.reply_text(
        message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
    )

    return MANUAL_TXID


# ==================== ZINIPAY FLOW ====================
# See services/zinipay_payment.py for the verify+confirm logic.
# The user is shown payment instructions (merchant number from bot_config),
# then asked to submit their TXID.  Verification happens in-bot via the
# ZiniPay /v1/trx/verify → /v1/trx/confirm API; no hosted checkout link,
# no webhook, no background polling.

async def _finish_zinipay_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float
):
    """Create the internal order for a ZiniPay top-up and show the payment
    instruction screen, then ask the user for their Transaction ID.

    The user pays via bKash / Nagad / Rocket / Upay directly to the merchant
    numbers configured in the admin panel.  The BDT amount (converted from USD
    using the admin-configured or global exchange rate) is shown to the user
    and stored in Transaction.crypto_address so it can be used for ZiniPay
    API verification later.
    """
    from services.zinipay_payment import ZiniPayService
    from services.pricing import get_usd_to_bdt_rate

    telegram_id = update.effective_user.id
    svc = ZiniPayService()

    if not svc.enabled or not svc.is_configured():
        await update.message.reply_text(
            "❌ BKash • Nagad • Rocket is not available right now. Please choose another method or contact support."
        )
        return ConversationHandler.END

    # Load all wallet numbers + rate from PaymentGatewayConfig.
    with get_db_session() as session:
        from database.models import PaymentGatewayConfig as _PGC
        pgc = session.query(_PGC).filter_by(gateway="zinipay").first()
        bkash_num  = (pgc.zinipay_bkash_number  or "").strip() if pgc else ""
        nagad_num  = (pgc.zinipay_nagad_number   or "").strip() if pgc else ""
        rocket_num = (pgc.zinipay_rocket_number  or "").strip() if pgc else ""
        upay_num   = (pgc.zinipay_upay_number    or "").strip() if pgc else ""
        default_provider = (pgc.zinipay_default_provider or "bkash").lower() if pgc else "bkash"
        custom_rate = pgc.zinipay_usd_to_bdt_rate if pgc else None
        instructions_text = (pgc.zinipay_instructions or "").strip() if pgc else ""

    # Exchange rate: use per-gateway override if set, otherwise global Settings rate.
    if custom_rate and custom_rate > 0:
        rate = float(custom_rate)
    else:
        rate = get_usd_to_bdt_rate()

    bdt_amount = round(usd_amount * rate, 2)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        _auto_cancel_expired_pending(session, user.id, PaymentMethod.ZINIPAY)

        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id, payment_method=PaymentMethod.ZINIPAY,
            status=TransactionStatus.PENDING,
        ).first()
        if existing_pending:
            await update.message.reply_text(
                pui.user_payment_card(
                    gateway_key="zinipay", stage="waiting",
                    amount=format_price(existing_pending.amount),
                    order_id=existing_pending.id,
                    note="⚠️ You already have a pending order for this gateway. "
                         "Please complete or cancel it before starting a new one.",
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🧾 Submit TXID", callback_data=f"zinipay_submit:{existing_pending.id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                ]]),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.ZINIPAY,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(cfg.get_int("payment_expiry_minutes", 30) / 60.0),
            # Store expected BDT amount so zinipay_txid_received can verify
            # against the correct local-currency figure.
            crypto_address=f"bdt:{bdt_amount:.2f}",
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        tx_id = transaction.id

    # Build the wallet numbers block — only show lines that are configured.
    PROVIDER_EMOJI = {
        "bkash":  "💗",
        "nagad":  "🟠",
        "rocket": "🟣",
        "upay":   "🔵",
    }
    wallet_lines = []
    for provider, number in [
        ("bkash",  bkash_num),
        ("nagad",  nagad_num),
        ("rocket", rocket_num),
        ("upay",   upay_num),
    ]:
        if number:
            star = " ⭐" if provider == default_provider else ""
            emoji = PROVIDER_EMOJI[provider]
            wallet_lines.append(
                f"  {emoji} <b>{provider.title()}{star}:</b>  <code>{number}</code>"
            )

    if not wallet_lines:
        await update.message.reply_text(
            "❌ No payment numbers configured yet. Please contact support."
        )
        return ConversationHandler.END

    wallet_block = "\n".join(wallet_lines)

    # Default instructions if admin hasn't set custom ones.
    if not instructions_text:
        instructions_text = (
            "1. Open your mobile banking app.\n"
            "2. Send the exact amount.\n"
            "3. Copy your Transaction ID (TXID).\n"
            "4. Tap \"Submit Transaction ID\"."
        )

    message = pui.user_payment_card(
        gateway_key="zinipay",
        stage="waiting",
        amount=f"৳{bdt_amount:.2f} BDT (≈ ${usd_amount:.2f})",
        order_id=tx_id,
        extra=[
            ("💱", "Rate", f"1 USD = {rate:.2f} BDT"),
            ("⏱", "Expires in", "30 minutes"),
        ],
        note=(
            f"📲 <b>Send exactly ৳{bdt_amount:.2f} to:</b>\n{wallet_block}\n\n"
            f"📋 <b>Steps:</b>\n{instructions_text}"
        ),
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Submit Transaction ID", callback_data=f"zinipay_submit:{tx_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
    return ConversationHandler.END


async def zinipay_submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the 'Submit Transaction ID' button on a ZiniPay
    payment screen — a standalone mini-conversation, independent of the
    (already-ended) top-up conversation."""
    query = update.callback_query
    await query.answer()
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order", show_alert=True)
        return ConversationHandler.END

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            await query.answer("⛔ Not your order.", show_alert=True)
            return ConversationHandler.END
        if tx.payment_method != PaymentMethod.ZINIPAY:
            await query.answer("Invalid order type.", show_alert=True)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await query.answer("This order is no longer pending.", show_alert=True)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await query.answer("⏰ This order has expired.", show_alert=True)
            return ConversationHandler.END

    context.user_data['zinipay_tx_id'] = tx_id
    await query.message.reply_text(
        "🧾 Please paste your payment Transaction ID (TXID).\n\n"
        "This is the transaction/reference ID you received from bKash / Nagad / Rocket "
        "after completing your payment.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="zinipay_cancel_submit"),
        ]]),
    )
    return ZINIPAY_TXID


async def zinipay_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the ZiniPay TXID submission mini-conversation."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('zinipay_tx_id', None)
    try:
        await query.edit_message_text(
            "❌ Cancelled. Your order is still pending — you can submit the "
            "Transaction ID again anytime before it expires."
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END


async def zinipay_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify and confirm a submitted ZiniPay TXID.

    Flow:
      1. Call ZiniPayService.verify_transaction() → get trxID + verify_id.
      2. Call ZiniPayService.confirm_transaction()
      3. Insert ZiniPayUsedTransaction (UNIQUE on trx_id → replay guard).
      4. Atomically flip Transaction → COMPLETED and credit wallet.
    """
    from services.zinipay_payment import ZiniPayService

    telegram_id = update.effective_user.id
    txid_raw = (update.message.text or "").strip()
    tx_id = context.user_data.get('zinipay_tx_id')

    if not tx_id:
        await update.message.reply_text(
            "❌ Session expired. Please tap 'Submit TXID' from your pending order again."
        )
        return ConversationHandler.END

    if not txid_raw or len(txid_raw) < 4:
        await update.message.reply_text(
            "❌ That doesn't look like a valid Transaction ID. "
            "Please paste the exact TXID from your payment confirmation."
        )
        return ZINIPAY_TXID

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            context.user_data.pop('zinipay_tx_id', None)
            return ConversationHandler.END

        tx = session.query(Transaction).filter_by(id=tx_id, user_id=user.id).first()
        if not tx or tx.payment_method != PaymentMethod.ZINIPAY:
            await update.message.reply_text("❌ Order not found.")
            context.user_data.pop('zinipay_tx_id', None)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await update.message.reply_text("❌ This order is no longer pending.")
            context.user_data.pop('zinipay_tx_id', None)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await update.message.reply_text("⏰ This order has expired. Please start a new top-up.")
            context.user_data.pop('zinipay_tx_id', None)
            return ConversationHandler.END

        # Fast pre-check — the UNIQUE constraint at INSERT time is the real guard.
        already = session.query(ZiniPayUsedTransaction).filter_by(trx_id=txid_raw).first()
        if already:
            await update.message.reply_text(
                "❌ This Transaction ID has already been used. "
                "If you believe this is an error, please contact support."
            )
            return ZINIPAY_TXID

        usd_amount = tx.amount
        # Recover the expected BDT amount stored at order-creation time.
        # If the field is missing (old row or edge case) fall back to recalculating.
        bdt_amount: float = 0.0
        if tx.crypto_address and tx.crypto_address.startswith("bdt:"):
            try:
                bdt_amount = float(tx.crypto_address[4:])
            except ValueError:
                pass
        if bdt_amount <= 0:
            from services.pricing import get_usd_to_bdt_rate as _gbdt
            bdt_amount = round(usd_amount * _gbdt(), 2)

    # ---- Step 1: Verify ----
    processing_msg = await update.message.reply_text("⏳ Verifying your transaction…")

    svc = ZiniPayService()
    # Verify against the BDT amount the user was instructed to send, not the
    # internal USD amount — ZiniPay checks the actual transferred amount.
    verify_result = await asyncio.to_thread(
        svc.verify_transaction,
        amount=bdt_amount,
        transaction_id=txid_raw,
    )

    if verify_result is None:
        error_detail = svc.last_error or "Unknown error"
        lower_err = error_detail.lower()
        is_amount_mismatch = "wrong amount" in lower_err or "amount" in lower_err

        # Persist the attempt so support/admins can see the history.
        try:
            with get_db_session() as _sess:
                _sess.add(VerificationAttemptLog(
                    gateway="zinipay",
                    telegram_user_id=telegram_id,
                    internal_order_id=tx_id,
                    submitted_txid=txid_raw,
                    outcome="AUTO_VERIFY_FAILED",
                    detail=error_detail[:500] if error_detail else None,
                ))
                _sess.commit()
        except Exception:
            logger.exception("Failed to write VerificationAttemptLog (zinipay)")

        # ── Queue every failed Mobile Banking (bKash/Nagad/Rocket) payment
        # for admin manual review — previously this was dropped entirely,
        # leaving the user's payment stuck with no way for an admin to
        # approve or reject it. Dedup on (gateway, order, txid) so retries
        # of the same TXID don't spam admins with duplicate notifications. ──
        pmv_id = None
        try:
            with get_db_session() as _sess:
                existing_pmv = _sess.query(PendingManualVerification).filter_by(
                    gateway="zinipay",
                    internal_order_id=tx_id,
                    submitted_txid=txid_raw,
                ).first()
                if existing_pmv:
                    pmv_id = existing_pmv.id
                else:
                    pmv = PendingManualVerification(
                        gateway="zinipay",
                        telegram_user_id=telegram_id,
                        internal_order_id=tx_id,
                        submitted_txid=txid_raw,
                        amount=usd_amount,
                        currency="USD",
                        payment_type="mobile_banking",
                        auto_outcome="AUTO_VERIFY_FAILED",
                        auto_detail=(f"{error_detail} (expected ৳{bdt_amount:.2f} BDT)")[:500],
                        status="pending",
                    )
                    _sess.add(pmv)
                    _sess.commit()
                    _sess.refresh(pmv)
                    pmv_id = pmv.id
        except Exception:
            logger.exception("Failed to create PendingManualVerification (zinipay)")

        if pmv_id is not None:
            # Per-order dedup — only the FIRST failed-verify attempt for this
            # order should ever alert admins, no matter how many times the
            # user resubmits a TXID afterward. Atomic conditional UPDATE:
            # only the caller that flips review_notified False→True sends.
            review_claimed = False
            try:
                with get_db_session() as _rsess:
                    review_claimed = _rsess.query(Transaction).filter(
                        Transaction.id == tx_id,
                        Transaction.review_notified.is_(False),
                    ).update(
                        {Transaction.review_notified: True},
                        synchronize_session=False,
                    ) == 1
                    _rsess.commit()
            except Exception:
                logger.exception("Failed to claim review_notified for tx %s (zinipay)", tx_id)
                review_claimed = False

            if review_claimed:
                try:
                    _uname = update.effective_user.username
                    _uname_display = f"@{_uname}" if _uname else "(no username)"
                    order_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    for admin_id in _gateway_admin_recipient_ids():
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="zinipay",
                                    amount=f"৳{bdt_amount:.2f} BDT (${usd_amount:.2f} USD)",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    customer_name=_uname_display,
                                    user_id=telegram_id,
                                    time_str=order_time,
                                    status_key="pending_review",
                                    note=f"⚠️ <b>Auto-verify failed:</b> {error_detail}",
                                ),
                                reply_markup=pui.admin_review_keyboard(
                                    verify_cb=f"admin_zinipay_verify_{tx_id}_{pmv_id}",
                                    approve_cb=f"admin_zinipay_approve_{tx_id}_{pmv_id}",
                                    reject_cb=f"admin_zinipay_reject_start_{tx_id}_{pmv_id}",
                                    view_user_cb=f"admin_view_user_pmv_{telegram_id}",
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            logger.exception("Failed to notify admin %s for ZiniPay manual verification", admin_id)
                except Exception:
                    logger.exception("Failed to send admin notification(s) for ZiniPay manual verification")

        # Provide user-friendly messages for known rejection reasons.
        if is_amount_mismatch and pmv_id:
            user_msg = pui.user_payment_card(
                gateway_key="zinipay", stage="pending_review",
                amount=f"৳{bdt_amount:.2f} BDT", order_id=tx_id, txn_id=txid_raw,
                note="⚠️ Amount mismatch detected — our team has been notified and "
                     "will review your payment shortly.",
            )
        elif is_amount_mismatch:
            user_msg = (
                f"❌ Amount mismatch.\n\nThe transaction amount does not match the expected "
                f"৳{bdt_amount:.2f} BDT. Please ensure you sent the correct amount."
            )
        elif pmv_id:
            user_msg = pui.user_payment_card(
                gateway_key="zinipay", stage="pending_review",
                amount=f"৳{bdt_amount:.2f} BDT", order_id=tx_id, txn_id=txid_raw,
                note="Our team has been notified and will review your transaction shortly. "
                     "You'll be notified the moment your balance is updated.",
            )
        elif "already used" in lower_err or "duplicate" in lower_err:
            user_msg = "❌ This Transaction ID has already been used."
        elif "invalid" in lower_err:
            user_msg = "❌ Invalid Transaction ID. Please check and try again."
        elif "expired" in lower_err:
            user_msg = "❌ This transaction has expired or is no longer valid."
        elif "disabled" in lower_err or "api disabled" in lower_err:
            user_msg = "⚠️ Payment verification is temporarily unavailable. Please try again shortly."
        elif "insufficient" in lower_err or "credits" in lower_err:
            user_msg = "⚠️ Payment verification is temporarily unavailable. Please try again shortly."
        else:
            user_msg = "❌ Transaction could not be verified.\n\nPlease check your TXID and try again."

        try:
            await processing_msg.edit_text(user_msg, parse_mode='HTML')
            if pmv_id:
                pui.remember_pending_message(pmv_id, processing_msg.chat_id, processing_msg.message_id)
        except Exception:
            sent = await update.message.reply_text(user_msg, parse_mode='HTML')
            if pmv_id:
                pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
        return ZINIPAY_TXID

    # ---- Step 2: Confirm ----
    confirmed = await asyncio.to_thread(
        svc.confirm_transaction,
        verify_result.trx_id,
        bdt_amount,   # Must match the amount sent in verify.
        verify_result.verify_id,
    )

    if not confirmed:
        error_detail = svc.last_error or "Unknown error"
        user_msg = (
            "⚠️ Payment verified but confirmation failed. "
            "Please contact support with your Transaction ID: "
            f"<code>{txid_raw}</code> and Deposit ID: <code>{pui.format_deposit_id(tx_id)}</code>."
        )
        try:
            await processing_msg.edit_text(user_msg, parse_mode='HTML')
        except Exception:
            await update.message.reply_text(user_msg, parse_mode='HTML')
        logger.error(
            "ZiniPay confirm failed for tx=%s txid=%s trxID=%s error=%s",
            tx_id, txid_raw, verify_result.trx_id, error_detail,
        )
        return ConversationHandler.END

    # ---- Step 3 + 4: Record trxID (replay guard) + credit wallet atomically ----
    new_balance = 0.0
    with get_db_session() as session:
        # Atomic status flip — idempotent guard against double-credit.
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update(
            {Transaction.status: TransactionStatus.COMPLETED,
             Transaction.completed_at: datetime.utcnow()},
            synchronize_session=False,
        )
        if flipped == 0:
            try:
                await processing_msg.edit_text("❌ This order is no longer pending.")
            except Exception:
                pass
            context.user_data.pop('zinipay_tx_id', None)
            return ConversationHandler.END

        # Record the trxID to prevent replay attacks.
        used_txn = ZiniPayUsedTransaction(
            trx_id=verify_result.trx_id,
            verify_id=verify_result.verify_id,
            telegram_user_id=telegram_id,
            internal_order_id=tx_id,
            provider=verify_result.provider,
            sender=verify_result.sender,
            amount=usd_amount,
        )
        session.add(used_txn)
        try:
            session.flush()
        except Exception:
            # UNIQUE violation: another concurrent request claimed this trxID.
            session.rollback()
            # Roll back the COMPLETED flip too — re-mark as PENDING so the
            # user can try again (though the trxID won't work again).
            session.query(Transaction).filter(Transaction.id == tx_id).update(
                {Transaction.status: TransactionStatus.PENDING},
                synchronize_session=False,
            )
            session.commit()
            try:
                await processing_msg.edit_text(
                    "❌ This Transaction ID has already been used. "
                    "Please contact support if you believe this is an error."
                )
            except Exception:
                pass
            return ConversationHandler.END

        # Atomic wallet credit — writes WalletLedger row in same session.
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            from services.wallet import credit_locked, WalletError
            try:
                new_balance = credit_locked(
                    session, user.id, usd_amount,
                    reason=f"ZiniPay top-up #{tx_id}",
                    actor_type="system", ref_type="zinipay",
                    ref_id=verify_result.trx_id,
                )
            except WalletError:
                logger.exception("ZiniPay credit_locked failed for tx %s", tx_id)
                session.rollback()
                try:
                    await processing_msg.edit_text(
                        "⚠️ Payment verified but crediting your balance failed. "
                        f"Please contact support with Transaction ID: "
                        f"<code>{txid_raw}</code> and Deposit ID: <code>{pui.format_deposit_id(tx_id)}</code>.",
                        parse_mode='HTML',
                    )
                except Exception:
                    pass
                context.user_data.pop('zinipay_tx_id', None)
                return ConversationHandler.END
            # V19 — deposit receipt + activity log (best-effort)
            try:
                from handlers.account_features import create_receipt_record, log_activity
                create_receipt_record(
                    order_id=None, transaction_id=tx_id,
                    user_id_db=user.id, receipt_type="deposit",
                )
                log_activity(
                    user_id_db=user.id, action="deposit", status="success",
                    details=f"${usd_amount:.2f} deposited via ZiniPay",
                    ref_type="transaction", ref_id=str(tx_id),
                )
            except Exception:
                pass
        # The get_db_session() context manager commits on clean exit.

    success_text = pui.deposit_success_card(
        amount=f"${usd_amount:.2f} USD",
        payment_method="bKash • Nagad • Rocket",
        deposit_id=pui.format_deposit_id(tx_id),
    )
    try:
        await processing_msg.edit_text(
            success_text, reply_markup=pui.deposit_success_keyboard(), parse_mode='HTML',
        )
    except Exception:
        await update.message.reply_text(
            success_text, reply_markup=pui.deposit_success_keyboard(), parse_mode='HTML',
        )

    logger.info(
        "ZiniPay payment confirmed: tx=%s txid=%s trxID=%s provider=%s sender=%s amount=%.2f",
        tx_id, txid_raw, verify_result.trx_id,
        verify_result.provider, verify_result.sender, usd_amount,
    )
    context.user_data.pop('zinipay_tx_id', None)
    return ConversationHandler.END


# ==================== BINANCE PAY FLOW ====================
# See services/binance_pay.py for the verification logic itself. This block
# only handles the Telegram-facing order creation / TXID submission UX.

def _binance_currency_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    svc = BinancePayService()
    row = [
        InlineKeyboardButton(c, callback_data=f"binance_currency:{tx_id}:{c}")
        for c in svc.allowed_currencies
    ]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])


async def _finish_binance_payment(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   usd_amount: float, currency: str = None):
    """Create the internal order for a Binance Pay top-up and show the
    payment screen (Pay ID + amount + 'Submit Transaction ID' button).
    No hosted checkout link exists for Binance Pay — the user pays manually
    from their own Binance app and reports the transaction ID afterwards."""
    telegram_id = update.effective_user.id
    svc = BinancePayService()

    if not svc.enabled or not svc.is_configured() or not svc.pay_id:
        await update.message.reply_text(
            "❌ Binance Pay is not available right now. Please choose another method or contact support."
        )
        return ConversationHandler.END

    if currency is None:
        if len(svc.allowed_currencies) > 1:
            # Ask which currency the user will send — need to create the
            # order first so we have a stable id for the callback, but keep
            # it PENDING with no currency committed until chosen.
            with get_db_session() as session:
                user = session.query(User).filter_by(telegram_id=telegram_id).first()
                if not user:
                    await update.message.reply_text("❌ User not found.")
                    return ConversationHandler.END
                # Expired orders must never block a new one — reconcile first.
                _auto_cancel_expired_pending(session, user.id, PaymentMethod.BINANCE_PAY)

                existing_pending = session.query(Transaction).filter_by(
                    user_id=user.id, payment_method=PaymentMethod.BINANCE_PAY, status=TransactionStatus.PENDING,
                ).first()
                if existing_pending:
                    await update.message.reply_text(
                        "⚠️ You already have a pending Binance Pay deposit "
                        f"({pui.format_deposit_id(existing_pending.id, existing_pending.created_at)}). "
                        f"Please complete or cancel it before starting a new one.",
                        reply_markup=create_cancel_keyboard(),
                    )
                    return ConversationHandler.END
                transaction = Transaction(
                    user_id=user.id, amount=usd_amount, payment_method=PaymentMethod.BINANCE_PAY,
                    status=TransactionStatus.PENDING,
                    expires_at=calculate_expiry_time(svc.order_expiry_minutes / 60.0),
                )
                session.add(transaction)
                session.commit()
                session.refresh(transaction)
                tx_id = transaction.id
            await update.message.reply_text(
                f"🟡 Binance Pay selected.\n\n💬 Which currency will you send for ${usd_amount:.2f}?",
                reply_markup=_binance_currency_keyboard(tx_id),
            )
            return METHOD
        currency = svc.allowed_currencies[0] if svc.allowed_currencies else "USDT"

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        # Expired orders must never block a new one — reconcile first.
        _auto_cancel_expired_pending(session, user.id, PaymentMethod.BINANCE_PAY)

        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id, payment_method=PaymentMethod.BINANCE_PAY, status=TransactionStatus.PENDING,
        ).first()
        if existing_pending:
            await update.message.reply_text(
                "⚠️ You already have a pending Binance Pay deposit "
                f"({pui.format_deposit_id(existing_pending.id, existing_pending.created_at)}). "
                f"Please complete or cancel it before starting a new one.",
                reply_markup=create_cancel_keyboard(),
            )
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.BINANCE_PAY,
            crypto_address=currency,  # reused column: stores the chosen currency (USDT/USDC)
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(svc.order_expiry_minutes / 60.0),
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        tx_id = transaction.id

    await _send_binance_payment_screen(update, context, tx_id, usd_amount, currency, svc, is_new_message=True)
    return ConversationHandler.END


async def binance_currency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked USDT/USDC for a Binance Pay order created without a currency yet."""
    query = update.callback_query
    await query.answer()
    try:
        _, tx_id_s, currency = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await query.answer("Invalid selection", show_alert=True)
        return ConversationHandler.END

    svc = BinancePayService()
    if currency not in svc.allowed_currencies:
        await query.answer("Unsupported currency", show_alert=True)
        return METHOD

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(
            id=tx_id, payment_method=PaymentMethod.BINANCE_PAY, status=TransactionStatus.PENDING,
        ).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            try:
                await query.edit_message_text("❌ Order not found or already handled.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        tx.crypto_address = currency
        usd_amount = tx.amount
        session.commit()

    await _send_binance_payment_screen(update, context, tx_id, usd_amount, currency, svc, is_new_message=False)
    return ConversationHandler.END


async def _send_binance_payment_screen(update, context, tx_id: int, usd_amount: float,
                                        currency: str, svc: "BinancePayService", is_new_message: bool):
    instructions = svc.instructions.strip() if svc.instructions else (
        "1. Open Binance App → Pay → Send\n"
        "2. Enter the Binance Pay ID shown above\n"
        f"3. Send the exact {currency} amount\n"
        "4. Open the completed Binance Pay transaction\n"
        "5. Copy the Binance Pay Transaction ID / Order ID\n"
        "6. Return to the bot\n"
        "7. Tap \"Submit Transaction ID\"\n"
        "8. Paste the transaction ID"
    )
    message = pui.user_payment_card(
        gateway_key="binance_pay",
        stage="waiting",
        amount=f"{usd_amount:.2f} {currency}",
        order_id=tx_id,
        extra=[
            ("🆔", "Pay ID", f"<code>{svc.pay_id}</code>"),
            ("⏱", "Expires in", f"{svc.order_expiry_minutes} minutes"),
        ],
        note=f"📋 <b>Steps:</b>\n{instructions}",
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Submit Transaction ID", callback_data=f"binance_submit:{tx_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    if is_new_message:
        await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def binance_submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the 'Submit Transaction ID' button — a standalone
    mini-conversation, independent of the (already-ended) top-up conversation."""
    query = update.callback_query
    await query.answer()
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order", show_alert=True)
        return ConversationHandler.END

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            await query.answer("⛔ Not your order.", show_alert=True)
            return ConversationHandler.END
        if tx.payment_method != PaymentMethod.BINANCE_PAY:
            await query.answer("Invalid order.", show_alert=True)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await query.answer("This order is no longer pending.", show_alert=True)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await query.answer("⏰ This order has expired.", show_alert=True)
            return ConversationHandler.END

    context.user_data['binance_tx_id'] = tx_id
    await query.message.reply_text(
        "🧾 Please paste your Binance Pay Transaction ID (or Order ID) below.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="binance_cancel_submit")]]),
    )
    return BINANCE_TXID


async def binance_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('binance_tx_id', None)
    try:
        await query.edit_message_text("❌ Cancelled. Your order is still pending — you can submit the Transaction ID again anytime before it expires.")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END


async def binance_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify a submitted Binance Pay transaction ID against Binance's own
    transaction history (services/binance_pay.py) and, only if it checks
    out, atomically credit the wallet exactly once."""
    telegram_id = update.effective_user.id
    txid_raw = (update.message.text or "").strip()
    tx_id = context.user_data.get('binance_tx_id')

    if not tx_id:
        await update.message.reply_text("❌ Session expired. Please start again from your pending order.")
        return ConversationHandler.END

    if not is_valid_txid_format(txid_raw):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Transaction ID. Please paste the exact "
            "Transaction ID / Order ID from your completed Binance Pay payment."
        )
        return BINANCE_TXID

    if is_rate_limited(telegram_id):
        await update.message.reply_text(
            "⚠️ Too many verification attempts. Please wait a minute and try again."
        )
        return BINANCE_TXID

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            context.user_data.pop('binance_tx_id', None)
            return ConversationHandler.END
        tx = session.query(Transaction).filter_by(id=tx_id, user_id=user.id).first()
        if not tx or tx.payment_method != PaymentMethod.BINANCE_PAY:
            await update.message.reply_text("❌ Order not found.")
            context.user_data.pop('binance_tx_id', None)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await update.message.reply_text("❌ This order is no longer pending.")
            context.user_data.pop('binance_tx_id', None)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await update.message.reply_text("⏰ This order has expired. Please start a new top-up.")
            context.user_data.pop('binance_tx_id', None)
            return ConversationHandler.END

        # Fast pre-check (defense in depth — the UNIQUE constraint at insert
        # time below is the real, race-proof guarantee).
        already = session.query(BinancePayTransaction).filter_by(transaction_id=txid_raw).first()
        if already:
            await update.message.reply_text("❌ This transaction has already been used.")
            return BINANCE_TXID

        order_created_at = tx.created_at
        expected_amount = _to_decimal_amount(tx.amount)
        currency = tx.crypto_address or "USDT"
        user_id = tx.user_id

    # Prevent two concurrent submissions for the SAME order from both
    # racing the (slow) Binance API call in parallel.
    lock = get_order_lock(telegram_id, tx_id)
    if not lock.acquire(blocking=False):
        await update.message.reply_text("⏳ Your previous submission for this order is still being verified — please wait.")
        return BINANCE_TXID

    try:
        svc = BinancePayService()
        result = await asyncio.to_thread(
            svc.verify_transaction,
            transaction_id=txid_raw, expected_amount=expected_amount,
            currency=currency, order_created_at=order_created_at,
        )
    finally:
        lock.release()

    # ---- Outcomes that are clear, pre-API-call user errors — return inline,
    # no admin notification (nothing for an admin to review yet). ----
    if result.outcome == VerificationOutcome.NOT_CONFIGURED:
        await update.message.reply_text("⚠️ Binance verification is temporarily unavailable.\n\nPlease try again shortly.")
        return BINANCE_TXID

    # ---- Every outcome that actually reached the Binance API but wasn't a
    # clean SUCCESS warrants admin review — never silently ask the user to
    # "just retry" once real transaction data has been inspected. Only a
    # pure client-side format check (handled above via is_valid_txid_format)
    # and NOT_CONFIGURED (no API call made at all) skip admin review. ----
    _BINANCE_ADMIN_NOTIFY_OUTCOMES = {
        VerificationOutcome.API_ERROR,
        VerificationOutcome.NOT_FOUND,
        VerificationOutcome.TOO_OLD,
        VerificationOutcome.AMOUNT_MISMATCH,
        VerificationOutcome.WRONG_DIRECTION,
        VerificationOutcome.CURRENCY_MISMATCH,
    }
    if result.outcome in _BINANCE_ADMIN_NOTIFY_OUTCOMES or (result.outcome != VerificationOutcome.SUCCESS):
        outcome_str = result.outcome.name if hasattr(result.outcome, 'name') else str(result.outcome)
        detail_str = (
            f"expected {expected_amount} {currency}, "
            f"received {result.received_amount} {result.currency or currency}"
            if result.outcome == VerificationOutcome.AMOUNT_MISMATCH
            else str(getattr(result, 'detail', '') or '')
        )

        # Persist the attempt log
        try:
            with get_db_session() as _sess:
                _sess.add(VerificationAttemptLog(
                    gateway="binance_pay",
                    telegram_user_id=telegram_id,
                    internal_order_id=tx_id,
                    submitted_txid=txid_raw,
                    outcome=outcome_str,
                    detail=detail_str[:500] if detail_str else None,
                ))
                _sess.commit()
        except Exception:
            logger.exception("Failed to write VerificationAttemptLog (binance)")

        # Queue for admin review (suppress duplicate queues for same txid/order)
        pmv_id = None
        if result.outcome in _BINANCE_ADMIN_NOTIFY_OUTCOMES:
            try:
                with get_db_session() as _sess:
                    existing_pmv = _sess.query(PendingManualVerification).filter_by(
                        gateway="binance_pay",
                        internal_order_id=tx_id,
                        submitted_txid=txid_raw,
                    ).first()
                    if existing_pmv:
                        pmv_id = existing_pmv.id
                    else:
                        pmv = PendingManualVerification(
                            gateway="binance_pay",
                            telegram_user_id=telegram_id,
                            internal_order_id=tx_id,
                            submitted_txid=txid_raw,
                            amount=expected_amount,
                            currency=currency,
                            auto_outcome=outcome_str,
                            auto_detail=detail_str[:500] if detail_str else None,
                            status="pending",
                        )
                        _sess.add(pmv)
                        _sess.commit()
                        _sess.refresh(pmv)
                        pmv_id = pmv.id
            except Exception:
                logger.exception("Failed to create PendingManualVerification (binance)")

            # Notify every admin with manage_payments (plus the owner), each
            # with the full set of action buttons. Per-order dedup: only the
            # attempt that flips review_notified False→True actually sends,
            # so resubmitting a TXID for the same order never re-alerts.
            if pmv_id is not None:
                review_claimed = False
                try:
                    with get_db_session() as _rsess:
                        review_claimed = _rsess.query(Transaction).filter(
                            Transaction.id == tx_id,
                            Transaction.review_notified.is_(False),
                        ).update(
                            {Transaction.review_notified: True},
                            synchronize_session=False,
                        ) == 1
                        _rsess.commit()
                except Exception:
                    logger.exception("Failed to claim review_notified for tx %s (binance)", tx_id)
                    review_claimed = False

            if pmv_id is not None and review_claimed:
                try:
                    reason_map = {
                        VerificationOutcome.API_ERROR: "API error / timeout — could not reach Binance",
                        VerificationOutcome.NOT_FOUND: "Payment not found in Binance account history",
                        VerificationOutcome.TOO_OLD: "Transaction too old — outside search window",
                        VerificationOutcome.AMOUNT_MISMATCH: f"Wrong amount — expected {expected_amount} {currency}, received {result.received_amount} {result.currency or currency}",
                        VerificationOutcome.WRONG_DIRECTION: "Matching transaction found but it was outgoing (SEND), not a received payment",
                        VerificationOutcome.CURRENCY_MISMATCH: f"Wrong currency — expected {currency}, received {result.currency or 'unknown'}",
                    }
                    reason = reason_map.get(result.outcome, f"Verification failed ({outcome_str})")
                    _uname = update.effective_user.username
                    _uname_display = f"@{_uname}" if _uname else f"(no username)"
                    for admin_id in _gateway_admin_recipient_ids():
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="binance_pay",
                                    amount=f"{expected_amount} {currency}",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    customer_name=_uname_display,
                                    user_id=telegram_id,
                                    status_key="pending_review",
                                    note=f"⚠️ <b>Auto-verify failed:</b> {reason}",
                                ),
                                reply_markup=pui.admin_review_keyboard(
                                    verify_cb=f"admin_binance_verify_{tx_id}_{pmv_id}",
                                    approve_cb=f"admin_binance_approve_{tx_id}_{pmv_id}",
                                    reject_cb=f"admin_binance_reject_start_{tx_id}_{pmv_id}",
                                    view_user_cb=f"admin_view_user_pmv_{telegram_id}",
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            logger.exception("Failed to notify admin %s for Binance manual verification", admin_id)
                except Exception:
                    logger.exception("Failed to send admin notification(s) for Binance manual verification")

        # User-facing message
        if result.outcome == VerificationOutcome.AMOUNT_MISMATCH:
            if pmv_id:
                sent = await update.message.reply_text(
                    pui.user_payment_card(
                        gateway_key="binance_pay", stage="pending_review",
                        amount=f"{expected_amount} {currency}", order_id=tx_id, txn_id=txid_raw,
                        extra=[("📥", "Received", f"{result.received_amount} {result.currency or currency}")],
                        note="⚠️ Amount mismatch detected — our team has been notified and "
                             "will review your payment shortly.",
                    ),
                    parse_mode='HTML',
                )
                pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
            else:
                await update.message.reply_text(
                    "❌ Payment amount mismatch.\n\n"
                    f"Expected: {expected_amount} {currency}\n"
                    f"Received: {result.received_amount} {result.currency or currency}"
                )
        elif pmv_id:
            sent = await update.message.reply_text(
                pui.user_payment_card(
                    gateway_key="binance_pay", stage="pending_review",
                    amount=f"{expected_amount} {currency}", order_id=tx_id, txn_id=txid_raw,
                    note="Our team has been notified and will review your transaction shortly. "
                         "You'll be notified the moment your balance is updated.",
                ),
                parse_mode='HTML',
            )
            pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
        else:
            await update.message.reply_text("❌ Transaction could not be verified.\n\nPlease check the Transaction ID and try again.")
        return BINANCE_TXID

    # ---- Verified — log the successful attempt, then credit the wallet
    # exactly once, atomically. ----
    try:
        with get_db_session() as _sess:
            _sess.add(VerificationAttemptLog(
                gateway="binance_pay",
                telegram_user_id=telegram_id,
                internal_order_id=tx_id,
                submitted_txid=txid_raw,
                outcome="SUCCESS",
                detail=f"received {result.received_amount} {result.currency or currency}"[:500],
            ))
            _sess.commit()
    except Exception:
        logger.exception("Failed to write VerificationAttemptLog (binance success)")

    import json as _json
    from services.wallet import credit_locked, WalletError

    credited_usd = 0.0
    bonus_amount = 0.0
    new_balance = 0.0
    dup = False
    with get_db_session() as session:
        # Re-check PENDING under this transaction (closes the race between
        # the read above and now).
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update({Transaction.status: TransactionStatus.COMPLETED, Transaction.completed_at: datetime.utcnow()},
                 synchronize_session=False)
        if flipped == 0:
            await update.message.reply_text("❌ This order is no longer pending.")
            context.user_data.pop('binance_tx_id', None)
            return ConversationHandler.END

        raw_json = None
        try:
            raw_json = _json.dumps(result.matched_record or {})[:8000]
        except Exception:
            raw_json = None

        bpt = BinancePayTransaction(
            transaction_id=txid_raw,
            binance_order_id=result.binance_order_id,
            telegram_user_id=telegram_id,
            internal_order_id=tx_id,
            currency=result.currency or currency,
            expected_amount=expected_amount,
            received_amount=result.received_amount,
            transaction_time=(datetime.utcfromtimestamp(result.transaction_time / 1000)
                               if result.transaction_time else None),
            raw_transaction_data=raw_json,
        )
        session.add(bpt)
        try:
            session.flush()
        except IntegrityError:
            # Another concurrent request won the race — this txid was just
            # claimed by someone else. Roll back our COMPLETED flip too.
            session.rollback()
            dup = True
        else:
            bonus_percent = BinancePayService().bonus_percent
            base_usd = float(expected_amount)
            bonus_amount = round(base_usd * (bonus_percent / 100.0), 2) if bonus_percent else 0.0
            credited_usd = base_usd + bonus_amount
            try:
                new_balance = credit_locked(
                    session, user_id, credited_usd,
                    reason=f"Binance Pay top-up #{tx_id}", actor_type="system",
                    ref_type="binance_pay", ref_id=str(tx_id),
                )
            except WalletError:
                logger.exception("Binance Pay wallet credit failed for tx %s", tx_id)
                session.rollback()
                await update.message.reply_text(
                    "⚠️ Verification succeeded but crediting your balance failed. Please contact support with your Deposit ID: %s" % pui.format_deposit_id(tx_id)
                )
                context.user_data.pop('binance_tx_id', None)
                return ConversationHandler.END
            session.commit()
            # V19 — deposit receipt + activity log (best-effort)
            try:
                from handlers.account_features import create_receipt_record, log_activity
                create_receipt_record(
                    order_id=None, transaction_id=tx_id,
                    user_id_db=user_id, receipt_type="deposit",
                )
                log_activity(
                    user_id_db=user_id, action="deposit", status="success",
                    details=f"${credited_usd:.2f} deposited via Binance Pay",
                    ref_type="transaction", ref_id=str(tx_id),
                )
            except Exception:
                pass

    context.user_data.pop('binance_tx_id', None)

    if dup:
        await update.message.reply_text("❌ This transaction has already been used.")
        return ConversationHandler.END

    _bonus_str = f"+{bonus_amount:.2f} USD" if bonus_amount else None
    await update.message.reply_text(
        pui.deposit_success_card(
            amount=f"${credited_usd:.2f} USD",
            payment_method="Binance Pay",
            deposit_id=pui.format_deposit_id(tx_id),
            bonus_line=_bonus_str,
        ),
        reply_markup=pui.deposit_success_keyboard(),
        parse_mode='HTML',
    )
    return ConversationHandler.END


# ==================== BYBIT PAY FLOW ====================
# See services/bybit_pay.py for the verification logic itself. This block
# only handles the Telegram-facing order creation / type+network selection /
# TXID submission UX. Bybit Pay is USDT-only (matches the spec).

BYBIT_CURRENCY = "USDT"


def _bybit_meta(payment_type: str, network: str = "-") -> str:
    """Pack (payment_type, network) into the reused `crypto_address` column,
    the same convention services/binance_pay.py's flow uses for currency."""
    return f"bybit:{payment_type}:{network or '-'}:{BYBIT_CURRENCY}"


def _parse_bybit_meta(crypto_address: str):
    """Returns (payment_type, network) from a packed `crypto_address` value,
    or (None, None) if it isn't a Bybit meta string."""
    if not crypto_address or not crypto_address.startswith("bybit:"):
        return None, None
    parts = crypto_address.split(":")
    if len(parts) < 3:
        return None, None
    payment_type = parts[1]
    network = parts[2] if parts[2] != "-" else None
    return payment_type, network


def _bybit_type_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 UID Transfer", callback_data=f"bybit_type:{tx_id}:uid")],
        [InlineKeyboardButton("🔹 On-chain Deposit", callback_data=f"bybit_type:{tx_id}:onchain")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def _bybit_network_keyboard(tx_id: int, svc: "BybitPayService") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(net, callback_data=f"bybit_network:{tx_id}:{net}")]
        for net in svc.networks_with_wallets()
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"bybit_back_type:{tx_id}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def _finish_bybit_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float):
    """Create the internal order for a Bybit Pay (UID Transfer) top-up and show
    the UID payment screen directly.
    On-chain networks (TRC20/BEP20/ERC20) are now direct main-menu entries
    handled by _finish_bybit_onchain_direct / payment_method_bybit_trc20 etc."""
    telegram_id = update.effective_user.id
    svc = BybitPayService()

    if not svc.enabled or not svc.is_configured() or not svc.uid:
        await update.message.reply_text(
            "❌ Bybit Pay (UID Transfer) is not available right now. Please choose another method or contact support."
        )
        return ConversationHandler.END

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        # Expired orders must never block a new one — reconcile first.
        _auto_cancel_expired_pending(session, user.id, PaymentMethod.BYBIT_PAY)

        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
        ).first()
        if existing_pending:
            await update.message.reply_text(
                "⚠️ You already have a pending Bybit Pay deposit "
                f"({pui.format_deposit_id(existing_pending.id, existing_pending.created_at)}). "
                f"Please complete or cancel it before starting a new one.",
                reply_markup=create_cancel_keyboard(),
            )
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.BYBIT_PAY,
            crypto_address=_bybit_meta("uid_transfer"),
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(svc.order_expiry_minutes / 60.0),
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        tx_id = transaction.id

    await _send_bybit_uid_screen(update, context, tx_id, usd_amount, svc, is_new_message=True)
    return ConversationHandler.END


async def _finish_bybit_onchain_direct(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                        usd_amount: float, network: str):
    """Create a Bybit Pay on-chain deposit order for a specific network and
    show the deposit address screen directly — no type/network sub-menu.
    Called from payment_method_bybit_trc20 / bep20 / erc20 / ltc entry points.
    Uses the same order, verification, and TXID flow as the former on-chain
    Deposit sub-menu so all existing Bybit API logic is reused exactly.

    For non-stablecoin networks (LTC), a live exchange rate is fetched and
    locked into the order so the required crypto amount is fixed at creation
    time and never recalculated."""
    telegram_id = update.effective_user.id
    svc = BybitPayService()
    network = network.strip().upper()

    if not svc.enabled or not svc.is_configured():
        await update.message.reply_text(
            "❌ USDT deposits are not available right now. Please choose another method or contact support."
        )
        return ConversationHandler.END

    address = svc.wallet_for_network(network)
    if not address:
        await update.message.reply_text(
            f"❌ {network} deposits are not configured right now. Please choose another method.",
            reply_markup=create_cancel_keyboard(),
        )
        return ConversationHandler.END

    # For non-stablecoin networks (LTC), fetch the live rate and lock it.
    locked_rate: Optional[float] = None
    locked_crypto_amount: Optional[float] = None
    if network == "LTC":
        try:
            ltc_rate_val = await asyncio.to_thread(_ltc_rate_svc.get_ltc_usd_rate)
            locked_rate = float(ltc_rate_val)
            locked_crypto_amount = round(usd_amount / locked_rate, 8)
        except Exception as _rate_err:
            logger.warning("LTC/USD rate fetch failed: %s", _rate_err)
            await update.message.reply_text(
                "❌ Could not fetch the current LTC exchange rate. Please try again in a moment.",
                reply_markup=create_cancel_keyboard(),
            )
            return ConversationHandler.END

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        # Expired orders must never block a new one — reconcile first.
        _auto_cancel_expired_pending(session, user.id, PaymentMethod.BYBIT_PAY)

        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
        ).first()
        if existing_pending:
            await update.message.reply_text(
                "⚠️ You already have a pending deposit "
                f"({pui.format_deposit_id(existing_pending.id, existing_pending.created_at)}). "
                f"Please complete or cancel it before starting a new one.",
                reply_markup=create_cancel_keyboard(),
            )
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.BYBIT_PAY,
            crypto_address=_bybit_meta("onchain", network),
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(svc.order_expiry_minutes / 60.0),
            locked_crypto_rate=locked_rate,
            locked_crypto_amount=locked_crypto_amount,
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        tx_id = transaction.id

    await _send_bybit_onchain_screen(
        update, context, tx_id, usd_amount, network, svc,
        is_new_message=True,
        locked_rate=locked_rate,
        locked_crypto_amount=locked_crypto_amount,
    )
    return ConversationHandler.END


async def _set_bybit_type(tx_id: int, payment_type: str, network: str = "-"):
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if tx:
            tx.crypto_address = _bybit_meta(payment_type, network)
            session.commit()


async def bybit_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked UID Transfer or On-chain Deposit for a pending Bybit Pay order."""
    query = update.callback_query
    await query.answer()
    try:
        _, tx_id_s, choice = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await query.answer("Invalid selection", show_alert=True)
        return ConversationHandler.END

    svc = BybitPayService()
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(
            id=tx_id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
        ).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            try:
                await query.edit_message_text("❌ Order not found or already handled.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        usd_amount = tx.amount
        tx_created_at = tx.created_at

    if choice == "uid":
        if not svc.uid:
            await query.answer("UID Transfer is not available right now.", show_alert=True)
            return METHOD
        await _set_bybit_type(tx_id, "uid_transfer")
        await _send_bybit_uid_screen(update, context, tx_id, usd_amount, svc, is_new_message=False)
        return ConversationHandler.END

    if choice == "onchain":
        if not svc.networks_with_wallets():
            await query.answer("On-chain Deposit is not available right now.", show_alert=True)
            return METHOD
        try:
            await query.edit_message_text(
                pui.build_card(
                    title="Bybit Payment",
                    title_emoji="💙",
                    fields=[
                        ("🧾", "Deposit ID", pui.format_deposit_id(tx_id, tx_created_at)),
                        ("💰", "Amount", pui.copy_code(f"{usd_amount:.2f} {BYBIT_CURRENCY}")),
                    ],
                    note="Choose network:",
                ),
                reply_markup=_bybit_network_keyboard(tx_id, svc),
                parse_mode='HTML',
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return METHOD

    await query.answer("Invalid selection", show_alert=True)
    return METHOD


async def bybit_back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'⬅️ Back' from the network list back to the UID/On-chain choice."""
    query = update.callback_query
    await query.answer()
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(
            id=tx_id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
        ).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            return ConversationHandler.END
        usd_amount = tx.amount
        tx_created_at = tx.created_at
    try:
        await query.edit_message_text(
            pui.build_card(
                title="Bybit Payment",
                title_emoji="💙",
                fields=[
                    ("🧾", "Deposit ID", pui.format_deposit_id(tx_id, tx_created_at)),
                    ("💰", "Amount", pui.copy_code(f"{usd_amount:.2f} {BYBIT_CURRENCY}")),
                ],
                note="Choose payment type:",
            ),
            reply_markup=_bybit_type_keyboard(tx_id),
            parse_mode='HTML',
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD


async def bybit_network_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked TRC20 / BEP20 / ERC20 for a Bybit on-chain deposit order."""
    query = update.callback_query
    await query.answer()
    try:
        _, tx_id_s, network = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await query.answer("Invalid selection", show_alert=True)
        return ConversationHandler.END

    svc = BybitPayService()
    network = network.strip().upper()
    if network not in svc.networks_with_wallets():
        await query.answer("Unsupported or unavailable network", show_alert=True)
        return METHOD

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(
            id=tx_id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
        ).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            try:
                await query.edit_message_text("❌ Order not found or already handled.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        tx.crypto_address = _bybit_meta("onchain", network)
        usd_amount = tx.amount
        session.commit()

    await _send_bybit_onchain_screen(update, context, tx_id, usd_amount, network, svc, is_new_message=False)
    return ConversationHandler.END


async def _send_bybit_uid_screen(update, context, tx_id: int, usd_amount: float,
                                  svc: "BybitPayService", is_new_message: bool):
    instructions = svc.instructions.strip() if svc.instructions else (
        "1. Open Bybit App → Assets → Transfer\n"
        "2. Select \"UID Transfer\"\n"
        f"3. Enter the UID shown above and send exactly {usd_amount:.2f} {BYBIT_CURRENCY}\n"
        "4. Open the completed transfer in your Bybit history\n"
        "5. Copy the Transaction ID\n"
        "6. Return to the bot\n"
        "7. Tap \"Submit Transaction ID\"\n"
        "8. Paste the Transaction ID"
    )
    message = pui.user_payment_card(
        gateway_key="bybit_pay",
        stage="waiting",
        amount=f"{usd_amount:.2f} {BYBIT_CURRENCY}",
        order_id=tx_id,
        extra=[
            ("🆔", "UID", f"<code>{svc.uid}</code>"),
            ("⏱", "Expires in", f"{svc.order_expiry_minutes} minutes"),
        ],
        note=f"📋 <b>Steps:</b>\n{instructions}",
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Submit Transaction ID", callback_data=f"bybit_submit:{tx_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    if is_new_message:
        await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def _send_bybit_onchain_screen(update, context, tx_id: int, usd_amount: float, network: str,
                                      svc: "BybitPayService", is_new_message: bool,
                                      *, locked_rate: Optional[float] = None,
                                      locked_crypto_amount: Optional[float] = None):
    address = svc.wallet_for_network(network)
    _NETWORK_LABELS = {"LTC": "Litecoin (LTC)"}
    if locked_crypto_amount is not None and locked_rate is not None:
        # Non-stablecoin order (e.g. LTC) — show locked rate and exact crypto amount.
        net_label = _NETWORK_LABELS.get(network, network)
        message = pui.user_payment_card(
            gateway_key="bybit_pay",
            gateway_label_override=f"Bybit Pay — {net_label}",
            stage="waiting",
            amount=f"${usd_amount:.2f} USD",
            order_id=tx_id,
            extra=[
                ("💱", "Rate", f"1 LTC = ${locked_rate:.2f} USD"),
                ("📤", "Send exactly", f"<code>{locked_crypto_amount:.8f} LTC</code>"),
                ("🌐", "Network", net_label),
                ("📮", "Deposit Address", f"<code>{address}</code>"),
                ("⏱", "Rate locked for", f"{svc.order_expiry_minutes} minutes"),
            ],
            note=f"⚠️ Send exactly <b>{locked_crypto_amount:.8f} LTC</b> to the address above using the "
                 f"<b>Litecoin</b> network. Sending any other amount, asset, or network may delay or "
                 f"prevent automatic verification.\n\nAfter sending, copy the blockchain Transaction ID "
                 f"(TXID) and tap \"Submit Transaction ID\" below.",
        )
    else:
        message = pui.user_payment_card(
            gateway_key="bybit_pay",
            gateway_label_override=f"Bybit Pay — USDT ({network})",
            stage="waiting",
            amount=f"{usd_amount:.2f} {BYBIT_CURRENCY}",
            order_id=tx_id,
            extra=[
                ("🌐", "Network", network),
                ("📮", "Deposit Address", f"<code>{address}</code>"),
                ("⏱", "Expires in", f"{svc.order_expiry_minutes} minutes"),
            ],
            note=f"⚠️ Send only <b>{BYBIT_CURRENCY}</b> using <b>{network}</b>. Sending any other "
                 f"asset or using a different network may result in permanent loss of funds.\n\n"
                 f"After sending, copy the blockchain Transaction ID (TXID) and tap "
                 f"\"Submit Transaction ID\" below.",
        )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Submit Transaction ID", callback_data=f"bybit_submit:{tx_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    if is_new_message:
        await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def bybit_submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the 'Submit Transaction ID' button — a standalone
    mini-conversation, independent of the (already-ended) top-up conversation."""
    query = update.callback_query
    await query.answer()
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order", show_alert=True)
        return ConversationHandler.END

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx or tx.user.telegram_id != update.effective_user.id:
            await query.answer("⛔ Not your order.", show_alert=True)
            return ConversationHandler.END
        if tx.payment_method != PaymentMethod.BYBIT_PAY:
            await query.answer("Invalid order.", show_alert=True)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await query.answer("This order is no longer pending.", show_alert=True)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await query.answer("⏰ This order has expired.", show_alert=True)
            return ConversationHandler.END
        payment_type, network = _parse_bybit_meta(tx.crypto_address or "")
        if payment_type not in (BybitPaymentType.UID_TRANSFER, BybitPaymentType.ONCHAIN):
            await query.answer("Please choose a payment type first.", show_alert=True)
            return ConversationHandler.END

    context.user_data['bybit_tx_id'] = tx_id
    prompt = (
        "🧾 Please paste your Bybit internal Transaction ID below."
        if payment_type == BybitPaymentType.UID_TRANSFER else
        "🧾 Please paste the blockchain Transaction ID (TXID) below."
    )
    await query.message.reply_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bybit_cancel_submit")]]),
    )
    return BYBIT_TXID


async def bybit_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('bybit_tx_id', None)
    try:
        await query.edit_message_text("❌ Cancelled. Your order is still pending — you can submit the Transaction ID again anytime before it expires.")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END


_BYBIT_FRIENDLY_ERROR = {
    BybitVerificationOutcome.NOT_CONFIGURED: "⚠️ Bybit verification is temporarily unavailable.\n\nPlease try again shortly.",
    BybitVerificationOutcome.API_ERROR: "⚠️ Bybit verification is temporarily unavailable.\n\nPlease try again shortly.",
    BybitVerificationOutcome.NOT_FOUND: "❌ Transaction could not be verified.\n\nPlease check the Transaction ID and try again.",
    BybitVerificationOutcome.NOT_SUCCESSFUL: "❌ This transaction hasn't completed successfully yet on Bybit's side. Please wait a moment and try again.",
    BybitVerificationOutcome.TOO_OLD: "❌ Transaction could not be verified.\n\nPlease check the Transaction ID and try again.",
    BybitVerificationOutcome.NETWORK_MISMATCH: "❌ Wrong network for this order.",
    BybitVerificationOutcome.WRONG_ADDRESS: "❌ This deposit was not sent to our configured deposit address.",
    BybitVerificationOutcome.INVALID_TXID: None,  # handled separately (custom message)
}

# Same fallback messages as _BYBIT_FRIENDLY_ERROR, but for the USDT
# TRC20/BEP20/ERC20 on-chain deposit flow, which must never surface the
# word "Bybit" in user-facing text (unlike the Bybit Pay / UID Transfer
# method above, which keeps its original wording).
_BYBIT_FRIENDLY_ERROR_ONCHAIN = {
    **_BYBIT_FRIENDLY_ERROR,
    BybitVerificationOutcome.NOT_CONFIGURED: "⚠️ Verification is temporarily unavailable.\n\nPlease try again shortly.",
    BybitVerificationOutcome.API_ERROR: "⚠️ Verification is temporarily unavailable.\n\nPlease try again shortly.",
    BybitVerificationOutcome.NOT_SUCCESSFUL: "❌ This transaction has not been confirmed yet. Please wait a moment and try again.",
}


async def bybit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify a submitted Bybit Transaction ID against Bybit's own deposit
    history (services/bybit_pay.py) and, only if it checks out, atomically
    credit the wallet exactly once."""
    telegram_id = update.effective_user.id
    txid_raw = (update.message.text or "").strip()
    tx_id = context.user_data.get('bybit_tx_id')

    if not tx_id:
        await update.message.reply_text("❌ Session expired. Please start again from your pending order.")
        return ConversationHandler.END

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END
        tx = session.query(Transaction).filter_by(id=tx_id, user_id=user.id).first()
        if not tx or tx.payment_method != PaymentMethod.BYBIT_PAY:
            await update.message.reply_text("❌ Order not found.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            await update.message.reply_text("❌ This order is no longer pending.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END
        if tx.expires_at and datetime.utcnow() > tx.expires_at:
            await update.message.reply_text("⏰ This order has expired. Please start a new top-up.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END

        payment_type, network = _parse_bybit_meta(tx.crypto_address or "")
        if payment_type not in (BybitPaymentType.UID_TRANSFER, BybitPaymentType.ONCHAIN):
            await update.message.reply_text("❌ Please choose a payment type first.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END

        order_created_at = tx.created_at
        usd_amount_for_credit = tx.amount            # always USD — used for wallet credit
        locked_crypto_amount = tx.locked_crypto_amount  # None for USDT-based orders
        user_id = tx.user_id

    is_uid = payment_type == BybitPaymentType.UID_TRANSFER

    # Determine verification currency and expected amount.
    # LTC orders lock a crypto amount at creation time; all USDT-based
    # networks (TRC20, BEP20, ERC20, AVAXC, TON) verify against USD amount.
    _NON_STABLECOIN_NETWORKS = {"LTC"}
    if not is_uid and network in _NON_STABLECOIN_NETWORKS and locked_crypto_amount:
        verify_currency = network                              # "LTC"
        expected_amount = Decimal(str(round(locked_crypto_amount, 8)))
        verify_tolerance = Decimal("0.000001")                # 1 millionth LTC rounding tolerance
    else:
        verify_currency = BYBIT_CURRENCY                      # "USDT"
        expected_amount = _to_decimal_amount(usd_amount_for_credit)
        verify_tolerance = Decimal("0")

    valid_format = is_valid_uid_txid_format(txid_raw) if is_uid else is_valid_onchain_txid_format(txid_raw)
    if not valid_format:
        await update.message.reply_text(
            "❌ That doesn't look like a valid Transaction ID. Please paste the exact "
            + ("internal Transaction ID from your completed UID Transfer."
               if is_uid else "blockchain Transaction ID (TXID) from your completed deposit.")
        )
        return BYBIT_TXID

    if bybit_is_rate_limited(telegram_id):
        await update.message.reply_text(
            "⚠️ Too many verification attempts. Please wait a minute and try again."
        )
        return BYBIT_TXID

    # Fast pre-check (defense in depth — the UNIQUE constraint at insert
    # time below is the real, race-proof guarantee).
    with get_db_session() as session:
        already = session.query(BybitPayTransaction).filter_by(transaction_id=txid_raw).first()
        if already:
            await update.message.reply_text("❌ This transaction has already been used.")
            return BYBIT_TXID

    # Prevent two concurrent submissions for the SAME order from both
    # racing the (slow) Bybit API call in parallel.
    lock = bybit_get_order_lock(telegram_id, tx_id)
    if not lock.acquire(blocking=False):
        await update.message.reply_text("⏳ Your previous submission for this order is still being verified — please wait.")
        return BYBIT_TXID

    try:
        svc = BybitPayService()
        if is_uid:
            result = await asyncio.to_thread(
                svc.verify_uid_transfer,
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=verify_currency, order_created_at=order_created_at,
            )
        else:
            result = await asyncio.to_thread(
                svc.verify_onchain_deposit,
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=verify_currency, network=network, order_created_at=order_created_at,
                tolerance=verify_tolerance,
            )
    finally:
        lock.release()

    # ---- Outcomes that are clear user errors — return inline, no admin notification. ----
    if result.outcome == BybitVerificationOutcome.CURRENCY_MISMATCH:
        await update.message.reply_text("❌ Unsupported payment currency.")
        return BYBIT_TXID

    # ---- Outcomes that warrant admin review — log + queue + notify. ----
    # Per spec: ANY submission that reached the Bybit API but could not be
    # fully, automatically verified must be escalated to admin review —
    # never silently dropped and never auto-approved. Only outcomes that
    # never touched the API at all (bad TXID format) are excluded, since
    # there is nothing yet for an admin to review.
    _BYBIT_ADMIN_NOTIFY_OUTCOMES = {
        BybitVerificationOutcome.API_ERROR,
        BybitVerificationOutcome.NOT_FOUND,
        BybitVerificationOutcome.AMOUNT_MISMATCH,
        BybitVerificationOutcome.NOT_SUCCESSFUL,
        BybitVerificationOutcome.TOO_OLD,
        BybitVerificationOutcome.NETWORK_MISMATCH,
        BybitVerificationOutcome.WRONG_ADDRESS,
        BybitVerificationOutcome.NOT_CONFIGURED,
    }
    if result.outcome in _BYBIT_ADMIN_NOTIFY_OUTCOMES or (result.outcome != BybitVerificationOutcome.SUCCESS):
        outcome_str = result.outcome.name if hasattr(result.outcome, 'name') else str(result.outcome)
        detail_str = (
            f"expected {expected_amount} {verify_currency}, "
            f"received {result.received_amount} {result.currency or verify_currency}"
            if result.outcome == BybitVerificationOutcome.AMOUNT_MISMATCH
            else str(getattr(result, 'error_message', '') or '')
        )

        try:
            with get_db_session() as _sess:
                _sess.add(VerificationAttemptLog(
                    gateway="bybit_pay",
                    telegram_user_id=telegram_id,
                    internal_order_id=tx_id,
                    submitted_txid=txid_raw,
                    outcome=outcome_str,
                    detail=detail_str[:500] if detail_str else None,
                ))
                _sess.commit()
        except Exception:
            logger.exception("Failed to write VerificationAttemptLog (bybit)")

        pmv_id = None
        if result.outcome in _BYBIT_ADMIN_NOTIFY_OUTCOMES:
            try:
                with get_db_session() as _sess:
                    existing_pmv = _sess.query(PendingManualVerification).filter_by(
                        gateway="bybit_pay",
                        internal_order_id=tx_id,
                        submitted_txid=txid_raw,
                    ).first()
                    if existing_pmv:
                        pmv_id = existing_pmv.id
                    else:
                        pmv = PendingManualVerification(
                            gateway="bybit_pay",
                            telegram_user_id=telegram_id,
                            internal_order_id=tx_id,
                            submitted_txid=txid_raw,
                            amount=expected_amount,
                            currency=verify_currency,
                            payment_type=payment_type,
                            network=network,
                            auto_outcome=outcome_str,
                            auto_detail=detail_str[:500] if detail_str else None,
                            status="pending",
                        )
                        _sess.add(pmv)
                        _sess.commit()
                        _sess.refresh(pmv)
                        pmv_id = pmv.id
            except Exception:
                logger.exception("Failed to create PendingManualVerification (bybit)")

            # Per-order dedup: only the attempt that flips review_notified
            # False→True actually sends, so resubmitting a TXID for the
            # same order never re-alerts admins.
            if pmv_id is not None:
                review_claimed = False
                try:
                    with get_db_session() as _rsess:
                        review_claimed = _rsess.query(Transaction).filter(
                            Transaction.id == tx_id,
                            Transaction.review_notified.is_(False),
                        ).update(
                            {Transaction.review_notified: True},
                            synchronize_session=False,
                        ) == 1
                        _rsess.commit()
                except Exception:
                    logger.exception("Failed to claim review_notified for tx %s (bybit)", tx_id)
                    review_claimed = False

            if pmv_id is not None and review_claimed:
                try:
                    net_label = f" ({payment_type}/{network})" if payment_type else ""
                    bybit_reason_map = {
                        BybitVerificationOutcome.API_ERROR: "⚠️ API error (temporary)",
                        BybitVerificationOutcome.NOT_FOUND: "❓ TXID not found in Bybit account history",
                        BybitVerificationOutcome.AMOUNT_MISMATCH: f"💸 Amount mismatch — expected {expected_amount}, got {result.received_amount}",
                        BybitVerificationOutcome.NOT_SUCCESSFUL: "⏳ Transaction found but not yet marked successful on Bybit",
                        BybitVerificationOutcome.TOO_OLD: "🕰️ Transaction time is outside the order window",
                        BybitVerificationOutcome.NETWORK_MISMATCH: "🔀 Deposit network does not match the order",
                        BybitVerificationOutcome.WRONG_ADDRESS: "📮 Deposit was sent to an address we don't recognize",
                        BybitVerificationOutcome.NOT_CONFIGURED: "⚙️ Bybit Pay API is not configured",
                    }
                    reason = bybit_reason_map.get(result.outcome, outcome_str)
                    _uname_b = update.effective_user.username
                    _uname_display_b = f"@{_uname_b}" if _uname_b else "(no username)"
                    _net_detail = f" • Network: {payment_type}/{network}" if payment_type else ""
                    admin_ids = _gateway_admin_recipient_ids()
                    for admin_id in admin_ids:
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="bybit_pay",
                                    amount=f"{expected_amount} {verify_currency}",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    customer_name=_uname_display_b,
                                    user_id=telegram_id,
                                    status_key="pending_review",
                                    extra=[("🌐", "Network", f"{payment_type}/{network}")] if payment_type else (),
                                    note=f"⚠️ <b>Auto-verify failed:</b> {reason}",
                                ),
                                reply_markup=pui.admin_review_keyboard(
                                    verify_cb=f"admin_bybit_verify_{tx_id}_{pmv_id}",
                                    approve_cb=f"admin_bybit_approve_{tx_id}_{pmv_id}",
                                    reject_cb=f"admin_bybit_reject_start_{tx_id}_{pmv_id}",
                                    view_user_cb=f"admin_view_user_pmv_{telegram_id}",
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            logger.exception("Failed to send Bybit manual-verification notification to admin %s", admin_id)
                except Exception:
                    logger.exception("Failed to send admin notification for Bybit manual verification")

        if result.outcome == BybitVerificationOutcome.AMOUNT_MISMATCH:
            if pmv_id:
                sent = await update.message.reply_text(
                    pui.user_payment_card(
                        gateway_key="bybit_pay", stage="pending_review",
                        amount=f"{expected_amount} {verify_currency}", order_id=tx_id, txn_id=txid_raw,
                        extra=[("📥", "Received", f"{result.received_amount} {result.currency or verify_currency}")],
                        note="⚠️ Amount mismatch detected — our team has been notified and "
                             "will review your payment shortly.",
                    ),
                    parse_mode='HTML',
                )
                pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
            else:
                await update.message.reply_text(
                    "❌ Payment amount mismatch.\n\n"
                    f"Expected: {expected_amount} {verify_currency}\n"
                    f"Received: {result.received_amount} {result.currency or verify_currency}"
                )
        elif pmv_id:
            sent = await update.message.reply_text(
                pui.user_payment_card(
                    gateway_key="bybit_pay", stage="pending_review",
                    amount=f"{expected_amount} {verify_currency}", order_id=tx_id, txn_id=txid_raw,
                    note="Our team has been notified and will review your transaction shortly. "
                         "You'll be notified the moment your balance is updated.",
                ),
                parse_mode='HTML',
            )
            pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
        else:
            error_map = _BYBIT_FRIENDLY_ERROR if is_uid else _BYBIT_FRIENDLY_ERROR_ONCHAIN
            friendly = error_map.get(
                result.outcome, "❌ Transaction could not be verified.\n\nPlease check the Transaction ID and try again."
            )
            await update.message.reply_text(friendly)
        return BYBIT_TXID

    # ---- Verified — log the successful attempt, then credit the wallet exactly once, atomically. ----
    try:
        with get_db_session() as _sess:
            _sess.add(VerificationAttemptLog(
                gateway="bybit_pay",
                telegram_user_id=telegram_id,
                internal_order_id=tx_id,
                submitted_txid=txid_raw,
                outcome="SUCCESS",
                detail=f"received {result.received_amount} {result.currency or BYBIT_CURRENCY}"[:500],
            ))
            _sess.commit()
    except Exception:
        logger.exception("Failed to write VerificationAttemptLog (bybit success)")

    import json as _json
    from services.wallet import credit_locked, WalletError

    credited_usd = 0.0
    bonus_amount = 0.0
    dup = False
    with get_db_session() as session:
        # Re-check PENDING under this transaction (closes the race between
        # the read above and now).
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update({Transaction.status: TransactionStatus.COMPLETED, Transaction.completed_at: datetime.utcnow()},
                 synchronize_session=False)
        if flipped == 0:
            await update.message.reply_text("❌ This order is no longer pending.")
            context.user_data.pop('bybit_tx_id', None)
            return ConversationHandler.END

        raw_json = None
        try:
            raw_json = _json.dumps(result.matched_record or {})[:8000]
        except Exception:
            raw_json = None

        bpt = BybitPayTransaction(
            transaction_id=txid_raw,
            bybit_record_id=result.bybit_record_id,
            telegram_user_id=telegram_id,
            internal_order_id=tx_id,
            payment_type=payment_type,
            network=network if not is_uid else None,
            currency=result.currency or BYBIT_CURRENCY,
            expected_amount=expected_amount,
            received_amount=result.received_amount,
            transaction_time=(datetime.utcfromtimestamp(result.transaction_time / 1000)
                               if result.transaction_time else None),
            raw_transaction_data=raw_json,
        )
        session.add(bpt)
        try:
            session.flush()
        except IntegrityError:
            # Another concurrent request won the race — this txid was just
            # claimed by someone else. Roll back our COMPLETED flip too.
            session.rollback()
            dup = True
        else:
            bonus_percent = BybitPayService().bonus_percent
            base_usd = float(usd_amount_for_credit)  # always USD regardless of crypto network
            bonus_amount = round(base_usd * (bonus_percent / 100.0), 2) if bonus_percent else 0.0
            credited_usd = base_usd + bonus_amount
            try:
                credit_locked(
                    session, user_id, credited_usd,
                    reason=f"Bybit Pay top-up #{tx_id}", actor_type="system",
                    ref_type="bybit_pay", ref_id=str(tx_id),
                )
            except WalletError:
                logger.exception("Bybit Pay wallet credit failed for tx %s", tx_id)
                session.rollback()
                await update.message.reply_text(
                    "⚠️ Verification succeeded but crediting your balance failed. Please contact support with your Deposit ID: %s" % pui.format_deposit_id(tx_id)
                )
                context.user_data.pop('bybit_tx_id', None)
                return ConversationHandler.END
            session.commit()
            # V19 — deposit receipt + activity log (best-effort)
            try:
                from handlers.account_features import create_receipt_record, log_activity
                create_receipt_record(
                    order_id=None, transaction_id=tx_id,
                    user_id_db=user_id, receipt_type="deposit",
                )
                log_activity(
                    user_id_db=user_id, action="deposit", status="success",
                    details=f"${credited_usd:.2f} deposited via Bybit Pay",
                    ref_type="transaction", ref_id=str(tx_id),
                )
            except Exception:
                pass

    context.user_data.pop('bybit_tx_id', None)

    if dup:
        await update.message.reply_text("❌ This transaction has already been used.")
        return ConversationHandler.END

    _bonus_str = f"+{bonus_amount:.2f} USD" if bonus_amount else None
    await update.message.reply_text(
        pui.deposit_success_card(
            amount=f"${credited_usd:.2f} USD",
            payment_method="Bybit Pay",
            deposit_id=pui.format_deposit_id(tx_id),
            bonus_line=_bonus_str,
        ),
        reply_markup=pui.deposit_success_keyboard(),
        parse_mode='HTML',
    )
    return ConversationHandler.END


def _to_decimal_amount(value) -> Decimal:
    try:
        return Decimal(str(round(float(value), 2)))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


async def payment_method_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bKash payment method selection — ask for amount next."""
    gmin = cfg.get_float("bkash_min_amount", 0.0)
    gmax = cfg.get_float("bkash_max_amount", 0.0)
    return await _ask_amount_for_gateway(update, context, "bkash", "bKash", "📱", gmin, gmax)


async def payment_method_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Nagad payment method selection — ask for amount next."""
    gmin = cfg.get_float("nagad_min_amount", 0.0)
    gmax = cfg.get_float("nagad_max_amount", 0.0)
    return await _ask_amount_for_gateway(update, context, "nagad", "Nagad", "🟠", gmin, gmax)


async def payment_method_cryptomus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Cryptomus (USDT/crypto) payment method selection — ask for amount next.

    Cryptomus is used instead of @CryptoBot for regions (e.g. Bangladesh)
    where @CryptoBot isn't usable. Fully automated — no Manual mode, unlike
    bKash/Nagad.
    """
    return await _ask_amount_for_gateway(update, context, "cryptomus", "Cryptomus (USDT/Crypto)", "💠")


async def payment_method_nowpayments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle NOWPayments (crypto) payment method selection — ask for amount next.

    Fully automated — no Manual mode. See services/nowpayments_payment.py.
    """
    return await _ask_amount_for_gateway(update, context, "nowpayments", "NOWPayments (Crypto)", "🌐")


async def payment_method_zinipay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle BKash • Nagad • Rocket (ZiniPay-backed) payment method selection — ask for amount next.

    Fully automated — no Manual mode. See services/zinipay_payment.py.
    """
    return await _ask_amount_for_gateway(update, context, "zinipay", "BKash • Nagad • Rocket", "🇧🇩")


async def payment_method_binance_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Binance Pay payment method selection — ask for amount next.

    Fully automated verification (Binance transaction-history lookup), but
    NOT a hosted checkout link — the user pastes their own Binance Pay
    transaction ID back into the bot afterwards. See services/binance_pay.py.
    """
    svc = BinancePayService()
    return await _ask_amount_for_gateway(update, context, "binance_pay", "Binance Pay", "🟡", svc.min_amount, svc.max_amount)


async def payment_method_bybit_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Bybit Pay (UID Transfer) payment method selection — ask for amount next.

    Fully automated verification via the official Bybit V5 API
    (GET /v5/asset/deposit/query-internal-record). The user pays from their
    own Bybit app and reports the internal Transaction ID. See services/bybit_pay.py.
    On-chain deposits (TRC20/BEP20/ERC20) are handled by the dedicated
    payment_method_bybit_trc20 / bep20 / erc20 handlers below.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_pay", "Bybit Pay", "💙", svc.min_amount, svc.max_amount)


async def payment_method_bybit_trc20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT TRC20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for TRC20 and shows the Bybit TRC20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → TRC20' sub-menu path. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_trc20", "USDT TRC20", "💵", svc.min_amount, svc.max_amount)


async def payment_method_bybit_bep20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT BEP20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for BEP20 and shows the Bybit BEP20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → BEP20' sub-menu path. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_bep20", "USDT BEP20", "🟢", svc.min_amount, svc.max_amount)


async def payment_method_bybit_erc20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT ERC20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for ERC20 and shows the Bybit ERC20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → ERC20' sub-menu path. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_erc20", "USDT ERC20", "🔵", svc.min_amount, svc.max_amount)


async def payment_method_bybit_ton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT TON payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for TON on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_ton", "USDT TON", "⚫", svc.min_amount, svc.max_amount)


async def payment_method_bybit_avaxc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Avalanche C-Chain payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for AVAXC on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_avaxc", "USDT Avalanche C-Chain", "🔺", svc.min_amount, svc.max_amount)


async def payment_method_bybit_ltc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Litecoin (LTC) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for LTC on-chain deposit and shows the
    configured LTC deposit address directly. Verification uses the same
    Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record) as
    TRC20/BEP20/ERC20. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_ltc", "Litecoin (LTC)", "🪙", svc.min_amount, svc.max_amount)


async def payment_method_bybit_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Base (Coinbase Base L2) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for BASE on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_base", "USDT Base", "🔷", svc.min_amount, svc.max_amount)


async def payment_method_bybit_arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Arbitrum One payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for ARBONE on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_arb", "USDT Arbitrum", "🔵", svc.min_amount, svc.max_amount)


async def payment_method_bybit_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Optimism payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for OP on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_op", "USDT Optimism", "🔴", svc.min_amount, svc.max_amount)


async def payment_method_bybit_matic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Polygon (MATIC) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for MATIC on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE/OP. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_matic", "USDT Polygon", "🟣", svc.min_amount, svc.max_amount)


async def payment_method_bybit_sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Solana payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for SOL on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE/OP/MATIC. See services/bybit_pay.py.
    """
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_sol", "USDT Solana", "🟢", svc.min_amount, svc.max_amount)


async def payment_method_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Card payment via Telegram Payments (native sendInvoice flow)."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    provider_token = app_settings.TELEGRAM_PROVIDER_TOKEN
    if not provider_token:
        try:
            await query.edit_message_text(
                "❌ Card payments are not configured yet.\n\nPlease choose another payment method or contact support.",
                reply_markup=create_cancel_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    if usd_amount <= 0:
        try:
            await query.edit_message_text("❌ Invalid amount. Please start the top-up again.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    # Create a pending transaction; its id is carried in the invoice payload.
    # Card transactions have no expires_at: confirmation arrives via Telegram's
    # successful_payment update, so the expiry job should not touch them.
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.CARD,
            status=TransactionStatus.PENDING
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        transaction_id = transaction.id
        transaction_created_at = transaction.created_at

    # Replace the method-selection message with a short notice, then send the invoice.
    try:
        try:
            await query.edit_message_text(
                pui.user_payment_card(
                    gateway_key="card",
                    stage="waiting",
                    amount=pui.copy_code(format_price(usd_amount)),
                    order_id=transaction_id,
                    created_at=transaction_created_at,
                    note="👉 Please complete the secure card payment below.",
                ),
                parse_mode='HTML',
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        logger.warning('Ignored Telegram/API error', exc_info=True)

    # Telegram expects the price in the smallest currency unit (e.g. cents for USD).
    prices = [LabeledPrice(label="Wallet Top-up", amount=int(round(usd_amount * 100)))]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Wallet Top-up",
        description=f"Add {format_price(usd_amount)} to your wallet balance.",
        payload=f"topup_{transaction_id}",
        provider_token=provider_token,
        currency=app_settings.PAYMENT_CURRENCY,
        prices=prices,
        start_parameter=f"topup-{transaction_id}"
    )

    return ConversationHandler.END


async def payment_method_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram Stars payment method selection — ask for amount next."""
    query = update.callback_query
    await query.answer()

    stars_cfg = telegram_stars_service.get_config()
    if not stars_cfg["enabled"]:
        try:
            await query.edit_message_text(
                "❌ Telegram Stars payments are not enabled right now.\n\n"
                "Please choose another payment method or contact support.",
                reply_markup=create_cancel_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    context.user_data['topup_method'] = ('gateway', 'stars')
    try:
        await query.edit_message_text(
            "⭐ Telegram Stars selected.\n\n💬 How much would you like to add to your wallet, in USD?\nExample: 10",
            reply_markup=create_cancel_keyboard(),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT


async def _finish_stars_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float):
    """Create the Stars (native XTR sendInvoice) payment, once the amount has
    been collected. Mirrors the old payment_method_stars body, but replies to
    a text message and validates star limits (rather than relying on a
    pre-set context.user_data['topup_amount'])."""
    user_id = update.effective_user.id

    stars_cfg = telegram_stars_service.get_config()
    if not stars_cfg["enabled"]:
        await update.message.reply_text(
            "❌ Telegram Stars payments are not enabled right now.\n\n"
            "Please choose another payment method or contact support.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    stars_amount = telegram_stars_service.stars_for_usd(usd_amount)
    if not (stars_cfg["min_stars"] <= stars_amount <= stars_cfg["max_stars"]):
        await update.message.reply_text(
            f"❌ This amount needs {stars_amount} ⭐, which is outside the "
            f"allowed range ({stars_cfg['min_stars']}–{stars_cfg['max_stars']} ⭐).\n\n"
            "Please enter a different amount, or choose another method to start again.",
            reply_markup=create_cancel_keyboard()
        )
        return AMOUNT

    # Create a pending transaction; its id is carried in the invoice payload.
    # Like Card, Stars top-ups have no expires_at — confirmation arrives via
    # Telegram's own successful_payment update, not the expiry sweep job.
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.STARS,
            status=TransactionStatus.PENDING,
            stars_amount=stars_amount,
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        transaction_id = transaction.id
        transaction_created_at = transaction.created_at

    # Send a short notice, then send the invoice.
    try:
        await update.message.reply_text(
            pui.user_payment_card(
                gateway_key="stars",
                stage="waiting",
                amount=pui.copy_code(format_price(usd_amount)),
                order_id=transaction_id,
                created_at=transaction_created_at,
                extra=[("⭐", "Cost", f"{stars_amount} Stars")],
                note="👉 Please complete the Stars payment below.",
            ),
            parse_mode='HTML',
        )
    except Exception:
        logger.warning('Ignored Telegram/API error', exc_info=True)

    # Telegram Stars (XTR): the price is the exact Star count — it is NOT
    # multiplied by 100 like fiat currencies — and `provider_token` MUST be
    # an empty string since Telegram itself settles the payment.
    prices = [LabeledPrice(label="Wallet Top-up", amount=stars_amount)]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Wallet Top-up",
        description=f"Add {format_price(usd_amount)} to your wallet balance using {stars_amount} ⭐ Stars.",
        payload=f"stars_topup_{transaction_id}",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter=f"stars-topup-{transaction_id}"
    )

    return ConversationHandler.END


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout query for a valid pending Card or Stars top-up."""
    query = update.pre_checkout_query
    payload = query.invoice_payload or ""

    transaction_id = None
    expected_method = None
    if payload.startswith("stars_topup_"):
        expected_method = PaymentMethod.STARS
        try:
            transaction_id = int(payload.split("_", 2)[2])
        except (ValueError, IndexError):
            transaction_id = None
    elif payload.startswith("topup_"):
        expected_method = PaymentMethod.CARD
        try:
            transaction_id = int(payload.split("_", 1)[1])
        except (ValueError, IndexError):
            transaction_id = None

    is_valid = False
    if transaction_id is not None and expected_method is not None:
        with get_db_session() as session:
            transaction = session.query(Transaction).filter_by(
                id=transaction_id,
                payment_method=expected_method
            ).first()
            # Allow if not already credited (PENDING, or EXPIRED for a late-but-honoured pay).
            if transaction and transaction.status != TransactionStatus.COMPLETED:
                if expected_method == PaymentMethod.STARS:
                    # Cross-check the Star amount Telegram is about to charge
                    # against what we quoted at invoice-creation time, so a
                    # mid-flight admin rate change can't under/over-charge.
                    quoted_stars = transaction.stars_amount or 0
                    is_valid = bool(quoted_stars) and query.total_amount == quoted_stars
                else:
                    is_valid = True

    if is_valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="This payment order is no longer valid. Please start a new top-up."
        )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Credit the wallet once Telegram confirms a successful Card or Stars payment."""
    payment = update.message.successful_payment
    payload = payment.invoice_payload or ""

    if payload.startswith("stars_topup_"):
        method = PaymentMethod.STARS
        try:
            transaction_id = int(payload.split("_", 2)[2])
        except (ValueError, IndexError):
            return
    elif payload.startswith("topup_"):
        method = PaymentMethod.CARD
        try:
            transaction_id = int(payload.split("_", 1)[1])
        except (ValueError, IndexError):
            return
    else:
        return

    # DB-backed idempotency: claim this Telegram charge ID exactly once.
    # This MUST fail CLOSED: if the claim call itself raises (DB error,
    # import error, etc.) we do NOT know whether this charge was already
    # processed, so we abort without crediting the wallet or delivering
    # anything. Silently "proceeding anyway" here was the fail-open bug —
    # a transient error during the idempotency check must never result in
    # a duplicate wallet credit for the same Telegram payment.
    charge_id = payment.telegram_payment_charge_id or ""
    if not charge_id:
        logger.error(
            "successful_payment_callback: missing telegram_payment_charge_id "
            "for transaction payload %s — refusing to credit wallet (fail closed)",
            payload,
        )
        return
    idem_source = "tg_stars_topup" if method == PaymentMethod.STARS else "tg_card_topup"
    try:
        from services.idempotency import claim as _idem_claim
        with _idem_claim(idem_source, charge_id) as _ok:
            if not _ok:
                logger.info("successful_payment_callback: duplicate charge %s", charge_id)
                return
    except Exception:
        logger.error(
            "idempotency.claim raised for charge %s — refusing to credit wallet "
            "(fail closed, no delivery/credit performed)", charge_id, exc_info=True,
        )
        return

    notif = None
    with get_db_session() as session:
        transaction = session.query(Transaction).filter_by(
            id=transaction_id,
            payment_method=method
        ).first()

        if not transaction:
            return

        # Belt-and-suspenders: status check after idempotency claim.
        if transaction.status == TransactionStatus.COMPLETED:
            return

        if method == PaymentMethod.STARS:
            quoted_stars = transaction.stars_amount or 0
            if quoted_stars and payment.total_amount != quoted_stars:
                # Telegram already took the user's Stars at this point, so we
                # still credit the wallet — but log loudly since this means
                # the quoted price and the charged price disagree (e.g. the
                # admin changed the rate mid-flight). We credit the USD value
                # that was quoted/frozen on the transaction, not a recomputed one.
                logger.warning(
                    "Stars payment amount mismatch for transaction %s: "
                    "quoted=%s paid=%s — crediting the originally quoted USD value",
                    transaction_id, quoted_stars, payment.total_amount,
                )

        transaction.status = TransactionStatus.COMPLETED
        transaction.completed_at = datetime.utcnow()
        # Store Telegram's charge id in crypto_address for reference.
        transaction.crypto_address = f"tg_charge:{payment.telegram_payment_charge_id}"
        credit_amount = float(transaction.amount)
        stars_paid = transaction.stars_amount
        session.flush()

        user = session.query(User).filter_by(id=transaction.user_id).first()
        if not user:
            session.commit()
            return
        user_db_id = user.id
        user_telegram_id = user.telegram_id

        if method == PaymentMethod.STARS:
            # Use the ledgered wallet service for Stars so the credit shows
            # up in Admin Wallets / WalletLedger history.
            session.commit()
        else:
            # Card path unchanged from before: direct balance update in the
            # same transaction as the status flip.
            user.wallet_balance += credit_amount
            session.commit()
            notif = {
                'telegram_id': user_telegram_id,
                'amount': credit_amount,
                'new_balance': user.wallet_balance,
                'transaction_id': transaction_id,
                'method': 'card',
            }

    if method == PaymentMethod.STARS:
        try:
            from services import wallet as wallet_svc
            new_balance = wallet_svc.credit(
                user_db_id, credit_amount,
                reason=f"Telegram Stars top-up (#{transaction_id}, {stars_paid} ⭐)",
                actor_type="system",
                ref_type="stars_topup",
                ref_id=str(transaction_id),
            )
        except Exception:
            logger.exception(
                "wallet credit failed for Stars transaction %s — falling back "
                "to a direct balance update", transaction_id,
            )
            with get_db_session() as session2:
                user2 = session2.query(User).filter_by(id=user_db_id).first()
                if not user2:
                    return
                user2.wallet_balance = float(user2.wallet_balance or 0.0) + credit_amount
                new_balance = user2.wallet_balance
                session2.commit()
        notif = {
            'telegram_id': user_telegram_id,
            'amount': credit_amount,
            'new_balance': new_balance,
            'transaction_id': transaction_id,
            'method': 'stars',
            'stars': stars_paid,
        }

    # V19 — deposit receipt + activity log (best-effort)
    try:
        from handlers.account_features import create_receipt_record, log_activity
        create_receipt_record(
            order_id=None, transaction_id=transaction_id,
            user_id_db=user_db_id, receipt_type="deposit",
        )
        log_activity(
            user_id_db=user_db_id, action="deposit", status="success",
            details=f"${credit_amount:.2f} deposited via {method.value if method else 'card'}",
            ref_type="transaction", ref_id=str(transaction_id),
        )
    except Exception:
        pass

    if not notif:
        return

    method_label = "Telegram Stars ⭐" if notif['method'] == 'stars' else "Card"
    extra_rows = [("⭐", "Stars Paid", notif['stars'])] if notif['method'] == 'stars' else []
    user_message = sanitize_message(
        pui.deposit_success_card(
            amount=format_price(notif['amount']),
            payment_method=method_label,
            deposit_id=pui.format_deposit_id(notif['transaction_id']),
        )
    )

    await update.message.reply_text(
        user_message, reply_markup=pui.deposit_success_keyboard(), parse_mode='HTML',
    )

    admin_message = pui.admin_review_card(
        gateway_key="stars" if notif['method'] == 'stars' else "card",
        gateway_label_override=method_label,
        amount=format_price(notif['amount']),
        order_id=notif['transaction_id'],
        user_id=notif['telegram_id'],
        status_key="approved",
    )

    await notify_admin(context, admin_message, parse_mode='HTML')


async def _cancel_pending_and_go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared Cancel behavior for every payment page.

    Cancels whatever PENDING transaction(s) this user currently has (same
    business/DB logic as before), then behaves exactly like a Back tap:
    the current payment message is deleted if Telegram allows it and the
    Payment Method screen is sent fresh; if deletion isn't possible, the
    existing message is edited into the Payment Method screen instead.
    No "Payment Cancelled" card, no Back/Support buttons, and never both a
    delete *and* an edit — only ever one resulting message.

    Returns ``is_empty`` (True if no payment method is configured at all)
    so callers driving a ConversationHandler can end it appropriately.
    """
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            _cancel_user_pending_transactions(session, user.id)

    context.user_data.pop('topup_amount', None)
    context.user_data.pop('topup_method', None)

    text, keyboard, is_empty = _build_topup_method_screen()

    deleted = False
    try:
        await query.message.delete()
        deleted = True
    except Exception:
        deleted = False

    if deleted:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, reply_markup=keyboard,
                parse_mode="HTML")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    context.user_data.clear()
    return is_empty


async def cancel_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the top-up process (during conversation).

    Behaves like a Back tap straight to the Payment Method screen — no
    "Payment Cancelled" card. A pending Transaction row may or may not
    exist yet at this point in the flow, depending on which step the user
    cancelled from; if one *was* already created, it's marked CANCELLED so
    it can never keep blocking a future payment order.
    """
    await _cancel_pending_and_go_back(update, context)
    return ConversationHandler.END


async def cancel_payment_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel from payment instruction page (outside conversation).

    This button is shared by every gateway's payment-instructions page, so
    there's no single tx_id in the callback data — instead, cancel whatever
    PENDING transaction(s) this user currently has (this is what actually
    frees the user to start a new payment order immediately) and behave
    like a Back tap straight to the Payment Method screen.
    """
    await _cancel_pending_and_go_back(update, context)


async def check_pending_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to check pending payment transactions (non-blocking)."""
    import asyncio

    def _check_and_process_payments_sync():
        """Synchronous database operations run in thread pool."""
        payment_notifications = []

        with get_db_session() as session:
            pending_transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING
            ).all()

            for transaction in pending_transactions:
                # Check if transaction has expired
                if transaction.expires_at and datetime.utcnow() > transaction.expires_at:
                    continue  # Will be handled by check_expired_payments

                # Verify payment based on payment method
                is_paid = False
                if transaction.payment_method == PaymentMethod.CRYPTO_WALLET:
                    crypto_service = CryptoBotService()
                    is_paid = crypto_service.check_payment_status(transaction.crypto_address, transaction.amount)
                elif transaction.payment_method == PaymentMethod.BKASH:
                    bkash_service = BkashPaymentService()
                    is_paid = bkash_service.check_payment_status(transaction.crypto_address, transaction.amount)
                elif transaction.payment_method == PaymentMethod.NAGAD:
                    nagad_service = NagadPaymentService()
                    is_paid = nagad_service.check_payment_status(transaction.crypto_address, transaction.amount)
                elif transaction.payment_method == PaymentMethod.CRYPTOMUS:
                    cryptomus_service = CryptomusPaymentService()
                    is_paid = cryptomus_service.check_payment_status(transaction.crypto_address, transaction.amount)
                elif transaction.payment_method == PaymentMethod.NOWPAYMENTS:
                    nowpayments_service = NowPaymentsService()
                    is_paid = nowpayments_service.check_payment_status(transaction.crypto_address, transaction.amount)
                # NOTE: PaymentMethod.ZINIPAY is no longer polled here.
                # The new ZiniPay flow is user-driven (verify+confirm on TXID
                # submission) — there is no background polling or webhook.
                # Pending ZINIPAY transactions are cleaned up by
                # check_expired_payments as usual.

                if is_paid:
                    # Idempotency guard — stable reference is the transaction's
                    # own DB id (never a Telegram update_id — this job has no
                    # update_id at all, and re-runs on every poll interval, so
                    # a durable per-transaction claim is essential). Defense in
                    # depth alongside the atomic conditional UPDATE below: if
                    # the claim itself raises, fail CLOSED (skip this cycle,
                    # no credit) rather than risk a double-credit race.
                    #
                    # Uses claim_locked() (not claim()) because we are already
                    # inside this outer get_db_session() loop — claim() opens
                    # and closes its OWN nested session, which would close the
                    # shared scoped_session out from under this loop and
                    # detach `transaction`/`pending_transactions`.
                    try:
                        from services.idempotency import claim_locked as _idem_claim_locked
                        if not _idem_claim_locked(session, "crypto_verify", f"tx:{transaction.id}"):
                            continue  # already claimed by another run/path
                    except Exception:
                        logger.error(
                            "idempotency.claim_locked raised for crypto_verify tx %s — "
                            "skipping this cycle (fail closed)", transaction.id,
                            exc_info=True,
                        )
                        continue

                    # Atomic status flip — idempotent guard against double-credit.
                    flipped = session.query(Transaction).filter(
                        Transaction.id == transaction.id,
                        Transaction.status == TransactionStatus.PENDING,
                    ).update(
                        {
                            Transaction.status: TransactionStatus.COMPLETED,
                            Transaction.completed_at: datetime.utcnow(),
                        },
                        synchronize_session=False,
                    )
                    if flipped == 0:
                        continue  # Already processed by another path — skip

                    # Atomic wallet credit — writes WalletLedger row in same session.
                    try:
                        from services.wallet import credit_locked as _cl, WalletError as _WE
                        _cl(
                            session, transaction.user_id, transaction.amount,
                            reason=f"{transaction.payment_method.value} top-up #{transaction.id}",
                            actor_type="system", ref_type="bg_poll",
                            ref_id=str(transaction.id),
                        )
                    except Exception:
                        logger.exception(
                            "credit_locked failed for polled tx %s — skipping",
                            transaction.id,
                        )
                        session.rollback()
                        continue
                    session.commit()

                    user = session.query(User).filter_by(id=transaction.user_id).first()
                    if user:
                        payment_notifications.append({
                            'user_telegram_id': user.telegram_id,
                            'amount': transaction.amount,
                            'new_balance': user.wallet_balance,
                            'transaction_id': transaction.id,
                            'payment_method': transaction.payment_method.value
                        })

        return payment_notifications

    # Run blocking database operations in thread pool
    notifications = await asyncio.to_thread(_check_and_process_payments_sync)

    # Send notifications asynchronously
    for notif in notifications:
        # Notify user
        _pm_key = notif['payment_method'].lower() if notif['payment_method'] else None
        _pm_label = pui.gateway_meta(_pm_key, fallback_label=notif['payment_method'])[0]
        user_message = sanitize_message(
            pui.deposit_success_card(
                amount=format_price(notif['amount']),
                payment_method=_pm_label,
                deposit_id=pui.format_deposit_id(notif['transaction_id']),
            )
        )

        try:
            await context.bot.send_message(
                chat_id=notif['user_telegram_id'],
                text=user_message,
                reply_markup=pui.deposit_success_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            logger.warning('Ignored Telegram/API error', exc_info=True)

        # Notify admin
        admin_message = pui.admin_review_card(
            gateway_key=notif['payment_method'].lower() if notif['payment_method'] else None,
            gateway_label_override=notif['payment_method'],
            amount=format_price(notif['amount']),
            order_id=notif['transaction_id'],
            user_id=notif['user_telegram_id'],
            status_key="approved",
        )

        await notify_admin(context, admin_message, parse_mode='HTML')


async def check_expired_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to mark expired payment transactions (non-blocking).

    IMPORTANT: For automated gateways (NOWPayments, Cryptomus) we always
    verify with the upstream API before cancelling.  A payment that was
    confirmed on the gateway side before expiry must be credited — not
    silently dropped — even if the regular polling missed it.
    """
    import asyncio

    # Gateway methods that can self-report confirmed payments via API.
    AUTOMATED_GATEWAY_METHODS = {
        PaymentMethod.NOWPAYMENTS,
        PaymentMethod.CRYPTOMUS,
        PaymentMethod.CRYPTO_WALLET,  # CryptoBot
    }

    def _check_expired_sync():
        """Synchronous database operations run in thread pool."""
        expired_notifications = []
        late_credit_notifications = []

        with get_db_session() as session:
            # Only PENDING, not-yet-notified orders are even candidates here.
            # `expiry_notified` (not just `status`) is the skip condition —
            # it's the durable "already handled" marker that survives a bot
            # restart or an overlapping run of this same job, whereas relying
            # on `status` alone left a window between the CANCELLED commit
            # and the outbound send_message() where a re-run (or a second
            # process) could pick the row up again.
            pending_transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING,
                expiry_notified=False,
            ).all()

            for transaction in pending_transactions:
                if not (transaction.expires_at and datetime.utcnow() > transaction.expires_at):
                    continue  # Not expired yet — handled by check_pending_payments

                # ── Automated gateway: check upstream before cancelling ─────
                # If the user actually paid before the clock ran out we MUST
                # credit them even though the expiry window has passed.
                if transaction.payment_method in AUTOMATED_GATEWAY_METHODS:
                    is_paid = False
                    try:
                        if transaction.payment_method == PaymentMethod.NOWPAYMENTS:
                            svc = NowPaymentsService()
                            is_paid = svc.check_payment_status(
                                transaction.crypto_address, transaction.amount
                            )
                        elif transaction.payment_method == PaymentMethod.CRYPTOMUS:
                            svc = CryptomusPaymentService()
                            is_paid = svc.check_payment_status(
                                transaction.crypto_address, transaction.amount
                            )
                        elif transaction.payment_method == PaymentMethod.CRYPTO_WALLET:
                            svc = CryptoBotService()
                            is_paid = svc.check_payment_status(
                                transaction.crypto_address, transaction.amount
                            )
                    except Exception:
                        logger.exception(
                            "[EXPIRY CHECK] gateway status query failed for tx %s — "
                            "will cancel to unblock user (safe retry on next expiry run)",
                            transaction.id,
                        )

                    if is_paid:
                        # Late credit — the gateway confirmed payment but the
                        # regular polling missed it (e.g. API hiccup, or the
                        # WEBHOOK_URL was not configured).
                        logger.info(
                            "[EXPIRY LATE CREDIT] tx=%s user=%s amount=%.2f — "
                            "gateway confirmed after expiry, crediting now",
                            transaction.id, transaction.user_id, transaction.amount,
                        )
                        try:
                            from services.idempotency import claim_locked as _idem_claim_locked
                            if not _idem_claim_locked(session, "expiry_late_credit",
                                                      f"tx:{transaction.id}"):
                                logger.info(
                                    "[EXPIRY LATE CREDIT] tx=%s already claimed — skipping",
                                    transaction.id,
                                )
                                continue
                        except Exception:
                            logger.exception(
                                "[EXPIRY LATE CREDIT] idempotency check failed tx=%s — skipping",
                                transaction.id,
                            )
                            continue

                        flipped = session.query(Transaction).filter(
                            Transaction.id == transaction.id,
                            Transaction.status == TransactionStatus.PENDING,
                        ).update(
                            {
                                Transaction.status: TransactionStatus.COMPLETED,
                                Transaction.completed_at: datetime.utcnow(),
                            },
                            synchronize_session=False,
                        )
                        if flipped == 0:
                            continue  # Already handled by another path

                        try:
                            from services.wallet import credit_locked as _cl
                            _cl(
                                session, transaction.user_id, transaction.amount,
                                reason=(
                                    f"{transaction.payment_method.value} late credit "
                                    f"#{transaction.id}"
                                ),
                                actor_type="system", ref_type="expiry_late_credit",
                                ref_id=str(transaction.id),
                            )
                            session.commit()
                        except Exception:
                            logger.exception(
                                "[EXPIRY LATE CREDIT] credit_locked failed tx=%s",
                                transaction.id,
                            )
                            session.rollback()
                            continue

                        user = session.query(User).filter_by(id=transaction.user_id).first()
                        if user:
                            late_credit_notifications.append({
                                'telegram_id': user.telegram_id,
                                'amount': transaction.amount,
                                'new_balance': user.wallet_balance,
                                'transaction_id': transaction.id,
                                'created_at': transaction.created_at,
                                'payment_method': transaction.payment_method.value if transaction.payment_method else None,
                            })
                        continue  # Do NOT cancel — we just credited

                # ── Cancel the expired transaction ────────────────────────────
                # An expired order must never be left PENDING (it blocks new
                # orders). Per lifecycle: expiry → CANCELLED.
                #
                # Atomic conditional UPDATE: flips status AND claims
                # expiry_notified in the SAME statement, gated on the row
                # still being PENDING/un-notified. This is the single choke
                # point that guarantees exactly one "Payment Expired" send
                # per order — a second worker, a re-run after a crash, or
                # this same loop somehow revisiting the row will all get
                # `claimed == 0` and skip the notification below.
                claimed = session.query(Transaction).filter(
                    Transaction.id == transaction.id,
                    Transaction.status == TransactionStatus.PENDING,
                    Transaction.expiry_notified.is_(False),
                ).update(
                    {
                        Transaction.status: TransactionStatus.CANCELLED,
                        Transaction.expiry_notified: True,
                    },
                    synchronize_session=False,
                )
                session.commit()

                if claimed == 0:
                    continue  # Already claimed/handled elsewhere — skip

                user = session.query(User).filter_by(id=transaction.user_id).first()
                if user:
                    expired_notifications.append({
                        'telegram_id': user.telegram_id,
                        'amount': transaction.amount,
                        'transaction_id': transaction.id,
                        'created_at': transaction.created_at,
                        'payment_method': transaction.payment_method.value if transaction.payment_method else None,
                    })

        return expired_notifications, late_credit_notifications

    # Run blocking database operations in thread pool
    expired_notifications, late_credit_notifications = await asyncio.to_thread(
        _check_expired_sync
    )

    # ── Notify users whose orders expired ────────────────────────────────────
    for notif in expired_notifications:
        message = sanitize_message(
            pui.user_payment_card(
                gateway_key=notif.get('payment_method'),
                stage="expired",
                amount=format_price(notif['amount']),
                order_id=notif['transaction_id'],
                created_at=notif.get('created_at'),
                note="This payment window closed before we received your funds. "
                     "No balance was deducted — start a new deposit whenever you're ready.",
            )
        )

        try:
            await context.bot.send_message(
                chat_id=notif['telegram_id'],
                text=message,
                reply_markup=pui.payment_expired_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            # User may have blocked the bot
            pass

    # ── Enterprise Admin Notification: payment expired (best-effort) ─────────
    for notif in expired_notifications:
        try:
            from services.notifications import notify_admins as _notify_admins
            import asyncio as _asyncio
            _asyncio.create_task(_notify_admins(
                context.bot,
                "payment_expired",
                pui.admin_review_card(
                    gateway_key=notif.get('payment_method'),
                    amount=format_price(notif['amount']),
                    order_id=notif['transaction_id'],
                    created_at=notif.get('created_at'),
                    user_id=notif['telegram_id'],
                    status_key="expired",
                ),
            ))
        except Exception:
            pass

    # ── Notify users whose payment was credited late ──────────────────────────
    for notif in late_credit_notifications:
        _lc_gateway = notif.get('payment_method')
        message = sanitize_message(
            pui.deposit_success_card(
                amount=format_price(notif['amount']),
                payment_method=pui.gateway_meta(_lc_gateway)[0],
                deposit_id=pui.format_deposit_id(notif['transaction_id'], notif.get('created_at')),
            )
        )

        try:
            await context.bot.send_message(
                chat_id=notif['telegram_id'],
                text=message,
                reply_markup=pui.deposit_success_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            pass


async def buy_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the direct purchase flow - ask for quantity."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    # Extract product_id from callback data (format: buy_123)
    try:
        product_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return ConversationHandler.END

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        if not product.is_active:
            try:
                await query.edit_message_text("❌ This product is no longer available.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Use inventory service for real available count (excludes active reservations)
        from services import inventory as _inv_svc
        from services.quantity_presets import build_keyboard as _build_qty_kb
        available = _inv_svc.count_available(product_id)

        if available == 0:
            try:
                await query.edit_message_text("❌ This product is out of stock.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Store product info in context for later
        context.user_data['purchase_product_id'] = product_id
        context.user_data['purchase_product_name'] = product.name
        context.user_data['purchase_product_price'] = product.price
        context.user_data['purchase_product_stock'] = available
        context.user_data['purchase_product_type'] = product.product_type

        # V18 — track recently viewed
        try:
            from handlers.feature_handlers import track_recently_viewed
            track_recently_viewed(update.effective_user.id, product_id)
        except Exception:
            pass

        # For file products, quantity is always 1
        if product.product_type == ProductType.FILE:
            context.user_data['purchase_quantity'] = 1
            # Skip quantity input, go straight to confirmation
            return await show_purchase_confirmation(update, context)

        # For key products, show dynamic quantity preset keyboard
        message = (
            f"🛒 {product.name}\n"
            f"\n"
            f"💰 Price: {format_price(product.price)}\n"
            f"🟢 {available} In Stock\n"
            f"\n"
            f"Select a quantity below or enter a custom amount (1–{available})."
        )

        # Build dynamic preset keyboard from quantity_presets service
        qty_markup = _build_qty_kb(product, available=available, product_id=product_id)

        # V18 — inject Wishlist / Price-Alert toggle buttons above the Cancel row
        try:
            from handlers.feature_handlers import build_product_feature_buttons
            from telegram import InlineKeyboardMarkup as _IKM
            _feat_rows = build_product_feature_buttons(update.effective_user.id, product_id)
            if _feat_rows:
                _old_kb = list(qty_markup.inline_keyboard)
                # Put feature rows between last qty preset row and the cancel row
                _cancel_rows = [r for r in _old_kb if any(
                    getattr(b, 'callback_data', '') in ('cancel_purchase', 'cancel')
                    for b in r
                )]
                _main_rows = [r for r in _old_kb if r not in _cancel_rows]
                qty_markup = _IKM(_main_rows + _feat_rows + _cancel_rows)
        except Exception:
            pass

        # If coming from a photo message, delete it and create new text message
        if query.message.photo:
            await query.message.delete()
            await query.message.reply_text(message, reply_markup=qty_markup)
        else:
            try:
                await query.edit_message_text(message, reply_markup=qty_markup)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

        return PURCHASE_QUANTITY


async def purchase_quantity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quantity input for direct purchase."""
    quantity_str = update.message.text.strip()

    # Validate quantity
    try:
        quantity = int(quantity_str)
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    # Use stored available count (set by buy_product_start via inventory service)
    product_stock = context.user_data.get('purchase_product_stock', 0)

    if quantity < 1:
        await update.message.reply_text(
            "❌ Quantity must be at least 1.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    if quantity > product_stock:
        await update.message.reply_text(
            f"❌ Not enough stock. Maximum available: {product_stock}",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    # Store quantity and show confirmation
    context.user_data['purchase_quantity'] = quantity
    return await show_purchase_confirmation(update, context, is_message=True)


async def show_purchase_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, is_message=False):
    """Show purchase confirmation with total price."""
    product_id = context.user_data.get('purchase_product_id')
    product_name = context.user_data.get('purchase_product_name')
    product_price = context.user_data.get('purchase_product_price')
    quantity = context.user_data.get('purchase_quantity')

    subtotal = product_price * quantity
    coupon_discount = float(context.user_data.get('purchase_coupon_discount', 0) or 0)
    coupon_code = context.user_data.get('purchase_coupon_code')
    total = max(0.0, subtotal - coupon_discount)
    telegram_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            if is_message:
                await update.message.reply_text("❌ User not found.")
            else:
                try:
                    await update.callback_query.edit_message_text("❌ User not found.")
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            return ConversationHandler.END

        wallet_balance = user.wallet_balance
        has_sufficient_balance = wallet_balance >= total

        remaining_after = wallet_balance - total

        if has_sufficient_balance:
            balance_section = (
                f"👛 Wallet Balance: {format_price(wallet_balance)}\n"
                f"💵 Remaining Balance: {format_price(remaining_after)}"
            )
        else:
            shortfall = total - wallet_balance
            balance_section = (
                f"⚠️ Insufficient Balance\n"
                f"👛 Wallet Balance: {format_price(wallet_balance)}\n"
                f"💳 Need {format_price(shortfall)} more — please top up first."
            )

        discount_line = ""
        if coupon_discount > 0 and coupon_code:
            discount_line = (
                f"🎟 Coupon ({coupon_code}): -{format_price(coupon_discount)}\n"
                f"🧾 Subtotal: {format_price(subtotal)}\n"
            )

        message = (
            f"🛒 Purchase Summary\n"
            f"\n"
            f"📦 Product: {product_name}\n"
            f"🔢 Quantity: {quantity}\n"
            f"💰 Unit Price: {format_price(product_price)}\n"
            f"{discount_line}"
            f"🧾 Total: {format_price(total)}\n"
            f"\n"
            f"{balance_section}"
        )

        if has_sufficient_balance:
            keyboard = [
                [InlineKeyboardButton(f"✅ Pay {format_price(total)}",
                                      callback_data=f"confirm_purchase_{product_id}_{quantity}")],
            ]
            if not coupon_code:
                keyboard.append([InlineKeyboardButton("🎟 Have a Coupon?", callback_data="apply_coupon")])
            else:
                keyboard.append([InlineKeyboardButton("🗑 Remove Coupon", callback_data="remove_coupon")])
            keyboard.append([
                InlineKeyboardButton("⬅ Back", callback_data=f"buy_{product_id}"),
                InlineKeyboardButton("❌ Close", callback_data="cancel_purchase"),
            ])
        else:
            keyboard = [
                [InlineKeyboardButton("💰 Top Up Wallet", callback_data="topup")],
                [
                    InlineKeyboardButton("⬅ Back", callback_data=f"buy_{product_id}"),
                    InlineKeyboardButton("❌ Close", callback_data="cancel_purchase"),
                ],
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        if is_message:
            await update.message.reply_text(message, reply_markup=reply_markup)
        else:
            query = update.callback_query
            if query.message.photo:
                await query.message.delete()
                await query.message.reply_text(message, reply_markup=reply_markup)
            else:
                try:
                    await query.edit_message_text(message, reply_markup=reply_markup)
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise

        return ConversationHandler.END


async def remove_coupon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear coupon from context and re-render the confirmation screen."""
    query = update.callback_query
    await query.answer("Coupon removed")
    for k in ('purchase_coupon_id', 'purchase_coupon_code', 'purchase_coupon_discount'):
        context.user_data.pop(k, None)
    await show_purchase_confirmation(update, context, is_message=False)


async def confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the confirmed purchase."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # Extract product_id and quantity from callback data (format: confirm_purchase_123_5)
    try:
        parts = query.data.split("_")
        product_id = int(parts[2])
        quantity = int(parts[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    # Idempotency — reject duplicate confirm taps (double callback delivery)
    try:
        from services.idempotency import claim as _idem_claim
        _upd_id = str(update.update_id or getattr(query, "id", None) or "")
    except ImportError:
        _idem_claim = None
        _upd_id = ""
    if _idem_claim and _upd_id:
        with _idem_claim("confirm_purchase", f"tg{telegram_id}:u{_upd_id}") as _ok:
            if not _ok:
                await query.answer("This order is already being processed.", show_alert=True)
                return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        if not product.is_active:
            try:
                await query.edit_message_text("❌ This product is no longer available.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        if product.stock_count < quantity:
            try:
                await query.edit_message_text(f"❌ Not enough stock. Only {product.stock_count} available.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        subtotal = product.price * quantity
        coupon_id = context.user_data.get('purchase_coupon_id')
        user_db_id = user.id  # snapshot for post-commit redemption logging

        # Fix: Coupon revalidation from DB — never trust user_data cache
        coupon_discount = 0.0
        if coupon_id:
            _c = session.query(Coupon).filter_by(id=coupon_id).first()
            _cerr = None
            if not _c:
                _cerr = "not found"
            elif not _c.is_active:
                _cerr = "inactive"
            elif _c.expires_at and _c.expires_at < datetime.utcnow():
                _cerr = "expired"
            elif _c.max_uses and _c.used_count >= _c.max_uses:
                _cerr = "limit reached"
            elif _c.min_order_amount and float(subtotal) < _c.min_order_amount:
                _cerr = "minimum order not met"
            elif _c.per_user_limit:
                _used = session.query(CouponRedemption).filter_by(
                    coupon_id=_c.id, user_id=user_db_id
                ).count()
                if _used >= _c.per_user_limit:
                    _cerr = "per-user limit reached"
            if _cerr:
                logger.info("Buy Now coupon %s invalidated at confirm: %s", coupon_id, _cerr)
                for _k in ('purchase_coupon_id', 'purchase_coupon_code',
                           'purchase_coupon_discount'):
                    context.user_data.pop(_k, None)
                coupon_id = None
            else:
                if _c.discount_type == DiscountType.PERCENT:
                    coupon_discount = float(subtotal) * (_c.discount_value / 100.0)
                else:
                    coupon_discount = float(_c.discount_value)
                coupon_discount = round(min(coupon_discount, float(subtotal)), 2)
        coupon_discount = min(coupon_discount, float(subtotal))
        total = max(0.0, float(subtotal) - coupon_discount)

        # Snapshot balance for messaging BEFORE any atomic update
        current_balance = float(user.wallet_balance or 0)
        if current_balance < total:
            try:
                await query.edit_message_text(
                    f"❌ Insufficient balance.\n💰 Your balance: {format_price(current_balance)}\n💵 Required: {format_price(total)}"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # ────────────────────────────────────────────────────────────
        # Atomic reservations — prevent race conditions with concurrent
        # purchases (double-spend of wallet balance / over-selling stock).
        # ────────────────────────────────────────────────────────────
        user_pk = user.id
        product_pk = product.id
        product_name = product.name
        product_type_val = product.product_type
        product_download_link = product.download_link
        product_price_val = float(product.price)

        # === Inventory reservation for KEY products ===
        # Reserve BEFORE wallet debit so stock is locked before money moves.
        # reserve() opens its own scoped session (closes/reopens the outer one),
        # which is safe because all needed data is captured as local scalars above.
        _inv_reservation_id = None
        from services import inventory as _inv_svc
        if product_type_val in _inv_svc.KEY_BACKED_TYPES:
            try:
                _inv_res = _inv_svc.reserve(user_pk, product_pk, quantity)
                _inv_reservation_id = _inv_res.id
            except _inv_svc.ReservationError as _re:
                try:
                    await query.edit_message_text(
                        f"❌ Stock no longer available: {_re}\nPlease try again.",
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return

        # 1) Atomic wallet debit — succeeds only if balance is still >= total.
        debited = session.query(User).filter(
            User.id == user_pk,
            User.wallet_balance >= total,
        ).update(
            {User.wallet_balance: User.wallet_balance - total},
            synchronize_session=False,
        )
        if debited == 0:
            session.rollback()
            try:
                await query.edit_message_text(
                    "❌ Insufficient balance. Please top up and try again.",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # 2) Atomic stock reservation — for FILE-type products only.
        # KEY-backed types (KEY, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER): inventory
        # already reserved via reserve() above (ProductKey rows locked).
        if product_type_val not in _inv_svc.KEY_BACKED_TYPES:
            reserved = session.query(Product).filter(
                Product.id == product_pk,
                Product.stock_count >= quantity,
            ).update(
                {Product.stock_count: Product.stock_count - quantity},
                synchronize_session=False,
            )
            if reserved == 0:
                # Refund the wallet atomically and abort.
                session.query(User).filter(User.id == user_pk).update(
                    {User.wallet_balance: User.wallet_balance + total},
                    synchronize_session=False,
                )
                session.commit()
                try:
                    await query.edit_message_text("❌ Not enough stock available. Please try a smaller quantity.")
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return

        # Persist wallet/stock reservation before delivery attempts.
        session.commit()

        # Create order at PROCESSING status (lifecycle transitions below)
        order = Order(
            user_id=user_pk,
            total_amount=total,
            status=OrderStatus.PROCESSING,
        )
        session.add(order)
        session.commit()
        session.refresh(order)

        # Attach the reservation to this order so delivery_service can find
        # the EXISTING reservation (via _find_active_reservation) instead of
        # pulling fresh unreserved ProductKey rows.
        if _inv_reservation_id:
            session.query(StockReservation).filter(
                StockReservation.id == _inv_reservation_id
            ).update({StockReservation.order_id: order.id}, synchronize_session=False)
            session.commit()

        # Lifecycle: PROCESSING
        try:
            from services import order_lifecycle as _lc
            _lc.transition(order.id, OrderLifecycleStatus.PROCESSING)
        except Exception:
            logger.exception("Lifecycle PROCESSING failed for order %s", order.id)

        order_item = OrderItem(
            order_id=order.id,
            product_id=product_pk,
            quantity=quantity,
            price=product_price_val,
        )

        order_details = ""
        bulk_keys = None
        bulk_product_name = None
        _v11_oversized_content = None
        try:
            # V11 — try the new dispatcher first. It handles the 10 new
            # product types; KEY/FILE fall through to the legacy branches
            # below (dispatcher returns handled=False for those).
            _dispatcher_result = None
            if product_type_val not in (ProductType.KEY, ProductType.FILE):
                # OrderItem must exist before dispatcher runs so it can write
                # ``delivered_asset``. Add + flush without committing.
                session.add(order_item)
                session.flush()
                try:
                    from services.delivery_service import dispatch as _v11_dispatch
                    _dispatcher_result = _v11_dispatch(order.id, session=session)
                except Exception as _e:
                    logging.getLogger(__name__).exception(
                        "V11 dispatcher raised for order %s: %s", order.id, _e
                    )
                if _dispatcher_result and _dispatcher_result.handled:
                    if _dispatcher_result.success or _dispatcher_result.queued:
                        from services.purchase_success import is_delivery_oversized
                        if _dispatcher_result.success and is_delivery_oversized(
                            _dispatcher_result.user_message
                        ):
                            # Multi-quantity delivery (e.g. many ACCOUNT_LOGIN /
                            # REDEEM_LINK / VOUCHER items) too large to safely
                            # inline in one Telegram message — defer to a .txt
                            # file the same way legacy bulk KEY delivery already
                            # does, instead of risking a Message_too_long failure.
                            _v11_oversized_content = _dispatcher_result.user_message
                            order_details = (
                                f"📦 {product_name} (x{quantity})\n"
                                f"📎 Delivered as attached .txt file below.\n"
                            )
                        else:
                            order_details = (
                                f"📦 {product_name} (x{quantity})\n"
                                f"{_dispatcher_result.user_message}\n"
                            )
                    else:
                        raise RuntimeError(
                            _dispatcher_result.error or "delivery failed"
                        )
                    # Refresh the ORM copy so subsequent code sees updates
                    # persisted by the dispatcher.
                    session.expire(order_item)

            if _dispatcher_result is None or not _dispatcher_result.handled:
                if product_type_val == ProductType.KEY:
                    # Use inventory.consume() when a reservation was created above;
                    # falls back to assign_product_keys() for legacy/admin-created orders.
                    if _inv_reservation_id:
                        from services import inventory as _inv_svc
                        keys = _inv_svc.consume(_inv_reservation_id, order.id)
                    else:
                        keys = assign_product_keys(session, product_pk, quantity, order.id)
                    if not keys or len(keys) < quantity:
                        raise RuntimeError(
                            f"Only {len(keys) if keys else 0}/{quantity} keys could be assigned"
                        )
                    order_item.delivered_asset = "\n".join(keys)
                    from utils.bot_config import cfg as _cfg
                    _bulk_th = _cfg.get_int("bulk_delivery_threshold", BULK_DELIVERY_THRESHOLD)
                    if quantity > _bulk_th:
                        bulk_keys = keys
                        bulk_product_name = product_name
                        order_details = (
                            f"📦 {product_name} (x{quantity})\n"
                            f"🔐 {quantity} keys delivered as attached .txt file below.\n"
                        )
                    else:
                        # V17 — Formatted Account Delivery: if the admin set a
                        # delivery_format_template for this product, render
                        # each key through it. Falls back to the exact legacy
                        # raw-text message when no template is configured.
                        _tmpl = None
                        try:
                            from database import Product as _ProductModel
                            _tmpl_product = session.query(_ProductModel).filter_by(id=product_pk).first()
                            _tmpl = getattr(_tmpl_product, "delivery_format_template", None) if _tmpl_product else None
                        except Exception:
                            _tmpl = None
                        if _tmpl:
                            from services.structured_delivery import render_delivery_message
                            _rendered = "\n\n".join(render_delivery_message(_tmpl, k) for k in keys)
                            order_details = f"📦 {product_name} (x{quantity})\n{_rendered}\n"
                        else:
                            order_details = f"📦 {product_name} (x{quantity})\n🔐 Keys:\n{order_item.delivered_asset}\n"

                elif product_type_val == ProductType.FILE:
                    if not product_download_link:
                        raise RuntimeError("Product download link is not configured")
                    order_item.delivered_asset = product_download_link
                    order_details = f"📦 {product_name}\n🔗 Download: {order_item.delivered_asset}\n"

                session.add(order_item)


            # Award loyalty points (best-effort — never blocks the purchase)
            try:
                from handlers.loyalty_handlers import award_loyalty_points
                _user_row = session.query(User).filter_by(id=user_pk).first()
                if _user_row is not None:
                    award_loyalty_points(session, _user_row, order.id, total)
            except Exception:
                import logging as _lg
                _lg.getLogger(__name__).exception("Loyalty award failed")

            session.commit()

            # Capture delivered_asset NOW before lifecycle transitions reuse/close
            # the shared scoped_session and detach ORM objects.
            _captured_delivered = order_item.delivered_asset

            # For bulk deliveries (>threshold items), the success message shown
            # to the user must NOT embed all keys inline — those are delivered
            # via TXT file below. Use a short summary placeholder instead.
            _display_delivered = (
                f"📎 {len(bulk_keys)} items delivered as attached .txt file below."
                if bulk_keys else
                (f"📎 {quantity} item(s) delivered as attached .txt file below."
                 if _v11_oversized_content else _captured_delivered)
            )

            # Lifecycle: DELIVERED → COMPLETED
            try:
                from services import order_lifecycle as _lc
                _lc.transition(order.id, OrderLifecycleStatus.DELIVERED, bot=None)
                _lc.transition(order.id, OrderLifecycleStatus.COMPLETED, bot=None,
                               send_invoice=False)
            except Exception:
                logger.exception("Lifecycle COMPLETED failed for order %s", order.id)

            # Coupon redemption (atomic used_count increment inside helper)
            if coupon_id and coupon_discount > 0:
                try:
                    from handlers.coupon_handlers import record_coupon_redemption
                    record_coupon_redemption(coupon_id, user_db_id, order.id, coupon_discount)
                except Exception:
                    import logging as _lg
                    _lg.getLogger(__name__).exception("Coupon redemption log failed")
            for _k in ('purchase_coupon_id', 'purchase_coupon_code', 'purchase_coupon_discount'):
                context.user_data.pop(_k, None)

            # V18 — save QuickBuyConfig so user can repeat this purchase in one click
            try:
                from handlers.feature_handlers import save_quick_buy_config
                save_quick_buy_config(
                    telegram_id=telegram_id,
                    product_id=product_pk,
                    payment_method="wallet_balance",
                    quantity=quantity,
                )
            except Exception:
                pass  # never block purchase on feature tracking

        except Exception as delivery_err:
            # Delivery failed AFTER wallet+stock were reserved →
            # atomically refund wallet + restore stock, mark order failed.
            import logging as _lg
            _lg.getLogger(__name__).exception(
                "Delivery failed for order %s: %s", order.id, delivery_err
            )
            try:
                session.rollback()
            except Exception:
                _lg.getLogger(__name__).exception("Session rollback failed")
            try:
                session.query(User).filter(User.id == user_pk).update(
                    {User.wallet_balance: User.wallet_balance + total},
                    synchronize_session=False,
                )
                # Restore stock_count for FILE-type only; KEY-backed reservation released below.
                if product_type_val not in _inv_svc.KEY_BACKED_TYPES:
                    session.query(Product).filter(Product.id == product_pk).update(
                        {Product.stock_count: Product.stock_count + quantity},
                        synchronize_session=False,
                    )
                session.commit()
            except Exception:
                _lg.getLogger(__name__).exception("Compensation (refund/restock) failed")
                try:
                    session.rollback()
                except Exception:
                    logger.warning('Ignored Telegram/API error', exc_info=True)
            # Release KEY reservation on failure
            if _inv_reservation_id:
                try:
                    from services import inventory as _inv_svc
                    _inv_svc.release_for_order(order.id, reason="delivery_failed")
                except Exception:
                    _lg.getLogger(__name__).exception(
                        "release_for_order failed for order %s", order.id)
            # Lifecycle: FAILED (transition() syncs order.status via _LEGACY_MAP)
            try:
                from services import order_lifecycle as _lc
                _lc.transition(order.id, OrderLifecycleStatus.FAILED,
                               reason=str(delivery_err)[:200])
            except Exception:
                logger.exception("Lifecycle FAILED failed for order %s", order.id)
            try:
                await query.edit_message_text(
                    "❌ Order Failed\n"
                    "\n"
                    "We couldn't complete your order.\n\n"
                    "💰 Refund\n"
                    "Your wallet has been refunded in full.\n\n"
                    "Please try again in a moment, or contact support "
                    "if the issue continues.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")
                    ]])
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            await notify_admin(
                context,
                f"❗️ Delivery failed for user {telegram_id} on product #{product_id} "
                f"(qty {quantity}). Wallet auto-refunded. Reason: {delivery_err}"
            )
            return


        # ── Enterprise Purchase Success Experience ────────────────────────────
        # 1) Generate & store order display ID  ORD-YYYYMMDD-NNNNNN
        from utils.helpers import format_order_id as _fmt_order_id_fallback
        _receipt_number = _fmt_order_id_fallback(order.id, getattr(order, "created_at", None))
        try:
            from services.purchase_success import get_or_create_receipt
            _receipt_number = get_or_create_receipt(order.id, user_pk)
        except Exception:
            logger.exception("Receipt generation failed for order %s", order.id)

        # 2) Build the single consolidated success message
        try:
            from services.purchase_success import build_success_text, build_success_keyboard
            user_message = build_success_text(
                order_id=order.id,
                product_name=product_name,
                quantity=quantity,
                total=total,
                receipt_number=_receipt_number,
                delivered_asset=_display_delivered,
                product_type=(str(product_type_val.value)
                              if product_type_val else None),
                product_id=product_pk,
                purchase_date=datetime.utcnow(),
            )
            reply_markup = build_success_keyboard(
                order_id=order.id,
                product_id=product_pk,
                delivered_asset=_display_delivered,
            )
        except Exception:
            logger.exception(
                "Success message builder failed for order %s — using fallback",
                order.id,
            )
            user_message = (
                f"✅ Payment Successful\n"
                f"\n"
                f"🧾 Order #{order.id}\n"
                f"📄 Receipt: {_receipt_number}\n"
                f"💰 Amount Paid\n{format_price(total)}\n\n"
                f"{order_details}\n"
                f"Thank you for your purchase!"
            )
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
                InlineKeyboardButton("📦 My Orders", callback_data="order_history"),
            ]])

        try:
            await query.edit_message_text(user_message, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

        # For bulk orders, send keys as a .txt file — with auto-refund on failure
        if bulk_keys:
            safe_name = "".join(c for c in bulk_product_name if c.isalnum() or c in ("-", "_"))[:40] or "product"
            filename = f"order_{order.id}_{safe_name}_keys.txt"
            tmp_path = os.path.join(tempfile.gettempdir(), filename)
            delivery_ok = False
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(bulk_keys))
                with open(tmp_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=telegram_id,
                        document=InputFile(f, filename=filename),
                        caption=f"🔐 {len(bulk_keys)} keys for order #{order.id}"
                    )
                delivery_ok = True
                with open(tmp_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=app_settings.ADMIN_TELEGRAM_ID,
                        document=InputFile(f, filename=filename),
                        caption=f"📎 Bulk delivery — order #{order.id} ({len(bulk_keys)} keys)"
                    )
            except Exception as e:
                import logging as _lg
                _lg.getLogger(__name__).exception("Bulk delivery failed for order %s", order.id)
                if not delivery_ok:
                    # User never got the file → atomic auto-refund + restock
                    try:
                        session.query(User).filter(User.id == user_pk).update(
                            {User.wallet_balance: User.wallet_balance + total},
                            synchronize_session=False,
                        )
                        session.query(Product).filter(Product.id == product_pk).update(
                            {Product.stock_count: Product.stock_count + quantity},
                            synchronize_session=False,
                        )
                        _o = session.query(Order).filter_by(id=order.id).first()
                        if _o is not None:
                            _o.status = OrderStatus.REFUNDED
                        session.commit()
                        await context.bot.send_message(
                            chat_id=telegram_id,
                            text=sanitize_message(
                                f"❌ Order Failed\n"
                                f"\n"
                                f"🧾 Order #{order.id}\n"
                                f"Delivery couldn't be completed.\n\n"
                                f"💰 Refund\n{format_price(total)} refunded to your wallet."
                            )
                        )
                    except Exception:
                        _lg.getLogger(__name__).exception("Auto-refund after bulk-delivery failure crashed")
                        try:
                            session.rollback()
                        except Exception:
                            logger.warning('Ignored Telegram/API error', exc_info=True)
                await notify_admin(
                    context,
                    f"❗️ Bulk file delivery failed for order #{order.id}: {e}"
                )
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        # Universal fallback for the 11 newer dispatcher-backed product types
        # (see services/delivery_service.py) — same safety net as bulk_keys
        # above, generalized so ACCOUNT_LOGIN/REDEEM_LINK/VOUCHER/etc. multi-
        # quantity purchases never risk a Message_too_long failure.
        if _v11_oversized_content:
            from services.purchase_success import send_delivery_as_file
            try:
                await send_delivery_as_file(
                    context.bot, telegram_id, order.id, product_name,
                    _v11_oversized_content,
                    caption=f"📎 {quantity} item(s) for order #{order.id}",
                    admin_chat_id=app_settings.ADMIN_TELEGRAM_ID,
                )
            except Exception:
                logger.exception(
                    "Oversized V11 delivery file send failed for order %s", order.id
                )
                await notify_admin(
                    context,
                    f"❗️ Oversized delivery file failed to send for order #{order.id}"
                )

        # ── Single merged admin notification (one message per completed order) ──
        try:
            import asyncio as _asyncio
            from services.notifications import notify_admins as _notify_admins
            from utils.notify_format import render as _render_notif, utc_now_str as _ts
            _af_cname = getattr(update.effective_user, 'full_name', '') or str(telegram_id)
            _af_cuname = getattr(update.effective_user, 'username', '')
            _delivery_type_str = (
                "File"
                if (bulk_keys or _v11_oversized_content) else
                "Instant"
            )
            from utils.helpers import format_order_id as _fmt_order_id
            _order_display_id = _fmt_order_id(order.id, getattr(order, 'created_at', None))
            _merged_notif = _render_notif("✅", "Order Completed", [
                ("Order ID", _order_display_id),
                ("Customer", f"{_af_cname} (@{_af_cuname})" if _af_cuname else _af_cname),
                ("Telegram ID", f"<code>{telegram_id}</code>"),
                ("Product", product_name),
                ("Quantity", quantity),
                ("Amount", format_price(total)),
                ("Delivery", _delivery_type_str),
            ], _ts())
            _asyncio.create_task(_notify_admins(
                context.bot,
                "order_delivered",
                _merged_notif,
            ))
        except Exception:
            pass

        # Activity Feed: new order + delivery (best-effort, non-blocking)
        try:
            import asyncio as _asyncio
            from services.activity_feed import post_event as _af_post, EVENT_NEW_ORDER
            _af_customer_name = getattr(update.effective_user, 'full_name', '') or ''
            _af_customer_uname = getattr(update.effective_user, 'username', '') or ''
            _asyncio.create_task(_af_post(context.bot, EVENT_NEW_ORDER, {
                "customer_telegram_id": telegram_id,
                "customer_name": _af_customer_name,
                "customer_username": _af_customer_uname,
                "product_name": product_name,
                "quantity": quantity,
                "price": total,
                "currency": "USD",
                "payment_method": "Wallet Balance",
                "order_id": order.id,
                "order_status": "Completed",
                "delivery_type": "Instant",
            }))
            # Coupon event if one was used
            _af_coupon_id = context.user_data.get('purchase_coupon_id')
            _af_coupon_code = context.user_data.get('purchase_coupon_code', '')
            _af_coupon_disc = context.user_data.get('purchase_coupon_discount', 0.0)
            if _af_coupon_id and _af_coupon_disc:
                from services.activity_feed import EVENT_COUPON_USED
                _asyncio.create_task(_af_post(context.bot, EVENT_COUPON_USED, {
                    "customer_telegram_id": telegram_id,
                    "coupon_code": _af_coupon_code,
                    "discount": _af_coupon_disc,
                    "order_id": order.id,
                    "product_name": product_name,
                }))
        except Exception:
            pass

        # Referral commission (5% per order) — fire-and-forget so it never
        # blocks the handler loop after the user has seen their success message.
        try:
            from handlers.referral_handlers import process_referral_reward
            asyncio.create_task(
                process_referral_reward(
                    context, telegram_id,
                    order_id=order.id,
                    order_amount=float(total or 0),
                )
            )
        except Exception as e:
            logger.warning("[referral] hook setup failed: %s", e)


async def cancel_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the purchase process."""
    query = update.callback_query
    await query.answer()

    from utils import create_main_menu_keyboard

    # Clear purchase data
    context.user_data.pop('purchase_product_id', None)
    context.user_data.pop('purchase_product_name', None)
    context.user_data.pop('purchase_product_price', None)
    context.user_data.pop('purchase_product_stock', None)
    context.user_data.pop('purchase_product_type', None)
    context.user_data.pop('purchase_quantity', None)

    try:
        await query.edit_message_text(
            "❌ Purchase cancelled.",
            reply_markup=create_main_menu_keyboard(user_id=update.effective_user.id)
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

    return ConversationHandler.END


async def qty_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quantity preset button taps from the dynamic preset keyboard.

    Callback data format: ``qty_preset_<product_id>_<qty>``

    Sets ``context.user_data['purchase_quantity']`` and advances to the
    confirmation screen without requiring the user to type a number.
    """
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    # Format: qty_preset_<product_id>_<qty>
    try:
        quantity = int(parts[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid preset selection.", show_alert=True)
        return

    product_stock = context.user_data.get('purchase_product_stock', 0)

    if quantity < 1 or quantity > product_stock:
        await query.answer(
            f"❌ Quantity {quantity} out of range (max {product_stock}).",
            show_alert=True,
        )
        return

    context.user_data['purchase_quantity'] = quantity
    await show_purchase_confirmation(update, context, is_message=False)


def assign_product_keys(session, product_id: int, quantity: int, order_id: int) -> list:
    """Atomically assign product keys to an order from the product_keys table."""
    # Get available keys (not sold)
    available_keys = session.query(ProductKey).filter_by(
        product_id=product_id,
        is_sold=False
    ).limit(quantity).with_for_update().all()

    if len(available_keys) < quantity:
        raise ValueError(f"Not enough keys available. Requested: {quantity}, Available: {len(available_keys)}")

    assigned_keys = []
    for key in available_keys:
        key.is_sold = True
        key.order_id = order_id
        key.sold_at = datetime.utcnow()
        assigned_keys.append(key.key_value)

    session.commit()

    return assigned_keys


async def broadcast_availability_to_all_users(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to broadcast availability to all users every 12 hours (non-blocking with rate limiting)."""
    import asyncio
    import logging
    from utils import build_availability_text

    logger = logging.getLogger(__name__)
    logger.info("Starting availability broadcast to all users...")

    def _get_users_and_availability_sync():
        """Synchronous database operations run in thread pool."""
        try:
            with get_db_session() as session:
                from database import Category, Product

                # Get all non-banned users
                users = session.query(User).filter_by(is_banned=False).all()
                user_ids = [user.telegram_id for user in users]

                logger.info(f"Found {len(user_ids)} users to notify")

                # Build products by category dictionary
                products_by_category = {}
                categories = session.query(Category).all()

                for category in categories:
                    products = session.query(Product).filter_by(
                        category_id=category.id,
                        is_active=True
                    ).limit(15).all()

                    if products:
                        products_by_category[category.name] = products

                # Get availability text
                if not products_by_category:
                    availability_text = "📦 No products available yet."
                else:
                    availability_text = build_availability_text(products_by_category)

                return user_ids, availability_text
        except Exception as e:
            logger.error(f"Error in _get_users_and_availability_sync: {e}")
            raise

    try:
        # Run blocking database operations in thread pool
        user_ids, availability_text = await asyncio.to_thread(_get_users_and_availability_sync)
    except Exception as e:
        logger.error(f"Failed to get users and availability: {e}")
        return

    if not user_ids:
        logger.info("No users to notify, skipping broadcast")
        return  # No users to notify

    logger.info(f"Broadcasting availability to {len(user_ids)} users...")

    # Create availability keyboard
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton("🛒 Browse Products", callback_data="products")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send to all users with rate limiting
    success_count = 0
    fail_count = 0

    for telegram_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=availability_text,
                reply_markup=reply_markup
            )
            success_count += 1

            # Rate limiting: 50ms delay = ~20 messages/second (well under Telegram's 30/sec limit)
            await asyncio.sleep(0.05)
        except Exception as e:
            # User may have blocked the bot
            logger.debug(f"Failed to send to {telegram_id}: {e}")
            fail_count += 1

    logger.info(f"Availability broadcast complete: {success_count} sent, {fail_count} failed")

    # Notify admin about broadcast completion
    try:
        from utils import notify_admin
        admin_message = f"""📢 Availability Broadcast Complete

✅ Sent successfully: {success_count}
❌ Failed: {fail_count}
👥 Total users: {len(user_ids)}"""

        await notify_admin(context, admin_message)
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")


# =============================================================================
# ADMIN MANUAL VERIFICATION — APPROVE / REJECT
# Called from InlineKeyboardButton callbacks sent to the admin when a Binance
# Pay, Bybit Pay, or ZiniPay (bKash/Nagad/Rocket) auto-verification fails.
# Callback patterns:
#   admin_binance_approve_{tx_id}_{pmv_id}
#   admin_binance_reject_{tx_id}_{pmv_id}
#   admin_bybit_approve_{tx_id}_{pmv_id}
#   admin_bybit_reject_{tx_id}_{pmv_id}
#   admin_zinipay_approve_{tx_id}_{pmv_id}
#   admin_zinipay_reject_{tx_id}_{pmv_id}
# =============================================================================

_PMV_GATEWAY_LABELS = {
    "binance_pay": "Binance Pay",
    "bybit_pay": "Bybit Pay",
    "zinipay": "Mobile Banking (bKash/Nagad/Rocket)",
}


async def _pmv_resolve(
    update,
    context,
    gateway: str,
    tx_id: int,
    pmv_id: int,
    approve: bool,
    **kwargs,
):
    """Shared implementation for approve/reject of a PendingManualVerification."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if not pmv:
            await query.answer(f"❌ PMV #{pmv_id} not found.", show_alert=True)
            return

        if pmv.status != "pending":
            await query.answer(f"⚠️ Already {pmv.status}.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if not approve:
            # ── REJECT ────────────────────────────────────────────────────────
            # reject_reason may be passed via keyword arg (from the new rejection flow)
            reject_reason = kwargs.get("reject_reason", "")
            admin_actor = update.effective_user
            pmv.status = "rejected"
            pmv.admin_note = (
                f"Rejected by admin @{admin_actor.username or admin_actor.id} (TG ID: {admin_actor.id}) "
                f"at {datetime.utcnow().isoformat()}"
                + (f"\nReason: {reject_reason}" if reject_reason else "")
            )
            # Populate dedicated columns for easier querying
            try:
                pmv.admin_telegram_id = admin_actor.id
            except Exception:
                pass
            try:
                pmv.reject_reason = reject_reason or None
            except Exception:
                pass
            pmv.resolved_at = datetime.utcnow()

            # Write audit log
            try:
                session.add(AdminAuditLog(
                    admin_telegram_id=admin_actor.id,
                    action="payment.reject",
                    target_type="transaction",
                    target_id=str(tx_id),
                    details=f"Rejected {gateway} PMV #{pmv_id} | Order #{tx_id} | TXID {pmv.submitted_txid}" + (f" | Reason: {reject_reason}" if reject_reason else ""),
                ))
            except Exception:
                logger.warning("Failed to write audit log for PMV rejection %s", pmv_id)
            session.commit()

            gateway_label = _PMV_GATEWAY_LABELS.get(gateway, gateway)
            gateway_ui_key = gateway
            # Clean up the earlier "could not verify automatically" notice so
            # the user only sees the final status.
            await pui.clear_pending_user_message(context.bot, pmv_id)
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=pmv.telegram_user_id,
                    text=pui.user_payment_card(
                        gateway_key=gateway_ui_key,
                        gateway_label_override=gateway_label,
                        stage="rejected",
                        amount=f"{pmv.amount} {pmv.currency}" if pmv.currency else None,
                        order_id=tx_id,
                        txn_id=pmv.submitted_txid,
                        note=(f"📝 <b>Reason:</b> {reject_reason}\n\n" if reject_reason else "") +
                             "Please contact support if you believe this is an error.",
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.warning("Could not notify user %s of PMV rejection", pmv.telegram_user_id)

            try:
                admin_tag = f"@{admin_actor.username}" if admin_actor.username else f"Admin {admin_actor.id}"
                suffix = pui.admin_resolution_suffix("rejected", admin_tag, reject_reason)
                await query.edit_message_text(
                    query.message.text + suffix,
                    reply_markup=None,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # ── APPROVE ───────────────────────────────────────────────────────────
        # Load the transaction while still in the session
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            await query.answer(f"❌ Deposit {pui.format_deposit_id(tx_id)} not found.", show_alert=True)
            return

        if tx.status != TransactionStatus.PENDING:
            pmv.status = "approved" if tx.status == TransactionStatus.COMPLETED else "rejected"
            pmv.admin_note = f"Order already {tx.status.name}"
            pmv.resolved_at = datetime.utcnow()
            session.commit()
            await query.answer(f"Deposit {pui.format_deposit_id(tx_id)} is already {tx.status.name}.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        user_db_id = tx.user_id
        expected_amount = float(pmv.amount)
        currency = pmv.currency

        # Flip transaction to COMPLETED atomically
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update(
            {Transaction.status: TransactionStatus.COMPLETED, Transaction.completed_at: datetime.utcnow()},
            synchronize_session=False,
        )
        if flipped == 0:
            await query.answer("⚠️ Order already completed by another action.", show_alert=True)
            return

        # Record the verified transaction (skip UNIQUE violation if already credited)
        from services.wallet import credit_locked, WalletError
        gateway_label = _PMV_GATEWAY_LABELS.get(gateway, gateway)
        try:
            if gateway == "binance_pay":
                bpt = BinancePayTransaction(
                    transaction_id=pmv.submitted_txid,
                    binance_order_id=None,
                    telegram_user_id=pmv.telegram_user_id,
                    internal_order_id=tx_id,
                    currency=currency,
                    expected_amount=pmv.amount,
                    received_amount=pmv.amount,  # admin confirmed
                    transaction_time=None,
                    raw_transaction_data='{"manual_approval": true}',
                )
                session.add(bpt)
            elif gateway == "bybit_pay":
                bpt = BybitPayTransaction(
                    transaction_id=pmv.submitted_txid,
                    bybit_record_id=None,
                    telegram_user_id=pmv.telegram_user_id,
                    internal_order_id=tx_id,
                    payment_type=pmv.payment_type or "uid_transfer",
                    network=pmv.network,
                    currency=currency,
                    expected_amount=pmv.amount,
                    received_amount=pmv.amount,
                    transaction_time=None,
                    raw_transaction_data='{"manual_approval": true}',
                )
                session.add(bpt)
            else:  # zinipay (bKash / Nagad / Rocket)
                # Reuse the submitted TXID as the replay-guard key — the same
                # UNIQUE constraint that protects auto-verified ZiniPay
                # payments also stops this TXID being manually approved twice.
                zut = ZiniPayUsedTransaction(
                    trx_id=pmv.submitted_txid,
                    verify_id=None,
                    telegram_user_id=pmv.telegram_user_id,
                    internal_order_id=tx_id,
                    provider="manual_review",
                    sender=None,
                    amount=pmv.amount,
                )
                session.add(zut)
            session.flush()
        except IntegrityError:
            session.rollback()
            await query.answer("⚠️ TXID already credited to another order.", show_alert=True)
            return

        # Credit the wallet
        bonus_percent = 0.0
        try:
            if gateway == "binance_pay":
                from services.binance_pay import BinancePayService
                bonus_percent = BinancePayService().bonus_percent or 0.0
            elif gateway == "bybit_pay":
                from services.bybit_pay import BybitPayService
                bonus_percent = BybitPayService().bonus_percent or 0.0
        except Exception:
            pass

        bonus_amount = round(expected_amount * (bonus_percent / 100.0), 2) if bonus_percent else 0.0
        credited_usd = expected_amount + bonus_amount
        ref_type = gateway

        try:
            new_balance = credit_locked(
                session, user_db_id, credited_usd,
                reason=f"{gateway_label} top-up #{tx_id} (manual approval)",
                actor_type="admin",
                ref_type=ref_type, ref_id=str(tx_id),
            )
        except WalletError:
            session.rollback()
            await query.answer("⚠️ Wallet credit failed — check server logs.", show_alert=True)
            return

        pmv.status = "approved"
        admin_actor = update.effective_user
        pmv.admin_note = f"Approved by admin @{admin_actor.username or admin_actor.id} (TG ID: {admin_actor.id}) at {datetime.utcnow().isoformat()}"
        pmv.resolved_at = datetime.utcnow()

        # Write audit log
        try:
            session.add(AdminAuditLog(
                admin_telegram_id=admin_actor.id,
                action="payment.approve",
                target_type="transaction",
                target_id=str(tx_id),
                details=f"Manual approval of {gateway} top-up #{tx_id} | PMV #{pmv_id} | TXID {pmv.submitted_txid} | {credited_usd:.2f} USD credited",
            ))
        except Exception:
            logger.warning("Failed to write audit log for PMV approval %s", pmv_id)
        session.commit()

    # ── Post-commit: notify user and update admin message ─────────────────
    bonus_line = ("🎁", "Bonus", f"+{bonus_amount:.2f}") if bonus_amount else None
    extra_rows = [("💳", "Credited", f"{credited_usd:.2f}")]
    if bonus_line:
        extra_rows.append(bonus_line)
    await pui.clear_pending_user_message(context.bot, pmv_id)
    try:
        await context.bot.send_message(
            chat_id=pmv.telegram_user_id,
            text=sanitize_message(
                pui.user_payment_card(
                    gateway_key=gateway,
                    gateway_label_override=gateway_label,
                    stage="approved",
                    amount=f"{expected_amount:.2f} {currency}",
                    order_id=tx_id,
                    extra=extra_rows,
                    note="Your wallet has been updated. Thank you!",
                )
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not notify user %s of PMV approval", pmv.telegram_user_id)

    try:
        admin_tag = f"@{update.effective_user.username}" if update.effective_user.username else f"Admin {update.effective_user.id}"
        await query.edit_message_text(
            query.message.text + pui.admin_resolution_suffix("approved", admin_tag),
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass


async def admin_approve_binance_verification(update, context):
    """Handle admin_binance_approve_{tx_id}_{pmv_id}."""
    data = update.callback_query.data  # e.g. admin_binance_approve_42_7
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "binance_pay", tx_id, pmv_id, approve=True)


async def admin_reject_binance_verification(update, context):
    """Handle admin_binance_reject_{tx_id}_{pmv_id}."""
    data = update.callback_query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "binance_pay", tx_id, pmv_id, approve=False)


async def admin_approve_zinipay_verification(update, context):
    """Handle admin_zinipay_approve_{tx_id}_{pmv_id} (bKash / Nagad / Rocket)."""
    data = update.callback_query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "zinipay", tx_id, pmv_id, approve=True)


async def admin_reject_zinipay_verification(update, context):
    """Handle admin_zinipay_reject_{tx_id}_{pmv_id} (bKash / Nagad / Rocket)."""
    data = update.callback_query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "zinipay", tx_id, pmv_id, approve=False)


async def admin_approve_bybit_verification(update, context):
    """Handle admin_bybit_approve_{tx_id}_{pmv_id}."""
    data = update.callback_query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "bybit_pay", tx_id, pmv_id, approve=True)


async def admin_reject_bybit_verification(update, context):
    """Handle admin_bybit_reject_{tx_id}_{pmv_id}."""
    data = update.callback_query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, "bybit_pay", tx_id, pmv_id, approve=False)


# =============================================================================
# NEW ADMIN HANDLERS: VERIFY AGAIN, REJECT WITH REASON, VIEW USER
# Callback patterns handled here:
#   admin_binance_verify_{tx_id}_{pmv_id}  → Verify Again (Binance)
#   admin_bybit_verify_{tx_id}_{pmv_id}    → Verify Again (Bybit)
#   admin_binance_reject_start_{tx_id}_{pmv_id}  → Reject with reason (Binance)
#   admin_bybit_reject_start_{tx_id}_{pmv_id}    → Reject with reason (Bybit)
#   admin_view_user_pmv_{telegram_id}      → View user info from PMV notification
# =============================================================================

# Conversation state for admin rejection reason flow
PMV_REJECT_REASON_STATE = 902


def _build_verify_again_admin_keyboard(gateway: str, tx_id: int, pmv_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    """Rebuild the admin action keyboard for an updated notification."""
    gw = {"binance_pay": "binance", "bybit_pay": "bybit", "zinipay": "zinipay"}.get(gateway, gateway)
    return pui.admin_review_keyboard(
        verify_cb=f"admin_{gw}_verify_{tx_id}_{pmv_id}",
        approve_cb=f"admin_{gw}_approve_{tx_id}_{pmv_id}",
        reject_cb=f"admin_{gw}_reject_start_{tx_id}_{pmv_id}",
        view_user_cb=f"admin_view_user_pmv_{telegram_id}",
    )


async def _admin_verify_again(update, context, gateway: str):
    """Shared implementation of Verify Again for Binance Pay and Bybit Pay.

    Re-runs the full automatic verification against the exchange API.
    If it now succeeds → credits the wallet, marks order COMPLETED, notifies user.
    If it still fails → updates the admin notification with the new failure reason.
    """
    query = update.callback_query
    await query.answer("🔄 Re-verifying…", show_alert=False)

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    # Parse callback data: admin_binance_verify_{tx_id}_{pmv_id}
    data = query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if not pmv:
            await query.answer(f"❌ PMV #{pmv_id} not found.", show_alert=True)
            return
        if pmv.status != "pending":
            await query.answer(f"⚠️ Already {pmv.status}.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx or tx.status != TransactionStatus.PENDING:
            await query.answer("⚠️ Order is no longer pending.", show_alert=True)
            return

        txid_raw = pmv.submitted_txid
        expected_amount = _to_decimal_amount(float(pmv.amount))
        order_created_at = tx.created_at
        user_db_id = tx.user_id
        telegram_user_id = pmv.telegram_user_id
        currency = pmv.currency
        payment_type = pmv.payment_type
        network = pmv.network

    # Run verification in a thread to avoid blocking the event loop
    import asyncio as _asyncio

    new_reason = "Verification failed"  # sentinel — overwritten in the branch below
    if gateway == "binance_pay":
        svc = BinancePayService()
        result = await _asyncio.to_thread(
            svc.verify_transaction,
            transaction_id=txid_raw, expected_amount=expected_amount,
            currency=currency, order_created_at=order_created_at,
        )
        success = result.outcome == VerificationOutcome.SUCCESS
        gw_label = "Binance Pay 🟡"

        if not success:
            reason_map = {
                VerificationOutcome.API_ERROR: "API error / timeout",
                VerificationOutcome.NOT_FOUND: "Payment not found in Binance account history",
                VerificationOutcome.TOO_OLD: "Transaction too old",
                VerificationOutcome.AMOUNT_MISMATCH: f"Wrong amount — expected {expected_amount} {currency}, received {result.received_amount}",
                VerificationOutcome.WRONG_DIRECTION: "Matching transaction was outgoing (SEND), not received",
                VerificationOutcome.CURRENCY_MISMATCH: f"Wrong currency — expected {currency}, received {result.currency or 'unknown'}",
            }
            new_reason = reason_map.get(result.outcome, str(result.outcome))
    else:
        svc = BybitPayService()
        is_uid = payment_type == BybitPaymentType.UID_TRANSFER
        if is_uid:
            result = await _asyncio.to_thread(
                svc.verify_uid_transfer,
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=currency, order_created_at=order_created_at,
            )
        else:
            result = await _asyncio.to_thread(
                svc.verify_onchain_deposit,
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=currency, network=network, order_created_at=order_created_at,
            )
        success = result.outcome == BybitVerificationOutcome.SUCCESS
        gw_label = "Bybit Pay 🔵"

        if not success:
            bybit_reason_map = {
                BybitVerificationOutcome.API_ERROR: "API error / timeout",
                BybitVerificationOutcome.NOT_FOUND: "Payment not found in Bybit account history",
                BybitVerificationOutcome.AMOUNT_MISMATCH: f"Wrong amount — expected {expected_amount}, received {result.received_amount}",
            }
            new_reason = bybit_reason_map.get(result.outcome, str(result.outcome))

    if not success:
        # Update admin message with new failure reason
        try:
            old_text = query.message.text or ""
            # Replace the failure reason line
            import re as _re
            new_text = _re.sub(
                r"<b>Failure Reason:</b>.*",
                f"<b>Failure Reason:</b> {new_reason}\n\n<i>🔄 Re-verified at {datetime.utcnow().strftime('%H:%M:%S UTC')} — still failed</i>",
                old_text,
            )
            await query.edit_message_text(
                new_text,
                reply_markup=_build_verify_again_admin_keyboard(gateway, tx_id, pmv_id, telegram_user_id),
                parse_mode="HTML",
            )
        except Exception:
            await query.answer(f"⚠️ Still failed: {new_reason}", show_alert=True)
        return

    # ── Verification succeeded! Credit the wallet ──────────────────────────
    import json as _json
    from services.wallet import credit_locked, WalletError

    with get_db_session() as session:
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update(
            {Transaction.status: TransactionStatus.COMPLETED, Transaction.completed_at: datetime.utcnow()},
            synchronize_session=False,
        )
        if flipped == 0:
            await query.answer("⚠️ Order already completed.", show_alert=True)
            return

        raw_json = None
        try:
            raw_json = _json.dumps(result.matched_record or {})[:8000]
        except Exception:
            pass

        # Record the verified TXID to prevent double-credits
        try:
            if gateway == "binance_pay":
                session.add(BinancePayTransaction(
                    transaction_id=txid_raw,
                    binance_order_id=getattr(result, 'binance_order_id', None),
                    telegram_user_id=telegram_user_id,
                    internal_order_id=tx_id,
                    currency=result.currency or currency,
                    expected_amount=expected_amount,
                    received_amount=result.received_amount,
                    transaction_time=None,
                    raw_transaction_data=raw_json,
                ))
            else:
                session.add(BybitPayTransaction(
                    transaction_id=txid_raw,
                    bybit_record_id=getattr(result, 'bybit_record_id', None),
                    telegram_user_id=telegram_user_id,
                    internal_order_id=tx_id,
                    payment_type=payment_type or "uid_transfer",
                    network=network,
                    currency=result.currency or currency,
                    expected_amount=expected_amount,
                    received_amount=result.received_amount,
                    transaction_time=None,
                    raw_transaction_data=raw_json,
                ))
            session.flush()
        except IntegrityError:
            session.rollback()
            await query.answer("⚠️ TXID already credited.", show_alert=True)
            return

        bonus_percent = svc.bonus_percent or 0.0
        base_usd = float(expected_amount)
        bonus_amount = round(base_usd * (bonus_percent / 100.0), 2) if bonus_percent else 0.0
        credited_usd = base_usd + bonus_amount
        try:
            ref_type = "binance_pay" if gateway == "binance_pay" else "bybit_pay"
            credit_locked(
                session, user_db_id, credited_usd,
                reason=f"{gw_label} top-up #{tx_id} (verify again)",
                actor_type="system", ref_type=ref_type, ref_id=str(tx_id),
            )
        except WalletError:
            session.rollback()
            await query.answer("⚠️ Wallet credit failed.", show_alert=True)
            return

        pmv_row = session.query(PendingManualVerification).filter_by(id=pmv_id).first()
        if pmv_row:
            pmv_row.status = "approved"
            pmv_row.admin_note = f"Auto-approved via Verify Again by admin {update.effective_user.id}"
            pmv_row.resolved_at = datetime.utcnow()

        # Audit log
        try:
            session.add(AdminAuditLog(
                admin_telegram_id=update.effective_user.id,
                action="payment.verify_again",
                target_type="transaction",
                target_id=str(tx_id),
                details=f"Verify Again succeeded for {gateway} top-up #{tx_id} | {credited_usd:.2f} USD credited",
            ))
        except Exception:
            pass
        session.commit()

    # Notify user
    _bonus_str = f"+{bonus_amount:.2f} USD" if bonus_amount else None
    _gw_label = pui.gateway_meta("binance_pay" if gateway == "binance_pay" else "bybit_pay")[0]
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=sanitize_message(
                pui.deposit_success_card(
                    amount=f"${credited_usd:.2f} USD",
                    payment_method=_gw_label,
                    deposit_id=pui.format_deposit_id(tx_id),
                    bonus_line=_bonus_str,
                )
            ),
            reply_markup=pui.deposit_success_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not notify user %s of payment verification", telegram_user_id)

    # Update admin message
    try:
        await query.edit_message_text(
            query.message.text + f"\n\n✅ <b>Verified Again & Credited</b> by @{update.effective_user.username or update.effective_user.id}",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass


async def admin_verify_again_binance(update, context):
    """Handle admin_binance_verify_{tx_id}_{pmv_id} — re-verify via Binance API."""
    await _admin_verify_again(update, context, gateway="binance_pay")


async def admin_verify_again_bybit(update, context):
    """Handle admin_bybit_verify_{tx_id}_{pmv_id} — re-verify via Bybit API."""
    await _admin_verify_again(update, context, gateway="bybit_pay")


async def admin_verify_again_zinipay(update, context):
    """Handle admin_zinipay_verify_{tx_id}_{pmv_id} — re-run ZiniPay's own
    verify API against the originally submitted TXID. If it now succeeds,
    reuse the standard PMV-approve path (wallet credit, audit log, user
    notification); if it still fails, refresh the admin card with the new
    reason so admins keep the exact same Verify/Approve/Reject/View-User
    controls as Binance Pay and Bybit Pay."""
    query = update.callback_query
    await query.answer("🔄 Re-verifying…", show_alert=False)

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    data = query.data  # admin_zinipay_verify_{tx_id}_{pmv_id}
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway="zinipay").first()
        if not pmv:
            await query.answer(f"❌ PMV #{pmv_id} not found.", show_alert=True)
            return
        if pmv.status != "pending":
            await query.answer(f"⚠️ Already {pmv.status}.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        txid_raw = pmv.submitted_txid
        expected_amount = float(pmv.amount)
        telegram_user_id = pmv.telegram_user_id

    svc = ZiniPayService()
    result = await asyncio.to_thread(svc.verify_transaction, amount=expected_amount, transaction_id=txid_raw)

    if result is None:
        new_reason = svc.last_error or "Verification failed"
        try:
            old_text = query.message.text or ""
            import re as _re
            new_text = _re.sub(
                r"⚠️ <b>Auto-verify failed:</b>.*",
                f"⚠️ <b>Auto-verify failed:</b> {new_reason}\n\n"
                f"<i>🔄 Re-verified at {datetime.utcnow().strftime('%H:%M:%S UTC')} — still failed</i>",
                old_text,
            )
            await query.edit_message_text(
                new_text,
                reply_markup=_build_verify_again_admin_keyboard("zinipay", tx_id, pmv_id, telegram_user_id),
                parse_mode="HTML",
            )
        except Exception:
            await query.answer(f"⚠️ Still failed: {new_reason}", show_alert=True)
        return

    # Verification succeeded — hand off to the standard PMV-approve path so
    # wallet crediting, audit logging, and user notification stay identical
    # to a manual approval.
    await _pmv_resolve(update, context, "zinipay", tx_id, pmv_id, approve=True)


# ── REJECT WITH REASON ─────────────────────────────────────────────────────

async def admin_reject_start(update, context):
    """Entry point for admin rejection — prompts admin to type a rejection reason.

    Handles both:
      admin_binance_reject_start_{tx_id}_{pmv_id}
      admin_bybit_reject_start_{tx_id}_{pmv_id}
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return ConversationHandler.END

    data = query.data
    parts = data.split("_")
    try:
        pmv_id = int(parts[-1])
        tx_id = int(parts[-2])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid data.", show_alert=True)
        return ConversationHandler.END

    if "binance" in data:
        gateway = "binance_pay"
    elif "bybit" in data:
        gateway = "bybit_pay"
    else:
        gateway = "zinipay"
    gw_label = {
        "binance_pay": "Binance Pay 🟡",
        "bybit_pay": "Bybit Pay 🔵",
        "zinipay": "Mobile Banking (bKash/Nagad/Rocket) 🇧🇩",
    }[gateway]

    # Check PMV still pending
    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if not pmv:
            await query.answer(f"❌ PMV #{pmv_id} not found.", show_alert=True)
            return ConversationHandler.END
        if pmv.status != "pending":
            await query.answer(f"⚠️ Already {pmv.status}.", show_alert=True)
            return ConversationHandler.END

    context.user_data['pmv_reject'] = {
        'pmv_id': pmv_id,
        'tx_id': tx_id,
        'gateway': gateway,
        'msg_id': query.message.message_id,
        'chat_id': query.message.chat_id,
    }

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"❌ <b>Reject {gw_label} Payment</b>\n\n"
            f"🧾 Deposit {pui.format_deposit_id(tx_id)} (review #{pmv_id})\n\n"
            "Please type the <b>rejection reason</b> to notify the user.\n"
            "Send /skip to reject without a specific reason."
        ),
        parse_mode="HTML",
    )
    return PMV_REJECT_REASON_STATE


async def admin_reject_reason_received(update, context, reason_override: str = None):
    """Receives the rejection reason text from the admin and processes the rejection.

    `reason_override`, when given, is used instead of reading update.message.text.
    This lets callers like admin_reject_reason_skip() short-circuit straight to
    "no reason given" behavior without touching the incoming Message object
    (python-telegram-bot's Message is immutable, so update.message.text can
    never be assigned to).
    """
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    reject_data = context.user_data.pop('pmv_reject', None)
    if not reject_data:
        return ConversationHandler.END

    raw_text = reason_override if reason_override is not None else (update.message.text or "")
    reason_text = raw_text.strip()
    if reason_text.lower() in ('/skip', 'skip', '-', '--'):
        reason_text = ""

    pmv_id = reject_data['pmv_id']
    tx_id = reject_data['tx_id']
    gateway = reject_data['gateway']
    gw_label = _PMV_GATEWAY_LABELS.get(gateway, gateway)

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if not pmv or pmv.status != "pending":
            await update.message.reply_text("⚠️ This PMV is no longer pending.")
            return ConversationHandler.END

        admin_actor = update.effective_user
        pmv.status = "rejected"
        pmv.admin_note = (
            f"Rejected by admin @{admin_actor.username or admin_actor.id} (TG ID: {admin_actor.id}) "
            f"at {datetime.utcnow().isoformat()}"
            + (f"\nReason: {reason_text}" if reason_text else "")
        )
        # Store in the dedicated columns too (for easier querying / future admin panel)
        try:
            pmv.admin_telegram_id = admin_actor.id
        except Exception:
            pass  # graceful if column not yet migrated
        try:
            pmv.reject_reason = reason_text or None
        except Exception:
            pass
        pmv.resolved_at = datetime.utcnow()

        # Audit log
        try:
            session.add(AdminAuditLog(
                admin_telegram_id=admin_actor.id,
                action="payment.reject",
                target_type="transaction",
                target_id=str(tx_id),
                details=f"Rejected {gateway} PMV #{pmv_id} | Order #{tx_id} | TXID {pmv.submitted_txid}"
                        + (f" | Reason: {reason_text}" if reason_text else ""),
            ))
        except Exception:
            logger.warning("Failed to write audit log for PMV rejection %s", pmv_id)

        telegram_user_id = pmv.telegram_user_id
        submitted_txid = pmv.submitted_txid
        session.commit()

    # Notify the user
    await pui.clear_pending_user_message(context.bot, pmv_id)
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=pui.user_payment_card(
                gateway_key=gateway,
                gateway_label_override=gw_label,
                stage="rejected",
                amount=None,
                order_id=tx_id,
                txn_id=submitted_txid,
                note=(f"📝 <b>Reason:</b> {reason_text}\n\n" if reason_text else "") +
                     "Please contact support if you believe this is an error.",
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not notify user %s of PMV rejection", telegram_user_id)

    admin_tag = f"@{admin_actor.username}" if admin_actor.username else f"Admin {admin_actor.id}"
    reason_suffix = f"\n📝 Reason: {reason_text}" if reason_text else ""
    await update.message.reply_text(
        f"✅ <b>Rejected</b>\n\n🧾 Deposit {pui.format_deposit_id(tx_id)} (review #{pmv_id})\n"
        f"User notified.{reason_suffix}",
        parse_mode="HTML",
    )

    # Try to update the original admin message if we have its coords
    if reject_data.get('msg_id') and reject_data.get('chat_id'):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=reject_data['chat_id'],
                message_id=reject_data['msg_id'],
                reply_markup=None,
            )
        except Exception:
            pass

    return ConversationHandler.END


async def admin_reject_reason_skip(update, context):
    """Handle /skip (and /cancel, as a fallback) during rejection reason collection.

    Both reject with no specific reason. python-telegram-bot's Message object
    is immutable (Message.text can't be assigned), so instead of faking the
    incoming message text we pass the "skip" value directly to the shared
    handler via reason_override, which is functionally identical to the old
    (broken) `update.message.text = "/skip"` approach without ever touching
    the Update/Message objects.
    """
    context.user_data['_skip_reject'] = True
    return await admin_reject_reason_received(update, context, reason_override="/skip")


def build_admin_pmv_reject_conv():
    """Build the ConversationHandler for admin PMV rejection reason collection."""
    from telegram.ext import CallbackQueryHandler as CQH, MessageHandler as MH, CommandHandler as CH, filters

    return ConversationHandler(
        entry_points=[
            CQH(admin_reject_start, pattern=r"^admin_binance_reject_start_\d+_\d+$"),
            CQH(admin_reject_start, pattern=r"^admin_bybit_reject_start_\d+_\d+$"),
            CQH(admin_reject_start, pattern=r"^admin_zinipay_reject_start_\d+_\d+$"),
        ],
        states={
            PMV_REJECT_REASON_STATE: [
                CH("skip", admin_reject_reason_skip),
                MH(filters.TEXT & ~filters.COMMAND, admin_reject_reason_received),
            ],
        },
        fallbacks=[
            CH("cancel", admin_reject_reason_skip),
        ],
        allow_reentry=True,
        per_message=False,
    )


async def admin_view_user_from_pmv(update, context):
    """Handle admin_view_user_pmv_{telegram_id} — show user summary to admin."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    data = query.data  # admin_view_user_pmv_{telegram_id}
    try:
        tg_id = int(data.split("_")[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid data.", show_alert=True)
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            await query.answer("❌ User not found in database.", show_alert=True)
            return

        uname = f"@{user.username}" if user.username else "(no username)"
        balance = user.wallet_balance or 0.0
        total_orders = session.query(Transaction).filter_by(
            user_id=user.id, status=TransactionStatus.COMPLETED
        ).count()
        pending_orders = session.query(Transaction).filter_by(
            user_id=user.id, status=TransactionStatus.PENDING
        ).count()

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"👤 <b>User Info</b>\n\n"
            f"<b>Username:</b> {uname}\n"
            f"<b>Telegram ID:</b> <code>{tg_id}</code>\n"
            f"<b>Wallet Balance:</b> ${balance:.2f}\n"
            f"<b>Completed Orders:</b> {total_orders}\n"
            f"<b>Pending Orders:</b> {pending_orders}\n\n"
            f"<b>Profile:</b> tg://user?id={tg_id}"
        ),
        parse_mode="HTML",
    )
