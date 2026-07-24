"""Cryptomus payment gateway service.

Cryptomus (https://cryptomus.com) is used here instead of @CryptoBot because
@CryptoBot isn't usable from Bangladesh. Mirrors the shape of
``services/bkash_payment.py`` / ``services/crypto_bot.py`` so it plugs into
the same conventions in ``handlers/payment_handlers.py``:

    reference = cryptomus_service.create_payment(amount, transaction_id)  # "uuid|payment_url"
    paid      = cryptomus_service.check_payment_status(uuid)              # bool

Credentials (Merchant UUID + Payment API Key) are resolved in this order
(first match wins):
    1. The dedicated "cryptomus" row in ``PaymentGatewayConfig`` (set from
       the Telegram admin panel — see handlers/admin_cryptomus.py). Unlike
       bKash/Nagad (which use loose key/value pairs in ``bot_config``),
       Cryptomus only needs two fields, so it gets its own typed columns —
       same pattern as Telegram Stars (services/telegram_stars.py).
    2. Environment variables CRYPTOMUS_MERCHANT_UUID / CRYPTOMUS_API_KEY
       (config/settings.py) — useful for first-time deployment before the
       admin has configured anything from the bot.

Cryptomus API reference used here:
    - Auth: every request body is signed with
          sign = md5(base64(json_payload) + api_key)
      sent in the "sign" header, alongside "merchant" (merchant UUID).
    - Create invoice: POST https://api.cryptomus.com/v1/payment
          body: {amount, currency="USD", order_id, url_callback, url_return, lifetime}
          -> result.uuid, result.url
    - Check status:   POST https://api.cryptomus.com/v1/payment/info
          body: {uuid} or {order_id}
          -> result.status: paid | paid_over | wrong_amount | cancel | process | confirm_check ...
    - Webhook: Cryptomus POSTs the same payload shape to url_callback, with
      its own "sign" field inside the JSON body (verified the same way).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.cryptomus.com/v1"

# Statuses that mean "money received" per Cryptomus docs.
PAID_STATUSES = {"paid", "paid_over"}


def _gw_cfg():
    """Lazy import — avoids a hard DB dependency at module import time."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        return get_db_session, PaymentGatewayConfig
    except Exception:
        return None, None


def _get_or_create_config(session, PaymentGatewayConfig):
    row = session.query(PaymentGatewayConfig).filter_by(gateway="cryptomus").first()
    if not row:
        row = PaymentGatewayConfig(gateway="cryptomus", is_enabled=False)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


class CryptomusPaymentService:
    """Service for creating and verifying Cryptomus payments."""

    SOURCE = "cryptomus"  # used for payment_idempotency rows

    def __init__(self):
        get_db_session, PaymentGatewayConfig = _gw_cfg()

        merchant_uuid = ""
        api_key = ""
        enabled = False

        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    merchant_uuid = row.merchant_uuid or ""
                    api_key = row.api_key or ""
                    enabled = bool(row.is_enabled)
            except Exception:
                logger.exception("Failed to load Cryptomus config from PaymentGatewayConfig")

        # .env fallback — only used if nothing is set in the admin panel yet.
        self.merchant_uuid = merchant_uuid or (getattr(settings, "CRYPTOMUS_MERCHANT_UUID", "") or "")
        self.api_key = api_key or (getattr(settings, "CRYPTOMUS_API_KEY", "") or "")
        self.enabled = enabled

        self.callback_base_url = (settings.WEBHOOK_URL or "").rstrip("/")

    # ------------------------------------------------------------------
    # Credential / signing helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.merchant_uuid and self.api_key)

    def _sign(self, payload: dict) -> str:
        """sign = md5(base64_encode(json_payload) + api_key)

        NOTE: Cryptomus requires the base64 step to run over the EXACT bytes
        it will also receive back in a webhook re-check, so we use compact,
        stable JSON (no extra whitespace, keys in insertion order) rather
        than re-serializing with different formatting later.
        """
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        b64 = base64.b64encode(raw)
        # MD5 is mandated by Cryptomus's signature protocol (not used for
        # password/credential hashing); usedforsecurity=False documents
        # intent and avoids tripping generic "weak hash" scanners.
        return hashlib.md5(b64 + self.api_key.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _headers(self, payload: dict) -> dict:
        return {
            "Content-Type": "application/json",
            "merchant": self.merchant_uuid,
            "sign": self._sign(payload),
        }

    # ------------------------------------------------------------------
    # Public API (matches BkashPaymentService's shape)
    # ------------------------------------------------------------------

    def create_payment(
        self, amount: float, transaction_id: int,
        callback_url: Optional[str] = None, return_url: Optional[str] = None,
    ) -> Optional[str]:
        """Create a Cryptomus invoice.

        Returns "uuid|payment_url" (same "id|url" convention as
        services/crypto_bot.py / services/bkash_payment.py) or None on failure.
        """
        if not self.is_configured():
            logger.warning("Cryptomus not configured (missing merchant_uuid/api_key)")
            return None

        callback_url = callback_url or (
            f"{self.callback_base_url}/webhook/cryptomus" if self.callback_base_url else None
        )
        payload = {
            "amount": f"{amount:.2f}",
            "currency": "USD",
            "order_id": str(transaction_id),
            "lifetime": "1800",  # 30 minutes, matches PAYMENT_EXPIRY_HOURS default
        }
        if callback_url:
            payload["url_callback"] = callback_url
        if return_url:
            payload["url_return"] = return_url

        try:
            resp = requests.post(
                f"{API_BASE_URL}/payment",
                headers=self._headers(payload),
                json=payload,
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            result = data.get("result") or {}
            if resp.status_code != 200 or not result.get("uuid") or not result.get("url"):
                logger.error("Cryptomus create payment failed: %s - %s", resp.status_code, data)
                return None
            return f"{result['uuid']}|{result['url']}"
        except Exception:
            logger.exception("Error creating Cryptomus payment")
            return None

    def get_payment_info(self, uuid: str) -> Optional[dict]:
        """Query current status of a payment (POST /payment/info)."""
        if not self.is_configured() or not uuid:
            return None

        payload = {"uuid": uuid}
        try:
            resp = requests.post(
                f"{API_BASE_URL}/payment/info",
                headers=self._headers(payload),
                json=payload,
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            return data.get("result")
        except Exception:
            logger.exception("Error querying Cryptomus payment status %s", uuid)
            return None

    def check_payment_status(self, crypto_address: str, expected_amount: Optional[float] = None) -> bool:
        """Polling fallback used by handlers.payment_handlers.check_pending_payments.

        Prefer the /webhook/cryptomus callback route for instant confirmation;
        this polling path exists so payments still complete even if the
        webhook never arrives.

        ``crypto_address`` is the stored "uuid|payment_url" reference (same
        convention as bKash/Nagad/CryptoBot); only the uuid half is used.
        """
        if not crypto_address:
            return False
        uuid = crypto_address.split("|", 1)[0]
        if not uuid:
            return False

        info = self.get_payment_info(uuid)
        if not info:
            return False
        status = info.get("status")
        return status in PAID_STATUSES

    # ------------------------------------------------------------------
    # Webhook signature verification
    # ------------------------------------------------------------------

    def verify_webhook_signature(self, payload_dict: dict, received_sign: str) -> bool:
        """Verify a Cryptomus webhook's "sign" field.

        Cryptomus includes "sign" INSIDE the JSON body itself; it must be
        removed from the payload before recomputing the signature (it isn't
        part of what was originally signed).
        """
        if not received_sign or not self.api_key:
            return False
        body = {k: v for k, v in payload_dict.items() if k != "sign"}
        expected = self._sign(body)
        return expected == received_sign
