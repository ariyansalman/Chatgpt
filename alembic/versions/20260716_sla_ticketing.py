"""sla_ticketing: priority + SLA deadline on support tickets & disputes (V16).

Revision ID: 20260716_slatix
Revises: 20260714_flashsales

Adds a ``priority`` column (low/medium/high/urgent) and SLA-tracking
columns (``sla_deadline``, ``sla_reminder_sent``, ``sla_breached``) to
both ``support_tickets`` and ``disputes``, plus a ``resolved_at`` column
on ``support_tickets`` (disputes already had one). Also adds two new
per-admin notification toggles (``sla_warning`` / ``sla_breach``) on
``admin_notification_prefs``.

Fully additive — no existing columns touched, safe to run on a live DB.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_slatix"
down_revision = "20260714_flashsales"
branch_labels = None
depends_on = None


# NOTE: TicketPriority is stored as a native SQL ENUM (matching the existing
# convention used by TicketStatus / DisputeStatus on these same tables).
ticket_priority = sa.Enum("low", "medium", "high", "urgent", name="ticketpriority")


def upgrade():
    bind = op.get_bind()
    ticket_priority.create(bind, checkfirst=True)

    # ─── support_tickets ────────────────────────────────────────────────
    op.add_column("support_tickets",
                  sa.Column("priority", ticket_priority, nullable=False,
                            server_default="medium"))
    op.add_column("support_tickets",
                  sa.Column("sla_deadline", sa.DateTime(), nullable=True))
    op.add_column("support_tickets",
                  sa.Column("sla_reminder_sent", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.add_column("support_tickets",
                  sa.Column("sla_breached", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.add_column("support_tickets",
                  sa.Column("resolved_at", sa.DateTime(), nullable=True))
    op.create_index("ix_support_tickets_priority", "support_tickets", ["priority"])
    op.create_index("ix_support_tickets_sla_deadline", "support_tickets", ["sla_deadline"])

    # ─── disputes ────────────────────────────────────────────────────────
    op.add_column("disputes",
                  sa.Column("priority", ticket_priority, nullable=False,
                            server_default="high"))
    op.add_column("disputes",
                  sa.Column("sla_deadline", sa.DateTime(), nullable=True))
    op.add_column("disputes",
                  sa.Column("sla_reminder_sent", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.add_column("disputes",
                  sa.Column("sla_breached", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.create_index("ix_disputes_priority", "disputes", ["priority"])
    op.create_index("ix_disputes_sla_deadline", "disputes", ["sla_deadline"])

    # ─── admin_notification_prefs ──────────────────────────────────────
    op.add_column("admin_notification_prefs",
                  sa.Column("sla_warning", sa.Boolean(), nullable=False,
                            server_default=sa.true()))
    op.add_column("admin_notification_prefs",
                  sa.Column("sla_breach", sa.Boolean(), nullable=False,
                            server_default=sa.true()))


def downgrade():
    op.drop_column("admin_notification_prefs", "sla_breach")
    op.drop_column("admin_notification_prefs", "sla_warning")

    op.drop_index("ix_disputes_sla_deadline", table_name="disputes")
    op.drop_index("ix_disputes_priority", table_name="disputes")
    op.drop_column("disputes", "sla_breached")
    op.drop_column("disputes", "sla_reminder_sent")
    op.drop_column("disputes", "sla_deadline")
    op.drop_column("disputes", "priority")

    op.drop_index("ix_support_tickets_sla_deadline", table_name="support_tickets")
    op.drop_index("ix_support_tickets_priority", table_name="support_tickets")
    op.drop_column("support_tickets", "resolved_at")
    op.drop_column("support_tickets", "sla_breached")
    op.drop_column("support_tickets", "sla_reminder_sent")
    op.drop_column("support_tickets", "sla_deadline")
    op.drop_column("support_tickets", "priority")

    bind = op.get_bind()
    ticket_priority.drop(bind, checkfirst=True)
