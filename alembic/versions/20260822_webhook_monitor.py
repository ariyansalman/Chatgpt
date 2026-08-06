"""V27 — Webhook Monitor & API Health Dashboard.

Revision ID: 20260822_webhook_monitor
Revises: 20260821_scheduled_broadcast_v2

Changes
-------
• Creates ``api_health_log`` table  — per-check API status history.
• Creates ``webhook_log`` table     — every inbound webhook event.
• Creates ``webhook_retry_queue``   — failed webhook retry queue.
• Seeds new BotConfig keys for the monitor settings.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260822_webhook_monitor"
down_revision = "20260821_scheduled_broadcast_v2"
branch_labels = None
depends_on    = None


def _table_exists(bind, table: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": table},
    ).fetchone()
    return bool(row)


_BOT_CONFIG_KEYS = [
    # key, value_type, default, category
    ("webhook_monitor_status",         "str",  "enabled",   "monitoring"),
    ("webhook_monitor_auto_refresh",   "bool", "True",      "monitoring"),
    ("webhook_monitor_refresh_interval","int", "60",        "monitoring"),
    ("webhook_monitor_retry_count",    "int",  "3",         "monitoring"),
    ("webhook_monitor_timeout",        "int",  "10",        "monitoring"),
    ("webhook_monitor_admin_alerts",   "bool", "True",      "monitoring"),
    ("webhook_log_retention_days",     "int",  "30",        "monitoring"),
    ("health_slow_threshold_ms",       "int",  "2000",      "monitoring"),
    ("health_warn_threshold_ms",       "int",  "5000",      "monitoring"),
    ("health_check_interval",          "int",  "300",       "monitoring"),  # seconds
]


def upgrade():
    bind = op.get_bind()

    # ── 1. api_health_log ─────────────────────────────────────────────────
    if not _table_exists(bind, "api_health_log"):
        op.create_table(
            "api_health_log",
            sa.Column("id",               sa.Integer,     primary_key=True),
            sa.Column("service_name",     sa.String(64),  nullable=False, index=True),
            sa.Column("status",           sa.String(16),  nullable=False, index=True),
            # online|slow|warning|offline
            sa.Column("response_time_ms", sa.Integer,     nullable=True),
            sa.Column("error_message",    sa.String(512), nullable=True),
            sa.Column("http_status",      sa.Integer,     nullable=True),
            sa.Column("checked_at",       sa.DateTime,    nullable=False, index=True),
        )
        op.create_index("ix_ahl_service_checked", "api_health_log",
                        ["service_name", "checked_at"])
        logger.info("webhook_monitor: created table api_health_log")
    else:
        logger.info("webhook_monitor: table api_health_log already exists — skip")

    # ── 2. webhook_log ────────────────────────────────────────────────────
    if not _table_exists(bind, "webhook_log"):
        op.create_table(
            "webhook_log",
            sa.Column("id",                  sa.Integer,     primary_key=True),
            sa.Column("webhook_uuid",         sa.String(128), nullable=False,
                      unique=True, index=True),
            sa.Column("provider",             sa.String(32),  nullable=False, index=True),
            # nowpayments|binance|bybit|heleket|trc20|bep20|erc20|mobile|telegram
            sa.Column("received_at",          sa.DateTime,    nullable=False, index=True),
            sa.Column("processing_time_ms",   sa.Integer,     nullable=True),
            sa.Column("status",               sa.String(16),  nullable=False, index=True),
            # received|processed|failed|duplicate|ignored
            sa.Column("error_message",        sa.Text,        nullable=True),
            sa.Column("retry_count",          sa.Integer,     nullable=False, default=0),
            sa.Column("order_id",             sa.Integer,     nullable=True, index=True),
            sa.Column("user_id",              sa.Integer,     nullable=True, index=True),
            sa.Column("payment_id",           sa.String(128), nullable=True),
            sa.Column("transaction_id",       sa.String(128), nullable=True),
            sa.Column("raw_payload",          sa.Text,        nullable=True),
        )
        op.create_index("ix_whl_provider_status", "webhook_log", ["provider", "status"])
        op.create_index("ix_whl_received",        "webhook_log", ["received_at"])
        logger.info("webhook_monitor: created table webhook_log")
    else:
        logger.info("webhook_monitor: table webhook_log already exists — skip")

    # ── 3. webhook_retry_queue ─────────────────────────────────────────────
    if not _table_exists(bind, "webhook_retry_queue"):
        op.create_table(
            "webhook_retry_queue",
            sa.Column("id",             sa.Integer,     primary_key=True),
            sa.Column("webhook_log_id", sa.Integer,
                      sa.ForeignKey("webhook_log.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("provider",       sa.String(32),  nullable=False),
            sa.Column("payload",        sa.Text,        nullable=True),
            sa.Column("retry_at",       sa.DateTime,    nullable=True, index=True),
            sa.Column("attempts",       sa.Integer,     nullable=False, default=0),
            sa.Column("status",         sa.String(16),  nullable=False,
                      default="pending", index=True),
            # pending|processing|success|failed|abandoned
            sa.Column("last_error",     sa.String(512), nullable=True),
            sa.Column("created_at",     sa.DateTime,    nullable=True),
        )
        op.create_index("ix_wrq_status",    "webhook_retry_queue", ["status"])
        op.create_index("ix_wrq_retry_at",  "webhook_retry_queue", ["retry_at"])
        logger.info("webhook_monitor: created table webhook_retry_queue")
    else:
        logger.info("webhook_monitor: table webhook_retry_queue already exists — skip")

    # ── 4. Seed BotConfig ──────────────────────────────────────────────────
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
            logger.info("webhook_monitor: seeded BotConfig key %r", key)


def downgrade():
    bind = op.get_bind()
    for tbl in ("webhook_retry_queue", "webhook_log", "api_health_log"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
    for key, _, _, _ in _BOT_CONFIG_KEYS:
        bind.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
