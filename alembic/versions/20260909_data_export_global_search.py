"""V43 — Data Export Center + Global Search Engine

Revision ID: 20260909_data_export_global_search
Revises:     20260908_plugin_module_global_timeline
Create Date: 2026-09-09

Creates:
  - export_jobs            (Data Export Center queue + history)
  - search_records         (Global Search history + saved searches)

Seeds bot_config keys for both features.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# ─── Alembic metadata ────────────────────────────────────────────────────────
revision = "20260909_data_export_global_search"
down_revision = "20260908_plugin_module_global_timeline"
branch_labels = None
depends_on = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(name)


def _config_key_exists(conn, key: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone()
    return result is not None


# ─── Upgrade ─────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # ── export_jobs ──────────────────────────────────────────────────────────
    if not _table_exists("export_jobs"):
        op.create_table(
            "export_jobs",
            sa.Column("id",                 sa.Integer(),    primary_key=True),
            sa.Column("admin_telegram_id",  sa.BigInteger(), nullable=False, index=True),
            sa.Column("export_type",        sa.String(32),   nullable=False),
            sa.Column("format",             sa.String(8),    nullable=False),
            # pending | running | done | failed | scheduled
            sa.Column("status",             sa.String(16),   nullable=False,
                       server_default="pending", index=True),
            sa.Column("filters",            sa.Text(),       nullable=True),
            sa.Column("file_path",          sa.String(512),  nullable=True),
            sa.Column("file_size",          sa.Integer(),    nullable=True),
            sa.Column("row_count",          sa.Integer(),    nullable=True),
            sa.Column("error_message",      sa.Text(),       nullable=True),
            sa.Column("label",              sa.String(128),  nullable=True),
            sa.Column("scheduled_at",       sa.DateTime(),   nullable=True),
            sa.Column("started_at",         sa.DateTime(),   nullable=True),
            sa.Column("completed_at",       sa.DateTime(),   nullable=True),
            sa.Column("created_at",         sa.DateTime(),   nullable=False, index=True),
        )

    # ── search_records ───────────────────────────────────────────────────────
    if not _table_exists("search_records"):
        op.create_table(
            "search_records",
            sa.Column("id",                 sa.Integer(),    primary_key=True),
            sa.Column("admin_telegram_id",  sa.BigInteger(), nullable=False, index=True),
            sa.Column("query",              sa.String(256),  nullable=False, index=True),
            sa.Column("modules",            sa.Text(),       nullable=True),
            sa.Column("result_count",       sa.Integer(),    nullable=True, server_default="0"),
            sa.Column("search_time_ms",     sa.Integer(),    nullable=True),
            sa.Column("is_saved",           sa.Boolean(),    nullable=False,
                       server_default=sa.false()),
            sa.Column("label",              sa.String(128),  nullable=True),
            sa.Column("created_at",         sa.DateTime(),   nullable=False, index=True),
        )

    # ── Seed bot_config keys ─────────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        # Data Export Center
        ("dec_status",              "enabled",  "DEC: feature status (enabled/maintenance/disabled)"),
        ("dec_auto_cleanup_days",   "30",       "DEC: auto-delete export files after N days"),
        ("dec_max_file_mb",         "50",       "DEC: max allowed export file size in MB"),
        ("dec_allow_pdf",           "true",     "DEC: allow PDF format export"),
        ("dec_allow_zip",           "true",     "DEC: allow ZIP archive export"),
        # Global Search Engine
        ("gse_status",              "enabled",  "GSE: feature status (enabled/maintenance/disabled)"),
        ("gse_max_results",         "50",       "GSE: maximum results returned per search"),
        ("gse_fuzzy",               "true",     "GSE: enable fuzzy/partial matching"),
        ("gse_keep_history",        "true",     "GSE: save search history"),
        ("gse_max_history",         "100",      "GSE: max history records per admin"),
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


# ─── Downgrade ────────────────────────────────────────────────────────────────

def downgrade() -> None:
    if _table_exists("search_records"):
        op.drop_table("search_records")
    if _table_exists("export_jobs"):
        op.drop_table("export_jobs")

    conn = op.get_bind()
    keys = [
        "dec_status", "dec_auto_cleanup_days", "dec_max_file_mb",
        "dec_allow_pdf", "dec_allow_zip",
        "gse_status", "gse_max_results", "gse_fuzzy",
        "gse_keep_history", "gse_max_history",
    ]
    for k in keys:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": k})
