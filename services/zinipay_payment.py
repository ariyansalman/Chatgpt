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


# ---------------------------------------------------------------------------
# Per-provider (bKash / Nagad / Rocket / Upay) configuration status.
#
# ZiniPay is a single API-key gateway that fans out to four BD mobile-money
# providers, each identified purely by whether the admin has set a wallet
# (merchant) number for it in the Wallet Manager. A provider with no wallet
# number is always "Not Configured":
#   - it must never be shown to customers on the payment selection screen
#   - it can never be selected as the Default Provider
# regardless of the overall ZiniPay enable/disable toggle. These helpers are
# the single source of truth every other module reads to answer that
# question, so a Wallet Manager edit is reflected everywhere immediately —
# no caching, no restart required.
# ---------------------------------------------------------------------------

PROVIDER_ORDER = ("bkash", "nagad", "rocket", "upay")


def provider_numbers() -> dict:
    """Return {provider: wallet_number_or_""}, read fresh from the DB on
    every call."""
    numbers = {p: "" for p in PROVIDER_ORDER}
    get_db_session, PaymentGatewayConfig = _gw_cfg()
    if get_db_session is None:
        return numbers
    try:
        with get_db_session() as session:
            row = session.query(PaymentGatewayConfig).filter_by(gateway="zinipay").first()
            if row:
                numbers = {
                    "bkash":  (row.zinipay_bkash_number  or "").strip(),
                    "nagad":  (row.zinipay_nagad_number  or "").strip(),
                    "rocket": (row.zinipay_rocket_number or "").strip(),
                    "upay":   (row.zinipay_upay_number   or "").strip(),
                }
    except Exception:
        logger.exception("Failed to load ZiniPay provider wallet numbers")
    return numbers


def configured_providers(numbers: Optional[dict] = None) -> dict:
    """Return {provider: bool} — True only when a wallet number is set for
    that provider AND the admin panel's per-provider checkbox is enabled
    (``zinipay_provider_<name>_enabled`` in the generic BotConfig store,
    default True — see handlers/admin_zinipay.py). A provider with no
    wallet number, or one an admin has unchecked, is "Not Configured" /
    hidden from the customer deposit menu and can't be selected."""
    numbers = numbers if numbers is not None else provider_numbers()
    try:
        from utils.bot_config import cfg
        enabled = {
            p: cfg.get_bool(f"zinipay_provider_{p}_enabled", True)
            for p in PROVIDER_ORDER
        }
    except Exception:
        # Config store unavailable — fail open to the pre-existing
        # wallet-number-only behavior rather than hiding every provider.
        enabled = {p: True for p in PROVIDER_ORDER}
    return {p: bool(numbers.get(p)) and enabled[p] for p in PROVIDER_ORDER}


def is_any_provider_configured(numbers: Optional[dict] = None) -> bool:
    """True when at least one BD mobile-money provider has a wallet number
    set. Used to decide whether the combined ZiniPay entry point (top-level
    'Mobile Banking' row / legacy gateway list) should appear at all."""
    return any(configured_providers(numbers).values())


def first_configured_provider(numbers: Optional[dict] = None) -> Optional[str]:
    """First configured provider in canonical bKash → Nagad → Rocket → Upay
    order, or None when nothing is configured at all."""
    cfg = configured_providers(numbers)
    for p in PROVIDER_ORDER:
        if cfg.get(p):
            return p
    return None


def provider_status(provider: str, *, enabled_overall: bool, numbers: Optional[dict] = None) -> str:
    """One of "not_configured" | "enabled" | "disabled" for a single provider:
      - "not_configured": no wallet number set. Must never be shown to
        customers and can never be the Default Provider.
      - "enabled": wallet number set AND the ZiniPay gateway is turned on.
      - "disabled": wallet number set but the ZiniPay gateway is turned off.
    """
    numbers = numbers if numbers is not None else provider_numbers()
    if not numbers.get(provider):
        return "not_configured"
    return "enabled" if enabled_overall else "disabled"


def resolve_bdt_amount(usd_amount: float, crypto_address: Optional[str]):
    """Single source of truth for "what BDT amount was this user quoted?"

    ZiniPay (bKash / Nagad / Rocket / Upay) transactions always keep
    Transaction.amount in USD (the wallet currency) and stash the BDT
    figure + chosen provider the user was actually shown at order-creation
    time in Transaction.crypto_address, formatted as
    "bdt:<amount>:<provider>" (see handlers/payment_handlers.py,
    _finish_zinipay_payment).

    Every screen that re-displays an existing ZiniPay order (payment
    instructions, pending-deposit notice, TXID prompt, verification
    screens) MUST call this helper instead of re-deriving the BDT amount
    itself, so bKash/Nagad/Rocket all show the exact figure the user
    locked in — never the raw USD number with a ৳ symbol slapped on it,
    and never a second, possibly different, recalculated figure.

    Falls back to a fresh USD->BDT conversion only for legacy rows created
    before the BDT amount was stored on crypto_address.

    Returns (bdt_amount, provider_or_None).
    """
    provider = None
    bdt_amount = 0.0
    if crypto_address and crypto_address.startswith("bdt:"):
        parts = crypto_address.split(":")
        if len(parts) > 1 and parts[1]:
            try:
                bdt_amount = float(parts[1])
            except ValueError:
                bdt_amount = 0.0
        if len(parts) > 2 and parts[2]:
            provider = parts[2].strip().lower()
    if bdt_amount <= 0:
        from services.pricing import convert_currency
        bdt_amount = convert_currency(usd_amount, "USD", "BDT")
    return bdt_amount, provider


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
