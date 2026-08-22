"""Bybit payment verification service.

Bybit does not offer a public, easily-accessible merchant "Pay" API for
retail integrators. Instead this uses two V5 Asset endpoints that work with
a REGULAR personal account's API key/secret (read-only "Wallet" permission
is enough):

- GET /v5/asset/deposit/query-internal-record — off-chain, free transfers
  made within Bybit (UID/email to UID/email — the "send to my UID" flow,
  no network/chain selection). This is the primary path.
- GET /v5/asset/deposit/query-record — on-chain deposit history, used as a
  fallback for buyers who send on-chain to BYBIT_DEPOSIT_ADDRESS instead.

Both endpoints only return records for the authenticated account, so a
buyer cannot forge a match by guessing an ID — the reference has to
correspond to a transfer that actually landed in this account.
"""

import hashlib
import hmac
import time
from decimal import Decimal

import requests

from config.settings import settings

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "10000"

AMOUNT_TOLERANCE = Decimal("0.01")


class BybitPaymentError(Exception):
    """Raised when the Bybit API call itself fails (network, auth, etc)."""


def _signed_get(path: str, params: dict) -> dict:
    if not settings.BYBIT_API_KEY or not settings.BYBIT_API_SECRET:
        raise BybitPaymentError("Bybit API credentials are not configured.")

    timestamp = str(int(time.time() * 1000))
    # Bybit V5 GET signing: timestamp + api_key + recv_window + sorted query string
    query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
    payload = timestamp + settings.BYBIT_API_KEY + RECV_WINDOW + query_string
    signature = hmac.new(
        settings.BYBIT_API_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": settings.BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }

    url = f"{BASE_URL}{path}"
    if query_string:
        url += f"?{query_string}"

    try:
        response = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise BybitPaymentError(f"Network error calling Bybit API: {e}")

    if response.status_code != 200:
        raise BybitPaymentError(
            f"Bybit API error {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    if data.get("retCode") != 0:
        raise BybitPaymentError(f"Bybit API error: {data.get('retMsg')}")

    return data


def _find_internal_record(reference: str):
    """Search internal (off-chain) deposit records for a matching txID/id."""
    data = _signed_get("/v5/asset/deposit/query-internal-record", {
        "startTime": int((time.time() - 30 * 86400) * 1000),
        "endTime": int(time.time() * 1000),
        "limit": 50,
    })

    for row in data.get("result", {}).get("rows", []):
        if row.get("txID") == reference or str(row.get("id")) == reference:
            return row
    return None


def _find_onchain_deposit(reference: str):
    """Search on-chain deposit records for a matching txID."""
    data = _signed_get("/v5/asset/deposit/query-record", {
        "limit": 50,
    })

    for row in data.get("result", {}).get("rows", []):
        if row.get("txID") == reference:
            return row
    return None


def verify_payment(reference: str, expected_amount) -> tuple[bool, str]:
    """Verify a buyer-submitted Bybit reference against the admin's own account.

    Args:
        reference: the internal transfer txID/id, or on-chain txID, the
            buyer submitted.
        expected_amount: the USD/USDT amount the buyer is expected to have
            paid (Decimal or float).

    Returns:
        (ok, message) — ok is True only if a matching, completed transfer
        of the right amount was found.
    """
    reference = (reference or "").strip()
    if not reference:
        return False, "No reference provided."

    expected = Decimal(str(expected_amount))

    # Internal (UID/email) transfer — the expected path.
    row = _find_internal_record(reference)
    if row:
        # status: 1=processing, 2=success, 3=failed (per Bybit deposit status enum)
        if row.get("status") != 2:
            return False, "Transfer found but not yet completed. Please wait and resubmit."
        amount = Decimal(str(row.get("amount", "0")))
        if abs(amount - expected) > AMOUNT_TOLERANCE:
            return False, f"Amount mismatch: received {amount} but expected {expected}."
        return True, "Verified via Bybit internal transfer record."

    # Fallback: on-chain deposit.
    row = _find_onchain_deposit(reference)
    if row:
        if row.get("status") != 3:  # 3 = success for on-chain deposits
            return False, "Deposit found but not yet confirmed on-chain. Please wait and resubmit."
        amount = Decimal(str(row.get("amount", "0")))
        if abs(amount - expected) > AMOUNT_TOLERANCE:
            return False, f"Amount mismatch: received {amount} but expected {expected}."
        return True, "Verified via on-chain deposit history."

    return False, "No matching transaction found on our end yet. Double-check the ID or wait a minute and try again."
