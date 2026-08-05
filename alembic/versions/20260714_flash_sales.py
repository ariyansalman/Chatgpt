"""flash_sales: time-boxed % discounts on a product or category (V15).

Revision ID: 20260714_flashsales
Revises: 20260713_mktauto

Fully additive — a brand new ``flash_sales`` table, nothing existing touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260714_flashsales"
down_revision = "20260713_mktauto"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "flash_sales",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=True, index=True),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id"), nullable=True, index=True),
        sa.Column("discount_percent", sa.Float, nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_flash_sales_product_id", "flash_sales", ["product_id"])
    op.create_index("ix_flash_sales_category_id", "flash_sales", ["category_id"])
    op.create_index("ix_flash_sales_start_time", "flash_sales", ["start_time"])
    op.create_index("ix_flash_sales_end_time", "flash_sales", ["end_time"])
    op.create_index("ix_flash_sales_is_active", "flash_sales", ["is_active"])


def downgrade():
    op.drop_index("ix_flash_sales_is_active", table_name="flash_sales")
    op.drop_index("ix_flash_sales_end_time", table_name="flash_sales")
    op.drop_index("ix_flash_sales_start_time", table_name="flash_sales")
    op.drop_index("ix_flash_sales_category_id", table_name="flash_sales")
    op.drop_index("ix_flash_sales_product_id", table_name="flash_sales")
    op.drop_table("flash_sales")
