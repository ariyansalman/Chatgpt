"""Nagad Merchant Checkout (Remote Payment Gateway) payment service.

Nagad's checkout flow is a 2-step signed/encrypted handshake:

    1. initialize  -> server returns a `challenge` + `paymentReferenceId`
    2. complete    -> server returns a `callBackUrl` the user is redirected to

After the user finishes paying on Nagad's page, Nagad redirects the user's
browser to our callback URL with `payment_ref_id` + `status` query params
(handled in webhook_server.py), and the payment is confirmed via the
`verify` endpoint. Polling (`check_payment_status`, used by
handlers.payment_handlers.check_pending_payments) also calls `verify`, so a
payment still completes even if the callback redirect is missed.

Mirrors the shape of services/crypto_bot.py / services/bkash_payment.py:

    address = nagad_service.create_payment(amount, transaction_id)  # "paymentRefId|callBackUrl"
    paid    = nagad_service.check_payment_status(address, amount)   # bool

See the security note in services/bkash_payment.py — the same applies here:
credentials set from the admin panel are stored as plain text in `bot_config`.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

SANDBOX_BASE_URL = "http://sandbox.mynagad.com:10080/remote-payment-gateway-1.0/api/dfs"
LIVE_BASE_URL = "https://api.mynagad.com/api/dfs"


def _cfg():
    try:
        from utils.bot_config import cfg
        return cfg
    except Exception:
        return None


def _normalize_pem(raw: str, kind: str) -> str:
    """Accept either a full PEM block or just the base64 body and return a
    proper PEM string, so admins can paste keys either way from the bot."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "BEGIN" in raw:
        return raw
    header = "PUBLIC KEY" if kind == "public" else "PRIVATE KEY"
    body = "\n".join(raw[i:i + 64] for i in range(0, len(raw), 64))
    return f"-----BEGIN {header}-----\n{body}\n-----END {header}-----"


class NagadPaymentService:
    """Service for creating and verifying Nagad checkout payments."""

    SOURCE = "nagad"  # used for payment_idempotency rows

    def __init__(self):
        cfg = _cfg()

        def _get(key: str, env_default: str) -> str:
            if cfg is not None:
                val = cfg.get_str(key, "")
                if val:
                    return val
            return env_default

        self.enabled = cfg.get_bool("nagad_enabled", False) if cfg else False
        self.mode = _get("nagad_mode", settings.NAGAD_MODE or "sandbox").strip().lower()
        self.merchant_id = _get("nagad_merchant_id", settings.NAGAD_MERCHANT_ID)
        self.merchant_number = _get("nagad_merchant_number", settings.NAGAD_MERCHANT_NUMBER)
        self.public_key_pem = _normalize_pem(
            _get("nagad_public_key", settings.NAGAD_PUBLIC_KEY), "public"
        )
        self.private_key_pem = _normalize_pem(
            _get("nagad_private_key", settings.NAGAD_PRIVATE_KEY), "private"
        )
        self.base_url = LIVE_BASE_URL if self.mode == "live" else SANDBOX_BASE_URL
        self.callback_base_url = (settings.WEBHOOK_URL or "").rstrip("/")

    def is_configured(self) -> bool:
        return bool(
            self.merchant_id and self.merchant_number
            and self.public_key_pem and self.private_key_pem
        )

    # ------------------------------------------------------------------
    # Crypto helpers
    # ------------------------------------------------------------------

    def _load_keys(self):
        from cryptography.hazmat.primitives import serialization
        public_key = serialization.load_pem_public_key(self.public_key_pem.encode())
        private_key = serialization.load_pem_private_key(
            self.private_key_pem.encode(), password=None
        )
        return public_key, private_key

    def _encrypt(self, plain_text: str, public_key) -> str:
        from cryptography.hazmat.primitives.asymmetric import padding
        encrypted = public_key.encrypt(plain_text.encode(), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    def _sign(self, plain_text: str, private_key) -> str:
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes
        signature = private_key.sign(plain_text.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def _headers(self, client_ip: str = "127.0.0.1") -> dict:
        return {
            "Content-Type": "application/json",
            "X-KM-Api-Version": "v-0.2.0",
            "X-KM-IP-V4": client_ip,
            "X-KM-Client-Type": "PC_WEB",
        }

    # ------------------------------------------------------------------
    # Public API (matches CryptoBotService's shape)
    # ------------------------------------------------------------------

    def create_payment(self, amount: float, transaction_id: int) -> Optional[str]:
        """Initialize + complete a Nagad checkout in one call.

        Returns "paymentReferenceId|callBackUrl" (open callBackUrl in a
        browser for the user to complete payment) or None on failure.
        """
        if not self.is_configured():
            logger.warning("Nagad not configured (missing merchant_id/keys)")
            return f"SAMPLE_{transaction_id}|https://sandbox.mynagad.com/sample_checkout_{transaction_id}"

        order_id = f"TOPUP{transaction_id}-{uuid.uuid4().hex[:8]}"
        try:
            public_key, private_key = self._load_keys()
        except Exception:
            logger.exception("Failed to load Nagad RSA keys — check bot_config nagad_public_key/nagad_private_key")
            return None

        # ---- Step 1: initialize ----
        dt = datetime.now().strftime("%Y%m%d%H%M%S")
        sensitive_data = f'{{"merchantId":"{self.merchant_id}","datetime":"{dt}","orderId":"{order_id}","challenge":"{uuid.uuid4().hex}"}}'
        try:
            resp = requests.post(
                f"{self.base_url}/check-out/initialize/{self.merchant_id}/{order_id}",
                headers=self._headers(),
                json={
                    "accountNumber": self.merchant_number,
                    "dateTime": dt,
                    "sensitiveData": self._encrypt(sensitive_data, public_key),
                    "signature": self._sign(sensitive_data, private_key),
                },
                timeout=15,
            )
            init_data = resp.json() if resp.content else {}
            if resp.status_code != 200 or not init_data.get("paymentReferenceId"):
                logger.error("Nagad initialize failed: %s - %s", resp.status_code, init_data)
                return None
            payment_ref_id = init_data["paymentReferenceId"]
            challenge = init_data.get("challenge", "")
        except Exception:
            logger.exception("Error initializing Nagad payment")
            return None

        # ---- Step 2: complete ----
        callback_url = f"{self.callback_base_url}/webhook/nagad" if self.callback_base_url else "https://example.com/webhook/nagad"
        complete_data = (
            f'{{"merchantId":"{self.merchant_id}","orderId":"{order_id}",'
            f'"currencyCode":"050","amount":"{amount:.2f}","challenge":"{challenge}"}}'
        )
        try:
            resp = requests.post(
                f"{self.base_url}/check-out/complete/{payment_ref_id}",
                headers=self._headers(),
                json={
                    "sensitiveData": self._encrypt(complete_data, public_key),
                    "signature": self._sign(complete_data, private_key),
                    "merchantCallbackURL": callback_url,
                },
                timeout=15,
            )
            complete_resp = resp.json() if resp.content else {}
            callback = complete_resp.get("callBackUrl")
            if resp.status_code != 200 or not callback:
                logger.error("Nagad complete failed: %s - %s", resp.status_code, complete_resp)
                return None
            return f"{payment_ref_id}|{callback}"
        except Exception:
            logger.exception("Error completing Nagad initialization")
            return None

    def verify_payment(self, payment_ref_id: str) -> Optional[dict]:
        """Verify a payment's final status by paymentReferenceId."""
        if "SAMPLE_" in str(payment_ref_id):
            return None
        try:
            resp = requests.get(
                f"{self.base_url}/verify/payment/{payment_ref_id}",
                headers=self._headers(),
                timeout=15,
            )
            return resp.json() if resp.content else {}
        except Exception:
            logger.exception("Error verifying Nagad payment %s", payment_ref_id)
            return None

    def check_payment_status(self, crypto_address: str, expected_amount: float) -> bool:
        """Polling fallback used by handlers.payment_handlers.check_pending_payments."""
        if not crypto_address:
            return False

        payment_ref_id = crypto_address.split("|", 1)[0]
        if not payment_ref_id or "SAMPLE_" in payment_ref_id:
            return False

        result = self.verify_payment(payment_ref_id)
        if not result:
            return False

        status = result.get("status")
        if status == "Success":
            try:
                paid_amount = float(result.get("amount", expected_amount))
            except (TypeError, ValueError):
                paid_amount = expected_amount
            return abs(paid_amount - expected_amount) < 0.01
        return False
