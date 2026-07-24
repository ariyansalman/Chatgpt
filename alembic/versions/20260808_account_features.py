"""account_features: OrderReceipt, UserDownload, ActivityLog, UserSession tables.

Revision ID: 20260808_account_features
Revises: 20260807_user_features

V19 — Account & Order Features.
Fully additive — uses IF NOT EXISTS guards everywhere so re-running is safe.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260808_account_features"
down_revision = "20260807_user_features"
branch_labels = None
depends_on = None


def _table_exists(bind, table: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        return sa_inspect(bind).has_table(table)
    except Exception:
        return False


def upgrade():
    bind = op.get_bind()

    # ── order_receipts ────────────────────────────────────────────────────
    if not _table_exists(bind, "order_receipts"):
        try:
            op.create_table(
                "order_receipts",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("receipt_number", sa.String(32), nullable=False),
                sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
                sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("receipt_type", sa.String(16), nullable=False, server_default="purchase"),
                sa.Column("created_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_or_receipt_number", "order_receipts", ["receipt_number"], unique=True)
            op.create_index("ix_or_order_id", "order_receipts", ["order_id"])
            op.create_index("ix_or_user_id", "order_receipts", ["user_id"])
            op.create_unique_constraint("uq_receipt_order_id", "order_receipts", ["order_id"])
            logger.info("Created order_receipts table")
        except Exception as e:
            logger.warning("order_receipts: %s", e)

    # ── user_downloads ────────────────────────────────────────────────────
    if not _table_exists(bind, "user_downloads"):
        try:
            op.create_table(
                "user_downloads",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
                sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_items.id"), nullable=False),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
                sa.Column("product_name", sa.String(255), nullable=False),
                sa.Column("asset_type", sa.String(32), nullable=False, server_default="key"),
                sa.Column("download_count", sa.Integer(), nullable=True, server_default="0"),
                sa.Column("last_downloaded_at", sa.DateTime(), nullable=True),
                sa.Column("expires_at", sa.DateTime(), nullable=True),
                sa.Column("created_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_ud_user_id", "user_downloads", ["user_id"])
            op.create_index("ix_ud_order_id", "user_downloads", ["order_id"])
            op.create_index("ix_ud_order_item_id", "user_downloads", ["order_item_id"])
            op.create_index("ix_ud_product_id", "user_downloads", ["product_id"])
            op.create_index("ix_ud_created_at", "user_downloads", ["created_at"])
            op.create_unique_constraint(
                "uq_download_user_item", "user_downloads", ["user_id", "order_item_id"]
            )
            logger.info("Created user_downloads table")
        except Exception as e:
            logger.warning("user_downloads: %s", e)

    # ── activity_logs ─────────────────────────────────────────────────────
    if not _table_exists(bind, "activity_logs"):
        try:
            op.create_table(
                "activity_logs",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("action", sa.String(64), nullable=False),
                sa.Column("status", sa.String(16), nullable=False, server_default="success"),
                sa.Column("details", sa.Text(), nullable=True),
                sa.Column("ref_type", sa.String(32), nullable=True),
                sa.Column("ref_id", sa.String(64), nullable=True),
                sa.Column("created_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_al_user_id", "activity_logs", ["user_id"])
            op.create_index("ix_al_action", "activity_logs", ["action"])
            op.create_index("ix_al_created_at", "activity_logs", ["created_at"])
            logger.info("Created activity_logs table")
        except Exception as e:
            logger.warning("activity_logs: %s", e)

    # ── user_sessions ─────────────────────────────────────────────────────
    if not _table_exists(bind, "user_sessions"):
        try:
            op.create_table(
                "user_sessions",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("session_token", sa.String(64), nullable=False),
                sa.Column("device_info", sa.String(255), nullable=True),
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
                sa.Column("created_at", sa.DateTime(), nullable=True),
                sa.Column("last_active_at", sa.DateTime(), nullable=True),
                sa.Column("terminated_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_us_user_id", "user_sessions", ["user_id"])
            op.create_index("ix_us_session_token", "user_sessions", ["session_token"], unique=True)
            op.create_index("ix_us_is_active", "user_sessions", ["is_active"])
            op.create_index("ix_us_created_at", "user_sessions", ["created_at"])
            logger.info("Created user_sessions table")
        except Exception as e:
            logger.warning("user_sessions: %s", e)


def downgrade():
    """Non-destructive — intentionally leaves tables in place on downgrade."""
    pass
