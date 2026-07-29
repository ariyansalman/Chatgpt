"""Crypto Bot API service for cryptocurrency payments."""

import logging
import requests
from config.settings import settings

logger = logging.getLogger(__name__)


class CryptoBotService:
    """Service for integrating with Crypto Bot API for cryptocurrency payments."""

    def __init__(self):
        """Initialize Crypto Bot service with API key."""
        self.api_key = settings.CRYPTO_BOT_API_KEY
        self.base_url = "https://pay.crypt.bot/api"

    def generate_payment_address(self, amount: float, transaction_id: int, crypto_currency: str = None, crypto_network: str = None) -> str:
        """Generate a unique payment invoice that accepts any cryptocurrency.

        Args:
            amount: Amount in USD
            transaction_id: Transaction ID for reference
            crypto_currency: Deprecated - kept for backwards compatibility
            crypto_network: Deprecated - kept for backwards compatibility

        Returns:
            String format: "invoice_id|pay_url" or None if failed
        """
        if not self.api_key:
            logger.warning("CRYPTO_BOT_API_KEY not configured")
            # Return format: "invoice_id|pay_url" with sample data
            return f"{transaction_id}|https://t.me/CryptoBot?start=sample_invoice_{transaction_id}"

        try:
            headers = {
                "Crypto-Pay-API-Token": self.api_key
            }

            # Create invoice in USD that accepts ANY cryptocurrency
            # User can choose which crypto to pay with on CryptoBot payment page
            payload = {
                "currency_type": "fiat",
                "fiat": "USD",
                "amount": str(amount),
                "description": f"Wallet top-up #{transaction_id}",
                "paid_btn_name": "callback",
                "paid_btn_url": f"https://t.me/your_bot?start=payment_{transaction_id}",
                "allow_comments": False,
                "allow_anonymous": False
            }

            response = requests.post(
                f"{self.base_url}/createInvoice",
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                logger.debug("CryptoBot createInvoice response: %s", data)

                if not data.get("ok"):
                    logger.warning("CryptoBot API returned ok=false: %s", data)
                    return None

                result = data.get("result", {})
                # Get invoice ID and payment URL
                invoice_id = result.get("invoice_id", "")  # Numeric ID for API calls
                invoice_hash = result.get("hash", "")      # Hash for URLs
                bot_invoice_url = result.get("bot_invoice_url", "")
                mini_app_url = result.get("mini_app_invoice_url", "")
                pay_url = bot_invoice_url or mini_app_url

                logger.debug("Created invoice: ID=%s, hash=%s, url=%s", invoice_id, invoice_hash, pay_url)

                # Store format: "invoice_id|pay_url" for later verification
                # We need the invoice_id for API calls, and pay_url for user payment
                if invoice_id and pay_url:
                    return f"{invoice_id}|{pay_url}"
                else:
                    logger.warning("Missing invoice_id or pay_url in CryptoBot response: %s", result)
                    return None
            else:
                logger.error("Crypto Bot API error: %s - %s", response.status_code, response.text)
                return None

        except Exception as e:
            logger.exception("Error generating crypto payment invoice: %s", e)
            return None

    def check_payment_status(self, crypto_address: str, expected_amount: float) -> bool:
        """Check if payment has been received for the invoice.

        NOTE: This polling-based approach is a FALLBACK mechanism.
        For REAL-TIME payment notifications, use webhooks instead:
        Configure the CryptoBot webhook to POST to your /webhook/cryptobot endpoint.

        Args:
            crypto_address: Format "invoice_id|pay_url" or legacy format
            expected_amount: Expected payment amount in USD

        Returns:
            True if payment confirmed, False otherwise
        """
        if not self.api_key:
            logger.warning("CRYPTO_BOT_API_KEY not configured")
            return False

        # Extract invoice_id from the stored format
        invoice_id = None
        if crypto_address and "|" in crypto_address:
            # New format: "invoice_id|pay_url"
            parts = crypto_address.split("|", 1)
            invoice_id_str = parts[0]
            try:
                invoice_id = int(invoice_id_str)
                logger.debug("Extracted numeric invoice_id from new format: %s", invoice_id)
            except ValueError:
                logger.warning("Could not parse invoice_id from: %s", invoice_id_str)
                invoice_id = None
        else:
            logger.warning("Old invoice format detected (URL-only). Cannot auto-verify. Admin must manually confirm.")
            return False

        if not invoice_id:
            logger.debug("Skipping check for sample/invalid address: %s", crypto_address)
            return False

        try:
            headers = {"Crypto-Pay-API-Token": self.api_key}
            params = {"invoice_ids": str(invoice_id)}
            logger.debug("Calling getInvoices with params: %s", params)

            response = requests.get(
                f"{self.base_url}/getInvoices",
                headers=headers,
                params=params,
                timeout=10
            )

            logger.debug("CryptoBot API Response: %s", response.status_code)

            if response.status_code == 200:
                data = response.json()
                logger.debug("CryptoBot Response Data: %s", data)

                items = data.get("result", {}).get("items", [])

                if items:
                    invoice = items[0]
                    status = invoice.get("status")
                    paid_at = invoice.get("paid_at")
                    paid_amount = invoice.get("paid_amount")
                    paid_asset = invoice.get("paid_asset")

                    logger.debug(
                        "Invoice %s details: status=%s paid_at=%s paid_amount=%s paid_asset=%s",
                        invoice_id, status, paid_at, paid_amount, paid_asset,
                    )

                    # CryptoBot invoice statuses: active, paid, expired
                    # Primary check: status should be "paid"
                    if status == "paid":
                        logger.info("Invoice %s is PAID (status=paid)", invoice_id)
                        return True

                    # Fallback check: if paid_at exists, payment was received
                    # (This handles cases where blockchain confirmation is pending)
                    if paid_at:
                        logger.info("Invoice %s is PAID (paid_at=%s, status=%s)", invoice_id, paid_at, status)
                        return True

                    logger.debug("Invoice %s status: %s (payment pending)", invoice_id, status)
                    return False
                else:
                    logger.warning("No invoice found with ID: %s", invoice_id)
                    return False
            else:
                logger.error("Crypto Bot API error checking status: %s - %s", response.status_code, response.text)
                return False

        except Exception as e:
            logger.exception("Error checking crypto payment status: %s", e)
            return False
