"""auto_verify_retry_lock: add verification_in_progress / verification_locked_at / auto_verify_attempts.

Revision ID: 20260921_autoverifylock
Revises:     20260920_paynotify
Create Date: 2026-09-21

Backs the universal auto-verification retry engine (services/payment_workflow.py
run_auto_verification_with_retries). Every payment gateway now retries its
own API/gateway check several times before a deposit is ever handed to
manual review, and these columns are what make that safe under concurrency:

  verification_in_progress / verification_locked_at — a DB-backed lock,
    claimed via an atomic conditional UPDATE, so a user resubmitting a TXID,
    a background retry, and an admin's "Verify Again" tap can never run two
    verification jobs for the same order at once. The timestamp lets a
    lock left behind by a crashed/killed worker be treated as stale and
    reclaimed after a timeout, instead of wedging the order forever.

  auto_verify_attempts — running count of automatic verification attempts
    made for the order, for audit/observability only; never read for any
    business decision.

All three columns have server defaults so existing rows are silently
back-filled and no other Transaction feature is affected.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260921_autoverifylock"
down_revision = "20260920_paynotify"
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
    if not _column_exists(_TABLE, "verification_in_progress"):
        op.add_column(
            _TABLE,
            sa.Column("verification_in_progress", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.execute(sa.text(f"UPDATE {_TABLE} SET verification_in_progress = false WHERE verification_in_progress IS NULL"))

    if not _column_exists(_TABLE, "verification_locked_at"):
        op.add_column(_TABLE, sa.Column("verification_locked_at", sa.DateTime(), nullable=True))

    if not _column_exists(_TABLE, "auto_verify_attempts"):
        op.add_column(
            _TABLE,
            sa.Column("auto_verify_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
        op.execute(sa.text(f"UPDATE {_TABLE} SET auto_verify_attempts = 0 WHERE auto_verify_attempts IS NULL"))


def downgrade() -> None:
    if _column_exists(_TABLE, "auto_verify_attempts"):
        op.drop_column(_TABLE, "auto_verify_attempts")
    if _column_exists(_TABLE, "verification_locked_at"):
        op.drop_column(_TABLE, "verification_locked_at")
    if _column_exists(_TABLE, "verification_in_progress"):
        op.drop_column(_TABLE, "verification_in_progress")
