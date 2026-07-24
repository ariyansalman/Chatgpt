"""Heleket Static Wallet service for reusable automatic crypto deposits."""
from __future__ import annotations
import base64, hashlib, hmac, json, logging
from typing import Optional
import requests
from sqlalchemy.exc import IntegrityError
from config.settings import settings
from database import get_db_session
from database.models import PaymentGatewayConfig, HeleketStaticWallet, User
from utils.bot_config import cfg

logger = logging.getLogger(__name__)
API_BASE_URL = "https://api.heleket.com/v1"
SUPPORTED_ASSETS = {
    "usdt_tron": ("USDT", "tron", "USDT TRC20"),
    "usdt_bsc": ("USDT", "bsc", "USDT BEP20"),
    "btc_btc": ("BTC", "btc", "Bitcoin"),
    "ltc_ltc": ("LTC", "ltc", "Litecoin"),
    "doge_doge": ("DOGE", "doge", "Dogecoin"),
    "sol_sol": ("SOL", "sol", "Solana"),
}
PAID_STATUSES = {"paid", "paid_over"}

def _get_or_create_config(session):
    row = session.query(PaymentGatewayConfig).filter_by(gateway="heleket").first()
    if not row:
        row = PaymentGatewayConfig(gateway="heleket", is_enabled=False)
        session.add(row); session.commit(); session.refresh(row)
    return row

class HeleketPaymentService:
    SOURCE = "heleket_deposit"
    def __init__(self):
        merchant_id = api_key = ""; enabled = False
        try:
            with get_db_session() as session:
                row = _get_or_create_config(session)
                merchant_id, api_key, enabled = row.merchant_uuid or "", row.api_key or "", bool(row.is_enabled)
        except Exception:
            logger.exception("Failed to load Heleket config")
        self.merchant_id = merchant_id or settings.HELEKET_MERCHANT_ID
        self.api_key = api_key or settings.HELEKET_PAYMENT_API_KEY
        self.enabled = enabled
        base_url = (settings.WEBHOOK_URL or "").strip() or cfg.get_str("webhook_base_url", "").strip()
        self.callback_url = f"{base_url.rstrip('/')}/webhook/heleket" if base_url else ""

    def is_configured(self): return bool(self.merchant_id and self.api_key)

    @staticmethod
    def _json_bytes(payload: dict) -> bytes:
        # PHP json_encode-compatible compact JSON. Heleket explicitly requires escaped slashes.
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/").encode("utf-8")

    def _sign(self, payload: dict) -> str:
        # MD5 is mandated by Heleket's signature protocol (not used for
        # password/credential hashing), so usedforsecurity=False documents
        # intent and avoids tripping generic "weak hash" scanners.
        return hashlib.md5(
            base64.b64encode(self._json_bytes(payload)) + self.api_key.encode(),
            usedforsecurity=False,
        ).hexdigest()

    def _headers(self, payload: dict):
        return {"Content-Type":"application/json", "merchant":self.merchant_id, "sign":self._sign(payload)}

    def verify_webhook_signature(self, payload: dict, received_sign: str) -> bool:
        if not self.api_key or not received_sign: return False
        body = {k:v for k,v in payload.items() if k != "sign"}
        return hmac.compare_digest(self._sign(body), received_sign)

    def create_or_get_static_wallet(self, telegram_user_id: int, currency: str, network: str) -> Optional[dict]:
        currency, network = currency.upper(), network.lower()
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=telegram_user_id).first()
            if not user: return None
            existing = session.query(HeleketStaticWallet).filter_by(user_id=user.id, currency=currency, network=network).first()
            if existing:
                logger.info("Reused Heleket static wallet user=%s %s/%s", user.id, currency, network)
                return self._as_dict(existing)
            user_id = user.id
        if not self.is_configured() or not self.callback_url:
            logger.warning("Heleket not configured or WEBHOOK_URL missing")
            return None
        order_id = f"HKU_{telegram_user_id}_{currency}_{network}"[:100]
        payload = {"currency":currency, "network":network, "order_id":order_id, "url_callback":self.callback_url}
        try:
            resp = requests.post(f"{API_BASE_URL}/wallet", headers=self._headers(payload), data=self._json_bytes(payload), timeout=20)
            data = resp.json() if resp.content else {}; result = data.get("result") or {}
            if resp.status_code != 200 or data.get("state") != 0 or not result.get("address") or not result.get("uuid"):
                logger.error("Heleket wallet creation failed status=%s response=%s", resp.status_code, data)
                return None
            with get_db_session() as session:
                row = HeleketStaticWallet(user_id=user_id, telegram_user_id=telegram_user_id, order_id=result.get("order_id") or order_id,
                    heleket_wallet_uuid=result.get("wallet_uuid"), wallet_address_uuid=result["uuid"], address=result["address"],
                    currency=result.get("currency") or currency, network=result.get("network") or network)
                session.add(row)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    row = session.query(HeleketStaticWallet).filter_by(user_id=user_id, currency=currency, network=network).first()
                    return self._as_dict(row) if row else None
                logger.info("Created Heleket static wallet user=%s %s/%s", user_id, currency, network)
                return self._as_dict(row)
        except Exception:
            logger.exception("Heleket static wallet creation error")
            return None

    @staticmethod
    def _as_dict(row):
        return {"order_id":row.order_id,"wallet_uuid":row.heleket_wallet_uuid,"wallet_address_uuid":row.wallet_address_uuid,
                "address":row.address,"currency":row.currency,"network":row.network}
