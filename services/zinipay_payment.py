"""ZiniPay Transaction Verification Service.

New API (v1/trx): POST /verify + POST /confirm
Base URL: https://api.zinipay.com/v1/trx
Auth header: zinipay-api-key: <API_KEY>

User Flow:
    Deposit → Choose ZiniPay → Enter Amount → Bot shows payment instructions
    → User makes payment → Bot asks: "Please send your Transaction ID (TXID)."
    → verify_transaction() → confirm_transaction() → Credit wallet

Security:
    - API key never exposed — read from DB (PaymentGatewayConfig.api_key,
      gateway="zinipay") or env var ZINIPAY_API_KEY.
    - Duplicate TXID prevention: trxID stored in ZiniPayUsedTransaction
      (UNIQUE constraint) so the same transaction can never be credited twice.
    - Wallet is credited ONLY after confirm succeeds.

Credentials resolved from (first match wins):
    1. PaymentGatewayConfig row (gateway="zinipay"), api_key column.
    2. ZINIPAY_API_KEY environment variable.

Old endpoints removed:
    /v1/payment/create — removed
    /v1/payment/verify — removed
    All legacy payment creation / webhook / polling logic — removed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

# New API base — all requests go to /v1/trx/verify or /v1/trx/confirm.
TRX_BASE_URL = "https://api.zinipay.com/v1/trx"


@dataclass
class ZiniPayVerifyResult:
    """Data returned by a successful POST /verify call.

    All fields are saved before the confirm step, per the spec.
    """
    verify_id: int       # data.id
    trx_id: str          # data.trxID — used in /confirm + duplicate guard
    provider: str = ""   # e.g. "bkash", "nagad", "rocket"
    sender: str = ""     # sender mobile / account identifier
    timestamp: str = ""  # payment timestamp from ZiniPay


def _gw_cfg():
    """Lazy import — avoids a hard DB dependency at module import time."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        return get_db_session, PaymentGatewayConfig
    except Exception:
        return None, None


def _get_or_create_config(session, PaymentGatewayConfig):
    row = session.query(PaymentGatewayConfig).filter_by(gateway="zinipay").first()
    if not row:
        row = PaymentGatewayConfig(gateway="zinipay", is_enabled=False)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


class ZiniPayService:
    """Service for ZiniPay verify+confirm deposit flow.

    Usage (in handlers):
        svc = ZiniPayService()
        result = svc.verify_transaction(amount=10.0, transaction_id="TXID123")
        if result:
            ok = svc.confirm_transaction(result.trx_id, 10.0, result.verify_id)
            if ok:
                # credit wallet, record result.trx_id for duplicate prevention
    """

    SOURCE = "zinipay"  # used for payment_idempotency rows / logging

    def __init__(self):
        get_db_session, PaymentGatewayConfig = _gw_cfg()

        api_key = ""
        enabled = False

        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    api_key = row.api_key or ""
                    enabled = bool(row.is_enabled)
            except Exception:
                logger.exception("Failed to load ZiniPay config from PaymentGatewayConfig")

        # Env-var fallback — only used if nothing is configured in the admin panel.
        self.api_key = api_key or (getattr(settings, "ZINIPAY_API_KEY", "") or "")
        self.enabled = enabled
        self.last_error = ""

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """True only when an API key is present."""
        return bool(self.api_key)

    def _headers(self) -> dict:
        # New auth header per the updated ZiniPay API spec.
        return {
            "Content-Type": "application/json",
            "zinipay-api-key": self.api_key,
        }

    # ------------------------------------------------------------------
    # Step 1 — Verify
    # ------------------------------------------------------------------

    def verify_transaction(
        self,
        amount: float,
        transaction_id: Optional[str] = None,
        sms_ref: Optional[str] = None,
    ) -> Optional[ZiniPayVerifyResult]:
        """POST /v1/trx/verify — verify a user-submitted TXID or SMS reference.

        Exactly one of transaction_id or sms_ref must be supplied.
        Returns ZiniPayVerifyResult on success, None on any failure.

        Do NOT credit the wallet yet — call confirm_transaction() first.

        Args:
            amount:         Expected deposit amount.
            transaction_id: The transactionId the user submitted.
            sms_ref:        The smsRef the user submitted (alternative to TXID).
        """
        if not self.is_configured():
            self.last_error = "ZiniPay API key is not set."
            logger.warning("ZiniPay not configured (missing api_key)")
            return None

        if not transaction_id and not sms_ref:
            self.last_error = "Must supply transactionId or smsRef."
            return None

        payload: dict = {"amount": round(amount, 2)}
        if transaction_id:
            payload["transactionId"] = transaction_id
        else:
            payload["smsRef"] = sms_ref

        try:
            resp = requests.post(
                f"{TRX_BASE_URL}/verify",
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
            data = resp.json() if resp.content else {}

            if resp.status_code != 200 or not data.get("success"):
                msg = data.get("message") or data.get("error") or str(data)
                self.last_error = f"verify HTTP {resp.status_code}: {msg}"
                logger.error("ZiniPay /verify failed: status=%s body=%s", resp.status_code, data)
                return None

            inner = data.get("data") or {}
            # Accept both "trxID" and "trxId" spellings from ZiniPay.
            trx_id = str(inner.get("trxID") or inner.get("trxId") or "").strip()
            verify_id = inner.get("id")

            if not trx_id or verify_id is None:
                self.last_error = "ZiniPay verify response missing trxID or id."
                logger.error("ZiniPay /verify incomplete response: %s", data)
                return None

            return ZiniPayVerifyResult(
                verify_id=int(verify_id),
                trx_id=trx_id,
                provider=str(inner.get("provider") or ""),
                sender=str(inner.get("sender") or ""),
                timestamp=str(inner.get("timestamp") or ""),
            )

        except Exception as exc:
            self.last_error = f"Request error: {exc}"
            logger.exception("Error calling ZiniPay /verify")
            return None

    # ------------------------------------------------------------------
    # Step 2 — Confirm (call immediately after a successful verify)
    # ------------------------------------------------------------------

    def confirm_transaction(
        self,
        trx_id: str,
        amount: float,
        verify_id: int,
    ) -> bool:
        """POST /v1/trx/confirm — confirm a previously verified transaction.

        Must be called immediately after verify_transaction() returns a result.
        Wallet credit and COMPLETED status MUST only happen when this returns True.

        Args:
            trx_id:     data.trxID from the verify response.
            amount:     Same amount used in verify.
            verify_id:  data.id from the verify response.
        """
        if not self.is_configured():
            self.last_error = "ZiniPay API key is not set."
            return False

        payload = {
            "transactionId": trx_id,
            "amount": round(amount, 2),
            "id": verify_id,
        }

        try:
            resp = requests.post(
                f"{TRX_BASE_URL}/confirm",
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
            data = resp.json() if resp.content else {}

            if resp.status_code != 200 or not data.get("success"):
                msg = data.get("message") or data.get("error") or str(data)
                self.last_error = f"confirm HTTP {resp.status_code}: {msg}"
                logger.error("ZiniPay /confirm failed: status=%s body=%s", resp.status_code, data)
                return False

            return True

        except Exception as exc:
            self.last_error = f"Request error: {exc}"
            logger.exception("Error calling ZiniPay /confirm")
            return False
