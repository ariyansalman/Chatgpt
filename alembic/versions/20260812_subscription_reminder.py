"""subscription_reminder: Add subscription_reminder_logs table.

Revision ID: 20260812_subscription_reminder
Revises: 20260811_giftcardtype_enum

Tracks which expiry-reminder intervals (30/15/7/3/1 days before expiry,
and 0 for the expired notice) have already been sent per subscription.
A UniqueConstraint on (subscription_id, interval_days) prevents duplicates.

New table: subscription_reminder_logs
Columns:
    id                  INTEGER  PRIMARY KEY
    subscription_id     INTEGER  NOT NULL  FK → subscriptions.id  INDEX
    interval_days       INTEGER  NOT NULL  (30/15/7/3/1/0)
    sent_at             DATETIME NOT NULL  DEFAULT now
    success             BOOLEAN  NOT NULL  DEFAULT true
    retry_count         INTEGER  NOT NULL  DEFAULT 0

Safe to re-run (idempotent: guarded by table-exists check).
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision     = "20260812_subscription_reminder"
down_revision = "20260811_giftcardtype_enum"
branch_labels = None
depends_on    = None


def upgrade():
    bind = op.get_bind()

    # ── Idempotency guard ────────────────────────────────────────────────
    if bind.dialect.name == "postgresql":
        row = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'subscription_reminder_logs'"
        )).fetchone()
    else:
        # SQLite
        row = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='subscription_reminder_logs'"
        )).fetchone()

    if row is not None:
        logger.info("subscription_reminder: table already exists — skipping.")
        return

    op.create_table(
        "subscription_reminder_logs",
        sa.Column("id",              sa.Integer(),  primary_key=True, nullable=False),
        sa.Column("subscription_id", sa.Integer(),  sa.ForeignKey("subscriptions.id"),
                  nullable=False, index=True),
        sa.Column("interval_days",   sa.Integer(),  nullable=False),
        sa.Column("sent_at",         sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("success",         sa.Boolean(),  nullable=False, server_default=sa.text("1")),
        sa.Column("retry_count",     sa.Integer(),  nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("subscription_id", "interval_days",
                            name="uq_sub_reminder_interval"),
    )
    logger.info("subscription_reminder: created subscription_reminder_logs table.")


def downgrade():
    """Drop the table. Data is non-critical (reminders are re-sent on next cycle)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'subscription_reminder_logs'"
        )).fetchone()
    else:
        row = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='subscription_reminder_logs'"
        )).fetchone()

    if row is not None:
        op.drop_table("subscription_reminder_logs")
        logger.info("subscription_reminder: dropped subscription_reminder_logs.")
