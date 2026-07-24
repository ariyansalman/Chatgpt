"""Webhook server for receiving CryptoBot payment notifications.

This server receives real-time payment notifications from CryptoBot
when invoices are paid, providing immediate payment confirmation.

Setup:
1. Install Flask: pip install flask
2. For local testing, use ngrok: ngrok http 5000
3. Configure webhook in CryptoBot:
   - Open @CryptoBot in Telegram
   - Go to Crypto Pay → My Apps → Select your app → Webhooks
   - Enable webhooks and set URL: https://your-domain.com/webhook/cryptobot
4. For production, deploy this on a server with HTTPS
"""

from flask import Flask, request, jsonify, g
import hmac
import hashlib
import json
import logging
import os
import time
import uuid as _uuid
from datetime import datetime
from typing import Optional
from database.db import get_db_session
from database.models import Transaction, TransactionStatus, User
from config.settings import settings

logger = logging.getLogger(__name__)
app = Flask(__name__)

# ── V27: Webhook observability middleware ──────────────────────────────────
# These hooks add observability-only logging without modifying any payment
# logic or response codes.  Failures are swallowed — a logging error must
# NEVER affect payment processing.

_PROVIDER_ROUTES = {
    "/webhook/cryptobot":  "telegram",
    "/webhook/heleket":    "heleket",
    "/webhook/bkash":      "mobile_banking",
    "/webhook/nowpayments":"nowpayments",
    "/webhook/binance":    "binance",
    "/webhook/bybit":      "bybit",
    "/webhook/trc20":      "trc20",
    "/webhook/bep20":      "bep20",
    "/webhook/erc20":      "erc20",
}


@app.before_request
def _wh_before():
    """Record request start time and snapshot raw body for logging."""
    try:
        g._wh_start = time.monotonic()
        # Peek at the raw payload for logging BEFORE the handler reads it.
        # Flask caches request.data so the handler still gets the full body.
        g._wh_raw = request.get_data(as_text=True)[:4096]
    except Exception:
        pass


@app.after_request
def _wh_after(response):
    """Log completed webhook request to webhook_log table."""
    try:
        path     = request.path
        provider = _PROVIDER_ROUTES.get(path)
        if provider is None:
            return response  # not a tracked webhook route

        elapsed_ms = int((time.monotonic() - getattr(g, "_wh_start", time.monotonic())) * 1000)
        raw_body   = getattr(g, "_wh_raw", "")
        status_code = response.status_code

        # Derive a stable UUID from the provider + body hash (replay-safe)
        body_hash  = hashlib.sha256(raw_body.encode()).hexdigest()[:32]
        wh_uuid    = f"{provider}:{body_hash}"

        # Map HTTP status → webhook status string
        if status_code in (200, 201):
            wh_status = "processed"
        elif status_code == 401:
            wh_status = "ignored"   # bad signature
        elif status_code == 409:
            wh_status = "duplicate"
        else:
            wh_status = "failed"

        # Extract common fields from JSON body (best-effort)
        try:
            body_json   = json.loads(raw_body) if raw_body else {}
        except Exception:
            body_json   = {}

        order_id    = body_json.get("order_id") or body_json.get("orderId")
        payment_id  = (str(body_json.get("invoice_id") or body_json.get("payment_id")
                           or body_json.get("uuid") or ""))[:128] or None
        tx_id       = (str(body_json.get("transaction_id") or body_json.get("txid")
                           or body_json.get("tx_hash") or ""))[:128] or None
        try:
            if order_id is not None:
                order_id = int(order_id)
        except (TypeError, ValueError):
            order_id = None

        error_msg = None
        if wh_status == "failed":
            try:
                resp_json = response.get_json(silent=True) or {}
                error_msg = str(resp_json.get("error", ""))[:256]
            except Exception:
                pass

        from services.health_monitor import log_webhook_event
        log_webhook_event(
            provider           = provider,
            webhook_uuid       = wh_uuid,
            status             = wh_status,
            processing_time_ms = elapsed_ms,
            error_message      = error_msg,
            order_id           = order_id,
            payment_id         = payment_id,
            transaction_id     = tx_id,
            raw_payload        = raw_body[:2048],
        )
    except Exception:
        pass  # observability must NEVER break payment processing
    return response


def verify_signature(body: bytes, signature: str) -> bool:
    """
    Verify CryptoBot webhook signature.

    Args:
        body: Raw request body bytes
        signature: Signature from crypto-pay-api-signature header

    Returns:
        True if signature is valid, False otherwise
    """
    # Create secret key from SHA256 hash of API token
    secret_key = hashlib.sha256(settings.CRYPTO_BOT_API_KEY.encode()).digest()

    # Calculate HMAC-SHA256 signature
    calculated_signature = hmac.new(
        secret_key,
        body,
        hashlib.sha256
    ).hexdigest()

    # Compare signatures
    return hmac.compare_digest(calculated_signature, signature)


def process_invoice_paid(invoice_data: dict):
    """
    Process a paid invoice notification.

    Args:
        invoice_data: Invoice object from CryptoBot webhook
    """
    try:
        invoice_id = invoice_data.get('invoice_id')
        status = invoice_data.get('status')
        paid_at = invoice_data.get('paid_at')

        logger.info(f"📩 Webhook received: Invoice #{invoice_id}, status={status}, paid_at={paid_at}")

        if status != 'paid':
            logger.warning(f"⚠️ Invoice {invoice_id} not in 'paid' status, ignoring")
            return

        if not invoice_id:
            logger.error("❌ Webhook payload missing invoice_id, ignoring")
            return

        # Idempotency guard — stable reference is CryptoBot's own invoice_id
        # (never a Telegram update_id; this is an HTTP webhook that CryptoBot
        # may legitimately redeliver on retry). Fail CLOSED: if the claim
        # call itself raises, do NOT credit the wallet.
        try:
            from services.idempotency import claim as _idem_claim
            with _idem_claim("crypto_webhook", f"invoice:{invoice_id}") as _ok:
                if not _ok:
                    logger.warning(f"⚠️ Invoice {invoice_id} already processed (idempotent replay), ignoring")
                    return
        except Exception:
            logger.error(f"❌ idempotency.claim raised for invoice {invoice_id} — refusing to credit wallet (fail closed)", exc_info=True)
            return

        # Find transaction by invoice_id in crypto_address field
        # Format is: "invoice_id|pay_url"
        with get_db_session() as session:
            # Search for transaction with this invoice_id
            transactions = session.query(Transaction).filter(
                Transaction.payment_method.in_(['crypto_wallet']),
                Transaction.status == TransactionStatus.PENDING
            ).all()

            transaction = None
            for txn in transactions:
                if txn.crypto_address and txn.crypto_address.startswith(f"{invoice_id}|"):
                    transaction = txn
                    break

            if not transaction:
                logger.error(f"❌ No pending transaction found for invoice {invoice_id}")
                return

            # Atomic conditional UPDATE — closes the TOCTOU window between the
            # lookup above and the status flip. Only one concurrent caller can
            # win this race; a second delivery of the same webhook (or a race
            # with the polling job in payment_handlers.check_pending_payments)
            # will see rowcount 0 and back off without crediting twice.
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
                logger.warning(f"⚠️ Transaction {transaction.id} already completed by another path, skipping credit")
                return

            # Atomic wallet credit — writes WalletLedger row in same session.
            from services.wallet import credit_locked, WalletError
            try:
                credit_locked(
                    session, transaction.user_id, transaction.amount,
                    reason=f"CryptoBot top-up #{transaction.id}",
                    actor_type="system", ref_type="crypto_webhook",
                    ref_id=str(invoice_id),
                )
            except WalletError as _we:
                logger.error(f"❌ credit_locked failed for invoice {invoice_id}: {_we}")
                session.rollback()
                return
            session.commit()

            user = session.query(User).filter_by(id=transaction.user_id).first()

            logger.info(f"✅ Payment processed via webhook!")
            logger.info(f"   Transaction #{transaction.id}")
            logger.info(f"   User: @{user.username if user else transaction.user_id}")
            logger.info(f"   Amount: ${transaction.amount:.2f}")
            logger.info(f"   New balance: ${user.wallet_balance if user else 0:.2f}")

            # Notify the user their deposit landed. This webhook process has
            # no access to the running bot/Application instance (it's a
            # separate Flask process), so we call the Bot API directly with
            # the token — same fire-and-forget pattern as heleket_webhook()
            # below. Without this, the user gets no confirmation: the
            # background poller in payment_handlers.check_pending_payments()
            # is the only other place that sends this message, and it only
            # looks at transactions still PENDING — which this one no longer
            # is, since we just flipped it to COMPLETED above.
            if user and settings.BOT_TOKEN:
                try:
                    import requests as _requests
                    _requests.post(
                        f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
                        json={
                            'chat_id': user.telegram_id,
                            'text': (
                                f"✅ Payment Confirmed!\n\n"
                                f"💰 Amount: ${transaction.amount:.2f}\n"
                                f"🔄 Your new wallet balance: ${user.wallet_balance:.2f}\n\n"
                                f"Thank you for your payment!"
                            ),
                        },
                        timeout=8,
                    )
                except Exception:
                    logger.error(f"❌ Could not notify user for crypto_webhook invoice {invoice_id}")

    except Exception as e:
        logger.error(f"❌ Error processing webhook: {e}", exc_info=True)


@app.route('/webhook/cryptobot', methods=['POST'])
def cryptobot_webhook():
    """
    Webhook endpoint for CryptoBot payment notifications.

    CryptoBot sends POST requests to this endpoint when invoices are paid.
    """
    try:
        # Get signature from header
        signature = request.headers.get('crypto-pay-api-signature')

        if not signature:
            logger.error("❌ No signature in webhook request")
            return jsonify({'error': 'No signature'}), 401

        # Get raw request body
        body = request.get_data()

        # Verify signature
        if not verify_signature(body, signature):
            logger.error("❌ Invalid webhook signature")
            return jsonify({'error': 'Invalid signature'}), 401

        # Parse JSON
        data = request.get_json()

        logger.info(f"📩 CryptoBot Webhook received:")
        logger.info(json.dumps(data, indent=2))

        # Extract update info
        update_type = data.get('update_type')
        data.get('request_date')
        payload = data.get('payload')

        # Check update type
        if update_type != 'invoice_paid':
            logger.warning(f"⚠️ Unknown update type: {update_type}")
            return jsonify({'ok': True}), 200

        # Process the paid invoice
        process_invoice_paid(payload)

        return jsonify({'ok': True}), 200

    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500



@app.route('/webhook/heleket', methods=['POST'])
def heleket_webhook():
    """Verified, idempotent Heleket Static Wallet balance credit callback."""
    from services.heleket_payment import HeleketPaymentService, PAID_STATUSES
    from services.idempotency import claim_locked
    from services.wallet import credit_locked
    from database.models import HeleketStaticWallet, HeleketDeposit
    from sqlalchemy.exc import IntegrityError
    import logging, requests as _requests
    log = logging.getLogger(__name__)
    data = request.get_json(silent=True) or {}
    sign = data.get('sign', '')
    svc = HeleketPaymentService()
    if not svc.verify_webhook_signature(data, sign):
        log.warning("Invalid Heleket webhook signature")
        return jsonify({'error':'invalid signature'}), 401
    payment_uuid = str(data.get('uuid') or '')
    wallet_uuid = str(data.get('wallet_address_uuid') or '')
    order_id = str(data.get('order_id') or '')
    status = str(data.get('status') or '')
    if data.get('type') != 'wallet' or status not in PAID_STATUSES or not payment_uuid:
        return jsonify({'ok':True}), 200
    try:
        amount_usd = float(data.get('payment_amount_usd'))
        payment_amount = float(data.get('payment_amount'))
        merchant_amount = float(data.get('merchant_amount')) if data.get('merchant_amount') is not None else None
        if amount_usd <= 0 or payment_amount <= 0: raise ValueError
    except (TypeError, ValueError):
        log.warning("Invalid Heleket amount payment=%s", payment_uuid)
        return jsonify({'error':'invalid amount'}), 400
    user_telegram_id = None
    try:
        with get_db_session() as session:
            wallet = None
            if wallet_uuid:
                wallet = session.query(HeleketStaticWallet).filter_by(wallet_address_uuid=wallet_uuid).first()
            if not wallet and order_id:
                wallet = session.query(HeleketStaticWallet).filter_by(order_id=order_id).first()
            if not wallet:
                log.warning("Unknown Heleket wallet payment=%s order=%s", payment_uuid, order_id)
                return jsonify({'ok':True}), 200
            if not claim_locked(session, 'heleket_deposit', payment_uuid):
                log.info("Duplicate Heleket callback payment=%s", payment_uuid)
                return jsonify({'ok':True}), 200
            dep = HeleketDeposit(user_id=wallet.user_id, heleket_payment_uuid=payment_uuid, order_id=wallet.order_id,
                wallet_address_uuid=wallet.wallet_address_uuid, currency=str(data.get('currency') or wallet.currency),
                network=str(data.get('network') or wallet.network), payment_amount=payment_amount,
                payment_amount_usd=amount_usd, merchant_amount=merchant_amount, status=status)
            session.add(dep)
            try:
                session.flush()
            except IntegrityError:
                session.rollback(); log.info("Duplicate Heleket payment row payment=%s", payment_uuid)
                return jsonify({'ok':True}), 200
            new_balance = credit_locked(session, wallet.user_id, amount_usd, reason=f"Heleket {wallet.currency}/{wallet.network} deposit",
                ref_type='heleket_deposit', ref_id=payment_uuid)
            dep.credited_at = datetime.utcnow()
            user_telegram_id = wallet.telegram_user_id
            currency, network = dep.currency, dep.network
            # Create a COMPLETED Transaction row for full audit trail and
            # user-facing transaction history.  Requires PaymentMethod.HELEKET
            # to exist in the DB enum (added by migration 20260722_enumfix).
            from database.models import (
                Transaction as _Tx, TransactionStatus as _TxS,
                PaymentMethod as _PM,
            )
            heleket_tx = _Tx(
                user_id=wallet.user_id,
                amount=amount_usd,
                payment_method=_PM.HELEKET,
                crypto_address=f"heleket:{payment_uuid}",
                status=_TxS.COMPLETED,
                completed_at=datetime.utcnow(),
            )
            session.add(heleket_tx)
        log.info("Heleket deposit credited payment=%s amount_usd=%s", payment_uuid, amount_usd)
        if user_telegram_id and settings.BOT_TOKEN:
            try:
                _dep_text = (
                    "✅ <b>Deposit Successful</b>\n\n"
                    "💰 <b>Amount Credited</b>\n"
                    f"${amount_usd:.2f} {currency}\n\n"
                    "💳 <b>Payment Method</b>\n"
                    "Heleket\n\n"
                    "👛 Your wallet has been updated successfully."
                )
                _requests.post(
                    f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
                    json={
                        'chat_id': user_telegram_id,
                        'text': _dep_text,
                        'parse_mode': 'HTML',
                        'reply_markup': '{"inline_keyboard": [[{"text": "💳 Check Wallet", "callback_data": "wallet"}, {"text": "🛍️ Continue Shopping", "callback_data": "products"}]]}',
                    },
                    timeout=8,
                )
            except Exception:
                log.exception("Could not notify user for Heleket payment=%s", payment_uuid)
        return jsonify({'ok':True}), 200
    except Exception:
        log.exception("Heleket webhook processing failed payment=%s", payment_uuid)
        return jsonify({'error':'processing failed'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'service': 'CryptoBot Webhook Receiver',
        'timestamp': datetime.utcnow().isoformat()
    }), 200


def _credit_wallet_once(source: str, external_ref: str, transaction) -> Optional['User']:
    """Shared atomic credit helper for bKash/Nagad callbacks — same idempotency
    + conditional-UPDATE pattern as process_invoice_paid() above."""
    try:
        from services.idempotency import claim as _idem_claim
        with _idem_claim(source, external_ref) as _ok:
            if not _ok:
                logger.warning(f"⚠️ {source} ref {external_ref} already processed (idempotent replay), ignoring")
                return None
    except Exception:
        logger.error(f"❌ idempotency.claim raised for {source} ref {external_ref} — refusing to credit (fail closed)", exc_info=True)
        return None

    with get_db_session() as session:
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
            logger.warning(f"⚠️ Transaction {transaction.id} already completed by another path, skipping credit")
            return None

        # Atomic wallet credit — writes WalletLedger row in same session.
        from services.wallet import credit_locked, WalletError
        try:
            credit_locked(
                session, transaction.user_id, transaction.amount,
                reason=f"{source} top-up #{transaction.id}",
                actor_type="system", ref_type=source,
                ref_id=external_ref,
            )
        except WalletError as _we:
            logger.error(f"❌ credit_locked failed for {source} ref {external_ref}: {_we}")
            session.rollback()
            return None
        session.commit()
        user = session.query(User).filter_by(id=transaction.user_id).first()

    # Notify the user their deposit landed. Without this, webhook-credited
    # transactions never surface a confirmation: the background poller in
    # handlers/payment_handlers.check_pending_payments() is the only other
    # place that sends this message, and it only looks at transactions still
    # PENDING — which this one no longer is, since we just flipped it to
    # COMPLETED above. Same fire-and-forget pattern as heleket_webhook().
    if user and settings.BOT_TOKEN:
        try:
            import requests as _requests
            _pm_label = source.replace("_webhook", "").replace("_", " ").title()
            _dep_text = (
                "✅ <b>Deposit Successful</b>\n\n"
                "💰 <b>Amount Credited</b>\n"
                f"${transaction.amount:.2f} USD\n\n"
                "💳 <b>Payment Method</b>\n"
                f"{_pm_label}\n\n"
                "👛 Your wallet has been updated successfully."
            )
            _requests.post(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
                json={
                    'chat_id': user.telegram_id,
                    'text': _dep_text,
                    'parse_mode': 'HTML',
                    'reply_markup': '{"inline_keyboard": [[{"text": "💳 Check Wallet", "callback_data": "wallet"}, {"text": "🛍️ Continue Shopping", "callback_data": "products"}]]}',
                },
                timeout=8,
            )
        except Exception:
            logger.error(f"❌ Could not notify user for {source} ref {external_ref}")
    return user


def _find_pending_transaction(method_value: str, gateway_ref: str):
    """Look up the pending Transaction whose crypto_address starts with
    "<gateway_ref>|" — same "id|url" storage convention as CryptoBot."""
    with get_db_session() as session:
        transactions = session.query(Transaction).filter(
            Transaction.payment_method.in_([method_value]),
            Transaction.status == TransactionStatus.PENDING,
        ).all()
        for txn in transactions:
            if txn.crypto_address and txn.crypto_address.startswith(f"{gateway_ref}|"):
                return txn
    return None


@app.route('/webhook/bkash', methods=['GET', 'POST'])
def bkash_webhook():
    """bKash redirects the user's browser here (GET) after checkout with
    ?paymentID=...&status=... . We finalize with execute_payment() and, if
    Completed, credit the wallet — same idempotent flow as the CryptoBot
    webhook above. Polling (check_pending_payments) is the fallback if this
    redirect never lands (e.g. user closes the browser early).
    """
    try:
        payment_id = request.args.get('paymentID') or (request.get_json(silent=True) or {}).get('paymentID')
        status = request.args.get('status', '')
        logger.info(f"📩 bKash callback received: paymentID={payment_id}, status={status}")

        if not payment_id:
            return jsonify({'error': 'missing paymentID'}), 400

        if status.lower() == 'cancel' or status.lower() == 'failure':
            logger.warning(f"⚠️ bKash payment {payment_id} was cancelled/failed by user")
            return "<h3>Payment cancelled. You can return to the bot and try again.</h3>", 200

        from services.bkash_payment import BkashPaymentService
        transaction = _find_pending_transaction('bkash', payment_id)
        if not transaction:
            logger.error(f"❌ No pending bKash transaction found for paymentID {payment_id}")
            return "<h3>No matching pending payment found.</h3>", 404

        result = BkashPaymentService().execute_payment(payment_id)
        if not result or result.get('transactionStatus') != 'Completed':
            logger.info(f"⏳ bKash payment {payment_id} not completed yet: {result}")
            return "<h3>Payment not confirmed yet. If you completed the payment, it will be credited shortly.</h3>", 200

        user = _credit_wallet_once('bkash_webhook', f"payment:{payment_id}", transaction)
        if user:
            logger.info(f"✅ bKash payment processed via webhook! Transaction #{transaction.id}, "
                  f"user {user.telegram_id}, new balance ${user.wallet_balance:.2f}")
        return "<h3>✅ Payment confirmed! Return to the bot — your balance has been updated.</h3>", 200
    except Exception as e:
        logger.error(f"❌ Error processing bKash webhook: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/webhook/nagad', methods=['GET', 'POST'])
def nagad_webhook():
    """Nagad redirects the user's browser here (GET) after checkout with
    ?payment_ref_id=...&status=... . We finalize with verify_payment() and,
    if Success, credit the wallet — same idempotent flow as above.
    """
    try:
        payment_ref_id = (
            request.args.get('payment_ref_id')
            or request.args.get('paymentRefId')
            or (request.get_json(silent=True) or {}).get('payment_ref_id')
        )
        status = request.args.get('status', '')
        logger.info(f"📩 Nagad callback received: payment_ref_id={payment_ref_id}, status={status}")

        if not payment_ref_id:
            return jsonify({'error': 'missing payment_ref_id'}), 400

        if status and status.lower() not in ('success', 'completed'):
            logger.warning(f"⚠️ Nagad payment {payment_ref_id} status={status} (not success)")
            return "<h3>Payment not successful. You can return to the bot and try again.</h3>", 200

        from services.nagad_payment import NagadPaymentService
        transaction = _find_pending_transaction('nagad', payment_ref_id)
        if not transaction:
            logger.error(f"❌ No pending Nagad transaction found for payment_ref_id {payment_ref_id}")
            return "<h3>No matching pending payment found.</h3>", 404

        result = NagadPaymentService().verify_payment(payment_ref_id)
        if not result or result.get('status') != 'Success':
            logger.info(f"⏳ Nagad payment {payment_ref_id} not verified as successful yet: {result}")
            return "<h3>Payment not confirmed yet. If you completed the payment, it will be credited shortly.</h3>", 200

        user = _credit_wallet_once('nagad_webhook', f"payment:{payment_ref_id}", transaction)
        if user:
            logger.info(f"✅ Nagad payment processed via webhook! Transaction #{transaction.id}, "
                  f"user {user.telegram_id}, new balance ${user.wallet_balance:.2f}")
        return "<h3>✅ Payment confirmed! Return to the bot — your balance has been updated.</h3>", 200
    except Exception as e:
        logger.error(f"❌ Error processing Nagad webhook: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/webhook/cryptomus', methods=['POST'])
def cryptomus_webhook():
    """Cryptomus posts server-to-server here when an invoice's status
    changes (unlike bKash/Nagad, which redirect the user's *browser* here
    via GET — Cryptomus never touches the user's browser at all).

    Body contains the invoice fields plus a "sign" field computed the same
    way as outgoing requests (see services/cryptomus_payment.py._sign):
        sign = md5(base64_encode(json_without_sign) + api_key)
    """
    try:
        data = request.get_json(silent=True) or {}
        received_sign = data.get('sign', '')
        uuid = data.get('uuid')
        order_id = data.get('order_id')
        status = data.get('status')
        logger.info(f"📩 Cryptomus webhook received: uuid={uuid}, order_id={order_id}, status={status}")

        if not uuid:
            return jsonify({'error': 'missing uuid'}), 400

        from services.cryptomus_payment import CryptomusPaymentService
        service = CryptomusPaymentService()
        if not service.verify_webhook_signature(data, received_sign):
            logger.error(f"❌ Invalid Cryptomus webhook signature for uuid {uuid}")
            return jsonify({'error': 'invalid signature'}), 401

        if status not in ('paid', 'paid_over'):
            logger.warning(f"⚠️ Cryptomus invoice {uuid} status={status} (not paid), ignoring")
            return jsonify({'ok': True}), 200

        transaction = _find_pending_transaction('cryptomus', uuid)
        if not transaction:
            logger.error(f"❌ No pending Cryptomus transaction found for uuid {uuid}")
            return jsonify({'error': 'no matching pending transaction'}), 404

        user = _credit_wallet_once('cryptomus_webhook', f"payment:{uuid}", transaction)
        if user:
            logger.info(f"✅ Cryptomus payment processed via webhook! Transaction #{transaction.id}, "
                  f"user {user.telegram_id}, new balance ${user.wallet_balance:.2f}")
        return jsonify({'ok': True}), 200
    except Exception as e:
        logger.error(f"❌ Error processing Cryptomus webhook: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/webhook/nowpayments', methods=['POST'])
def nowpayments_webhook():
    """NOWPayments posts server-to-server here (IPN) when a payment status
    changes.  Signed with header "x-nowpayments-sig" (HMAC-SHA512 over the
    alphabetically sorted JSON body).

    Transaction matching strategy (in priority order):
      1. order_id field  — we set this to str(transaction.id) at invoice
         creation time, so it's always our DB id.  Direct, reliable.
      2. invoice_id field from IPN — stored as the first segment of
         crypto_address ("invoice_id|invoice_url").
      3. payment_id field from IPN — older fallback for rows that stored
         payment_id directly in crypto_address.
    """
    import logging as _log
    log = _log.getLogger(__name__)

    try:
        raw_body = request.get_data(as_text=True)
        data = request.get_json(silent=True) or {}
        received_sig = request.headers.get('x-nowpayments-sig', '')

        payment_id = str(data.get('payment_id') or data.get('id') or '')
        order_id = str(data.get('order_id') or '')
        invoice_id = str(data.get('invoice_id') or '')
        status = str(data.get('payment_status') or '').lower()

        # [WEBHOOK RECEIVED] — log the full payload for diagnostics.
        log.info(
            "[NOWPAYMENTS WEBHOOK RECEIVED] payment_id=%s order_id=%s "
            "invoice_id=%s status=%s sig=%s body=%s",
            payment_id, order_id, invoice_id, status, received_sig[:16] or '(none)', raw_body[:500],
        )

        if not payment_id and not order_id:
            log.warning("NOWPayments webhook missing payment_id and order_id")
            return jsonify({'error': 'missing payment_id and order_id'}), 400

        from services.nowpayments_payment import NowPaymentsService, PAID_STATUSES
        service = NowPaymentsService()

        # Signature check — skip only when no IPN secret is configured.
        if service.ipn_secret:
            if not service.verify_webhook_signature(data, received_sig):
                log.warning(
                    "[NOWPAYMENTS WEBHOOK] Invalid signature for payment_id=%s", payment_id
                )
                return jsonify({'error': 'invalid signature'}), 401
            log.info("[NOWPAYMENTS WEBHOOK VERIFIED] payment_id=%s", payment_id)
        else:
            log.warning(
                "[NOWPAYMENTS WEBHOOK] No IPN secret configured — skipping signature check"
            )

        if status not in PAID_STATUSES:
            log.info(
                "[NOWPAYMENTS WEBHOOK] payment_id=%s status=%s (not paid) — ignoring",
                payment_id, status,
            )
            return jsonify({'ok': True}), 200

        # ------------------------------------------------------------------
        # Find the matching pending Transaction (three-tier search).
        # ------------------------------------------------------------------
        transaction = None

        # 1. Match by order_id → direct Transaction.id lookup (most reliable).
        if order_id:
            try:
                db_tx_id = int(order_id)
                with get_db_session() as _s:
                    from database.models import PaymentMethod as _PM
                    tx = _s.query(Transaction).filter(
                        Transaction.id == db_tx_id,
                        Transaction.status == TransactionStatus.PENDING,
                    ).first()
                    if tx and str(getattr(tx.payment_method, 'value', tx.payment_method)) == 'nowpayments':
                        transaction = tx
                        log.info("[NOWPAYMENTS PAYMENT MATCHED] by order_id=%s → tx.id=%s", order_id, tx.id)
            except (ValueError, Exception):
                log.debug("order_id '%s' is not a valid integer or DB error", order_id)

        # 2. Match by invoice_id → crypto_address prefix.
        if not transaction and invoice_id:
            transaction = _find_pending_transaction('nowpayments', invoice_id)
            if transaction:
                log.info(
                    "[NOWPAYMENTS PAYMENT MATCHED] by invoice_id=%s → tx.id=%s",
                    invoice_id, transaction.id,
                )

        # 3. Match by payment_id → crypto_address prefix (older rows).
        if not transaction and payment_id:
            transaction = _find_pending_transaction('nowpayments', payment_id)
            if transaction:
                log.info(
                    "[NOWPAYMENTS PAYMENT MATCHED] by payment_id=%s → tx.id=%s",
                    payment_id, transaction.id,
                )

        if not transaction:
            log.error(
                "[NOWPAYMENTS WEBHOOK] No pending transaction found for "
                "payment_id=%s order_id=%s invoice_id=%s",
                payment_id, order_id, invoice_id,
            )
            # Return 200 so NOWPayments doesn't keep retrying for unknown orders.
            return jsonify({'ok': True, 'note': 'no matching pending transaction'}), 200

        # ------------------------------------------------------------------
        # Credit wallet — idempotent, atomic.
        # ------------------------------------------------------------------
        # Store payment_id in txid for audit trail before crediting.
        if payment_id:
            try:
                with get_db_session() as _s:
                    _s.query(Transaction).filter(Transaction.id == transaction.id).update(
                        {Transaction.txid: payment_id},
                        synchronize_session=False,
                    )
                    _s.commit()
            except Exception:
                log.warning("Could not save payment_id to txid for tx=%s", transaction.id, exc_info=True)

        user = _credit_wallet_once(
            'nowpayments_webhook',
            f"payment:{payment_id or order_id}",
            transaction,
        )
        if user:
            log.info(
                "[NOWPAYMENTS BALANCE CREDITED] tx=%s user=%s amount=%.2f new_balance=%.2f",
                transaction.id, user.telegram_id, transaction.amount, user.wallet_balance,
            )
        return jsonify({'ok': True}), 200

    except Exception as exc:
        import logging as _log2
        _log2.getLogger(__name__).exception("NOWPayments webhook processing failed")
        return jsonify({'error': str(exc)}), 500


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with setup instructions."""
    return """
    <h1>CryptoBot Webhook Receiver</h1>
    <p>This server is running and ready to receive CryptoBot payment notifications.</p>

    <h2>Setup Instructions:</h2>
    <ol>
        <li>Go to <a href="https://t.me/CryptoBot">@CryptoBot</a> in Telegram</li>
        <li>Navigate to: Crypto Pay → My Apps → Select your app</li>
        <li>Tap "Webhooks..." and then "Enable Webhooks"</li>
        <li>Enter your webhook URL: <code>https://your-domain.com/webhook/cryptobot</code></li>
        <li>Save and start receiving real-time payment notifications!</li>
    </ol>

    <h2>Endpoints:</h2>
    <ul>
        <li><code>POST /webhook/cryptobot</code> - CryptoBot webhook endpoint</li>
        <li><code>GET/POST /webhook/bkash</code> - bKash checkout callback (set as callbackURL)</li>
        <li><code>GET/POST /webhook/nagad</code> - Nagad checkout callback (set as merchantCallbackURL)</li>
        <li><code>POST /webhook/cryptomus</code> - Cryptomus payment webhook (set as url_callback)</li>
        <li><code>POST /webhook/nowpayments</code> - NOWPayments IPN callback (set as ipn_callback_url)</li>
        <li><code>GET /health</code> - Health check</li>
    </ul>

    <p><strong>Note:</strong> For local testing, use ngrok to create a public HTTPS URL. Set
    WEBHOOK_URL in .env to that public HTTPS base — services/bkash_payment.py,
    services/nagad_payment.py and services/cryptomus_payment.py build the callback URLs from it.</p>
    """, 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("Payment Gateway Webhook Server")
    print("=" * 60)
    print(f"Server starting on http://0.0.0.0:{port}")
    print(f"Webhook endpoint: /webhook/cryptobot")
    print()
    print("For local testing with ngrok:")
    print("  1. Run: ngrok http " + str(port))
    print("  2. Copy the HTTPS URL (e.g., https://abc123.ngrok.io)")
    print("  3. Set webhook in CryptoBot to: https://abc123.ngrok.io/webhook/cryptobot")
    print()
    print("Waiting for webhooks...")
    print("=" * 60)

    # Run Flask server
    app.run(host='0.0.0.0', port=port, debug=False)
