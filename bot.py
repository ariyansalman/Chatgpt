"""Main bot entry point for the Telegram Digital Products Store."""

import logging
import warnings
# Suppress PTBUserWarning about per_message=False in ConversationHandlers.
# These ConversationHandlers intentionally track state per-user (the correct
# behaviour for a store bot), so the warning is expected and harmless.
warnings.filterwarnings(
    "ignore",
    message=".*per_message=False.*CallbackQueryHandler.*",
    category=UserWarning,
)
# datetime.utcnow() is deprecated in Python 3.12+; the codebase uses it
# extensively. Suppress the noise until a full migration is done.
warnings.filterwarnings(
    "ignore",
    message=".*utcnow.*deprecated.*",
    category=DeprecationWarning,
)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler, PreCheckoutQueryHandler
from utils import global_button_colors  # noqa: F401 -- patches InlineKeyboardButton so every button in the bot gets a color, not just the main menu
from config import settings, validate_settings
from database.init_data import initialize_database
from handlers import (
    user_handlers, admin_handlers, payment_handlers, admin_conversations, dispute_handlers,
    referral_handlers, support_handlers,
    search_handlers, coupon_handlers,
    loyalty_handlers, review_handlers, analytics_handlers,
)
from handlers import admin_payment_methods as admin_pm
from handlers import admin_config_handlers as admin_cfg
from handlers import admin_dashboard as admin_dash
from handlers import variant_handlers, cart_handlers
from handlers import admin_redelivery, admin_badges, wallet_handlers
from handlers import admin_order_search as _aos
from handlers import admin_broadcast_center
from handlers import admin_auth
from handlers import admin_delivery_format
from handlers import feature_handlers, admin_features
from handlers import account_features, admin_account_features
from handlers import admin_menu_manager
from handlers import admin_activity_feed
from services import inventory as inventory_svc
from utils.logging_config import setup_logging
from utils.error_handler import global_error_handler
from utils.safe_conversation import cancel_command
from utils.bot_config import cfg, seed_defaults
from i18n import SUPPORTED_LANGUAGES
from telegram.ext import TypeHandler
from telegram import Update as _TgUpdate

# Configure structured logging (console + rotating files)
setup_logging()
logger = logging.getLogger(__name__)


async def _track_activity(update, context):
    """Best-effort ``User.last_seen_at`` touch — feeds win-back detection in
    ``services/marketing_automation.py``. Runs before all handlers, never
    blocks, and throttles writes (skips if we touched this user in the last
    5 minutes) so it doesn't add a DB write to every single update.

    V19: Also maintains UserSession and logs first-seen login events.
    """
    try:
        user = getattr(update, "effective_user", None)
        if not user:
            return
        import asyncio
        from datetime import datetime, timedelta
        from database import get_db_session, User as _User

        is_new_session = False

        def _touch():
            nonlocal is_new_session
            with get_db_session() as s:
                row = s.query(_User).filter_by(telegram_id=user.id).first()
                if not row:
                    return
                now = datetime.utcnow()
                throttle = timedelta(minutes=5)
                if row.last_seen_at and (now - row.last_seen_at) < throttle:
                    return
                if not row.last_seen_at:
                    is_new_session = True
                elif (now - row.last_seen_at) > timedelta(hours=12):
                    is_new_session = True
                row.last_seen_at = now

        await asyncio.to_thread(_touch)

        # V19 — session + login activity logging (best-effort, non-blocking)
        try:
            from database import get_db_session as _gds, User as _DbUser
            account_features.ensure_session(user.id)
            if is_new_session:
                with _gds() as _s:
                    _u = _s.query(_DbUser).filter_by(telegram_id=user.id).first()
                    if _u:
                        account_features.log_activity(
                            _u.id, "login",
                            details=f"Telegram user @{user.username or user.first_name}",
                        )
        except Exception:
            logger.debug("V19 session/login tracking failed", exc_info=True)

        # V32 — Login Activity & Device Management (best-effort, non-blocking)
        if is_new_session:
            try:
                from services.login_activity import (
                    record_login as _rl,
                    send_new_login_alert as _alert,
                )
                from database import get_db_session as _gds32, User as _DbUser32
                import asyncio as _aio32
                _result = None
                with _gds32() as _s32:
                    _u32 = _s32.query(_DbUser32).filter_by(telegram_id=user.id).first()
                    if _u32:
                        _result = _rl(user, _u32.id)
                if _result:
                    _rid, _is_nd, _is_sus = _result
                    _aio32.create_task(
                        _alert(
                            context.bot,
                            telegram_id=user.id,
                            is_new_device=_is_nd,
                            is_suspicious=_is_sus,
                            language_code=getattr(user, "language_code", None),
                            created_at=datetime.utcnow(),
                        )
                    )
            except Exception:
                logger.debug("V32 login activity tracking failed", exc_info=True)

    except Exception:
        logger.debug("activity tracking failed", exc_info=True)


async def _maintenance_gate(update, context):
    """Block non-admin traffic when maintenance mode is on. Runs before all handlers."""
    from telegram.ext import ApplicationHandlerStop
    try:
        if not cfg.get_bool("maintenance_mode", False):
            return
        user = getattr(update, "effective_user", None)
        if user and user.id == settings.ADMIN_TELEGRAM_ID:
            return
        # V20: Whitelist bypass — comma-separated Telegram user IDs in bot_config
        if user:
            try:
                wl_raw = cfg.get_str("maintenance_whitelist", "")
                if wl_raw:
                    wl_ids = [int(x.strip()) for x in wl_raw.split(",")
                              if x.strip().isdigit()]
                    if user.id in wl_ids:
                        return
            except Exception:
                pass
        chat = getattr(update, "effective_chat", None)
        if chat:
            try:
                base_msg = cfg.get_str(
                    "maintenance_message",
                    "🔧 The bot is under maintenance. Please try again shortly.",
                )
                # V20: Append estimated return time if set
                return_time = cfg.get_str("maintenance_estimated_return", "")
                msg = base_msg
                if return_time:
                    msg += f"\n\n⏰ <b>Estimated return:</b> {return_time}"
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=msg,
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception:
        return
    raise ApplicationHandlerStop



def _apply_pending_migrations():
    """Auto-apply missing database columns/enum values on every startup.

    Safe to run repeatedly — every statement uses IF NOT EXISTS so duplicate
    runs are harmless.  Covers:
      • PaymentMethod / TransactionStatus enum values missing from the DB type
      • ZiniPay wallet number columns added in the 20260723 migration
    """
    import psycopg2
    from config.settings import settings as _s

    db_url = getattr(_s, "DATABASE_URL", "") or ""
    if not db_url or "sqlite" in db_url.lower():
        return  # SQLite — no native enum types, nothing to do

    logger.info("[AUTO-MIGRATION] Connecting to apply pending schema changes …")
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True          # Required for ALTER TYPE ADD VALUE
        cur = conn.cursor()
    except Exception as exc:
        logger.error("[AUTO-MIGRATION] Could not connect: %s", exc)
        raise

    def _run(label, sql):
        try:
            cur.execute(sql)
            logger.info("[AUTO-MIGRATION] ✓ %s", label)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "duplicate" in msg:
                logger.debug("[AUTO-MIGRATION] – %s (already present)", label)
            else:
                logger.warning("[AUTO-MIGRATION] ✗ %s — %s", label, exc)

    # ── GiftCardType enum (create if missing, then add all values) ────────
    # Use a DO block to safely create the enum only if it doesn't exist,
    # because CREATE TYPE IF NOT EXISTS is not universally supported.
    _run("ensure giftcardtype enum exists",
         """
         DO $$ BEGIN
             IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'giftcardtype') THEN
                 CREATE TYPE giftcardtype AS ENUM ('fixed', 'percent', 'custom');
             END IF;
         END $$
         """)
    for val in ("fixed", "percent", "custom"):
        _run(f"giftcardtype ← {val}",
             f"ALTER TYPE giftcardtype ADD VALUE IF NOT EXISTS '{val}'")

    # ── Enum values ────────────────────────────────────────────────────
    # Use lowercase values matching the Python PaymentMethod enum .value strings
    for val in ("stars", "cryptomus", "nowpayments", "zinipay", "binance_pay", "heleket"):
        _run(f"paymentmethod ← {val}",
             f"ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS '{val}'")

    for val in ("AWAITING_CONFIRMATION", "REJECTED"):
        _run(f"transactionstatus ← {val}",
             f"ALTER TYPE transactionstatus ADD VALUE IF NOT EXISTS '{val}'")

    # ── ZiniPay wallet columns ─────────────────────────────────────────
    cols = [
        ("zinipay_bkash_number",     "VARCHAR(120)", "NULL"),
        ("zinipay_nagad_number",     "VARCHAR(120)", "NULL"),
        ("zinipay_rocket_number",    "VARCHAR(120)", "NULL"),
        ("zinipay_upay_number",      "VARCHAR(120)", "NULL"),
        ("zinipay_default_provider", "VARCHAR(10)",  "'bkash'"),
        ("zinipay_usd_to_bdt_rate",  "FLOAT",        "NULL"),
        ("zinipay_auto_rate",        "BOOLEAN",      "FALSE"),
        ("zinipay_instructions",     "TEXT",         "NULL"),
    ]
    for col, ctype, default in cols:
        _run(col,
             f"ALTER TABLE payment_gateway_configs "
             f"ADD COLUMN IF NOT EXISTS {col} {ctype} DEFAULT {default}")

    # ── Binance / Bybit admin-configurable API key columns ──────────────
    for col in ("binance_api_key", "binance_api_secret",
                "bybit_api_key",   "bybit_api_secret"):
        _run(col,
             f"ALTER TABLE payment_gateway_configs "
             f"ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT NULL")

    # ── Bybit LTC (Litecoin) deposit address column ──────────────────────
    _run("bybit_wallet_ltc",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_ltc VARCHAR(255) DEFAULT NULL")

    # ── Bybit AVAXC (USDT Avalanche C-Chain) deposit address column ──────
    _run("bybit_wallet_avaxc",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_avaxc VARCHAR(255) DEFAULT NULL")

    # ── Bybit TON (USDT TON) deposit address column ──────────────────────
    _run("bybit_wallet_ton",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_ton VARCHAR(255) DEFAULT NULL")

    # ── Bybit BASE (USDT Base / Coinbase Base L2) deposit address column ─
    _run("bybit_wallet_base",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_base VARCHAR(255) DEFAULT NULL")

    # ── Bybit ARBONE (USDT Arbitrum One) deposit address column ──────────
    _run("bybit_wallet_arb",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_arb VARCHAR(255) DEFAULT NULL")

    # ── Bybit OP (USDT Optimism) deposit address column ───────────────────
    _run("bybit_wallet_op",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_op VARCHAR(255) DEFAULT NULL")

    # ── Bybit MATIC (USDT Polygon) deposit address column ─────────────────
    _run("bybit_wallet_matic",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_matic VARCHAR(255) DEFAULT NULL")

    # ── Bybit SOL (USDT Solana) deposit address column ────────────────────
    _run("bybit_wallet_sol",
         "ALTER TABLE payment_gateway_configs "
         "ADD COLUMN IF NOT EXISTS bybit_wallet_sol VARCHAR(255) DEFAULT NULL")

    # ── Locked exchange rate columns for non-stablecoin orders (LTC etc.) ─
    for col, coltype in (("locked_crypto_rate", "DOUBLE PRECISION"), ("locked_crypto_amount", "DOUBLE PRECISION")):
        _run(col,
             f"ALTER TABLE transactions "
             f"ADD COLUMN IF NOT EXISTS {col} {coltype} DEFAULT NULL")

    # ── Verification audit-log table ────────────────────────────────────
    _run("CREATE payment_verification_log", """
        CREATE TABLE IF NOT EXISTS payment_verification_log (
            id                SERIAL PRIMARY KEY,
            gateway           VARCHAR(32)  NOT NULL,
            telegram_user_id  BIGINT       NOT NULL,
            internal_order_id INTEGER      NOT NULL,
            submitted_txid    VARCHAR(256) NOT NULL,
            outcome           VARCHAR(64)  NOT NULL,
            detail            TEXT,
            ip_hash           VARCHAR(64),
            created_at        TIMESTAMP    NOT NULL DEFAULT NOW()
        )
    """)
    for idx, col in (
        ("ix_pvl_gateway", "gateway"),
        ("ix_pvl_user",    "telegram_user_id"),
        ("ix_pvl_order",   "internal_order_id"),
    ):
        _run(f"idx payment_verification_log.{col}",
             f"CREATE INDEX IF NOT EXISTS {idx} ON payment_verification_log({col})")

    # ── Pending-manual-verifications table (admin approve/reject queue) ──
    _run("CREATE pending_manual_verifications", """
        CREATE TABLE IF NOT EXISTS pending_manual_verifications (
            id                SERIAL PRIMARY KEY,
            gateway           VARCHAR(32)   NOT NULL,
            telegram_user_id  BIGINT        NOT NULL,
            internal_order_id INTEGER       NOT NULL,
            submitted_txid    VARCHAR(256)  NOT NULL,
            amount            NUMERIC(20,8) NOT NULL,
            currency          VARCHAR(16)   NOT NULL,
            payment_type      VARCHAR(32),
            network           VARCHAR(16),
            auto_outcome      VARCHAR(64),
            auto_detail       TEXT,
            status            VARCHAR(16)   NOT NULL DEFAULT 'pending',
            admin_note        TEXT,
            created_at        TIMESTAMP     NOT NULL DEFAULT NOW(),
            resolved_at       TIMESTAMP,
            UNIQUE (gateway, internal_order_id, submitted_txid)
        )
    """)
    for idx, col in (
        ("ix_pmv_gateway", "gateway"),
        ("ix_pmv_user",    "telegram_user_id"),
        ("ix_pmv_order",   "internal_order_id"),
    ):
        _run(f"idx pending_manual_verifications.{col}",
             f"CREATE INDEX IF NOT EXISTS {idx} ON pending_manual_verifications({col})")

    # ── V18: User features tables (safe IF NOT EXISTS guards) ──────────────
    for tbl, pk, extra in [
        ("user_wishlists",
         "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
         "product_id INTEGER REFERENCES products(id), created_at TIMESTAMP DEFAULT NOW()",
         "UNIQUE (user_id, product_id)"),
        ("price_drop_alerts",
         "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
         "product_id INTEGER REFERENCES products(id), subscribed_at TIMESTAMP DEFAULT NOW(), "
         "last_notified_price FLOAT",
         "UNIQUE (user_id, product_id)"),
        ("recently_viewed",
         "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
         "product_id INTEGER REFERENCES products(id), viewed_at TIMESTAMP DEFAULT NOW()",
         "UNIQUE (user_id, product_id)"),
        ("quick_buy_configs",
         "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
         "product_id INTEGER REFERENCES products(id), payment_method VARCHAR(64), "
         "quantity INTEGER DEFAULT 1, last_used_at TIMESTAMP DEFAULT NOW()",
         "UNIQUE (user_id, product_id)"),
        ("preferred_payments",
         "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) UNIQUE, "
         "payment_method VARCHAR(64) NOT NULL, set_at TIMESTAMP DEFAULT NOW()",
         ""),
    ]:
        cols = pk + (f", {extra}" if extra else "")
        _run(f"CREATE {tbl}",
             f"CREATE TABLE IF NOT EXISTS {tbl} ({cols})")

    # ── V19: Account & Order Features tables (safe IF NOT EXISTS guards) ─────
    for tbl, cols in [
        ("order_receipts",
         "id SERIAL PRIMARY KEY, "
         "receipt_number VARCHAR(32) NOT NULL UNIQUE, "
         "order_id INTEGER REFERENCES orders(id), "
         "transaction_id INTEGER REFERENCES transactions(id), "
         "user_id INTEGER NOT NULL REFERENCES users(id), "
         "receipt_type VARCHAR(16) NOT NULL DEFAULT 'purchase', "
         "created_at TIMESTAMP DEFAULT NOW()"),
        ("user_downloads",
         "id SERIAL PRIMARY KEY, "
         "user_id INTEGER NOT NULL REFERENCES users(id), "
         "order_id INTEGER NOT NULL REFERENCES orders(id), "
         "order_item_id INTEGER NOT NULL REFERENCES order_items(id), "
         "product_id INTEGER NOT NULL REFERENCES products(id), "
         "product_name VARCHAR(255) NOT NULL, "
         "asset_type VARCHAR(32) NOT NULL DEFAULT 'key', "
         "download_count INTEGER DEFAULT 0, "
         "last_downloaded_at TIMESTAMP, "
         "expires_at TIMESTAMP, "
         "created_at TIMESTAMP DEFAULT NOW(), "
         "UNIQUE (user_id, order_item_id)"),
        ("activity_logs",
         "id SERIAL PRIMARY KEY, "
         "user_id INTEGER NOT NULL REFERENCES users(id), "
         "action VARCHAR(64) NOT NULL, "
         "status VARCHAR(16) NOT NULL DEFAULT 'success', "
         "details TEXT, "
         "ref_type VARCHAR(32), "
         "ref_id VARCHAR(64), "
         "created_at TIMESTAMP DEFAULT NOW()"),
        ("user_sessions",
         "id SERIAL PRIMARY KEY, "
         "user_id INTEGER NOT NULL REFERENCES users(id), "
         "session_token VARCHAR(64) NOT NULL UNIQUE, "
         "device_info VARCHAR(255), "
         "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
         "created_at TIMESTAMP DEFAULT NOW(), "
         "last_active_at TIMESTAMP DEFAULT NOW(), "
         "terminated_at TIMESTAMP"),
    ]:
        _run(f"CREATE {tbl}",
             f"CREATE TABLE IF NOT EXISTS {tbl} ({cols})")

    # V19 indexes (IF NOT EXISTS)
    for idx, tbl, col, uniq in [
        ("ix_or_receipt_number",  "order_receipts",  "receipt_number", True),
        ("ix_or_user_id",         "order_receipts",  "user_id",        False),
        ("ix_ud_user_id",         "user_downloads",  "user_id",        False),
        ("ix_ud_order_item_id",   "user_downloads",  "order_item_id",  False),
        ("ix_al_user_id",         "activity_logs",   "user_id",        False),
        ("ix_al_action",          "activity_logs",   "action",         False),
        ("ix_al_created_at",      "activity_logs",   "created_at",     False),
        ("ix_us_user_id",         "user_sessions",   "user_id",        False),
        ("ix_us_session_token",   "user_sessions",   "session_token",  True),
        ("ix_us_is_active",       "user_sessions",   "is_active",      False),
    ]:
        uniq_sql = "UNIQUE " if uniq else ""
        _run(f"idx {tbl}.{col}",
             f"CREATE {uniq_sql}INDEX IF NOT EXISTS {idx} ON {tbl}({col})")

    # ── V20: Advanced Features migration ────────────────────────────────────
    # Additive columns on existing tables
    for (t, col, ddl) in [
        ("support_tickets", "category",           "VARCHAR(32) DEFAULT 'general'"),
        ("support_tickets", "assigned_admin_id",  "BIGINT"),
        ("support_tickets", "ticket_number",      "VARCHAR(20)"),
        ("ticket_messages", "file_id",            "VARCHAR(256)"),
        ("ticket_messages", "file_type",          "VARCHAR(16)"),
        ("low_stock_alert_state", "silent_mode",          "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("low_stock_alert_state", "custom_threshold",     "INTEGER"),
        ("low_stock_alert_state", "fast_sell_alert_sent", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("low_stock_alert_state", "fast_sell_sales_count","INTEGER DEFAULT 0"),
        ("low_stock_alert_state", "fast_sell_window_start","TIMESTAMP"),
    ]:
        _run(f"ADD COL {t}.{col}",
             f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {ddl}")

    # V20: New tables
    for tbl, cols in [
        ("referral_clicks",
         "id SERIAL PRIMARY KEY, "
         "referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "clicked_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "ip_hash VARCHAR(64)"),
        ("referral_commissions",
         "id SERIAL PRIMARY KEY, "
         "referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "referred_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL, "
         "order_amount FLOAT NOT NULL DEFAULT 0, "
         "commission_rate FLOAT NOT NULL DEFAULT 0, "
         "commission_amount FLOAT NOT NULL DEFAULT 0, "
         "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "cleared_at TIMESTAMP"),
        ("referral_withdrawals",
         "id SERIAL PRIMARY KEY, "
         "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "amount FLOAT NOT NULL DEFAULT 0, "
         "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
         "admin_note TEXT, "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "resolved_at TIMESTAMP"),
        ("announcements",
         "id SERIAL PRIMARY KEY, "
         "title VARCHAR(255) NOT NULL, "
         "content TEXT NOT NULL, "
         "target VARCHAR(32) NOT NULL DEFAULT 'all', "
         "target_user_ids TEXT, "
         "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
         "is_pinned BOOLEAN NOT NULL DEFAULT FALSE, "
         "is_scheduled BOOLEAN NOT NULL DEFAULT FALSE, "
         "scheduled_at TIMESTAMP, "
         "expires_at TIMESTAMP, "
         "sent_count INTEGER NOT NULL DEFAULT 0, "
         "is_sent BOOLEAN NOT NULL DEFAULT FALSE, "
         "sent_at TIMESTAMP, "
         "announcement_type VARCHAR(16) NOT NULL DEFAULT 'popup', "
         "created_by BIGINT, "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "updated_at TIMESTAMP NOT NULL DEFAULT NOW()"),
        ("announcement_reads",
         "id SERIAL PRIMARY KEY, "
         "announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE, "
         "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "read_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "CONSTRAINT uq_annread_ann_user UNIQUE (announcement_id, user_id)"),
    ]:
        _run(f"CREATE {tbl}", f"CREATE TABLE IF NOT EXISTS {tbl} ({cols})")

    # V20: Indexes on new tables
    for idx, tbl, col, uniq in [
        ("ix_rc_referrer_id",   "referral_clicks",      "referrer_id",     False),
        ("ix_rco_referrer_id",  "referral_commissions", "referrer_id",     False),
        ("ix_rco_status",       "referral_commissions", "status",          False),
        ("ix_rw_user_id",       "referral_withdrawals", "user_id",         False),
        ("ix_rw_status",        "referral_withdrawals", "status",          False),
        ("ix_ann_is_active",    "announcements",        "is_active",       False),
        ("ix_ann_is_pinned",    "announcements",        "is_pinned",       False),
        ("ix_ann_scheduled_at", "announcements",        "scheduled_at",    False),
        ("ix_annr_ann_id",      "announcement_reads",   "announcement_id", False),
        ("ix_annr_user_id",     "announcement_reads",   "user_id",         False),
        ("ix_st_ticket_number", "support_tickets",      "ticket_number",   True),
    ]:
        uniq_sql = "UNIQUE " if uniq else ""
        _run(f"idx {tbl}.{col}",
             f"CREATE {uniq_sql}INDEX IF NOT EXISTS {idx} ON {tbl}({col})")

    # ── V21: Six New Features migration ─────────────────────────────────────
    # Additional columns on existing tables
    for (t, col, ddl) in [
        ("admin_audit_logs", "old_value",  "TEXT"),
        ("admin_audit_logs", "new_value",  "TEXT"),
        ("admin_audit_logs", "ip_address", "VARCHAR(45)"),
        ("admin_audit_logs", "module",     "VARCHAR(64)"),
        ("coupons", "max_discount_amount", "FLOAT"),
        ("coupons", "activation_date",     "TIMESTAMP"),
        ("coupons", "target_user_id",      "INTEGER"),
        ("coupons", "product_ids",         "TEXT"),
        ("coupons", "category_ids",        "TEXT"),
        ("coupons", "coupon_type",         "VARCHAR(32) DEFAULT 'manual'"),
        ("coupons", "free_product_id",     "INTEGER"),
    ]:
        _run(f"ADD COL {t}.{col}",
             f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {ddl}")

    # V21: New tables
    for tbl, cols in [
        ("scheduled_broadcasts",
         "id SERIAL PRIMARY KEY, "
         "title VARCHAR(255) NOT NULL, "
         "message_text TEXT, "
         "media_file_id VARCHAR(512), "
         "media_type VARCHAR(16), "
         "parse_mode VARCHAR(16) DEFAULT 'HTML', "
         "target_segment VARCHAR(32) NOT NULL DEFAULT 'all', "
         "status VARCHAR(24) NOT NULL DEFAULT 'draft', "
         "scheduled_at TIMESTAMP, "
         "sent_at TIMESTAMP, "
         "total_recipients INTEGER NOT NULL DEFAULT 0, "
         "sent_count INTEGER NOT NULL DEFAULT 0, "
         "failed_count INTEGER NOT NULL DEFAULT 0, "
         "is_recurring BOOLEAN NOT NULL DEFAULT FALSE, "
         "recur_interval_hours INTEGER, "
         "next_run_at TIMESTAMP, "
         "created_by BIGINT, "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "updated_at TIMESTAMP NOT NULL DEFAULT NOW()"),
        ("refunds",
         "id SERIAL PRIMARY KEY, "
         "order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL, "
         "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
         "amount FLOAT NOT NULL DEFAULT 0, "
         "currency VARCHAR(8) NOT NULL DEFAULT 'USD', "
         "reason TEXT, "
         "status VARCHAR(24) NOT NULL DEFAULT 'pending', "
         "trigger VARCHAR(32) NOT NULL DEFAULT 'manual', "
         "refund_method VARCHAR(32) NOT NULL DEFAULT 'wallet', "
         "admin_note TEXT, "
         "processed_by BIGINT, "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "updated_at TIMESTAMP NOT NULL DEFAULT NOW()"),
        ("language_configs",
         "id SERIAL PRIMARY KEY, "
         "code VARCHAR(8) NOT NULL UNIQUE, "
         "name VARCHAR(64) NOT NULL, "
         "native_name VARCHAR(64), "
         "is_enabled BOOLEAN NOT NULL DEFAULT TRUE, "
         "is_default BOOLEAN NOT NULL DEFAULT FALSE, "
         "user_count INTEGER NOT NULL DEFAULT 0, "
         "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
         "updated_at TIMESTAMP NOT NULL DEFAULT NOW()"),
    ]:
        _run(f"CREATE {tbl}", f"CREATE TABLE IF NOT EXISTS {tbl} ({cols})")

    # V21: Indexes on new tables
    for idx, tbl, col, uniq in [
        ("ix_sb_status",     "scheduled_broadcasts", "status",       False),
        ("ix_sb_sched_at",   "scheduled_broadcasts", "scheduled_at", False),
        ("ix_sb_next_run",   "scheduled_broadcasts", "next_run_at",  False),
        ("ix_rf_order_id",   "refunds",              "order_id",     False),
        ("ix_rf_user_id",    "refunds",              "user_id",      False),
        ("ix_rf_status",     "refunds",              "status",       False),
        ("ix_lc_code",       "language_configs",     "code",         True),
        ("ix_lc_is_default", "language_configs",     "is_default",   False),
        ("ix_aal_module",    "admin_audit_logs",     "module",       False),
    ]:
        uniq_sql = "UNIQUE " if uniq else ""
        _run(f"idx {tbl}.{col}",
             f"CREATE {uniq_sql}INDEX IF NOT EXISTS {idx} ON {tbl}({col})")

    # ── Enterprise Admin Notification System — new per-admin pref columns ─
    # Adds the 6 new boolean preference columns introduced by the enterprise
    # notification system. All default TRUE so existing admins are opted-in
    # without any manual step. Idempotent: IF NOT EXISTS skips duplicates.
    _enterprise_notif_cols = [
        "new_user",
        "deposit",
        "payment_failed",
        "payment_expired",
        "payment_reversed",
        "order_delivered",
    ]
    for _col in _enterprise_notif_cols:
        _run(
            f"ADD COL admin_notification_prefs.{_col}",
            f"ALTER TABLE admin_notification_prefs "
            f"ADD COLUMN IF NOT EXISTS {_col} BOOLEAN NOT NULL DEFAULT TRUE",
        )

    # ── Payment notification dedup flags (transactions) ─────────────────
    # Mirrors alembic revision 20260920_paynotify. These are the durable
    # "already notified" markers the payment scheduler gates on before
    # sending "Payment Expired" / "Payment Review" messages, so a job
    # re-run, an overlapping execution, or a bot restart never re-sends
    # the same notification for an order. NOT NULL with a server default
    # so existing rows are backfilled to False automatically.
    for _col in ("expiry_notified", "review_notified"):
        _run(
            f"ADD COL transactions.{_col}",
            f"ALTER TABLE transactions "
            f"ADD COLUMN IF NOT EXISTS {_col} BOOLEAN NOT NULL DEFAULT FALSE",
        )

    # ── Ensure alembic_version is clean (single head) ─────────────────
    # The migration chain has been fixed (20260916_search_indexes is the
    # prior head; 20260920_paynotify is the current head). Remove any
    # stale intermediate entries so alembic works cleanly.
    try:
        cur.execute(
            "DELETE FROM alembic_version "
            "WHERE version_num NOT IN ("
            "  '20260920_paynotify',"
            "  '20260919_product_soft_delete',"
            "  '20260918_product_template_system',"
            "  '20260917_enterprise_admin_notifications',"
            "  '20260916_search_indexes',"
            "  '20260915_enterprise_v45',"
            "  '20260914_broadcast_campaign_manager'"
            ")"
        )
        logger.debug("[AUTO-MIGRATION] alembic_version cleaned up")
    except Exception as exc:
        logger.warning("[AUTO-MIGRATION] alembic_version cleanup skipped: %s", exc)

    cur.close()
    conn.close()
    logger.info("[AUTO-MIGRATION] Done.")


def main():
    """Initialize and start the bot."""
    # Validate configuration
    try:
        validate_settings()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    # ── Auto-migration: apply any missing schema changes on startup ────────
    # Must run BEFORE initialize_database() so enum types exist before
    # create_all() tries to create tables that reference them.
    # Every statement uses IF NOT EXISTS / ADD VALUE IF NOT EXISTS so
    # duplicate runs are harmless.
    try:
        _apply_pending_migrations()
    except Exception:
        logger.exception("Auto-migration failed — bot will still start but "
                         "some features may not work until migrations are applied")

    # Initialize database
    try:
        initialize_database()
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return

    # Seed BotConfig defaults (idempotent — only inserts missing keys)
    try:
        seed_defaults()
    except Exception:
        logger.exception("BotConfig seeding failed")

    # Create application
    application = Application.builder().token(settings.BOT_TOKEN).build()

    # ── Global middleware ──────────────────────────────────────────────
    # Maintenance mode gate — must run BEFORE all other handlers
    application.add_handler(TypeHandler(_TgUpdate, _track_activity), group=-2)
    application.add_handler(TypeHandler(_TgUpdate, _maintenance_gate), group=-1)

    # Register command handlers
    application.add_handler(CommandHandler("start", user_handlers.start_command))
    application.add_handler(CommandHandler("admin", admin_handlers.admin_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("language", user_handlers.language_command))

    # ── Multi-admin RBAC + OTP 2FA (see utils/permissions.py, handlers/admin_auth.py) ──
    application.add_handler(admin_auth.build_admin_login_conversation())
    application.add_handler(CommandHandler("admin_logout", admin_auth.admin_logout_command))
    application.add_handler(CommandHandler("admin_list", admin_auth.admin_list_command))
    application.add_handler(CommandHandler("admin_add", admin_auth.admin_add_command))
    application.add_handler(CommandHandler("admin_role", admin_auth.admin_role_command))
    application.add_handler(CommandHandler("admin_remove", admin_auth.admin_remove_command))

    # Register conversation handlers for multi-step flows

    # Top-up conversation
    topup_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.topup_start, pattern="^topup$")],
        states={
            payment_handlers.AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.topup_amount)],
            payment_handlers.METHOD: [
                # "Back to Menu" button on the payment-method selection screen.
                CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$"),
                CallbackQueryHandler(payment_handlers.topup_amount_path, pattern="^topup_amount_path$"),
                CallbackQueryHandler(payment_handlers.payment_method_heleket, pattern="^pay_heleket$"),
                CallbackQueryHandler(payment_handlers.heleket_asset_selected, pattern="^heleket_asset:"),
                # Manual (admin-managed) payment methods — new unified pattern
                CallbackQueryHandler(payment_handlers.payment_method_manual, pattern="^pay_pm_\\d+$"),
                # Legacy alias kept for one release (in-flight conversations from
                # before the payment-v2 upgrade).
                CallbackQueryHandler(payment_handlers.payment_method_manual, pattern="^pay_manual_\\d+$"),
                # Legacy methods still callable if configured
                CallbackQueryHandler(payment_handlers.payment_method_crypto, pattern="^pay_crypto$"),
                CallbackQueryHandler(payment_handlers.payment_method_card, pattern="^pay_card$"),
                # Automated gateways — bKash / Nagad (admin-enabled, see admin_payment_methods.py)
                CallbackQueryHandler(payment_handlers.payment_method_bkash, pattern="^pay_bkash$"),
                CallbackQueryHandler(payment_handlers.payment_method_nagad, pattern="^pay_nagad$"),
                # Telegram Stars (native XTR payments, admin-enabled via PaymentGatewayConfig —
                # see services/telegram_stars.py and handlers/admin_stars.py)
                CallbackQueryHandler(payment_handlers.payment_method_stars, pattern="^pay_stars$"),
                # Cryptomus (USDT/crypto, admin-enabled via PaymentGatewayConfig —
                # see services/cryptomus_payment.py and handlers/admin_cryptomus.py)
                CallbackQueryHandler(payment_handlers.payment_method_cryptomus, pattern="^pay_cryptomus$"),
                # NOWPayments (crypto, admin-enabled via PaymentGatewayConfig —
                # see services/nowpayments_payment.py and handlers/admin_nowpayments.py)
                CallbackQueryHandler(payment_handlers.payment_method_nowpayments, pattern="^pay_nowpayments$"),
                # ZiniPay (bKash/Nagad/Rocket, admin-enabled via PaymentGatewayConfig —
                # see services/zinipay_payment.py and handlers/admin_zinipay.py)
                CallbackQueryHandler(payment_handlers.payment_method_zinipay, pattern="^pay_zinipay$"),
                # Binance Pay (admin-enabled via PaymentGatewayConfig — see
                # services/binance_pay.py and handlers/admin_binance.py)
                CallbackQueryHandler(payment_handlers.payment_method_binance_pay, pattern="^pay_binance_pay$"),
                CallbackQueryHandler(payment_handlers.binance_currency_selected, pattern="^binance_currency:"),
                # Bybit Pay — UID Transfer (admin-enabled via PaymentGatewayConfig)
                CallbackQueryHandler(payment_handlers.payment_method_bybit_pay, pattern="^pay_bybit_pay$"),
                # Bybit Pay — on-chain: TRC20 / BEP20 / ERC20 / LTC (direct main-menu entries)
                CallbackQueryHandler(payment_handlers.payment_method_bybit_trc20, pattern="^pay_bybit_trc20$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_bep20, pattern="^pay_bybit_bep20$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_erc20, pattern="^pay_bybit_erc20$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_ltc, pattern="^pay_bybit_ltc$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_avaxc, pattern="^pay_bybit_avaxc$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_ton, pattern="^pay_bybit_ton$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_base, pattern="^pay_bybit_base$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_arb, pattern="^pay_bybit_arb$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_op, pattern="^pay_bybit_op$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_matic, pattern="^pay_bybit_matic$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit_sol, pattern="^pay_bybit_sol$"),
                # Legacy Bybit type/network sub-menu callbacks — kept for backward compat
                # with any orders that were in-flight before this change.
                CallbackQueryHandler(payment_handlers.bybit_type_selected, pattern="^bybit_type:"),
                CallbackQueryHandler(payment_handlers.bybit_back_to_type, pattern="^bybit_back_type:"),
                CallbackQueryHandler(payment_handlers.bybit_network_selected, pattern="^bybit_network:"),
            ],
            payment_handlers.MANUAL_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               payment_handlers.payment_manual_txid),
                CallbackQueryHandler(payment_handlers.cancel_topup, pattern="^cancel$"),
            ],
            payment_handlers.MANUAL_PROOF: [
                MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                               payment_handlers.payment_manual_proof),
                CallbackQueryHandler(payment_handlers.cancel_topup, pattern="^cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_topup, pattern="^cancel$"),
            CallbackQueryHandler(payment_handlers.cancel_topup)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(topup_conv_handler)

    # Binance Pay: 'Submit Transaction ID' — its own small conversation,
    # independent of topup_conv_handler since it's entered from a button on
    # a message sent long after that conversation already ended.
    binance_submit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.binance_submit_start, pattern="^binance_submit:\\d+$")],
        states={
            payment_handlers.BINANCE_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.binance_txid_received),
                CallbackQueryHandler(payment_handlers.binance_cancel_submit, pattern="^binance_cancel_submit$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.binance_cancel_submit, pattern="^binance_cancel_submit$"),
            CommandHandler("cancel", cancel_command),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(binance_submit_conv)

    # Bybit Pay: 'Submit Transaction ID' — its own small conversation,
    # independent of topup_conv_handler since it's entered from a button on
    # a message sent long after that conversation already ended.
    bybit_submit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.bybit_submit_start, pattern="^bybit_submit:\\d+$")],
        states={
            payment_handlers.BYBIT_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.bybit_txid_received),
                CallbackQueryHandler(payment_handlers.bybit_cancel_submit, pattern="^bybit_cancel_submit$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.bybit_cancel_submit, pattern="^bybit_cancel_submit$"),
            CommandHandler("cancel", cancel_command),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(bybit_submit_conv)

    # ZiniPay: 'Submit Transaction ID' — its own small conversation,
    # independent of topup_conv_handler since it's entered from a button on
    # a message sent after that conversation already ended.
    zinipay_submit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.zinipay_submit_start, pattern="^zinipay_submit:\\d+$")],
        states={
            payment_handlers.ZINIPAY_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.zinipay_txid_received),
                CallbackQueryHandler(payment_handlers.zinipay_cancel_submit, pattern="^zinipay_cancel_submit$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.zinipay_cancel_submit, pattern="^zinipay_cancel_submit$"),
            CommandHandler("cancel", cancel_command),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(zinipay_submit_conv)

    # Admin approve/reject buttons for manual payments (outside of any conversation)
    application.add_handler(CallbackQueryHandler(payment_handlers.admin_manual_approve, pattern="^mp_approve_\\d+$"))
    application.add_handler(CallbackQueryHandler(payment_handlers.admin_manual_reject, pattern="^mp_reject_\\d+$"))
    application.add_handler(CallbackQueryHandler(payment_handlers.admin_manual_verify_again, pattern="^mp_verify_\\d+$"))

    # Quantity preset buttons (dynamic keyboard built by quantity_presets.build_keyboard)
    application.add_handler(CallbackQueryHandler(
        payment_handlers.qty_preset_callback,
        pattern=r"^qty_preset_\d+_\d+$",
    ))

    # Telegram Payments (Card) handlers — confirmation arrives via the bot's update
    # polling, not a separate job: approve the pre-checkout, then credit on success.
    # Handles BOTH Card (Telegram Payments) and Telegram Stars (XTR) top-ups —
    # each callback branches on the invoice payload / Transaction.payment_method
    # (see handlers/payment_handlers.py precheckout_callback / successful_payment_callback).
    application.add_handler(PreCheckoutQueryHandler(payment_handlers.precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handlers.successful_payment_callback))


    # Product creation conversation
    create_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_product_start, pattern="^admin_create_product$")],
        states={
            admin_conversations.PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_name),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_desc),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_price),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_TYPE: [
                # V11 — new paginated 12-type picker + legacy fallbacks.
                CallbackQueryHandler(admin_conversations.product_type, pattern="^ptype:"),
                CallbackQueryHandler(admin_conversations.product_type, pattern="^ptype_page:"),
                CallbackQueryHandler(admin_conversations.product_type, pattern="^type_"),
                CallbackQueryHandler(admin_conversations.product_type, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_CATEGORY: [
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cat_"),
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_SUBCATEGORY: [
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^subcat_"),
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_IMAGE: [
                # Document uploads during the image step are handled gracefully
                # (user is prompted to type "skip" instead of triggering cancel)
                MessageHandler(filters.Document.ALL, admin_conversations.product_image),
                MessageHandler(filters.PHOTO | filters.TEXT, admin_conversations.product_image),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DOWNLOAD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_download_link),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_KEYS: [
                MessageHandler(filters.Document.ALL, admin_conversations.product_keys),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_keys),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
        },
        fallbacks=[
            # Only explicit /cancel command or the cancel_product callback cancels creation
            MessageHandler(filters.COMMAND, admin_conversations.cancel_product_creation),
            CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$"),
            # Any other callback query is ignored (not cancelled) to avoid false positives
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_product_conv)

    # Product edit conversation
    edit_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_product_start, pattern="^admin_edit_product$")],
        states={
            admin_conversations.EDIT_SELECT_PRODUCT: [
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^edit_prod_"),
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^admin_edit_product_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_products$")
            ],
            admin_conversations.EDIT_SELECT_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^edit_"),
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_NEW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_new_value),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodcat_"),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodsubcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_IMAGE_VALUE: [
                MessageHandler(filters.PHOTO, admin_conversations.edit_image_value),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^remove_product_image$"),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^cancel_edit$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_product_conv)

    # Category creation conversation
    create_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_category_start, pattern="^admin_create_category$")],
        states={
            admin_conversations.CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_name)],
            admin_conversations.CATEGORY_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_desc)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_category_conv)

    # Subcategory creation conversation
    create_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_subcategory_start, pattern="^admin_create_subcategory$")],
        states={
            admin_conversations.SUBCATEGORY_CATEGORY: [
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^subcat_cat_"),
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^cancel_subcat$")
            ],
            admin_conversations.SUBCATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.subcategory_name)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_subcategory_conv)

    # Category edit conversation
    edit_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_category_start, pattern="^admin_edit_category$")],
        states={
            admin_conversations.EDIT_CATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^edit_cat_"),
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^admin_edit_category_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_CATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^editcat_"),
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^cancel_edit_cat$")
            ],
            admin_conversations.EDIT_CATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_category_value),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_cat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_category_conv)

    # Subcategory edit conversation
    edit_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_subcategory_start, pattern="^admin_edit_subcategory$")],
        states={
            admin_conversations.EDIT_SUBCATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^edit_subcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^admin_edit_subcategory_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^editsubcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^cancel_edit_subcat$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_subcategory_value),
                CallbackQueryHandler(admin_conversations.edit_subcategory_value, pattern="^newcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_subcat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_subcategory_conv)

    # Support username configuration conversation
    config_support_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_support_username, pattern="^admin_support_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_support_conv)

    # Channel username configuration conversation
    config_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_channel_username, pattern="^admin_channel_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_channel_conv)

    # Welcome message configuration conversation
    config_welcome_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_welcome_message, pattern="^admin_welcome_msg$")],
        states={
            admin_conversations.WELCOME_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.welcome_message_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_welcome_conv)

    # Store logo configuration conversation
    config_logo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_store_logo, pattern="^admin_store_logo$")],
        states={
            admin_conversations.STORE_LOGO: [MessageHandler(filters.PHOTO, admin_conversations.store_logo_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_logo_conv)

    # Text-only broadcast conversation
    broadcast_text_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_text_start, pattern="^admin_broadcast_text$")],
        states={
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_text_message)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_text_conv)

    # Image + Text broadcast conversation
    broadcast_image_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_image_start, pattern="^admin_broadcast_image$")],
        states={
            admin_conversations.BROADCAST_IMAGE: [
                MessageHandler(filters.PHOTO, admin_conversations.broadcast_image_photo)
            ],
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_image_text)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_image_conv)

    # Dispute conversation
    dispute_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dispute_handlers.open_dispute_start, pattern="^open_dispute_")],
        states={
            dispute_handlers.DISPUTE_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dispute_handlers.dispute_reason_received)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(dispute_handlers.dispute_cancel, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, dispute_handlers.dispute_cancel)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(dispute_conv)

    # Direct purchase conversation (Buy Now flow)
    purchase_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.buy_product_start, pattern="^(buy_|product_)")],
        states={
            payment_handlers.PURCHASE_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.purchase_quantity_input),
                CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"),
            MessageHandler(filters.COMMAND, payment_handlers.cancel_purchase)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(purchase_conv)

    # Register callback query handlers
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^back$"))  # Back button goes to main menu
    application.add_handler(CallbackQueryHandler(user_handlers.currency_toggle_callback, pattern="^currency_toggle$"))
    application.add_handler(CallbackQueryHandler(user_handlers.language_menu_callback, pattern="^language_menu$"))
    application.add_handler(CallbackQueryHandler(
        user_handlers.set_language_callback,
        pattern=f"^set_lang_({'|'.join(SUPPORTED_LANGUAGES)})$",
    ))
    application.add_handler(CallbackQueryHandler(user_handlers.back_to_products_callback, pattern="^back_to_products$"))
    application.add_handler(CallbackQueryHandler(user_handlers.products_callback, pattern="^products"))
    application.add_handler(CallbackQueryHandler(user_handlers.category_callback, pattern="^category_"))
    application.add_handler(CallbackQueryHandler(user_handlers.subcategory_callback, pattern="^subcategory_"))
    # Note: clicking a product now goes straight into purchase_conv (registered
    # above) via the "^product_" pattern, so the old product-detail page /
    # separate "Buy Now" step is skipped entirely.
    application.add_handler(CallbackQueryHandler(user_handlers.availability_callback, pattern="^availability$"))
    application.add_handler(CallbackQueryHandler(user_handlers.flash_sales_callback, pattern="^flash_sales$"))
    # Legacy "☎️ Support" buttons (still on old sent messages) route to the
    # same Support Center ticket flow as the main-menu "support_center"
    # button, so the flow is identical everywhere instead of showing the
    # old "My Shop is Open 24/7" contact-only page.
    application.add_handler(CallbackQueryHandler(support_handlers.support_center_callback, pattern="^support$"))
    application.add_handler(CallbackQueryHandler(user_handlers.order_history_callback, pattern="^order_history"))
    application.add_handler(CallbackQueryHandler(user_handlers.user_order_detail_callback, pattern="^user_order_detail_"))
    # Order Detail — show/hide sensitive fields and one-tap copy
    application.add_handler(CallbackQueryHandler(user_handlers.oh_toggle_callback, pattern=r"^oh_toggle_"))
    application.add_handler(CallbackQueryHandler(user_handlers.oh_copy_callback, pattern=r"^oh_copy_"))
    # V25 — Order Timeline user callback
    from handlers.user_order_timeline import user_timeline_callback
    application.add_handler(CallbackQueryHandler(user_timeline_callback, pattern=r"^user_timeline_\d+$"))

    # Purchase confirmation and cancellation handlers
    application.add_handler(CallbackQueryHandler(payment_handlers.confirm_purchase, pattern="^confirm_purchase_"))
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"))

    # Global cancel handler for payment pages (outside conversation)
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_payment_page, pattern="^cancel$"))

    # Admin callback handlers
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_menu_callback, pattern="^admin_menu$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_products_callback, pattern="^admin_products"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_manage_inventory_callback, pattern="^admin_manage_inventory$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_restock_keys_callback, pattern="^admin_restock_keys$"))  # legacy compat
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_inv_page_callback, pattern=r"^inv_page_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_inv_product_callback, pattern=r"^inv_prod_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_inv_varsel_callback, pattern=r"^inv_varsel_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_manage_categories_callback, pattern="^admin_manage_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_categories_callback, pattern="^admin_view_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_users_callback, pattern="^admin_view_users"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_user_detail_callback, pattern="^view_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_ban_user_callback, pattern="^ban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_unban_user_callback, pattern="^unban_user_"))

    # ── New User Management panel (usr:* callbacks) ────────────────────────────
    from handlers import admin_users as _au
    # Users list + sort
    application.add_handler(CallbackQueryHandler(_au.users_list, pattern=r"^usr:list:\d+:(asc|desc)$"))
    # User detail
    application.add_handler(CallbackQueryHandler(_au.user_detail, pattern=r"^usr:det:\d+$"))
    # Balance menu + confirmation callback (NOT entry points — those live in conv)
    application.add_handler(CallbackQueryHandler(_au.balance_menu, pattern=r"^usr:bal:\d+$"))
    application.add_handler(CallbackQueryHandler(_au.balance_confirm, pattern=r"^usr:bal:cfm:\d+:.{1,16}$"))
    # Ban / Unban screens + execute
    application.add_handler(CallbackQueryHandler(_au.ban_screen,    pattern=r"^usr:ban:\d+$"))
    application.add_handler(CallbackQueryHandler(_au.ban_execute,   pattern=r"^usr:ban:cfm:\d+$"))
    application.add_handler(CallbackQueryHandler(_au.unban_screen,  pattern=r"^usr:ubn:\d+$"))
    application.add_handler(CallbackQueryHandler(_au.unban_execute, pattern=r"^usr:ubn:cfm:\d+$"))
    # Purchase history
    application.add_handler(CallbackQueryHandler(_au.purchase_history, pattern=r"^usr:ord:\d+:\d+$"))
    # Position
    application.add_handler(CallbackQueryHandler(_au.position_view, pattern=r"^usr:pos:\d+$"))

    # ── Customer 360° View panel (c360:* callbacks) ─────────────────────────────
    from handlers import admin_customer_view as _c360
    application.add_handler(_c360.build_c360_search_conv())
    application.add_handler(CallbackQueryHandler(_c360.c360_view, pattern=r"^c360:view:\d+$"))

    # ── Advanced User Profile panel (up:* callbacks) ───────────────────────────
    from handlers import admin_user_profile as _up
    application.add_handler(CallbackQueryHandler(_up.up_menu,           pattern=r"^up:menu$"))
    application.add_handler(CallbackQueryHandler(_up.up_list,           pattern=r"^up:list:\d+:(asc|desc)$"))
    application.add_handler(CallbackQueryHandler(_up.up_view,           pattern=r"^up:view:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_orders,         pattern=r"^up:ord:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_topup_history,  pattern=r"^up:topup:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_referrals,      pattern=r"^up:ref:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_wallet_history, pattern=r"^up:wal:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_activity,       pattern=r"^up:act:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_coupons,        pattern=r"^up:coup:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_bal_confirm,    pattern=r"^up:bal:cfm:\d+:(add|ded|bon):[0-9a-f]{16}$"))
    application.add_handler(CallbackQueryHandler(_up.up_ban_screen,     pattern=r"^up:ban:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_ban_execute,    pattern=r"^up:ban:cfm:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_unban_screen,   pattern=r"^up:ubn:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_unban_execute,  pattern=r"^up:ubn:cfm:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_delete_screen,  pattern=r"^up:del:\d+$"))
    application.add_handler(CallbackQueryHandler(_up.up_delete_execute, pattern=r"^up:del:cfm:\d+$"))

    # ── New Manual Payments panel (mp:* callbacks) ─────────────────────────────
    from handlers import admin_manual_payments as _amp
    application.add_handler(CallbackQueryHandler(_amp.payments_list,           pattern=r"^mp:list:\d+:(asc|desc)$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_detail,          pattern=r"^mp:det:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_get_proof,       pattern=r"^mp:proof:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_confirm_ask,     pattern=r"^mp:cfm_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_confirm_execute, pattern=r"^mp:cfm_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_reject_ask,      pattern=r"^mp:rej_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.payment_reject_execute,  pattern=r"^mp:rej_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(_amp.edit_debitable_confirm,  pattern=r"^mp:edit_cfm:\d+:.{1,16}$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_orders_callback, pattern="^admin_view_orders"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_order_menu, pattern="^admin_confirm_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_menu, pattern="^admin_cancel_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_payment_callback, pattern="^confirm_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_payment_callback, pattern="^cancel_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_delete_payment_callback, pattern="^delete_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_order_detail_callback, pattern="^view_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_complete_order_callback, pattern="^complete_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_callback, pattern="^cancel_order_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_view_disputes_callback, pattern="^admin_view_disputes"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_dispute_detail_callback, pattern="^admin_dispute_detail_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_resolve_dispute_callback, pattern="^resolve_dispute_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_dispute_set_priority_callback, pattern="^adm_disp_pri_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_users_callback, pattern="^admin_users"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_orders_callback, pattern="^admin_orders"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_settings_callback, pattern="^admin_settings"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_toggle_currency_button_callback, pattern="^admin_toggle_currency_btn$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_broadcast_callback, pattern="^admin_broadcast"))

    # ─── Admin dashboard extras (low stock, preview, audit, maintenance) ─
    application.add_handler(CallbackQueryHandler(admin_dash.admin_low_stock_view,
                                                 pattern="^admin_low_stock$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_preview_menu,
                                                 pattern="^admin_preview$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_preview_welcome,
                                                 pattern="^admin_preview_welcome$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_preview_product,
                                                 pattern="^admin_preview_product$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_preview_receipt,
                                                 pattern="^admin_preview_receipt$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_preview_payment,
                                                 pattern="^admin_preview_payment$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_audit_log_view,
                                                 pattern="^admin_audit_log_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_dash.admin_maintenance_toggle,
                                                 pattern="^admin_maintenance_toggle$"))

    # ── Manage Inventory conversation handler (replaces legacy Restock Keys) ────
    manage_inv_conv = ConversationHandler(
        entry_points=[
            # New inventory-management entry points (inv_add_{pid} or inv_add_{pid}_v{vid})
            CallbackQueryHandler(
                admin_handlers.admin_inv_add_start_callback,
                pattern=r"^inv_add_\d+(_v\d+)?$",
            ),
            # Legacy entry point (select_product_{id}) — redirects to new flow
            CallbackQueryHandler(
                admin_handlers.admin_select_product_restock_callback,
                pattern=r"^select_product_\d+$",
            ),
        ],
        states={
            admin_handlers.WAITING_FOR_INV: [
                # Document uploads come FIRST so they are never consumed by the text handler
                MessageHandler(
                    filters.Document.ALL & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_handlers.handle_inv_add_file,
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_handlers.handle_inv_add_paste,
                ),
            ],
            # Legacy WAITING_FOR_KEYS state — redirect to new handlers
            admin_handlers.WAITING_FOR_KEYS: [
                MessageHandler(
                    filters.Document.ALL & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_handlers.handle_inv_add_file,
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_handlers.handle_inv_add_paste,
                ),
            ],
        },
        fallbacks=[
            # Explicit cancel button (cancel_inv) or legacy cancel_restock
            CallbackQueryHandler(admin_handlers.cancel_manage_inventory, pattern="^cancel_inv$"),
            CallbackQueryHandler(admin_handlers.cancel_restock, pattern="^cancel_restock$"),
            CommandHandler(
                "cancel",
                admin_handlers.cancel_manage_inventory,
                filters=filters.User(settings.ADMIN_TELEGRAM_ID),
            ),
        ],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(manage_inv_conv)

    # ── Delivery Format ("📄 Formatted Account") conversation ───────────────
    delivery_fmt_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_delivery_format.admin_delivery_format_start_callback,
                pattern=r"^delivery_fmt_\d+$",
            ),
        ],
        states={
            admin_delivery_format.WAITING_FOR_DELIVERY_TEMPLATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_delivery_format.handle_delivery_format_text,
                ),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_delivery_format.cancel_delivery_format, pattern="^delivery_fmt_cancel$"),
            CommandHandler(
                "cancel",
                admin_delivery_format.cancel_delivery_format,
                filters=filters.User(settings.ADMIN_TELEGRAM_ID),
            ),
        ],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(delivery_fmt_conv)
    application.add_handler(CallbackQueryHandler(
        admin_delivery_format.admin_delivery_format_preview_callback,
        pattern=r"^delivery_fmt_preview_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        admin_delivery_format.admin_delivery_format_clear_callback,
        pattern=r"^delivery_fmt_clear_\d+$",
    ))

    # V9: bot is English-only. No language / setlang callbacks are registered.


    # Refer & Earn
    application.add_handler(CallbackQueryHandler(referral_handlers.refer_callback, pattern="^refer$"))

    # Support Center — user side
    application.add_handler(CallbackQueryHandler(support_handlers.support_center_callback, pattern="^support_center$"))
    application.add_handler(CallbackQueryHandler(support_handlers.show_info_page_callback, pattern=r"^sc_page_(terms|faq|about)$"))
    application.add_handler(CallbackQueryHandler(support_handlers.my_tickets_callback, pattern="^sc_list$"))
    application.add_handler(CallbackQueryHandler(support_handlers.view_ticket_callback, pattern="^sc_view_"))
    application.add_handler(CallbackQueryHandler(support_handlers.close_ticket_callback, pattern="^sc_close_"))
    application.add_handler(CallbackQueryHandler(support_handlers.reopen_ticket_callback, pattern="^sc_reopen_"))

    # New ticket conversation
    new_ticket_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(support_handlers.new_ticket_start, pattern="^sc_new$")],
        states={
            support_handlers.TICKET_SUBJECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, support_handlers.new_ticket_subject),
                CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$"),
            ],
            # V20: Category selection step
            support_handlers.TICKET_CATEGORY: [
                CallbackQueryHandler(support_handlers.new_ticket_category, pattern=r"^sc_cat_"),
                CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$"),
            ],
            support_handlers.TICKET_MESSAGE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                    support_handlers.new_ticket_message
                ),
                CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$")],
        allow_reentry=True,
    )
    application.add_handler(new_ticket_conv)

    # User reply to ticket
    user_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(support_handlers.reply_ticket_start, pattern="^sc_reply_")],
        states={
            support_handlers.TICKET_REPLY: [
                # V20: Accept photo attachments in user replies
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                    support_handlers.reply_ticket_message
                ),
                CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(support_handlers.new_ticket_cancel, pattern="^sc_cancel$")],
        allow_reentry=True,
    )
    application.add_handler(user_reply_conv)

    # Support Center — admin side
    application.add_handler(CallbackQueryHandler(support_handlers.admin_tickets_callback, pattern="^admin_tickets$"))
    application.add_handler(CallbackQueryHandler(support_handlers.admin_ticket_view_callback, pattern="^adm_tk_view_"))
    application.add_handler(CallbackQueryHandler(support_handlers.admin_ticket_close_callback, pattern="^adm_tk_close_"))
    application.add_handler(CallbackQueryHandler(support_handlers.admin_ticket_reopen_callback, pattern="^adm_tk_reopen_"))
    application.add_handler(CallbackQueryHandler(support_handlers.admin_ticket_set_priority_callback, pattern="^adm_tk_pri_"))

    admin_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(support_handlers.admin_ticket_reply_start, pattern="^adm_tk_reply_")],
        states={
            support_handlers.ADMIN_TICKET_REPLY: [
                # V20: Accept photo attachments in admin replies
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND
                    & filters.User(settings.ADMIN_TELEGRAM_ID),
                    support_handlers.admin_ticket_reply_message
                ),
                CallbackQueryHandler(support_handlers.admin_ticket_view_callback, pattern="^adm_tk_view_"),
            ],
        },
        fallbacks=[CallbackQueryHandler(support_handlers.admin_ticket_view_callback, pattern="^adm_tk_view_")],
        allow_reentry=True,
    )
    application.add_handler(admin_reply_conv)

    # Admin referral settings
    application.add_handler(CallbackQueryHandler(referral_handlers.admin_referral_toggle, pattern="^admin_referral_toggle$"))
    referral_amount_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(referral_handlers.admin_referral_amount_start, pattern="^admin_referral_reward$")],
        states={
            referral_handlers.REFERRAL_AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               referral_handlers.admin_referral_amount_input),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_handlers.admin_settings_callback, pattern="^admin_settings$")],
        allow_reentry=True,
    )
    application.add_handler(referral_amount_conv)

    # ─── V3: Admin Manual Payment Methods (CRUD) ─────────────────────────
    application.add_handler(CallbackQueryHandler(admin_pm.admin_payment_methods_menu,
                                                  pattern="^admin_payment_methods$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_view,
                                                  pattern="^admin_pm_view_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_toggle,
                                                  pattern="^admin_pm_toggle_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_delete,
                                                  pattern="^admin_pm_delete_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_delete_all_confirm,
                                                  pattern="^admin_pm_delete_all_confirm$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_delete_all,
                                                  pattern="^admin_pm_delete_all_go$"))
    # V6: toggle require_txid / require_proof from the detail view
    application.add_handler(CallbackQueryHandler(admin_pm.admin_pm_toggle_flag,
                                                  pattern="^admin_pm_tgl_(txid|proof)_\\d+$"))

    # Add new manual payment method conversation
    pm_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_pm.admin_pm_add_start, pattern="^admin_pm_add$")],
        states={
            admin_pm.PM_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_pm_add_name),
                CallbackQueryHandler(admin_pm.admin_pm_cancel, pattern="^admin_payment_methods$"),
            ],
            admin_pm.PM_ADD_EMOJI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_pm_add_emoji),
                CallbackQueryHandler(admin_pm.admin_pm_add_emoji, pattern="^pm_add_emoji_skip$"),
            ],
            admin_pm.PM_ADD_INSTRUCTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_pm_add_instructions),
            ],
            admin_pm.PM_ADD_MIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_pm_add_min),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_pm.admin_pm_cancel, pattern="^admin_payment_methods$")],
        allow_reentry=True,
    )
    application.add_handler(pm_add_conv)

    # Edit existing manual payment method (single-field) conversation
    pm_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_pm.admin_pm_edit_start,
                                            pattern="^admin_pm_edit_(name|emoji|instr|min|max|label|acct|order)_\\d+$")],
        states={
            admin_pm.PM_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_pm_edit_value),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_pm.admin_pm_cancel, pattern="^admin_payment_methods$")],
        allow_reentry=True,
    )
    application.add_handler(pm_edit_conv)

    # ─── Admin Deposit Settings (Global Minimum Deposit) ────────────────────
    from handlers import admin_deposit_settings as admin_dep
    application.add_handler(CallbackQueryHandler(admin_dep.admin_deposit_view,
                                                  pattern="^admin_deposit_view$"))
    application.add_handler(CallbackQueryHandler(admin_dep.admin_deposit_enable,
                                                  pattern="^admin_deposit_enable$"))
    application.add_handler(CallbackQueryHandler(admin_dep.admin_deposit_disable,
                                                  pattern="^admin_deposit_disable$"))
    application.add_handler(admin_dep.build_admin_deposit_conv())

    # ─── Admin Payment Gateways (bKash / Nagad) enable/disable + credentials ──
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gateways_menu,
                                                  pattern="^admin_gateways$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gw_view,
                                                  pattern="^admin_gw_view_(bkash|nagad)$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gw_toggle,
                                                  pattern="^admin_gw_toggle_(bkash|nagad)$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gw_disable_all_confirm,
                                                  pattern="^admin_gw_disable_all_confirm$"))
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gw_disable_all,
                                                  pattern="^admin_gw_disable_all_go$"))
    # NEW: Auto <-> Manual mode toggle (separate from admin_gw_toggle, which
    # is the gateway's enabled/disabled switch).
    application.add_handler(CallbackQueryHandler(admin_pm.admin_gw_toggle_mode,
                                                  pattern="^admin_gw_mode_toggle_(bkash|nagad)$"))

    # Edit a single bKash/Nagad credential field conversation
    gw_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            admin_pm.admin_gw_edit_start,
            pattern="^admin_gw_edit_(mode|appkey|appsecret|username|password|"
                    "merchantid|merchantnumber|pubkey|privkey|min|max|"
                    "manualnumber|manualinstr)_(bkash|nagad)$",
        )],
        states={
            admin_pm.GW_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_pm.admin_gw_edit_value),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_pm.admin_gw_cancel, pattern="^admin_gateways$")],
        allow_reentry=True,
    )
    application.add_handler(gw_edit_conv)

    # ─── Admin Payment Gateway: Telegram Stars (native XTR) ────────────
    from handlers import admin_stars
    application.add_handler(CallbackQueryHandler(admin_stars.admin_stars_view,
                                                  pattern="^admin_stars_view$"))
    application.add_handler(CallbackQueryHandler(admin_stars.admin_stars_toggle,
                                                  pattern="^admin_stars_toggle$"))
    application.add_handler(admin_stars.build_stars_edit_conv())

    # ─── Admin Payment Gateway: Cryptomus (USDT/crypto) ─────────────────
    from handlers import admin_cryptomus
    application.add_handler(CallbackQueryHandler(admin_cryptomus.admin_cryptomus_view,
                                                  pattern="^admin_cryptomus_view$"))
    application.add_handler(CallbackQueryHandler(admin_cryptomus.admin_cryptomus_toggle,
                                                  pattern="^admin_cryptomus_toggle$"))
    application.add_handler(admin_cryptomus.build_cryptomus_edit_conv())

    # ─── Admin Payment Gateway: Heleket Static Wallet ───────────────────
    from handlers import admin_heleket
    application.add_handler(CallbackQueryHandler(admin_heleket.admin_heleket_view, pattern="^admin_heleket_view$"))
    application.add_handler(CallbackQueryHandler(admin_heleket.admin_heleket_toggle, pattern="^admin_heleket_toggle$"))
    application.add_handler(admin_heleket.build_heleket_edit_conv())

    # ─── Admin Payment Gateway: NOWPayments (crypto) ────────────────────
    from handlers import admin_nowpayments
    application.add_handler(CallbackQueryHandler(admin_nowpayments.admin_nowpayments_view,
                                                  pattern="^admin_nowpayments_view$"))
    application.add_handler(CallbackQueryHandler(admin_nowpayments.admin_nowpayments_toggle,
                                                  pattern="^admin_nowpayments_toggle$"))
    application.add_handler(admin_nowpayments.build_nowpayments_edit_conv())

    # ─── Admin Payment Gateway: ZiniPay (bKash/Nagad/Rocket) ────────────
    from handlers import admin_zinipay
    application.add_handler(CallbackQueryHandler(admin_zinipay.admin_zinipay_view,
                                                  pattern="^admin_zinipay_view$"))
    application.add_handler(CallbackQueryHandler(admin_zinipay.admin_zinipay_toggle,
                                                  pattern="^admin_zinipay_toggle$"))
    application.add_handler(CallbackQueryHandler(admin_zinipay.admin_zinipay_toggle_autorate,
                                                  pattern="^admin_zinipay_toggle_autorate$"))
    application.add_handler(CallbackQueryHandler(admin_zinipay.admin_zinipay_provider_menu,
                                                  pattern="^admin_zinipay_provider_menu$"))
    application.add_handler(CallbackQueryHandler(admin_zinipay.admin_zinipay_set_provider,
                                                  pattern="^admin_zinipay_setprovider_"))
    application.add_handler(admin_zinipay.build_zinipay_edit_conv())

    # ─── Admin Payment Gateway: Binance Pay ─────────────────────────────
    # HMAC API transaction-history verification — see services/binance_pay.py
    # and handlers/admin_binance.py. API Key/Secret are env-var only, never
    # editable here.
    from handlers import admin_binance
    application.add_handler(CallbackQueryHandler(admin_binance.admin_binance_view,
                                                  pattern="^admin_binance_view$"))
    application.add_handler(CallbackQueryHandler(admin_binance.admin_binance_toggle,
                                                  pattern="^admin_binance_toggle$"))
    application.add_handler(CallbackQueryHandler(admin_binance.admin_binance_toggle_currency,
                                                  pattern="^admin_binance_toggle_cur_"))
    application.add_handler(CallbackQueryHandler(admin_binance.admin_binance_test,
                                                  pattern="^admin_binance_test$"))
    application.add_handler(CallbackQueryHandler(admin_binance.admin_binance_pending,
                                                  pattern="^admin_binance_pending$"))
    application.add_handler(admin_binance.build_binance_edit_conv())
    # Admin approve/reject/verify-again for Binance Pay manual verifications
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_approve_binance_verification,
        pattern=r"^admin_binance_approve_\d+_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_reject_binance_verification,
        pattern=r"^admin_binance_reject_\d+_\d+$",  # legacy — kept for backward compat
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_verify_again_binance,
        pattern=r"^admin_binance_verify_\d+_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_view_user_from_pmv,
        pattern=r"^admin_view_user_pmv_\d+$",
    ))

    # ─── Admin Payment Gateway: Bybit Pay ───────────────────────────────
    # Official Bybit V5 API (UID Transfer + on-chain deposit) — see
    # services/bybit_pay.py and handlers/admin_bybit.py.
    # API Key/Secret can be set via admin panel or env vars (env = fallback).
    from handlers import admin_bybit
    application.add_handler(CallbackQueryHandler(admin_bybit.admin_bybit_view,
                                                  pattern="^admin_bybit_view$"))
    application.add_handler(CallbackQueryHandler(admin_bybit.admin_bybit_toggle,
                                                  pattern="^admin_bybit_toggle$"))
    application.add_handler(CallbackQueryHandler(admin_bybit.admin_bybit_toggle_network,
                                                  pattern="^admin_bybit_toggle_net_"))
    application.add_handler(CallbackQueryHandler(admin_bybit.admin_bybit_test,
                                                  pattern="^admin_bybit_test$"))
    application.add_handler(CallbackQueryHandler(admin_bybit.admin_bybit_pending,
                                                  pattern="^admin_bybit_pending$"))
    application.add_handler(admin_bybit.build_bybit_edit_conv())
    # Admin approve/reject/verify-again for Bybit Pay manual verifications
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_approve_bybit_verification,
        pattern=r"^admin_bybit_approve_\d+_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_reject_bybit_verification,
        pattern=r"^admin_bybit_reject_\d+_\d+$",  # legacy — kept for backward compat
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_verify_again_bybit,
        pattern=r"^admin_bybit_verify_\d+_\d+$",
    ))

    # ─── Admin: ZiniPay (bKash / Nagad / Rocket) manual-review queue ────
    # Approve/reject buttons sent to admins when auto-verification fails
    # for a Mobile Banking (ZiniPay-backed) deposit — see
    # handlers/payment_handlers.py: zinipay_txid_received / _pmv_resolve.
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_approve_zinipay_verification,
        pattern=r"^admin_zinipay_approve_\d+_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_reject_zinipay_verification,
        pattern=r"^admin_zinipay_reject_\d+_\d+$",  # legacy — kept for backward compat
    ))
    application.add_handler(CallbackQueryHandler(
        payment_handlers.admin_verify_again_zinipay,
        pattern=r"^admin_zinipay_verify_\d+_\d+$",
    ))

    # Admin PMV rejection-with-reason conversation (Binance + Bybit + ZiniPay)
    application.add_handler(payment_handlers.build_admin_pmv_reject_conv())


    # ─── V4 (Phase 2): Search, Coupons, Currency, Receipts ───────────────
    # /search command + Search menu button (conversation)
    application.add_handler(CommandHandler("search", search_handlers.search_command))
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_handlers.search_start, pattern="^search$")],
        states={
            search_handlers.SEARCH_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_handlers.search_query_input),
                CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$")],
        allow_reentry=True,
    )
    application.add_handler(search_conv)

    # PDF receipt download
    application.add_handler(CallbackQueryHandler(
        user_handlers.download_receipt_callback, pattern="^receipt_\\d+$"))

    # Coupons — user apply flow (from purchase confirmation)
    apply_coupon_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(coupon_handlers.apply_coupon_start, pattern="^apply_coupon$")],
        states={
            coupon_handlers.COUPON_CODE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_handlers.apply_coupon_input),
                CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$")],
        allow_reentry=True,
    )
    application.add_handler(apply_coupon_conv)
    application.add_handler(CallbackQueryHandler(
        payment_handlers.remove_coupon_callback, pattern="^remove_coupon$"))

    # Coupons — admin CRUD
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_coupons_menu, pattern="^admin_coupons$"))
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_coupon_view, pattern="^admin_coupon_view_\\d+$"))
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_coupon_toggle, pattern="^admin_coupon_toggle_\\d+$"))
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_coupon_delete, pattern="^admin_coupon_delete_\\d+$"))

    coupon_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(coupon_handlers.admin_coupon_add_start, pattern="^admin_coupon_add$")],
        states={
            coupon_handlers.ADD_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_coupon_add_code),
                CallbackQueryHandler(coupon_handlers.admin_coupon_add_cancel, pattern="^admin_coupons$"),
            ],
            coupon_handlers.ADD_TYPE: [
                CallbackQueryHandler(coupon_handlers.admin_coupon_add_type, pattern="^coupontype_"),
            ],
            coupon_handlers.ADD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_coupon_add_value),
            ],
            coupon_handlers.ADD_MAX_USES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_coupon_add_max_uses),
            ],
        },
        fallbacks=[CallbackQueryHandler(coupon_handlers.admin_coupon_add_cancel, pattern="^admin_coupons$")],
        allow_reentry=True,
    )
    application.add_handler(coupon_add_conv)

    # Currency settings — admin
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_currency_menu, pattern="^admin_currency$"))
    application.add_handler(CallbackQueryHandler(coupon_handlers.admin_currency_clear, pattern="^admin_currency_clear$"))
    currency_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(coupon_handlers.admin_currency_set_start, pattern="^admin_currency_set$")],
        states={
            coupon_handlers.CUR_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_currency_code),
            ],
            coupon_handlers.CUR_SYMBOL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_currency_symbol),
            ],
            coupon_handlers.CUR_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               coupon_handlers.admin_currency_rate),
            ],
        },
        fallbacks=[CallbackQueryHandler(coupon_handlers.admin_currency_menu, pattern="^admin_currency$")],
        allow_reentry=True,
    )
    application.add_handler(currency_conv)

    # ─── V5 (Phase 3): Loyalty, Reviews, Analytics ───────────────────────
    # Loyalty — user side
    application.add_handler(CallbackQueryHandler(loyalty_handlers.loyalty_menu, pattern="^loyalty$"))
    loyalty_redeem_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(loyalty_handlers.loyalty_redeem_start, pattern="^loyalty_redeem$")],
        states={
            loyalty_handlers.REDEEM_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, loyalty_handlers.loyalty_redeem_amount),
                CallbackQueryHandler(loyalty_handlers.loyalty_cancel, pattern="^main_menu$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(loyalty_handlers.loyalty_cancel, pattern="^main_menu$")],
        allow_reentry=True,
    )
    application.add_handler(loyalty_redeem_conv)

    # Loyalty — admin
    application.add_handler(CallbackQueryHandler(loyalty_handlers.admin_loyalty_menu, pattern="^admin_loyalty$"))
    application.add_handler(CallbackQueryHandler(loyalty_handlers.admin_loyalty_toggle, pattern="^admin_loy_toggle$"))
    admin_loy_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(loyalty_handlers.admin_loyalty_set_earn, pattern="^admin_loy_earn$"),
            CallbackQueryHandler(loyalty_handlers.admin_loyalty_set_redeem, pattern="^admin_loy_redeem$"),
            CallbackQueryHandler(loyalty_handlers.admin_loyalty_set_min, pattern="^admin_loy_min$"),
        ],
        states={
            loyalty_handlers.LOY_SET_EARN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                loyalty_handlers.admin_loyalty_earn_input)],
            loyalty_handlers.LOY_SET_REDEEM: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                loyalty_handlers.admin_loyalty_redeem_input)],
            loyalty_handlers.LOY_SET_MIN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                loyalty_handlers.admin_loyalty_min_input)],
        },
        fallbacks=[CallbackQueryHandler(loyalty_handlers.admin_loyalty_menu, pattern="^admin_loyalty$")],
        allow_reentry=True,
    )
    application.add_handler(admin_loy_conv)

    # Reviews — view public reviews
    application.add_handler(CallbackQueryHandler(review_handlers.product_reviews_view, pattern="^reviews_\\d+$"))
    # Reviews — write flow (from order detail)
    review_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(review_handlers.review_start, pattern="^review_start_\\d+_\\d+$")],
        states={
            review_handlers.REVIEW_COMMENT: [
                CallbackQueryHandler(review_handlers.review_rating_pick, pattern="^reviewrate_[1-5]$"),
                CallbackQueryHandler(review_handlers.review_skip_comment, pattern="^review_skip$"),
                CallbackQueryHandler(review_handlers.review_cancel, pattern="^review_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, review_handlers.review_comment_text),
            ],
        },
        fallbacks=[CallbackQueryHandler(review_handlers.review_cancel, pattern="^review_cancel$")],
        allow_reentry=True,
    )
    application.add_handler(review_conv)

    # Analytics — admin
    application.add_handler(CallbackQueryHandler(analytics_handlers.admin_analytics_menu, pattern="^admin_analytics$"))
    application.add_handler(CallbackQueryHandler(analytics_handlers.admin_cohort_analysis, pattern="^admin_analytics_cohort$"))
    application.add_handler(CallbackQueryHandler(analytics_handlers.admin_ltv_analysis, pattern="^admin_analytics_ltv$"))
    application.add_handler(CallbackQueryHandler(analytics_handlers.admin_churn_analysis, pattern="^admin_analytics_churn$"))

    # ─── V8 Premium Core: Variants + Cart ─────────────────────────────
    variant_handlers.register(application)
    cart_handlers.register(application)
    admin_redelivery.register(application)
    admin_badges.register_handlers(application)
    wallet_handlers.register_handlers(application)

    # ─── V6: Unified Bot Configuration (admin) ─────────────────────────
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_menu,        pattern=r"^admin_bot_config$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_section,     pattern=r"^cfg_sec_[a-z]+$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_category,    pattern=r"^cfg_cat_[a-z_]+(__p\d+)?$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_view,        pattern=r"^cfg_view_[a-z_]+$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_toggle,      pattern=r"^cfg_toggle_[a-z_]+$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_reset,       pattern=r"^cfg_reset_[a-z_]+$"))
    application.add_handler(CallbackQueryHandler(admin_cfg.admin_config_search_page, pattern=r"^cfg_srp__p\d+$"))

    # Search conversation (entry via 🔍 button → user types query → results)
    cfg_search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_cfg.admin_config_search_start, pattern=r"^cfg_search$")],
        states={
            admin_cfg.SEARCH_QUERY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                    admin_cfg.admin_config_search_do,
                ),
                CallbackQueryHandler(admin_cfg.admin_config_search_start, pattern=r"^cfg_search$"),
                CallbackQueryHandler(lambda u, c: u.callback_query.answer() or ConversationHandler.END,
                                     pattern=r"^admin_bot_config$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_cfg.admin_config_menu, pattern=r"^admin_bot_config$")],
        allow_reentry=True,
        per_user=True, per_chat=True,
    )
    application.add_handler(cfg_search_conv)

    # Edit conversation
    cfg_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_cfg.admin_config_edit_start, pattern=r"^cfg_edit_[a-z_]+$")],
        states={
            admin_cfg.EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(settings.ADMIN_TELEGRAM_ID),
                               admin_cfg.admin_config_edit_value),
                CallbackQueryHandler(admin_cfg.admin_config_edit_cancel, pattern=r"^cfg_view_[a-z_]+$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(admin_cfg.admin_config_edit_cancel, pattern=r"^cfg_view_[a-z_]+$")],
        allow_reentry=True,
        per_user=True, per_chat=True,
    )
    application.add_handler(cfg_edit_conv)

    # Schedule background jobs
    job_queue = application.job_queue

    # Payment checking jobs — interval is admin-configurable via bot_config
    _pay_interval = cfg.get_int("payment_check_interval_seconds",
                                settings.PAYMENT_CHECK_INTERVAL)
    job_queue.run_repeating(
        payment_handlers.check_pending_payments,
        interval=_pay_interval,
        first=10
    )
    job_queue.run_repeating(
        payment_handlers.check_expired_payments,
        interval=60,
        first=30
    )

    # V8 Premium Core: sweep expired stock reservations every 60s.
    job_queue.run_repeating(
        inventory_svc.expire_reservations_job,
        interval=60,
        first=45,
    )

    # V9 (Premium Admin Control Center): low-stock notifier job.
    from handlers import admin_control_center as _acc  # noqa: F401 (ensure imported)
    from services import notifications as _notif_svc
    from database import get_db_session as _get_db_session, Product as _Product
    from database.models import LowStockAlertState as _LSAS
    from utils.bot_config import cfg as _cfg

    async def _low_stock_job(context):
        try:
            th = _cfg.get_int("low_stock_threshold", 5)
            if th <= 0 or not _cfg.get_bool("notif_low_stock", True):
                return
            alerts = []
            with _get_db_session() as s:
                products = (s.query(_Product)
                            .filter(_Product.is_active == True,  # noqa: E712
                                    _Product.stock_count <= th)
                            .limit(50).all())
                for p in products:
                    state = (s.query(_LSAS)
                             .filter(_LSAS.product_id == p.id,
                                     _LSAS.variant_id.is_(None))
                             .first())
                    # Edge-triggered: only alert if previous seen stock was above threshold
                    if state is None:
                        state = _LSAS(product_id=p.id, variant_id=None,
                                      last_stock_seen=p.stock_count)
                        s.add(state)
                        alerts.append((p.id, p.name, p.stock_count))
                    elif state.last_stock_seen > th and p.stock_count <= th:
                        alerts.append((p.id, p.name, p.stock_count))
                    state.last_stock_seen = p.stock_count
                s.commit()
            for pid, pname, stk in alerts:
                from utils.notify_format import render as _render_notif, utc_now_str as _ts
                if stk <= 0:
                    icon, title = "📦", "Out of Stock"
                    fields = [("Product", pname)]
                else:
                    icon, title = "⚠️", "Low Stock"
                    fields = [("Product", pname), ("Remaining", stk), ("Threshold", th)]
                await _notif_svc.notify_admins(
                    context.bot, "low_stock",
                    _render_notif(icon, title, fields, _ts()),
                )
        except Exception:
            logger.exception("low-stock job failed")

    _ls_interval = max(1, cfg.get_int("low_stock_check_interval_minutes", 30)) * 60
    job_queue.run_repeating(_low_stock_job, interval=_ls_interval, first=90)

    # V13: Subscription recurring billing — renewal reminders + auto-deduct.
    from services import subscription_service as _sub_svc
    _sub_reminder_interval = max(1, cfg.get_int(
        "subscription_reminder_check_interval_minutes", 60)) * 60
    _sub_billing_interval = max(1, cfg.get_int(
        "subscription_billing_check_interval_minutes", 30)) * 60
    job_queue.run_repeating(_sub_svc.reminder_job,
                            interval=_sub_reminder_interval, first=120)
    job_queue.run_repeating(_sub_svc.billing_job,
                            interval=_sub_billing_interval, first=150)

    # V14: Marketing Automation — abandoned-cart reminders + win-back offers.
    from services import marketing_automation as _mkt_svc
    _mkt_interval = max(1, cfg.get_int("marketing_check_interval_minutes", 15)) * 60
    job_queue.run_repeating(_mkt_svc.cart_reminder_job, interval=_mkt_interval, first=180)
    job_queue.run_repeating(_mkt_svc.winback_job, interval=_mkt_interval, first=210)

    # V16: Priority-Based Ticketing — SLA warning/breach reminders for
    # support tickets & disputes.
    from services import notifications as _sla_svc
    _sla_interval = max(1, cfg.get_int("sla_check_interval_minutes", 5)) * 60
    job_queue.run_repeating(_sla_svc.sla_reminder_job, interval=_sla_interval, first=60)

    # V22: Favorites — search ConversationHandler MUST come before fav_dispatch
    from handlers.favorites_handlers import (
        build_fav_search_conv, fav_dispatch, my_favorites_menu,
    )
    application.add_handler(build_fav_search_conv())
    application.add_handler(CallbackQueryHandler(fav_dispatch, pattern=r"^fav:"))

    # V22: Product Compare — user-facing compare callbacks (cmp:*)
    from handlers.compare_handlers import cmp_dispatch
    application.add_handler(CallbackQueryHandler(cmp_dispatch, pattern=r"^cmp:"))

    # V23: Recently Viewed — search ConversationHandler MUST come before rv_dispatch
    from handlers.recently_viewed_handlers import (
        build_rv_search_conv, rv_dispatch,
    )
    application.add_handler(build_rv_search_conv())
    application.add_handler(CallbackQueryHandler(rv_dispatch, pattern=r"^rv:"))

    # V23: Price History — product price timeline
    from handlers.price_history_handlers import ph_dispatch
    application.add_handler(CallbackQueryHandler(ph_dispatch, pattern=r"^ph:"))

    # V23: Inventory Reservation System — user-facing reservation UI
    from handlers.reservation_handlers import irs_dispatch
    application.add_handler(CallbackQueryHandler(irs_dispatch, pattern=r"^irs:"))

    # V22: Subscription Expiry Reminders — separate from billing-renewal reminders.
    # Sends one-time messages at configurable intervals before a subscription expires.
    from services import subscription_reminder as _sub_exp_svc
    _sub_exp_interval = max(1, cfg.get_int(
        "sub_expiry_reminder_check_interval_minutes", 60)) * 60
    job_queue.run_repeating(_sub_exp_svc.expiry_reminder_job,
                            interval=_sub_exp_interval, first=180)

    # ─── V30: Admin Dashboard Widget System ──────────────────────────────
    from handlers.admin_dashboard_widgets import register_handlers as _adw_register
    _adw_register(application)

    # ─── V31: Smart Fraud Detection System ───────────────────────────────
    from handlers.admin_fraud_detection import register_handlers as _fds_register
    _fds_register(application)

    # ─── V32: Login Activity & Device Management ──────────────────────────
    from handlers.admin_login_activity import register_handlers as _lam_register
    _lam_register(application)
    from services.login_activity import cleanup_expired_sessions_job as _lam_cleanup
    job_queue.run_repeating(_lam_cleanup, interval=3600, first=600)

    # ─── V33: Customer Notes & CRM System ────────────────────────────────
    from handlers.admin_customer_crm import register_handlers as _crm_register
    _crm_register(application)
    from services.customer_crm import reminder_check_job as _crm_reminder_job
    job_queue.run_repeating(_crm_reminder_job, interval=3600, first=900)

    # ─── V9: Premium Admin Control Center dispatcher ─────────────────────
    from handlers.admin_control_center import acc_dispatch, render_control_center
    from handlers import admin_wallets

    # /panel opens the new Admin Control Center directly.
    async def _panel_command(update, context):
        from utils.helpers import is_admin
        if not is_admin(update.effective_user.id):
            return
        await render_control_center(update, context)
    application.add_handler(CommandHandler("panel", _panel_command))

    # ── noop handler — must come AFTER specific patterns to avoid false positives ──
    application.add_handler(CallbackQueryHandler(lambda u, c: None, pattern="^noop$"))

    # ─── V18: User Features (Wishlist, Price Alerts, Recently Viewed,
    #          Quick Buy, Preferred Payment, Buy Again) ─────────────────────────
    # IMPORTANT: these must be registered BEFORE purchase_conv so that
    # uf:* callbacks pressed while the user is mid-conversation are handled
    # here (same group, earlier registration = first match wins).
    application.add_handler(CallbackQueryHandler(feature_handlers.wishlist_menu,        pattern=r"^uf:wl$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.wishlist_add,         pattern=r"^uf:wl:a:\d+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.wishlist_remove,      pattern=r"^uf:wl:r:\d+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.price_alerts_menu,    pattern=r"^uf:pa$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.price_alert_subscribe,   pattern=r"^uf:pa:s:\d+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.price_alert_unsubscribe, pattern=r"^uf:pa:u:\d+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.recently_viewed_menu, pattern=r"^uf:rv$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.quick_buy_menu,       pattern=r"^uf:qb$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.quick_buy_execute,    pattern=r"^uf:qb:b:\d+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.preferred_payment_menu, pattern=r"^uf:pp$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.preferred_payment_set,  pattern=r"^uf:pp:s:.+$"))
    application.add_handler(CallbackQueryHandler(feature_handlers.buy_again_menu,       pattern=r"^uf:ba$"))

    # ─── V18: Admin Feature Management Panel (af:* namespace) ─────────────────
    application.add_handler(CallbackQueryHandler(admin_features.features_menu,      pattern=r"^af:menu$"))
    application.add_handler(CallbackQueryHandler(admin_features.feature_detail,     pattern=r"^af:f:.+$"))
    application.add_handler(CallbackQueryHandler(admin_features.feature_enable,     pattern=r"^af:on:.+$"))
    application.add_handler(CallbackQueryHandler(admin_features.feature_disable,    pattern=r"^af:off:.+$"))
    application.add_handler(CallbackQueryHandler(admin_features.feature_set_option, pattern=r"^af:set:.+$"))

    # ─── Part 3: Sales & Marketing Features ──────────────────────────────────

    # ── Gift Purchase (user-facing conversation) ──────────────────────────────
    from handlers import gift_purchase_handlers
    gift_purchase_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gift_purchase_handlers.gift_start, pattern=r"^gp:start:\d+$"),
        ],
        states={
            gift_purchase_handlers.GF_RECIPIENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               gift_purchase_handlers.gift_recipient_input),
            ],
            gift_purchase_handlers.GF_MESSAGE: [
                CallbackQueryHandler(gift_purchase_handlers.gift_skip_message,  pattern=r"^gp:skip_msg$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               gift_purchase_handlers.gift_message_input),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(gift_purchase_handlers.gift_cancel, pattern=r"^gp:cancel$"),
            CommandHandler("cancel", gift_purchase_handlers.gift_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(gift_purchase_conv)
    application.add_handler(CallbackQueryHandler(
        gift_purchase_handlers.gift_toggle_anon, pattern=r"^gp:toggle_anon$"))

    # ── Gift Card redemption (user-facing conversation) ───────────────────────
    from handlers import gift_card_handlers
    gift_card_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gift_card_handlers.redeem_start, pattern=r"^gc:redeem$"),
        ],
        states={
            gift_card_handlers.GC_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               gift_card_handlers.redeem_code_input),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(gift_card_handlers.redeem_cancel, pattern=r"^gc:cancel$"),
            CommandHandler("cancel", gift_card_handlers.redeem_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(gift_card_conv)
    application.add_handler(CallbackQueryHandler(
        gift_card_handlers.redeem_history, pattern=r"^gc:history$"))

    # ── Admin Gift Purchase panel (agp:*) ─────────────────────────────────────
    from handlers import admin_gift_purchase
    application.add_handler(CallbackQueryHandler(
        admin_gift_purchase.gift_purchase_menu,        pattern=r"^agp:menu$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_purchase.gift_purchase_toggle,      pattern=r"^agp:toggle$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_purchase.gift_purchase_toggle_anon, pattern=r"^agp:toggle_anon$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_purchase.gift_purchase_list,        pattern=r"^agp:list$"))

    # ── Admin Gift Card CRUD (agc:*) — includes a conversation ───────────────
    from handlers import admin_gift_cards
    gift_card_admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_gift_cards.create_start, pattern=r"^agc:create_start$"),
        ],
        states={
            admin_gift_cards.AGC_VALUE: [
                CallbackQueryHandler(admin_gift_cards.create_type_selected, pattern=r"^agc:ctype:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_gift_cards.create_value_input),
            ],
            admin_gift_cards.AGC_EXPIRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_gift_cards.create_expiry_input),
            ],
            admin_gift_cards.AGC_MAXUSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_gift_cards.create_maxuses_input),
            ],
            admin_gift_cards.AGC_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_gift_cards.create_label_input),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_gift_cards.create_cancel, pattern=r"^agc:cancel_create$"),
            CommandHandler("cancel", admin_gift_cards.create_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(gift_card_admin_conv)
    application.add_handler(CallbackQueryHandler(
        admin_gift_cards.gift_card_menu,        pattern=r"^agc:menu$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_cards.gift_card_toggle,      pattern=r"^agc:toggle$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_cards.gift_card_list,        pattern=r"^agc:list:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_cards.gift_card_view,        pattern=r"^agc:view:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_gift_cards.gift_card_deactivate,  pattern=r"^agc:deactivate:\d+$"))

    # ── Admin Bundle Manager (abn:*) — includes conversations ─────────────────
    from handlers import admin_bundles
    bundle_price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_bundles.set_price_start, pattern=r"^abn:setprice:\d+$"),
        ],
        states={
            admin_bundles.ABN_BUNDLE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_bundles.set_price_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    bundle_discount_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_bundles.set_discount_start, pattern=r"^abn:setdisc:\d+$"),
        ],
        states={
            admin_bundles.ABN_BUNDLE_DISCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_bundles.set_discount_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    bundle_addchild_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_bundles.add_child_start, pattern=r"^abn:addchild:\d+$"),
        ],
        states={
            admin_bundles.ABN_CHILD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_bundles.add_child_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(bundle_price_conv)
    application.add_handler(bundle_discount_conv)
    application.add_handler(bundle_addchild_conv)
    application.add_handler(CallbackQueryHandler(
        admin_bundles.bundle_menu,   pattern=r"^abn:menu$"))
    application.add_handler(CallbackQueryHandler(
        admin_bundles.bundle_view,   pattern=r"^abn:view:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_bundles.remove_child,  pattern=r"^abn:rmchild:\d+$"))

    # ── Admin Review Manager (arv:*) ──────────────────────────────────────────
    from handlers import admin_reviews
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_admin_menu,       pattern=r"^arv:menu$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_toggle,           pattern=r"^arv:toggle$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_toggle_approval,  pattern=r"^arv:toggle_approval$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_list,             pattern=r"^arv:list:\w+:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_view,             pattern=r"^arv:view:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_approve,          pattern=r"^arv:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_reject,           pattern=r"^arv:reject:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_hide,             pattern=r"^arv:hide:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_unhide,           pattern=r"^arv:unhide:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_pin,              pattern=r"^arv:pin:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_reviews.review_delete,           pattern=r"^arv:delete:\d+$"))

    # ── User review management (my_reviews, edit, delete) ────────────────────
    application.add_handler(CallbackQueryHandler(
        review_handlers.my_reviews,            pattern=r"^my_reviews$"))
    application.add_handler(CallbackQueryHandler(
        review_handlers.review_manage,         pattern=r"^review_manage_\d+$"))
    application.add_handler(CallbackQueryHandler(
        review_handlers.review_delete_start,   pattern=r"^review_del_\d+$"))
    application.add_handler(CallbackQueryHandler(
        review_handlers.review_delete_confirm, pattern=r"^review_del_confirm_\d+$"))
    # Review edit conversation
    review_edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(review_handlers.review_edit_start, pattern=r"^review_edit_\d+$"),
        ],
        states={
            review_handlers.REVIEW_EDIT_RATING: [
                CallbackQueryHandler(review_handlers.review_edit_rating_pick,
                                     pattern=r"^review_edit_rate_[1-5]$"),
            ],
            review_handlers.REVIEW_EDIT_COMMENT: [
                CallbackQueryHandler(review_handlers.review_edit_skip_comment,
                                     pattern=r"^review_edit_skip_comment$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               review_handlers.review_edit_comment_text),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(review_handlers.review_edit_cancel,
                                 pattern=r"^review_edit_cancel$"),
            CommandHandler("cancel", review_handlers.review_edit_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(review_edit_conv)

    # ─── V19: User Account & Order Features (ua:* namespace) ─────────────────
    application.add_handler(CallbackQueryHandler(account_features.ua_dispatch, pattern=r"^ua:"))

    # ─── V20: Admin Main Menu Manager (mm:* namespace) ────────────────────────
    application.add_handler(CallbackQueryHandler(admin_menu_manager.mm_dispatch, pattern=r"^mm:"))

    # ─── V21: Activity Feed Manager (af:* namespace) ──────────────────────────
    # ConversationHandler for channel ID text input must be registered FIRST
    application.add_handler(admin_activity_feed.build_af_channel_conv())
    application.add_handler(CallbackQueryHandler(admin_activity_feed.af_dispatch, pattern=r"^af:"))

    # ─── V19: Admin Account Feature Management (aaf:* namespace) ─────────────
    application.add_handler(CallbackQueryHandler(
        admin_account_features.account_features_menu,   pattern=r"^aaf:menu$"))
    application.add_handler(CallbackQueryHandler(
        admin_account_features.account_feature_detail,  pattern=r"^aaf:f:.+$"))
    application.add_handler(CallbackQueryHandler(
        admin_account_features.account_feature_enable,  pattern=r"^aaf:on:.+$"))
    application.add_handler(CallbackQueryHandler(
        admin_account_features.account_feature_disable, pattern=r"^aaf:off:.+$"))
    application.add_handler(CallbackQueryHandler(
        admin_account_features.account_feature_set_option, pattern=r"^aaf:set:.+$"))

    # ── User Search conversation (enters on usr:search callback) ───────────────
    from handlers.admin_users import build_user_search_conv, build_balance_conv
    application.add_handler(build_user_search_conv())
    application.add_handler(build_balance_conv())

    # ── Advanced User Profile conversations ────────────────────────────────────
    from handlers.admin_user_profile import build_up_search_conv, build_up_bal_conv
    application.add_handler(build_up_search_conv())
    application.add_handler(build_up_bal_conv())

    # ── Edit Debitable conversation (enters on mp:edit:<tx_id> callback) ───────
    from handlers.admin_manual_payments import build_edit_debitable_conv
    application.add_handler(build_edit_debitable_conv())

    # Wallet adjust conversation (credit / debit) — entered from acc:wal:credit|debit:<uid>
    wallet_adjust_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_wallets.adjust_start_credit,
                                 pattern=r"^acc:wal:credit:\d+$"),
            CallbackQueryHandler(admin_wallets.adjust_start_debit,
                                 pattern=r"^acc:wal:debit:\d+$"),
        ],
        states={
            admin_wallets.ADJ_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_wallets.adjust_amount),
            ],
            admin_wallets.ADJ_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_wallets.adjust_reason),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(wallet_adjust_conv)

    # ── V15: Flash Sale conversations (create / edit) ───────────────────────
    from handlers import admin_promotions

    flash_sale_new_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_promotions.fs_new_start,
                                 pattern=r"^acc:promo:fs_new:(product|category)$"),
        ],
        states={
            admin_promotions.FS_TARGET_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_target_id),
            ],
            admin_promotions.FS_DISCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_discount),
            ],
            admin_promotions.FS_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_start),
            ],
            admin_promotions.FS_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_end),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(flash_sale_new_conv)

    flash_sale_edit_pct_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_promotions.fs_edit_pct_start,
                                 pattern=r"^acc:promo:fs_edit_pct:\d+$"),
        ],
        states={
            admin_promotions.FS_EDIT_PCT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_edit_pct_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(flash_sale_edit_pct_conv)

    flash_sale_edit_end_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_promotions.fs_edit_end_start,
                                 pattern=r"^acc:promo:fs_edit_end:\d+$"),
        ],
        states={
            admin_promotions.FS_EDIT_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_promotions.fs_edit_end_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(flash_sale_edit_end_conv)

    # ── V12: Broadcast Center conversations ─────────────────────────────
    # "✍️ Custom Broadcast" — compose text conversation (also re-entered by
    # the preview's "✏️ Edit Message" button).
    custom_broadcast_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_broadcast_center.custom_start,
                                 pattern=r"^acc:bc:custom:start$"),
            CallbackQueryHandler(admin_broadcast_center.custom_start,
                                 pattern=r"^acc:bc:custom:edit$"),
        ],
        states={
            admin_broadcast_center.BC_CUSTOM_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_broadcast_center.custom_receive_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(custom_broadcast_conv)

    # "📦 Product Broadcast" → "✏️ Edit Message" — replace the generated
    # product-broadcast text with admin-supplied text.
    prod_broadcast_edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_broadcast_center.prod_edit_start,
                                 pattern=r"^acc:bc:prod:edit$"),
        ],
        states={
            admin_broadcast_center.BC_PROD_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               admin_broadcast_center.prod_edit_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(prod_broadcast_edit_conv)

    # ─── V10: Business Scale conversations ─────────────────────────────
    from handlers.admin_suppliers import build_supplier_add_conv
    from handlers.admin_batches import build_batch_add_conv
    from handlers.admin_resellers import build_reseller_convs
    application.add_handler(build_supplier_add_conv())
    application.add_handler(build_batch_add_conv())
    for _c in build_reseller_convs():
        application.add_handler(_c)

    # ─── V24: Supplier Auto Assignment conversations ────────────────────
    from handlers.admin_supplier_auto_assign import build_sas_addprod_conv
    application.add_handler(build_sas_addprod_conv())

    # ─── V25: Order Timeline conversations ─────────────────────────────
    from handlers.admin_order_timeline import build_ots_note_conv
    application.add_handler(build_ots_note_conv())

    # ─── V25: Product FAQ conversations ────────────────────────────────
    from handlers.admin_product_faq import (
        build_pfaq_add_conv, build_pfaq_edit_conv,
        build_pfaq_copy_conv, build_pfaq_search_conv,
    )
    application.add_handler(build_pfaq_add_conv())
    application.add_handler(build_pfaq_edit_conv())
    application.add_handler(build_pfaq_copy_conv())
    application.add_handler(build_pfaq_search_conv())

    from handlers.user_product_faq import (
        pfaq_view_callback, build_user_faq_search_conv,
    )
    application.add_handler(
        CallbackQueryHandler(pfaq_view_callback, pattern=r"^pfaq:view:\d+$")
    )
    application.add_handler(build_user_faq_search_conv())

    # V10: periodic delivery retry sweep (best-effort; deliverer wiring
    # remains in payment_handlers — this job just moves due retries back to
    # PENDING so a future worker can execute them).
    async def _delivery_retry_sweep(context):
        try:
            from services import delivery_queue as _dq
            from database import get_db_session as _gs
            from database.models import DeliveryJob as _DJ
            due = _dq.due_retries(limit=50)
            if not due:
                return
            with _gs() as _s:
                for _jid in due:
                    _j = _s.get(_DJ, _jid)
                    if _j and _j.status == "RETRY_SCHEDULED":
                        _j.status = "PENDING"
                        _j.next_retry_at = None
                _s.commit()
        except Exception:
            logger.exception("delivery retry sweep failed")

    job_queue.run_repeating(_delivery_retry_sweep, interval=60, first=45)

    # ── Part 3: Gift purchase notification sweep (every 60s) ─────────────────
    from handlers.gift_purchase_handlers import process_completed_gifts
    job_queue.run_repeating(process_completed_gifts, interval=60, first=30)

    # ── V20: Advanced Referral Dashboard handlers ────────────────────────────
    from handlers.referral_dashboard import (
        rd_menu, rd_commissions, rd_top_referrers,
        rd_admin_menu, rd_admin_toggle_dashboard, rd_admin_toggle_lifetime,
        rd_admin_withdrawals_list, rd_admin_approve_withdrawal, rd_admin_reject_withdrawal,
        build_rd_admin_convs,
    )
    application.add_handler(CallbackQueryHandler(rd_menu,                     pattern=r"^rd:menu$"))
    application.add_handler(CallbackQueryHandler(rd_commissions,              pattern=r"^rd:comm$"))
    application.add_handler(CallbackQueryHandler(rd_top_referrers,            pattern=r"^rd:top$"))
    application.add_handler(CallbackQueryHandler(rd_admin_menu,               pattern=r"^rd:admin$"))
    application.add_handler(CallbackQueryHandler(rd_admin_toggle_dashboard,   pattern=r"^rd:adm:toggle_dashboard$"))
    application.add_handler(CallbackQueryHandler(rd_admin_toggle_lifetime,    pattern=r"^rd:adm:toggle_lifetime$"))
    application.add_handler(CallbackQueryHandler(rd_admin_withdrawals_list,   pattern=r"^rd:adm:withdrawals$"))
    application.add_handler(CallbackQueryHandler(rd_admin_approve_withdrawal, pattern=r"^rd:adm:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(rd_admin_reject_withdrawal,  pattern=r"^rd:adm:reject:\d+$"))
    # NOTE: build_rd_withdraw_conv() is replaced by the V29 withdrawal approval system below
    for _rd_conv in build_rd_admin_convs():
        application.add_handler(_rd_conv)

    # ── V29: Withdrawal Approval System ─────────────────────────────────────
    # Replaces the old single-step rd:withdraw flow with the full approval
    # workflow: payment method → wallet address → amount → admin approval panel.
    from handlers.withdrawal_approval import register_handlers as _wda_register
    _wda_register(application)

    # ── V20: Enhanced Maintenance Mode handlers ──────────────────────────────
    from handlers.admin_maintenance import (
        maintenance_menu, maintenance_toggle,
        maintenance_whitelist_view, maintenance_whitelist_remove,
        build_maintenance_convs,
    )
    application.add_handler(CallbackQueryHandler(maintenance_menu,             pattern=r"^maint:menu$"))
    application.add_handler(CallbackQueryHandler(maintenance_toggle,           pattern=r"^maint:toggle$"))
    application.add_handler(CallbackQueryHandler(maintenance_whitelist_view,   pattern=r"^maint:wl$"))
    application.add_handler(CallbackQueryHandler(maintenance_whitelist_remove, pattern=r"^maint:wl:rm:\d+$"))
    for _maint_conv in build_maintenance_convs():
        application.add_handler(_maint_conv)

    # ── V20: Announcement System handlers ───────────────────────────────────
    from handlers.admin_announcements import (
        announcements_menu, ann_toggle, ann_list, ann_pinned, ann_scheduled,
        ann_view, ann_pin, ann_unpin, ann_activate, ann_deactivate, ann_delete,
        ann_send_now, ann_mark_read, build_ann_create_conv,
        announcement_send_job,
    )
    application.add_handler(CallbackQueryHandler(announcements_menu, pattern=r"^ann:menu$"))
    application.add_handler(CallbackQueryHandler(ann_toggle,         pattern=r"^ann:toggle$"))
    application.add_handler(CallbackQueryHandler(ann_list,           pattern=r"^ann:list:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_pinned,         pattern=r"^ann:pinned$"))
    application.add_handler(CallbackQueryHandler(ann_scheduled,      pattern=r"^ann:scheduled$"))
    application.add_handler(CallbackQueryHandler(ann_view,           pattern=r"^ann:view:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_pin,            pattern=r"^ann:pin:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_unpin,          pattern=r"^ann:unpin:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_activate,       pattern=r"^ann:activate:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_deactivate,     pattern=r"^ann:deactivate:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_delete,         pattern=r"^ann:delete:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_send_now,       pattern=r"^ann:send:\d+$"))
    application.add_handler(CallbackQueryHandler(ann_mark_read,      pattern=r"^ann:read:\d+$"))
    application.add_handler(build_ann_create_conv())
    job_queue.run_repeating(announcement_send_job, interval=60, first=30)

    # ── V20: Enhanced support — admin delete / assign ────────────────────────
    application.add_handler(CallbackQueryHandler(
        support_handlers.admin_ticket_delete_callback,
        pattern=r"^adm_tk_delete_\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        support_handlers.admin_ticket_assign_callback,
        pattern=r"^adm_tk_assign_\d+$",
    ))

    # ── V21 / V26: Scheduled Broadcast handlers ─────────────────────────────
    from handlers.admin_scheduled_broadcast import (
        build_asb_conv,
        asb_menu, asb_list, asb_view, asb_send_now, asb_cancel_broadcast,
        asb_delete_ask, asb_delete_ok, asb_duplicate, asb_preview,
        asb_pause, asb_resume, asb_retry, asb_logs,
        asb_stats, asb_settings, asb_settings_status, asb_settings_toggle,
        scheduled_broadcast_job,
        # Enterprise Broadcast Center (V44)
        asb_test_send, asb_drafts, asb_scheduled_list,
        asb_reports, asb_report_view, asb_export,
        asb_continue, asb_interrupted_list, asb_settings_adjust,
    )
    application.add_handler(build_asb_conv())
    # Core CRUD
    application.add_handler(CallbackQueryHandler(asb_menu,             pattern=r"^asb:menu$"))
    application.add_handler(CallbackQueryHandler(asb_list,             pattern=r"^asb:list:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_view,             pattern=r"^asb:view:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_send_now,         pattern=r"^asb:send:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_cancel_broadcast, pattern=r"^asb:cancel:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_delete_ask,       pattern=r"^asb:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_delete_ok,        pattern=r"^asb:del_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_duplicate,        pattern=r"^asb:dup:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_preview,          pattern=r"^asb:preview:\d+$"))
    # V26: Pause / Resume / Retry / Logs
    application.add_handler(CallbackQueryHandler(asb_pause,            pattern=r"^asb:pause:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_resume,           pattern=r"^asb:resume:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_retry,            pattern=r"^asb:retry:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_logs,             pattern=r"^asb:logs:\d+$"))
    # V26: Stats & Settings
    application.add_handler(CallbackQueryHandler(asb_stats,            pattern=r"^asb:stats$"))
    application.add_handler(CallbackQueryHandler(asb_settings,         pattern=r"^asb:settings$"))
    application.add_handler(CallbackQueryHandler(asb_settings_status,  pattern=r"^asb:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(asb_settings_toggle,  pattern=r"^asb:settings:toggle:.+$"))
    # Enterprise Broadcast Center (V44): new handlers
    application.add_handler(CallbackQueryHandler(asb_test_send,        pattern=r"^asb:test_send:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_drafts,           pattern=r"^asb:drafts$"))
    application.add_handler(CallbackQueryHandler(asb_scheduled_list,   pattern=r"^asb:scheduled_list$"))
    application.add_handler(CallbackQueryHandler(asb_reports,          pattern=r"^asb:reports$"))
    application.add_handler(CallbackQueryHandler(asb_report_view,      pattern=r"^asb:report:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_export,           pattern=r"^asb:export:\d+:.+$"))
    application.add_handler(CallbackQueryHandler(asb_continue,         pattern=r"^asb:continue:\d+$"))
    application.add_handler(CallbackQueryHandler(asb_interrupted_list, pattern=r"^asb:interrupted$"))
    application.add_handler(CallbackQueryHandler(asb_settings_adjust,  pattern=r"^asb:settings:adj:.+$"))
    # Scheduled job (every 60 seconds)
    job_queue.run_repeating(scheduled_broadcast_job, interval=60, first=30)

    # Enterprise Broadcast Analytics (V44.3)
    from handlers.admin_broadcast_analytics import (
        build_bca_conv,
        bca_menu, bca_analytics,
        bca_history, bca_history_clear_search,
        bca_reports, bca_report_view,
        bca_period_reports, bca_period_view, bca_period_export,
        bca_export_menu, bca_export, bca_export_hub,
        bca_errors,
        bca_retry_menu, bca_retry_all, bca_retry_clear,
        bca_archive, bca_delete_ask, bca_delete_ok,
        bca_settings, bca_settings_status, bca_settings_toggle, bca_settings_adj,
    )
    application.add_handler(build_bca_conv())
    # Dashboard & history
    application.add_handler(CallbackQueryHandler(bca_menu,                  pattern=r"^bca:menu$"))
    application.add_handler(CallbackQueryHandler(bca_analytics,             pattern=r"^bca:analytics:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_history,               pattern=r"^bca:history(:.+)?$"))
    application.add_handler(CallbackQueryHandler(bca_history_clear_search,  pattern=r"^bca:history:clear_search$"))
    # Reports
    application.add_handler(CallbackQueryHandler(bca_reports,               pattern=r"^bca:reports:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_report_view,           pattern=r"^bca:report:.+:\d+$"))
    # Period reports
    application.add_handler(CallbackQueryHandler(bca_period_reports,        pattern=r"^bca:period_reports$"))
    application.add_handler(CallbackQueryHandler(bca_period_view,           pattern=r"^bca:period_view:.+$"))
    application.add_handler(CallbackQueryHandler(bca_period_export,         pattern=r"^bca:period_export:.+:.+$"))
    # Export
    application.add_handler(CallbackQueryHandler(bca_export_hub,            pattern=r"^bca:export_hub$"))
    application.add_handler(CallbackQueryHandler(bca_export_menu,           pattern=r"^bca:export_menu:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_export,                pattern=r"^bca:export:\d+:.+$"))
    # Error management & retry
    application.add_handler(CallbackQueryHandler(bca_errors,                pattern=r"^bca:errors:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_retry_menu,            pattern=r"^bca:retry_menu:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_retry_all,             pattern=r"^bca:retry_all:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_retry_clear,           pattern=r"^bca:retry_clear:\d+$"))
    # Archive / delete
    application.add_handler(CallbackQueryHandler(bca_archive,               pattern=r"^bca:archive:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_delete_ask,            pattern=r"^bca:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(bca_delete_ok,             pattern=r"^bca:del_ok:\d+$"))
    # Settings
    application.add_handler(CallbackQueryHandler(bca_settings,              pattern=r"^bca:settings$"))
    application.add_handler(CallbackQueryHandler(bca_settings_status,       pattern=r"^bca:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(bca_settings_toggle,       pattern=r"^bca:settings:toggle:.+$"))
    application.add_handler(CallbackQueryHandler(bca_settings_adj,          pattern=r"^bca:settings:adj:.+$"))

    # Advanced Broadcast Types (V44.2)
    from handlers.admin_broadcast_types import (
        build_abt_conv,
        abt_menu, abt_compose,
        abt_audience, abt_audience_sel,
        abt_filters, abt_filter_toggle, abt_filters_clear,
        abt_variables, abt_var_edit, abt_vars_clear,
        abt_audience_preview, abt_preview_msg,
        abt_test_self, abt_test_user_ask,
        abt_send_now, abt_schedule_ask,
        abt_settings, abt_settings_status, abt_settings_toggle,
    )
    application.add_handler(build_abt_conv())
    # Core navigation
    application.add_handler(CallbackQueryHandler(abt_menu,              pattern=r"^abt:menu$"))
    application.add_handler(CallbackQueryHandler(abt_compose,           pattern=r"^abt:compose:.+$"))
    # Audience
    application.add_handler(CallbackQueryHandler(abt_audience,          pattern=r"^abt:audience(:\d+)?$"))
    application.add_handler(CallbackQueryHandler(abt_audience_preview,  pattern=r"^abt:audience_preview$"))
    # Filters
    application.add_handler(CallbackQueryHandler(abt_filters,           pattern=r"^abt:filters$"))
    application.add_handler(CallbackQueryHandler(abt_filters_clear,     pattern=r"^abt:filters_clear$"))
    # Variables
    application.add_handler(CallbackQueryHandler(abt_variables,         pattern=r"^abt:variables$"))
    application.add_handler(CallbackQueryHandler(abt_vars_clear,        pattern=r"^abt:vars_clear$"))
    # Preview / test / send
    application.add_handler(CallbackQueryHandler(abt_preview_msg,       pattern=r"^abt:preview_msg$"))
    application.add_handler(CallbackQueryHandler(abt_test_self,         pattern=r"^abt:test_self$"))
    application.add_handler(CallbackQueryHandler(abt_send_now,          pattern=r"^abt:send_now$"))
    # Settings
    application.add_handler(CallbackQueryHandler(abt_settings,          pattern=r"^abt:settings$"))
    application.add_handler(CallbackQueryHandler(abt_settings_status,   pattern=r"^abt:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(abt_settings_toggle,   pattern=r"^abt:settings:toggle:.+$"))

    # ── V27: Webhook Monitor & API Health handlers ───────────────────────────
    from handlers.admin_webhook_monitor import awm_dispatch
    from services.health_monitor import health_check_job
    application.add_handler(CallbackQueryHandler(awm_dispatch, pattern=r"^awm:.+$"))
    # Health check job — interval from BotConfig (default 300 s = 5 min)
    _hc_interval = int(cfg.get_int("health_check_interval", 300))
    job_queue.run_repeating(health_check_job, interval=_hc_interval, first=60)

    # ── V28: Product Clone & Template System ─────────────────────────────────
    from handlers.admin_product_clone import build_pct_conv, pct_dispatch
    # ConversationHandler claims entry points (save/edit template, clone with name/price/stock)
    application.add_handler(build_pct_conv())
    # Dispatcher handles all remaining pct:* callbacks (menu, quick clone, bulk, history, settings)
    application.add_handler(CallbackQueryHandler(pct_dispatch, pattern=r"^pct:.+$"))

    # ── V46: Enterprise Product Template System ───────────────────────────────
    from handlers.admin_product_templates import register_handlers as _apt_reg
    _apt_reg(application)

    # ── V21: Advanced Analytics handlers ────────────────────────────────────
    from handlers.admin_advanced_analytics import aana_dispatch
    application.add_handler(CallbackQueryHandler(aana_dispatch, pattern=r"^aana:.+$"))

    # ── V21: Multi-Language handlers ─────────────────────────────────────────
    from handlers.admin_language import alng_dispatch, build_alng_import_conv
    application.add_handler(build_alng_import_conv())
    application.add_handler(CallbackQueryHandler(alng_dispatch, pattern=r"^alng:.+$"))

    # ── V21: Advanced Coupons handlers ───────────────────────────────────────
    from handlers.admin_advanced_coupons import acpn_dispatch, build_acpn_conv
    application.add_handler(build_acpn_conv())
    application.add_handler(CallbackQueryHandler(acpn_dispatch, pattern=r"^acpn:.+$"))

    # ── V21: Refund handlers ─────────────────────────────────────────────────
    from handlers.admin_refund import (
        aref_dispatch, build_aref_manual_conv, build_aref_reject_conv,
        auto_refund_job,
    )
    application.add_handler(build_aref_manual_conv())
    application.add_handler(build_aref_reject_conv())
    application.add_handler(CallbackQueryHandler(aref_dispatch, pattern=r"^aref:.+$"))
    job_queue.run_repeating(auto_refund_job, interval=3600, first=300)

    # ── V21: Enhanced Audit Log conversation ─────────────────────────────────
    from handlers.admin_audit_enhanced import build_audit_search_conv
    application.add_handler(build_audit_search_conv())

    # ── V34: Settings Backup import (file upload conversation) ────────────────
    from handlers.admin_backups import build_bak_import_conv, settings_backup_auto_job
    application.add_handler(build_bak_import_conv())

    # ── V34: Auto settings backup job ────────────────────────────────────────
    _sbi_hours = cfg.get_int("backup_settings_interval_hours", 24)
    job_queue.run_repeating(
        settings_backup_auto_job,
        interval=_sbi_hours * 3600,
        first=600,
        name="settings_backup_auto",
    )

    # ── V34: Auto diagnostics scan job ───────────────────────────────────────
    from handlers.admin_diagnostics import diagnostics_auto_scan_job
    _diag_hours = cfg.get_int("diagnostics_scan_interval_hours", 6)
    job_queue.run_repeating(
        diagnostics_auto_scan_job,
        interval=_diag_hours * 3600,
        first=900,
        name="diagnostics_auto_scan",
    )

    # ── V36: Delivery Management System ─────────────────────────────────────
    from handlers.admin_delivery_manager import register_handlers as _dms_register
    _dms_register(application)

    # V35: Bulk Product Import/Export
    from handlers.admin_bulk_products import register_handlers as _bpim_register
    _bpim_register(application)

    # V35: Bulk User Management
    from handlers.admin_bulk_users import register_handlers as _bum_register
    _bum_register(application)

    # ── V37: Admin Notification Center ───────────────────────────────────────
    from handlers.admin_notification_center import register_handlers as _anc_register
    _anc_register(application)

    # ── Notification Settings module (delivery mode, log channel, and
    #    per-category/per-event enable-disable) ───────────────────────────
    from handlers.admin_notification_settings import register_handlers as _nsm_register
    _nsm_register(application)

    # ── V37: File & License Key Manager ──────────────────────────────────────
    from handlers.admin_file_license_manager import register_handlers as _flm_register
    _flm_register(application)

    # ── V38: Flash Sale Manager ───────────────────────────────────────────────
    from handlers.admin_flash_sale_manager import register_handlers as _fsm_register
    _fsm_register(application)
    # Flash sale scheduler job — runs every 60 seconds
    from services.flash_sale_service import flash_sale_scheduler_job
    job_queue.run_repeating(flash_sale_scheduler_job, interval=60, first=30, name="flash_sale_tick")

    # ── V39: Multi-Currency Wallet ────────────────────────────────────────────
    from handlers.wallet_multicurrency_handlers import register_handlers as _mcw_reg
    from handlers.admin_multicurrency_wallet import register_handlers as _amcw_reg
    _mcw_reg(application)
    _amcw_reg(application)
    # Seed default currencies on startup (safe — skips existing rows)
    from services.multicurrency_wallet import seed_default_currencies
    seed_default_currencies()

    # ── V39: Exchange Rate Manager ────────────────────────────────────────────
    from handlers.admin_exchange_rate import register_handlers as _aerm_reg
    _aerm_reg(application)
    # Seed default exchange rate pairs on startup
    from services.exchange_rate_service import seed_default_pairs, exchange_rate_scheduler_job
    seed_default_pairs()
    # Exchange rate auto-update job — checks which pairs are due for refresh
    _erm_interval = int(cfg.get_int("erm_scheduler_interval_seconds", 60))
    job_queue.run_repeating(
        exchange_rate_scheduler_job,
        interval=_erm_interval,
        first=45,
        name="exchange_rate_tick",
    )

    # ── V40: Business Insights & Sales Forecast ───────────────────────────────
    from handlers.admin_business_insights import register_handlers as _abiz_reg
    from handlers.admin_sales_forecast import register_handlers as _asf_reg
    _abiz_reg(application)
    _asf_reg(application)

    # ── V40: Anti-Spam Middleware (group -1 — runs before all other handlers) ─
    from services.anti_spam import antispam_middleware
    application.add_handler(TypeHandler(_TgUpdate, antispam_middleware), group=-1)

    # ── V40: Anti-Spam Admin Handler ─────────────────────────────────────────
    from handlers.admin_anti_spam import register_handlers as _aasm_reg
    _aasm_reg(application)

    # ── V41: VIP Tier Manager ─────────────────────────────────────────────────
    from handlers.admin_vip_manager import register_handlers as _avip_reg
    from handlers.vip_handlers import (
        vip_profile, vip_claim_reward, vip_pts_history,
    )
    _avip_reg(application)
    application.add_handler(CallbackQueryHandler(vip_profile,       pattern=r"^vip_profile$"))
    application.add_handler(CallbackQueryHandler(vip_claim_reward,  pattern=r"^vip_claim:\d+$"))
    application.add_handler(CallbackQueryHandler(vip_pts_history,   pattern=r"^vip_pts_history$"))

    # ── V41: API Key & Integration Manager ───────────────────────────────────
    from handlers.admin_api_manager import register_handlers as _aaim_reg
    _aaim_reg(application)

    # ── V41: Seed built-in integrations + schedule health checks ─────────────
    from services.api_integration_service import (
        seed_built_in_integrations, health_check_job,
    )
    seed_built_in_integrations()
    _aim_interval = cfg.get_int("aim_health_check_interval_minutes", 15)
    job_queue.run_repeating(
        health_check_job,
        interval=_aim_interval * 60,
        first=60,
        name="aim_health_check",
    )

    # ── V40: Daily analytics snapshot job — runs at 00:05 UTC each day ───────
    from services.sales_forecast import daily_report_job, weekly_report_job
    job_queue.run_daily(daily_report_job,  time=__import__("datetime").time(0, 5, 0), name="biz_daily_report")
    job_queue.run_daily(weekly_report_job, time=__import__("datetime").time(0, 10, 0),
                        days=(0,), name="biz_weekly_report")

    # ── V42: Plugin & Module Manager ─────────────────────────────────────────
    from handlers.admin_module_manager import register_handlers as _pmm_reg
    _pmm_reg(application)

    # ── V42: Seed built-in modules ────────────────────────────────────────────
    from services.module_manager import seed_modules as _seed_modules
    _seed_modules()

    # ── V42: Global Activity Timeline ─────────────────────────────────────────
    from handlers.admin_global_timeline import register_handlers as _gat_reg
    _gat_reg(application)

    # ── V43: Data Export Center ───────────────────────────────────────────────
    from handlers.admin_data_export import register_handlers as _dec_reg
    _dec_reg(application)

    # ── V43: Global Search Engine ─────────────────────────────────────────────
    from handlers.admin_global_search import register_handlers as _gse_reg
    _gse_reg(application)

    # ── V43: Scheduled export job — checks every 5 minutes ───────────────────
    from services.data_export_service import process_scheduled_jobs as _dec_scheduler
    job_queue.run_repeating(_dec_scheduler, interval=300, first=60,
                            name="dec_scheduled_exports")

    # ── V44: Performance & Cache Manager ──────────────────────────────────────
    from handlers.admin_performance_manager import register_handlers as _pcm_reg
    _pcm_reg(application)

    # ── V44: Performance snapshot job — every 15 minutes ─────────────────────
    from services.performance_cache_service import take_snapshot as _pcm_snapshot
    job_queue.run_repeating(_pcm_snapshot, interval=900, first=120,
                            name="pcm_performance_snapshot")

    # ── V44: Auto-maintenance job — every 24 hours ────────────────────────────
    from services.performance_cache_service import run_auto_maintenance as _pcm_maint
    job_queue.run_repeating(_pcm_maint, interval=86400, first=3600,
                            name="pcm_auto_maintenance")

    # ── V44.4: Enterprise Broadcast Campaign Manager ──────────────────────────
    from handlers.admin_broadcast_campaign_manager import register_handlers as _bcm_reg
    _bcm_reg(application)

    # Campaign scheduler job — checks for due campaigns every 60 seconds
    from services.broadcast_campaign_service import campaign_scheduler_job as _bcm_scheduler
    job_queue.run_repeating(_bcm_scheduler, interval=60, first=45,
                            name="bcm_campaign_scheduler")

    # ── V45: Enterprise Features ──────────────────────────────────────────────

    # V45: Admin Restock Notifications panel
    from handlers.admin_restock_notifications import register_handlers as _rsn_reg
    _rsn_reg(application)

    # V45: User Restock Notification subscribe/unsubscribe
    from handlers.user_restock_notifications import register_handlers as _urns_reg
    _urns_reg(application)

    # V45: Restock background job — checks every 5 minutes
    from services.restock_service import process_restock_notifications as _rsn_job
    job_queue.run_repeating(_rsn_job, interval=300, first=120,
                            name="rsn_restock_notifications")

    # V45: Product Scheduler admin panel
    from handlers.admin_product_scheduler import register_handlers as _aps_reg
    _aps_reg(application)

    # V45: Product Scheduler background job — executes due schedules every 60s
    from services.product_scheduler_service import process_due_schedules as _aps_job
    job_queue.run_repeating(_aps_job, interval=60, first=30,
                            name="aps_product_scheduler")

    # V45: Admin Recommendation Engine management panel
    from handlers.admin_recommendations import register_handlers as _arec_reg
    _arec_reg(application)

    # V45: User-facing Recommendation handlers
    from handlers.user_recommendations import register_handlers as _urec_reg
    _urec_reg(application)

    # V45: Customer Segmentation & Tags admin panel
    from handlers.admin_customer_segmentation import register_handlers as _cseg_reg
    _cseg_reg(application)

    # Central dispatcher for every acc:* callback that is NOT a wallet adjust entry
    # Also excludes V21 namespaces that have their own handlers above
    application.add_handler(CallbackQueryHandler(
        acc_dispatch,
        pattern=r"^acc:(?!wal:(?:credit|debit):\d+$)(?!sup:add$)(?!bat:add$)(?!res:(?:add|assign)$)"
                r"(?!bc:custom:(?:start|edit)$)(?!bc:prod:edit$)"
                r"(?!promo:fs_new:(?:product|category)$)"
                r"(?!promo:fs_edit_pct:\d+$)(?!promo:fs_edit_end:\d+$).+$"))

    # Copy-link / download-links buttons on the purchase success keyboard
    from services.purchase_success import (
        copy_link_callback,
        copy_all_links_callback,
        download_links_txt_callback,
    )
    application.add_handler(CallbackQueryHandler(copy_link_callback,          pattern=r"^copy_link_\d+$"))
    application.add_handler(CallbackQueryHandler(copy_all_links_callback,     pattern=r"^copy_all_links_\d+$"))
    application.add_handler(CallbackQueryHandler(download_links_txt_callback, pattern=r"^download_links_txt_\d+$"))

    # ── Enterprise Order Search (aos: namespace) ──────────────────────────
    # ConversationHandler must be registered first (entry on aos:menu callback).
    application.add_handler(_aos.build_aos_conv())
    # Pure callbacks — no conversation state needed.
    application.add_handler(CallbackQueryHandler(_aos.aos_view,   pattern=r"^aos:view:\d+$"))
    application.add_handler(CallbackQueryHandler(_aos.aos_copy,   pattern=r"^aos:copy:\d+$"))
    # Standalone cancel — covers stale Cancel buttons from ended conversations.
    application.add_handler(CallbackQueryHandler(_aos.aos_cancel, pattern=r"^aos:cancel$"))

    # Register global error handler — catches every unhandled exception
    application.add_error_handler(global_error_handler)

    # Start the bot (Phase 4: polling OR webhook mode)
    logger.info("Bot started successfully!")

    allowed = ["message", "callback_query", "pre_checkout_query"]

    if settings.RUN_MODE == "webhook":
        if not settings.WEBHOOK_URL:
            logger.error("RUN_MODE=webhook but WEBHOOK_URL is not set. Falling back to polling.")
            application.run_polling(allowed_updates=allowed)
        else:
            full_url = settings.WEBHOOK_URL.rstrip("/") + settings.WEBHOOK_PATH
            logger.info(f"Starting webhook server on {settings.WEBHOOK_LISTEN}:{settings.WEBHOOK_PORT}")
            logger.info(f"Telegram will POST updates to: {full_url}")
            application.run_webhook(
                listen=settings.WEBHOOK_LISTEN,
                port=settings.WEBHOOK_PORT,
                url_path=settings.WEBHOOK_PATH.lstrip("/"),
                webhook_url=full_url,
                secret_token=settings.WEBHOOK_SECRET or None,
                allowed_updates=allowed,
            )
    else:
        application.run_polling(allowed_updates=allowed)




if __name__ == "__main__":
    main()
