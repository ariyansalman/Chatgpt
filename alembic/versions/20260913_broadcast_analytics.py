"""Enterprise Broadcast Analytics, Reports & Delivery Management.

Revision ID: 20260913_broadcast_analytics
Revises:     20260912_advanced_broadcast_types
Create Date: 2026-09-13

New table:  broadcast_export_history  — audit log of every generated export
New column: scheduled_broadcasts.is_archived — soft-archive flag
Seeds new bot_config keys for the analytics feature.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision      = "20260913_broadcast_analytics"
down_revision = "20260912_advanced_broadcast_types"
branch_labels = None
depends_on    = None


def _col_exists(table: str, col: str) -> bool:
    from sqlalchemy import inspect
    return col in [c["name"] for c in inspect(op.get_bind()).get_columns(table)]


def _table_exists(table: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(table)


def _key_exists(conn, key: str) -> bool:
    return bool(conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone())


def upgrade() -> None:
    # ── broadcast_export_history ─────────────────────────────────────────────
    if not _table_exists("broadcast_export_history"):
        op.create_table(
            "broadcast_export_history",
            sa.Column("id",              sa.Integer(),   primary_key=True),
            sa.Column("broadcast_id",    sa.Integer(),   sa.ForeignKey(
                "scheduled_broadcasts.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("export_type",     sa.String(16),  nullable=False),   # csv|excel|json|pdf
            sa.Column("report_type",     sa.String(64),  nullable=False),   # delivery|failure|…|period
            sa.Column("period",          sa.String(16),  nullable=True),    # daily|weekly|monthly
            sa.Column("generated_at",    sa.DateTime(),  nullable=False),
            sa.Column("generated_by",    sa.BigInteger(), nullable=True),   # telegram_id of admin
            sa.Column("file_size_bytes", sa.Integer(),   nullable=True),
            sa.Column("filename",        sa.String(255), nullable=True),
        )

    # ── is_archived column on scheduled_broadcasts ───────────────────────────
    if not _col_exists("scheduled_broadcasts", "is_archived"):
        op.add_column(
            "scheduled_broadcasts",
            sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        )

    # ── bot_config keys ───────────────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        ("broadcast_analytics_status",   "enabled",
         "Enterprise Broadcast Analytics feature status: enabled / maintenance / disabled."),
        ("broadcast_analytics_enabled",  "true",
         "Show per-broadcast analytics (real-time stats, status, speed)."),
        ("broadcast_export_enabled",     "true",
         "Allow exporting broadcast reports as CSV, Excel, JSON, or PDF."),
        ("broadcast_retry_manager_enabled", "true",
         "Allow admins to retry failed deliveries or clear the retry queue."),
        ("broadcast_log_retention_days", "90",
         "Days to retain broadcast delivery logs and export history. 0 = forever."),
        ("broadcast_max_history",        "500",
         "Maximum number of broadcast history records shown in the history browser."),
    ]
    for key, value, desc in seed:
        if not _key_exists(conn, key):
            conn.execute(
                sa.text("INSERT INTO bot_config (key, value, description) VALUES (:k, :v, :d)"),
                {"k": key, "v": value, "d": desc},
            )


def downgrade() -> None:
    try:
        op.drop_column("scheduled_broadcasts", "is_archived")
    except Exception:
        pass
    try:
        op.drop_table("broadcast_export_history")
    except Exception:
        pass
    conn = op.get_bind()
    for key in (
        "broadcast_analytics_status", "broadcast_analytics_enabled",
        "broadcast_export_enabled", "broadcast_retry_manager_enabled",
        "broadcast_log_retention_days", "broadcast_max_history",
    ):
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
