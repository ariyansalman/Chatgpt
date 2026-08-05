"""admin_center: wallet ledger, promotions, admin notification prefs, low-stock alert state

Revision ID: 20260706_admc
Revises: 20260705_pc
Create Date: 2026-07-06

Fully additive / non-destructive:
 - creates ``wallet_ledger``, ``promotions``, ``admin_notification_prefs``,
   ``low_stock_alert_state``
 - re-runnable: every op is guarded by an existence check
 - no columns dropped, no types changed
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260706_admc"
down_revision = "20260705_pc"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, "wallet_ledger"):
        op.create_table(
            "wallet_ledger",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("delta", sa.Float, nullable=False),
            sa.Column("balance_after", sa.Float, nullable=False),
            sa.Column("reason", sa.String(255), nullable=True),
            sa.Column("actor_type", sa.String(16), nullable=False,
                      server_default="system"),
            sa.Column("actor_id", sa.BigInteger, nullable=True),
            sa.Column("ref_type", sa.String(32), nullable=True),
            sa.Column("ref_id", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
        )
        op.create_index("ix_wallet_ledger_user_created",
                        "wallet_ledger", ["user_id", "created_at"])

    if not _has_table(bind, "promotions"):
        op.create_table(
            "promotions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("coupon_id", sa.Integer, sa.ForeignKey("coupons.id"),
                      nullable=True, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=True, index=True),
            sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id"),
                      nullable=True, index=True),
            sa.Column("discount_pct", sa.Float, nullable=True),
            sa.Column("starts_at", sa.DateTime, nullable=True),
            sa.Column("ends_at", sa.DateTime, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=True,
                      server_default=sa.text("1"), index=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table(bind, "admin_notification_prefs"):
        op.create_table(
            "admin_notification_prefs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("admin_telegram_id", sa.BigInteger, nullable=False,
                      unique=True, index=True),
            sa.Column("new_order", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("manual_payment", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("dispute", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("low_stock", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("refund", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("ticket_reply", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table(bind, "low_stock_alert_state"):
        op.create_table(
            "low_stock_alert_state",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("variant_id", sa.Integer,
                      sa.ForeignKey("product_variants.id"),
                      nullable=True, index=True),
            sa.Column("last_alert_at", sa.DateTime, nullable=True),
            sa.Column("last_stock_seen", sa.Integer, nullable=True,
                      server_default=sa.text("0")),
        )


def downgrade():
    # Intentional no-op: additive, non-destructive migration.
    # If a true rollback is ever required, drop the four tables manually.
    pass
