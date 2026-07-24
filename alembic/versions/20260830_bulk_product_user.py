"""Bulk Product Import/Export & Bulk User Management — V35

Adds:
  • bulk_import_records   — tracks product bulk import jobs
  • bulk_export_records   — tracks product/user export jobs
  • bulk_action_records   — tracks bulk actions on products/users
  • New bot_config keys for both features

Revision ID: 20260830_bulk_product_user
Revises:     20260829_backup_diagnostics
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260830_bulk_product_user"
down_revision = "20260829_backup_diagnostics"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # ── Bulk Product Manager ──────────────────────────────────────────────────
    ("bulk_product_manager_status",      "str",  "enabled",  "bulk_products",
     "Bulk Product Manager Status",
     "enabled = fully operational; maintenance = read-only; disabled = off."),
    ("bulk_product_import_max_rows",     "int",  "1000",     "bulk_products",
     "Max Import Rows",
     "Maximum number of product rows allowed in a single import file."),
    ("bulk_product_export_max_rows",     "int",  "5000",     "bulk_products",
     "Max Export Rows",
     "Maximum number of products that can be exported in a single operation."),
    ("bulk_product_delete_confirm",      "bool", "true",     "bulk_products",
     "Bulk Delete Confirmation Required",
     "When ON, admin must confirm before bulk deleting products."),
    ("bulk_product_action_log_enabled",  "bool", "true",     "bulk_products",
     "Bulk Product Action Logging",
     "When ON, all bulk product actions are recorded in bulk_action_records."),
    # ── Bulk User Manager ─────────────────────────────────────────────────────
    ("bulk_user_manager_status",         "str",  "enabled",  "bulk_users",
     "Bulk User Manager Status",
     "enabled = fully operational; maintenance = read-only; disabled = off."),
    ("bulk_user_export_max_rows",        "int",  "10000",    "bulk_users",
     "Max User Export Rows",
     "Maximum number of users that can be exported in one operation."),
    ("bulk_user_delete_confirm",         "bool", "true",     "bulk_users",
     "Bulk User Delete Confirmation Required",
     "When ON, admin must confirm before bulk deleting inactive users."),
    ("bulk_user_broadcast_limit",        "int",  "500",      "bulk_users",
     "Bulk Broadcast Recipient Limit",
     "Maximum number of users who can receive a bulk broadcast message."),
    ("bulk_user_action_log_enabled",     "bool", "true",     "bulk_users",
     "Bulk User Action Logging",
     "When ON, all bulk user actions are recorded in bulk_action_records."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── bulk_import_records ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS bulk_import_records (
            id            SERIAL PRIMARY KEY,
            admin_id      BIGINT      NOT NULL,
            file_format   VARCHAR(8)  NOT NULL,
            status        VARCHAR(16) NOT NULL DEFAULT 'RUNNING',
            total_rows    INTEGER     NOT NULL DEFAULT 0,
            imported      INTEGER     NOT NULL DEFAULT 0,
            failed        INTEGER     NOT NULL DEFAULT 0,
            duplicates    INTEGER     NOT NULL DEFAULT 0,
            report        TEXT,
            started_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at  TIMESTAMP WITHOUT TIME ZONE,
            error_summary VARCHAR(500)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bir_admin_id   ON bulk_import_records (admin_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bir_status     ON bulk_import_records (status)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bir_started_at ON bulk_import_records (started_at)"
    ))

    # ── bulk_export_records ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS bulk_export_records (
            id            SERIAL PRIMARY KEY,
            admin_id      BIGINT      NOT NULL,
            export_type   VARCHAR(16) NOT NULL,
            file_format   VARCHAR(8)  NOT NULL,
            scope         VARCHAR(32) NOT NULL,
            status        VARCHAR(16) NOT NULL DEFAULT 'RUNNING',
            row_count     INTEGER     NOT NULL DEFAULT 0,
            size_bytes    BIGINT,
            started_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at  TIMESTAMP WITHOUT TIME ZONE,
            error_summary VARCHAR(500)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ber_admin_id   ON bulk_export_records (admin_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ber_status     ON bulk_export_records (status)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ber_started_at ON bulk_export_records (started_at)"
    ))

    # ── bulk_action_records ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS bulk_action_records (
            id            SERIAL PRIMARY KEY,
            admin_id      BIGINT      NOT NULL,
            action_type   VARCHAR(32) NOT NULL,
            entity_type   VARCHAR(16) NOT NULL,
            scope         VARCHAR(32),
            target_count  INTEGER     NOT NULL DEFAULT 0,
            success_count INTEGER     NOT NULL DEFAULT 0,
            failed_count  INTEGER     NOT NULL DEFAULT 0,
            details       TEXT,
            status        VARCHAR(16) NOT NULL DEFAULT 'COMPLETED',
            created_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at  TIMESTAMP WITHOUT TIME ZONE
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bar_admin_id    ON bulk_action_records (admin_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bar_action_type ON bulk_action_records (action_type)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bar_created_at  ON bulk_action_records (created_at)"
    ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS bulk_action_records"))
    conn.execute(sa.text("DROP TABLE IF EXISTS bulk_export_records"))
    conn.execute(sa.text("DROP TABLE IF EXISTS bulk_import_records"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
