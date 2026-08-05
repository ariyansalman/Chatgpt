"""zinipay_used_transactions: duplicate-payment prevention for ZiniPay.

Revision ID: 20260721_zinipay
Revises: 20260720_txn_cancelled

Creates the ``zinipay_used_transactions`` table.  A row is inserted only
after a successful POST /v1/trx/confirm — the UNIQUE constraint on
``trx_id`` prevents any trxID from ever being credited twice, even under
concurrent requests.  This mirrors the same pattern used by
BinancePayTransaction / BybitPayTransaction.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260721_zinipay"
down_revision = "20260720_txncancel"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "zinipay_used_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trx_id", sa.String(128), nullable=False, index=True),
        sa.Column("verify_id", sa.Integer(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column(
            "internal_order_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("sender", sa.String(128), nullable=True),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("trx_id", name="uq_zinipay_trx_id"),
    )


def downgrade():
    op.drop_table("zinipay_used_transactions")
