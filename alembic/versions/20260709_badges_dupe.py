"""Section 14 badges + Section 15 duplicate protection.

Revision ID: 20260709_bd
Revises: 20260708_pt360

Additive, non-destructive, re-runnable.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260709_bd"
down_revision = "20260708_pt360"
branch_labels = None
depends_on = None


def _has_col(table: str, col: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade():
    # Section 14 — products badge fields
    if not _has_col("products", "is_featured"):
        op.add_column("products", sa.Column(
            "is_featured", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_col("products", "sale_price"):
        op.add_column("products", sa.Column("sale_price", sa.Float(), nullable=True))
    if not _has_col("products", "sales_count"):
        op.add_column("products", sa.Column(
            "sales_count", sa.Integer(), nullable=False, server_default="0"))
    if not _has_index("products", "ix_products_is_featured"):
        op.create_index("ix_products_is_featured", "products", ["is_featured"])

    # Section 15 — product_keys fingerprint
    if not _has_col("product_keys", "key_fingerprint"):
        op.add_column("product_keys", sa.Column(
            "key_fingerprint", sa.String(length=64), nullable=True))
    if not _has_index("product_keys", "ix_product_keys_key_fingerprint"):
        op.create_index(
            "ix_product_keys_key_fingerprint",
            "product_keys",
            ["key_fingerprint"],
        )
    # Composite uniqueness per product to catch duplicate imports.
    if not _has_index("product_keys", "uq_product_keys_product_fp"):
        try:
            op.create_index(
                "uq_product_keys_product_fp",
                "product_keys",
                ["product_id", "key_fingerprint"],
                unique=True,
                postgresql_where=sa.text("key_fingerprint IS NOT NULL"),
            )
        except Exception:
            # SQLite / older Postgres without partial index support — best-effort
            pass


def downgrade():
    # Intentionally minimal (non-destructive downgrade).
    for ix in ("uq_product_keys_product_fp",
               "ix_product_keys_key_fingerprint",
               "ix_products_is_featured"):
        try:
            op.drop_index(ix)
        except Exception:
            pass
    for col in ("key_fingerprint",):
        try:
            op.drop_column("product_keys", col)
        except Exception:
            pass
    for col in ("sales_count", "sale_price", "is_featured"):
        try:
            op.drop_column("products", col)
        except Exception:
            pass
