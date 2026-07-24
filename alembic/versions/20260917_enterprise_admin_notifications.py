"""Enterprise Admin Notification System — new per-admin preference columns.

Adds six new boolean columns to ``admin_notification_prefs`` so the
fan-out service can gate the new enterprise events (new_user, deposit,
payment_failed, payment_expired, payment_reversed, order_delivered)
on each admin's personal toggle, exactly like the existing columns.

Revision ID: 20260917_enterprise_admin_notifications
Revises:     20260916_search_indexes
Create Date: 2026-09-17

All columns default to TRUE so existing admins silently opt-in to every
new event type without any manual migration step.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260917_enterprise_admin_notifications"
down_revision = "20260916_search_indexes"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    """Return True if the column already exists (idempotent guard)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return (result.scalar() or 0) > 0


_TABLE = "admin_notification_prefs"

_NEW_COLUMNS = [
    ("new_user",         sa.Boolean(), True),
    ("deposit",          sa.Boolean(), True),
    ("payment_failed",   sa.Boolean(), True),
    ("payment_expired",  sa.Boolean(), True),
    ("payment_reversed", sa.Boolean(), True),
    ("order_delivered",  sa.Boolean(), True),
]


def upgrade() -> None:
    for col_name, col_type, default_val in _NEW_COLUMNS:
        if not _column_exists(_TABLE, col_name):
            op.add_column(
                _TABLE,
                sa.Column(
                    col_name,
                    col_type,
                    nullable=False,
                    server_default=sa.text("true"),
                ),
            )
            # Back-fill any NULL rows left by older DB engines
            op.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET {col_name} = true WHERE {col_name} IS NULL"
                )
            )


def downgrade() -> None:
    for col_name, _, _ in _NEW_COLUMNS:
        if _column_exists(_TABLE, col_name):
            op.drop_column(_TABLE, col_name)
