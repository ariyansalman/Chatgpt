"""admin_api_keys_approval: allow admins to configure API keys via panel,
add verification log and pending manual verification tables.

Revision ID: 20260724_admin_api_keys
Revises: 20260723_zinipay_wallet_numbers

Fully additive / non-destructive:
- Adds binance_api_key / binance_api_secret columns to payment_gateway_configs
- Adds bybit_api_key / bybit_api_secret columns to payment_gateway_configs
- Creates payment_verification_log table (security audit trail)
- Creates pending_manual_verifications table (admin approve/reject flow)
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260724_admin_api_keys"
down_revision = "20260723_zinipay_wallets"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # ── payment_gateway_configs: add admin-configurable API key columns ──
    # Use try/except per column so partial upgrades don't block the next run.
    for col_name, col_type in [
        ("binance_api_key", sa.Text()),
        ("binance_api_secret", sa.Text()),
        ("bybit_api_key", sa.Text()),
        ("bybit_api_secret", sa.Text()),
    ]:
        try:
            op.add_column("payment_gateway_configs", sa.Column(col_name, col_type, nullable=True))
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                logger.info("Column %s already exists — skipping", col_name)
            else:
                logger.warning("Could not add column %s: %s", col_name, e)

    # ── payment_verification_log ─────────────────────────────────────────
    if not _table_exists(bind, "payment_verification_log"):
        op.create_table(
            "payment_verification_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("gateway", sa.String(32), nullable=False, index=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("submitted_txid", sa.String(256), nullable=False),
            sa.Column("outcome", sa.String(64), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("ip_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    # ── pending_manual_verifications ─────────────────────────────────────
    if not _table_exists(bind, "pending_manual_verifications"):
        op.create_table(
            "pending_manual_verifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("gateway", sa.String(32), nullable=False, index=True),  # "binance_pay" | "bybit_pay"
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("submitted_txid", sa.String(256), nullable=False),
            sa.Column("amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(16), nullable=False),
            sa.Column("payment_type", sa.String(32), nullable=True),   # uid_transfer | onchain (Bybit)
            sa.Column("network", sa.String(16), nullable=True),         # TRC20/BEP20/ERC20 (Bybit onchain)
            sa.Column("auto_outcome", sa.String(64), nullable=True),    # what the API returned
            sa.Column("auto_detail", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            # pending | approved | rejected
            sa.Column("admin_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )


def downgrade():
    """Non-destructive — intentionally leaves new columns and tables."""
    pass


def _table_exists(bind, table_name: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        insp = sa_inspect(bind)
        return insp.has_table(table_name)
    except Exception:
        return False
