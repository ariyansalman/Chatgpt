"""Exchange Rate Manager — V39

Adds:
  • exchange_rate_pairs    — configured currency pairs with rates & settings
  • exchange_rate_history  — historical rate snapshots per pair
  • exchange_rate_logs     — admin action audit trail

Revision ID: 20260904_exchange_rate_manager
Revises:     20260903_multicurrency_wallet
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260904_exchange_rate_manager"
down_revision = "20260903_multicurrency_wallet"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS = [
    ("exchange_rate_manager_status", "str", "enabled", "exchange_rates",
     "Exchange Rate Manager Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("erm_auto_update_enabled", "bool", "true", "exchange_rates",
     "Auto-Update Rates",
     "When ON, the bot automatically refreshes exchange rates on their configured interval."),
    ("erm_scheduler_interval_seconds", "int", "60", "exchange_rates",
     "Scheduler Tick (seconds)",
     "How often the auto-update job runs to check for pairs due for a refresh."),
    ("erm_default_auto_interval_minutes", "int", "60", "exchange_rates",
     "Default Auto-Update Interval (minutes)",
     "Default auto-update frequency for newly-added pairs."),
    ("erm_default_margin_pct", "float", "0.0", "exchange_rates",
     "Default Margin (%)",
     "Default buy/sell spread applied to newly-added pairs."),
    ("erm_reset_daily_counters", "bool", "true", "exchange_rates",
     "Reset Daily Counters at Midnight",
     "Reset updates_today / failed_updates_today counters each day."),
    ("erm_history_retention_days", "int", "30", "exchange_rates",
     "History Retention (days)",
     "How many days of rate history to keep per pair. 0 = keep forever."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── exchange_rate_pairs ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS exchange_rate_pairs (
            id                    SERIAL PRIMARY KEY,
            from_currency         VARCHAR(16)  NOT NULL,
            to_currency           VARCHAR(16)  NOT NULL,
            display_name          VARCHAR(64),
            mid_rate              FLOAT,
            buy_rate              FLOAT,
            sell_rate             FLOAT,
            margin_pct            FLOAT        NOT NULL DEFAULT 0.0,
            rate_source           VARCHAR(16)  NOT NULL DEFAULT 'manual',
            auto_update_interval  INTEGER      NOT NULL DEFAULT 60,
            api_url               VARCHAR(512),
            api_response_path     VARCHAR(128),
            manual_override_rate  FLOAT,
            is_locked             BOOLEAN      NOT NULL DEFAULT FALSE,
            status                VARCHAR(16)  NOT NULL DEFAULT 'enabled',
            is_active             BOOLEAN      NOT NULL DEFAULT TRUE,
            previous_mid_rate     FLOAT,
            last_updated          TIMESTAMP WITHOUT TIME ZONE,
            last_auto_update      TIMESTAMP WITHOUT TIME ZONE,
            last_update_source    VARCHAR(16),
            last_update_error     TEXT,
            updates_today         INTEGER      NOT NULL DEFAULT 0,
            failed_updates_today  INTEGER      NOT NULL DEFAULT 0,
            created_at            TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_erp_pair UNIQUE (from_currency, to_currency)
        )
    """))
    for col, idx in [
        ("from_currency", "ix_erp_from"),
        ("to_currency",   "ix_erp_to"),
        ("is_active",     "ix_erp_is_active"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON exchange_rate_pairs ({col})"
        ))

    # ── Seed default pairs ────────────────────────────────────────────────────
    default_pairs = [
        ("USD",  "BDT",  "USD / BDT",  "auto_api", 60),
        ("USD",  "USDT", "USD / USDT", "auto_api", 5),
        ("USDT", "BDT",  "USDT / BDT", "auto_api", 60),
        ("BTC",  "USD",  "BTC / USD",  "auto_api", 5),
        ("ETH",  "USD",  "ETH / USD",  "auto_api", 5),
        ("LTC",  "USD",  "LTC / USD",  "auto_api", 15),
        ("BNB",  "USD",  "BNB / USD",  "auto_api", 15),
        ("TRX",  "USD",  "TRX / USD",  "auto_api", 15),
    ]
    for from_c, to_c, name, source, interval in default_pairs:
        conn.execute(sa.text("""
            INSERT INTO exchange_rate_pairs
                (from_currency, to_currency, display_name, rate_source,
                 auto_update_interval, status, is_active)
            VALUES (:from_c, :to_c, :name, :source, :interval, 'enabled', TRUE)
            ON CONFLICT (from_currency, to_currency) DO NOTHING
        """), {"from_c": from_c, "to_c": to_c, "name": name,
               "source": source, "interval": interval})

    # ── exchange_rate_history ─────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS exchange_rate_history (
            id              SERIAL PRIMARY KEY,
            pair_id         INTEGER NOT NULL
                                REFERENCES exchange_rate_pairs(id) ON DELETE CASCADE,
            from_currency   VARCHAR(16) NOT NULL,
            to_currency     VARCHAR(16) NOT NULL,
            mid_rate        FLOAT,
            buy_rate        FLOAT,
            sell_rate       FLOAT,
            margin_pct      FLOAT,
            source          VARCHAR(16) NOT NULL DEFAULT 'manual',
            recorded_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("pair_id",     "ix_erh_pair_id"),
        ("recorded_at", "ix_erh_recorded_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON exchange_rate_history ({col})"
        ))

    # ── exchange_rate_logs ────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS exchange_rate_logs (
            id            SERIAL PRIMARY KEY,
            pair_id       INTEGER NOT NULL
                              REFERENCES exchange_rate_pairs(id) ON DELETE CASCADE,
            action        VARCHAR(64) NOT NULL,
            old_rate      FLOAT,
            new_rate      FLOAT,
            actor_type    VARCHAR(16) NOT NULL DEFAULT 'system',
            actor_id      BIGINT,
            notes         TEXT,
            created_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("pair_id",    "ix_erl_pair_id"),
        ("created_at", "ix_erl_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON exchange_rate_logs ({col})"
        ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val,
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS exchange_rate_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS exchange_rate_history"))
    conn.execute(sa.text("DROP TABLE IF EXISTS exchange_rate_pairs"))
    for row in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": row[0]})
