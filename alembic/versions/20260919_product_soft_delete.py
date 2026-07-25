"""Product soft-delete — fix NotNullViolation on order_items.product_id.

Root cause: the admin "delete product" action called session.delete(product).
order_items.product_id is NOT NULL, and Product.order_items /
OrderItem.product is a plain (non-cascading) relationship, so on flush
SQLAlchemy tried to dissociate every related OrderItem by setting its
product_id to NULL, which violates the NOT NULL constraint and raised
IntegrityError. Deleting the row also would have permanently broken the
FK target for any OrderItem that *did* get flushed first.

Fix: never physically delete a Product that might have order history.
Add is_deleted / deleted_at so the admin "delete" action can hide the
product (is_active=False, is_deleted=True) while keeping the row alive,
which keeps every existing OrderItem.product_id valid and non-null and
requires no change to the order system itself.

Revision ID: 20260919_product_soft_delete
Revises:     20260918_product_template_system
Create Date: 2026-09-19

Both new columns have server defaults so existing rows are silently
back-filled and every other Product feature is unaffected.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260919_product_soft_delete"
down_revision = "20260918_product_template_system"
branch_labels = None
depends_on = None

_TABLE = "products"


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return (result.scalar() or 0) > 0


def upgrade() -> None:
    if not _column_exists(_TABLE, "is_deleted"):
        op.add_column(
            _TABLE,
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.execute(sa.text(f"UPDATE {_TABLE} SET is_deleted = false WHERE is_deleted IS NULL"))

    if not _column_exists(_TABLE, "deleted_at"):
        op.add_column(_TABLE, sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _column_exists(_TABLE, "deleted_at"):
        op.drop_column(_TABLE, "deleted_at")
    if _column_exists(_TABLE, "is_deleted"):
        op.drop_column(_TABLE, "is_deleted")
