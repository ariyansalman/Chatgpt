"""global_delivery_template: global delivery message template on settings (V40).

Revision ID: 20260728_globaldeliv
Revises: 20260728_binance_expiry_default

Adds a single nullable ``delivery_message_template`` TEXT column to the
``settings`` table. Admins configure it via the new Delivery Message Builder
in the admin panel. When NULL the system falls back to the built-in
DEFAULT_TEMPLATE so existing stores are unaffected.

Fully additive and nullable — zero impact on existing rows.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260728_globaldeliv"
down_revision = "20260728_binance_expiry_default"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "settings",
        sa.Column("delivery_message_template", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("settings", "delivery_message_template")
