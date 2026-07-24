"""Bybit Pay payment verification service.

IMPORTANT — this integrates the official Bybit V5 REST API directly, using a
regular read-only HMAC API Key/Secret. It is NOT the Bybit Pay merchant/
checkout product and never creates a payment or invoice on Bybit's side.
Two payment types are supported, both fully automatic:

    1. UID (Internal) Transfer
       The bot shows the store's Bybit UID and the exact amount to send.
       The user sends USDT from their own Bybit account (Assets → Transfer
       → UID Transfer) to that UID, then pastes the resulting internal
       transfer Transaction ID back into the bot. This service looks that
       transaction ID up in the store owner's own Bybit account via:

           GET /v5/asset/deposit/query-internal-record

    2. On-chain Deposit (USDT TRC20 / BEP20 / ERC20)
       The bot shows one of the store's admin-configured deposit addresses
       for the chosen network. The user sends USDT on-chain, then pastes
       the blockchain Transaction ID (TXID) back into the bot. This service
       looks that TXID up via:

           GET /v5/asset/deposit/query-record

In both cases the Bybit API response is always the source of truth — a
user-submitted transaction ID by itself proves nothing and is never
sufficient to credit a wallet (see handlers/payment_handlers.py, which only
credits the wallet after a matching, successful record is found here).

Credentials: BYBIT_API_KEY / BYBIT_API_SECRET are read from environment
variables ONLY (config/settings.py). They are never stored in the database
and never accepted through a Telegram message — see handlers/admin_bybit.py,
which only lets the admin manage display/limit/wallet-address settings.

This integration is READ-ONLY. It must never be extended to call Bybit's
withdrawal, transfer-out, trading, or order endpoints — only the deposit /
internal-deposit record (GET) endpoints above, plus a lightweight read-only
call used for the admin panel's "Test API" button.
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

API_BASE_URL = "https://api.bybit.com"
INTERNAL_DEPOSIT_PATH = "/v5/asset/deposit/query-internal-record"   # UID transfer (off-chain)
ONCHAIN_DEPOSIT_PATH = "/v5/asset/deposit/query-record"             # on-chain deposit
RECV_WINDOW_MS = 10_000
REQUEST_TIMEOUT_S = 15

# Bybit V5 depositStatus enum (on-chain) — only "3" (success) is ever
# accepted; everything else (pending/processing/failed/rolled back) is
# treated as not-yet-verifiable. See https://bybit-exchange.github.io/docs/v5/enum#depositstatus
ONCHAIN_SUCCESS_STATUS = 3

# Internal (UID) deposit status: 1=Processing, 2=Success, 3=deposit failed.
INTERNAL_SUCCESS_STATUS = 2

# Network label (as shown/admin-configured) -> Bybit "chain" code returned by
# GET /v5/asset/deposit/query-record. These are Bybit's own chain codes.
NETWORK_CHAIN_MAP = {
    "TRC20": "TRX",
    "BEP20": "BSC",
    "ERC20": "ETH",
    "LTC": "LTC",   # Litecoin on-chain deposit
    "AVAXC": "AVAXC",  # USDT Avalanche C-Chain on-chain deposit
    "TON": "TON",      # USDT TON on-chain deposit
    "BASE": "BASE",    # USDT Base (Coinbase Base L2) on-chain deposit
    "ARBONE": "ARBONE",  # USDT Arbitrum One on-chain deposit
    "OP": "OP",          # USDT Optimism on-chain deposit
    "MATIC": "MATIC",    # USDT Polygon (MATIC) on-chain deposit
    "SOL": "SOL",        # USDT Solana on-chain deposit
}
ALL_NETWORKS = tuple(NETWORK_CHAIN_MAP.keys())

# Transaction ID format guards — reject obvious junk before ever calling the
# API. Bybit's own lookup is still the real check; these are cheap pre-filters.
_UID_TXID_RE = re.compile(r"^[A-Za-z0-9_-]{5,80}$")
_ONCHAIN_TXID_RE = re.compile(r"^(0x)?[A-Za-z0-9]{5,128}$")


def _to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class PaymentType:
    UID_TRANSFER = "uid_transfer"
    ONCHAIN = "onchain"


class VerificationOutcome:
    """Result codes returned by BybitPayService.verify_*()."""
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    NETWORK_MISMATCH = "network_mismatch"
    WRONG_ADDRESS = "wrong_address"
    NOT_SUCCESSFUL = "not_successful"  # matched but not yet in a final success state
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
    network: Optional[str] = None
    bybit_record_id: Optional[str] = None
    transaction_time: Optional[int] = None  # epoch ms, normalized
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
    row = session.query(PaymentGatewayConfig).filter_by(gateway="bybit_pay").first()
    if not row:
        row = PaymentGatewayConfig(
            gateway="bybit_pay", is_enabled=False,
            bybit_allowed_networks="TRC20,BEP20,ERC20",
            bybit_order_expiry_minutes=30,
            bybit_bonus_percent=0.0,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Rate limiting + concurrency protection (SECURITY: rate-limit verification /
# prevent concurrent verification). In-process only — matches the rest of
# this project's services (see services/binance_pay.py for the same pattern).
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
    credit the wallet. The atomic DB unique constraint is the real
    guarantee; this just avoids doing the (slow) Bybit API call twice in
    parallel for the same order."""
    key = (telegram_user_id, internal_order_id)
    with _verify_locks_guard:
        lock = _verify_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _verify_locks[key] = lock
        return lock


def is_valid_uid_txid_format(txid: str) -> bool:
    return bool(txid) and bool(_UID_TXID_RE.match(txid.strip()))


def is_valid_onchain_txid_format(txid: str) -> bool:
    return bool(txid) and bool(_ONCHAIN_TXID_RE.match(txid.strip()))


class BybitPayService:
    """Service for verifying Bybit UID transfers and on-chain deposits via
    the official Bybit V5 REST API (read-only)."""

    SOURCE = "bybit_pay"  # used for payment_idempotency / ledger ref_type rows

    def __init__(self):
        get_db_session, PaymentGatewayConfig = _gw_cfg()

        uid = ""
        wallets = {"TRC20": "", "BEP20": "", "ERC20": "", "LTC": "", "AVAXC": "", "TON": "", "BASE": "", "ARBONE": "", "OP": "", "MATIC": "", "SOL": ""}
        allowed_networks = "TRC20,BEP20,ERC20,LTC,AVAXC,TON,BASE,ARBONE,OP,MATIC,SOL"
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
                    uid = row.bybit_uid or ""
                    wallets["TRC20"] = row.bybit_wallet_trc20 or ""
                    wallets["BEP20"] = row.bybit_wallet_bep20 or ""
                    wallets["ERC20"] = row.bybit_wallet_erc20 or ""
                    wallets["LTC"] = row.bybit_wallet_ltc or ""
                    wallets["AVAXC"] = row.bybit_wallet_avaxc or ""
                    wallets["TON"] = row.bybit_wallet_ton or ""
                    wallets["BASE"] = row.bybit_wallet_base or ""
                    wallets["ARBONE"] = row.bybit_wallet_arb or ""
                    wallets["OP"] = row.bybit_wallet_op or ""
                    wallets["MATIC"] = row.bybit_wallet_matic or ""
                    wallets["SOL"] = row.bybit_wallet_sol or ""
                    allowed_networks = row.bybit_allowed_networks or "TRC20,BEP20,ERC20,LTC,AVAXC,TON,BASE,ARBONE,OP,MATIC,SOL"
                    min_amount = row.bybit_min_amount or 0.0
                    max_amount = row.bybit_max_amount or 0.0
                    order_expiry_minutes = row.bybit_order_expiry_minutes or 30
                    bonus_percent = row.bybit_bonus_percent or 0.0
                    instructions = row.bybit_instructions or ""
                    enabled = bool(row.is_enabled)
            except Exception:
                logger.exception("Failed to load Bybit Pay config")

        self.uid = uid
        self.wallets = wallets
        self.allowed_networks = [
            n.strip().upper() for n in allowed_networks.split(",")
            if n.strip().upper() in NETWORK_CHAIN_MAP
        ] or list(ALL_NETWORKS)
        self.min_amount = min_amount
        self.max_amount = max_amount
        self.order_expiry_minutes = order_expiry_minutes
        self.bonus_percent = bonus_percent
        self.instructions = instructions
        self.enabled = enabled

        # Credentials: environment variables ONLY. Never read from the DB,
        # never settable via Telegram — see module docstring / SECURITY.
        # Credentials: DB-configured key takes priority over environment variable.
        db_api_key = ""
        db_api_secret = ""
        if get_db_session is not None:
            try:
                with get_db_session() as session:
                    row = _get_or_create_config(session, PaymentGatewayConfig)
                    db_api_key = (row.bybit_api_key or "").strip()
                    db_api_secret = (row.bybit_api_secret or "").strip()
            except Exception:
                pass  # Fall through to env vars
        self.api_key = db_api_key or settings.BYBIT_API_KEY or ""
        self.api_secret = db_api_secret or settings.BYBIT_API_SECRET or ""
        # Track where the credentials came from (for admin status display)
        self.credentials_source = "db" if db_api_key else ("env" if settings.BYBIT_API_KEY else "none")
        self.last_error = ""

    # ------------------------------------------------------------------
    # Credential / config helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def credential_diagnostics(self) -> dict:
        """Masked, non-secret-leaking snapshot of exactly what this process
        sees for BYBIT_API_KEY / BYBIT_API_SECRET — for debugging "Not
        Configured" without ever printing/logging the real values.

        Compares os.getenv() directly against settings.BYBIT_API_KEY/SECRET
        so a stale import or a config-layer bug (rather than a genuinely
        missing Render env var) would show up as a mismatch here.
        """
        import os as _os

        def _mask(v: str) -> str:
            if not v:
                return "(empty)"
            if len(v) <= 4:
                return "*" * len(v)
            return f"{v[:2]}{'*' * (len(v) - 4)}{v[-2:]}"

        raw_key = _os.getenv("BYBIT_API_KEY", "")
        raw_secret = _os.getenv("BYBIT_API_SECRET", "")

        return {
            "env_key_present": bool(raw_key),
            "env_key_len": len(raw_key),
            "env_key_masked": _mask(raw_key),
            "env_key_has_whitespace": raw_key != raw_key.strip(),
            "env_secret_present": bool(raw_secret),
            "env_secret_len": len(raw_secret),
            "env_secret_masked": _mask(raw_secret),
            "env_secret_has_whitespace": raw_secret != raw_secret.strip(),
            "settings_key_len": len(self.api_key),
            "settings_secret_len": len(self.api_secret),
            "settings_matches_env": (raw_key.strip() == self.api_key and raw_secret.strip() == self.api_secret),
        }

    def wallet_for_network(self, network: str) -> str:
        return self.wallets.get((network or "").strip().upper(), "")

    def networks_with_wallets(self) -> list:
        """Networks that are both admin-enabled AND have a configured
        deposit address — the only ones ever shown to users."""
        return [n for n in self.allowed_networks if self.wallet_for_network(n)]

    # ------------------------------------------------------------------
    # Low-level signed request plumbing (Bybit V5 HMAC signing)
    # ------------------------------------------------------------------

    def _sign(self, payload: str) -> str:
        """HMAC SHA256 signature per Bybit's documented convention:
        sign = HMAC_SHA256(timestamp + api_key + recv_window + queryString, api_secret)
        Never log the secret, the payload together with the secret, or the
        computed signature alongside either — only HTTP status / already-
        public response bodies are logged on error."""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_get(self, path: str, params: Optional[dict] = None) -> tuple:
        """Perform a signed, read-only GET request against the Bybit V5 API.
        Returns (ok, status_code, data_or_None).

        On any failure — network exception, HTTP error, or a Bybit-level
        rejection (retCode != 0) — ``self.last_error`` is set to a precise,
        human-readable reason (exact exception text, or the exact Bybit
        retCode/retMsg) so callers can surface *why* it failed instead of a
        generic "Invalid"/"Unreachable".

        Never logs BYBIT_API_KEY, BYBIT_API_SECRET, or the computed
        signature — only the HTTP status and (already-public) response body
        are logged on error.
        """
        self.last_error = ""

        if not self.is_configured():
            self.last_error = "BYBIT_API_KEY / BYBIT_API_SECRET not set in this process's environment"
            return False, 0, None

        query_string = urlencode(dict(params or {}), doseq=True)
        timestamp = str(int(time.time() * 1000))
        recv_window = str(RECV_WINDOW_MS)
        to_sign = f"{timestamp}{self.api_key}{recv_window}{query_string}"
        signature = self._sign(to_sign)

        url = f"{API_BASE_URL}{path}"
        if query_string:
            url += f"?{query_string}"

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
        }

        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        except Exception as exc:
            # Exact exception (DNS failure, timeout, connection refused,
            # TLS error, etc.) — never swallowed, never replaced with a
            # generic message.
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bybit API request error for %s", path)
            return False, 0, None

        try:
            data = resp.json() if resp.content else None
        except ValueError as exc:
            data = None
            self.last_error = f"Non-JSON response from Bybit (HTTP {resp.status_code}): {exc}"

        ret_code = data.get("retCode") if isinstance(data, dict) else None
        ret_msg = data.get("retMsg") if isinstance(data, dict) else None
        if resp.status_code != 200 or (ret_code is not None and ret_code != 0):
            # data may contain a Bybit error code/msg — safe to log, it
            # never contains our credentials.
            if ret_code is not None:
                self.last_error = f"Bybit retCode={ret_code} retMsg={ret_msg!r} (HTTP {resp.status_code})"
            else:
                self.last_error = f"HTTP {resp.status_code}: {data!r}"
            logger.error("Bybit API %s failed: HTTP %s - %s", path, resp.status_code, data)
            return False, resp.status_code, data

        return True, resp.status_code, data

    # ------------------------------------------------------------------
    # Public API — connectivity test (admin panel)
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple:
        """Safe, read-only connectivity check for the admin panel's
        'Test Bybit API' button. Returns (ok: bool, message: str).

        ``message`` is only ever "Connected" after Bybit has actually
        returned HTTP 200 + retCode 0 for a signed, authenticated request —
        never based on credentials merely being present. On any failure the
        exact cause (missing env vars, network exception, or Bybit's own
        retCode/retMsg) is included verbatim.
        """
        if not self.is_configured():
            diag = self.credential_diagnostics()
            missing = []
            if not diag["env_key_present"]:
                missing.append("BYBIT_API_KEY")
            if not diag["env_secret_present"]:
                missing.append("BYBIT_API_SECRET")
            return False, f"Not Configured — missing from this process's environment: {', '.join(missing) or 'unknown'}"

        ok, status, data = self._signed_get(ONCHAIN_DEPOSIT_PATH, {"limit": 1})
        if ok:
            return True, "Connected"

        if status == 0:
            # _signed_get never reached Bybit — network/exception failure.
            return False, f"Request failed: {self.last_error or 'unknown error'}"

        ret_code = data.get("retCode") if isinstance(data, dict) else None
        ret_msg = data.get("retMsg") if isinstance(data, dict) else None
        if ret_code is not None:
            return False, f"Bybit rejected the request — retCode {ret_code}: {ret_msg}"
        return False, f"Unreachable (HTTP {status})"

    # ------------------------------------------------------------------
    # Public API — raw record fetchers
    # ------------------------------------------------------------------

    def get_internal_deposit_records(self, coin: Optional[str] = None, limit: int = 50) -> Optional[list]:
        """GET /v5/asset/deposit/query-internal-record — UID (off-chain)
        transfer history received by this Bybit account."""
        params = {"limit": limit}
        if coin:
            params["coin"] = coin.upper()
        ok, status, data = self._signed_get(INTERNAL_DEPOSIT_PATH, params)
        if not ok or not isinstance(data, dict):
            return None
        rows = (data.get("result") or {}).get("rows")
        return rows if isinstance(rows, list) else None

    def get_onchain_deposit_records(self, coin: Optional[str] = None, limit: int = 50) -> Optional[list]:
        """GET /v5/asset/deposit/query-record — on-chain deposit history."""
        params = {"limit": limit}
        if coin:
            params["coin"] = coin.upper()
        ok, status, data = self._signed_get(ONCHAIN_DEPOSIT_PATH, params)
        if not ok or not isinstance(data, dict):
            return None
        rows = (data.get("result") or {}).get("rows")
        return rows if isinstance(rows, list) else None

    # ------------------------------------------------------------------
    # Public API — verification
    # ------------------------------------------------------------------

    def verify_uid_transfer(
        self,
        *,
        transaction_id: str,
        expected_amount: Decimal,
        currency: str,
        order_created_at,  # datetime — the internal order's created_at
    ) -> VerificationResult:
        """Look up ``transaction_id`` in Bybit's internal (UID) deposit
        history and validate it against everything the spec requires EXCEPT
        the DB duplicate-use check (that must be enforced atomically at
        insert time by the caller — see handlers/payment_handlers.py — since
        a pure read here can't prevent a race between two concurrent
        verifications)."""
        if not self.is_configured():
            return VerificationResult(VerificationOutcome.NOT_CONFIGURED)

        if not is_valid_uid_txid_format(transaction_id):
            return VerificationResult(VerificationOutcome.INVALID_TXID)

        rows = self.get_internal_deposit_records(coin=currency, limit=50)
        if rows is None:
            return VerificationResult(
                VerificationOutcome.API_ERROR,
                detail="Bybit verification is temporarily unavailable.",
            )

        txid = transaction_id.strip()
        match = None
        for row in rows:
            candidate_ids = {str(row.get("txID") or ""), str(row.get("id") or "")}
            if txid in candidate_ids and txid:
                match = row
                break

        if not match:
            return VerificationResult(VerificationOutcome.NOT_FOUND)

        # Status must be a final success (2 = Success).
        try:
            status = int(match.get("status"))
        except (TypeError, ValueError):
            status = None
        if status != INTERNAL_SUCCESS_STATUS:
            return VerificationResult(VerificationOutcome.NOT_SUCCESSFUL, matched_record=match,
                                       detail=f"Internal transfer status is '{status}', not Success.")

        # Currency must match the pending order's currency.
        received_currency = str(match.get("coin") or "").upper()
        if received_currency != currency.strip().upper():
            return VerificationResult(VerificationOutcome.CURRENCY_MISMATCH, matched_record=match,
                                       currency=received_currency)

        # Received amount must exactly match the pending order amount.
        received_amount = _to_decimal(match.get("amount"))
        if received_amount is None:
            return VerificationResult(VerificationOutcome.NOT_FOUND, matched_record=match,
                                       detail="Transaction record had no readable amount.")
        if received_amount != expected_amount:
            return VerificationResult(
                VerificationOutcome.AMOUNT_MISMATCH, matched_record=match,
                received_amount=received_amount, currency=received_currency,
            )

        # Transfer timestamp must be after the order was created.
        # createdTime is returned in SECONDS (per Bybit's docs/example).
        tx_time_ms = self._to_epoch_ms(match.get("createdTime"))
        if tx_time_ms is not None and order_created_at is not None:
            order_created_ms = int(order_created_at.timestamp() * 1000)
            # Small grace window for clock skew between our server and Bybit.
            if tx_time_ms < order_created_ms - 60_000:
                return VerificationResult(VerificationOutcome.TOO_OLD, matched_record=match)

        bybit_record_id = str(match.get("id") or match.get("txID") or "") or None

        return VerificationResult(
            VerificationOutcome.SUCCESS,
            matched_record=match,
            received_amount=received_amount,
            currency=received_currency,
            bybit_record_id=bybit_record_id,
            transaction_time=tx_time_ms,
        )

    def verify_onchain_deposit(
        self,
        *,
        transaction_id: str,
        expected_amount: Decimal,
        currency: str,
        network: str,
        order_created_at,  # datetime — the internal order's created_at
        tolerance: Decimal = Decimal("0"),  # allowed absolute difference in received vs expected
    ) -> VerificationResult:
        """Look up ``transaction_id`` (the blockchain TXID) in Bybit's
        on-chain deposit history and validate it against everything the spec
        requires EXCEPT the DB duplicate-use check (enforced atomically at
        insert time by the caller)."""
        if not self.is_configured():
            return VerificationResult(VerificationOutcome.NOT_CONFIGURED)

        network = (network or "").strip().upper()
        expected_chain = NETWORK_CHAIN_MAP.get(network)
        if not expected_chain:
            return VerificationResult(VerificationOutcome.NETWORK_MISMATCH, detail="Unsupported network.")

        if not is_valid_onchain_txid_format(transaction_id):
            return VerificationResult(VerificationOutcome.INVALID_TXID)

        expected_address = self.wallet_for_network(network)
        if not expected_address:
            return VerificationResult(VerificationOutcome.NOT_CONFIGURED,
                                       detail=f"No {network} deposit address configured.")

        rows = self.get_onchain_deposit_records(coin=currency, limit=50)
        if rows is None:
            return VerificationResult(
                VerificationOutcome.API_ERROR,
                detail="Bybit verification is temporarily unavailable.",
            )

        txid = transaction_id.strip().lower()
        match = None
        for row in rows:
            candidate_ids = {str(row.get("txID") or "").lower(), str(row.get("id") or "").lower()}
            if txid in candidate_ids and txid:
                match = row
                break

        if not match:
            return VerificationResult(VerificationOutcome.NOT_FOUND)

        # Chain (network) must match what the user was asked to send on.
        received_chain = str(match.get("chain") or "").upper()
        if received_chain != expected_chain:
            return VerificationResult(VerificationOutcome.NETWORK_MISMATCH, matched_record=match,
                                       network=received_chain)

        # Deposit must have landed on OUR configured wallet address —
        # "Belongs to configured wallet" requirement.
        to_address = str(match.get("toAddress") or "").strip()
        if to_address.lower() != expected_address.strip().lower():
            return VerificationResult(VerificationOutcome.WRONG_ADDRESS, matched_record=match)

        # Status must be a final success (3 = success).
        try:
            status = int(match.get("status"))
        except (TypeError, ValueError):
            status = None
        if status != ONCHAIN_SUCCESS_STATUS:
            return VerificationResult(VerificationOutcome.NOT_SUCCESSFUL, matched_record=match,
                                       detail=f"Deposit status is '{status}', not yet confirmed successful.")

        # Currency must match.
        received_currency = str(match.get("coin") or "").upper()
        if received_currency != currency.strip().upper():
            return VerificationResult(VerificationOutcome.CURRENCY_MISMATCH, matched_record=match,
                                       currency=received_currency)

        # Received amount must exactly match the pending order amount.
        received_amount = _to_decimal(match.get("amount"))
        if received_amount is None:
            return VerificationResult(VerificationOutcome.NOT_FOUND, matched_record=match,
                                       detail="Deposit record had no readable amount.")
        _tol = tolerance if tolerance is not None else Decimal("0")
        if abs(received_amount - expected_amount) > _tol:
            return VerificationResult(
                VerificationOutcome.AMOUNT_MISMATCH, matched_record=match,
                received_amount=received_amount, currency=received_currency,
            )

        # Deposit timestamp must be after the order was created.
        tx_time_ms = self._to_epoch_ms(match.get("successAt"))
        if tx_time_ms is not None and order_created_at is not None:
            order_created_ms = int(order_created_at.timestamp() * 1000)
            if tx_time_ms < order_created_ms - 60_000:
                return VerificationResult(VerificationOutcome.TOO_OLD, matched_record=match)

        bybit_record_id = str(match.get("id") or match.get("txID") or "") or None

        return VerificationResult(
            VerificationOutcome.SUCCESS,
            matched_record=match,
            received_amount=received_amount,
            currency=received_currency,
            network=network,
            bybit_record_id=bybit_record_id,
            transaction_time=tx_time_ms,
        )

    @staticmethod
    def _to_epoch_ms(value) -> Optional[int]:
        """Bybit timestamps show up as either second- or millisecond-epoch
        strings depending on the endpoint. Normalize to epoch ms."""
        if value is None:
            return None
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return None
        # 10-digit (or fewer) numbers are seconds; 13-digit are already ms.
        return ivalue * 1000 if ivalue < 10_000_000_000 else ivalue
