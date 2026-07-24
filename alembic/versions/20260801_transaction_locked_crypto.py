"""transaction_locked_crypto: add locked_crypto_rate and locked_crypto_amount to transactions.

Revision ID: 20260801_transaction_locked_crypto
Revises: 20260731_bybit_ton

Adds two nullable Float columns to the ``transactions`` table so that
non-stablecoin on-chain orders (e.g. LTC) can lock the exchange rate and
exact crypto amount at order-creation time.

  locked_crypto_rate   — USD per 1 unit of the crypto asset at order time
  locked_crypto_amount — exact crypto units the user is required to send

Both columns are NULL for all existing USDT-based orders and are only
populated when an LTC (or future non-stablecoin) order is created.

Fully additive / non-destructive.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260801_transaction_locked_crypto"
down_revision = "20260731_bybit_ton"
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

    if not sa_inspect(bind).has_table("transactions"):
        logger.warning("transactions table not found — skipping locked_crypto migration.")
        return

    _safe_add_column("transactions", "locked_crypto_rate",   sa.Float)
    _safe_add_column("transactions", "locked_crypto_amount", sa.Float)


def downgrade():
    """Non-destructive — intentionally leaves the columns in place on downgrade."""
    pass
