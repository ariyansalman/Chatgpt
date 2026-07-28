"""delivery_format: per-product formatted-delivery template (V17).

Revision ID: 20260718_delivfmt
Revises: 20260716_slatix

Adds a single nullable ``delivery_format_template`` TEXT column to
``products``. Admins use this to define a "📄 Formatted Account" style
template (e.g. containing ``{email}``, ``{password}``, ``{recovery}``,
``{expiry}`` placeholders) that is rendered against structured
``ProductKey.key_value`` data at delivery time.

Fully additive and nullable — existing products keep working with the
legacy raw-text delivery path exactly as before when this column is NULL.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260718_delivfmt"
down_revision = "20260716_slatix"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("delivery_format_template", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("products", "delivery_format_template")
