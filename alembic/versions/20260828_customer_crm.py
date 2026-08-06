"""Customer Notes & CRM System — V33

Adds:
  • customer_profiles       — per-user CRM profile (priority, status)
  • customer_notes          — admin-private notes on any user
  • customer_tags           — global tag definitions
  • customer_tag_assignments — many-to-many user↔tag
  • customer_reminders      — follow-up reminders per user
  • 7 crm_* bot_config keys

Revision ID: 20260828_customer_crm
Revises:     20260827_login_activity
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260828_customer_crm"
down_revision = "20260827_login_activity"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # (key, type, default_value, category)
    ("crm_status",                "str",  "enabled", "crm"),
    ("crm_allow_multiple_notes",  "bool", "true",    "crm"),
    ("crm_allow_tags",            "bool", "true",    "crm"),
    ("crm_allow_priority",        "bool", "true",    "crm"),
    ("crm_allow_reminders",       "bool", "true",    "crm"),
    ("crm_allow_internal_status", "bool", "true",    "crm"),
    ("crm_max_notes",             "int",  "0",       "crm"),   # 0 = unlimited
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── customer_profiles ─────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_profiles (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            priority       VARCHAR(16)  NOT NULL DEFAULT 'low',
            crm_status     VARCHAR(64)  NOT NULL DEFAULT 'new_customer',
            custom_status  VARCHAR(128),
            notes_count    INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_by     BIGINT
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cp_user_id  ON customer_profiles (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cp_priority ON customer_profiles (priority)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cp_status   ON customer_profiles (crm_status)"
    ))

    # ── customer_notes ────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_notes (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            admin_id    BIGINT  NOT NULL,
            admin_name  VARCHAR(255),
            content     TEXT    NOT NULL,
            is_pinned   BOOLEAN NOT NULL DEFAULT FALSE,
            is_archived BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cn_user_id    ON customer_notes (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cn_admin_id   ON customer_notes (admin_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cn_created_at ON customer_notes (created_at)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cn_pinned     ON customer_notes (is_pinned) "
        "WHERE is_pinned = TRUE"
    ))

    # ── customer_tags ─────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_tags (
            id         SERIAL PRIMARY KEY,
            name       VARCHAR(64) NOT NULL UNIQUE,
            color      VARCHAR(16),
            created_by BIGINT,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ── customer_tag_assignments ──────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_tag_assignments (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tag_id      INTEGER NOT NULL REFERENCES customer_tags(id) ON DELETE CASCADE,
            assigned_by BIGINT,
            assigned_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_tag_assignment UNIQUE (user_id, tag_id)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cta_user_id ON customer_tag_assignments (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cta_tag_id  ON customer_tag_assignments (tag_id)"
    ))

    # ── customer_reminders ────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_reminders (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            admin_id     BIGINT  NOT NULL,
            reason       TEXT    NOT NULL,
            remind_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            is_completed BOOLEAN NOT NULL DEFAULT FALSE,
            completed_at TIMESTAMP WITHOUT TIME ZONE,
            created_at   TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cr_user_id    ON customer_reminders (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cr_admin_id   ON customer_reminders (admin_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cr_remind_at  ON customer_reminders (remind_at)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cr_pending    ON customer_reminders (is_completed) "
        "WHERE is_completed = FALSE"
    ))

    # ── Seed default tags ─────────────────────────────────────────────────────
    _default_tags = [
        ("VIP",                 "#FFD700"),
        ("Wholesale",           "#1E90FF"),
        ("Trusted",             "#32CD32"),
        ("Suspicious",          "#FF4500"),
        ("High Spender",        "#9400D3"),
        ("Frequent Buyer",      "#00CED1"),
        ("Pending Verification","#FFA500"),
        ("Refund Risk",         "#DC143C"),
        ("Support Priority",    "#FF69B4"),
    ]
    for name, color in _default_tags:
        conn.execute(sa.text("""
            INSERT INTO customer_tags (name, color, created_by)
            VALUES (:name, :color, NULL)
            ON CONFLICT (name) DO NOTHING
        """), {"name": name, "color": color})

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_reminders"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_tag_assignments"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_tags"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_notes"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_profiles"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
