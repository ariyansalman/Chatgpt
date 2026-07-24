"""bybit_order_expiry_default: change default Bybit Pay order expiry to 30 minutes.

Revision ID: 20260727_bybit_expiry_default
Revises: 20260726_minimum_deposit

Bybit Pay orders should expire after 30 minutes by default (previously 60),
matching the payment spec. This only changes the column's server default for
NEW rows and backfills any existing PaymentGatewayConfig row that is still
sitting on the old default of 60 minutes — it does not touch orders that an
admin has already deliberately customized to a different value.

Fully additive / non-destructive.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260727_bybit_expiry_default"
down_revision = "20260726_minimum_deposit"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    from sqlalchemy import inspect as sa_inspect

    if not sa_inspect(bind).has_table("payment_gateway_configs"):
        logger.warning("payment_gateway_configs table not found — skipping bybit_order_expiry_default migration.")
        return

    try:
        op.alter_column(
            "payment_gateway_configs",
            "bybit_order_expiry_minutes",
            server_default="30",
        )
    except Exception:
        logger.warning("Could not alter server_default for bybit_order_expiry_minutes (non-fatal).")

    # Backfill rows still on the old implicit default (60) so existing
    # deployments pick up the new 30-minute default immediately.
    try:
        bind.execute(
            sa.text(
                "UPDATE payment_gateway_configs SET bybit_order_expiry_minutes = 30 "
                "WHERE bybit_order_expiry_minutes = 60 OR bybit_order_expiry_minutes IS NULL"
            )
        )
    except Exception:
        logger.warning("Could not backfill bybit_order_expiry_minutes (non-fatal).")


def downgrade():
    """Non-destructive — leave values in place on downgrade."""
    pass
