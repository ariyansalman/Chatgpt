"""Admin Dashboard Widget System — V30

Adds:
  • admin_dashboard_layouts  table  (per-admin layout JSON storage)
  • 6 new bot_config keys prefixed adw_*

Revision ID: 20260825_admin_dashboard_widgets
Revises:     20260824_withdrawal_approval
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260825_admin_dashboard_widgets"
down_revision = "20260824_withdrawal_approval"
branch_labels = None
depends_on = None


# ─── New bot_config rows ──────────────────────────────────────────────────────

_NEW_CONFIG_KEYS: list[tuple[str, str, str, str]] = [
    # (key, type, value, category)
    ("adw_status",           "str",  "enabled", "admin"),
    ("adw_auto_refresh",     "bool", "false",   "admin"),
    ("adw_refresh_interval", "int",  "60",      "admin"),
    ("adw_charts_enabled",   "bool", "true",    "admin"),
    ("adw_quick_actions",    "bool", "true",    "admin"),
    ("adw_statistics",       "bool", "true",    "admin"),
]


def upgrade() -> None:
    # ── Create admin_dashboard_layouts ────────────────────────────────────────
    conn = op.get_bind()
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS admin_dashboard_layouts (
            id          SERIAL PRIMARY KEY,
            admin_tg_id BIGINT NOT NULL UNIQUE,
            layout_json TEXT   NOT NULL DEFAULT '{}',
            created_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
            updated_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_adl_admin_tg_id "
        "ON admin_dashboard_layouts (admin_tg_id)"
    ))

    # ── Seed bot_config keys (idempotent) ─────────────────────────────────────
    for key, typ, val, cat in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS admin_dashboard_layouts"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
