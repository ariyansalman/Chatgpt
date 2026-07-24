"""Global Settings Backup & Restore + System Diagnostics Center — V34

Adds:
  • settings_backup_records  — JSON settings backup metadata
  • diagnostics_records       — diagnostics scan history
  • New bot_config keys for both features

Revision ID: 20260829_backup_diagnostics
Revises:     20260828_customer_crm
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260829_backup_diagnostics"
down_revision = "20260828_customer_crm"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # ── Backup Manager ────────────────────────────────────────────────────────
    ("backup_manager_status",         "str",  "enabled",  "backups",
     "Backup Manager Status",
     "enabled = fully operational; maintenance = read-only; disabled = off."),
    ("backup_auto_settings_enabled",  "bool", "false",    "backups",
     "Auto Settings Backup Enabled",
     "When ON, settings are backed up automatically on the configured interval."),
    ("backup_settings_interval_hours","int",  "24",       "backups",
     "Settings Backup Interval (hours)",
     "How often the automatic settings backup job runs."),
    ("backup_max_count",              "int",  "30",       "backups",
     "Maximum Backup Count",
     "Total number of settings backups to keep. Oldest are pruned first."),
    ("backup_restore_confirm",        "bool", "true",     "backups",
     "Restore Confirmation Required",
     "When ON, admin must confirm before any restore operation is applied."),
    ("backup_compression",            "bool", "true",     "backups",
     "Backup Compression",
     "When ON, settings backup files are gzip-compressed (smaller size)."),
    # ── Diagnostics ───────────────────────────────────────────────────────────
    ("diagnostics_status",            "str",  "enabled",  "diagnostics",
     "Diagnostics Center Status",
     "enabled = fully operational; maintenance = read-only; disabled = off."),
    ("diagnostics_auto_scan",         "bool", "false",    "diagnostics",
     "Auto Diagnostics Scan Enabled",
     "When ON, a diagnostics scan runs automatically at the configured interval."),
    ("diagnostics_scan_interval_hours","int", "6",        "diagnostics",
     "Diagnostics Scan Interval (hours)",
     "How often the automatic diagnostics scan runs."),
    ("diagnostics_admin_alerts",      "bool", "true",     "diagnostics",
     "Diagnostics Admin Alerts",
     "When ON, admin receives a message when Critical issues are detected."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── settings_backup_records ───────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS settings_backup_records (
            id               SERIAL PRIMARY KEY,
            backup_type      VARCHAR(32)  NOT NULL DEFAULT 'settings',
            filename         VARCHAR(255) NOT NULL,
            size_bytes       BIGINT,
            status           VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
            checksum         VARCHAR(64),
            note             VARCHAR(255),
            created_by       BIGINT,
            created_at       TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at     TIMESTAMP WITHOUT TIME ZONE,
            restore_count    INTEGER NOT NULL DEFAULT 0,
            last_restored_at TIMESTAMP WITHOUT TIME ZONE,
            last_restored_by BIGINT,
            error_summary    VARCHAR(500)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_sbr_status     ON settings_backup_records (status)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_sbr_created_at ON settings_backup_records (created_at)"
    ))

    # ── diagnostics_records ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS diagnostics_records (
            id             SERIAL PRIMARY KEY,
            scan_type      VARCHAR(16)  NOT NULL DEFAULT 'full',
            triggered_by   VARCHAR(16)  NOT NULL DEFAULT 'manual',
            admin_id       BIGINT,
            started_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at   TIMESTAMP WITHOUT TIME ZONE,
            status         VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
            overall_health VARCHAR(16),
            summary        TEXT,
            total_checks   INTEGER NOT NULL DEFAULT 0,
            healthy_count  INTEGER NOT NULL DEFAULT 0,
            warning_count  INTEGER NOT NULL DEFAULT 0,
            critical_count INTEGER NOT NULL DEFAULT 0
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_dr_status     ON diagnostics_records (status)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_dr_started_at ON diagnostics_records (started_at)"
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
    conn.execute(sa.text("DROP TABLE IF EXISTS diagnostics_records"))
    conn.execute(sa.text("DROP TABLE IF EXISTS settings_backup_records"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
