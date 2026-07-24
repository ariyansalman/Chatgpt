"""V26 — Scheduled Broadcast V2: extended columns, broadcast_logs, broadcast_retry_queue,
and new BotConfig settings.

Revision ID: 20260821_scheduled_broadcast_v2
Revises: 20260820_product_faq

Changes
-------
• Adds new columns to ``scheduled_broadcasts`` (idempotent — uses IF NOT EXISTS).
• Creates ``broadcast_logs`` table.
• Creates ``broadcast_retry_queue`` table.
• Seeds new BotConfig keys for broadcast settings.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260821_scheduled_broadcast_v2"
down_revision = "20260820_product_faq"
branch_labels = None
depends_on    = None


def _col_exists(bind, table: str, col: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": col},
    ).fetchone()
    return bool(row)


def _table_exists(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).fetchone()
    return bool(row)


_NEW_COLS = [
    # (column_name, SQLAlchemy type, server_default or None)
    ("timezone",             sa.String(64),  "'UTC'"),
    ("parse_mode",           sa.String(16),  "'HTML'"),
    ("disable_notification", sa.Boolean,     "FALSE"),
    ("started_at",           sa.DateTime,    None),
    ("finished_at",          sa.DateTime,    None),
    ("skipped_count",        sa.Integer,     "0"),
    ("total_recipients",     sa.Integer,     "0"),
    ("error_log",            sa.Text,        None),
    ("retry_count",          sa.Integer,     "0"),
    ("max_retries",          sa.Integer,     "3"),
    ("is_paused",            sa.Boolean,     "FALSE"),
    ("next_run_at",          sa.DateTime,    None),
    ("target_user_ids",      sa.Text,        None),    # JSON list for specific IDs
    ("target_language",      sa.String(8),   None),    # language code
    ("delivered_count",      sa.Integer,     "0"),
    ("blocked_count",        sa.Integer,     "0"),
]

_BOT_CONFIG_KEYS = [
    ("scheduled_broadcast_status",       "str",   "enabled",      "broadcast"),
    ("broadcast_max_speed",              "int",   "20",           "broadcast"),
    ("broadcast_delay_ms",               "int",   "50",           "broadcast"),
    ("broadcast_retry_failed",           "bool",  "True",         "broadcast"),
    ("broadcast_retry_count",            "int",   "3",            "broadcast"),
    ("broadcast_silent",                 "bool",  "False",        "broadcast"),
    ("broadcast_disable_notifications",  "bool",  "False",        "broadcast"),
]


def upgrade():
    bind = op.get_bind()

    # ── 1. Extend scheduled_broadcasts ───────────────────────────────────────
    for col_name, col_type, server_default in _NEW_COLS:
        if not _col_exists(bind, "scheduled_broadcasts", col_name):
            col = sa.Column(col_name, col_type, nullable=True,
                            server_default=server_default)
            op.add_column("scheduled_broadcasts", col)
            logger.info("broadcast_v2: added column scheduled_broadcasts.%s", col_name)
        else:
            logger.info("broadcast_v2: column scheduled_broadcasts.%s already exists — skip", col_name)

    # ── 2. Create broadcast_logs table ────────────────────────────────────────
    if not _table_exists(bind, "broadcast_logs"):
        op.create_table(
            "broadcast_logs",
            sa.Column("id",               sa.Integer,     primary_key=True),
            sa.Column("broadcast_id",     sa.Integer,
                      sa.ForeignKey("scheduled_broadcasts.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("started_at",       sa.DateTime,    nullable=True),
            sa.Column("finished_at",      sa.DateTime,    nullable=True),
            sa.Column("total_recipients", sa.Integer,     nullable=False, default=0),
            sa.Column("sent",             sa.Integer,     nullable=False, default=0),
            sa.Column("delivered",        sa.Integer,     nullable=False, default=0),
            sa.Column("failed",           sa.Integer,     nullable=False, default=0),
            sa.Column("blocked",          sa.Integer,     nullable=False, default=0),
            sa.Column("skipped",          sa.Integer,     nullable=False, default=0),
            sa.Column("error_log",        sa.Text,        nullable=True),
            sa.Column("created_at",       sa.DateTime,    nullable=True),
        )
        op.create_index("ix_bl_broadcast_id", "broadcast_logs", ["broadcast_id"])
        logger.info("broadcast_v2: created table broadcast_logs")
    else:
        logger.info("broadcast_v2: table broadcast_logs already exists — skip")

    # ── 3. Create broadcast_retry_queue table ─────────────────────────────────
    if not _table_exists(bind, "broadcast_retry_queue"):
        op.create_table(
            "broadcast_retry_queue",
            sa.Column("id",           sa.Integer,     primary_key=True),
            sa.Column("broadcast_id", sa.Integer,
                      sa.ForeignKey("scheduled_broadcasts.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("telegram_id",  sa.BigInteger,  nullable=False, index=True),
            sa.Column("error_msg",    sa.String(512), nullable=True),
            sa.Column("retry_at",     sa.DateTime,    nullable=True),
            sa.Column("attempts",     sa.Integer,     nullable=False, default=0),
            sa.Column("status",       sa.String(16),  nullable=False,
                      default="pending", index=True),
            sa.Column("created_at",   sa.DateTime,    nullable=True),
        )
        op.create_index("ix_brq_status",      "broadcast_retry_queue", ["status"])
        op.create_index("ix_brq_retry_at",    "broadcast_retry_queue", ["retry_at"])
        op.create_index("ix_brq_broadcast",   "broadcast_retry_queue", ["broadcast_id"])
        logger.info("broadcast_v2: created table broadcast_retry_queue")
    else:
        logger.info("broadcast_v2: table broadcast_retry_queue already exists — skip")

    # ── 4. Seed BotConfig keys ────────────────────────────────────────────────
    for key, vtype, default, category in _BOT_CONFIG_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category) "
                    "VALUES (:k, :vt, :v, :cat)"
                ),
                {"k": key, "vt": vtype, "v": default, "cat": category},
            )
            logger.info("broadcast_v2: seeded BotConfig key %r", key)
        else:
            logger.info("broadcast_v2: BotConfig key %r already exists — skip", key)


def downgrade():
    bind = op.get_bind()

    # Drop tables
    for tbl in ("broadcast_retry_queue", "broadcast_logs"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)

    # Remove BotConfig keys
    for key, _, _, _ in _BOT_CONFIG_KEYS:
        bind.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})

    # Drop added columns (PostgreSQL supports DROP COLUMN IF EXISTS)
    for col_name, _, _ in _NEW_COLS:
        try:
            op.drop_column("scheduled_broadcasts", col_name)
        except Exception:
            pass
