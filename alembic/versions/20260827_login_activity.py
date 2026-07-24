"""Login Activity & Device Management System — V32

Adds:
  • login_records   — detailed per-login event rows
  • user_devices    — per-user device fingerprint registry
  • 10 lam_* bot_config keys controlling the feature

Revision ID: 20260827_login_activity
Revises:     20260826_fraud_detection
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260827_login_activity"
down_revision = "20260826_fraud_detection"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple[str, str, str, str]] = [
    # (key, type, default_value, category)
    ("lam_status",              "str",  "enabled", "security"),
    ("lam_track_history",       "bool", "true",    "security"),
    ("lam_track_devices",       "bool", "true",    "security"),
    ("lam_track_ip",            "bool", "true",    "security"),
    ("lam_track_location",      "bool", "true",    "security"),
    ("lam_max_history",         "int",  "50",      "security"),
    ("lam_session_expiry_days", "int",  "30",      "security"),
    ("lam_max_sessions",        "int",  "0",       "security"),   # 0 = unlimited
    ("lam_notify_new_login",    "bool", "true",    "security"),
    ("lam_notify_new_device",   "bool", "true",    "security"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── login_records ─────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS login_records (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            telegram_id     BIGINT   NOT NULL,
            username        VARCHAR(255),
            session_id      INTEGER  REFERENCES user_sessions(id) ON DELETE SET NULL,
            login_method    VARCHAR(64)  NOT NULL DEFAULT 'telegram',
            device_name     VARCHAR(255),
            os_name         VARCHAR(128),
            app_version     VARCHAR(64),
            language_code   VARCHAR(16),
            ip_address      VARCHAR(64),
            country         VARCHAR(128),
            city            VARCHAR(128),
            is_suspicious   BOOLEAN NOT NULL DEFAULT FALSE,
            is_new_device   BOOLEAN NOT NULL DEFAULT FALSE,
            is_new_location BOOLEAN NOT NULL DEFAULT FALSE,
            alert_sent      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_lr_user_id     ON login_records (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_lr_telegram_id ON login_records (telegram_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_lr_created_at  ON login_records (created_at)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_lr_suspicious  ON login_records (is_suspicious) "
        "WHERE is_suspicious = TRUE"
    ))

    # ── user_devices ──────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS user_devices (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_hash   VARCHAR(64) NOT NULL,
            device_name   VARCHAR(255),
            os_name       VARCHAR(128),
            app_version   VARCHAR(64),
            language_code VARCHAR(16),
            first_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            last_seen_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            is_trusted    BOOLEAN NOT NULL DEFAULT FALSE,
            login_count   INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT uq_device_user_hash UNIQUE (user_id, device_hash)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ud_user_id     ON user_devices (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ud_device_hash ON user_devices (device_hash)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ud_last_seen   ON user_devices (last_seen_at)"
    ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS user_devices"))
    conn.execute(sa.text("DROP TABLE IF EXISTS login_records"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
