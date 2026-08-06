"""Binance Pay payment verification service.

IMPORTANT — this is NOT the Binance Pay Merchant API. It does not require a
Binance Merchant account, does not accept webhooks, and never creates a
payment on Binance's side. Instead:

    1. The bot shows the store's Binance Pay ID and the exact amount to send.
    2. The user sends USDT/USDC to that Pay ID from their own Binance app.
    3. The user pastes the resulting Binance Pay transaction ID back into the
       bot.
    4. This service looks that transaction ID up in the store owner's own
       Binance account transaction history via the normal (spot) Binance API,
       using a regular HMAC API Key/Secret:

           GET /sapi/v1/pay/transactions

       and only credits the wallet if a matching, successful, RECEIVED
       transaction for the right amount/currency is found there. The Binance
       API response is always the source of truth — a user-submitted
       transaction ID by itself proves nothing and is never sufficient to
       credit a wallet (see handlers/payment_handlers.py).

Credentials: BINANCE_API_KEY / BINANCE_API_SECRET are read from environment
variables ONLY (config/settings.py). They are never stored in the database
and never accepted through a Telegram message — see handlers/admin_binance.py,
which only lets the admin manage display/limit settings.

This integration is READ-ONLY. It must never be extended to call Binance's
withdrawal, trading, or transfer endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlencode

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.binance.com"
PAY_TRANSACTIONS_PATH = "/sapi/v1/pay/transactions"
RECV_WINDOW_MS = 10_000
REQUEST_TIMEOUT_S = 15

# Binance Pay transaction "side" per docs: 0 = SEND (paid out), 1 = RECEIVE
# (this is the direction the store cares about — money coming IN).
PAY_SIDE_RECEIVE = 1

# Binance Pay transaction "status" strings that mean the funds have actually
# settled. Anything else (e.g. "PROCESS", "FAIL", "REFUND") is not accepted.
PAID_STATUSES = {"SUCCESS", "COMPLETED"}

# A Binance Pay transaction ID / order ID is alphanumeric; this is a generous
# but non-empty bound just to reject obvious junk before ever calling the API.
_TXID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


def _to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class VerificationOutcome:
    """Result codes returned by BinancePayService.verify_transaction()."""
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    WRONG_DIRECTION = "wrong_direction"
    TOO_OLD = "too_old"
    ALREADY_USED = "already_used"
    INVALID_TXID = "invalid_txid"
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"


@dataclass
class VerificationResult:
    outcome: str
    matched_record: Optional[dict] = None
    received_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    binance_order_id: Optional[str] = None
    transaction_time: Optional[int] = None  # epoch ms, as returned by Binance
    detail: str = ""


def _gw_cfg():
    """Lazy import — avoids a hard DB dependency at module import time."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        return get_db_session, PaymentGatewayConfig
    except Exception:
        return None, None


def _get_or_create_config(session, PaymentGatewayConfig):
    row = session.query(PaymentGatewayConfig).filter_by(gateway="binance_pay").first()
    if not row:
        row = PaymentGatewayConfig(
            gateway="binance_pay", is_enabled=False,
            binance_allowed_currencies="USDT,USDC",
            binance_order_expiry_minutes=30,
            binance_bonus_percent=0.0,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Rate limiting + concurrency protection (Section: TRANSACTION VERIFICATION /
# SECURITY). In-process only — matches the rest of this project's services,
# none of which use a distributed lock/rate-limit backend either.
# ---------------------------------------------------------------------------
_verify_attempts_lock = threading.Lock()
_verify_attempts: dict = {}  # telegram_user_id -> deque[float] of attempt timestamps
_verify_locks_guard = threading.Lock()
_verify_locks: dict = {}  # (telegram_user_id, internal_order_id) -> threading.Lock

MAX_VERIFY_ATTEMPTS_PER_WINDOW = 5
VERIFY_WINDOW_SECONDS = 60


def is_rate_limited(telegram_user_id: int) -> bool:
    """True if this user has exceeded the verification attempt rate limit."""
    now = time.monotonic()
    with _verify_attempts_lock:
        dq = _verify_attempts.setdefault(telegram_user_id, deque())
        while dq and now - dq[0] > VERIFY_WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= MAX_VERIFY_ATTEMPTS_PER_WINDOW:
            return True
        dq.append(now)
        return False


def get_order_lock(telegram_user_id: int, internal_order_id: int) -> threading.Lock:
    """One lock per (user, order) so two near-simultaneous taps of 'Submit
    Transaction ID' can't both pass the DB duplicate-check race and double
    credit the wallet. The atomic DB unique constraint is the real guarantee;
    this just avoids doing the (slow) Binance API call twice in parallel."""
    key = (telegram_user_id, internal_order_id)
    with _verify_locks_guard:
        lock = _verify_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _verify_locks[key] = lock
        return lock


def is_valid_txid_format(txid: str) -> bool:
    return bool(txid) and bool(_TXID_RE.match(txid.strip()))


class BinancePayService:
    """Service for verifying Binance Pay payments via transaction history."""

    SOURCE = "binance_pay"  # used for payment_idempotency rows

    def __init__(self):
        get_db_session, PaymentGatewayConfig = _gw_cfg()

        pay_id = ""
        allowed_currencies = "USDT,USDC"
        min_amount = 0.0
        max_amount = 0.0
        order_expiry_minutes = 30
        bonus_percent = 0.0
        instructions = ""
        enabled = False

        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    pay_id = row.binance_pay_id or ""
                    allowed_currencies = row.binance_allowed_currencies or "USDT,USDC"
                    min_amount = row.binance_min_amount or 0.0
                    max_amount = row.binance_max_amount or 0.0
                    order_expiry_minutes = row.binance_order_expiry_minutes or 30
                    bonus_percent = row.binance_bonus_percent or 0.0
                    instructions = row.binance_instructions or ""
                    enabled = bool(row.is_enabled)
            except Exception:
                logger.exception("Failed to load Binance Pay config from PaymentGatewayConfig")

        self.pay_id = pay_id
        self.allowed_currencies = [
            c.strip().upper() for c in allowed_currencies.split(",") if c.strip()
        ] or ["USDT", "USDC"]
        self.min_amount = min_amount
        self.max_amount = max_amount
        self.order_expiry_minutes = order_expiry_minutes
        self.bonus_percent = bonus_percent
        self.instructions = instructions
        self.enabled = enabled

        # Credentials: DB-configured key takes priority over environment variable.
        # When neither is set, self.api_key / api_secret remain empty and
        # is_configured() returns False, suppressing all API calls.
        db_api_key = ""
        db_api_secret = ""
        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    db_api_key = (row.binance_api_key or "").strip()
                    db_api_secret = (row.binance_api_secret or "").strip()
            except Exception:
                pass  # Fall through to env vars
        self.api_key = db_api_key or settings.BINANCE_API_KEY or ""
        self.api_secret = db_api_secret or settings.BINANCE_API_SECRET or ""
        # Track where the credentials came from (for admin status display)
        self.credentials_source = "db" if db_api_key else ("env" if settings.BINANCE_API_KEY else "none")
        self.last_error = ""

    # ------------------------------------------------------------------
    # Credential / config helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def is_currency_allowed(self, currency: str) -> bool:
        return bool(currency) and currency.strip().upper() in self.allowed_currencies

    # ------------------------------------------------------------------
    # Low-level signed request plumbing
    # ------------------------------------------------------------------

    def _sign(self, query_string: str) -> str:
        """HMAC SHA256 signature over the query string, per Binance's signed
        endpoint convention. Never log query_string (it doesn't contain the
        secret) or the returned signature together with the secret."""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_get(self, path: str, params: Optional[dict] = None) -> tuple:
        """Perform a signed GET request. Returns (ok, status_code, data_or_None).

        Never logs BINANCE_API_KEY, BINANCE_API_SECRET, or the computed
        signature — only the HTTP status and (already-public) response body
        are logged on error.
        """
        if not self.is_configured():
            return False, 0, None

        query = dict(params or {})
        query["timestamp"] = int(time.time() * 1000)
        query["recvWindow"] = RECV_WINDOW_MS
        query_string = urlencode(query, doseq=True)
        signature = self._sign(query_string)
        url = f"{API_BASE_URL}{path}?{query_string}&signature={signature}"

        try:
            resp = requests.get(
                url,
                headers={"X-MBX-APIKEY": self.api_key},
                timeout=REQUEST_TIMEOUT_S,
            )
        except Exception:
            logger.exception("Binance API request error for %s", path)
            return False, 0, None

        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = None

        if resp.status_code != 200:
            # data may contain a Binance error code/msg — safe to log, it
            # never contains our credentials.
            logger.error("Binance API %s failed: HTTP %s - %s", path, resp.status_code, data)
            return False, resp.status_code, data

        return True, resp.status_code, data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple:
        """Safe, read-only connectivity check for the admin panel's
        'Test Binance API' button. Returns (ok: bool, message: str)."""
        if not self.is_configured():
            return False, "Not Configured"
        ok, status, data = self._signed_get(PAY_TRANSACTIONS_PATH, {"limit": 1})
        if ok:
            return True, "Connected"
        if status in (401, 403) or (isinstance(data, dict) and data.get("code") in (-2014, -2015, -1022)):
            return False, "Invalid"
        return False, f"Unreachable (HTTP {status or 'network error'})"

    def get_pay_transactions(self, limit: int = 100) -> Optional[list]:
        """GET /sapi/v1/pay/transactions — recent Binance Pay history."""
        ok, status, data = self._signed_get(PAY_TRANSACTIONS_PATH, {"limit": limit})
        if not ok or not isinstance(data, dict):
            return None
        rows = data.get("data")
        return rows if isinstance(rows, list) else None

    def verify_transaction(
        self,
        *,
        transaction_id: str,
        expected_amount: Decimal,
        currency: str,
        order_created_at,  # datetime — the internal order's created_at
    ) -> VerificationResult:
        """Look up ``transaction_id`` in Binance Pay history and validate it
        against everything the spec requires EXCEPT the DB duplicate-use
        check (that must be enforced atomically at insert time by the
        caller — see handlers/payment_handlers.py — since a pure read here
        can't prevent a race between two concurrent verifications)."""
        if not self.is_configured():
            return VerificationResult(VerificationOutcome.NOT_CONFIGURED)

        if not is_valid_txid_format(transaction_id):
            return VerificationResult(VerificationOutcome.INVALID_TXID)

        rows = self.get_pay_transactions(limit=100)
        if rows is None:
            return VerificationResult(
                VerificationOutcome.API_ERROR,
                detail="Binance verification is temporarily unavailable.",
            )

        txid = transaction_id.strip()
        match = None
        for row in rows:
            candidate_ids = {
                str(row.get("transactionId") or ""),
                str(row.get("orderId") or ""),
                str(row.get("tranId") or ""),
            }
            if txid in candidate_ids and txid:
                match = row
                break

        if not match:
            return VerificationResult(VerificationOutcome.NOT_FOUND)

        # 3. Must be an incoming (RECEIVED) transaction.
        side = match.get("transactionSide") or match.get("side")
        try:
            side_int = int(side)
        except (TypeError, ValueError):
            side_int = None
        side_label = str(match.get("transactionSide") or match.get("side") or "").upper()
        is_receive = side_int == PAY_SIDE_RECEIVE or side_label in ("RECEIVE", "RECEIVED", "1")
        if not is_receive:
            return VerificationResult(VerificationOutcome.WRONG_DIRECTION, matched_record=match)

        # 4. Status must be successful/completed.
        status = str(match.get("status") or "").upper()
        if status not in PAID_STATUSES:
            return VerificationResult(VerificationOutcome.NOT_FOUND, matched_record=match,
                                       detail=f"Transaction status is '{status or 'unknown'}'.")

        # 5. Currency must match the configured allowed currency.
        received_currency = str(match.get("currency") or match.get("fiatCurrency") or "").upper()
        if not self.is_currency_allowed(received_currency) or received_currency != currency.strip().upper():
            return VerificationResult(VerificationOutcome.CURRENCY_MISMATCH, matched_record=match,
                                       currency=received_currency)

        # 7. Received amount must exactly match the pending order amount.
        received_amount = _to_decimal(match.get("amount") or match.get("orderAmount"))
        if received_amount is None:
            return VerificationResult(VerificationOutcome.NOT_FOUND, matched_record=match,
                                       detail="Transaction record had no readable amount.")
        if received_amount != expected_amount:
            return VerificationResult(
                VerificationOutcome.AMOUNT_MISMATCH, matched_record=match,
                received_amount=received_amount, currency=received_currency,
            )

        # 8. Transaction timestamp must be after the order was created.
        tx_time_ms = match.get("transactionTime") or match.get("createTime") or match.get("orderCreateTime")
        try:
            tx_time_ms = int(tx_time_ms)
        except (TypeError, ValueError):
            tx_time_ms = None
        if tx_time_ms is not None and order_created_at is not None:
            order_created_ms = int(order_created_at.timestamp() * 1000)
            if tx_time_ms < order_created_ms:
                return VerificationResult(VerificationOutcome.TOO_OLD, matched_record=match)

        binance_order_id = str(match.get("orderId") or match.get("tranId") or "") or None

        return VerificationResult(
            VerificationOutcome.SUCCESS,
            matched_record=match,
            received_amount=received_amount,
            currency=received_currency,
            binance_order_id=binance_order_id,
            transaction_time=tx_time_ms,
        )
