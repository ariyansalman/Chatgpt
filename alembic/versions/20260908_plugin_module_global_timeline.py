"""V42: Plugin & Module Manager + Global Activity Timeline.

Adds:
  module_configs          — built-in module registry with status management
  global_activity_entries — centralized system-wide activity audit trail

Also seeds bot_config keys for both features.

Revision ID: 20260908_plugin_module_global_timeline
Revises:     20260907_vip_api_manager
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260908_plugin_module_global_timeline"
down_revision = "20260907_vip_api_manager"
branch_labels = None
depends_on = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _table_exists(bind, name: str) -> bool:
    if bind.dialect.name == "postgresql":
        r = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": name}).fetchone()
    else:
        r = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"
        ), {"t": name}).fetchone()
    return r is not None


def _col_exists(bind, table: str, col: str) -> bool:
    if bind.dialect.name == "postgresql":
        r = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ), {"t": table, "c": col}).fetchone()
    else:
        rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(row[1] == col for row in rows)
    return r is not None


# ── upgrade ───────────────────────────────────────────────────────────────────

def upgrade():
    bind = op.get_bind()

    # ── module_configs ────────────────────────────────────────────────────────
    if not _table_exists(bind, "module_configs"):
        op.create_table(
            "module_configs",
            sa.Column("id",              sa.Integer(),     primary_key=True, nullable=False),
            sa.Column("slug",            sa.String(64),    nullable=False),
            sa.Column("name",            sa.String(128),   nullable=False),
            sa.Column("version",         sa.String(32),    nullable=True),
            sa.Column("description",     sa.Text(),        nullable=True),
            sa.Column("author",          sa.String(64),    nullable=True),
            sa.Column("dependencies",    sa.Text(),        nullable=True),   # JSON array of slugs
            sa.Column("category",        sa.String(64),    nullable=True),
            sa.Column("is_core",         sa.Boolean(),     nullable=False, server_default="false"),
            # status: enabled | maintenance | disabled
            sa.Column("status",          sa.String(16),    nullable=False, server_default="enabled"),
            sa.Column("last_updated_at", sa.DateTime(),    nullable=True),
            sa.Column("created_at",      sa.DateTime(),    nullable=True, server_default=sa.func.now()),
        )
        op.create_index("ix_module_configs_slug",     "module_configs", ["slug"],     unique=True)
        op.create_index("ix_module_configs_status",   "module_configs", ["status"])
        op.create_index("ix_module_configs_category", "module_configs", ["category"])
        logger.info("plugin_module_global_timeline: created module_configs")
    else:
        logger.info("plugin_module_global_timeline: module_configs already exists — skipping")

    # ── global_activity_entries ───────────────────────────────────────────────
    if not _table_exists(bind, "global_activity_entries"):
        op.create_table(
            "global_activity_entries",
            sa.Column("id",                 sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("user_id",            sa.Integer(),    nullable=True),        # FK to users.id (nullable — system events)
            sa.Column("username",           sa.String(64),   nullable=True),
            sa.Column("admin_telegram_id",  sa.BigInteger(), nullable=True),
            sa.Column("action",             sa.String(64),   nullable=False),
            sa.Column("category",           sa.String(32),   nullable=False),
            sa.Column("description",        sa.Text(),       nullable=True),
            sa.Column("ip_address",         sa.String(45),   nullable=True),
            sa.Column("status",             sa.String(16),   nullable=False, server_default="success"),
            sa.Column("ref_type",           sa.String(32),   nullable=True),
            sa.Column("ref_id",             sa.String(64),   nullable=True),
            sa.Column("extra",              sa.Text(),       nullable=True),        # JSON blob
            sa.Column("created_at",         sa.DateTime(),   nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_gae_action",     "global_activity_entries", ["action"])
        op.create_index("ix_gae_category",   "global_activity_entries", ["category"])
        op.create_index("ix_gae_user_id",    "global_activity_entries", ["user_id"])
        op.create_index("ix_gae_admin_tgid", "global_activity_entries", ["admin_telegram_id"])
        op.create_index("ix_gae_created_at", "global_activity_entries", ["created_at"])
        op.create_index("ix_gae_status",     "global_activity_entries", ["status"])
        logger.info("plugin_module_global_timeline: created global_activity_entries")
    else:
        logger.info("plugin_module_global_timeline: global_activity_entries already exists — skipping")

    # ── bot_config keys ───────────────────────────────────────────────────────
    new_keys = [
        # Plugin & Module Manager
        ("pmm_status",              "enabled",  "plugin_module_manager",
         "🧩 Module Manager Status",         "enabled | maintenance | disabled"),
        ("pmm_dependency_check",    "true",     "plugin_module_manager",
         "🧩 Dependency Check",              "Check dependencies before enabling a module."),
        ("pmm_safe_mode",           "true",     "plugin_module_manager",
         "🧩 Safe Mode",                     "Prevent disabling modules that others depend on."),
        ("pmm_module_logs",         "true",     "plugin_module_manager",
         "🧩 Module Logs",                   "Log module status changes to the activity timeline."),
        ("pmm_auto_health_check",   "false",    "plugin_module_manager",
         "🧩 Auto Health Check",             "Periodically check module dependency health."),
        # Global Activity Timeline
        ("gat_status",              "enabled",  "global_activity_timeline",
         "📜 Timeline Status",               "enabled | maintenance | disabled"),
        ("gat_enabled",             "true",     "global_activity_timeline",
         "📜 Timeline Enabled",              "Record events to the global activity timeline."),
        ("gat_auto_archive",        "false",    "global_activity_timeline",
         "📜 Auto Archive",                  "Automatically archive old timeline entries."),
        ("gat_retention_days",      "90",       "global_activity_timeline",
         "📜 Retention (days)",              "Days to keep activity entries (0 = forever)."),
    ]
    for key, default_val, category, label, description in new_keys:
        try:
            bind.execute(sa.text(
                "INSERT INTO bot_config (key, value, value_type, category, label, description) "
                "VALUES (:k, :v, 'str', :cat, :lab, :desc) "
                "ON CONFLICT (key) DO NOTHING"
            ), {"k": key, "v": default_val, "cat": category, "lab": label, "desc": description})
        except Exception as exc:
            logger.warning("plugin_module_global_timeline: bot_config insert %s skipped: %s", key, exc)

    logger.info("plugin_module_global_timeline: migration complete")


def downgrade():
    bind = op.get_bind()
    for tbl in ("global_activity_entries", "module_configs"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
    logger.info("plugin_module_global_timeline: downgrade complete")
