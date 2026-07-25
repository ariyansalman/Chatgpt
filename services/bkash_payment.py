"""bKash Tokenized Checkout (Personal / Merchant API) payment service.

Mirrors the shape of ``services/crypto_bot.py`` so it plugs into the same
polling-based verification loop in ``handlers/payment_handlers.py``:

    address = bkash_service.create_payment(amount, transaction_id)   # "paymentID|bkashURL"
    paid    = bkash_service.check_payment_status(address, amount)    # bool

Credentials are resolved in this order (first match wins):
    1. Admin-set values in ``bot_config`` (set from the Telegram admin panel via
       handlers/admin_payment_methods.py -> "Payment Gateways" section).
    2. Environment variables (config/settings.py) — useful for first-time
       deployment before the admin has configured anything from the bot.

NOTE ON SECRETS: like the rest of this project (see ManualPaymentMethod.account_number),
credentials set from the admin panel are stored as plain text in the `bot_config` table
so the admin can manage them without touching the server. If you need stronger secrecy,
set the values via environment variables instead and leave the bot_config fields blank —
env values are only used as a fallback when nothing is configured in bot_config.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

# bKash Tokenized Checkout base URLs (v1.2.0-beta)
SANDBOX_BASE_URL = "https://tokenized.sandbox.bka.sh/v1.2.0-beta"
LIVE_BASE_URL = "https://tokenized.pay.bka.sh/v1.2.0-beta"

# In-process token cache, keyed by app_key, shared across instances so we
# don't re-authenticate on every request. Not persisted across restarts.
_token_cache: dict = {}


def _cfg():
    """Lazy import to avoid a hard dependency at module import time
    (bot_config touches the DB, which may not be initialized yet in some
    tooling / test contexts)."""
    try:
        from utils.bot_config import cfg
        return cfg
    except Exception:
        return None


class BkashPaymentService:
    """Service for creating and verifying bKash Tokenized Checkout payments."""

    SOURCE = "bkash"  # used for payment_idempotency rows

    def __init__(self):
        cfg = _cfg()

        def _get(key: str, env_default: str) -> str:
            if cfg is not None:
                val = cfg.get_str(key, "")
                if val:
                    return val
            return env_default

        self.enabled = cfg.get_bool("bkash_enabled", False) if cfg else False
        self.mode = _get("bkash_mode", settings.BKASH_MODE or "sandbox").strip().lower()
        self.app_key = _get("bkash_app_key", settings.BKASH_APP_KEY)
        self.app_secret = _get("bkash_app_secret", settings.BKASH_APP_SECRET)
        self.username = _get("bkash_username", settings.BKASH_USERNAME)
        self.password = _get("bkash_password", settings.BKASH_PASSWORD)
        self.base_url = LIVE_BASE_URL if self.mode == "live" else SANDBOX_BASE_URL
        # Where the bKash checkout page redirects back to after payment.
        # Only meaningful if webhook_server.py is deployed publicly.
        self.callback_base_url = (settings.WEBHOOK_URL or "").rstrip("/")

    # ------------------------------------------------------------------
    # Credential / configuration helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.username and self.password)

    def _headers(self, id_token: Optional[str] = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if id_token:
            headers["Authorization"] = id_token
            headers["X-App-Key"] = self.app_key
        return headers

    def _grant_token(self) -> Optional[str]:
        """Get (and cache) a bKash id_token via the grant-token endpoint."""
        cached = _token_cache.get(self.app_key)
        if cached and cached["expires_at"] > time.time() + 30:
            return cached["id_token"]

        try:
            resp = requests.post(
                f"{self.base_url}/tokenized/checkout/token/grant",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "username": self.username,
                    "password": self.password,
                },
                json={"app_key": self.app_key, "app_secret": self.app_secret},
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code != 200 or not data.get("id_token"):
                logger.error("bKash grant token failed: %s - %s", resp.status_code, data)
                return None

            _token_cache[self.app_key] = {
                "id_token": data["id_token"],
                "refresh_token": data.get("refresh_token"),
                # expires_in is in seconds; be conservative if missing
                "expires_at": time.time() + int(data.get("expires_in", 3300)),
            }
            return data["id_token"]
        except Exception:
            logger.exception("Error requesting bKash grant token")
            return None

    # ------------------------------------------------------------------
    # Public API (matches CryptoBotService's shape)
    # ------------------------------------------------------------------

    def create_payment(self, amount: float, transaction_id: int) -> Optional[str]:
        """Create a bKash checkout payment.

        Returns "paymentID|bkashURL" (same "id|url" convention as
        services/crypto_bot.py) or None on failure.
        """
        if not self.is_configured():
            logger.warning("bKash not configured (missing app_key/app_secret/username/password)")
            return f"SAMPLE_{transaction_id}|https://sandbox.bka.sh/sample_checkout_{transaction_id}"

        id_token = self._grant_token()
        if not id_token:
            return None

        callback_url = f"{self.callback_base_url}/webhook/bkash" if self.callback_base_url else "https://example.com/webhook/bkash"

        try:
            resp = requests.post(
                f"{self.base_url}/tokenized/checkout/create",
                headers=self._headers(id_token),
                json={
                    "mode": "0011",
                    "payerReference": str(transaction_id),
                    "callbackURL": callback_url,
                    "merchantAssociationInfo": "topup",
                    "amount": f"{amount:.2f}",
                    "currency": "BDT",
                    "intent": "sale",
                    "merchantInvoiceNumber": f"TOPUP{transaction_id}",
                },
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code not in (200, 201) or not data.get("paymentID"):
                logger.error("bKash create payment failed: %s - %s", resp.status_code, data)
                return None

            payment_id = data["paymentID"]
            bkash_url = data.get("bkashURL", "")
            if not bkash_url:
                return None
            return f"{payment_id}|{bkash_url}"
        except Exception:
            logger.exception("Error creating bKash payment")
            return None

    def execute_payment(self, payment_id: str) -> Optional[dict]:
        """Finalize ("execute") a payment after the user completes it on bKash's page.

        Returns the execute response dict (contains ``transactionStatus``,
        ``trxID`` ...) or None on failure. Safe to call more than once —
        bKash returns the existing completed payment info on repeat calls.
        """
        if "SAMPLE_" in str(payment_id):
            return None

        id_token = self._grant_token()
        if not id_token:
            return None

        try:
            resp = requests.post(
                f"{self.base_url}/tokenized/checkout/execute/{payment_id}",
                headers=self._headers(id_token),
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            return data
        except Exception:
            logger.exception("Error executing bKash payment %s", payment_id)
            return None

    def query_payment_status(self, payment_id: str) -> Optional[dict]:
        """Query current status of a payment without mutating it."""
        if "SAMPLE_" in str(payment_id):
            return None

        id_token = self._grant_token()
        if not id_token:
            return None

        try:
            resp = requests.post(
                f"{self.base_url}/tokenized/checkout/payment/status",
                headers=self._headers(id_token),
                json={"paymentID": payment_id},
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            return data
        except Exception:
            logger.exception("Error querying bKash payment status %s", payment_id)
            return None

    def check_payment_status(self, crypto_address: str, expected_amount: float) -> bool:
        """Polling fallback used by handlers.payment_handlers.check_pending_payments.

        Prefer the /webhook/bkash callback route for instant confirmation;
        this polling path exists so payments still complete even if the
        callback never arrives (browser closed, etc.).
        """
        if not crypto_address:
            return False

        payment_id = crypto_address.split("|", 1)[0]
        if not payment_id or "SAMPLE_" in payment_id:
            return False

        status_data = self.query_payment_status(payment_id)
        status = (status_data or {}).get("transactionStatus") or (status_data or {}).get("statusCode")

        if status == "Completed":
            return True

        # Some accounts only flip to Completed once /execute is called
        # (the checkout redirect normally triggers this via the webhook route).
        result = self.execute_payment(payment_id)
        if not result:
            return False
        exec_status = result.get("transactionStatus")
        if exec_status == "Completed":
            try:
                paid_amount = float(result.get("amount", expected_amount))
            except (TypeError, ValueError):
                paid_amount = expected_amount
            # Allow tiny float rounding differences.
            return abs(paid_amount - expected_amount) < 0.01
        return False
