"""admin_roles: multi-admin RBAC (super_admin/moderator/support_staff) + OTP 2FA.

Revision ID: 20260712_adminroles
Revises: 20260711_gwpay

Fully additive — new table only, nothing existing is touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260712_adminroles"
down_revision = "20260711_gwpay"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    role_enum = sa.Enum("SUPER_ADMIN", "MODERATOR", "SUPPORT_STAFF", name="adminroletype")
    role_enum.create(bind, checkfirst=True)

    op.create_table(
        "admin_roles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("telegram_id", sa.BigInteger, nullable=False, unique=True, index=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", role_enum, nullable=False, server_default="SUPPORT_STAFF"),
        sa.Column("manage_products", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_orders", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_users", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_broadcasts", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_payments", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("view_analytics", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_settings", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manage_admins", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("added_by", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("otp_code_hash", sa.String(128), nullable=True),
        sa.Column("otp_expires_at", sa.DateTime, nullable=True),
        sa.Column("otp_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("otp_last_sent_at", sa.DateTime, nullable=True),
        sa.Column("session_verified_until", sa.DateTime, nullable=True),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_table("admin_roles")
    bind = op.get_bind()
    sa.Enum(name="adminroletype").drop(bind, checkfirst=True)
