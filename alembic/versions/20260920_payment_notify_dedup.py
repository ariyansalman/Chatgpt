"""payment_notify_dedup: add expiry_notified / review_notified flags.

Revision ID: 20260920_paynotify
Revises:     20260919_product_soft_delete
Create Date: 2026-09-20

Root cause of the duplicate "Payment Expired" / "Payment Review" messages:
notification sends were gated only on the transaction's `status` (or, for
manual-verification retries, on a (gateway, order, txid) tuple that changes
on every retry). Neither survives a re-submitted TXID, an overlapping
scheduler run, or a bot restart between the DB commit and the outbound
`send_message` call.

These two columns are the durable, per-order "already notified" markers
required by the fix: they are flipped exactly once via an atomic conditional
UPDATE at the moment a notification is actually sent, so the scheduler and
the retry handlers can unconditionally skip any order that already has one.

Both columns have server defaults so existing rows are silently back-filled
and no other Transaction feature is affected.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260920_paynotify"
down_revision = "20260919_product_soft_delete"
branch_labels = None
depends_on = None

_TABLE = "transactions"


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return (result.scalar() or 0) > 0


def upgrade() -> None:
    if not _column_exists(_TABLE, "expiry_notified"):
        op.add_column(
            _TABLE,
            sa.Column("expiry_notified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.execute(sa.text(f"UPDATE {_TABLE} SET expiry_notified = false WHERE expiry_notified IS NULL"))

    if not _column_exists(_TABLE, "review_notified"):
        op.add_column(
            _TABLE,
            sa.Column("review_notified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.execute(sa.text(f"UPDATE {_TABLE} SET review_notified = false WHERE review_notified IS NULL"))


def downgrade() -> None:
    if _column_exists(_TABLE, "review_notified"):
        op.drop_column(_TABLE, "review_notified")
    if _column_exists(_TABLE, "expiry_notified"):
        op.drop_column(_TABLE, "expiry_notified")
