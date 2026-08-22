"""Binance payment verification service.

This does NOT use the merchant-only Binance Pay API (that requires an
approved business/KYB account). Instead it uses two endpoints that work
with a REGULAR personal account's API key/secret:

- GET /sapi/v1/pay/transactions   ("Get Pay Trade History") — the account's
  own Binance Pay / C2C transfer history. Used to confirm a buyer's Binance
  Pay / "send to Binance ID" transfer landed in the admin's account.
- GET /sapi/v1/capital/deposit/hisrec — the account's own on-chain deposit
  history. Used as a fallback for buyers who send an on-chain transfer to
  BINANCE_DEPOSIT_ADDRESS instead.

Both are signed (HMAC-SHA256) endpoints scoped to the calling account —
there is no way to query someone else's Binance account with them, so a
buyer cannot forge a match by guessing IDs; the reference must correspond
to a real transaction that actually credited this account.
"""

import hashlib
import hmac
import time
from decimal import Decimal
from urllib.parse import urlencode

import requests

from config.settings import settings

BASE_URL = "https://api.binance.com"

# How close the found transaction's amount must be to what the user claims
# to have paid, to absorb exchange-side fee deductions / rounding.
AMOUNT_TOLERANCE = Decimal("0.01")


class BinancePaymentError(Exception):
    """Raised when the Binance API call itself fails (network, auth, etc)."""


def _signed_request(path: str, params: dict) -> dict:
    if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
        raise BinancePaymentError("Binance API credentials are not configured.")

    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 10000)

    query_string = urlencode(params)
    signature = hmac.new(
        settings.BINANCE_API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()
    query_string += f"&signature={signature}"

    headers = {"X-MBX-APIKEY": settings.BINANCE_API_KEY}

    try:
        response = requests.get(
            f"{BASE_URL}{path}?{query_string}",
            headers=headers,
            timeout=15
        )
    except requests.RequestException as e:
        raise BinancePaymentError(f"Network error calling Binance API: {e}")

    if response.status_code != 200:
        raise BinancePaymentError(
            f"Binance API error {response.status_code}: {response.text[:300]}"
        )

    return response.json()


def _find_pay_transaction(reference: str):
    """Search the account's Binance Pay / C2C history for a matching transactionId.

    Get Pay Trade History only returns the last 90 days by default and does
    not support filtering by transactionId server-side, so we page through
    recent history and match client-side.
    """
    start_time = int((time.time() - 90 * 86400) * 1000)
    data = _signed_request("/sapi/v1/pay/transactions", {
        "startTime": start_time,
        "limit": 100,
    })

    if data.get("code") != "000000" or not data.get("success", True):
        raise BinancePaymentError(f"Binance Pay history error: {data}")

    for txn in data.get("data", []):
        if str(txn.get("transactionId")) == reference:
            return txn
    return None


def _find_deposit(reference: str):
    """Search the account's on-chain deposit history for a matching txId."""
    data = _signed_request("/sapi/v1/capital/deposit/hisrec", {
        "txId": reference,
    })

    # This endpoint returns a bare list on success.
    if isinstance(data, dict) and data.get("code"):
        raise BinancePaymentError(f"Binance deposit history error: {data}")

    for dep in data:
        if dep.get("txId") == reference:
            return dep
    return None


def verify_payment(reference: str, expected_amount) -> tuple[bool, str]:
    """Verify a buyer-submitted Binance reference against the admin's own account.

    Args:
        reference: the transactionId (Binance Pay/C2C) or on-chain txid the
            buyer submitted.
        expected_amount: the USD/USDT amount the buyer is expected to have
            paid (Decimal or float).

    Returns:
        (ok, message) — ok is True only if a matching, completed, incoming
        transaction of the right amount was found.
    """
    reference = (reference or "").strip()
    if not reference:
        return False, "No reference provided."

    expected = Decimal(str(expected_amount))

    # Try Binance Pay / C2C history first (this is the expected path for
    # buyers sending to BINANCE_PAY_ID).
    txn = _find_pay_transaction(reference)
    if txn:
        amount = Decimal(str(txn.get("amount", "0")))
        # Positive amount = incoming funds. A negative amount means this was
        # a payment the admin's account SENT, not received — never credit that.
        if amount <= 0:
            return False, "That transaction is an outgoing payment, not an incoming one."
        if abs(amount - expected) > AMOUNT_TOLERANCE:
            return False, (
                f"Amount mismatch: received {amount} but expected {expected}."
            )
        return True, "Verified via Binance Pay history."

    # Fallback: on-chain deposit.
    dep = _find_deposit(reference)
    if dep:
        # status: 0=pending, 6=credited but cannot withdraw, 1=success
        if dep.get("status") != 1:
            return False, "Deposit found but not yet confirmed on-chain. Please wait and resubmit."
        amount = Decimal(str(dep.get("amount", "0")))
        if abs(amount - expected) > AMOUNT_TOLERANCE:
            return False, (
                f"Amount mismatch: received {amount} but expected {expected}."
            )
        return True, "Verified via on-chain deposit history."

    return False, "No matching transaction found on our end yet. Double-check the ID or wait a minute and try again."
