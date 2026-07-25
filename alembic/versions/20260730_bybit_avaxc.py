"""bybit_avaxc: add USDT Avalanche C-Chain deposit address support to Bybit Pay gateway.

Revision ID: 20260730_bybit_avaxc
Revises: 20260729_bybit_ltc

Adds:
  - bybit_wallet_avaxc column to payment_gateway_configs (USDT Avalanche C-Chain deposit address)

Fully additive / non-destructive.  Uses IF NOT EXISTS guards so it is safe
to re-run on a database where the column already exists.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260730_bybit_avaxc"
down_revision = "20260729_bybit_ltc"
branch_labels = None
depends_on = None


def _col_exists(bind, table: str, column: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        cols = [c["name"] for c in sa_inspect(bind).get_columns(table)]
        return column in cols
    except Exception:
        return False


def _safe_add_column(table: str, column_name: str, column_type):
    bind = op.get_bind()
    if not _col_exists(bind, table, column_name):
        try:
            op.add_column(table, sa.Column(column_name, column_type, nullable=True))
            logger.info("Added column %s.%s", table, column_name)
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                logger.info("Column %s.%s already exists — skipping", table, column_name)
            else:
                logger.warning("Could not add column %s.%s: %s", table, column_name, e)


def upgrade():
    bind = op.get_bind()
    from sqlalchemy import inspect as sa_inspect

    if not sa_inspect(bind).has_table("payment_gateway_configs"):
        logger.warning(
            "payment_gateway_configs table not found — skipping bybit_avaxc migration."
        )
        return

    # Add the Avalanche C-Chain deposit address column alongside TRC20/BEP20/ERC20/LTC.
    _safe_add_column("payment_gateway_configs", "bybit_wallet_avaxc", sa.String(255))


def downgrade():
    """Non-destructive — intentionally leaves the column in place on downgrade."""
    pass
