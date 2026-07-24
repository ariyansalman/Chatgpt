"""admin_v2: create admin_audit_logs table

Revision ID: 20260704_adm2
Revises: 20260703_pv2
Create Date: 2026-07-04

Idempotent: safe to re-run and safe when the raw migrations/v7_admin_v2.py
has already been executed against the same database.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260704_adm2"
down_revision = "20260703_pv2"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade():
    bind = op.get_bind()
    if _has_table(bind, "admin_audit_logs"):
        return
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("admin_telegram_id", sa.BigInteger, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_admin_audit_logs_admin_telegram_id",
                    "admin_audit_logs", ["admin_telegram_id"])
    op.create_index("ix_admin_audit_logs_action",
                    "admin_audit_logs", ["action"])
    op.create_index("ix_admin_audit_logs_created_at",
                    "admin_audit_logs", ["created_at"])


def downgrade():
    bind = op.get_bind()
    if not _has_table(bind, "admin_audit_logs"):
        return
    op.drop_index("ix_admin_audit_logs_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_action", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_admin_telegram_id", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")