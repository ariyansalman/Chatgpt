"""V44 — Performance & Cache Manager

Revision ID: 20260910_performance_cache_manager
Revises:     20260909_data_export_global_search
Create Date: 2026-09-10

Creates:
  - performance_snapshots   (periodic system metric readings)
  - optimization_logs       (history of every optimization action)

Seeds bot_config keys for the Performance & Cache Manager.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260910_performance_cache_manager"
down_revision = "20260909_data_export_global_search"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(name)


def _config_key_exists(conn, key: str) -> bool:
    row = conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone()
    return row is not None


def upgrade() -> None:
    # ── performance_snapshots ────────────────────────────────────────────────
    if not _table_exists("performance_snapshots"):
        op.create_table(
            "performance_snapshots",
            sa.Column("id",           sa.Integer(),   primary_key=True),
            sa.Column("cpu_pct",      sa.Float(),     nullable=True),
            sa.Column("mem_pct",      sa.Float(),     nullable=True),
            sa.Column("disk_pct",     sa.Float(),     nullable=True),
            sa.Column("db_ping_ms",   sa.Float(),     nullable=True),
            sa.Column("db_size_mb",   sa.Float(),     nullable=True),
            sa.Column("db_conn",      sa.Integer(),   nullable=True),
            sa.Column("uptime_s",     sa.Integer(),   nullable=True),
            sa.Column("health_score", sa.Integer(),   nullable=True),
            sa.Column("health_label", sa.String(16),  nullable=True),
            sa.Column("extra",        sa.Text(),      nullable=True),   # JSON
            sa.Column("created_at",   sa.DateTime(),  nullable=False, index=True),
        )

    # ── optimization_logs ────────────────────────────────────────────────────
    if not _table_exists("optimization_logs"):
        op.create_table(
            "optimization_logs",
            sa.Column("id",            sa.Integer(),    primary_key=True),
            sa.Column("op_type",       sa.String(32),   nullable=False, index=True),
            sa.Column("target",        sa.String(64),   nullable=True),
            sa.Column("result",        sa.String(16),   nullable=False),  # success | failed
            sa.Column("details",       sa.String(500),  nullable=True),
            sa.Column("duration_ms",   sa.Integer(),    nullable=True),
            sa.Column("rows_affected", sa.Integer(),    nullable=True),
            sa.Column("created_at",    sa.DateTime(),   nullable=False, index=True),
        )

    # ── Seed bot_config keys ─────────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        ("pcm_status",                 "enabled",  "PCM: feature status (enabled/maintenance/disabled)"),
        ("pcm_auto_cache_cleanup",     "true",     "PCM: run cache cleanup in auto-maintenance"),
        ("pcm_auto_log_cleanup",       "true",     "PCM: run log cleanup in auto-maintenance"),
        ("pcm_auto_storage_cleanup",   "true",     "PCM: run storage cleanup in auto-maintenance"),
        ("pcm_auto_job_cleanup",       "true",     "PCM: run job cleanup in auto-maintenance"),
        ("pcm_auto_snapshot",          "true",     "PCM: save performance snapshot in auto-maintenance"),
        ("pcm_alerts_enabled",         "true",     "PCM: send performance alerts to admins"),
        ("pcm_snapshot_interval_min",  "15",       "PCM: minutes between auto-snapshots"),
        ("pcm_log_retention_days",     "90",       "PCM: days to retain log rows before auto-cleanup"),
        ("pcm_maintenance_interval_h", "24",       "PCM: hours between auto-maintenance runs"),
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
    if _table_exists("optimization_logs"):
        op.drop_table("optimization_logs")
    if _table_exists("performance_snapshots"):
        op.drop_table("performance_snapshots")

    conn = op.get_bind()
    keys = [
        "pcm_status", "pcm_auto_cache_cleanup", "pcm_auto_log_cleanup",
        "pcm_auto_storage_cleanup", "pcm_auto_job_cleanup", "pcm_auto_snapshot",
        "pcm_alerts_enabled", "pcm_snapshot_interval_min",
        "pcm_log_retention_days", "pcm_maintenance_interval_h",
    ]
    for k in keys:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": k})
