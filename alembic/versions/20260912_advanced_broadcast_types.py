"""Advanced Broadcast Types & Smart Audience Targeting.

Revision ID: 20260912_advanced_broadcast_types
Revises:     20260911_enterprise_broadcast_center
Create Date: 2026-09-12

Adds to scheduled_broadcasts:
  - broadcast_type       TEXT nullable   — type key (coupon, flash_sale, …)
  - audience_filters_json TEXT nullable  — JSON object of combined audience filters
  - template_used        TEXT nullable   — template key/snapshot used
  - variables_json       TEXT nullable   — JSON object of variable overrides/values

Seeds new bot_config keys for Advanced Broadcast Types.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision      = "20260912_advanced_broadcast_types"
down_revision = "20260911_enterprise_broadcast_center"
branch_labels = None
depends_on    = None


def _col_exists(table: str, col: str) -> bool:
    from sqlalchemy import inspect
    return col in [c["name"] for c in inspect(op.get_bind()).get_columns(table)]


def _key_exists(conn, key: str) -> bool:
    return bool(conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone())


def upgrade() -> None:
    for col, typ in [
        ("broadcast_type",       sa.String(64)),
        ("audience_filters_json", sa.Text()),
        ("template_used",        sa.Text()),
        ("variables_json",       sa.Text()),
    ]:
        if not _col_exists("scheduled_broadcasts", col):
            op.add_column("scheduled_broadcasts", sa.Column(col, typ, nullable=True))

    conn = op.get_bind()
    seed = [
        ("advanced_broadcast_types_status",   "enabled",
         "Advanced Broadcast Types feature status: enabled / maintenance / disabled."),
        ("broadcast_smart_filters_enabled",   "true",
         "Enable smart audience filter combinations in Advanced Broadcast Types."),
        ("broadcast_types_enabled",           "true",
         "Enable the Advanced Broadcast Types selection UI."),
        ("broadcast_variables_enabled",       "true",
         "Enable message variable substitution ({first_name}, {coupon_code}, etc.)."),
        ("broadcast_test_mode_enabled",       "true",
         "Enable test-send (to self or selected user) in Advanced Broadcast Types."),
        ("broadcast_audience_preview_enabled","true",
         "Enable live audience count preview before sending in Advanced Broadcast Types."),
        # Thresholds
        ("abt_top_customers_count",           "50",
         "Advanced Broadcast: how many users qualify as 'Top Customers' (by lifetime spend)."),
        ("abt_repeat_customer_orders",        "3",
         "Advanced Broadcast: minimum completed orders to qualify as 'Repeat Customer'."),
        ("abt_high_spend_threshold",          "50.0",
         "Advanced Broadcast: minimum lifetime spend (USD) for 'High Spending' segment."),
        ("abt_low_spend_threshold",           "10.0",
         "Advanced Broadcast: maximum lifetime spend (USD) for 'Low Spending' segment."),
        ("abt_new_user_days",                 "7",
         "Advanced Broadcast: users registered within this many days qualify as 'New Users'."),
        ("abt_active_user_days",              "7",
         "Advanced Broadcast: users seen within this many days qualify as 'Active Users'."),
        ("abt_inactive_user_days",            "30",
         "Advanced Broadcast: users not seen within this many days qualify as 'Inactive Users'."),
        ("abt_sub_expiring_days",             "7",
         "Advanced Broadcast: subscriptions expiring within this many days show as 'Expiring Soon'."),
    ]
    for key, value, desc in seed:
        if not _key_exists(conn, key):
            conn.execute(
                sa.text("INSERT INTO bot_config (key, value, description) VALUES (:k, :v, :d)"),
                {"k": key, "v": value, "d": desc},
            )


def downgrade() -> None:
    for col in ("variables_json", "template_used", "audience_filters_json", "broadcast_type"):
        try:
            op.drop_column("scheduled_broadcasts", col)
        except Exception:
            pass
    conn = op.get_bind()
    for key in (
        "advanced_broadcast_types_status", "broadcast_smart_filters_enabled",
        "broadcast_types_enabled", "broadcast_variables_enabled",
        "broadcast_test_mode_enabled", "broadcast_audience_preview_enabled",
        "abt_top_customers_count", "abt_repeat_customer_orders",
        "abt_high_spend_threshold", "abt_low_spend_threshold",
        "abt_new_user_days", "abt_active_user_days", "abt_inactive_user_days",
        "abt_sub_expiring_days",
    ):
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
