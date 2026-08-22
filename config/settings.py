"""Configuration settings loader from environment variables."""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _normalize_database_url(url: str) -> str:
    """Normalize a DATABASE_URL for SQLAlchemy 2.x compatibility.

    Supabase/Railway/Heroku-style connection strings are sometimes given
    with the legacy 'postgres://' scheme, which psycopg2 accepts but
    SQLAlchemy 2.x rejects outright. Rewrite it to 'postgresql://'.
    """
    if url.startswith('postgres://'):
        return 'postgresql://' + url[len('postgres://'):]
    return url


class Settings:
    """Stores all configuration settings for the bot."""

    # Telegram Bot Settings
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_TELEGRAM_ID = int(os.getenv('ADMIN_TELEGRAM_ID', 0))
    ADMIN_TELEGRAM_USERNAME = os.getenv('ADMIN_TELEGRAM_USERNAME', '')

    # Database Settings
    # Local dev defaults to SQLite. In production (Railway), set DATABASE_URL
    # to your Supabase Postgres connection string, e.g.:
    #   postgresql://postgres.xxxxxxxx:[email protected]:5432/postgres
    # Use Supabase's "Session pooler" or direct connection string (port 5432),
    # NOT the "Transaction pooler" (port 6543) — this bot holds a persistent
    # connection pool as a long-running worker, which the transaction pooler
    # is not designed for.
    DATABASE_URL = _normalize_database_url(
        os.getenv('DATABASE_URL', 'sqlite:///bot_database.db')
    )

    # Crypto Payment Settings
    CRYPTO_BOT_API_KEY = os.getenv('CRYPTO_BOT_API_KEY', '')

    # Telegram Payments (Card) Settings
    # Provider token from @BotFather → your bot → Payments → connect a provider.
    TELEGRAM_PROVIDER_TOKEN = os.getenv('TELEGRAM_PROVIDER_TOKEN', '')
    # Currency the card invoice is charged in. The numeric amount equals the USD
    # top-up value, so this must be a USD-denominated provider for amounts to match.
    PAYMENT_CURRENCY = os.getenv('PAYMENT_CURRENCY', 'USD')

    # Binance Pay Settings
    # API key/secret from a REGULAR (non-merchant) Binance account:
    # binance.com -> Account -> API Management -> Create API. Only needs the
    # default read permissions (no trading/withdrawal). Used to call
    # GET /sapi/v1/pay/transactions and GET /sapi/v1/capital/deposit/hisrec
    # on YOUR OWN account to confirm an incoming payment a buyer claims to
    # have sent.
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    # The Binance ID / Pay ID shown to buyers as the "send to" address for
    # Binance Pay / internal transfers.
    BINANCE_PAY_ID = os.getenv('BINANCE_PAY_ID', '')
    # Optional: an on-chain deposit address as a fallback for buyers who
    # can't use Binance Pay directly (e.g. USDT-TRC20 address).
    BINANCE_DEPOSIT_ADDRESS = os.getenv('BINANCE_DEPOSIT_ADDRESS', '')
    BINANCE_DEPOSIT_COIN = os.getenv('BINANCE_DEPOSIT_COIN', 'USDT')

    # Bybit Pay Settings
    # API key/secret from a REGULAR (non-merchant) Bybit account:
    # bybit.com -> Account -> API -> Create New Key. Read-only permissions
    # are enough (Wallet -> read). Used to call
    # GET /v5/asset/deposit/query-internal-record and
    # GET /v5/asset/deposit/query-record on YOUR OWN account.
    BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
    BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')
    # The Bybit UID or email shown to buyers as the "send to" address for
    # an internal (off-chain, no fee) Bybit-to-Bybit transfer.
    BYBIT_UID = os.getenv('BYBIT_UID', '')
    # Optional: an on-chain deposit address as a fallback.
    BYBIT_DEPOSIT_ADDRESS = os.getenv('BYBIT_DEPOSIT_ADDRESS', '')
    BYBIT_DEPOSIT_COIN = os.getenv('BYBIT_DEPOSIT_COIN', 'USDT')
    BYBIT_DEPOSIT_CHAIN = os.getenv('BYBIT_DEPOSIT_CHAIN', 'TRX')

    # ZiniPay (bKash / Nagad / Rocket) Settings
    # API key from your ZiniPay dashboard -> Brands -> Brand Key / API Key.
    ZINIPAY_API_KEY = os.getenv('ZINIPAY_API_KEY', '')
    # Public HTTPS base URL where webhook_server.py is reachable (same host
    # already used for the CryptoBot webhook, e.g. your Railway domain).
    # Used to build the webhook_url sent to ZiniPay, and the redirect/cancel
    # pages ZiniPay sends the buyer back to after paying.
    PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
    # redirect_url's domain must match the website domain registered against
    # your ZiniPay brand — if PUBLIC_BASE_URL's domain isn't registered
    # there, override these two explicitly with a URL on your registered
    # domain instead.
    ZINIPAY_REDIRECT_URL = os.getenv('ZINIPAY_REDIRECT_URL', '') or f"{PUBLIC_BASE_URL}/zinipay/success"
    ZINIPAY_CANCEL_URL = os.getenv('ZINIPAY_CANCEL_URL', '') or f"{PUBLIC_BASE_URL}/zinipay/cancel"

    # Application Settings
    PAYMENT_EXPIRY_HOURS = 0.5  # Payment order expiration time (30 minutes)
    PAYMENT_CHECK_INTERVAL = 30  # Seconds between payment verification checks

    # Asset Storage
    # STORAGE_DIR lets you point saved images (product photos, store logo,
    # broadcast images, restocked key uploads) at a persistent location.
    # On Railway the container filesystem is wiped on every redeploy, so for
    # production, attach a Railway Volume (e.g. mounted at /data) and set
    # STORAGE_DIR=/data as an environment variable. Left unset, everything
    # defaults to the project directory, which is fine for local development
    # but NOT persistent in production.
    STORAGE_DIR = os.getenv('STORAGE_DIR', '.')
    ASSETS_DIR = os.path.join(STORAGE_DIR, 'assets')
    LOGOS_DIR = os.path.join(ASSETS_DIR, 'logos')
    PRODUCTS_DIR = os.path.join(ASSETS_DIR, 'products')
    UPLOADS_DIR = os.path.join(STORAGE_DIR, 'uploads')


# Create settings instance
settings = Settings()


def validate_settings():
    """Validates that all required settings are configured."""
    if not settings.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required in .env file")

    if not settings.ADMIN_TELEGRAM_ID:
        raise ValueError("ADMIN_TELEGRAM_ID is required in .env file")

    print("[OK] Configuration validated successfully")
