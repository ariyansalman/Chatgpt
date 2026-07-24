"""Smart Fraud Detection System — V31

Adds:
  • fraud_user_risk      — per-user risk score, freeze/suspend/whitelist/blacklist state
  • fraud_logs           — every detection event with action taken
  • fraud_wallet_blacklist — blacklisted wallet addresses
  • 17 new bot_config keys with fds_* prefix

Revision ID: 20260826_fraud_detection
Revises:     20260825_admin_dashboard_widgets
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260826_fraud_detection"
down_revision = "20260825_admin_dashboard_widgets"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple[str, str, str, str]] = [
    # (key, type, default_value, category)
    ("fds_status",                  "str",  "enabled", "security"),
    ("fds_check_dup_txid",          "bool", "true",    "security"),
    ("fds_check_dup_wallet",        "bool", "true",    "security"),
    ("fds_check_dup_deposit",       "bool", "true",    "security"),
    ("fds_check_dup_withdrawal",    "bool", "true",    "security"),
    ("fds_check_referral_abuse",    "bool", "true",    "security"),
    ("fds_check_coupon_abuse",      "bool", "true",    "security"),
    ("fds_max_failed_payments",     "int",  "5",       "security"),
    ("fds_max_daily_withdrawals",   "int",  "3",       "security"),
    ("fds_max_daily_deposits",      "int",  "10",      "security"),
    ("fds_max_daily_orders",        "int",  "20",      "security"),
    ("fds_risk_threshold_medium",   "int",  "30",      "security"),
    ("fds_risk_threshold_high",     "int",  "60",      "security"),
    ("fds_risk_threshold_critical", "int",  "90",      "security"),
    ("fds_auto_freeze",             "bool", "true",    "security"),
    ("fds_auto_suspend",            "bool", "false",   "security"),
    ("fds_admin_alerts",            "bool", "true",    "security"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── fraud_user_risk ───────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS fraud_user_risk (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            risk_score      INTEGER NOT NULL DEFAULT 0,
            risk_level      VARCHAR(16) NOT NULL DEFAULT 'low',
            is_frozen       BOOLEAN NOT NULL DEFAULT FALSE,
            is_suspended    BOOLEAN NOT NULL DEFAULT FALSE,
            is_whitelisted  BOOLEAN NOT NULL DEFAULT FALSE,
            is_blacklisted  BOOLEAN NOT NULL DEFAULT FALSE,
            flags_json      TEXT,
            last_checked_at TIMESTAMP WITHOUT TIME ZONE,
            created_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
            updated_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fur_user_id    ON fraud_user_risk (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fur_risk_level ON fraud_user_risk (risk_level)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fur_frozen     ON fraud_user_risk (is_frozen) "
        "WHERE is_frozen = TRUE"
    ))

    # ── fraud_logs ────────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS fraud_logs (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            check_type       VARCHAR(64) NOT NULL,
            risk_score_delta INTEGER NOT NULL DEFAULT 0,
            risk_level       VARCHAR(16) NOT NULL DEFAULT 'low',
            details          TEXT,
            action_taken     VARCHAR(64),
            admin_notes      TEXT,
            resolved_by      BIGINT,
            resolved_at      TIMESTAMP WITHOUT TIME ZONE,
            created_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fl_user_id    ON fraud_logs (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fl_created_at ON fraud_logs (created_at)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fl_check_type ON fraud_logs (check_type)"
    ))

    # ── fraud_wallet_blacklist ────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS fraud_wallet_blacklist (
            id             SERIAL PRIMARY KEY,
            wallet_address TEXT NOT NULL UNIQUE,
            reason         TEXT,
            added_by       BIGINT,
            created_at     TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS fraud_wallet_blacklist"))
    conn.execute(sa.text("DROP TABLE IF EXISTS fraud_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS fraud_user_risk"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
