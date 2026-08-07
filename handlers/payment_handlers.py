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
    get_db_session, run_db, User, Transaction, Order, OrderItem, Product,
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
from utils.permissions import has_permission
from config.settings import settings as app_settings


def create_cancel_keyboard():
    """Payment-flow-local override of ``utils.create_cancel_keyboard``.

    Every call site in THIS file only ever reaches this button while inside
    (or just before) the Add Funds flow, where the dedicated
    "back_payment_methods" callback_data resolves to ``cancel_topup`` /
    ``cancel_payment_page`` — pure Back navigation to the Payment Method
    screen that never touches a pending deposit (see
    ``_go_back_to_methods`` below). This callback is intentionally its own
    dedicated name, distinct from "cancel"/"cancel_*", so a Back tap can
    never be routed to a real cancel handler. Shadowing the imported name
    here means every existing call site keeps working unmodified while
    showing the correct "⬅️ Back" label; it does not affect
    ``utils.create_cancel_keyboard`` itself, which
    handlers/admin_conversations.py and handlers/dispute_handlers.py still
    use for their own, unrelated, genuinely-destructive Cancel actions.

    Every call site of this builder fires BEFORE a Deposit ID/Transaction
    row exists — the amount-entry prompts, every amount-validation-error
    retry, and every "this gateway isn't available" notice all happen
    prior to payment creation — so per the navigation spec this shows only
    "⬅️ Back", never a destructive "❌ Cancel" row. Once a Deposit ID does
    exist, the payment screens built after it (invoice/payment pages,
    currency/network pickers, Submit Transaction/Order ID prompts) render
    through ``services/payment_ui.py:with_deposit_cancel`` instead, which
    is the only place that ever adds the real Cancel button.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_payment_methods")]])


def _plain_usd(amount: float) -> str:
    """Plain USD amount for invoice screens only.

    ``format_price`` appends a secondary-currency conversion, e.g.
    ``$12.50 (~৳1,375.00)``, when the admin has a display currency
    configured. That is an exchange-rate figure, and the invoice spec is
    explicit: no exchange rate ever appears on a payment invoice. Use this
    instead of ``format_price`` for every Amount field on an invoice card.
    """
    return f"${amount:.2f}"


from services.crypto_bot import CryptoBotService
from services.bkash_payment import BkashPaymentService
from services.nagad_payment import NagadPaymentService
from services.cryptomus_payment import CryptomusPaymentService
from services.heleket_payment import HeleketPaymentService, SUPPORTED_ASSETS
from services.nowpayments_payment import NowPaymentsService
from services.zinipay_payment import ZiniPayService
from services.binance_pay import BinancePayService, VerificationOutcome, is_rate_limited, get_order_lock, is_valid_txid_format
from services import ltc_rate as _ltc_rate_svc
from services.inventory_reservation_ui import format_time_remaining as _time_remaining
from services.bybit_pay import (
    BybitPayService, PaymentType as BybitPaymentType, VerificationOutcome as BybitVerificationOutcome,
    is_rate_limited as bybit_is_rate_limited, get_order_lock as bybit_get_order_lock,
    is_valid_uid_txid_format, is_valid_onchain_txid_format,
)
from services.telegram_stars import telegram_stars_service
from services import gateway_manual_mode as gw_mode
from services.pricing import convert_currency
from services import payment_ui as pui
from services import payment_selection_ui as psel
from services import amount_selection_ui as amtsel
from utils.bot_config import cfg
from utils.perf import perf_track
from utils.callback_safety import guarded_callback, safe_answer
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
AMOUNT, METHOD, MANUAL_PROOF, MANUAL_TXID, AMOUNT_SELECT = range(5)

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



def _collect_topup_gateways():
    """Gather the currently available gateways + admin manual methods.

    Pure data collection — no rendering. This is the single place that
    decides *which* payment methods exist right now (unchanged business
    logic); ``services/payment_selection_ui.py`` is the single place that
    decides how they're laid out on screen. Returns ``(gateways, method_objs)``.
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
        # The combined ZiniPay entry point only ever appears when at least
        # one BD mobile-money provider (bKash/Nagad/Rocket/Upay) actually has
        # a wallet number set — a provider (or the whole gateway, if none of
        # its providers are configured) is "Not Configured" and must never
        # reach the customer payment menu. See services/zinipay_payment.py.
        # Guarded: this must never be able to break the OTHER gateways
        # (Bybit/Binance/crypto/etc.) in this same list if it fails for any
        # reason — fail closed by hiding ZiniPay rather than raising.
        try:
            from services.zinipay_payment import is_any_provider_configured
            _zini_visible = is_any_provider_configured()
        except Exception:
            logger.exception("Failed to check ZiniPay provider configuration")
            _zini_visible = False
        if _zini_visible:
            gateways.append({"key": "zinipay", "label": "bKash • Nagad • Rocket • Upay", "emoji": "🇧🇩"})
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
    return gateways, method_objs


def _build_topup_method_screen(amount: float = None):
    """Build the "💰 Add Funds" payment-method selection screen (text +
    keyboard) via the shared ``services/payment_selection_ui`` component.

    Shared by ``topup_start`` (the normal entry point), ``topup_back_to_methods``
    (the Crypto Networks / Mobile Money submenus' Back button), and the
    Cancel handlers (``cancel_topup`` / ``cancel_payment_page``), which all
    behave like a tap straight back to this screen instead of a dead-end
    "Payment Cancelled" card.

    Returns ``(text, keyboard, is_empty)`` — ``is_empty`` is True when no
    gateway or manual payment method is configured at all, in which case
    the caller should end any in-progress conversation. When ``amount`` is
    given (the user already picked one on Step 1), it's echoed back on the
    screen as "Deposit Amount" — purely a display detail passed through to
    ``services/payment_selection_ui.py``.
    """
    gateways, method_objs = _collect_topup_gateways()

    if not method_objs and not gateways:
        text = (
            "❌ No payment methods are available right now.\n\n"
            "Please contact support — the admin needs to configure at least one payment method."
        )
        return text, create_cancel_keyboard(), True

    text, keyboard = psel.build_payment_selection_screen(gateways, method_objs, amount=amount)
    return text, keyboard, False


def _build_topup_amount_screen():
    """Build the "💰 Add Funds" amount-selection screen (text + keyboard) —
    shared by ``topup_start`` (the normal entry point) and
    ``topup_back_to_amount_selection`` (the Payment Method screen's Back
    button), so both render the exact same Step 1 screen."""
    try:
        from utils.bot_config import cfg
        _min_enabled = cfg.get_bool("minimum_deposit_enabled", False)
        gmin = cfg.get_float("topup_min_amount", 1.0) if _min_enabled else 0.01
    except Exception:
        gmin = 0.01
    return amtsel.build_amount_selection_screen(min_deposit=gmin)


def _build_crypto_networks_screen():
    """Build the "₿ Crypto Networks" submenu (text + keyboard)."""
    gateways, _ = _collect_topup_gateways()
    return psel.build_crypto_networks_screen(gateways)


def _build_other_coins_screen():
    """Build the "🪙 SELECT COIN" submenu (text + keyboard)."""
    gateways, _ = _collect_topup_gateways()
    return psel.build_other_coins_screen(gateways)


def _build_mobile_money_screen():
    """Build the "🇧🇩 Mobile Banking" submenu (text + keyboard)."""
    gateways, _ = _collect_topup_gateways()
    return psel.build_mobile_money_screen(gateways)


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the wallet top-up flow: show the shared "💰 Add Funds" amount
    screen first (services/amount_selection_ui.py) — the exact same screen
    for every gateway — then the payment-method screen once an amount has
    been picked (see topup_amount_selected / topup_amount_custom_prompt)."""
    query = update.callback_query
    await safe_answer(query)

    # Fresh start — clear any leftover state from a previous attempt.
    context.user_data.pop('topup_amount', None)
    context.user_data.pop('topup_method', None)
    context.user_data.pop('zinipay_provider', None)

    # If nothing is configured at all, skip straight to the same "no
    # payment methods available" message the method screen would show —
    # no point asking for an amount first in that case.
    # _build_topup_method_screen() instantiates every gateway service
    # (Bybit/Binance/Cryptomus/Heleket/NOWPayments/ZiniPay/...), each doing
    # its own DB read — run it on a worker thread so it never blocks the
    # event loop for other users.
    text, keyboard, is_empty = await run_db(_build_topup_method_screen)
    if is_empty:
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    amt_text, amt_keyboard = _build_topup_amount_screen()
    try:
        await query.edit_message_text(amt_text, reply_markup=amt_keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT_SELECT


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_amount_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A preset amount button (💵 $1 / $3 / $5 / $10) was tapped on the
    shared Amount Selection screen. Store the amount and move on to the
    existing, unmodified payment-method screen — every gateway shown there
    already knows the amount and won't ask for it again."""
    query = update.callback_query
    await safe_answer(query)

    amount = amtsel.parse_preset_callback(query.data)
    if amount is None or amount <= 0:
        await safe_answer(query, "❌ Invalid amount.", show_alert=True)
        return AMOUNT_SELECT

    context.user_data['topup_amount'] = amount
    context.user_data.pop('topup_method', None)
    context.user_data.pop('zinipay_provider', None)

    text, keyboard, is_empty = await run_db(_build_topup_method_screen, amount=amount)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END if is_empty else METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_amount_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"✍️ Custom Amount" tapped on the shared Amount Selection screen —
    prompt for free-text entry. Handled by the existing, unmodified
    topup_amount() text handler (AMOUNT state) exactly as before; since no
    method is pre-selected yet, it takes the same amount-eligible
    payment-method path it already did."""
    query = update.callback_query
    await safe_answer(query)
    context.user_data.pop('topup_method', None)

    # Premium, minimal prompt. Admin-configured min/max are no longer shown
    # here — they still apply exactly as before, but silently: topup_amount()
    # is completely unchanged and still validates against them and still
    # shows the existing "❌ Minimum top-up is $X." / "❌ Maximum single
    # top-up is $X." errors on an invalid entry, with this same Back
    # keyboard, in this same AMOUNT state.
    text = (
        "💰 <b>Top Up Wallet</b>\n\n"
        "✏️ Enter the amount in USD.\n\n"
        "<i>Example: 5 • 10.50 • 100</i>"
    )

    try:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="topup_back_to_amount"),
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_show_crypto_networks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🔗 Crypto Networks" tapped on the Add Funds screen — show the
    on-chain network submenu. Pure navigation: no payment is created here."""
    query = update.callback_query
    await safe_answer(query)

    text, keyboard = await run_db(_build_crypto_networks_screen)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_show_other_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🪙 OTHER COINS" tapped on the payment menu — show the coin submenu.
    Pure navigation: no payment is created here."""
    query = update.callback_query
    await safe_answer(query)

    text, keyboard = await run_db(_build_other_coins_screen)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_show_mobile_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🇧🇩 Mobile Money (BD)" tapped on the Add Funds screen — show the
    bKash / Nagad / Rocket submenu. Pure navigation: no payment is created here."""
    query = update.callback_query
    await safe_answer(query)

    text, keyboard = await run_db(_build_mobile_money_screen)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_back_to_methods(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped from the Crypto Networks / Mobile Money submenu —
    return to the top-level Add Funds screen."""
    query = update.callback_query
    await safe_answer(query)

    text, keyboard, is_empty = await run_db(_build_topup_method_screen, amount=context.user_data.get('topup_amount'))
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ConversationHandler.END if is_empty else METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_back_to_amount_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped on the Payment Method screen (Step 2) — return to
    the Amount Selection screen (Step 1) by editing the same message, never
    sending a new one and never restarting the deposit flow.

    The previously-picked amount is deliberately left in
    ``context.user_data['topup_amount']`` (not popped) so it's still there
    if the user picks the same amount again, and so a subsequent Back tap
    from Payment Method still has it to echo back.
    """
    query = update.callback_query
    await safe_answer(query)

    amt_text, amt_keyboard = _build_topup_amount_screen()
    try:
        await query.edit_message_text(amt_text, reply_markup=amt_keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT_SELECT


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_back_to_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped on the Amount Selection screen (Step 1) — leave the
    deposit flow entirely and return to the Wallet screen it was opened
    from, editing the same message via ``wallet_handlers.wallet_menu``
    rather than sending a new one. Ends the conversation cleanly (this is
    the top of the flow, so there's nothing left to go "back" to inside it)."""
    from handlers.wallet_handlers import wallet_menu

    context.user_data.pop('topup_amount', None)
    context.user_data.pop('topup_method', None)
    context.user_data.pop('zinipay_provider', None)

    await wallet_menu(update, context)
    return ConversationHandler.END


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_amount_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy entry point kept for old in-flight conversations/links.
    Falls back to the classic 'type an amount first' flow."""
    query = update.callback_query
    await safe_answer(query)
    context.user_data.pop('topup_method', None)
    try:
        await query.edit_message_text("💬 How much would you like to add to your wallet, in USD?\nExample: 10", reply_markup=create_cancel_keyboard())
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AMOUNT


@guarded_callback(fallback_state=ConversationHandler.END)
async def topup_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to Main Menu without retaining payment-flow selections."""
    context.user_data.pop('topup_amount', None)
    context.user_data.pop('topup_method', None)
    context.user_data.pop('zinipay_provider', None)
    from handlers.user_handlers import main_menu_callback
    await main_menu_callback(update, context)
    return ConversationHandler.END


def _amount_range_hint(gmin: float, gmax: float) -> str:
    """Small helper: build a '(Accepted range: ...)' hint line for amount prompts."""
    if gmin and gmax:
        return f"\n(Accepted range: ${gmin:.2f} – ${gmax:.2f})"
    if gmin:
        return f"\n(Minimum: ${gmin:.2f})"
    if gmax:
        return f"\n(Maximum: ${gmax:.2f})"
    return ""


# ── Amount-Selection-screen integration ──────────────────────────────────
# The shared Amount Selection screen (services/amount_selection_ui.py) now
# collects the amount BEFORE a gateway/method is chosen. When that's the
# case (the normal case going forward), the various "user picked gateway X
# — now ask for the amount" entry points below must skip the prompt and go
# straight to the existing, unmodified topup_amount() validation/creation
# pipeline — the exact same code path that already ran when a user *typed*
# an amount. These two tiny proxies exist only to satisfy topup_amount()'s
# expectations (update.message.text / .reply_text, update.effective_user)
# from a callback-query update, without changing topup_amount() itself or
# anything it calls.
class _PreselectedAmountMessage:
    __slots__ = ("text", "_message")

    def __init__(self, message, text: str):
        self._message = message
        self.text = text

    def reply_text(self, *args, **kwargs):
        return self._message.reply_text(*args, **kwargs)


class _PreselectedAmountUpdate:
    __slots__ = ("message", "effective_user")

    def __init__(self, message, effective_user):
        self.message = message
        self.effective_user = effective_user


async def _dispatch_with_preselected_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If ``context.user_data['topup_amount']`` is already set (the user
    picked it on the Amount Selection screen), feed it straight into
    topup_amount() — the same function that already validates the amount
    against this gateway/method's limits and creates the payment — instead
    of prompting the user to type it again. Returns None (meaning "no
    amount yet, prompt as usual") when nothing was pre-selected."""
    amount = context.user_data.get('topup_amount')
    if not amount:
        return None
    query = update.callback_query
    faux_message = _PreselectedAmountMessage(query.message, str(amount))
    faux_update = _PreselectedAmountUpdate(faux_message, query.from_user)
    return await topup_amount(faux_update, context)


async def _ask_amount_for_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   gateway_key: str, label: str, emoji: str,
                                   gmin: float = 0.0, gmax: float = 0.0):
    """Shared step: user picked an automated gateway. If an amount was
    already chosen on the Amount Selection screen, create the payment
    straight away; otherwise fall back to the classic text-entry prompt
    (e.g. old in-flight conversations / the "✍️ Custom Amount" path)."""
    query = update.callback_query
    # Idempotent: some callers (e.g. Bybit Pay / Binance Pay) already
    # answered the tap immediately, before doing slower work like reading
    # gateway config from the database — this is safe to call again.
    await safe_answer(query)
    context.user_data['topup_method'] = ('gateway', gateway_key)

    pre_result = await _dispatch_with_preselected_amount(update, context)
    if pre_result is not None:
        return pre_result

    hint = _amount_range_hint(gmin, gmax)
    try:
        await query.edit_message_text(
            f"{emoji} <b>{label}</b> selected.\n\n"
            f"✏️ Enter the amount in USD.{hint}\n\n"
            "<i>Example: 5 • 10.50 • 100</i>",
            reply_markup=create_cancel_keyboard(),
            parse_mode="HTML",
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


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_heleket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
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
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_payment_methods")])
    try:
        await query.edit_message_text("🪙 Select coin and network:", reply_markup=InlineKeyboardMarkup(rows))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return METHOD

@guarded_callback(fallback_state=ConversationHandler.END)
async def heleket_asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    key=query.data.split(":",1)[1]
    asset=SUPPORTED_ASSETS.get(key)
    if not asset:
        await safe_answer(query, "Unsupported asset", show_alert=True); return METHOD
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
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
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
        _zini_svc = ZiniPayService()
        if _zini_svc.enabled and _zini_svc.is_configured():
            # Same rule as _collect_topup_gateways above: only show the
            # combined ZiniPay entry when at least one provider actually has
            # a wallet number configured. Guarded the same way — a failure
            # here must not break the rest of this gateway list.
            try:
                from services.zinipay_payment import is_any_provider_configured
                _zini_visible = is_any_provider_configured()
            except Exception:
                logger.exception("Failed to check ZiniPay provider configuration")
                _zini_visible = False
            if _zini_visible:
                gateways.append({"key": "zinipay", "label": "bKash • Nagad • Rocket • Upay", "emoji": "🇧🇩"})
        stars_cfg = telegram_stars_service.get_config()
        if stars_cfg["enabled"]:
            stars_needed = telegram_stars_service.stars_for_usd(amount)
            if stars_cfg["min_stars"] <= stars_needed <= stars_cfg["max_stars"]:
                gateways.append({"key": "stars", "label": "Telegram Stars", "emoji": "⭐"})

        def _load_eligible_methods(_amount):
            with get_db_session() as session:
                methods = session.query(ManualPaymentMethod).filter_by(
                    is_active=True
                ).order_by(ManualPaymentMethod.sort_order, ManualPaymentMethod.id).all()

                eligible = [
                    m for m in methods
                    if _amount >= (m.min_amount or 0)
                    and (not m.max_amount or _amount <= m.max_amount)
                ]

                methods_data = [(m.id, m.emoji, m.name, m.min_amount) for m in eligible]
                all_methods_min = [m.min_amount or 0 for m in methods] if methods else []
                return methods_data, all_methods_min

        methods_data, all_methods_min = await run_db(_load_eligible_methods, amount)

        if not methods_data and not gateways:
            if not all_methods_min:
                msg = (
                    "❌ No payment methods are available right now.\n\n"
                    "Please contact support — the admin needs to configure at least one payment method."
                )
            else:
                min_needed = min(all_methods_min)
                msg = (
                    f"❌ Amount too low for available methods.\n"
                    f"Minimum accepted: ${min_needed:.2f}\n\nPlease start again with a larger amount."
                )
            await update.message.reply_text(msg, reply_markup=create_cancel_keyboard())
            return ConversationHandler.END

        class _M:
            __slots__ = ('id', 'emoji', 'name', 'min_amount')
            def __init__(self, i, e, n, mn):
                self.id, self.emoji, self.name, self.min_amount = i, e, n, mn

        method_objs = [_M(*d) for d in methods_data]

        message, keyboard = psel.build_payment_selection_screen(gateways, method_objs, amount=amount)
        await update.message.reply_text(
            message,
            reply_markup=keyboard,
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
            selected_provider = context.user_data.get('zinipay_provider')
            return await _finish_zinipay_payment(update, context, amount, provider=selected_provider)
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

@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked an admin-managed manual payment method — ask for the amount next."""
    query = update.callback_query
    await safe_answer(query)

    try:
        method_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        try:
            await query.edit_message_text("❌ Invalid payment method.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    def _load_method(_method_id):
        with get_db_session() as session:
            method = session.query(ManualPaymentMethod).filter_by(
                id=_method_id, is_active=True
            ).first()
            if not method:
                return None
            return method.name, method.emoji or "💳", method.min_amount or 0, method.max_amount or 0

    _m = await run_db(_load_method, method_id)
    if _m is None:
        try:
            await query.edit_message_text("❌ Payment method is no longer available.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END
    method_name, method_emoji, mmin, mmax = _m

    context.user_data['topup_method'] = ('manual', method_id)

    pre_result = await _dispatch_with_preselected_amount(update, context)
    if pre_result is not None:
        return pre_result

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

    def _create_manual_tx(_method_id, _usd_amount, _telegram_id):
        with get_db_session() as session:
            method = session.query(ManualPaymentMethod).filter_by(
                id=_method_id, is_active=True
            ).first()
            if not method:
                return {"outcome": "no_method"}

            if _usd_amount < (method.min_amount or 0):
                return {"outcome": "below_min", "min_amount": method.min_amount, "name": method.name}
            if method.max_amount and _usd_amount > method.max_amount:
                return {"outcome": "above_max", "max_amount": method.max_amount, "name": method.name}

            user = session.query(User).filter_by(telegram_id=_telegram_id).first()
            if not user:
                return {"outcome": "no_user"}

            transaction = Transaction(
                user_id=user.id,
                amount=_usd_amount,
                payment_method=PaymentMethod.MANUAL,
                manual_method_id=method.id,
                status=TransactionStatus.PENDING,
                expires_at=None,  # Manual payments don't auto-expire
            )
            session.add(transaction)
            session.commit()
            session.refresh(transaction)

            return {
                "outcome": "ok",
                "transaction_id": transaction.id,
                "transaction_created_at": transaction.created_at,
                "method_name": method.name,
                "method_emoji": method.emoji or "💳",
                "instructions": method.instructions,
                "acct_label": method.account_label or None,
                "acct_number": method.account_number or None,
                "req_txid": bool(method.require_txid),
                "req_proof": bool(method.require_proof),
            }

    _r = await run_db(_create_manual_tx, method_id, usd_amount, telegram_id)

    if _r["outcome"] == "no_method":
        await update.message.reply_text("❌ Payment method is no longer available. Please start again.")
        return ConversationHandler.END
    if _r["outcome"] == "below_min":
        await update.message.reply_text(
            f"❌ Amount below minimum for {_r['name']} (min ${_r['min_amount']:.2f}).",
            reply_markup=create_cancel_keyboard(),
        )
        return AMOUNT
    if _r["outcome"] == "above_max":
        await update.message.reply_text(
            f"❌ Amount above maximum for {_r['name']} (max ${_r['max_amount']:.2f}).",
            reply_markup=create_cancel_keyboard(),
        )
        return AMOUNT
    if _r["outcome"] == "no_user":
        await update.message.reply_text("❌ User not found.")
        return ConversationHandler.END

    transaction_id = _r["transaction_id"]
    transaction_created_at = _r["transaction_created_at"]
    method_name = _r["method_name"]
    method_emoji = _r["method_emoji"]
    instructions = _r["instructions"]
    acct_label = _r["acct_label"]
    acct_number = _r["acct_number"]
    req_txid = _r["req_txid"]
    req_proof = _r["req_proof"]

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
    if req_txid:
        category = pui.txid_category_for(method_name)
        text, keyboard = pui.submit_txid_prompt(category, cancel_cb="cancel")
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        return MANUAL_TXID
    return MANUAL_PROOF


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

    def _save_txid(_txid, _method_id, _transaction_id):
        with get_db_session() as session:
            # Reject reused TXID for the SAME method (per-method uniqueness).
            clash = session.query(Transaction).filter(
                Transaction.txid == _txid,
                Transaction.manual_method_id == _method_id,
                Transaction.id != _transaction_id,
                Transaction.status.in_([
                    TransactionStatus.AWAITING_CONFIRMATION,
                    TransactionStatus.COMPLETED,
                ]),
            ).first()
            if clash:
                return "clash"

            tx = session.query(Transaction).filter_by(id=_transaction_id).first()
            if not tx:
                return "not_found"
            tx.txid = _txid
            session.commit()
            return "ok"

    _outcome = await run_db(_save_txid, txid, method_id, transaction_id)
    if _outcome == "clash":
        await update.message.reply_text(
            "❌ This Transaction ID was already submitted. Please double-check and send the correct TXID."
        )
        return MANUAL_TXID
    if _outcome == "not_found":
        await update.message.reply_text("❌ Transaction not found.")
        return ConversationHandler.END

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

    def _save_proof(_transaction_id, _proof_text, _photo_file_id):
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(id=_transaction_id).first()
            if not tx:
                return None

            tx.proof = _proof_text
            if _photo_file_id:
                tx.proof_file_id = _photo_file_id
                # Legacy mirror for older admin panels reading crypto_address.
                tx.crypto_address = f"photo:{_photo_file_id}"
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
            from services.payment_workflow import is_foreign_currency_gateway
            is_gateway_manual = is_foreign_currency_gateway(tx.payment_method)
            return {
                "method_name": method_name,
                "is_gateway_manual": is_gateway_manual,
                "amount": tx.amount,
                "tg_id": user.telegram_id if user else None,
                "stored_txid": tx.txid,
            }

    _r = await run_db(_save_proof, transaction_id, proof_text, photo_file_id)
    if _r is None:
        await update.message.reply_text("❌ Transaction not found.")
        return ConversationHandler.END
    method_name = _r["method_name"]
    is_gateway_manual = _r["is_gateway_manual"]
    amount = _r["amount"]
    tg_id = _r["tg_id"]
    stored_txid = _r["stored_txid"]

    # bKash/Nagad manual transactions record `amount` in BDT (the real money
    # the user sent) — see _payment_method_gateway_manual / admin_manual_approve
    # for the BDT->USD conversion applied when the admin approves.
    amount_line = f"৳{amount:.2f} BDT" if is_gateway_manual else f"${amount:.2f}"

    # Manual payment methods have no live auto-verification step (a human
    # always makes the final call) — but the user should still see the
    # same "Verifying Payment" acknowledgment every other gateway shows
    # immediately after a submission, instead of jumping straight from
    # their input to a final screen with no feedback in between.
    processing_msg = await update.message.reply_text(
        pui.verifying_card(), reply_markup=pui.verifying_keyboard(), parse_mode='HTML',
    )

    await pui.edit_or_reply(
        processing_msg,
        pui.pending_review_card(
            gateway_key="manual",
            gateway_label_override=method_name,
            amount=amount_line,
            order_id=transaction_id,
            txn_id=stored_txid,
        ),
        reply_markup=pui.pending_review_keyboard(),
    )

    # Notify admin with the standardized review card + action buttons.
    # Per-order dedup: only the submission that flips review_notified
    # False→True actually alerts admins — if the user resends proof while
    # still in this conversation step, later attempts are silently skipped.
    review_claimed = False
    try:
        def _claim_review(_tx_id):
            with get_db_session() as _rsess:
                claimed = _rsess.query(Transaction).filter(
                    Transaction.id == _tx_id,
                    Transaction.review_notified.is_(False),
                ).update(
                    {Transaction.review_notified: True},
                    synchronize_session=False,
                ) == 1
                _rsess.commit()
                return claimed
        review_claimed = await run_db(_claim_review, transaction_id)
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
        full_name=update.effective_user.full_name,
        username=update.effective_user.username,
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
    if not has_permission(update.effective_user.id, "manage_payments"):
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

    if not has_permission(update.effective_user.id, "manage_payments"):
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
            Transaction.payment_method.in_(pui.reviewable_methods()),
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

        from services.payment_workflow import is_foreign_currency_gateway
        is_gateway_manual = is_foreign_currency_gateway(tx.payment_method)
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
            if tx.payment_method and tx.payment_method.value == "zinipay":
                _dep_pm_label = pui.zinipay_provider_meta(crypto_address=tx.crypto_address)[0]
            else:
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
            if tx and tx.payment_method and tx.payment_method.value == "zinipay":
                _dep_method = pui.zinipay_provider_meta(crypto_address=tx.crypto_address)[0]
            else:
                _dep_method = tx.payment_method.value if tx else "Manual"
            _asyncio.create_task(_notify_admins(
                context.bot,
                "deposit",
                _render_notif("💰", "Deposit Approved", [
                    ("Deposit ID", _fmt_did(tx_id)),
                    ("Amount", _dep_amt_str),
                    ("Payment Method", _dep_method),
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

    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        tx_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    def _reject_tx(_tx_id):
        with get_db_session() as session:
            flipped = session.query(Transaction).filter(
                Transaction.id == _tx_id,
                Transaction.payment_method.in_(pui.reviewable_methods()),
                Transaction.status.in_([
                    TransactionStatus.PENDING,
                    TransactionStatus.AWAITING_CONFIRMATION,
                ]),
            ).update(
                {Transaction.status: TransactionStatus.REJECTED},
                synchronize_session=False,
            )
            if flipped == 0:
                return None  # Already processed
            session.commit()

            tx = session.query(Transaction).filter_by(id=_tx_id).first()
            user = session.query(User).filter_by(id=tx.user_id).first() if tx else None
            _is_gateway_manual = False
            _user_tg_id = None
            _amount = 0.0
            if tx:
                from services.payment_workflow import is_foreign_currency_gateway
                _is_gateway_manual = is_foreign_currency_gateway(tx.payment_method)
            if user:
                _user_tg_id = user.telegram_id
                _amount = tx.amount
            return _user_tg_id, _amount, _is_gateway_manual

    _result = await run_db(_reject_tx, tx_id)
    if _result is None:
        return  # Already processed
    user_tg_id, amount, is_gateway_manual = _result

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



@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Crypto Wallet payment method selection."""
    query = update.callback_query
    await safe_answer(query)

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
            # Show the ONE dedicated Pending Deposit notice (Continue / Cancel
            # / Back) instead of the Payment Page itself — see
            # pending_deposit_continue for what "Continue Deposit" re-opens.
            # This keeps CryptoBot consistent with Binance Pay / Bybit Pay and
            # avoids duplicating the "deposit in progress" warning inside the
            # actual Payment Page.
            expires_str = (
                _time_remaining(existing_pending.expires_at)
                if existing_pending.expires_at else None
            )
            try:
                await query.edit_message_text(
                    pui.pending_deposit_card(
                        method_label="CryptoBot", method_emoji="🤖",
                        amount=_plain_usd(existing_pending.amount),
                        deposit_id=existing_pending.id, created_at=existing_pending.created_at,
                        expires_at=expires_str,
                    ),
                    reply_markup=pui.pending_deposit_keyboard(
                        continue_cb=f"pending_continue:{existing_pending.id}",
                    ),
                    parse_mode='HTML',
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return METHOD

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

        # Show payment invoice
        _amount_str = _plain_usd(usd_amount)
        message = pui.invoice_card(
            method_label="CryptoBot", method_emoji="🤖",
            amount=_amount_str, deposit_id=transaction.id,
            created_at=transaction.created_at, expires_at="30 minutes",
            instruction="👉 Tap below to pay with any supported cryptocurrency.",
        )
        reply_markup = pui.invoice_keyboard(
            amount_value=_amount_str,
            pay_url=pay_url, pay_url_label="💳 Pay with Any Crypto",
        )

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
    """Dispatcher: routes gateways that support a manual-mode toggle (per
    the Payment Gateway Registry — currently bKash/Nagad) to their
    manual-mode flow if the admin has enabled it (see
    services/gateway_manual_mode.py), otherwise creates the payment via the
    automated gateway. Gateways without the toggle (e.g. Cryptomus) always
    go automated."""
    from services.payment_workflow import supports_manual_toggle
    if supports_manual_toggle(gateway_key) and gw_mode.is_manual(gateway_key):
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
                _time_remaining(existing_pending.expires_at)
                if existing_pending.expires_at else None
            )
            if not pay_url:
                logger.warning(
                    "Existing pending %s transaction #%s has no valid pay_url (crypto_address=%r)",
                    gateway_label, existing_pending.id, existing_pending.crypto_address,
                )
            # Show the ONE dedicated Pending Deposit notice (Continue / Cancel
            # / Back) instead of the Payment Page itself, matching CryptoBot /
            # Binance Pay / Bybit Pay — see pending_deposit_continue for what
            # "Continue Deposit" re-opens.
            await update.message.reply_text(
                pui.pending_deposit_card(
                    method_label=gateway_label, method_emoji="💳",
                    amount=_plain_usd(existing_pending.amount),
                    deposit_id=existing_pending.id, expires_at=expires_str,
                ),
                reply_markup=pui.pending_deposit_keyboard(
                    continue_cb=f"pending_continue:{existing_pending.id}",
                ),
                parse_mode='HTML',
            )
            return METHOD

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
        def _mark_failed(_tx_id):
            with get_db_session() as _fail_session:
                _fail_session.query(Transaction).filter(
                    Transaction.id == _tx_id,
                    Transaction.status == TransactionStatus.PENDING,
                ).update(
                    {Transaction.status: TransactionStatus.FAILED},
                    synchronize_session=False,
                )
                _fail_session.commit()
        await run_db(_mark_failed, new_tx_id)
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

        _amount_str = _plain_usd(usd_amount)
        message = pui.invoice_card(
            method_label=gateway_label, method_emoji="💳",
            amount=_amount_str, deposit_id=transaction.id,
            expires_at="30 minutes",
            instruction=f"👉 Tap below to pay via {gateway_label}.",
        )
        if not pay_url:
            message += "\n\n⚠️ Payment link missing — contact support with your Deposit ID above."
            logger.warning(
                "New %s transaction #%s has no valid pay_url (reference=%r)",
                gateway_label, transaction.id, reference,
            )
        keyboard = pui.invoice_keyboard(
            amount_value=_amount_str,
            pay_url=pay_url, pay_url_label=pay_button_label,
        )
        await update.message.reply_text(
            message, reply_markup=keyboard, parse_mode='HTML',
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

    amount_str = f"৳{bdt_amount:.2f}"
    _rate = (bdt_amount / usd_amount) if usd_amount else 0.0
    message = pui.mobile_money_invoice(
        provider_label=gateway_label, provider_emoji=emoji,
        amount=amount_str, send_to=merchant_number,
        deposit_id=transaction_id,
        instruction="📌 Send the exact amount, then submit your TrxID.",
        wallet_credit=_plain_usd(usd_amount),
        exchange_rate=f"1 USD = ৳{_rate:.2f}",
    )
    # Amount and number are tap-to-copy in the message body; only Cancel here.
    keyboard = pui.invoice_keyboard(submit_cb=None, cancel_cb="cancel")
    await update.message.reply_text(
        message, reply_markup=keyboard, parse_mode='HTML',
    )

    text, submit_keyboard = pui.submit_txid_prompt(
        "mobile_money", cancel_cb="cancel", provider_name=gateway_label
    )
    await update.message.reply_text(text, reply_markup=submit_keyboard, parse_mode='HTML')

    return MANUAL_TXID


# ==================== ZINIPAY FLOW ====================
# See services/zinipay_payment.py for the verify+confirm logic.
# The user is shown payment instructions (merchant number from bot_config),
# then asked to submit their TXID.  Verification happens in-bot via the
# ZiniPay /v1/trx/verify → /v1/trx/confirm API; no hosted checkout link,
# no webhook, no background polling.

async def _finish_zinipay_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usd_amount: float,
    provider: Optional[str] = None,
):
    """Create the internal order for a ZiniPay top-up and show the payment
    instruction screen, then ask the user for their Transaction ID.

    The user pays via bKash / Nagad / Rocket / Upay directly to the merchant
    numbers configured in the admin panel.  The BDT amount (converted from USD
    using the admin-configured or global exchange rate) is shown to the user
    and stored in Transaction.crypto_address so it can be used for ZiniPay
    API verification later.

    ``provider`` is the specific bKash/Nagad/Rocket/Upay button the user
    tapped on the Mobile Money submenu (see payment_method_zinipay_bkash /
    _nagad / _rocket above). When given and that provider actually has a
    number configured, it takes priority over the admin's configured
    default provider — this is what makes selecting Nagad or Rocket show
    that provider's own payment page instead of always falling back to
    bKash.
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

    # Resolve which provider's number to show BEFORE creating the order, so
    # the choice can be persisted on the Transaction row itself.
    PROVIDER_EMOJI = {"bkash": "💗", "nagad": "🧡", "rocket": "💜", "upay": "🔵"}
    PROVIDER_LABEL = {"bkash": "bKash", "nagad": "Nagad", "rocket": "Rocket", "upay": "Upay"}
    numbers_by_provider = {
        "bkash": bkash_num, "nagad": nagad_num, "rocket": rocket_num, "upay": upay_num,
    }
    requested_provider = (provider or "").strip().lower() or None
    if requested_provider and numbers_by_provider.get(requested_provider):
        # The user explicitly picked this provider on the Mobile Money
        # submenu (bKash / Nagad / Rocket / Upay) — always honor that choice over
        # the admin's configured default, as long as a number is set for it.
        provider = requested_provider
    else:
        # The stored Default Provider is only honored while it's actually
        # configured. If it has since become "Not Configured" (its wallet
        # number was cleared), fall back to the first configured provider in
        # bKash → Nagad → Rocket → Upay order — see services/zinipay_payment.py.
        if numbers_by_provider.get(default_provider):
            provider = default_provider
        else:
            try:
                from services.zinipay_payment import first_configured_provider
                provider = first_configured_provider(numbers_by_provider)
            except Exception:
                logger.exception("Failed to resolve fallback ZiniPay provider")
                provider = next((p for p, n in numbers_by_provider.items() if n), None)
    send_to = numbers_by_provider.get(provider)
    if not provider or not send_to:
        await update.message.reply_text(
            "❌ No payment numbers configured yet. Please contact support."
        )
        return ConversationHandler.END

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
            # Recover the provider AND the exact BDT amount this pending
            # order was created with (stored as "bdt:<amount>:<provider>")
            # via the single shared helper, so the notice always reflects
            # what the user is really supposed to pay — never a generic
            # combined label and never a re-derived figure. Legacy rows
            # created before the provider was tracked fall back to
            # whichever provider was just requested, then the admin's
            # default, exactly like a brand-new order would resolve it.
            from services.zinipay_payment import resolve_bdt_amount
            pending_bdt_amount, pending_provider = resolve_bdt_amount(
                existing_pending.amount, existing_pending.crypto_address
            )
            if not pending_provider or not numbers_by_provider.get(pending_provider):
                pending_provider = provider

            pending_emoji = PROVIDER_EMOJI.get(pending_provider, "🇧🇩")
            pending_label = PROVIDER_LABEL.get(pending_provider, "Mobile Banking")

            # Show the ONE dedicated Pending Deposit notice (Continue /
            # Cancel / Back) instead of the Payment Page itself — see
            # pending_deposit_continue for what "Continue Deposit" re-opens.
            await update.message.reply_text(
                pui.pending_deposit_card(
                    method_label=pending_label, method_emoji=pending_emoji,
                    amount=_plain_usd(existing_pending.amount),
                    secondary_amount=f"৳{pending_bdt_amount:.2f}",
                    deposit_id=existing_pending.id,
                    expires_at=_time_remaining(existing_pending.expires_at)
                    if existing_pending.expires_at else None,
                ),
                reply_markup=pui.pending_deposit_keyboard(
                    continue_cb=f"pending_continue:{existing_pending.id}",
                ),
                parse_mode='HTML',
            )
            return METHOD

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.ZINIPAY,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(cfg.get_int("payment_expiry_minutes", 30) / 60.0),
            # Store expected BDT amount AND the selected provider so
            # zinipay_txid_received can verify against the correct
            # local-currency figure and show the right provider in any
            # admin-review card. Format: "bdt:<amount>:<provider>".
            crypto_address=f"bdt:{bdt_amount:.2f}:{provider}",
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        tx_id = transaction.id

    amount_str = f"৳{bdt_amount:.2f}"
    message = pui.mobile_money_invoice(
        provider_label=PROVIDER_LABEL.get(provider, "Mobile Banking"),
        provider_emoji=PROVIDER_EMOJI[provider],
        amount=amount_str, send_to=send_to,
        deposit_id=tx_id, expires_at="30 Minutes",
        wallet_credit=_plain_usd(usd_amount),
        exchange_rate=f"1 USD = ৳{rate:.2f}",
    )
    # Only Submit + Cancel — amount/number remain copyable through native controls.
    keyboard = pui.invoice_keyboard(
        submit_cb=f"zinipay_submit:{tx_id}", submit_label="🧾 Submit Transaction ID",
    )
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

    # Resolve the provider label (bKash / Nagad / Rocket / Upay) for the
    # prompt wording — purely cosmetic, no change to verification logic.
    _submit_provider: Optional[str] = None
    with get_db_session() as _psess:
        _ptx = _psess.query(Transaction).filter_by(id=tx_id).first()
        if _ptx and _ptx.crypto_address and _ptx.crypto_address.startswith("bdt:"):
            _pparts = _ptx.crypto_address.split(":")
            if len(_pparts) > 2 and _pparts[2]:
                _submit_provider = _pparts[2].strip().title()

    context.user_data['zinipay_tx_id'] = tx_id
    text, keyboard = pui.submit_txid_prompt(
        "mobile_money", cancel_cb="zinipay_cancel_submit", provider_name=_submit_provider
    )
    await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
    return ZINIPAY_TXID


async def zinipay_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped on the "Enter Transaction ID" prompt — return to
    the ZiniPay (Mobile Banking) Payment Details (invoice) screen.

    Pure navigation: the pending order is never touched, modified, or
    cancelled here. Re-renders the exact same invoice the user saw before
    tapping "Submit Transaction ID", straight from the still-PENDING
    transaction row and the admin's currently-configured payment number.
    """
    query = update.callback_query
    await query.answer()
    tx_id = context.user_data.pop('zinipay_tx_id', None)

    reopenable = False
    if tx_id:
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(id=tx_id).first()
            reopenable = bool(
                tx and tx.user.telegram_id == update.effective_user.id
                and tx.payment_method == PaymentMethod.ZINIPAY
                and tx.status == TransactionStatus.PENDING
                and not (tx.expires_at and datetime.utcnow() > tx.expires_at)
            )
            if reopenable:
                usd_amount = tx.amount
                crypto_address = tx.crypto_address or ""
                deposit_id = tx.id

            from database.models import PaymentGatewayConfig as _PGC
            pgc = session.query(_PGC).filter_by(gateway="zinipay").first()
            numbers_by_provider = {
                "bkash":  (pgc.zinipay_bkash_number  or "").strip() if pgc else "",
                "nagad":  (pgc.zinipay_nagad_number   or "").strip() if pgc else "",
                "rocket": (pgc.zinipay_rocket_number  or "").strip() if pgc else "",
                "upay":   (pgc.zinipay_upay_number    or "").strip() if pgc else "",
            }

        if reopenable:
            from services.zinipay_payment import resolve_bdt_amount
            bdt_amount, provider = resolve_bdt_amount(usd_amount, crypto_address)
            send_to = numbers_by_provider.get(provider) if provider else None

            if send_to:
                PROVIDER_EMOJI = {"bkash": "💗", "nagad": "🧡", "rocket": "💜", "upay": "🔵"}
                PROVIDER_LABEL = {"bkash": "bKash", "nagad": "Nagad", "rocket": "Rocket", "upay": "Upay"}
                amount_str = f"৳{bdt_amount:.2f}"
                _rate = (bdt_amount / usd_amount) if usd_amount else 0.0
                message = pui.mobile_money_invoice(
                    provider_label=PROVIDER_LABEL.get(provider, "Mobile Banking"),
                    provider_emoji=PROVIDER_EMOJI.get(provider, "🇧🇩"),
                    amount=amount_str, send_to=send_to,
                    deposit_id=deposit_id,
                    wallet_credit=_plain_usd(usd_amount),
                    exchange_rate=f"1 USD = ৳{_rate:.2f}",
                )
                keyboard = pui.invoice_keyboard(
                    submit_cb=f"zinipay_submit:{deposit_id}",
                    submit_label="🧾 Submit Transaction ID",
                )
                try:
                    await query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return ConversationHandler.END

    # Order no longer available, or its payment number was removed since —
    # fall back to the still-pending resubmit screen instead of a dead end.
    resubmit_cb = f"zinipay_submit:{tx_id}" if tx_id else None
    try:
        await query.edit_message_text(
            "This order is no longer available.\n\n"
            "It may have expired or already been completed.",
            reply_markup=pui.still_pending_keyboard(resubmit_cb),
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
        # Recover the expected BDT amount (and the provider the user was
        # shown) stored at order-creation time — see
        # services/zinipay_payment.py:resolve_bdt_amount, the single helper
        # every ZiniPay screen uses for this so bKash/Nagad/Rocket always
        # agree on the figure. Falls back gracefully for old rows that only
        # stored "bdt:<amount>" or nothing at all.
        from services.zinipay_payment import resolve_bdt_amount
        bdt_amount, selected_provider = resolve_bdt_amount(usd_amount, tx.crypto_address)

    # ---- Step 1: Auto-verify — retried automatically several times before
    # this ever reaches manual review (services/payment_workflow.py). ----
    # Never leave the user on the input screen: show the premium
    # "Verifying Your Payment" screen immediately, with all action buttons
    # disabled, while auto-verification runs.
    processing_msg = await update.message.reply_text(
        pui.mobile_money_verifying_card(
            txid=txid_raw,
            deposit_id=pui.format_deposit_id(tx_id),
        ),
        reply_markup=pui.verifying_keyboard(),
        parse_mode='HTML',
    )

    from services.payment_workflow import (
        run_auto_verification_with_retries, VerificationLockBusy,
        VERIFY_SUCCESS, VERIFY_TERMINAL, VERIFY_RETRYABLE, VERIFY_EXHAUSTED,
    )

    svc = ZiniPayService()

    def _classify_zinipay(raw):
        if raw is not None:
            return VERIFY_SUCCESS, "confirmed"
        err = (svc.last_error or "").lower()
        # A wrong amount, missing config, or bad request will never change
        # on retry — no point burning attempts on those.
        if "wrong amount" in err or "amount" in err:
            return VERIFY_TERMINAL, svc.last_error or "amount mismatch"
        if "not configured" in err or "must supply" in err or "invalid" in err:
            return VERIFY_TERMINAL, svc.last_error or "not configured"
        # HTTP/API errors, timeouts, or "not visible yet" — worth retrying.
        return VERIFY_RETRYABLE, svc.last_error or "verification not confirmed yet"

    try:
        verify_result, verify_kind, _verify_detail = await run_auto_verification_with_retries(
            gateway_id="zinipay",
            tx_id=tx_id,
            attempt_fn=lambda: svc.verify_transaction(amount=bdt_amount, transaction_id=txid_raw),
            classify=_classify_zinipay,
            telegram_user_id=telegram_id,
            submitted_txid=txid_raw,
        )
    except VerificationLockBusy:
        await processing_msg.edit_text(
            "⏳ Your previous submission for this order is still being verified — please wait."
        )
        return ZINIPAY_TXID

    # VERIFY_EXHAUSTED (ran out of automatic attempts without a definitive
    # yes/no — e.g. a slow network confirmation) reads better to the user
    # as "still in progress" than as a hard failure. Purely cosmetic: the
    # deposit is still queued for admin review exactly as before.
    _still_processing = verify_result is None and verify_kind == VERIFY_EXHAUSTED

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
            from services.payment_workflow import enqueue_pending_review
            with get_db_session() as _sess:
                pmv = enqueue_pending_review(
                    _sess,
                    gateway_id="zinipay",
                    telegram_user_id=telegram_id,
                    internal_order_id=tx_id,
                    submitted_txid=txid_raw,
                    amount=usd_amount,
                    currency="USD",
                    payment_type="mobile_banking",
                    auto_outcome="AUTO_VERIFY_FAILED",
                    auto_detail=(f"{error_detail} (expected ৳{bdt_amount:.2f} BDT)")[:500],
                )
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
                def _claim_review(_tx_id):
                    with get_db_session() as _rsess:
                        claimed = _rsess.query(Transaction).filter(
                            Transaction.id == _tx_id,
                            Transaction.review_notified.is_(False),
                        ).update(
                            {Transaction.review_notified: True},
                            synchronize_session=False,
                        ) == 1
                        _rsess.commit()
                        return claimed
                review_claimed = await run_db(_claim_review, tx_id)
            except Exception:
                logger.exception("Failed to claim review_notified for tx %s (zinipay)", tx_id)
                review_claimed = False

            if review_claimed:
                try:
                    order_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    for admin_id in _gateway_admin_recipient_ids():
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="zinipay",
                                    gateway_label_override=(
                                        selected_provider.title()
                                        if selected_provider else None
                                    ),
                                    amount=f"৳{bdt_amount:.2f} BDT (${usd_amount:.2f} USD)",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    full_name=update.effective_user.full_name,
                                    username=update.effective_user.username,
                                    user_id=telegram_id,
                                    time_str=order_time,
                                    status_key="pending_review",
                                    verification_status="failed",
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
        pending_review_kb = None
        if is_amount_mismatch and pmv_id:
            user_msg = pui.mobile_money_verification_pending_card()
            pending_review_kb = pui.pending_review_keyboard()
        elif is_amount_mismatch:
            user_msg = (
                f"❌ Amount mismatch.\n\nThe transaction amount does not match the expected "
                f"৳{bdt_amount:.2f} BDT. Please ensure you sent the correct amount."
            )
        elif pmv_id:
            # Both the "exhausted retries" and "terminal failure" paths show
            # the same premium "Verification in Progress" screen — the user
            # doesn't need to distinguish between them; the deposit is queued
            # for admin review in both cases. Layout is purely cosmetic here.
            user_msg = pui.mobile_money_verification_pending_card()
            pending_review_kb = pui.pending_review_keyboard()
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
            await processing_msg.edit_text(user_msg, reply_markup=pending_review_kb, parse_mode='HTML')
            if pmv_id:
                pui.remember_pending_message(pmv_id, processing_msg.chat_id, processing_msg.message_id)
        except Exception:
            sent = await update.message.reply_text(user_msg, reply_markup=pending_review_kb, parse_mode='HTML')
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

    # Prefer the provider ZiniPay's own API confirmed the payment came from;
    # fall back to the provider the user was shown at invoice-creation time.
    # Never fall back to the generic "bKash • Nagad • Rocket" combined label.
    _success_provider = (verify_result.provider or selected_provider or "").strip().lower() or None
    _provider_label, _provider_emoji = pui.zinipay_provider_meta(provider=_success_provider)
    success_text = pui.deposit_success_card(
        amount=f"${usd_amount:.2f} USD",
        payment_method=f"{_provider_emoji} {_provider_label}",
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
    """Currency picker shown for an already-created Binance Pay order
    (``tx_id`` is a real, PENDING Transaction/Deposit ID by this point —
    see ``_finish_binance_payment``), so per the navigation spec it gets
    the real, destructive "❌ Cancel" row alongside "⬅️ Back" — see
    ``services/payment_ui.py:with_deposit_cancel``."""
    svc = BinancePayService()
    row = [
        InlineKeyboardButton(c, callback_data=f"binance_currency:{tx_id}:{c}")
        for c in svc.allowed_currencies
    ]
    return pui.with_deposit_cancel(
        InlineKeyboardMarkup([row, [InlineKeyboardButton("⬅️ Back", callback_data="back_payment_methods")]])
    )


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
                        pui.pending_deposit_card(
                            method_label="Binance Pay", method_emoji="🟡",
                            amount=_plain_usd(existing_pending.amount),
                            deposit_id=existing_pending.id, created_at=existing_pending.created_at,
                            expires_at=_time_remaining(existing_pending.expires_at)
                            if existing_pending.expires_at else None,
                        ),
                        reply_markup=pui.pending_deposit_keyboard(
                            continue_cb=f"pending_continue:{existing_pending.id}",
                        ),
                        parse_mode='HTML',
                    )
                    return METHOD
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
                f"🟡 <b>Binance Pay</b>\n\n"
                f"Choose the currency you will send for <b>${usd_amount:.2f}</b>.",
                reply_markup=_binance_currency_keyboard(tx_id),
                parse_mode="HTML",
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
                pui.pending_deposit_card(
                    method_label="Binance Pay", method_emoji="🟡",
                    amount=_plain_usd(existing_pending.amount),
                    deposit_id=existing_pending.id, created_at=existing_pending.created_at,
                    expires_at=_time_remaining(existing_pending.expires_at)
                    if existing_pending.expires_at else None,
                ),
                reply_markup=pui.pending_deposit_keyboard(
                    continue_cb=f"pending_continue:{existing_pending.id}",
                ),
                parse_mode='HTML',
            )
            return METHOD

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


@guarded_callback(fallback_state=ConversationHandler.END)
async def binance_currency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked USDT/USDC for a Binance Pay order created without a currency yet."""
    query = update.callback_query
    await safe_answer(query)
    try:
        _, tx_id_s, currency = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await safe_answer(query, "Invalid selection", show_alert=True)
        return ConversationHandler.END

    svc = BinancePayService()
    if currency not in svc.allowed_currencies:
        await safe_answer(query, "Unsupported currency", show_alert=True)
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
    amount_str = f"{usd_amount:.2f} {currency}"
    message = pui.binance_pay_invoice(
        amount=amount_str,
        pay_id=svc.pay_id,
        deposit_id=tx_id,
        expires_at=f"{svc.order_expiry_minutes} Minutes",
    )
    keyboard = pui.binance_pay_keyboard(
        submit_cb=f"binance_submit:{tx_id}",
    )
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
    text, keyboard = pui.binance_order_id_prompt(cancel_cb="binance_cancel_submit")
    await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
    return BINANCE_TXID


async def binance_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped on the "Enter Order ID" prompt — return to the
    Binance Pay Payment Details (invoice) screen.

    Pure navigation: the pending order is never touched, modified, or
    cancelled here. Re-renders the exact same invoice the user saw before
    tapping "Submit Order ID", straight from the still-PENDING transaction
    row, so the user can re-check the Pay ID/amount or tap Submit again.
    """
    query = update.callback_query
    await query.answer()
    tx_id = context.user_data.pop('binance_tx_id', None)

    if tx_id:
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(id=tx_id).first()
            reopenable = bool(
                tx and tx.user.telegram_id == update.effective_user.id
                and tx.payment_method == PaymentMethod.BINANCE_PAY
                and tx.status == TransactionStatus.PENDING
                and tx.crypto_address
                and not (tx.expires_at and datetime.utcnow() > tx.expires_at)
            )
            if reopenable:
                usd_amount, currency = tx.amount, tx.crypto_address
        if reopenable:
            svc = BinancePayService()
            await _send_binance_payment_screen(
                update, context, tx_id, usd_amount, currency, svc, is_new_message=False,
            )
            return ConversationHandler.END

    # Order no longer available (expired/completed/not found) — fall back
    # to the still-pending resubmit screen instead of a dead end.
    resubmit_cb = await _active_resubmit_callback(tx_id, "binance_submit")
    try:
        await query.edit_message_text(
            "This order is no longer available.\n\n"
            "It may have expired or already been completed.",
            reply_markup=pui.still_pending_keyboard(
                resubmit_cb,
                resubmit_label="🧾 Submit Order ID Again",
            ),
            parse_mode="HTML",
        )
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
        await update.message.reply_text(
            "❌ Session expired. Please start again from your pending deposit."
        )
        return ConversationHandler.END

    if not is_valid_txid_format(txid_raw):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Order ID. Please enter the exact "
            "Order ID from your completed Binance Pay payment."
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

    # Never leave the user on the input screen: show the standard
    # "Verifying Payment" status immediately, with all action buttons
    # disabled, while auto-verification runs.
    processing_msg = await update.message.reply_text(
        pui.binance_verifying_card(
            order_id=txid_raw,
            deposit_id=pui.format_deposit_id(tx_id, order_created_at),
        ),
        reply_markup=pui.verifying_keyboard(),
        parse_mode='HTML',
    )

    # Prevent two concurrent submissions for the SAME order from both
    # racing the (slow) Binance API call in parallel.
    lock = get_order_lock(telegram_id, tx_id)
    if not lock.acquire(blocking=False):
        await pui.edit_or_reply(
            processing_msg,
            "⏳ Your previous submission for this order is still being verified — please wait.",
        )
        return BINANCE_TXID

    from services.payment_workflow import (
        run_auto_verification_with_retries, VerificationLockBusy,
        VERIFY_SUCCESS, VERIFY_TERMINAL, VERIFY_RETRYABLE, VERIFY_EXHAUSTED,
    )

    # Outcomes that are deterministic — will not change on retry — so the
    # engine finalizes on the first attempt instead of burning retries.
    _BINANCE_TERMINAL_OUTCOMES = {
        VerificationOutcome.NOT_CONFIGURED,
        VerificationOutcome.AMOUNT_MISMATCH,
        VerificationOutcome.WRONG_DIRECTION,
        VerificationOutcome.CURRENCY_MISMATCH,
        VerificationOutcome.TOO_OLD,
    }

    def _classify_binance(raw_result):
        if raw_result.outcome == VerificationOutcome.SUCCESS:
            return VERIFY_SUCCESS, "confirmed"
        if raw_result.outcome in _BINANCE_TERMINAL_OUTCOMES:
            return VERIFY_TERMINAL, str(getattr(raw_result, "detail", "") or raw_result.outcome)
        # API_ERROR / NOT_FOUND — transient, may resolve on the next check.
        return VERIFY_RETRYABLE, str(getattr(raw_result, "detail", "") or raw_result.outcome)

    svc = BinancePayService()
    try:
        try:
            result, verify_kind, _verify_detail = await run_auto_verification_with_retries(
                gateway_id="binance_pay",
                tx_id=tx_id,
                attempt_fn=lambda: svc.verify_transaction(
                    transaction_id=txid_raw, expected_amount=expected_amount,
                    currency=currency, order_created_at=order_created_at,
                ),
                classify=_classify_binance,
                telegram_user_id=telegram_id,
                submitted_txid=txid_raw,
            )
        except VerificationLockBusy:
            await pui.edit_or_reply(
                processing_msg,
                "⏳ Your previous submission for this order is still being verified — please wait.",
            )
            return BINANCE_TXID
    finally:
        lock.release()

    # VERIFY_EXHAUSTED (ran out of automatic attempts without a definitive
    # yes/no) reads better to the user as "still in progress" than as a
    # hard failure. Purely cosmetic: the deposit is still queued for admin
    # review exactly as before.
    _still_processing = verify_kind == VERIFY_EXHAUSTED

    # ---- Outcomes that are clear, pre-API-call user errors — return inline,
    # no admin notification (nothing for an admin to review yet). ----
    if result.outcome == VerificationOutcome.NOT_CONFIGURED:
        await pui.edit_or_reply(
            processing_msg,
            "⚠️ Payment verification is temporarily unavailable.\n\n"
            "Please try again shortly.",
        )
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
                from services.payment_workflow import enqueue_pending_review
                with get_db_session() as _sess:
                    pmv = enqueue_pending_review(
                        _sess,
                        gateway_id="binance_pay",
                        telegram_user_id=telegram_id,
                        internal_order_id=tx_id,
                        submitted_txid=txid_raw,
                        amount=expected_amount,
                        currency=currency,
                        auto_outcome=outcome_str,
                        auto_detail=detail_str[:500] if detail_str else None,
                    )
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
                    def _claim_review(_tx_id):
                        with get_db_session() as _rsess:
                            claimed = _rsess.query(Transaction).filter(
                                Transaction.id == _tx_id,
                                Transaction.review_notified.is_(False),
                            ).update(
                                {Transaction.review_notified: True},
                                synchronize_session=False,
                            ) == 1
                            _rsess.commit()
                            return claimed
                    review_claimed = await run_db(_claim_review, tx_id)
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
                    for admin_id in _gateway_admin_recipient_ids():
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="binance_pay",
                                    amount=f"{expected_amount} {currency}",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    full_name=update.effective_user.full_name,
                                    username=update.effective_user.username,
                                    user_id=telegram_id,
                                    status_key="pending_review",
                                    verification_status="failed",
                                    verification_reason=reason,
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

        # User-facing message — edit the "Verifying Payment" status message
        # in place rather than sending a new one, so the user always sees
        # one screen resolve to its final state.
        if result.outcome == VerificationOutcome.AMOUNT_MISMATCH:
            if pmv_id:
                sent = await pui.edit_or_reply(
                    processing_msg,
                    pui.pending_review_card(
                        gateway_key="binance_pay",
                        amount=f"{expected_amount} {currency}", order_id=tx_id, txn_id=txid_raw,
                        extra=[("📥", "Received", f"{result.received_amount} {result.currency or currency}")],
                        note="⚠️ Amount mismatch detected — our team has been notified and "
                             "will review your payment shortly.",
                    ),
                    reply_markup=pui.pending_review_keyboard(),
                )
                pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
            else:
                await pui.edit_or_reply(
                    processing_msg,
                    "❌ Payment amount mismatch.\n\n"
                    f"Expected: {expected_amount} {currency}\n"
                    f"Received: {result.received_amount} {result.currency or currency}",
                )
        elif pmv_id:
            status_card = (
                pui.binance_verification_pending_card(
                    order_id=txid_raw,
                    deposit_id=pui.format_deposit_id(tx_id, order_created_at),
                )
                if _still_processing else
                pui.pending_review_card(
                    gateway_key="binance_pay",
                    amount=f"{expected_amount} {currency}",
                    order_id=tx_id,
                    txn_id=txid_raw,
                )
            )
            sent = await pui.edit_or_reply(
                processing_msg, status_card, reply_markup=pui.pending_review_keyboard(),
            )
            pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
        else:
            await pui.edit_or_reply(
                processing_msg,
                "❌ Order ID could not be verified.\n\n"
                "Please check the Order ID and try again.",
            )
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
            await pui.edit_or_reply(processing_msg, "❌ This order is no longer pending.")
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
                await pui.edit_or_reply(
                    processing_msg,
                    "⚠️ Verification succeeded but crediting your balance failed. Please contact support with your Deposit ID: %s" % pui.format_deposit_id(tx_id),
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
        await pui.edit_or_reply(processing_msg, "❌ This transaction has already been used.")
        return ConversationHandler.END

    # ---- Verified — show the existing "Payment Verified / Deposit
    # Successful" card and credit the wallet (already done above). Edited
    # in place over the "Verifying Payment" status message. ----
    _bonus_str = f"+{bonus_amount:.2f} USD" if bonus_amount else None
    await pui.edit_or_reply(
        processing_msg,
        pui.deposit_success_card(
            amount=f"${credited_usd:.2f} USD",
            payment_method="Binance Pay",
            deposit_id=pui.format_deposit_id(tx_id, order_created_at),
            bonus_line=_bonus_str,
        ),
        reply_markup=pui.deposit_success_keyboard(),
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


def _bybit_pending_display(crypto_address: Optional[str]):
    """Resolve (method_label, method_emoji, network_label) for a PENDING
    Bybit-backed deposit for display purposes only.

    ``PaymentMethod.BYBIT_PAY`` backs two different user-selected payment
    methods — UID Transfer (real Bybit Pay) and on-chain crypto network
    deposits (TRC20/BEP20/ERC20/LTC/...) — distinguished only by the
    packed ``crypto_address`` meta string (see ``_bybit_meta`` /
    ``_parse_bybit_meta``). "Bybit Pay" must never be shown for the
    latter: on-chain deposits display as "Crypto" with the actual
    network the user selected. Never used for routing/business logic —
    presentation only.
    """
    payment_type, network = _parse_bybit_meta(crypto_address or "")
    if payment_type == "onchain" and network:
        return "Crypto", "🪙", pui.crypto_network_label(network)
    return "Bybit Pay", "🔷", None


def _bybit_type_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    """Payment-type picker (UID Transfer / On-chain) for an already-created
    Bybit Pay order (``tx_id`` is a real, PENDING Transaction/Deposit ID by
    this point), so it gets the real "❌ Cancel" row alongside Back — see
    ``services/payment_ui.py:with_deposit_cancel``."""
    return pui.with_deposit_cancel(InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 UID Transfer", callback_data=f"bybit_type:{tx_id}:uid")],
        [InlineKeyboardButton("🔹 On-chain Deposit", callback_data=f"bybit_type:{tx_id}:onchain")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_payment_methods")],
    ]))


def _bybit_network_keyboard(tx_id: int, svc: "BybitPayService") -> InlineKeyboardMarkup:
    """Network picker (TRC20/BEP20/...) for the same already-created Bybit
    Pay order — same reasoning as ``_bybit_type_keyboard`` above, so it
    also gets the real "❌ Cancel" row."""
    rows = [
        [InlineKeyboardButton(net, callback_data=f"bybit_network:{tx_id}:{net}")]
        for net in svc.networks_with_wallets()
    ]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"bybit_back_type:{tx_id}")])
    rows.append([InlineKeyboardButton("⬅️ Back to Payment Methods", callback_data="back_payment_methods")])
    return pui.with_deposit_cancel(InlineKeyboardMarkup(rows))


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
            _pm_label, _pm_emoji, _pm_network = _bybit_pending_display(existing_pending.crypto_address)
            await update.message.reply_text(
                pui.pending_deposit_card(
                    method_label=_pm_label, method_emoji=_pm_emoji, network=_pm_network,
                    amount=_plain_usd(existing_pending.amount),
                    deposit_id=existing_pending.id, created_at=existing_pending.created_at,
                    expires_at=_time_remaining(existing_pending.expires_at)
                    if existing_pending.expires_at else None,
                ),
                reply_markup=pui.pending_deposit_keyboard(
                    continue_cb=f"pending_continue:{existing_pending.id}",
                ),
                parse_mode='HTML',
            )
            return METHOD

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
            _pm_label, _pm_emoji, _pm_network = _bybit_pending_display(existing_pending.crypto_address)
            await update.message.reply_text(
                pui.pending_deposit_card(
                    method_label=_pm_label, method_emoji=_pm_emoji, network=_pm_network,
                    amount=_plain_usd(existing_pending.amount),
                    deposit_id=existing_pending.id, created_at=existing_pending.created_at,
                    expires_at=_time_remaining(existing_pending.expires_at)
                    if existing_pending.expires_at else None,
                ),
                reply_markup=pui.pending_deposit_keyboard(
                    continue_cb=f"pending_continue:{existing_pending.id}",
                ),
                parse_mode='HTML',
            )
            return METHOD

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
    def _update(_tx_id, _payment_type, _network):
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(id=_tx_id).first()
            if tx:
                tx.crypto_address = _bybit_meta(_payment_type, _network)
                session.commit()
    await run_db(_update, tx_id, payment_type, network)


@guarded_callback(fallback_state=ConversationHandler.END)
async def bybit_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked UID Transfer or On-chain Deposit for a pending Bybit Pay order."""
    query = update.callback_query
    await safe_answer(query)
    try:
        _, tx_id_s, choice = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await safe_answer(query, "Invalid selection", show_alert=True)
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
            await safe_answer(query, "UID Transfer is not available right now.", show_alert=True)
            return METHOD
        await _set_bybit_type(tx_id, "uid_transfer")
        await _send_bybit_uid_screen(update, context, tx_id, usd_amount, svc, is_new_message=False)
        return ConversationHandler.END

    if choice == "onchain":
        if not svc.networks_with_wallets():
            await safe_answer(query, "On-chain Deposit is not available right now.", show_alert=True)
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

    await safe_answer(query, "Invalid selection", show_alert=True)
    return METHOD


@guarded_callback(fallback_state=ConversationHandler.END)
async def bybit_back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'⬅️ Back' from the network list back to the UID/On-chain choice."""
    query = update.callback_query
    await safe_answer(query)
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    def _load_tx(_tx_id, _telegram_id):
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(
                id=_tx_id, payment_method=PaymentMethod.BYBIT_PAY, status=TransactionStatus.PENDING,
            ).first()
            if not tx or tx.user.telegram_id != _telegram_id:
                return None
            return tx.amount, tx.created_at

    _result = await run_db(_load_tx, tx_id, update.effective_user.id)
    if _result is None:
        return ConversationHandler.END
    usd_amount, tx_created_at = _result
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


@guarded_callback(fallback_state=ConversationHandler.END)
async def bybit_network_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked TRC20 / BEP20 / ERC20 for a Bybit on-chain deposit order."""
    query = update.callback_query
    await safe_answer(query)
    try:
        _, tx_id_s, network = query.data.split(":", 2)
        tx_id = int(tx_id_s)
    except (ValueError, IndexError):
        await safe_answer(query, "Invalid selection", show_alert=True)
        return ConversationHandler.END

    svc = BybitPayService()
    network = network.strip().upper()
    if network not in svc.networks_with_wallets():
        await safe_answer(query, "Unsupported or unavailable network", show_alert=True)
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
    amount_str = f"{usd_amount:.2f} {BYBIT_CURRENCY}"
    # Use the premium Bybit Pay invoice layout; amount and UID are tap-to-copy
    # in the message body so no separate copy buttons are needed on the keyboard.
    expires_label = f"{svc.order_expiry_minutes} Minutes"
    message = pui.bybit_pay_invoice(
        amount=amount_str, pay_id=svc.uid,
        deposit_id=tx_id, expires_at=expires_label,
    )
    keyboard = pui.bybit_pay_keyboard(
        submit_cb=f"bybit_submit:{tx_id}", cancel_cb="cancel",
    )
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
    expires_label = f"{svc.order_expiry_minutes} Minutes"
    if locked_crypto_amount is not None and locked_rate is not None:
        # Non-stablecoin order (e.g. LTC) — the exact crypto amount already
        # bakes in the rate, so no separate exchange-rate line is needed.
        amount_str = f"{locked_crypto_amount:.8f} LTC"
        message = pui.crypto_invoice(
            network="LTC", amount=amount_str, wallet_address=address,
            deposit_id=tx_id, expires_at=expires_label,
        )
    else:
        amount_str = f"{usd_amount:.2f} {BYBIT_CURRENCY}"
        message = pui.crypto_invoice(
            network=network, amount=amount_str, wallet_address=address,
            deposit_id=tx_id, expires_at=expires_label,
        )
    # Amount and address are tap-to-copy in the message body; only Submit TxHash + Cancel.
    keyboard = pui.invoice_keyboard(
        submit_cb=f"bybit_submit:{tx_id}", submit_label="🧾 Submit TxHash",
    )
    if is_new_message:
        await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


@guarded_callback(fallback_state=ConversationHandler.END)
async def pending_deposit_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"▶️ Continue Deposit" tapped on the Pending Deposit notice.

    Purely re-renders the existing PENDING order's payment-instructions
    screen — CryptoBot, bKash / Nagad / Cryptomus / NOWPayments, ZiniPay
    (Mobile Banking), Binance Pay, and Bybit Pay are all covered — it never
    creates a new deposit and never touches deposit/verification logic,
    only reads the already existing Transaction row and hands its values
    to the same screen renderers used at order-creation time."""
    query = update.callback_query
    await safe_answer(query)
    try:
        tx_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await safe_answer(query, "Invalid deposit", show_alert=True)
        return ConversationHandler.END

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            text, keyboard, is_empty = await run_db(
                _build_topup_method_screen,
                amount=context.user_data.get('topup_amount'),
            )
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            return ConversationHandler.END if is_empty else METHOD
        if tx.user.telegram_id != update.effective_user.id:
            await safe_answer(query, "⛔ Not your deposit.", show_alert=True)
            return ConversationHandler.END
        if tx.status != TransactionStatus.PENDING:
            try:
                await query.edit_message_text(
                    "This deposit is no longer pending.\n\n"
                    "Choose a payment method to start a new deposit.",
                    reply_markup=pui.payment_expired_keyboard(),
                    parse_mode="HTML",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        method = tx.payment_method
        usd_amount = tx.amount
        crypto_address = tx.crypto_address
        created_at = tx.created_at
        expires_at = tx.expires_at
        locked_rate = tx.locked_crypto_rate
        locked_crypto_amount = tx.locked_crypto_amount

    if method == PaymentMethod.CRYPTO_WALLET:
        # CryptoBot: crypto_address stores "invoice_id|pay_url".
        if crypto_address and "|" in crypto_address:
            _invoice_id, pay_url = crypto_address.split("|", 1)
        else:
            pay_url = crypto_address or None
        expires_str = _time_remaining(expires_at) if expires_at else None
        _amount_str = _plain_usd(usd_amount)
        message = pui.invoice_card(
            method_label="CryptoBot", method_emoji="🤖",
            amount=_amount_str, deposit_id=tx_id,
            created_at=created_at, expires_at=expires_str,
            instruction="👉 Tap below to pay with any supported cryptocurrency.",
        )
        reply_markup = pui.invoice_keyboard(
            amount_value=_amount_str,
            pay_url=pay_url, pay_url_label="💳 Pay with Any Crypto",
        )
        try:
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    if method in (PaymentMethod.BKASH, PaymentMethod.NAGAD, PaymentMethod.CRYPTOMUS, PaymentMethod.NOWPAYMENTS):
        _gateway_meta = {
            PaymentMethod.BKASH: ("bKash", "📱 Pay with bKash"),
            PaymentMethod.NAGAD: ("Nagad", "🟠 Pay with Nagad"),
            PaymentMethod.CRYPTOMUS: ("Cryptomus", "💠 Pay with Cryptomus"),
            PaymentMethod.NOWPAYMENTS: ("NOWPayments", "🌐 Pay with NOWPayments"),
        }
        gateway_label, pay_button_label = _gateway_meta[method]
        pay_url = _extract_pay_url(crypto_address)
        expires_str = _time_remaining(expires_at) if expires_at else None
        _amount_str = _plain_usd(usd_amount)
        message = pui.invoice_card(
            method_label=gateway_label, method_emoji="💳",
            amount=_amount_str, deposit_id=tx_id, expires_at=expires_str,
        )
        if not pay_url:
            message += "\n\n⚠️ Payment link missing — contact support with your Deposit ID above."
        reply_markup = pui.invoice_keyboard(
            amount_value=_amount_str,
            pay_url=pay_url, pay_url_label=pay_button_label,
        )
        try:
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    if method == PaymentMethod.ZINIPAY:
        from services.zinipay_payment import ZiniPayService, resolve_bdt_amount
        PROVIDER_EMOJI = {"bkash": "💗", "nagad": "🧡", "rocket": "💜", "upay": "🔵"}
        PROVIDER_LABEL = {"bkash": "bKash", "nagad": "Nagad", "rocket": "Rocket", "upay": "Upay"}
        bdt_amount, pending_provider = resolve_bdt_amount(usd_amount, crypto_address)
        with get_db_session() as session:
            from database.models import PaymentGatewayConfig as _PGC
            pgc = session.query(_PGC).filter_by(gateway="zinipay").first()
            numbers_by_provider = {
                "bkash": (pgc.zinipay_bkash_number or "").strip() if pgc else "",
                "nagad": (pgc.zinipay_nagad_number or "").strip() if pgc else "",
                "rocket": (pgc.zinipay_rocket_number or "").strip() if pgc else "",
                "upay": (pgc.zinipay_upay_number or "").strip() if pgc else "",
            }
        if not pending_provider or not numbers_by_provider.get(pending_provider):
            pending_provider = next((p for p, n in numbers_by_provider.items() if n), pending_provider)
        send_to = numbers_by_provider.get(pending_provider)
        _bdt_amount = f"৳{bdt_amount:.2f}"
        _rate = (bdt_amount / usd_amount) if usd_amount else 0.0
        expires_str = _time_remaining(expires_at) if expires_at else None
        if send_to:
            message = pui.mobile_money_invoice(
                provider_label=PROVIDER_LABEL.get(pending_provider, "Mobile Banking"),
                provider_emoji=PROVIDER_EMOJI.get(pending_provider, "🇧🇩"),
                amount=_bdt_amount, send_to=send_to,
                deposit_id=tx_id, expires_at=expires_str,
                wallet_credit=_plain_usd(usd_amount),
                exchange_rate=f"1 USD = ৳{_rate:.2f}",
            )
        else:
            message = pui.invoice_card(
                method_label=PROVIDER_LABEL.get(pending_provider, "Mobile Banking"),
                method_emoji=PROVIDER_EMOJI.get(pending_provider, "🇧🇩"),
                amount=_bdt_amount, deposit_id=tx_id, expires_at=expires_str,
            )
            message += "\n\n⚠️ Payment number missing — contact support with your Deposit ID above."
        keyboard = pui.invoice_keyboard(
            submit_cb=f"zinipay_submit:{tx_id}", submit_label="🧾 Submit Transaction ID",
        )
        try:
            await query.edit_message_text(message, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    if method == PaymentMethod.BINANCE_PAY:
        svc = BinancePayService()
        if not crypto_address:
            # Currency was never chosen for this order yet — reopen the
            # currency picker instead of an invoice that doesn't exist yet.
            try:
                await query.edit_message_text(
                    f"🟡 <b>Binance Pay</b>\n\n"
                    f"Choose the currency you will send for <b>${usd_amount:.2f}</b>.",
                    reply_markup=_binance_currency_keyboard(tx_id),
                    parse_mode="HTML",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return METHOD
        await _send_binance_payment_screen(
            update, context, tx_id, usd_amount, crypto_address, svc, is_new_message=False,
        )
        return ConversationHandler.END

    if method == PaymentMethod.BYBIT_PAY:
        svc = BybitPayService()
        payment_type, network = _parse_bybit_meta(crypto_address)
        if payment_type == "onchain" and network:
            await _send_bybit_onchain_screen(
                update, context, tx_id, usd_amount, network, svc, is_new_message=False,
                locked_rate=locked_rate, locked_crypto_amount=locked_crypto_amount,
            )
        else:
            await _send_bybit_uid_screen(update, context, tx_id, usd_amount, svc, is_new_message=False)
        return ConversationHandler.END

    await safe_answer(query, "Unable to reopen this deposit.", show_alert=True)
    return ConversationHandler.END


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
    if payment_type == BybitPaymentType.UID_TRANSFER:
        # Bybit Pay UID Transfer — premium Order ID prompt with Bybit-specific wording.
        text, keyboard = pui.bybit_order_id_prompt(cancel_cb="bybit_cancel_submit")
    else:
        # On-chain networks — generic crypto TXID prompt (blockchain hash format).
        text, keyboard = pui.submit_txid_prompt("crypto", cancel_cb="bybit_cancel_submit")
    await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
    return BYBIT_TXID


async def bybit_cancel_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped on the "Enter Order ID" / "Enter Transaction Hash"
    prompt — return to the Bybit Pay Payment Details (invoice) screen.

    Pure navigation: the pending order is never touched, modified, or
    cancelled here. Re-renders the exact same invoice (UID Transfer or
    on-chain network, whichever this order is) the user saw before tapping
    Submit, straight from the still-PENDING transaction row.
    """
    query = update.callback_query
    await query.answer()
    tx_id = context.user_data.pop('bybit_tx_id', None)

    if tx_id:
        with get_db_session() as session:
            tx = session.query(Transaction).filter_by(id=tx_id).first()
            reopenable = bool(
                tx and tx.user.telegram_id == update.effective_user.id
                and tx.payment_method == PaymentMethod.BYBIT_PAY
                and tx.status == TransactionStatus.PENDING
                and not (tx.expires_at and datetime.utcnow() > tx.expires_at)
            )
            if reopenable:
                usd_amount = tx.amount
                crypto_address = tx.crypto_address
                locked_rate = tx.locked_crypto_rate
                locked_crypto_amount = tx.locked_crypto_amount
        if reopenable:
            svc = BybitPayService()
            payment_type, network = _parse_bybit_meta(crypto_address)
            if payment_type == "onchain" and network:
                await _send_bybit_onchain_screen(
                    update, context, tx_id, usd_amount, network, svc, is_new_message=False,
                    locked_rate=locked_rate, locked_crypto_amount=locked_crypto_amount,
                )
            else:
                await _send_bybit_uid_screen(update, context, tx_id, usd_amount, svc, is_new_message=False)
            return ConversationHandler.END

    # Order no longer available (expired/completed/not found) — fall back
    # to the still-pending resubmit screen instead of a dead end.
    resubmit_cb = await _active_resubmit_callback(tx_id, "bybit_submit")
    try:
        await query.edit_message_text(
            "This order is no longer available.\n\n"
            "It may have expired or already been completed.",
            reply_markup=pui.still_pending_keyboard(resubmit_cb),
        )
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

    # Never leave the user on the input screen: show the premium verifying
    # screen immediately — Bybit Pay UID uses Order ID; on-chain uses TxHash.
    _dep_id_fmt = pui.format_deposit_id(tx_id)
    if is_uid:
        _verifying_text = pui.bybit_verifying_card(order_id=txid_raw, deposit_id=_dep_id_fmt)
    else:
        _verifying_text = pui.crypto_verifying_card(txhash=txid_raw, deposit_id=_dep_id_fmt)
    processing_msg = await update.message.reply_text(
        _verifying_text, reply_markup=pui.verifying_keyboard(), parse_mode='HTML',
    )

    # Prevent two concurrent submissions for the SAME order from both
    # racing the (slow) Bybit API call in parallel.
    lock = bybit_get_order_lock(telegram_id, tx_id)
    if not lock.acquire(blocking=False):
        await pui.edit_or_reply(
            processing_msg,
            "⏳ Your previous submission for this order is still being verified — please wait.",
        )
        return BYBIT_TXID

    from services.payment_workflow import (
        run_auto_verification_with_retries, VerificationLockBusy,
        VERIFY_SUCCESS, VERIFY_TERMINAL, VERIFY_RETRYABLE, VERIFY_EXHAUSTED,
    )

    # Outcomes that are deterministic — will not change on retry.
    _BYBIT_TERMINAL_OUTCOMES = {
        BybitVerificationOutcome.NOT_CONFIGURED,
        BybitVerificationOutcome.AMOUNT_MISMATCH,
        BybitVerificationOutcome.CURRENCY_MISMATCH,
        BybitVerificationOutcome.NETWORK_MISMATCH,
        BybitVerificationOutcome.WRONG_ADDRESS,
        BybitVerificationOutcome.TOO_OLD,
        BybitVerificationOutcome.NOT_SUCCESSFUL,
    }

    def _classify_bybit(raw_result):
        if raw_result.outcome == BybitVerificationOutcome.SUCCESS:
            return VERIFY_SUCCESS, "confirmed"
        if raw_result.outcome in _BYBIT_TERMINAL_OUTCOMES:
            return VERIFY_TERMINAL, str(getattr(raw_result, "error_message", "") or raw_result.outcome)
        # API_ERROR / NOT_FOUND — transient, may resolve on the next check.
        return VERIFY_RETRYABLE, str(getattr(raw_result, "error_message", "") or raw_result.outcome)

    svc = BybitPayService()
    try:
        if is_uid:
            attempt_fn = lambda: svc.verify_uid_transfer(
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=verify_currency, order_created_at=order_created_at,
            )
        else:
            attempt_fn = lambda: svc.verify_onchain_deposit(
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=verify_currency, network=network, order_created_at=order_created_at,
                tolerance=verify_tolerance,
            )
        try:
            result, verify_kind, _verify_detail = await run_auto_verification_with_retries(
                gateway_id="bybit_pay",
                tx_id=tx_id,
                attempt_fn=attempt_fn,
                classify=_classify_bybit,
                telegram_user_id=telegram_id,
                submitted_txid=txid_raw,
            )
        except VerificationLockBusy:
            await pui.edit_or_reply(
                processing_msg,
                "⏳ Your previous submission for this order is still being verified — please wait.",
            )
            return BYBIT_TXID
    finally:
        lock.release()

    # VERIFY_EXHAUSTED (ran out of automatic attempts without a definitive
    # yes/no) reads better to the user as "still in progress" than as a
    # hard failure. Purely cosmetic: the deposit is still queued for admin
    # review exactly as before.
    _still_processing = verify_kind == VERIFY_EXHAUSTED

    # ---- Outcomes that are clear user errors — return inline, no admin notification. ----
    if result.outcome == BybitVerificationOutcome.CURRENCY_MISMATCH:
        await pui.edit_or_reply(processing_msg, "❌ Unsupported payment currency.")
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
                from services.payment_workflow import enqueue_pending_review
                with get_db_session() as _sess:
                    pmv = enqueue_pending_review(
                        _sess,
                        gateway_id="bybit_pay",
                        telegram_user_id=telegram_id,
                        internal_order_id=tx_id,
                        submitted_txid=txid_raw,
                        amount=expected_amount,
                        currency=verify_currency,
                        payment_type=payment_type,
                        network=network,
                        auto_outcome=outcome_str,
                        auto_detail=detail_str[:500] if detail_str else None,
                    )
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
                    def _claim_review(_tx_id):
                        with get_db_session() as _rsess:
                            claimed = _rsess.query(Transaction).filter(
                                Transaction.id == _tx_id,
                                Transaction.review_notified.is_(False),
                            ).update(
                                {Transaction.review_notified: True},
                                synchronize_session=False,
                            ) == 1
                            _rsess.commit()
                            return claimed
                    review_claimed = await run_db(_claim_review, tx_id)
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
                    _net_detail = f" • Network: {payment_type}/{network}" if payment_type else ""
                    admin_ids = _gateway_admin_recipient_ids()
                    for admin_id in admin_ids:
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=pui.admin_review_card(
                                    gateway_key="bybit_pay" if is_uid else None,
                                    gateway_label_override=None if is_uid else "Crypto",
                                    amount=f"{expected_amount} {verify_currency}",
                                    order_id=tx_id,
                                    txn_id=txid_raw,
                                    full_name=update.effective_user.full_name,
                                    username=update.effective_user.username,
                                    user_id=telegram_id,
                                    status_key="pending_review",
                                    network=(pui.crypto_network_label(network) if network else None) if not is_uid else None,
                                    verification_status="failed",
                                    verification_reason=reason,
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

        # Build the correct pending card based on payment type AND outcome:
        #
        #  UID Transfer (Bybit Pay):
        #    → always clean card, no IDs shown
        #
        #  On-chain crypto (TRC20/BEP20/ERC20/LTC/...):
        #    _still_processing (VERIFY_EXHAUSTED — ran out of retries without a
        #      definitive yes/no): transaction was found but not yet confirmed
        #      enough → show "waiting for blockchain confirmations"
        #    terminal failure (API error, NOT_FOUND after retries, etc.):
        #      auto-verification could not complete → show "placed in Pending
        #      Review queue" with Deposit ID + TxHash for user reference
        def _pending_card():
            if is_uid:
                return pui.bybit_verification_pending_card()
            if _still_processing:
                return pui.crypto_blockchain_confirmation_pending_card()
            return pui.crypto_verification_pending_card(
                deposit_id=_dep_id_fmt, txhash=txid_raw
            )

        if result.outcome == BybitVerificationOutcome.AMOUNT_MISMATCH:
            if pmv_id:
                sent = await pui.edit_or_reply(
                    processing_msg,
                    _pending_card(),
                    reply_markup=pui.pending_review_keyboard(),
                )
                pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
            else:
                await pui.edit_or_reply(
                    processing_msg,
                    "❌ Payment amount mismatch.\n\n"
                    f"Expected: {expected_amount} {verify_currency}\n"
                    f"Received: {result.received_amount} {result.currency or verify_currency}",
                )
        elif pmv_id:
            # All queued-for-review paths (exhausted retries or terminal failure)
            # use the same pending card — purely cosmetic distinction.
            sent = await pui.edit_or_reply(
                processing_msg,
                _pending_card(),
                reply_markup=pui.pending_review_keyboard(),
            )
            pui.remember_pending_message(pmv_id, sent.chat_id, sent.message_id)
        else:
            error_map = _BYBIT_FRIENDLY_ERROR if is_uid else _BYBIT_FRIENDLY_ERROR_ONCHAIN
            friendly = error_map.get(
                result.outcome, "❌ Transaction could not be verified.\n\nPlease check the Transaction ID and try again."
            )
            await pui.edit_or_reply(processing_msg, friendly)
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
            await pui.edit_or_reply(processing_msg, "❌ This order is no longer pending.")
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
                await pui.edit_or_reply(
                    processing_msg,
                    "⚠️ Verification succeeded but crediting your balance failed. Please contact support with your Deposit ID: %s" % pui.format_deposit_id(tx_id),
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
    # For UID Transfer show "Bybit Pay"; for on-chain show the coin/network
    # label (e.g. "USDT (BEP20)", "Litecoin (LTC)"). Purely cosmetic.
    if is_uid:
        _success_method = "Bybit Pay"
    else:
        _success_method = pui.crypto_network_label(network or "")
    await update.message.reply_text(
        pui.deposit_success_card(
            amount=f"${credited_usd:.2f} USD",
            payment_method=_success_method,
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


async def _active_resubmit_callback(tx_id: Optional[int], prefix: str) -> Optional[str]:
    """Return a resubmit callback only while its deposit is still active."""
    if not tx_id:
        return None
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if (
            not tx
            or tx.status != TransactionStatus.PENDING
            or (tx.expires_at and datetime.utcnow() > tx.expires_at)
        ):
            return None
    return f"{prefix}:{tx_id}"


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bKash payment method selection — ask for amount next."""
    gmin = cfg.get_float("bkash_min_amount", 0.0)
    gmax = cfg.get_float("bkash_max_amount", 0.0)
    return await _ask_amount_for_gateway(update, context, "bkash", "bKash", "📱", gmin, gmax)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Nagad payment method selection — ask for amount next."""
    gmin = cfg.get_float("nagad_min_amount", 0.0)
    gmax = cfg.get_float("nagad_max_amount", 0.0)
    return await _ask_amount_for_gateway(update, context, "nagad", "Nagad", "🟠", gmin, gmax)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_cryptomus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Cryptomus (USDT/crypto) payment method selection — ask for amount next.

    Cryptomus is used instead of @CryptoBot for regions (e.g. Bangladesh)
    where @CryptoBot isn't usable. Fully automated — no Manual mode, unlike
    bKash/Nagad.
    """
    return await _ask_amount_for_gateway(update, context, "cryptomus", "Cryptomus (USDT/Crypto)", "💠")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_nowpayments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle NOWPayments (crypto) payment method selection — ask for amount next.

    Fully automated — no Manual mode. See services/nowpayments_payment.py.
    """
    return await _ask_amount_for_gateway(update, context, "nowpayments", "NOWPayments (Crypto)", "🌐")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_zinipay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle BKash • Nagad • Rocket (ZiniPay-backed) payment method selection — ask for amount next.

    Fully automated — no Manual mode. See services/zinipay_payment.py.

    Generic entry point (callback_data == "pay_zinipay") — used when the
    combined "BKash • Nagad • Rocket" button is tapped directly, without a
    specific provider chosen first (e.g. from the top-level Add Funds
    screen, before the Mobile Money submenu). No provider preference is
    stored here, so ``_finish_zinipay_payment`` falls back to the
    admin-configured default provider, exactly as before.
    """
    context.user_data.pop('zinipay_provider', None)
    return await _ask_amount_for_gateway(update, context, "zinipay", "BKash • Nagad • Rocket", "🇧🇩")


# ── Mobile Money (BD) submenu — bKash / Nagad / Rocket / Upay, each via its own
# callback_data (see services/payment_selection_ui.py build_mobile_money_screen).
# These three thin wrappers are the fix for the "Nagad/Rocket loads bKash"
# bug: each records EXACTLY which provider the user tapped in
# context.user_data['zinipay_provider'] before falling into the same
# gateway_key="zinipay" flow every ZiniPay-backed payment already used.
# Nothing about payment creation, verification, or the database changes —
# only which provider's number/label/icon is shown is now correct.
_ZINIPAY_PROVIDER_DISPLAY = {
    "bkash": ("bKash", "🩷"),
    "nagad": ("Nagad", "🧡"),
    "rocket": ("Rocket", "💜"),
    "upay": ("Upay", "🔵"),
}


async def _payment_method_zinipay_provider(update: Update, context: ContextTypes.DEFAULT_TYPE, provider: str):
    context.user_data['zinipay_provider'] = provider
    label, emoji = _ZINIPAY_PROVIDER_DISPLAY[provider]
    return await _ask_amount_for_gateway(update, context, "zinipay", label, emoji)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_zinipay_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mobile Money submenu — "bKash" tapped (routed via ZiniPay)."""
    return await _payment_method_zinipay_provider(update, context, "bkash")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_zinipay_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mobile Money submenu — "Nagad" tapped (routed via ZiniPay)."""
    return await _payment_method_zinipay_provider(update, context, "nagad")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_zinipay_rocket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mobile Money submenu — "Rocket" tapped (routed via ZiniPay)."""
    return await _payment_method_zinipay_provider(update, context, "rocket")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_zinipay_upay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mobile Money submenu — "Upay" tapped (routed via ZiniPay)."""
    return await _payment_method_zinipay_provider(update, context, "upay")


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_binance_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Binance Pay payment method selection — ask for amount next.

    Fully automated verification (Binance transaction-history lookup), but
    NOT a hosted checkout link — the user pastes their own Binance Pay
    transaction ID back into the bot afterwards. See services/binance_pay.py.
    """
    # Acknowledge the tap immediately — BinancePayService() reads its
    # config from the database, which can be slow under load. Answering
    # first guarantees the button never sits highlighted while that
    # happens, and a DB hiccup here can no longer cause a Telegram
    # callback-query timeout.
    await safe_answer(update.callback_query)
    svc = BinancePayService()
    return await _ask_amount_for_gateway(update, context, "binance_pay", "Binance Pay", "🟡", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Bybit Pay (UID Transfer) payment method selection — ask for amount next.

    Fully automated verification via the official Bybit V5 API
    (GET /v5/asset/deposit/query-internal-record). The user pays from their
    own Bybit app and reports the internal Transaction ID. See services/bybit_pay.py.
    On-chain deposits (TRC20/BEP20/ERC20) are handled by the dedicated
    payment_method_bybit_trc20 / bep20 / erc20 handlers below.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_pay", "Bybit Pay", "💙", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_trc20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT TRC20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for TRC20 and shows the Bybit TRC20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → TRC20' sub-menu path. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_trc20", "USDT TRC20", "💵", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_bep20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT BEP20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for BEP20 and shows the Bybit BEP20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → BEP20' sub-menu path. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_bep20", "USDT BEP20", "🟢", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_erc20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT ERC20 (Bybit on-chain) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for ERC20 and shows the Bybit ERC20 deposit
    address directly. Verification uses the same Bybit V5 on-chain API as the
    former Bybit 'On-chain Deposit → ERC20' sub-menu path. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_erc20", "USDT ERC20", "🔵", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_ton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT TON payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for TON on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_ton", "USDT TON", "⚫", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_avaxc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Avalanche C-Chain payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for AVAXC on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_avaxc", "USDT Avalanche C-Chain", "🔺", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_ltc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Litecoin (LTC) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for LTC on-chain deposit and shows the
    configured LTC deposit address directly. Verification uses the same
    Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record) as
    TRC20/BEP20/ERC20. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_ltc", "Litecoin (LTC)", "🪙", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Base (Coinbase Base L2) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for BASE on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_base", "USDT Base", "🔷", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Arbitrum One payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for ARBONE on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_arb", "USDT Arbitrum", "🔵", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Optimism payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for OP on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_op", "USDT Optimism", "🔴", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_matic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Polygon (MATIC) payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for MATIC on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE/OP. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_matic", "USDT Polygon", "🟣", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_bybit_sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle USDT Solana payment method selection — ask for amount next.

    Creates a BYBIT_PAY order tagged for SOL on-chain deposit. Verification
    uses the Bybit V5 on-chain deposit API (GET /v5/asset/deposit/query-record),
    identical to TRC20/BEP20/ERC20/LTC/AVAXC/TON/BASE/ARBONE/OP/MATIC. See services/bybit_pay.py.
    """
    await safe_answer(update.callback_query)
    svc = BybitPayService()
    return await _ask_amount_for_gateway(update, context, "bybit_sol", "USDT Solana", "🟢", svc.min_amount, svc.max_amount)


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Card payment via Telegram Payments (native sendInvoice flow)."""
    query = update.callback_query
    await safe_answer(query)

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
                pui.invoice_card(
                    method_label="Card Payment", method_emoji="💳",
                    amount=_plain_usd(usd_amount),
                    deposit_id=transaction_id, created_at=transaction_created_at,
                    instruction="👉 Please complete the secure card payment below.",
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


@guarded_callback(fallback_state=ConversationHandler.END)
async def payment_method_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram Stars payment method selection — ask for amount next."""
    query = update.callback_query
    await safe_answer(query)

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

    pre_result = await _dispatch_with_preselected_amount(update, context)
    if pre_result is not None:
        return pre_result

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
            pui.invoice_card(
                method_label="Telegram Stars", method_emoji="⭐",
                amount=f"{stars_amount} ⭐ Stars ({_plain_usd(usd_amount)})",
                deposit_id=transaction_id, created_at=transaction_created_at,
                instruction="👉 Please complete the Stars payment below.",
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
        def _check_valid(_tx_id, _expected_method, _quoted_total):
            with get_db_session() as session:
                transaction = session.query(Transaction).filter_by(
                    id=_tx_id,
                    payment_method=_expected_method
                ).first()
                # Allow if not already credited (PENDING, or EXPIRED for a late-but-honoured pay).
                if transaction and transaction.status != TransactionStatus.COMPLETED:
                    if _expected_method == PaymentMethod.STARS:
                        # Cross-check the Star amount Telegram is about to charge
                        # against what we quoted at invoice-creation time, so a
                        # mid-flight admin rate change can't under/over-charge.
                        quoted_stars = transaction.stars_amount or 0
                        return bool(quoted_stars) and _quoted_total == quoted_stars
                    return True
                return False
        is_valid = await run_db(_check_valid, transaction_id, expected_method, query.total_amount)

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

    def _complete_transaction(_tx_id, _method, _paid_total_amount, _charge_id):
        with get_db_session() as session:
            transaction = session.query(Transaction).filter_by(
                id=_tx_id,
                payment_method=_method
            ).first()

            if not transaction:
                return {"outcome": "not_found"}

            # Belt-and-suspenders: status check after idempotency claim.
            if transaction.status == TransactionStatus.COMPLETED:
                return {"outcome": "already_completed"}

            if _method == PaymentMethod.STARS:
                quoted_stars = transaction.stars_amount or 0
                if quoted_stars and _paid_total_amount != quoted_stars:
                    # Telegram already took the user's Stars at this point, so we
                    # still credit the wallet — but log loudly since this means
                    # the quoted price and the charged price disagree (e.g. the
                    # admin changed the rate mid-flight). We credit the USD value
                    # that was quoted/frozen on the transaction, not a recomputed one.
                    logger.warning(
                        "Stars payment amount mismatch for transaction %s: "
                        "quoted=%s paid=%s — crediting the originally quoted USD value",
                        _tx_id, quoted_stars, _paid_total_amount,
                    )

            transaction.status = TransactionStatus.COMPLETED
            transaction.completed_at = datetime.utcnow()
            # Store Telegram's charge id in crypto_address for reference.
            transaction.crypto_address = f"tg_charge:{_charge_id}"
            credit_amount = float(transaction.amount)
            stars_paid = transaction.stars_amount
            session.flush()

            user = session.query(User).filter_by(id=transaction.user_id).first()
            if not user:
                session.commit()
                return {"outcome": "no_user"}
            user_db_id = user.id
            user_telegram_id = user.telegram_id

            result = {
                "outcome": "ok",
                "credit_amount": credit_amount,
                "stars_paid": stars_paid,
                "user_db_id": user_db_id,
                "user_telegram_id": user_telegram_id,
                "notif": None,
            }

            if _method == PaymentMethod.STARS:
                # Use the ledgered wallet service for Stars so the credit shows
                # up in Admin Wallets / WalletLedger history.
                session.commit()
            else:
                # Card path unchanged from before: direct balance update in the
                # same transaction as the status flip.
                user.wallet_balance += credit_amount
                session.commit()
                result["notif"] = {
                    'telegram_id': user_telegram_id,
                    'amount': credit_amount,
                    'new_balance': user.wallet_balance,
                    'transaction_id': _tx_id,
                    'method': 'card',
                }
            return result

    _tx_result = await run_db(
        _complete_transaction, transaction_id, method, payment.total_amount,
        payment.telegram_payment_charge_id,
    )
    if _tx_result["outcome"] in ("not_found", "already_completed", "no_user"):
        return
    credit_amount = _tx_result["credit_amount"]
    stars_paid = _tx_result["stars_paid"]
    user_db_id = _tx_result["user_db_id"]
    user_telegram_id = _tx_result["user_telegram_id"]
    notif = _tx_result["notif"]

    if method == PaymentMethod.STARS:
        try:
            from services import wallet as wallet_svc
            new_balance = await run_db(
                wallet_svc.credit,
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

            def _fallback_credit(_user_db_id, _credit_amount):
                with get_db_session() as session2:
                    user2 = session2.query(User).filter_by(id=_user_db_id).first()
                    if not user2:
                        return None
                    user2.wallet_balance = float(user2.wallet_balance or 0.0) + _credit_amount
                    session2.commit()
                    return user2.wallet_balance

            new_balance = await run_db(_fallback_credit, user_db_id, credit_amount)
            if new_balance is None:
                return
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


# Keys that belong ONLY to the specific payment attempt/session that was
# just cancelled (which gateway/provider was picked, and any in-flight
# transaction-id/proof capture for it). These are cleared any time the
# user leaves a specific payment attempt's mini-flow — via Back OR via a
# real cancel — so re-selecting a method afterwards starts that method's
# flow cleanly instead of resuming stale state.
#
# Deliberately NOT included: 'topup_amount' (the amount picked on the Add
# Funds screen is part of the user's *navigation context* within the Add
# Funds flow, not the cancelled session — keeping it means re-picking a
# payment method after Back/Cancel opens that method's payment page
# directly, instead of re-prompting for the amount) and anything unrelated
# to payments (nav stacks, language, cart, etc.), which neither Back nor
# Cancel must ever touch.
_PAYMENT_SESSION_KEYS = (
    'topup_method',
    'zinipay_provider',
    'zinipay_tx_id',
    'binance_tx_id',
    'bybit_tx_id',
    'manual_method_id',
    'manual_tx_id',
    'manual_req_proof',
    'manual_req_txid',
)

# Everything cleared by a REAL "❌ Cancel" tap (see ``deposit_cancel``
# below) — every key in ``_PAYMENT_SESSION_KEYS`` above, PLUS ``topup_amount``.
# Unlike Back, a genuine Cancel ends the whole deposit attempt, not just the
# current mini-step within it, so the previously-picked amount must NOT
# survive it — the next deposit starts clean from Step 1.
_DEPOSIT_CANCEL_KEYS = _PAYMENT_SESSION_KEYS + ('topup_amount',)


async def deposit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"❌ Cancel" tapped from ANY screen where the user is actively
    creating or completing a deposit — Amount Selection, Payment Method
    Selection, Crypto Networks, Mobile Banking, an active invoice/payment
    page, or a Submit Transaction/Order ID prompt (see
    ``services/payment_ui.py:with_deposit_cancel`` for every call site that
    renders this button).

    Unlike every "⬅️ Back" button in the payment flow — which is pure
    navigation and never touches a pending deposit — this is a real,
    destructive cancel:
      • any still-PENDING deposit this user has is marked CANCELLED
      • the entire in-progress deposit session (picked amount, method,
        provider, any in-flight txid/proof capture) is cleared from
        ``context.user_data`` — clearing the amount too (unlike a plain
        Back) since the whole attempt is being abandoned, not just one
        step of it
      • cancelling frees the user to start a brand-new deposit immediately
        — there is no lingering "pending deposit" lock left behind

    Always shows the one shared "✅ Deposit cancelled successfully." card
    with "💳 Create New Deposit" / "🔙 Back" — never silently drops back
    into the flow the way Back does. Registered both inside every
    deposit-related ConversationHandler (as a fallback, so it fires
    regardless of which state the user is in) and standalone in bot.py,
    the same dual-registration pattern already used by
    ``cancel_pending_deposit`` / ``cancel_payment_page``.
    """
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id

    def _cancel(_telegram_id):
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=_telegram_id).first()
            if user:
                _cancel_user_pending_transactions(session, user.id)

    await run_db(_cancel, telegram_id)

    for _key in _DEPOSIT_CANCEL_KEYS:
        context.user_data.pop(_key, None)

    text = pui.deposit_cancelled_card()
    keyboard = pui.deposit_cancelled_keyboard()

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    except Exception:
        # If the original message can't be edited for any reason (e.g. it
        # was already deleted), fall back to sending a fresh message so the
        # confirmation is never silently lost.
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, reply_markup=keyboard,
                parse_mode="HTML")
        except Exception:
            logger.exception("Failed to show deposit-cancelled confirmation")

    return ConversationHandler.END


async def _redraw_as_payment_method_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared screen-drawing tail for both Back and (dedicated) Cancel:
    show the Payment Method screen, deleting the current message if
    Telegram allows it and sending fresh, or editing in place if not.
    Never both a delete *and* an edit — only ever one resulting message.
    Returns ``is_empty`` (True if no payment method is configured at all)
    so callers driving a ConversationHandler can end it appropriately.
    """
    query = update.callback_query
    text, keyboard, is_empty = await run_db(_build_topup_method_screen, amount=context.user_data.get('topup_amount'))

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

    return is_empty


async def _go_back_to_methods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared "⬅️ Back" behavior for every payment page (Payment Details,
    Submit Transaction ID, Submit Order ID, currency/network pickers, ...).

    Pure navigation, nothing else: a still-PENDING Transaction row is
    NEVER cancelled, modified, or deleted here — the deposit stays exactly
    as it was, so the user can resume it later (e.g. via "▶️ Continue
    Deposit" on the Pending Deposit notice, or by tapping the same
    payment method again). Only this specific payment attempt's own
    mini-conversation session state is cleared (see ``_PAYMENT_SESSION_KEYS``)
    so re-selecting a method afterwards starts that method's flow cleanly;
    the user's amount, navigation context, language, etc. are left
    untouched.

    Returns ``is_empty`` (True if no payment method is configured at all)
    so callers driving a ConversationHandler can end it appropriately.
    """
    query = update.callback_query
    await query.answer()

    for _key in _PAYMENT_SESSION_KEYS:
        context.user_data.pop(_key, None)

    return await _redraw_as_payment_method_screen(update, context)


async def _cancel_pending_deposit_and_go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared behavior for the ONE dedicated, explicit "❌ Cancel"
    action (the Pending Deposit notice's Continue / Cancel / Back menu —
    see ``services/payment_ui.py:pending_deposit_keyboard``).

    Unlike ``_go_back_to_methods``, this really does cancel whatever
    PENDING transaction(s) this user currently has, then behaves like a
    Back tap: the current message is deleted if Telegram allows it and the
    Payment Method screen is sent fresh; if deletion isn't possible, the
    existing message is edited into the Payment Method screen instead.

    Only the just-cancelled payment attempt's own session state is
    cleared (see ``_PAYMENT_SESSION_KEYS``) — the user's navigation
    context (which screen/flow they're in, previously-picked amount,
    language, etc.) is left untouched, so the user stays inside the Add
    Funds flow and picking another payment method immediately opens that
    method's payment page again, instead of bouncing to the Main Menu.

    Returns ``is_empty`` (True if no payment method is configured at all)
    so callers driving a ConversationHandler can end it appropriately.
    """
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id

    def _cancel_pending(_telegram_id):
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=_telegram_id).first()
            if user:
                _cancel_user_pending_transactions(session, user.id)

    await run_db(_cancel_pending, telegram_id)

    for _key in _PAYMENT_SESSION_KEYS:
        context.user_data.pop(_key, None)

    return await _redraw_as_payment_method_screen(update, context)


async def cancel_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped from inside the top-up conversation (callback_data
    "back_payment_methods" — its own dedicated name, never "cancel"/"cancel_*").

    Pure navigation straight to the Payment Method screen — never cancels,
    modifies, or deletes any pending Transaction row. See
    ``_go_back_to_methods`` for the full contract.

    Only ends the conversation if there's truly nothing left to show
    (``is_empty``) — otherwise transitions back to the Payment Method
    state so the Add Funds flow keeps going and every button on the
    screen just shown (Back, payment methods, ...) keeps working.
    """
    is_empty = await _go_back_to_methods(update, context)
    return ConversationHandler.END if is_empty else METHOD


async def cancel_payment_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⬅️ Back" tapped from a payment/invoice page (outside conversation),
    callback_data "back_payment_methods" — its own dedicated name, never
    "cancel"/"cancel_*".

    This button is shared by every gateway's payment-instructions page, so
    there's no single tx_id in the callback data — but unlike a real
    cancel, Back never needs one: it never touches any transaction, it
    just redraws the Payment Method screen. Any pending deposit(s) this
    user has stay exactly as they were and can still be resumed.

    This normally fires *after* the conversation that created the payment
    page has already ended (see bot.py) — so, unlike ``cancel_topup``, its
    return value isn't consumed by a ConversationHandler here. The Payment
    Method screen's buttons (Mobile Banking, Crypto Networks, Binance Pay,
    Bybit Pay, every individual gateway, ...) are additionally registered
    as standalone handlers in bot.py, right after the Add Funds
    ConversationHandler, so they keep working correctly whether or not a
    conversation is currently active for this user — this is what fixes
    "select a payment method after Back" landing on the Main Menu.
    """
    await _go_back_to_methods(update, context)


async def cancel_pending_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"❌ Cancel" tapped on the Pending Deposit notice (see
    ``services/payment_ui.py:pending_deposit_keyboard``).

    Delegates to the shared ``deposit_cancel`` — the same real, destructive
    cancel used by every other deposit screen (Amount Selection, Payment
    Method Selection, Crypto Networks, Mobile Banking, the active
    invoice/payment page, and every Submit Transaction/Order ID prompt) —
    so tapping Cancel here shows the exact same "✅ Deposit cancelled
    successfully." confirmation instead of a screen-specific behavior.
    Registered both inside the top-up ConversationHandler and standalone
    in bot.py — same dual registration as ``topup_back_to_methods`` —
    since the Pending Deposit notice can be shown with or without an
    active conversation.
    """
    return await deposit_cancel(update, context)


async def check_pending_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to check pending payment transactions (non-blocking)."""
    import asyncio
    from services.payment_workflow import (
        acquire_verification_lock, release_verification_lock, log_verification_attempt,
    )

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

                # These four methods are plain boolean check_payment_status()
                # gateways (no user-submitted TXID, no rich outcome
                # classifier) — CRYPTO_WALLET's registry id differs from its
                # enum value (see the same mapping in check_expired_payments).
                _POLL_GATEWAY_ID = {
                    PaymentMethod.CRYPTO_WALLET: "cryptobot",
                    PaymentMethod.BKASH: "bkash",
                    PaymentMethod.NAGAD: "nagad",
                    PaymentMethod.CRYPTOMUS: "cryptomus",
                    PaymentMethod.NOWPAYMENTS: "nowpayments",
                }.get(transaction.payment_method)

                # Claim the same per-order verification lock the retry engine
                # uses, so a concurrent admin "Verify Again" tap (or an
                # overlapping run of this same job) can't check/credit this
                # transaction at the same time. Not a failure if busy — just
                # skip this transaction for this cycle and retry next time.
                got_lock = True
                if _POLL_GATEWAY_ID:
                    got_lock = acquire_verification_lock(session, transaction.id)
                    session.commit()
                if not got_lock:
                    continue

                try:
                    # Verify payment based on payment method. Each check is
                    # isolated in its own try/except: an unexpected exception
                    # from one gateway/transaction (network blip, malformed
                    # stored reference, etc.) must never abort this whole
                    # polling cycle and silently skip every OTHER pending
                    # deposit — it just leaves this one is_paid=False and lets
                    # the next poll cycle retry it.
                    is_paid = False
                    attempt_detail = ""
                    try:
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
                    except Exception as _poll_exc:
                        attempt_detail = f"{type(_poll_exc).__name__}: {_poll_exc}"
                        logger.warning(
                            "[POLL] gateway status check raised for tx %s (%s) — "
                            "leaving PENDING for the next poll cycle",
                            transaction.id,
                            transaction.payment_method.value if transaction.payment_method else "?",
                            exc_info=True,
                        )
                        is_paid = False

                    if _POLL_GATEWAY_ID:
                        log_verification_attempt(
                            gateway_id=_POLL_GATEWAY_ID,
                            tx_id=transaction.id,
                            submitted_txid=transaction.crypto_address or "",
                            outcome="PAID" if is_paid else ("ERROR" if attempt_detail else "NOT_PAID_YET"),
                            detail=attempt_detail or ("Confirmed by gateway" if is_paid else "Not confirmed yet — will retry next poll cycle"),
                        )
                finally:
                    # Release the verification lock now — the check itself is
                    # done. The credit step below has its own independent
                    # atomic guards (idempotency claim + conditional status
                    # UPDATE), so it doesn't need to hold this lock too.
                    if _POLL_GATEWAY_ID:
                        release_verification_lock(transaction.id)

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
    from services.payment_workflow import (
        acquire_verification_lock, release_verification_lock, log_verification_attempt,
    )

    # Gateway methods that can self-report confirmed payments via API.
    AUTOMATED_GATEWAY_METHODS = {
        PaymentMethod.NOWPAYMENTS,
        PaymentMethod.CRYPTOMUS,
        PaymentMethod.CRYPTO_WALLET,  # CryptoBot
    }
    _EXPIRY_GATEWAY_ID = {
        PaymentMethod.NOWPAYMENTS: "nowpayments",
        PaymentMethod.CRYPTOMUS: "cryptomus",
        PaymentMethod.CRYPTO_WALLET: "cryptobot",
    }

    def _check_expired_sync():
        """Synchronous database operations run in thread pool."""
        expired_notifications = []
        late_credit_notifications = []
        gateway_escalation_notifications = []

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
                    _exp_gw_id = _EXPIRY_GATEWAY_ID.get(transaction.payment_method)
                    if _exp_gw_id and not acquire_verification_lock(session, transaction.id):
                        session.commit()
                        continue  # Another verification job is already checking this order
                    session.commit()

                    is_paid = False
                    gateway_raised = False
                    gw_error_detail = None
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
                    except Exception as _gw_exc:
                        gateway_raised = True
                        gw_error_detail = f"{type(_gw_exc).__name__}: {_gw_exc}"
                        logger.exception(
                            "[EXPIRY CHECK] gateway status query failed for tx %s — "
                            "escalating to manual review instead of cancelling "
                            "(we genuinely don't know if this was paid)",
                            transaction.id,
                        )

                    if _exp_gw_id:
                        release_verification_lock(transaction.id)
                        log_verification_attempt(
                            gateway_id=_exp_gw_id,
                            tx_id=transaction.id,
                            submitted_txid=transaction.crypto_address or "",
                            outcome="PAID" if is_paid else ("ERROR" if gateway_raised else "NOT_PAID_AT_EXPIRY"),
                            detail=gw_error_detail or ("Confirmed by gateway at expiry" if is_paid else "Not confirmed by gateway — expiring normally"),
                        )

                    if gateway_raised:
                        # We could not confirm payment status AND could not
                        # confirm non-payment — per the "API unavailable /
                        # Gateway timeout -> manual review" rule, this must
                        # reach an admin rather than being silently
                        # cancelled (which could drop a real payment).
                        # Gateway-agnostic: works for any gateway in
                        # AUTOMATED_GATEWAY_METHODS with no per-gateway code.
                        try:
                            from database.models import PendingManualVerification as _PMV
                            gw_key = _exp_gw_id or transaction.payment_method.value
                            ref = transaction.crypto_address or f"tx:{transaction.id}"
                            already_queued = session.query(_PMV).filter_by(
                                gateway=gw_key, internal_order_id=transaction.id, submitted_txid=ref,
                            ).first() is not None

                            from services.payment_workflow import enqueue_pending_review
                            pmv = enqueue_pending_review(
                                session,
                                gateway_id=gw_key,
                                telegram_user_id=(
                                    session.query(User).filter_by(id=transaction.user_id).first().telegram_id
                                    if transaction.user_id else 0
                                ),
                                internal_order_id=transaction.id,
                                submitted_txid=ref,
                                amount=float(transaction.amount or 0),
                                auto_outcome="exception",
                                auto_detail=gw_error_detail or "gateway status check failed at expiry",
                            )
                            session.flush()
                            if not already_queued:
                                gateway_escalation_notifications.append({
                                    'gateway': gw_key,
                                    'tx_id': transaction.id,
                                    'pmv_id': pmv.id,
                                    'telegram_id': (
                                        session.query(User).filter_by(id=transaction.user_id).first().telegram_id
                                        if transaction.user_id else None
                                    ),
                                    'amount': float(transaction.amount or 0),
                                })
                        except Exception:
                            logger.exception(
                                "[EXPIRY CHECK] failed to enqueue manual review for tx %s — "
                                "will retry escalating on the next expiry run",
                                transaction.id,
                            )
                        continue  # Leave PENDING — do not cancel an unconfirmable order.

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

                        # This transaction may have an earlier PMV row from a
                        # prior expiry cycle's gateway exception (see the
                        # escalation branch above). Now that the gateway has
                        # confirmed payment, that review request is stale —
                        # auto-resolve it so it doesn't linger in the Pending
                        # Deposits queue pointing at an already-completed order.
                        try:
                            from database.models import PendingManualVerification as _PMV2
                            session.query(_PMV2).filter(
                                _PMV2.internal_order_id == transaction.id,
                                _PMV2.status == "pending",
                            ).update(
                                {
                                    _PMV2.status: "approved",
                                    _PMV2.admin_note: "Auto-resolved: gateway confirmed payment on a later retry (late credit)",
                                    _PMV2.resolved_at: datetime.utcnow(),
                                },
                                synchronize_session=False,
                            )
                            session.commit()
                        except Exception:
                            logger.warning(
                                "Failed to auto-resolve stale PMV rows for late-credited tx %s",
                                transaction.id, exc_info=True,
                            )

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

        return expired_notifications, late_credit_notifications, gateway_escalation_notifications

    # Run blocking database operations in thread pool
    expired_notifications, late_credit_notifications, gateway_escalation_notifications = await asyncio.to_thread(
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

    # ── Notify admins: a deposit couldn't be auto-verified at expiry and was
    #    escalated to the Pending Deposits queue instead of being cancelled ──
    for notif in gateway_escalation_notifications:
        try:
            from services.notifications import notify_admins as _notify_admins
            await _notify_admins(
                context.bot,
                "payment_manual_review",
                pui.admin_review_card(
                    gateway_key=notif.get('gateway'),
                    amount=format_price(notif['amount']),
                    order_id=notif['tx_id'],
                    user_id=notif.get('telegram_id'),
                    verification_status="failed",
                    verification_reason="Gateway API unavailable — could not confirm payment before expiry",
                    status_key="pending_review",
                ) + "\n\n➡️ Open <b>⏳ Pending Deposits</b> in the admin Payments menu to review.",
            )
        except Exception:
            logger.warning("Failed to notify admins of gateway escalation for tx %s", notif.get('tx_id'))


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

    def _load_product(_product_id):
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=_product_id).first()
            if not product:
                return {"outcome": "not_found"}
            if not product.is_active:
                return {"outcome": "inactive"}

            # Use inventory service for real available count (excludes active reservations)
            from services import inventory as _inv_svc
            from services.quantity_presets import build_keyboard as _build_qty_kb
            available = _inv_svc.count_available(_product_id)
            if available == 0:
                return {"outcome": "out_of_stock"}

            qty_markup = _build_qty_kb(product, available=available, product_id=_product_id)

            return {
                "outcome": "ok",
                "name": product.name,
                "price": product.price,
                "available": available,
                "product_type": product.product_type,
                "qty_markup": qty_markup,
            }

    _p = await run_db(_load_product, product_id)

    if _p["outcome"] == "not_found":
        try:
            await query.edit_message_text("❌ Product not found.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END
    if _p["outcome"] == "inactive":
        try:
            await query.edit_message_text("❌ This product is no longer available.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END
    if _p["outcome"] == "out_of_stock":
        try:
            await query.edit_message_text("❌ This product is out of stock.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    product_name = _p["name"]
    product_price = _p["price"]
    available = _p["available"]
    product_type = _p["product_type"]
    qty_markup = _p["qty_markup"]

    # Store product info in context for later
    context.user_data['purchase_product_id'] = product_id
    context.user_data['purchase_product_name'] = product_name
    context.user_data['purchase_product_price'] = product_price
    context.user_data['purchase_product_stock'] = available
    context.user_data['purchase_product_type'] = product_type

    # V18 — track recently viewed
    try:
        from handlers.feature_handlers import track_recently_viewed
        track_recently_viewed(update.effective_user.id, product_id)
    except Exception:
        pass

    # V48: Product Information Builder — show info page before purchase flow
    # This is a UI-only step. No payment/delivery/order logic is touched.
    try:
        from services.product_info_service import (
            get_purchase_settings as _pib_settings,
            has_info_blocks as _pib_has,
            render_product_info_page as _pib_render,
        )
        _ps = _pib_settings(product_id)
        _show_info = (
            not _already_shown
            and _ps.get('show_info_before_purchase', True)
            and not (_ps.get('skip_if_no_blocks', True) and not _pib_has(product_id))
        )
        if _show_info:
            _info_html, _blk_count = _pib_render(product_id)
            if _info_html and _blk_count > 0:
                _info_text = (
                    f'📋 <b>Product Information: {product_name}</b>\n\n'
                    + _info_html
                )
                if len(_info_text) > 4000:
                    _info_text = _info_text[:3980] + '\n\n<i>…(see full details in store)</i>'
                _continue_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton('🛒 Continue to Purchase', callback_data=f'buy_{product_id}__skip_info')],
                    [InlineKeyboardButton('🔙 Back to Product', callback_data=f'product_{product_id}')],
                ])
                # Mark in user_data so the next buy_ call skips info page
                context.user_data['pib_info_shown_for'] = product_id
                if query.message.photo:
                    await query.message.delete()
                    await query.message.reply_text(_info_text, reply_markup=_continue_kb, parse_mode='HTML')
                else:
                    try:
                        await query.edit_message_text(_info_text, reply_markup=_continue_kb, parse_mode='HTML')
                    except BadRequest as _pib_e:
                        if 'Message is not modified' not in str(_pib_e):
                            raise
                return PURCHASE_QUANTITY
    except Exception as _pib_exc:
        logger.debug('PIB info page check failed (non-critical): %s', _pib_exc)

    # For file products, quantity is always 1
    if product_type == ProductType.FILE:
        context.user_data['purchase_quantity'] = 1
        # Skip quantity input, go straight to confirmation
        return await show_purchase_confirmation(update, context)

    # For key products, show the standardized quantity preset keyboard
    from services.quantity_presets import build_message as _build_qty_msg
    message = _build_qty_msg(product_name, product_price, product_type)

    # qty_markup was already built inside the DB thread above (needs the
    # ORM product row) via services.quantity_presets.build_keyboard — the
    # one standardized quantity keyboard used by every product in the
    # store: fixed preset row(s), ✏️ Custom Quantity, ⬅️ Back. No Cancel
    # button on this screen — no order or payment has been created yet.

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


async def qty_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the ✏️ Custom Quantity button tap.

    Callback data format: ``qty_custom_<product_id>``

    Prompts the user to type a quantity. The typed value is picked up by
    the existing ``purchase_quantity_input`` handler (same PURCHASE_QUANTITY
    conversation state used by the preset keyboard) — no purchase/validation
    logic is duplicated here.
    """
    query = update.callback_query
    await query.answer()

    try:
        product_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        product_id = context.user_data.get('purchase_product_id', 0)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data=f"buy_{product_id}"),
    ]])

    try:
        await query.edit_message_text(
            "✏️ Enter the quantity you'd like to purchase.",
            reply_markup=keyboard,
        )
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
            "❌ Please enter a valid whole number.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    # Use stored available count (set by buy_product_start via inventory service)
    product_stock = context.user_data.get('purchase_product_stock', 0)

    if quantity < 1:
        await update.message.reply_text(
            "❌ Please enter a valid whole number.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    if quantity > product_stock:
        await update.message.reply_text(
            f"❌ Only {product_stock} items are currently available.",
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

    def _load_wallet_balance(_telegram_id):
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=_telegram_id).first()
            if not user:
                return None
            return user.wallet_balance

    wallet_balance = await run_db(_load_wallet_balance, telegram_id)
    if wallet_balance is None:
        if is_message:
            await update.message.reply_text("❌ User not found.")
        else:
            try:
                await update.callback_query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        return ConversationHandler.END

    has_sufficient_balance = wallet_balance >= total

    remaining_after = wallet_balance - total

    if has_sufficient_balance:
        balance_section = (
            f"💳 Wallet Balance: {format_price(wallet_balance)}\n"
            f"💳 Balance After Purchase: {format_price(remaining_after)}"
        )
    else:
        shortfall = total - wallet_balance
        balance_section = (
            f"💳 Wallet Balance: {format_price(wallet_balance)}\n"
            f"⚠️ Short: {format_price(shortfall)}"
        )

    discount_line = ""
    if coupon_discount > 0 and coupon_code:
        discount_line = f"🎟 {coupon_code}: -{format_price(coupon_discount)}\n"

    message = (
        f"🛍 Purchase Summary\n"
        f"\n"
        f"📦 {product_name}\n"
        f"\n"
        f"💰 Unit Price: {format_price(product_price)}\n"
        f"🔢 Quantity: {quantity}\n"
        f"{discount_line}"
        f"💵 Total: {format_price(total)}\n"
        f"\n"
        f"{balance_section}"
    )

    if has_sufficient_balance:
        keyboard = [
            [InlineKeyboardButton("✅ Confirm Purchase",
                                  callback_data=f"confirm_purchase_{product_id}_{quantity}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"buy_{product_id}")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("💰 Add Funds", callback_data="topup")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"buy_{product_id}")],
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
        _notif_unit_price = format_price(product.price)
        coupon_id = context.user_data.get('purchase_coupon_id')
        user_db_id = user.id  # snapshot for post-commit redemption logging

        # Fix: Coupon revalidation from DB — never trust user_data cache
        coupon_discount = 0.0
        _notif_coupon_code = None
        _notif_coupon_label = None
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
                    _notif_coupon_label = f"-{_c.discount_value:.0f}%"
                else:
                    coupon_discount = float(_c.discount_value)
                    _notif_coupon_label = f"-{format_price(coupon_discount)}"
                coupon_discount = round(min(coupon_discount, float(subtotal)), 2)
                _notif_coupon_code = _c.code
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
                        if _dispatcher_result.success and (
                            is_delivery_oversized(_dispatcher_result.user_message)
                            or getattr(_dispatcher_result, "force_file_delivery", False)
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
                        InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
                    ]])
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            try:
                from utils.notify_format import (
                    render_order_notification as _render_order_failed,
                    dhaka_time_str as _dhaka_ts_failed,
                )
                from utils.helpers import format_order_id as _fmt_order_id_failed
                _fail_cname = getattr(update.effective_user, 'full_name', '') or str(telegram_id)
                _fail_cuname = getattr(update.effective_user, 'username', '') or None
                _failed_notif = _render_order_failed(
                    status="failed",
                    order_id=_fmt_order_id_failed(order.id, getattr(order, 'created_at', None)),
                    customer_name=_fail_cname,
                    customer_username=_fail_cuname,
                    telegram_id=telegram_id,
                    product_name=product_name,
                    quantity=quantity,
                    total_paid=format_price(total),
                    payment_method="Wallet",
                    delivery_status="failed",
                    reason=f"{str(delivery_err)[:200]} — wallet auto-refunded",
                    order_time=_dhaka_ts_failed(getattr(order, 'created_at', None)),
                )
                await notify_admin(context, _failed_notif, parse_mode='HTML')
            except Exception:
                await notify_admin(
                    context,
                    f"❗️ Delivery failed for order #{order.id} (qty {quantity}). "
                    f"Wallet auto-refunded. Reason: {delivery_err}"
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
                f"📦 Order #{order.id}\n"
                f"📄 Receipt: {_receipt_number}\n"
                f"💰 Amount Paid\n{format_price(total)}\n\n"
                f"{order_details}\n"
                f"Thank you for your purchase!"
            )
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"),
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
            filename = f"{safe_name}_{_receipt_number}.txt"
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
                                f"📦 Order #{order.id}\n"
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
                # For count-triggered account deliveries, build a structured
                # UTF-8 file with a proper header and numbered account blocks
                # per spec (ORD-YYYYMMDD-NNNNNN.txt).  Character-oversized
                # deliveries for other types (REDEEM_LINK, VOUCHER, …) keep
                # using the raw user_message as before.
                _file_content = _v11_oversized_content
                _filename_override = None
                if (getattr(_dispatcher_result, "force_file_delivery", False)
                        and getattr(_dispatcher_result, "assets", None)):
                    from services.inventory_import import build_account_delivery_file
                    _file_content = build_account_delivery_file(
                        receipt_number=_receipt_number,
                        product_name=product_name,
                        quantity=quantity,
                        assets=_dispatcher_result.assets,
                    )
                    # Use admin-configured filename format (accdel_txt_filename_format)
                    try:
                        from utils.bot_config import cfg as _accdel_cfg
                        _fn_fmt = _accdel_cfg.get_str(
                            "accdel_txt_filename_format", "{order_id}.txt"
                        ) or "{order_id}.txt"
                        _safe_prod = "".join(
                            c for c in (product_name or "product")
                            if c.isalnum() or c in ("-", "_")
                        )[:40] or "product"
                        _filename_override = _fn_fmt.format(
                            order_id=_receipt_number,
                            product=_safe_prod,
                        )
                        if not _filename_override.endswith(".txt"):
                            _filename_override += ".txt"
                    except Exception:
                        _filename_override = f"{_receipt_number}.txt"

                await send_delivery_as_file(
                    context.bot, telegram_id, order.id, product_name,
                    _file_content,
                    caption=f"📎 {quantity} account(s) for {_receipt_number}",
                    admin_chat_id=app_settings.ADMIN_TELEGRAM_ID,
                    receipt_number=_receipt_number,
                    filename_override=_filename_override,
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
            from utils.notify_format import (
                render_order_notification as _render_order,
                dhaka_time_str as _dhaka_ts,
            )
            _af_cname = getattr(update.effective_user, 'full_name', '') or str(telegram_id)
            _af_cuname = getattr(update.effective_user, 'username', '')
            _delivery_status_key = (
                "file"
                if (bulk_keys or _v11_oversized_content) else
                "instant"
            )
            from utils.helpers import format_order_id as _fmt_order_id
            _order_display_id = _fmt_order_id(order.id, getattr(order, 'created_at', None))
            _merged_notif = _render_order(
                status="completed",
                order_id=_order_display_id,
                customer_name=_af_cname,
                customer_username=(_af_cuname or None),
                telegram_id=telegram_id,
                product_name=product_name,
                quantity=quantity,
                unit_price=_notif_unit_price,
                total_paid=format_price(total),
                payment_method="Wallet",
                delivery_status=_delivery_status_key,
                coupon_code=_notif_coupon_code,
                coupon_discount_label=_notif_coupon_label,
                order_time=_dhaka_ts(getattr(order, 'created_at', None)),
            )
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
    """Cancel the purchase process — silently return to the product listing."""
    query = update.callback_query
    await query.answer()

    # Clear purchase data
    for _k in ('purchase_product_id', 'purchase_product_name', 'purchase_product_price',
               'purchase_product_stock', 'purchase_product_type', 'purchase_quantity',
               'purchase_coupon_id', 'purchase_coupon_code', 'purchase_coupon_discount'):
        context.user_data.pop(_k, None)

    # Silently navigate back to the product listing by re-using its render
    # — no "Purchase cancelled" message, no new message sent.
    from handlers.user_handlers import back_to_products_callback
    await back_to_products_callback(update, context)

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
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
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
    # Generic fallback ONLY — used when the specific bKash/Nagad/Rocket
    # provider can't be resolved from the deposit (e.g. legacy row). Whenever
    # a Transaction is available, _pmv_gateway_label resolves and returns
    # the actual stored provider instead of this label.
    "zinipay": "Mobile Banking",
}


def _pmv_gateway_label(gateway: str, tx=None) -> str:
    """Display label for a PendingManualVerification's gateway.

    For ``zinipay``, always resolves and returns the SPECIFIC bKash / Nagad
    / Rocket provider the deposit was actually created with (stored on
    ``tx.crypto_address`` as ``"bdt:<amount>:<provider>"``) when a
    Transaction is supplied — never the generic combined label. Falls back
    to the cosmetic override table only when no provider can be resolved,
    then to the Payment Gateway Registry's ``display_name`` for any gateway
    registered after this file was last touched, and finally to the raw key
    so nothing ever breaks."""
    if gateway == "zinipay":
        provider = pui.resolve_zinipay_provider(getattr(tx, "crypto_address", None))
        if provider:
            label, _ = pui.zinipay_provider_meta(provider=provider)
            return label
    if gateway in _PMV_GATEWAY_LABELS:
        return _PMV_GATEWAY_LABELS[gateway]
    from services.payment_gateway_registry import registry
    g = registry.get(gateway)
    return g.display_name if g else gateway


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

    if not has_permission(update.effective_user.id, "manage_payments"):
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

            _reject_tx = session.query(Transaction).filter_by(id=tx_id).first()
            gateway_label = _pmv_gateway_label(gateway, tx=_reject_tx)
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
        gateway_label = _pmv_gateway_label(gateway, tx=tx)
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
            elif gateway == "zinipay":  # bKash / Nagad / Rocket
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
            # else: gateway has no dedicated per-gateway audit table (e.g. a
            # PMV row escalated generically by check_expired_payments for
            # Cryptomus / NOWPayments / CryptoBot — see
            # handlers/payment_handlers.py check_expired_payments). Nothing
            # gateway-specific to insert here; the AdminAuditLog row written
            # below already records the approval generically for every
            # gateway, so no gateway is ever silently mislabeled as another.
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
    extra_rows = [("💵", "Credited", f"${credited_usd:.2f}")]
    if bonus_line:
        extra_rows.append(bonus_line)
    await pui.clear_pending_user_message(context.bot, pmv_id)
    try:
        if gateway == "binance_pay":
            _approved_text = pui.binance_deposit_success_card(
                amount=f"${credited_usd:.2f} USD",
                deposit_id=pui.format_deposit_id(tx_id, tx.created_at),
                bonus_line=f"+{bonus_amount:.2f}" if bonus_amount else None,
            )
        else:
            _approved_text = pui.user_payment_card(
                gateway_key=gateway,
                gateway_label_override=gateway_label,
                stage="approved",
                amount=f"{expected_amount:.2f} {currency}",
                order_id=tx_id,
                extra=extra_rows,
                note="🎉 Your wallet has been credited successfully.",
            )
        await context.bot.send_message(
            chat_id=pmv.telegram_user_id,
            text=sanitize_message(_approved_text),
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

    if not has_permission(update.effective_user.id, "manage_payments"):
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

    # Re-run the full automatic verification (with retries) rather than a
    # single check — same engine and same DB lock as the original attempt,
    # so this can never overlap with a concurrent user resubmission or a
    # background retry for the same order.
    from services.payment_workflow import (
        run_auto_verification_with_retries, VerificationLockBusy,
        VERIFY_SUCCESS, VERIFY_TERMINAL, VERIFY_RETRYABLE,
    )

    new_reason = "Verification failed"  # sentinel — overwritten in the branch below
    if gateway == "binance_pay":
        svc = BinancePayService()
        _TERMINAL = {
            VerificationOutcome.NOT_CONFIGURED, VerificationOutcome.AMOUNT_MISMATCH,
            VerificationOutcome.WRONG_DIRECTION, VerificationOutcome.CURRENCY_MISMATCH,
            VerificationOutcome.TOO_OLD,
        }

        def _classify(raw_result):
            if raw_result.outcome == VerificationOutcome.SUCCESS:
                return VERIFY_SUCCESS, "confirmed"
            if raw_result.outcome in _TERMINAL:
                return VERIFY_TERMINAL, str(raw_result.outcome)
            return VERIFY_RETRYABLE, str(raw_result.outcome)

        try:
            result, _kind, _detail = await run_auto_verification_with_retries(
                gateway_id="binance_pay", tx_id=tx_id,
                attempt_fn=lambda: svc.verify_transaction(
                    transaction_id=txid_raw, expected_amount=expected_amount,
                    currency=currency, order_created_at=order_created_at,
                ),
                classify=_classify, telegram_user_id=telegram_user_id, submitted_txid=txid_raw,
            )
        except VerificationLockBusy:
            await query.answer("⏳ A verification job is already running for this order — please wait.", show_alert=True)
            return
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
        _TERMINAL = {
            BybitVerificationOutcome.NOT_CONFIGURED, BybitVerificationOutcome.AMOUNT_MISMATCH,
            BybitVerificationOutcome.CURRENCY_MISMATCH, getattr(BybitVerificationOutcome, "NETWORK_MISMATCH", None),
            getattr(BybitVerificationOutcome, "WRONG_ADDRESS", None), getattr(BybitVerificationOutcome, "TOO_OLD", None),
            getattr(BybitVerificationOutcome, "NOT_SUCCESSFUL", None),
        } - {None}

        def _classify(raw_result):
            if raw_result.outcome == BybitVerificationOutcome.SUCCESS:
                return VERIFY_SUCCESS, "confirmed"
            if raw_result.outcome in _TERMINAL:
                return VERIFY_TERMINAL, str(raw_result.outcome)
            return VERIFY_RETRYABLE, str(raw_result.outcome)

        if is_uid:
            attempt_fn = lambda: svc.verify_uid_transfer(
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=currency, order_created_at=order_created_at,
            )
        else:
            attempt_fn = lambda: svc.verify_onchain_deposit(
                transaction_id=txid_raw, expected_amount=expected_amount,
                currency=currency, network=network, order_created_at=order_created_at,
            )
        try:
            result, _kind, _detail = await run_auto_verification_with_retries(
                gateway_id="bybit_pay", tx_id=tx_id, attempt_fn=attempt_fn,
                classify=_classify, telegram_user_id=telegram_user_id, submitted_txid=txid_raw,
            )
        except VerificationLockBusy:
            await query.answer("⏳ A verification job is already running for this order — please wait.", show_alert=True)
            return
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
    try:
        if gateway == "binance_pay":
            _success_text = pui.binance_deposit_success_card(
                amount=f"${credited_usd:.2f} USD",
                deposit_id=pui.format_deposit_id(tx_id, order_created_at),
                bonus_line=_bonus_str,
            )
        else:
            _gw_label = pui.gateway_meta("bybit_pay")[0]
            _success_text = pui.deposit_success_card(
                amount=f"${credited_usd:.2f} USD",
                payment_method=_gw_label,
                deposit_id=pui.format_deposit_id(tx_id, order_created_at),
                bonus_line=_bonus_str,
            )
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=sanitize_message(_success_text),
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

    if not has_permission(update.effective_user.id, "manage_payments"):
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

    from services.payment_workflow import (
        run_auto_verification_with_retries, VerificationLockBusy,
        VERIFY_SUCCESS, VERIFY_TERMINAL, VERIFY_RETRYABLE,
    )

    svc = ZiniPayService()

    def _classify(raw):
        if raw is not None:
            return VERIFY_SUCCESS, "confirmed"
        err = (svc.last_error or "").lower()
        if "wrong amount" in err or "amount" in err or "not configured" in err or "invalid" in err:
            return VERIFY_TERMINAL, svc.last_error or "verification failed"
        return VERIFY_RETRYABLE, svc.last_error or "verification not confirmed yet"

    try:
        result, _kind, _detail = await run_auto_verification_with_retries(
            gateway_id="zinipay", tx_id=tx_id,
            attempt_fn=lambda: svc.verify_transaction(amount=expected_amount, transaction_id=txid_raw),
            classify=_classify, telegram_user_id=telegram_user_id, submitted_txid=txid_raw,
        )
    except VerificationLockBusy:
        await query.answer("⏳ A verification job is already running for this order — please wait.", show_alert=True)
        return

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

    Handles:
      admin_binance_reject_start_{tx_id}_{pmv_id}
      admin_bybit_reject_start_{tx_id}_{pmv_id}
      admin_zinipay_reject_start_{tx_id}_{pmv_id}
      admin_pmv_reject_start_{gateway}_{tx_id}_{pmv_id}   (any other gateway)
    """
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Admin only.", show_alert=True)
        return ConversationHandler.END

    data = query.data
    if data.startswith("admin_pmv_reject_start_"):
        try:
            gateway, tx_id, pmv_id = _parse_pmv_generic_cb(data, "reject_start")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid data.", show_alert=True)
            return ConversationHandler.END
    else:
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

    # Check PMV still pending
    with get_db_session() as session:
        _reject_start_tx = session.query(Transaction).filter_by(id=tx_id).first()
        gw_label = _pmv_gateway_label(gateway, tx=_reject_start_tx)
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
            "❌ <b>Deposit Rejected</b>\n\n"
            f"🆔 {pui.copy_code(pui.format_deposit_id(tx_id))}\n\n"
            "✍ Enter a rejection reason for the user.\n"
            "Or send /skip to reject without a reason."
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
    if not has_permission(update.effective_user.id, "manage_payments"):
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

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if not pmv or pmv.status != "pending":
            await update.message.reply_text("⚠️ This PMV is no longer pending.")
            return ConversationHandler.END

        gw_label = _pmv_gateway_label(gateway, tx=session.query(Transaction).filter_by(id=tx_id).first())

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
            CQH(admin_reject_start, pattern=r"^admin_pmv_reject_start_[a-zA-Z0-9_]+_\d+_\d+$"),
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

    if not has_permission(update.effective_user.id, "manage_payments"):
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


# =============================================================================
# GENERIC PMV HANDLERS — Approve / Reject / Verify Again for ANY gateway not
# covered by the legacy Binance Pay / Bybit Pay / ZiniPay handlers above.
#
# Those three gateways keep using their existing, richer, per-gateway
# callback patterns (admin_binance_*, admin_bybit_*, admin_zinipay_*) —
# untouched, so nothing already working changes. This section exists so
# that Cryptomus, NOWPayments, CryptoBot, Heleket, and any gateway
# registered in the future can reach the exact same "failed auto
# verification -> admin review -> Approve/Reject/Verify Again" workflow
# with ZERO gateway-specific code added here or in bot.py when a new
# gateway is registered.
#
# Callback patterns:
#   admin_pmv_approve_{gateway}_{tx_id}_{pmv_id}
#   admin_pmv_reject_{gateway}_{tx_id}_{pmv_id}    (no-reason reject —
#       the fastest path; see module docstring on _pmv_resolve for the
#       richer reason-prompt flow the three legacy gateways use)
#   admin_pmv_verify_{gateway}_{tx_id}_{pmv_id}
# =============================================================================

def _parse_pmv_generic_cb(data: str, prefix: str):
    """Parse 'admin_pmv_{prefix}_{gateway}_{tx_id}_{pmv_id}' -> (gateway, tx_id, pmv_id).
    Gateway ids may themselves contain underscores (e.g. "binance_pay"),
    so only the trailing two tokens are trusted to be the numeric ids."""
    body = data[len(f"admin_pmv_{prefix}_"):]
    parts = body.split("_")
    if len(parts) < 3:
        raise ValueError(f"malformed PMV callback: {data}")
    pmv_id = int(parts[-1])
    tx_id = int(parts[-2])
    gateway = "_".join(parts[:-2])
    return gateway, tx_id, pmv_id


async def admin_pmv_generic_approve(update, context):
    """Handle admin_pmv_approve_{gateway}_{tx_id}_{pmv_id} for any gateway."""
    query = update.callback_query
    try:
        gateway, tx_id, pmv_id = _parse_pmv_generic_cb(query.data or "", "approve")
    except (ValueError, IndexError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return
    await _pmv_resolve(update, context, gateway, tx_id, pmv_id, approve=True)


async def admin_pmv_generic_verify(update, context):
    """Handle admin_pmv_verify_{gateway}_{tx_id}_{pmv_id} for any gateway
    registered in the Payment Gateway Registry whose service class exposes
    a boolean ``check_payment_status(reference, amount) -> bool`` method
    (Cryptomus, NOWPayments, CryptoBot today — any future gateway with the
    same shape automatically). Re-runs that single check; on success,
    hands off to the same generic ``_pmv_resolve`` approve path used
    everywhere else so wallet crediting is never duplicated. On failure,
    just refreshes the failure reason/timestamp shown on the card — the
    row stays in the Pending Deposits queue exactly as before.

    Gateways with a richer multi-attempt/outcome-classified verifier
    (Binance Pay, Bybit Pay, ZiniPay) keep using their existing
    ``admin_<gateway>_verify_`` handlers — this generic path is only for
    gateways that don't have one.
    """
    query = update.callback_query
    await query.answer("🔄 Re-verifying…", show_alert=False)

    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    try:
        gateway, tx_id, pmv_id = _parse_pmv_generic_cb(query.data or "", "verify")
    except (ValueError, IndexError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return

    from services.payment_gateway_registry import registry

    g = registry.get(gateway)
    if not g or not g.service_cls:
        await query.answer("⚠️ This gateway has no automated re-check available.", show_alert=True)
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
        reference = pmv.submitted_txid
        amount = float(pmv.amount)
        pmv_telegram_user_id = pmv.telegram_user_id

    from services.payment_workflow import (
        acquire_verification_lock, release_verification_lock, log_verification_attempt,
    )

    with get_db_session() as _lock_sess:
        got_lock = acquire_verification_lock(_lock_sess, tx_id)
        _lock_sess.commit()
    if not got_lock:
        await query.answer("⏳ A verification check is already running for this order — please wait.", show_alert=True)
        return

    try:
        try:
            svc = g.service_cls()
            is_paid = await asyncio.to_thread(svc.check_payment_status, reference, amount)
        except Exception as e:
            logger.warning("Generic PMV verify-again raised for gateway=%s pmv=%s", gateway, pmv_id, exc_info=True)
            is_paid = False
            _err = f"{type(e).__name__}: {e}"
        else:
            _err = None
        log_verification_attempt(
            gateway_id=gateway,
            tx_id=tx_id,
            telegram_user_id=pmv_telegram_user_id,
            submitted_txid=reference,
            outcome="PAID" if is_paid else ("ERROR" if _err else "NOT_PAID_YET"),
            detail=_err or ("Confirmed by gateway" if is_paid else "Still not confirmed — admin-triggered re-check"),
        )
    finally:
        release_verification_lock(tx_id)

    if is_paid:
        # Success -> reuse the exact same generic approve path (credits
        # wallet once, marks approved, notifies user/admin) — no duplicated
        # wallet logic here.
        await _pmv_resolve(update, context, gateway, tx_id, pmv_id, approve=True)
        return

    # Still not confirmed — update the card in place with a fresh timestamp.
    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id, gateway=gateway).first()
        if pmv and pmv.status == "pending":
            pmv.auto_detail = _err or "Still not confirmed by gateway"
            session.commit()

    try:
        import re as _re
        old_text = query.message.text or ""
        stamp = datetime.utcnow().strftime("%H:%M:%S UTC")
        new_text = _re.sub(
            r"(⚠ Auto Verify:.*(?:\n.*)?)",
            f"⚠ Auto Verify: Failed\n❌ {_err or 'Still not confirmed by gateway'}",
            old_text,
        ) if "⚠ Auto Verify:" in old_text else old_text
        new_text += f"\n\n<i>🔄 Re-verified at {stamp} — still not confirmed</i>"
        await query.edit_message_text(
            new_text,
            reply_markup=query.message.reply_markup,
            parse_mode="HTML",
        )
    except Exception:
        await query.answer("⚠️ Still not confirmed by the gateway.", show_alert=True)
