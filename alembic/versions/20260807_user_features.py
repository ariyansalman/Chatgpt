"""user_features: Wishlist, Price Drop Alerts, Recently Viewed,
Quick Buy, Preferred Payment tables.

Revision ID: 20260807_user_features
Revises: 20260806_bybit_sol

Fully additive — uses IF NOT EXISTS guards everywhere so re-running is safe.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260807_user_features"
down_revision = "20260806_bybit_sol"
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

    # ── user_wishlists ────────────────────────────────────────────────────
    if not _table_exists(bind, "user_wishlists"):
        try:
            op.create_table(
                "user_wishlists",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
                sa.Column("created_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_uwl_user", "user_wishlists", ["user_id"])
            op.create_index("ix_uwl_product", "user_wishlists", ["product_id"])
            op.create_unique_constraint(
                "uq_wishlist_user_product", "user_wishlists", ["user_id", "product_id"]
            )
            logger.info("Created user_wishlists table")
        except Exception as e:
            logger.warning("user_wishlists: %s", e)

    # ── price_drop_alerts ─────────────────────────────────────────────────
    if not _table_exists(bind, "price_drop_alerts"):
        try:
            op.create_table(
                "price_drop_alerts",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
                sa.Column("subscribed_at", sa.DateTime(), nullable=True),
                sa.Column("last_notified_price", sa.Float(), nullable=True),
            )
            op.create_index("ix_pda_user", "price_drop_alerts", ["user_id"])
            op.create_index("ix_pda_product", "price_drop_alerts", ["product_id"])
            op.create_unique_constraint(
                "uq_pda_user_product", "price_drop_alerts", ["user_id", "product_id"]
            )
            logger.info("Created price_drop_alerts table")
        except Exception as e:
            logger.warning("price_drop_alerts: %s", e)

    # ── recently_viewed ───────────────────────────────────────────────────
    if not _table_exists(bind, "recently_viewed"):
        try:
            op.create_table(
                "recently_viewed",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
                sa.Column("viewed_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_rv_user", "recently_viewed", ["user_id"])
            op.create_index("ix_rv_product", "recently_viewed", ["product_id"])
            op.create_unique_constraint(
                "uq_rv_user_product", "recently_viewed", ["user_id", "product_id"]
            )
            logger.info("Created recently_viewed table")
        except Exception as e:
            logger.warning("recently_viewed: %s", e)

    # ── quick_buy_configs ─────────────────────────────────────────────────
    if not _table_exists(bind, "quick_buy_configs"):
        try:
            op.create_table(
                "quick_buy_configs",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
                sa.Column("payment_method", sa.String(64), nullable=True),
                sa.Column("quantity", sa.Integer(), nullable=True),
                sa.Column("last_used_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_qbc_user", "quick_buy_configs", ["user_id"])
            op.create_index("ix_qbc_product", "quick_buy_configs", ["product_id"])
            op.create_unique_constraint(
                "uq_qbc_user_product", "quick_buy_configs", ["user_id", "product_id"]
            )
            logger.info("Created quick_buy_configs table")
        except Exception as e:
            logger.warning("quick_buy_configs: %s", e)

    # ── preferred_payments ────────────────────────────────────────────────
    if not _table_exists(bind, "preferred_payments"):
        try:
            op.create_table(
                "preferred_payments",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("payment_method", sa.String(64), nullable=False),
                sa.Column("set_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_pp_user", "preferred_payments", ["user_id"])
            op.create_unique_constraint("uq_pp_user", "preferred_payments", ["user_id"])
            logger.info("Created preferred_payments table")
        except Exception as e:
            logger.warning("preferred_payments: %s", e)


def downgrade():
    """Non-destructive — intentionally leaves tables in place on downgrade."""
    pass
