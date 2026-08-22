"""Payment and wallet management handlers."""

from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, User, Transaction, Order, OrderItem, Product,
    ProductKey, TransactionStatus, OrderStatus, PaymentMethod, ProductType
)
from utils import (
    get_or_create_user, format_price, validate_amount,
    create_cancel_keyboard, create_payment_method_keyboard,
    create_reference_cancel_keyboard,
    create_quantity_keyboard, create_main_menu_keyboard,
    calculate_expiry_time, notify_admin, check_user_banned
)
from decimal import Decimal
from config.settings import settings as app_settings
from services.crypto_bot import CryptoBotService
from services import binance_pay, bybit_pay, zinipay

# Conversation states for top-up
AMOUNT, METHOD, REFERENCE = range(3)

# Conversation states for direct purchase
PURCHASE_QUANTITY = 10


async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the wallet top-up flow."""
    query = update.callback_query
    await query.answer()

    message = "💬 Please reply the amount USD you want to fund your wallet.\nExample: 100"

    await query.edit_message_text(
        message,
        reply_markup=create_cancel_keyboard()
    )

    return AMOUNT


async def topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle amount input for wallet top-up."""
    amount_str = update.message.text

    # Validate amount
    is_valid, amount, error_msg = validate_amount(amount_str)

    if not is_valid:
        await update.message.reply_text(
            f"❌ {error_msg}\n\nPlease enter a valid amount:",
            reply_markup=create_cancel_keyboard()
        )
        return AMOUNT

    # Store amount in context
    context.user_data['topup_amount'] = amount

    # Show payment method selection directly (skip crypto selection for CryptoBot)
    message = f"💰 Amount: ${amount:.2f}\n\n💬 Please choose a payment method:"

    await update.message.reply_text(
        message,
        reply_markup=create_payment_method_keyboard()
    )

    return METHOD


async def payment_method_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Crypto Wallet payment method selection."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

        if not user:
            await query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

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

            message = f"""⚠️ You already have a pending CryptoBot payment!

💬 CryptoBot Payment

💰 Amount: {format_price(existing_pending.amount)}
🆔 Order ID: #{existing_pending.id}

Click the button below to complete your payment. You can pay with ANY cryptocurrency supported by CryptoBot:

✅ BTC (Bitcoin)
✅ TON (Toncoin)
✅ USDT (TRC20, TON)
✅ USDC (TRC20, TON)
✅ ETH (Ethereum)
✅ LTC (Litecoin)
✅ BNB (Binance Coin)
✅ TRX (Tron)
And many more!

The system will automatically verify and add ${existing_pending.amount:.2f} to your balance as soon as your payment is confirmed.

⏰ Expires: {existing_pending.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC') if existing_pending.expires_at else 'N/A'}

You cannot create a new order until this one is completed or expired."""

            # Create keyboard with payment button
            keyboard = [
                [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return ConversationHandler.END

        # Create transaction record
        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(app_settings.PAYMENT_EXPIRY_HOURS)
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
            await query.edit_message_text("❌ Failed to generate payment invoice. Please try again.")
            return ConversationHandler.END

        # Update transaction with crypto address (format: "invoice_id|pay_url")
        transaction.crypto_address = payment_address
        session.commit()

        # Extract pay_url from payment_address
        if "|" in payment_address:
            invoice_id, pay_url = payment_address.split("|", 1)
            print(f"Invoice created: ID={invoice_id}, URL={pay_url}")
        else:
            # Fallback for unexpected format
            pay_url = payment_address

        # Show payment instructions
        message = f"""💬 CryptoBot Payment

💰 Amount: {format_price(usd_amount)}
🆔 Order ID: #{transaction.id}

Click the button below to open the payment page. You can pay with ANY cryptocurrency supported by CryptoBot:

✅ BTC (Bitcoin)
✅ TON (Toncoin)
✅ USDT (TRC20, TON)
✅ USDC (TRC20, TON)
✅ ETH (Ethereum)
✅ LTC (Litecoin)
✅ BNB (Binance Coin)
✅ TRX (Tron)
And many more!

The system will automatically verify and add ${usd_amount:.2f} to your balance as soon as your payment is confirmed.

⏰ This order will expire in 30 Minutes."""

        # Create keyboard with payment button
        keyboard = [
            [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup)

    return ConversationHandler.END


async def payment_method_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Card payment via Telegram Payments (native sendInvoice flow)."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    provider_token = app_settings.TELEGRAM_PROVIDER_TOKEN
    if not provider_token:
        await query.edit_message_text(
            "❌ Card payments are not configured yet.\n\nPlease choose another payment method or contact support.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    if usd_amount <= 0:
        await query.edit_message_text("❌ Invalid amount. Please start the top-up again.")
        return ConversationHandler.END

    # Create a pending transaction; its id is carried in the invoice payload.
    # Card transactions have no expires_at: confirmation arrives via Telegram's
    # successful_payment update, so the expiry job should not touch them.
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text("❌ User not found.")
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

    # Replace the method-selection message with a short notice, then send the invoice.
    try:
        await query.edit_message_text(
            f"""💳 Card Payment

💰 Amount: {format_price(usd_amount)}
🆔 Order ID: #{transaction_id}

Please complete the secure card payment below 👇"""
        )
    except Exception:
        pass

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


async def payment_method_bkash_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bKash/Nagad/Rocket payment via ZiniPay hosted checkout."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    if not app_settings.ZINIPAY_API_KEY:
        await query.edit_message_text(
            "❌ bKash/Nagad payments are not configured yet.\n\nPlease choose another payment method.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.BKASH_NAGAD,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(app_settings.PAYMENT_EXPIRY_HOURS)
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        transaction_id = transaction.id

        webhook_url = (
            f"{app_settings.PUBLIC_BASE_URL}/webhook/zinipay"
            if app_settings.PUBLIC_BASE_URL else None
        )

        try:
            invoice_id, payment_url = zinipay.create_invoice(usd_amount, transaction_id, webhook_url)
        except zinipay.ZiniPayError as e:
            transaction.status = TransactionStatus.FAILED
            session.commit()
            await query.edit_message_text(f"❌ Failed to create payment invoice. Please try again.\n\n({e})")
            return ConversationHandler.END

        transaction.crypto_address = f"{invoice_id}|{payment_url}"
        session.commit()

    message = f"""📱 bKash / Nagad / Rocket Payment

💰 Amount: {format_price(usd_amount)}
🆔 Order ID: #{transaction_id}

Tap the button below, then choose bKash, Nagad, or Rocket on the payment page and complete it.

The system will automatically verify and add ${usd_amount:.2f} to your balance as soon as your payment is confirmed.

⏰ This order will expire in 30 minutes."""

    keyboard = [
        [InlineKeyboardButton("💳 Pay with bKash/Nagad/Rocket", url=payment_url)],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(message, reply_markup=reply_markup)
    return ConversationHandler.END


async def payment_method_binance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Binance payment instructions and ask the buyer to submit a reference."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)

    if not app_settings.BINANCE_API_KEY or not app_settings.BINANCE_PAY_ID:
        await query.edit_message_text(
            "❌ Binance Pay is not configured yet.\n\nPlease choose another payment method.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    context.user_data['topup_method'] = 'binance'

    lines = [
        "🟡 Binance Pay",
        "",
        f"💰 Amount to send: {format_price(usd_amount)}",
        f"🆔 Send to Binance ID / Pay ID: `{app_settings.BINANCE_PAY_ID}`",
    ]
    if app_settings.BINANCE_DEPOSIT_ADDRESS:
        lines += [
            "",
            "Or send on-chain to:",
            f"`{app_settings.BINANCE_DEPOSIT_ADDRESS}` ({app_settings.BINANCE_DEPOSIT_COIN})",
        ]
    lines += [
        "",
        "After sending, reply with the transaction ID shown in your Binance app "
        "(or the on-chain txid if you sent on-chain).",
    ]

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=create_reference_cancel_keyboard(),
        parse_mode='Markdown'
    )
    return REFERENCE


async def payment_method_bybit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Bybit payment instructions and ask the buyer to submit a reference."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)

    if not app_settings.BYBIT_API_KEY or not app_settings.BYBIT_UID:
        await query.edit_message_text(
            "❌ Bybit Pay is not configured yet.\n\nPlease choose another payment method.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    context.user_data['topup_method'] = 'bybit'

    lines = [
        "⚫ Bybit Pay",
        "",
        f"💰 Amount to send: {format_price(usd_amount)}",
        f"🆔 Send to Bybit UID / email: `{app_settings.BYBIT_UID}`",
        "(use Bybit's internal transfer — free, instant, no network selection)",
    ]
    if app_settings.BYBIT_DEPOSIT_ADDRESS:
        lines += [
            "",
            "Or send on-chain to:",
            f"`{app_settings.BYBIT_DEPOSIT_ADDRESS}` "
            f"({app_settings.BYBIT_DEPOSIT_COIN}, {app_settings.BYBIT_DEPOSIT_CHAIN} network)",
        ]
    lines += [
        "",
        "After sending, reply with the transaction ID from your Bybit transfer receipt "
        "(or the on-chain txid if you sent on-chain).",
    ]

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=create_reference_cancel_keyboard(),
        parse_mode='Markdown'
    )
    return REFERENCE


async def payment_reference_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the buyer submitting a Binance/Bybit transaction reference for verification."""
    reference = update.message.text.strip()
    method = context.user_data.get('topup_method')
    usd_amount = context.user_data.get('topup_amount', 0)
    telegram_id = update.effective_user.id

    if method not in ('binance', 'bybit') or not usd_amount:
        await update.message.reply_text(
            "❌ Something went wrong with this top-up session. Please start again with /start."
        )
        context.user_data.clear()
        return ConversationHandler.END

    if not reference:
        await update.message.reply_text(
            "❌ Please send the transaction ID as text.",
            reply_markup=create_reference_cancel_keyboard()
        )
        return REFERENCE

    payment_method = PaymentMethod.BINANCE_PAY if method == 'binance' else PaymentMethod.BYBIT_PAY
    verify_fn = binance_pay.verify_payment if method == 'binance' else bybit_pay.verify_payment
    error_cls = binance_pay.BinancePaymentError if method == 'binance' else bybit_pay.BybitPaymentError

    await update.message.reply_text("🔎 Checking your payment, please wait...")

    try:
        ok, message = verify_fn(reference, Decimal(str(usd_amount)))
    except error_cls as e:
        # API call itself failed (network/auth issue) — don't leave the
        # buyer stuck; record it as pending for admin review instead of
        # silently failing.
        ok, message = False, f"Verification temporarily unavailable ({e}). An admin will review it."

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            context.user_data.clear()
            return ConversationHandler.END

        # Reject upfront if this exact reference was already used for any
        # transaction (prevents replaying one real payment for multiple
        # top-ups, whether or not verification above succeeded).
        existing = session.query(Transaction).filter_by(external_reference=reference).first()
        if existing:
            await update.message.reply_text(
                "❌ This transaction ID has already been used for a top-up.",
                reply_markup=create_main_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=payment_method,
            external_reference=reference,
            status=TransactionStatus.PENDING
        )
        session.add(transaction)

        if not ok:
            session.commit()
            await update.message.reply_text(
                f"⚠️ {message}\n\n"
                f"Your submission has been recorded (Order #{transaction.id}) and an admin "
                f"will review it if it doesn't verify automatically on retry. "
                f"You can also try /start → check your order history later.",
                reply_markup=create_main_menu_keyboard()
            )
            await notify_admin(
                context,
                f"⚠️ Unverified {payment_method.value} top-up\n\n"
                f"User: {telegram_id}\n"
                f"Amount: {format_price(usd_amount)}\n"
                f"Reference: {reference}\n"
                f"Reason: {message}\n"
                f"Order ID: #{transaction.id}"
            )
            context.user_data.clear()
            return ConversationHandler.END

        # Verified — atomically credit the wallet, conditioned on this
        # transaction still being PENDING (mirrors the CryptoBot webhook /
        # purchase-flow pattern elsewhere in this file, so a duplicate
        # verification call can't double-credit).
        updated_rows = session.query(Transaction).filter(
            Transaction.id == transaction.id,
            Transaction.status == TransactionStatus.PENDING
        ).update({
            Transaction.status: TransactionStatus.COMPLETED,
            Transaction.completed_at: datetime.utcnow()
        }, synchronize_session=False)

        if updated_rows == 0:
            session.rollback()
            await update.message.reply_text("❌ Could not process this top-up. Please contact support.")
            context.user_data.clear()
            return ConversationHandler.END

        user.wallet_balance += usd_amount
        session.commit()
        new_balance = user.wallet_balance

    await update.message.reply_text(
        f"✅ Payment verified!\n\n"
        f"💰 Amount: {format_price(usd_amount)}\n"
        f"💵 New Balance: {format_price(new_balance)}",
        reply_markup=create_main_menu_keyboard()
    )
    await notify_admin(
        context,
        f"✅ {payment_method.value} top-up confirmed\n\n"
        f"User: {telegram_id}\n"
        f"Amount: {format_price(usd_amount)}\n"
        f"Reference: {reference}"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout query if it maps to a valid pending card top-up."""
    query = update.pre_checkout_query
    payload = query.invoice_payload or ""

    transaction_id = None
    if payload.startswith("topup_"):
        try:
            transaction_id = int(payload.split("_", 1)[1])
        except (ValueError, IndexError):
            transaction_id = None

    is_valid = False
    if transaction_id is not None:
        with get_db_session() as session:
            transaction = session.query(Transaction).filter_by(
                id=transaction_id,
                payment_method=PaymentMethod.CARD
            ).first()
            # Allow if not already credited (PENDING, or EXPIRED for a late-but-honoured pay).
            if transaction and transaction.status != TransactionStatus.COMPLETED:
                is_valid = True

    if is_valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="This payment order is no longer valid. Please start a new top-up."
        )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Credit the wallet once Telegram confirms a successful card payment."""
    payment = update.message.successful_payment
    payload = payment.invoice_payload or ""

    if not payload.startswith("topup_"):
        return

    try:
        transaction_id = int(payload.split("_", 1)[1])
    except (ValueError, IndexError):
        return

    notif = None
    with get_db_session() as session:
        transaction = session.query(Transaction).filter_by(
            id=transaction_id,
            payment_method=PaymentMethod.CARD
        ).first()

        if not transaction:
            return

        # Idempotency guard: atomically flip PENDING -> COMPLETED in a single
        # UPDATE so a duplicate Telegram delivery of this update can never
        # double-credit the wallet.
        updated_rows = session.query(Transaction).filter(
            Transaction.id == transaction.id,
            Transaction.status == TransactionStatus.PENDING
        ).update({
            Transaction.status: TransactionStatus.COMPLETED,
            Transaction.completed_at: datetime.utcnow(),
            # Store Telegram's charge id in crypto_address for reference.
            Transaction.crypto_address: f"tg_charge:{payment.telegram_payment_charge_id}"
        }, synchronize_session=False)

        if updated_rows == 0:
            return

        user = session.query(User).filter_by(id=transaction.user_id).first()
        if user:
            user.wallet_balance += transaction.amount
            session.commit()
            notif = {
                'telegram_id': user.telegram_id,
                'amount': transaction.amount,
                'new_balance': user.wallet_balance,
                'transaction_id': transaction.id
            }

    if not notif:
        return

    user_message = f"""✅ Payment Confirmed!

💳 Method: Card
💰 Amount: {format_price(notif['amount'])}
🔄 Your new wallet balance: {format_price(notif['new_balance'])}

Thank you for your payment!"""

    await update.message.reply_text(user_message, reply_markup=create_main_menu_keyboard())

    admin_message = f"""💳 New Card Payment Received

👤 User ID: {notif['telegram_id']}
💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}
🔄 Payment Method: Card"""

    await notify_admin(context, admin_message)


async def cancel_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the top-up process (during conversation)."""
    query = update.callback_query
    await query.answer()

    from utils import create_back_support_keyboard

    await query.edit_message_text(
        "❌ Top-up cancelled.",
        reply_markup=create_back_support_keyboard()
    )

    # Clear user data
    context.user_data.clear()

    return ConversationHandler.END


async def cancel_payment_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel from payment instruction page (outside conversation)."""
    query = update.callback_query
    await query.answer()

    from utils import create_main_menu_keyboard

    await query.edit_message_text(
        "❌ Payment cancelled. You can try again anytime.",
        reply_markup=create_main_menu_keyboard()
    )

    # Clear user data
    context.user_data.clear()


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
                elif transaction.payment_method == PaymentMethod.BKASH_NAGAD:
                    # Fallback in case the ZiniPay webhook delivery was missed.
                    if transaction.crypto_address and "|" in transaction.crypto_address:
                        invoice_id = transaction.crypto_address.split("|", 1)[0]
                        is_paid = zinipay.is_paid(invoice_id, transaction.amount)

                if is_paid:
                    # Idempotency guard: atomically flip PENDING -> COMPLETED.
                    # If the webhook (or another poll tick) already completed
                    # this transaction, rowcount is 0 and we skip crediting
                    # the wallet a second time.
                    updated_rows = session.query(Transaction).filter(
                        Transaction.id == transaction.id,
                        Transaction.status == TransactionStatus.PENDING
                    ).update({
                        Transaction.status: TransactionStatus.COMPLETED,
                        Transaction.completed_at: datetime.utcnow()
                    }, synchronize_session=False)

                    if updated_rows == 0:
                        continue

                    # Update user wallet balance
                    user = session.query(User).filter_by(id=transaction.user_id).first()
                    if user:
                        user.wallet_balance += transaction.amount
                        session.commit()

                        # Store notification data
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
        user_message = f"""✅ Payment Confirmed!

💰 Amount: {format_price(notif['amount'])}
🔄 Your new wallet balance: {format_price(notif['new_balance'])}

Thank you for your payment!"""

        try:
            await context.bot.send_message(
                chat_id=notif['user_telegram_id'],
                text=user_message
            )
        except Exception:
            pass

        # Notify admin
        admin_message = f"""💰 New Payment Received

👤 User ID: {notif['user_telegram_id']}
💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}
🔄 Payment Method: {notif['payment_method']}"""

        await notify_admin(context, admin_message)


async def check_expired_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to mark expired payment transactions (non-blocking)."""
    import asyncio

    def _check_expired_sync():
        """Synchronous database operations run in thread pool."""
        expired_notifications = []

        with get_db_session() as session:
            pending_transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING
            ).all()

            for transaction in pending_transactions:
                if transaction.expires_at and datetime.utcnow() > transaction.expires_at:
                    # Mark as expired
                    transaction.status = TransactionStatus.EXPIRED

                    # Get user info for notification
                    user = session.query(User).filter_by(id=transaction.user_id).first()
                    if user:
                        expired_notifications.append({
                            'telegram_id': user.telegram_id,
                            'amount': transaction.amount,
                            'transaction_id': transaction.id
                        })

                    session.commit()

        return expired_notifications

    # Run blocking database operations in thread pool
    notifications = await asyncio.to_thread(_check_expired_sync)

    # Send notifications asynchronously
    for notif in notifications:
        message = f"""⏰ Payment Order Expired

💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}

Your payment order has expired. Please create a new top-up request if you still want to fund your wallet."""

        try:
            await context.bot.send_message(
                chat_id=notif['telegram_id'],
                text=message
            )
        except Exception:
            # User may have blocked the bot
            pass


async def buy_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the direct purchase flow - ask for quantity."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return ConversationHandler.END

    # Extract product_id from callback data (format: buy_123)
    product_id = int(query.data.split("_")[1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await query.edit_message_text("❌ Product not found.")
            return ConversationHandler.END

        if not product.is_active:
            await query.edit_message_text("❌ This product is no longer available.")
            return ConversationHandler.END

        if product.stock_count == 0:
            await query.edit_message_text("❌ This product is out of stock.")
            return ConversationHandler.END

        # Store product info in context for later
        context.user_data['purchase_product_id'] = product_id
        context.user_data['purchase_product_name'] = product.name
        context.user_data['purchase_product_price'] = product.price
        context.user_data['purchase_product_stock'] = product.stock_count
        context.user_data['purchase_product_type'] = product.product_type

        # For file products, quantity is always 1
        if product.product_type == ProductType.FILE:
            context.user_data['purchase_quantity'] = 1
            # Skip quantity input, go straight to confirmation
            return await show_purchase_confirmation(update, context)

        # For key products, ask for quantity
        message = f"""🛒 Purchase: {product.name}

💰 Price: {format_price(product.price)} each
📦 Available: {product.stock_count}

💬 Please enter the quantity you want to buy (1-{product.stock_count}):"""

        # If coming from a photo message, delete it and create new text message
        if query.message.photo:
            await query.message.delete()
            await query.message.reply_text(
                message,
                reply_markup=create_quantity_keyboard(product_id)
            )
        else:
            await query.edit_message_text(
                message,
                reply_markup=create_quantity_keyboard(product_id)
            )

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

    total = product_price * quantity
    telegram_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            if is_message:
                await update.message.reply_text("❌ User not found.")
            else:
                await update.callback_query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

        wallet_balance = user.wallet_balance
        has_sufficient_balance = wallet_balance >= total

        if has_sufficient_balance:
            balance_text = f"💰 Your Wallet Balance: {format_price(wallet_balance)}"
        else:
            balance_text = f"⚠️ Insufficient Balance!\n💰 Your Wallet Balance: {format_price(wallet_balance)}\n\n💡 Please top up your wallet first."

        message = f"""🛒 Confirm Purchase

📦 Product: {product_name}
💰 Price: {format_price(product_price)} x {quantity}
💵 Total: {format_price(total)}

{balance_text}"""

        if has_sufficient_balance:
            keyboard = [
                [InlineKeyboardButton("✅ Confirm Purchase", callback_data=f"confirm_purchase_{product_id}_{quantity}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton("💰 Top Up Wallet", callback_data="topup")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")]
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
                await query.edit_message_text(message, reply_markup=reply_markup)

        return ConversationHandler.END


async def confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the confirmed purchase."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    # Extract product_id and quantity from callback data (format: confirm_purchase_123_5)
    parts = query.data.split("_")
    product_id = int(parts[2])
    quantity = int(parts[3])

    telegram_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.edit_message_text("❌ User not found.")
            return

        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            await query.edit_message_text("❌ Product not found.")
            return

        if not product.is_active:
            await query.edit_message_text("❌ This product is no longer available.")
            return

        if product.stock_count < quantity:
            await query.edit_message_text(f"❌ Not enough stock. Only {product.stock_count} available.")
            return

        total = product.price * quantity

        # Check balance
        if user.wallet_balance < total:
            await query.edit_message_text(
                f"❌ Insufficient balance.\n💰 Your balance: {format_price(user.wallet_balance)}\n💵 Required: {format_price(total)}"
            )
            return

        # Atomically deduct funds now, conditioned on the balance still being
        # sufficient. This closes the race where a user double-taps "confirm"
        # (or opens the confirmation on two devices) and both requests pass
        # the balance check above before either commits — only one UPDATE can
        # succeed since the WHERE clause re-checks the balance at write time.
        deducted_rows = session.query(User).filter(
            User.id == user.id,
            User.wallet_balance >= total
        ).update({
            User.wallet_balance: User.wallet_balance - total
        }, synchronize_session=False)

        if deducted_rows == 0:
            session.rollback()
            await query.edit_message_text("❌ Insufficient balance. Please refresh and try again.")
            return

        # Atomically reserve stock the same way — conditioned on enough
        # stock still being available, so two simultaneous buyers of the
        # last unit can't both succeed.
        stock_rows = session.query(Product).filter(
            Product.id == product.id,
            Product.stock_count >= quantity
        ).update({
            Product.stock_count: Product.stock_count - quantity
        }, synchronize_session=False)

        if stock_rows == 0:
            session.rollback()
            await query.edit_message_text("❌ Not enough stock. Please try again.")
            return

        # Create order (flush, not commit, so it stays in the same
        # transaction as the balance/stock changes above — nothing is
        # permanent yet).
        order = Order(
            user_id=user.id,
            total_amount=total,
            status=OrderStatus.COMPLETED
        )
        session.add(order)
        session.flush()
        session.refresh(order)

        # Create order item
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            price=product.price
        )

        # Deliver assets based on product type
        order_details = ""
        if product.product_type == ProductType.KEY:
            try:
                # Atomically assign keys from product_keys table
                keys = assign_product_keys(session, product.id, quantity, order.id)
            except ValueError:
                # Not enough keys actually available (stock_count was out of
                # sync with real key inventory). Roll back everything —
                # balance deduction, stock decrement, and order — so the
                # user is never charged for something that wasn't delivered.
                session.rollback()
                await query.edit_message_text(
                    "❌ Not enough stock available right now. You have not been charged."
                )
                return
            order_item.delivered_asset = "\n".join(keys)
            order_details = f"📦 {product.name} (x{quantity})\n🔐 Keys:\n{order_item.delivered_asset}\n"

        elif product.product_type == ProductType.FILE:
            # Provide download link
            order_item.delivered_asset = product.download_link
            order_details = f"📦 {product.name}\n🔗 Download: {order_item.delivered_asset}\n"

        session.add(order_item)

        # Everything above (balance deduction, stock decrement, order,
        # order item, key assignment) commits together as one transaction.
        session.commit()

        # Notify user
        user_message = f"""✅ Purchase Successful!

💰 Total Amount: {format_price(total)}
📝 Order ID: #{order.id}

{order_details}
Thank you for your purchase!"""

        # Create keyboard with Home and Order History buttons
        keyboard = [
            [
                InlineKeyboardButton("🏠 Home", callback_data="main_menu"),
                InlineKeyboardButton("📋 Order History", callback_data="order_history")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(user_message, reply_markup=reply_markup)

        # Notify admin
        admin_message = f"""🛍 New Order Received

👤 User ID: {telegram_id}
💰 Amount: {format_price(total)}
📝 Order ID: #{order.id}

{order_details}"""

        await notify_admin(context, admin_message)


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

    await query.edit_message_text(
        "❌ Purchase cancelled.",
        reply_markup=create_main_menu_keyboard()
    )

    return ConversationHandler.END


def assign_product_keys(session, product_id: int, quantity: int, order_id: int) -> list:
    """Atomically assign product keys to an order from the product_keys table."""
def assign_product_keys(session, product_id: int, quantity: int, order_id: int) -> list:
    """Atomically claim `quantity` unsold keys for this product and tag them
    with order_id, in a single UPDATE statement.

    This does NOT rely on SELECT ... FOR UPDATE (silently ignored by SQLite)
    or RETURNING (only supported on SQLite 3.35+). A single UPDATE ... WHERE
    id IN (subquery) statement is atomic on every backend: SQLite serializes
    writers at the file level, so a second concurrent UPDATE simply waits for
    the first to commit and then re-evaluates its own subquery against the
    now-updated rows — it can never claim a key another buyer already took.
    Does NOT commit; caller controls the transaction so a failure here can
    roll back any wallet deduction that already happened in the same purchase.
    """
    subquery = (
        session.query(ProductKey.id)
        .filter(ProductKey.product_id == product_id, ProductKey.is_sold == False)  # noqa: E712
        .limit(quantity)
        .subquery()
    )

    claimed = session.query(ProductKey).filter(
        ProductKey.id.in_(session.query(subquery.c.id))
    ).update({
        ProductKey.is_sold: True,
        ProductKey.order_id: order_id,
        ProductKey.sold_at: datetime.utcnow()
    }, synchronize_session=False)

    if claimed < quantity:
        raise ValueError(f"Not enough keys available. Requested: {quantity}, Claimed: {claimed}")

    session.flush()
    keys = session.query(ProductKey.key_value).filter_by(order_id=order_id).all()
    return [k[0] for k in keys]


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
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
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
