"""ZiniPay payment service (bKash / Nagad / Rocket via hosted checkout).

Flow:
1. create_invoice() -> hosted payment_url; buyer picks bKash/Nagad/Rocket
   on ZiniPay's own checkout page and pays.
2. ZiniPay calls our webhook (see webhook_server.py) with {invoice_id, status}.
3. Per ZiniPay's own recommendation, the webhook body is NOT trusted as
   proof of payment by itself — verify_invoice() is always called from our
   backend before crediting anything. The same verify_invoice() call also
   backs the polling fallback in payment_handlers.check_pending_payments,
   in case a webhook delivery is lost.

Docs: https://zinipay.com/docs
"""

from decimal import Decimal
from urllib.parse import urlparse

import requests

from config.settings import settings

BASE_URL = "https://api.zinipay.com"
AMOUNT_TOLERANCE = Decimal("0.01")


class ZiniPayError(Exception):
    """Raised when the ZiniPay API call itself fails (network, auth, etc)."""


def _headers():
    if not settings.ZINIPAY_API_KEY:
        raise ZiniPayError("ZiniPay API key is not configured.")
    return {
        "Content-Type": "application/json",
        "zini-api-key": settings.ZINIPAY_API_KEY,
    }


def create_invoice(amount, transaction_id: int, webhook_url: str = None) -> tuple[str, str]:
    """Create a hosted ZiniPay invoice.

    Returns:
        (invoice_id, payment_url)

    Raises:
        ZiniPayError on any failure.
    """
    payload = {
        "amount": float(amount),
        "metadata": {"order_id": str(transaction_id)},
        "redirect_url": settings.ZINIPAY_REDIRECT_URL,
        "cancel_url": settings.ZINIPAY_CANCEL_URL,
    }
    if webhook_url:
        payload["webhook_url"] = webhook_url

    try:
        response = requests.post(
            f"{BASE_URL}/v1/payment/create",
            headers=_headers(),
            json=payload,
            timeout=15
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay API: {e}")

    if response.status_code != 200:
        raise ZiniPayError(f"ZiniPay API error {response.status_code}: {response.text[:300]}")

    data = response.json()
    if not data.get("status"):
        raise ZiniPayError(f"ZiniPay invoice creation failed: {data.get('message')}")

    payment_url = data.get("payment_url", "")
    if not payment_url:
        raise ZiniPayError("ZiniPay response missing payment_url.")

    # The Create Invoice response doesn't return invoice_id directly — it's
    # the last path segment of payment_url
    # (https://secure.zinipay.com/payment/INVOICE_ID).
    invoice_id = urlparse(payment_url).path.rstrip('/').split('/')[-1]
    if not invoice_id:
        raise ZiniPayError(f"Could not extract invoice_id from payment_url: {payment_url}")

    return invoice_id, payment_url


def verify_invoice(invoice_id: str) -> dict:
    """Verify an invoice's current status directly from ZiniPay's backend.

    Returns the raw response dict: cus_name, cus_email, amount, invoice_id,
    payment_method, transaction_id, status (PENDING/COMPLETED/FAILED).

    Raises:
        ZiniPayError on any failure.
    """
    try:
        response = requests.post(
            f"{BASE_URL}/v1/payment/verify",
            headers=_headers(),
            json={"invoice_id": invoice_id},
            timeout=15
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay API: {e}")

    if response.status_code != 200:
        raise ZiniPayError(f"ZiniPay API error {response.status_code}: {response.text[:300]}")

    return response.json()


def is_paid(invoice_id: str, expected_amount) -> bool:
    """Convenience check used by the polling job: COMPLETED and amount matches."""
    try:
        data = verify_invoice(invoice_id)
    except ZiniPayError as e:
        print(f"ZiniPay verify error for {invoice_id}: {e}")
        return False

    if data.get("status") != "COMPLETED":
        return False

    amount = Decimal(str(data.get("amount", "0")))
    expected = Decimal(str(expected_amount))
    return abs(amount - expected) <= AMOUNT_TOLERANCE
