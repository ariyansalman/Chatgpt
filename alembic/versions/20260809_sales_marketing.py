"""sales_marketing: Part 3 — Gift Purchase, Gift Cards, Bundle pricing,
Review moderation columns.

Revision ID: 20260809_sales_marketing
Revises: 20260808_account_features

Fully additive:
  • New tables: gift_cards, gift_card_redemptions, gift_purchases
  • New columns on products: bundle_price, bundle_discount_percent
  • New columns on reviews: is_approved, is_pinned, updated_at

All changes use IF NOT EXISTS guards so the migration is safe to re-run.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision     = "20260809_sales_marketing"
down_revision = "20260808_account_features"
branch_labels = None
depends_on    = None


def _table_exists(bind, table: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        return sa_inspect(bind).has_table(table)
    except Exception:
        return False


def _column_exists(bind, table: str, column: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        cols = [c["name"] for c in sa_inspect(bind).get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade():
    bind = op.get_bind()

    # ── gift_cards ─────────────────────────────────────────────────────────
    if not _table_exists(bind, "gift_cards"):
        try:
            op.create_table(
                "gift_cards",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("code", sa.String(64), nullable=False),
                sa.Column("label", sa.String(120), nullable=True),
                # card_type stored as VARCHAR: 'fixed' | 'percent' | 'custom'
                sa.Column("card_type", sa.String(16), nullable=False, server_default="fixed"),
                sa.Column("value", sa.Float(), nullable=False),
                sa.Column("expires_at", sa.DateTime(), nullable=True),
                sa.Column("max_uses", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("is_single_use", sa.Boolean(), nullable=False, server_default="false"),
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
                sa.Column("created_at", sa.DateTime(), nullable=True),
                sa.Column("created_by", sa.BigInteger(), nullable=True),
            )
            op.create_index("ix_gc_code",      "gift_cards", ["code"],      unique=True)
            op.create_index("ix_gc_is_active",  "gift_cards", ["is_active"])
            op.create_index("ix_gc_expires_at", "gift_cards", ["expires_at"])
            logger.info("Created gift_cards table")
        except Exception as e:
            logger.warning("gift_cards: %s", e)

    # ── gift_card_redemptions ────────────────────────────────────────────────
    if not _table_exists(bind, "gift_card_redemptions"):
        try:
            op.create_table(
                "gift_card_redemptions",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("card_id", sa.Integer(), sa.ForeignKey("gift_cards.id"), nullable=False),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),      nullable=False),
                sa.Column("redeemed_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_gcr_card_id", "gift_card_redemptions", ["card_id"])
            op.create_index("ix_gcr_user_id", "gift_card_redemptions", ["user_id"])
            # Unique: one redemption per user per card
            op.create_index(
                "uq_gcr_card_user", "gift_card_redemptions",
                ["card_id", "user_id"], unique=True
            )
            logger.info("Created gift_card_redemptions table")
        except Exception as e:
            logger.warning("gift_card_redemptions: %s", e)

    # ── gift_purchases ───────────────────────────────────────────────────────
    if not _table_exists(bind, "gift_purchases"):
        try:
            op.create_table(
                "gift_purchases",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
                sa.Column("sender_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("recipient_telegram_id", sa.BigInteger(), nullable=True),
                sa.Column("recipient_username", sa.String(120), nullable=True),
                sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
                sa.Column("gift_message", sa.Text(), nullable=True),
                sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default="false"),
                # status: 'pending' | 'notified' | 'undeliverable'
                sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
                sa.Column("created_at", sa.DateTime(), nullable=True),
                sa.Column("notified_at", sa.DateTime(), nullable=True),
            )
            op.create_index("ix_gp_order_id",      "gift_purchases", ["order_id"])
            op.create_index("ix_gp_sender_user_id", "gift_purchases", ["sender_user_id"])
            op.create_index("ix_gp_status",         "gift_purchases", ["status"])
            op.create_index("ix_gp_created_at",     "gift_purchases", ["created_at"])
            logger.info("Created gift_purchases table")
        except Exception as e:
            logger.warning("gift_purchases: %s", e)

    # ── products: bundle columns ─────────────────────────────────────────────
    for col, ctype, default in [
        ("bundle_price",            "FLOAT", "NULL"),
        ("bundle_discount_percent", "FLOAT", "NULL"),
    ]:
        if not _column_exists(bind, "products", col):
            try:
                op.add_column(
                    "products",
                    sa.Column(col, sa.Float(), nullable=True)
                )
                logger.info("Added products.%s", col)
            except Exception as e:
                logger.warning("products.%s: %s", col, e)

    # ── reviews: moderation columns ──────────────────────────────────────────
    for col, col_def in [
        ("is_approved", sa.Column("is_approved", sa.Boolean(),
                                  nullable=False, server_default="true")),
        ("is_pinned",   sa.Column("is_pinned",   sa.Boolean(),
                                  nullable=False, server_default="false")),
        ("updated_at",  sa.Column("updated_at",  sa.DateTime(), nullable=True)),
    ]:
        if not _column_exists(bind, "reviews", col):
            try:
                op.add_column("reviews", col_def)
                logger.info("Added reviews.%s", col)
            except Exception as e:
                logger.warning("reviews.%s: %s", col, e)

    # Index on reviews.is_approved for fast pending-approval queries
    try:
        op.create_index("ix_reviews_is_approved", "reviews", ["is_approved"])
    except Exception:
        pass
    try:
        op.create_index("ix_reviews_is_pinned", "reviews", ["is_pinned"])
    except Exception:
        pass


def downgrade():
    """Non-destructive — intentionally leaves tables and columns in place."""
    pass
