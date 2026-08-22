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

from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import os
from datetime import datetime
from database.db import get_db_session
from database.models import Transaction, TransactionStatus, User, PaymentMethod
from config.settings import settings
from services import zinipay

app = Flask(__name__)


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

        print(f"📩 Webhook received: Invoice #{invoice_id}, status={status}, paid_at={paid_at}")

        if status != 'paid':
            print(f"⚠️ Invoice {invoice_id} not in 'paid' status, ignoring")
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
                print(f"❌ No pending transaction found for invoice {invoice_id}")
                return

            # Idempotency guard: atomically flip PENDING -> COMPLETED in a single
            # UPDATE. If another worker (the polling job, or a duplicate webhook
            # delivery) already completed this transaction, rowcount will be 0
            # and we skip crediting the wallet a second time.
            updated_rows = session.query(Transaction).filter(
                Transaction.id == transaction.id,
                Transaction.status == TransactionStatus.PENDING
            ).update({
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow()
            }, synchronize_session=False)

            if updated_rows == 0:
                print(f"⚠️ Transaction {transaction.id} already completed elsewhere, skipping credit")
                return

            # Get user
            user = session.query(User).filter_by(id=transaction.user_id).first()

            if not user:
                print(f"❌ User not found for transaction {transaction.id}")
                return

            # Add funds to user's wallet
            user.wallet_balance += transaction.amount

            # Session commits automatically on context manager exit
            print(f"✅ Payment processed via webhook!")
            print(f"   Transaction #{transaction.id}")
            print(f"   User: @{user.username}")
            print(f"   Amount: ${transaction.amount:.2f}")
            print(f"   New balance: ${user.wallet_balance:.2f}")

            # TODO: Send notification to user via bot
            # This requires access to the bot instance
            # For now, the user will see the updated balance when they check

    except Exception as e:
        print(f"❌ Error processing webhook: {e}")
        import traceback
        traceback.print_exc()


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
            print("❌ No signature in webhook request")
            return jsonify({'error': 'No signature'}), 401

        # Get raw request body
        body = request.get_data()

        # Verify signature
        if not verify_signature(body, signature):
            print("❌ Invalid webhook signature")
            return jsonify({'error': 'Invalid signature'}), 401

        # Parse JSON
        data = request.get_json()

        print(f"📩 CryptoBot Webhook received:")
        print(json.dumps(data, indent=2))

        # Extract update info
        update_type = data.get('update_type')
        request_date = data.get('request_date')
        payload = data.get('payload')

        # Check update type
        if update_type != 'invoice_paid':
            print(f"⚠️ Unknown update type: {update_type}")
            return jsonify({'ok': True}), 200

        # Process the paid invoice
        process_invoice_paid(payload)

        return jsonify({'ok': True}), 200

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/webhook/zinipay', methods=['GET', 'POST'])
def zinipay_webhook():
    """Webhook endpoint for ZiniPay (bKash/Nagad/Rocket) payment notifications.

    Per ZiniPay's own recommendation, the webhook body itself is never
    trusted as proof of payment — it only tells us which invoice_id to
    re-verify via ZiniPay's backend Verify Invoice API before crediting.
    ZiniPay can call this as JSON body or query params; both are accepted.
    """
    try:
        if request.method == 'POST' and request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.args.to_dict()

        invoice_id = data.get('invoice_id')
        if not invoice_id:
            print("❌ ZiniPay webhook missing invoice_id")
            return jsonify({'error': 'Missing invoice_id'}), 400

        print(f"📩 ZiniPay webhook received: invoice_id={invoice_id}")

        with get_db_session() as session:
            transactions = session.query(Transaction).filter(
                Transaction.payment_method == PaymentMethod.BKASH_NAGAD,
                Transaction.status == TransactionStatus.PENDING
            ).all()

            transaction = None
            for txn in transactions:
                if txn.crypto_address and txn.crypto_address.startswith(f"{invoice_id}|"):
                    transaction = txn
                    break

            if not transaction:
                print(f"❌ No pending transaction found for ZiniPay invoice {invoice_id}")
                return jsonify({'ok': True}), 200

            # Always re-verify from ZiniPay's backend before crediting — the
            # webhook payload itself is not trusted.
            if not zinipay.is_paid(invoice_id, transaction.amount):
                print(f"⚠️ ZiniPay verify did not confirm payment for invoice {invoice_id}")
                return jsonify({'ok': True}), 200

            # Idempotency guard: same atomic flip used for the CryptoBot
            # webhook above.
            updated_rows = session.query(Transaction).filter(
                Transaction.id == transaction.id,
                Transaction.status == TransactionStatus.PENDING
            ).update({
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow()
            }, synchronize_session=False)

            if updated_rows == 0:
                print(f"⚠️ Transaction {transaction.id} already completed elsewhere, skipping credit")
                return jsonify({'ok': True}), 200

            user = session.query(User).filter_by(id=transaction.user_id).first()
            if not user:
                print(f"❌ User not found for transaction {transaction.id}")
                return jsonify({'ok': True}), 200

            user.wallet_balance += transaction.amount

            print(f"✅ ZiniPay payment processed via webhook!")
            print(f"   Transaction #{transaction.id}")
            print(f"   User: @{user.username}")
            print(f"   Amount: ${transaction.amount:.2f}")
            print(f"   New balance: ${user.wallet_balance:.2f}")

        return jsonify({'ok': True}), 200

    except Exception as e:
        print(f"❌ ZiniPay webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/zinipay/success', methods=['GET'])
def zinipay_success():
    """Redirect target after a ZiniPay payment. Nothing to do here — the
    webhook/polling job credits the wallet; this page is just what the
    buyer sees in their browser before returning to Telegram."""
    return "<h2>✅ Payment received</h2><p>You can return to Telegram now — your balance will update shortly.</p>", 200


@app.route('/zinipay/cancel', methods=['GET'])
def zinipay_cancel():
    """Redirect target when a buyer cancels a ZiniPay payment."""
    return "<h2>❌ Payment cancelled</h2><p>You can return to Telegram and try again.</p>", 200


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'service': 'CryptoBot Webhook Receiver',
        'timestamp': datetime.utcnow().isoformat()
    }), 200


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
        <li><code>GET /health</code> - Health check</li>
    </ul>

    <p><strong>Note:</strong> For local testing, use ngrok to create a public HTTPS URL.</p>
    """, 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    print("=" * 60)
    print("CryptoBot Webhook Server")
    print("=" * 60)
    print(f"Server starting on http://0.0.0.0:{port}")
    print(f"Webhook endpoint: /webhook/cryptobot")
    print()
    print("For local testing with ngrok:")
    print("  1. Run: ngrok http 5000")
    print("  2. Copy the HTTPS URL (e.g., https://abc123.ngrok.io)")
    print("  3. Set webhook in CryptoBot to: https://abc123.ngrok.io/webhook/cryptobot")
    print()
    print("Waiting for webhooks...")
    print("=" * 60)

    # Run Flask server. In production (Railway) this file is instead run
    # via gunicorn, which does not execute this __main__ block — see
    # Procfile: `gunicorn webhook_server:app --bind 0.0.0.0:$PORT`.
    app.run(host='0.0.0.0', port=port, debug=False)
