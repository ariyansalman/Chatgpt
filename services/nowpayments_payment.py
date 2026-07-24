"""NOWPayments payment gateway service.

NOWPayments (https://nowpayments.io) is a non-custodial crypto payment
processor.  This mirrors the shape of ``services/cryptomus_payment.py`` so it
plugs into the same conventions in ``handlers/payment_handlers.py``:

    reference = nowpayments_service.create_payment(amount, transaction_id)
    # reference is "invoice_id|invoice_url" (or "invoice_id|" when URL absent)

Credentials resolved from (first match wins):
    1. PaymentGatewayConfig row (gateway="nowpayments") — api_key = API key,
       secondary_key = IPN secret.
    2. Environment variables NOWPAYMENTS_API_KEY / NOWPAYMENTS_IPN_SECRET.

NOWPayments API used here:
    POST /v1/invoice  → create an invoice (returns id + invoice_url)
    GET  /v1/payment  → list payments, filtered by invoiceId, to get payment_id
    GET  /v1/payment/{payment_id} → check payment status
    IPN (webhook): HMAC-SHA512 over alphabetically sorted JSON body,
                   sent in header "x-nowpayments-sig".

Key IDs (important — they differ):
    invoice_id   — returned by POST /v1/invoice as "id";
                   stored in crypto_address (first segment before "|").
    payment_id   — returned by GET /v1/payment when the user pays;
                   also sent in every IPN callback.
    order_id     — set by us to str(transaction.id); present in every IPN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.nowpayments.io/v1"

# Statuses that NOWPayments considers "paid" / "successful".
PAID_STATUSES = {"finished", "confirmed", "partially_paid"}

# Candidate field names for the payment/checkout URL, tried in order.
_URL_FIELDS = ["invoice_url", "payment_url", "pay_url", "payment_link", "hosted_url"]


def _gw_cfg():
    """Lazy import — avoids a hard DB dependency at module import time."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        return get_db_session, PaymentGatewayConfig
    except Exception:
        return None, None


def _get_or_create_config(session, PaymentGatewayConfig):
    row = session.query(PaymentGatewayConfig).filter_by(gateway="nowpayments").first()
    if not row:
        row = PaymentGatewayConfig(gateway="nowpayments", is_enabled=False)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _extract_url(data: dict) -> Optional[str]:
    """Try every known URL field name and return the first valid http(s) value."""
    for field in _URL_FIELDS:
        val = data.get(field)
        if val and isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    return None


class NowPaymentsService:
    """Service for creating and verifying NOWPayments payments."""

    SOURCE = "nowpayments"

    def __init__(self):
        get_db_session, PaymentGatewayConfig = _gw_cfg()

        api_key = ""
        ipn_secret = ""
        enabled = False

        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    api_key = row.api_key or ""
                    ipn_secret = row.secondary_key or ""
                    enabled = bool(row.is_enabled)
            except Exception:
                logger.exception("Failed to load NOWPayments config from PaymentGatewayConfig")

        self.api_key = api_key or (getattr(settings, "NOWPAYMENTS_API_KEY", "") or "")
        self.ipn_secret = ipn_secret or (getattr(settings, "NOWPAYMENTS_IPN_SECRET", "") or "")
        self.enabled = enabled
        self.callback_base_url = (getattr(settings, "WEBHOOK_URL", "") or "").rstrip("/")
        self.last_error = ""

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "x-api-key": self.api_key}

    # ------------------------------------------------------------------
    # Payment creation
    # ------------------------------------------------------------------

    def create_payment(
        self,
        amount: float,
        transaction_id: int,
        callback_url: Optional[str] = None,
        return_url: Optional[str] = None,
    ) -> Optional[str]:
        """Create a NOWPayments invoice.

        Returns "invoice_id|invoice_url" on success, or "invoice_id|" when the
        API doesn't return a usable URL (the IPN + polling path still works via
        invoice_id).  Returns None on complete failure.

        Stores the invoice_id (not payment_id) as the first segment so that
        the background polling job can look up payments by invoiceId.
        """
        if not self.is_configured():
            self.last_error = "NOWPayments API key is not set."
            logger.warning("NOWPayments not configured (missing api_key)")
            return None

        callback_url = callback_url or (
            f"{self.callback_base_url}/webhook/nowpayments"
            if self.callback_base_url
            else None
        )
        payload = {
            "price_amount": round(amount, 2),
            "price_currency": "usd",
            "order_id": str(transaction_id),
            "order_description": f"Wallet top-up #{transaction_id}",
        }
        if callback_url:
            payload["ipn_callback_url"] = callback_url
        if return_url:
            payload["success_url"] = return_url

        try:
            resp = requests.post(
                f"{API_BASE_URL}/invoice",
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
            data = resp.json() if resp.content else {}

            # ---- NOWPAYMENTS REQUEST/RESPONSE (debug) ----
            logger.info(
                "[NOWPAYMENTS REQUEST] POST /invoice payload=%s | "
                "[NOWPAYMENTS RESPONSE] status=%s body=%s",
                payload, resp.status_code, data,
            )

            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}: {data.get('message') or data}"
                logger.error(
                    "NOWPayments create invoice failed: status=%s body=%s",
                    resp.status_code, data,
                )
                return None

            invoice_id = data.get("id")
            if not invoice_id:
                self.last_error = f"Response missing 'id': {data}"
                logger.error("NOWPayments invoice response has no 'id': %s", data)
                return None

            invoice_url = _extract_url(data)
            if not invoice_url:
                # Log all field names so we can see what the API actually returned.
                logger.warning(
                    "NOWPayments invoice created (id=%s) but no usable URL found. "
                    "Fields returned: %s",
                    invoice_id, list(data.keys()),
                )
                # Still return a reference so the transaction isn't marked FAILED.
                # The polling path will pick up status from the API.
                return f"{invoice_id}|"

            return f"{invoice_id}|{invoice_url}"

        except Exception as exc:
            self.last_error = f"Request error: {exc}"
            logger.exception("Error creating NOWPayments invoice")
            return None

    # ------------------------------------------------------------------
    # Payment / invoice status
    # ------------------------------------------------------------------

    def get_payments_for_invoice(self, invoice_id: str) -> list:
        """GET /v1/payment?invoiceId={invoice_id} — list payments linked to
        an invoice.  Returns a list of payment objects (may be empty).

        NOWPayments supports both camelCase (invoiceId) and a flat list
        response shape, so we try both and normalise the result.
        """
        if not self.is_configured() or not invoice_id:
            return []
        try:
            resp = requests.get(
                f"{API_BASE_URL}/payment",
                headers=self._headers(),
                params={"invoiceId": invoice_id},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug(
                    "NOWPayments payment list for invoice %s: status=%s body=%s",
                    invoice_id, resp.status_code, resp.text[:200],
                )
                return []
            data = resp.json() if resp.content else {}
            # The API can return several shapes; handle all known ones:
            #   • A raw list: [{"payment_id": ..., ...}, ...]
            #   • {"data": [...]}: top-level list under "data"
            #   • {"data": {"items": [...]}}: nested items list
            if isinstance(data, list):
                return data
            items = data.get("data", {})
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                return items.get("items", [])
            return []
        except Exception:
            logger.exception("Error listing NOWPayments payments for invoice %s", invoice_id)
            return []

    def get_invoice_info(self, invoice_id: str) -> Optional[dict]:
        """GET /v1/invoice/{invoice_id} — fetch the invoice object directly.

        Used as a secondary status check when the payments-by-invoiceId lookup
        returns an empty list (e.g. the NOWPayments API didn't accept the
        invoiceId filter).  The invoice object itself carries a payment_status
        field once the customer has paid.
        """
        if not self.is_configured() or not invoice_id:
            return None
        try:
            resp = requests.get(
                f"{API_BASE_URL}/invoice/{invoice_id}",
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug(
                    "NOWPayments get_invoice(%s): status=%s body=%s",
                    invoice_id, resp.status_code, resp.text[:200],
                )
                return None
            return resp.json() if resp.content else None
        except Exception:
            logger.exception("Error querying NOWPayments invoice %s", invoice_id)
            return None

    def get_payment_info(self, payment_id: str) -> Optional[dict]:
        """GET /v1/payment/{payment_id} — fetch a single payment by its ID.

        NOTE: This method must only be called with a real payment_id (the
        numeric string returned by NOWPayments when the customer initiates a
        payment).  Do NOT pass an invoice_id here — they are different IDs.
        """
        if not self.is_configured() or not payment_id:
            return None
        try:
            resp = requests.get(
                f"{API_BASE_URL}/payment/{payment_id}",
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(
                    "NOWPayments get_payment_info(%s): status=%s body=%s",
                    payment_id, resp.status_code, resp.text[:200],
                )
                return None
            return resp.json() if resp.content else None
        except Exception:
            logger.exception("Error querying NOWPayments payment %s", payment_id)
            return None

    def check_payment_status(
        self, gateway_ref: str, expected_amount: Optional[float] = None
    ) -> bool:
        """Polling fallback for check_pending_payments.

        ``gateway_ref`` is the stored "invoice_id|invoice_url" value from
        ``crypto_address``.  We use a three-tier lookup so that a transient
        API quirk in any one path doesn't silently drop a completed payment:

        1. GET /v1/payment?invoiceId={invoice_id}
           — list all payments linked to this invoice and check their statuses.
           This is the primary path recommended by NOWPayments for invoice-based
           flows.

        2. GET /v1/invoice/{invoice_id}
           — fetch the invoice object directly. The invoice carries a
           payment_status field once the customer pays. Used when path 1
           returns an empty list (e.g. the API didn't honour the invoiceId
           filter, or the payment_id hasn't been attached yet).

        3. (Legacy) If gateway_ref looks like a bare payment_id (no "|" and
           all-digits), try GET /v1/payment/{gateway_ref} directly. Covers
           old transactions that stored payment_id in crypto_address before
           the invoice-based flow was introduced.

        IMPORTANT: Do NOT fall back from (1) by passing invoice_id to
        get_payment_info — invoice_id ≠ payment_id and that call will 404.
        """
        if not gateway_ref:
            return False

        # Split "invoice_id|invoice_url" — first segment is always invoice_id.
        invoice_id = gateway_ref.split("|", 1)[0].strip()
        if not invoice_id:
            return False

        logger.debug("[NOWPAYMENTS POLL] checking gateway_ref=%s invoice_id=%s",
                     gateway_ref[:60], invoice_id)

        # ── Path 1: payments linked to the invoice ──────────────────────────
        payments = self.get_payments_for_invoice(invoice_id)
        if payments:
            for p in payments:
                status = str(p.get("payment_status") or "").lower()
                logger.debug("[NOWPAYMENTS POLL] payment_id=%s status=%s",
                             p.get("payment_id"), status)
                if status in PAID_STATUSES:
                    return True
            # Payments exist but none are paid yet.
            return False

        # ── Path 2: invoice object itself ────────────────────────────────────
        invoice_info = self.get_invoice_info(invoice_id)
        if invoice_info:
            for field in ("payment_status", "status"):
                val = str(invoice_info.get(field) or "").lower()
                if val:
                    logger.debug("[NOWPAYMENTS POLL] invoice %s field %s=%s",
                                 invoice_id, field, val)
                if val in PAID_STATUSES:
                    return True

        # ── Path 3: legacy — gateway_ref stored as a bare payment_id ────────
        # Only attempt if the value looks like a numeric payment_id (no "|").
        if "|" not in gateway_ref and gateway_ref.strip().isdigit():
            info = self.get_payment_info(gateway_ref.strip())
            if info:
                status = str(info.get("payment_status") or "").lower()
                logger.debug("[NOWPAYMENTS POLL] legacy payment_id=%s status=%s",
                             gateway_ref.strip(), status)
                if status in PAID_STATUSES:
                    return True

        return False

    # ------------------------------------------------------------------
    # Webhook (IPN) signature verification
    # ------------------------------------------------------------------

    def verify_webhook_signature(self, payload_dict: dict, received_sig: str) -> bool:
        """Verify a NOWPayments IPN "x-nowpayments-sig" header.

        Per NOWPayments docs: sort the body's keys alphabetically (recursively)
        before computing HMAC-SHA512 with the IPN secret.
        """
        if not received_sig or not self.ipn_secret:
            return False
        sorted_body = json.dumps(_sort_dict(payload_dict), separators=(",", ":"))
        expected = hmac.new(
            self.ipn_secret.encode("utf-8"),
            sorted_body.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, received_sig.lower())


def _sort_dict(obj):
    """Recursively sort dict keys — mirrors NOWPayments' sortObject() helper."""
    if isinstance(obj, dict):
        return {k: _sort_dict(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_sort_dict(v) for v in obj]
    return obj
