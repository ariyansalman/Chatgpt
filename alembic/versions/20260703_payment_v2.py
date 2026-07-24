"""payment_v2: extend manual_payment_methods + transactions

Revision ID: 20260703_pv2
Revises:
Create Date: 2026-07-03

This mirrors migrations/v6_payment_v2.py so teams that use alembic get the
same schema. Idempotent via existence checks — safe to run when the raw v6
script has already been run.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260703_pv2"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    bind = op.get_bind()

    if _has_column(bind, "manual_payment_methods", "id"):
        with op.batch_alter_table("manual_payment_methods") as batch:
            if not _has_column(bind, "manual_payment_methods", "account_label"):
                batch.add_column(sa.Column("account_label", sa.String(120), nullable=True))
            if not _has_column(bind, "manual_payment_methods", "account_number"):
                batch.add_column(sa.Column("account_number", sa.String(255), nullable=True))
            if not _has_column(bind, "manual_payment_methods", "max_amount"):
                batch.add_column(sa.Column("max_amount", sa.Float(), nullable=True))
            if not _has_column(bind, "manual_payment_methods", "require_txid"):
                batch.add_column(sa.Column("require_txid", sa.Boolean(), nullable=False,
                                           server_default=sa.true()))
            if not _has_column(bind, "manual_payment_methods", "require_proof"):
                batch.add_column(sa.Column("require_proof", sa.Boolean(), nullable=False,
                                           server_default=sa.true()))

    if _has_column(bind, "transactions", "id"):
        with op.batch_alter_table("transactions") as batch:
            if not _has_column(bind, "transactions", "txid"):
                batch.add_column(sa.Column("txid", sa.String(128), nullable=True))
                batch.create_index("ix_transactions_txid", ["txid"])
            if not _has_column(bind, "transactions", "proof_file_id"):
                batch.add_column(sa.Column("proof_file_id", sa.String(256), nullable=True))

        # Backfill photo file_id from legacy crypto_address
        op.execute(
            "UPDATE transactions SET proof_file_id = SUBSTR(crypto_address, 7) "
            "WHERE crypto_address LIKE 'photo:%' "
            "AND (proof_file_id IS NULL OR proof_file_id = '')"
        )


def downgrade():
    with op.batch_alter_table("transactions") as batch:
        try:
            batch.drop_index("ix_transactions_txid")
        except Exception:
            pass
        for col in ("txid", "proof_file_id"):
            try:
                batch.drop_column(col)
            except Exception:
                pass

    with op.batch_alter_table("manual_payment_methods") as batch:
        for col in ("account_label", "account_number", "max_amount",
                    "require_txid", "require_proof"):
            try:
                batch.drop_column(col)
            except Exception:
                pass