"""Configuration settings loader from environment variables."""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Settings:
    """Stores all configuration settings for the bot."""

    # Telegram Bot Settings
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_TELEGRAM_ID = int(os.getenv('ADMIN_TELEGRAM_ID', 0))
    ADMIN_TELEGRAM_USERNAME = os.getenv('ADMIN_TELEGRAM_USERNAME', '')

    # Admin 2FA (OTP session verification) — global on/off switch.
    # When False (current default), /admin_login + the OTP-send flow still
    # exist and work (utils/permissions.py, handlers/admin_auth.py), but
    # NOTHING enforces having a verified session anymore — any active admin
    # (is_admin() true) can use every admin feature immediately, with no
    # "🔒 Your admin session expired" prompt. Flip to True to re-enable
    # enforcement without touching any other code.
    ADMIN_2FA_ENABLED = os.getenv('ADMIN_2FA_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

    # Database Settings
    # Production: Supabase PostgreSQL connection string, e.g.
    #   postgresql://USER:PASSWORD@HOST:PORT/postgres
    # Local dev fallback (no DATABASE_URL set): a local SQLite file.
    # Some hosts (Supabase, Heroku-style) issue "postgres://" — SQLAlchemy /
    # psycopg2 require "postgresql://", so normalize it here at load time.
    # SUPABASE_DB_URL takes priority (avoids conflict with Replit-managed DATABASE_URL)
    _raw_database_url = os.getenv('SUPABASE_DB_URL') or os.getenv('DATABASE_URL', 'sqlite:///bot_database.db')
    if _raw_database_url.startswith('postgres://'):
        _raw_database_url = 'postgresql://' + _raw_database_url[len('postgres://'):]
    DATABASE_URL = _raw_database_url

    # Crypto Payment Settings
    CRYPTO_BOT_API_KEY = os.getenv('CRYPTO_BOT_API_KEY', '')

    # bKash Tokenized Checkout Settings (Personal / Merchant API)
    # These are only FALLBACKS — the admin can set/override all of these
    # from the Telegram admin panel (Payment Gateways section), which takes
    # priority. Useful for first-run before the admin configures anything.
    BKASH_MODE = os.getenv('BKASH_MODE', 'sandbox')  # 'sandbox' or 'live'
    BKASH_APP_KEY = os.getenv('BKASH_APP_KEY', '')
    BKASH_APP_SECRET = os.getenv('BKASH_APP_SECRET', '')
    BKASH_USERNAME = os.getenv('BKASH_USERNAME', '')
    BKASH_PASSWORD = os.getenv('BKASH_PASSWORD', '')

    # Nagad Merchant Checkout Settings
    # Same fallback rule as bKash above.
    NAGAD_MODE = os.getenv('NAGAD_MODE', 'sandbox')  # 'sandbox' or 'live'
    NAGAD_MERCHANT_ID = os.getenv('NAGAD_MERCHANT_ID', '')
    NAGAD_MERCHANT_NUMBER = os.getenv('NAGAD_MERCHANT_NUMBER', '')
    NAGAD_PUBLIC_KEY = os.getenv('NAGAD_PUBLIC_KEY', '')   # Nagad's PG public key (PEM or base64 body)
    NAGAD_PRIVATE_KEY = os.getenv('NAGAD_PRIVATE_KEY', '')  # Merchant private key (PEM or base64 body)

    # Cryptomus (USDT / crypto) Settings — used instead of @CryptoBot where
    # that isn't available (e.g. Bangladesh). Same fallback rule as bKash/Nagad:
    # the admin panel value (PaymentGatewayConfig, gateway="cryptomus") wins.
    CRYPTOMUS_MERCHANT_UUID = os.getenv('CRYPTOMUS_MERCHANT_UUID', '')
    CRYPTOMUS_API_KEY = os.getenv('CRYPTOMUS_API_KEY', '')

    # Heleket Static Wallet automatic crypto top-ups. Admin-panel values win.
    HELEKET_MERCHANT_ID = os.getenv('HELEKET_MERCHANT_ID', '')
    HELEKET_PAYMENT_API_KEY = os.getenv('HELEKET_PAYMENT_API_KEY', '')

    # NOWPayments (https://nowpayments.io) — crypto invoice gateway.
    NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY', '')
    NOWPAYMENTS_IPN_SECRET = os.getenv('NOWPAYMENTS_IPN_SECRET', '')

    # ZiniPay (https://zinipay.com) — bKash/Nagad/Rocket payment automation (BD).
    ZINIPAY_API_KEY = os.getenv('ZINIPAY_API_KEY', '')

    # Binance Pay (verified via the normal Binance HMAC API, NOT the Binance
    # Pay Merchant API — see services/binance_pay.py). READ-ONLY: only used
    # to call GET /sapi/v1/pay/transactions. These must never be entered
    # through the Telegram admin panel and are never stored in the database —
    # env vars only, by design (see handlers/admin_binance.py).
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')

    # Bybit Pay (verified via the official Bybit V5 REST API — see
    # services/bybit_pay.py). READ-ONLY: only used to call
    # GET /v5/asset/deposit/query-internal-record (UID Transfer) and
    # GET /v5/asset/deposit/query-record (on-chain deposit). These must
    # never be entered through the Telegram admin panel and are never
    # stored in the database — env vars only, by design (see
    # handlers/admin_bybit.py).
    BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '').strip()
    BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '').strip()

    # Telegram Payments (Card) Settings
    # Provider token from @BotFather → your bot → Payments → connect a provider.
    TELEGRAM_PROVIDER_TOKEN = os.getenv('TELEGRAM_PROVIDER_TOKEN', '')
    # Currency the card invoice is charged in. The numeric amount equals the USD
    # top-up value, so this must be a USD-denominated provider for amounts to match.
    PAYMENT_CURRENCY = os.getenv('PAYMENT_CURRENCY', 'USD')

    # Application Settings
    PAYMENT_EXPIRY_HOURS = 0.5  # Payment order expiration time (30 minutes)
    PAYMENT_CHECK_INTERVAL = 30  # Seconds between payment verification checks

    # Asset Storage
    ASSETS_DIR = 'assets'
    LOGOS_DIR = os.path.join(ASSETS_DIR, 'logos')
    PRODUCTS_DIR = os.path.join(ASSETS_DIR, 'products')

    # Runtime Mode (Phase 4) — 'polling' (default) or 'webhook'
    RUN_MODE = os.getenv('RUN_MODE', 'polling').lower()
    WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')          # e.g. https://bot.example.com
    WEBHOOK_PATH = os.getenv('WEBHOOK_PATH', '/telegram')
    WEBHOOK_LISTEN = os.getenv('WEBHOOK_LISTEN', '0.0.0.0')
    WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8443'))
    WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')    # optional secret token



# Create settings instance
settings = Settings()


def validate_settings():
    """Validates that all required settings are configured."""
    if not settings.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required in .env file")

    if not settings.ADMIN_TELEGRAM_ID:
        raise ValueError("ADMIN_TELEGRAM_ID is required in .env file")

    print("[OK] Configuration validated successfully")
