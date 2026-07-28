"""premium_core: product variants, stock reservations, order status history

Revision ID: 20260705_pc
Revises: 20260704_adm2
Create Date: 2026-07-05

Fully additive / non-destructive:
 - creates ``product_variants``, ``stock_reservations``, ``order_status_history``
 - adds nullable columns to ``product_keys``, ``cart``, ``order_items``, ``orders``
 - re-runnable: every op is guarded by an existence check
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260705_pc"
down_revision = "20260704_adm2"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ── product_variants ────────────────────────────────────────────
    if not _has_table(bind, "product_variants"):
        op.create_table(
            "product_variants",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer,
                      sa.ForeignKey("products.id"), nullable=False, index=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("price", sa.Float, nullable=False),
            sa.Column("sale_price", sa.Float, nullable=True),
            sa.Column("stock_count", sa.Integer, nullable=True,
                      server_default=sa.text("0")),
            sa.Column("is_active", sa.Boolean, nullable=True,
                      server_default=sa.text("1" if dialect == "sqlite" else "true")),
            sa.Column("display_order", sa.Integer, nullable=True,
                      server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime, nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime, nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_product_variants_is_active",
                        "product_variants", ["is_active"])

    # ── stock_reservations ──────────────────────────────────────────
    if not _has_table(bind, "stock_reservations"):
        op.create_table(
            "stock_reservations",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("product_id", sa.Integer,
                      sa.ForeignKey("products.id"), nullable=False, index=True),
            sa.Column("variant_id", sa.Integer,
                      sa.ForeignKey("product_variants.id"), nullable=True, index=True),
            sa.Column("order_id", sa.Integer,
                      sa.ForeignKey("orders.id"), nullable=True, index=True),
            sa.Column("quantity", sa.Integer, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default=sa.text("'ACTIVE'")),
            sa.Column("created_at", sa.DateTime, nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("released_at", sa.DateTime, nullable=True),
        )
        op.create_index("ix_stock_reservations_status",
                        "stock_reservations", ["status"])
        op.create_index("ix_stock_reservations_expires_at",
                        "stock_reservations", ["expires_at"])

    # ── order_status_history ────────────────────────────────────────
    if not _has_table(bind, "order_status_history"):
        op.create_table(
            "order_status_history",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer,
                      sa.ForeignKey("orders.id"), nullable=False, index=True),
            sa.Column("from_status", sa.String(32), nullable=True),
            sa.Column("to_status", sa.String(32), nullable=False),
            sa.Column("actor_type", sa.String(16), nullable=False,
                      server_default=sa.text("'system'")),
            sa.Column("admin_id", sa.BigInteger, nullable=True),
            sa.Column("reason", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_order_status_history_created_at",
                        "order_status_history", ["created_at"])

    # ── nullable columns on existing tables ─────────────────────────
    if not _has_column(bind, "product_keys", "variant_id"):
        with op.batch_alter_table("product_keys") as batch:
            batch.add_column(sa.Column("variant_id", sa.Integer, nullable=True))
    if not _has_column(bind, "product_keys", "reservation_id"):
        with op.batch_alter_table("product_keys") as batch:
            batch.add_column(sa.Column("reservation_id", sa.Integer, nullable=True))

    if not _has_column(bind, "cart", "variant_id"):
        with op.batch_alter_table("cart") as batch:
            batch.add_column(sa.Column("variant_id", sa.Integer, nullable=True))
    if not _has_column(bind, "cart", "updated_at"):
        with op.batch_alter_table("cart") as batch:
            batch.add_column(sa.Column("updated_at", sa.DateTime, nullable=True))

    if not _has_column(bind, "order_items", "variant_id"):
        with op.batch_alter_table("order_items") as batch:
            batch.add_column(sa.Column("variant_id", sa.Integer, nullable=True))

    if not _has_column(bind, "orders", "lifecycle_status"):
        with op.batch_alter_table("orders") as batch:
            batch.add_column(sa.Column("lifecycle_status", sa.String(32), nullable=True))
    if not _has_column(bind, "orders", "payment_status"):
        with op.batch_alter_table("orders") as batch:
            batch.add_column(sa.Column("payment_status", sa.String(32), nullable=True))
    if not _has_column(bind, "orders", "delivery_status"):
        with op.batch_alter_table("orders") as batch:
            batch.add_column(sa.Column("delivery_status", sa.String(32), nullable=True))


def downgrade():
    # Non-destructive migration: downgrade is a no-op to preserve data.
    pass