"""Enterprise Broadcast Center — database and config additions.

Revision ID: 20260911_enterprise_broadcast_center
Revises:     20260910_performance_cache_manager
Create Date: 2026-09-11

Adds to scheduled_broadcasts:
  - custom_interval_hours  INTEGER  — interval in hours for "custom" recurrence
  - media_group_ids        TEXT     — JSON array of file_ids for future media-group support

Seeds new bot_config keys for the Enterprise Broadcast Center settings panel.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision    = "20260911_enterprise_broadcast_center"
down_revision = "20260910_performance_cache_manager"
branch_labels = None
depends_on    = None


def _col_exists(table: str, col: str) -> bool:
    from sqlalchemy import inspect
    insp = inspect(op.get_bind())
    return col in [c["name"] for c in insp.get_columns(table)]


def _config_key_exists(conn, key: str) -> bool:
    row = conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone()
    return row is not None


def upgrade() -> None:
    # ── New columns on scheduled_broadcasts ──────────────────────────────────
    if not _col_exists("scheduled_broadcasts", "custom_interval_hours"):
        op.add_column(
            "scheduled_broadcasts",
            sa.Column("custom_interval_hours", sa.Integer(), nullable=True),
        )

    if not _col_exists("scheduled_broadcasts", "media_group_ids"):
        op.add_column(
            "scheduled_broadcasts",
            sa.Column("media_group_ids", sa.Text(), nullable=True),
        )

    # ── Seed bot_config keys ─────────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        (
            "broadcast_max_concurrent",
            "3",
            "Enterprise Broadcast Center: maximum number of broadcasts that can run simultaneously. "
            "Additional triggers are queued until a slot is free.",
        ),
        (
            "broadcast_max_queue",
            "10",
            "Enterprise Broadcast Center: maximum number of broadcasts that can sit in the "
            "pending/scheduled queue at any one time. 0 = unlimited.",
        ),
        (
            "broadcast_scheduler_enabled",
            "true",
            "Enterprise Broadcast Center: enable the scheduler subsystem. "
            "When OFF, scheduled broadcasts are not dispatched even if due.",
        ),
        (
            "broadcast_drafts_enabled",
            "true",
            "Enterprise Broadcast Center: allow saving broadcasts as drafts.",
        ),
        (
            "broadcast_preview_enabled",
            "true",
            "Enterprise Broadcast Center: allow previewing a broadcast before sending.",
        ),
        (
            "broadcast_reports_enabled",
            "true",
            "Enterprise Broadcast Center: enable the reports and export section.",
        ),
        (
            "broadcast_test_send_enabled",
            "true",
            "Enterprise Broadcast Center: allow test-sending a broadcast to the admin only "
            "before the real mass-send.",
        ),
        (
            "broadcast_interrupted_stale_minutes",
            "30",
            "Enterprise Broadcast Center: minutes after which a broadcast stuck in 'sending' "
            "is considered interrupted and appears in the Continue Interrupted list.",
        ),
    ]
    for key, value, description in seed:
        if not _config_key_exists(conn, key):
            conn.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value, description) "
                    "VALUES (:k, :v, :d)"
                ),
                {"k": key, "v": value, "d": description},
            )


def downgrade() -> None:
    # Remove added columns
    try:
        op.drop_column("scheduled_broadcasts", "media_group_ids")
    except Exception:
        pass
    try:
        op.drop_column("scheduled_broadcasts", "custom_interval_hours")
    except Exception:
        pass

    conn = op.get_bind()
    keys = [
        "broadcast_max_concurrent",
        "broadcast_max_queue",
        "broadcast_scheduler_enabled",
        "broadcast_drafts_enabled",
        "broadcast_preview_enabled",
        "broadcast_reports_enabled",
        "broadcast_test_send_enabled",
        "broadcast_interrupted_stale_minutes",
    ]
    for k in keys:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": k})
