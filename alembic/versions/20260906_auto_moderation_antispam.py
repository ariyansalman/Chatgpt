"""Auto Moderation & Anti-Spam — V40

Adds:
  • user_moderation_status    — current ban/mute/cooldown state per user
  • spam_logs                 — raw spam event log
  • moderation_action_logs    — admin action audit trail
  • blacklist_entries         — word/user/referral/wallet blacklist
  • whitelist_entries         — trusted/VIP/admin whitelist

Revision ID: 20260906_auto_moderation_antispam
Revises:     20260905_sales_forecast_insights
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260906_auto_moderation_antispam"
down_revision = "20260905_sales_forecast_insights"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS = [
    ("antispam_status",             "str",   "enabled", "antispam",
     "🛡 Anti-Spam Status",
     "enabled = active; maintenance = log-only; disabled = off."),
    ("antispam_max_cmds_per_min",   "int",   10,        "antispam",
     "🛡 Max Commands per Minute",
     "How many /commands a user may send per minute before triggering rate-limit."),
    ("antispam_max_clicks_per_min", "int",   20,        "antispam",
     "🛡 Max Button Clicks per Minute",
     "How many inline button presses a user may make per minute."),
    ("antispam_max_msgs_per_min",   "int",   15,        "antispam",
     "🛡 Max Messages per Minute",
     "How many text messages a user may send per minute."),
    ("antispam_flood_window_secs",  "int",   10,        "antispam",
     "🛡 Flood Window (seconds)",
     "Short window in seconds for flood detection (burst threshold)."),
    ("antispam_flood_threshold",    "int",   8,         "antispam",
     "🛡 Flood Threshold (events)",
     "How many events in the flood window constitute a flood."),
    ("antispam_cooldown_secs",      "int",   60,        "antispam",
     "🛡 Cooldown Duration (seconds)",
     "How long a user is put on cooldown after reaching max warnings."),
    ("antispam_max_warnings",       "int",   3,         "antispam",
     "🛡 Max Warnings Before Action",
     "Number of violations before auto-mute or cooldown is applied."),
    ("antispam_auto_mute",          "bool",  True,      "antispam",
     "🛡 Auto-Mute on Max Warnings",
     "Automatically mute users who exceed the warning threshold."),
    ("antispam_auto_ban",           "bool",  False,     "antispam",
     "🛡 Auto-Ban on Repeated Mutes",
     "Automatically temp-ban users who are muted multiple times."),
    ("antispam_mute_secs",          "int",   300,       "antispam",
     "🛡 Mute Duration (seconds)",
     "Duration of an automatic mute. Default: 300 (5 minutes)."),
    ("antispam_ban_secs",           "int",   86400,     "antispam",
     "🛡 Temp-Ban Duration (seconds)",
     "Duration of an automatic temporary ban. Default: 86400 (24h)."),
    ("antispam_captcha_on_new",     "bool",  False,     "antispam",
     "🛡 Captcha for New Users",
     "Require captcha verification for brand-new users on first interaction."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── user_moderation_status ────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS user_moderation_status (
            id                SERIAL PRIMARY KEY,
            telegram_id       BIGINT       NOT NULL,
            username          VARCHAR(255),
            status            VARCHAR(16)  NOT NULL DEFAULT 'active',
            is_muted          BOOLEAN      NOT NULL DEFAULT FALSE,
            mute_expires_at   TIMESTAMP WITHOUT TIME ZONE,
            is_banned         BOOLEAN      NOT NULL DEFAULT FALSE,
            ban_type          VARCHAR(16),
            ban_expires_at    TIMESTAMP WITHOUT TIME ZONE,
            is_in_cooldown    BOOLEAN      NOT NULL DEFAULT FALSE,
            cooldown_expires  TIMESTAMP WITHOUT TIME ZONE,
            needs_captcha     BOOLEAN      NOT NULL DEFAULT FALSE,
            warning_count     INTEGER      NOT NULL DEFAULT 0,
            total_violations  INTEGER      NOT NULL DEFAULT 0,
            last_violation_at TIMESTAMP WITHOUT TIME ZONE,
            under_review      BOOLEAN      NOT NULL DEFAULT FALSE,
            notes             TEXT,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_ums_tgid UNIQUE (telegram_id)
        )
    """))
    for col, idx in [
        ("telegram_id", "ix_ums_tgid"),
        ("status",      "ix_ums_status"),
        ("is_muted",    "ix_ums_muted"),
        ("is_banned",   "ix_ums_banned"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON user_moderation_status ({col})"
        ))

    # ── spam_logs ─────────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS spam_logs (
            id              SERIAL PRIMARY KEY,
            telegram_id     BIGINT       NOT NULL,
            username        VARCHAR(255),
            violation_type  VARCHAR(32)  NOT NULL,
            action_taken    VARCHAR(32)  NOT NULL,
            detail          TEXT,
            raw_data        TEXT,
            created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("telegram_id",    "ix_sl_tgid"),
        ("violation_type", "ix_sl_vtype"),
        ("created_at",     "ix_sl_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON spam_logs ({col})"
        ))

    # ── moderation_action_logs ────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS moderation_action_logs (
            id              SERIAL PRIMARY KEY,
            target_tg_id    BIGINT       NOT NULL,
            action_type     VARCHAR(32)  NOT NULL,
            duration_secs   INTEGER,
            expires_at      TIMESTAMP WITHOUT TIME ZONE,
            reason          VARCHAR(255),
            actor_type      VARCHAR(16)  NOT NULL DEFAULT 'system',
            actor_id        BIGINT,
            notes           TEXT,
            created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("target_tg_id", "ix_mal_tgid"),
        ("created_at",   "ix_mal_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON moderation_action_logs ({col})"
        ))

    # ── blacklist_entries ─────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS blacklist_entries (
            id          SERIAL PRIMARY KEY,
            entry_type  VARCHAR(16)  NOT NULL,
            value       VARCHAR(512) NOT NULL,
            reason      VARCHAR(255),
            added_by    BIGINT,
            is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_bl_type_value UNIQUE (entry_type, value)
        )
    """))
    for col, idx in [
        ("entry_type", "ix_bl_type"),
        ("is_active",  "ix_bl_active"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON blacklist_entries ({col})"
        ))

    # ── whitelist_entries ─────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS whitelist_entries (
            id          SERIAL PRIMARY KEY,
            telegram_id BIGINT       NOT NULL,
            entry_type  VARCHAR(16)  NOT NULL,
            reason      VARCHAR(255),
            added_by    BIGINT,
            is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_wl_type_tgid UNIQUE (entry_type, telegram_id)
        )
    """))
    for col, idx in [
        ("telegram_id", "ix_wl_tgid"),
        ("entry_type",  "ix_wl_type"),
        ("is_active",   "ix_wl_active"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON whitelist_entries ({col})"
        ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ,
               "value": str(val).lower() if isinstance(val, bool) else str(val),
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS whitelist_entries"))
    conn.execute(sa.text("DROP TABLE IF EXISTS blacklist_entries"))
    conn.execute(sa.text("DROP TABLE IF EXISTS moderation_action_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS spam_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS user_moderation_status"))
    for row in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": row[0]})
