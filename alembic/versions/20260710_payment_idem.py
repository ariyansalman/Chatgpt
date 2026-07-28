"""Section 16 — PaymentIdempotency table.

Revision ID: 20260710_pi
Revises: 20260709_bd
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260710_pi"
down_revision = "20260709_bd"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table("payment_idempotency"):
        return
    op.create_table(
        "payment_idempotency",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False, index=True),
        sa.Column("external_ref", sa.String(length=180), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True, index=True),
        sa.UniqueConstraint("source", "external_ref",
                            name="uq_payment_idem_src_ref"),
    )


def downgrade():
    try:
        op.drop_table("payment_idempotency")
    except Exception:
        pass
