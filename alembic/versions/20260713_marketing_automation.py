"""marketing_automation: abandoned-cart + win-back campaigns (V14).

Revision ID: 20260713_mktauto
Revises: 20260712_adminroles

Fully additive:
  * ``users.last_seen_at`` — nullable activity timestamp (win-back detection).
  * ``marketing_touches`` — new dedup ledger table, nothing existing touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260713_mktauto"
down_revision = "20260712_adminroles"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    op.add_column("users", sa.Column("last_seen_at", sa.DateTime(), nullable=True))
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"])

    campaign_enum = sa.Enum(
        "CART_30M", "CART_24H", "WINBACK_7D", "WINBACK_30D",
        name="marketingcampaigntype",
    )
    campaign_enum.create(bind, checkfirst=True)

    op.create_table(
        "marketing_touches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("campaign_type", campaign_enum, nullable=False, index=True),
        sa.Column("reference_at", sa.DateTime(), nullable=False),
        sa.Column("coupon_code", sa.String(64), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "campaign_type", "reference_at",
                            name="uq_marketing_touch_dedup"),
    )


def downgrade():
    op.drop_table("marketing_touches")
    bind = op.get_bind()
    sa.Enum(name="marketingcampaigntype").drop(bind, checkfirst=True)
    op.drop_index("ix_users_last_seen_at", table_name="users")
    op.drop_column("users", "last_seen_at")
