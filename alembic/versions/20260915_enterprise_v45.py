"""V45 — Enterprise Features: Restock Notifications, Product Scheduler,
         Recommendation Pins.

Revision ID: 20260915_enterprise_v45
Revises:     20260914_broadcast_campaign_manager
Create Date: 2026-09-15

Creates:
  - restock_subscriptions        (user OOS notification subscriptions)
  - restock_notification_logs    (delivery audit trail)
  - product_schedules            (scheduled product changes)
  - product_recommendation_pins  (admin-pinned recommendations)

Seeds bot_config keys for all four features.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260915_enterprise_v45"
down_revision = "20260914_broadcast_campaign_manager"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(name)


def _config_key_exists(conn, key: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone()
    return result is not None


def upgrade() -> None:
    # ── restock_subscriptions ────────────────────────────────────────────────
    if not _table_exists("restock_subscriptions"):
        op.create_table(
            "restock_subscriptions",
            sa.Column("id",            sa.Integer(),    primary_key=True),
            sa.Column("user_id",       sa.Integer(),    sa.ForeignKey("users.id",    ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("product_id",    sa.Integer(),    sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("subscribed_at", sa.DateTime(),   nullable=False),
            sa.Column("notified",      sa.Boolean(),    nullable=False, server_default=sa.false(), index=True),
            sa.Column("notified_at",   sa.DateTime(),   nullable=True),
            sa.UniqueConstraint("user_id", "product_id", name="uq_restock_user_product"),
        )

    # ── restock_notification_logs ────────────────────────────────────────────
    if not _table_exists("restock_notification_logs"):
        op.create_table(
            "restock_notification_logs",
            sa.Column("id",                    sa.Integer(),    primary_key=True),
            sa.Column("product_id",            sa.Integer(),    sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("product_name_snapshot", sa.String(255),  nullable=True),
            sa.Column("telegram_id",           sa.BigInteger(), nullable=False, index=True),
            sa.Column("status",                sa.String(16),   nullable=False, server_default="sent", index=True),
            sa.Column("error_message",         sa.String(512),  nullable=True),
            sa.Column("sent_at",               sa.DateTime(),   nullable=False, index=True),
        )

    # ── product_schedules ────────────────────────────────────────────────────
    if not _table_exists("product_schedules"):
        op.create_table(
            "product_schedules",
            sa.Column("id",                    sa.Integer(),    primary_key=True),
            sa.Column("product_id",            sa.Integer(),    sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("product_name_snapshot", sa.String(255),  nullable=True),
            sa.Column("admin_id",              sa.BigInteger(), nullable=False, index=True),
            sa.Column("schedule_type",         sa.String(32),   nullable=False, index=True),
            sa.Column("execute_at",            sa.DateTime(),   nullable=False, index=True),
            sa.Column("payload_json",          sa.Text(),       nullable=True),
            sa.Column("timezone_name",         sa.String(64),   nullable=False, server_default="UTC"),
            sa.Column("notes",                 sa.Text(),       nullable=True),
            sa.Column("status",                sa.String(16),   nullable=False, server_default="pending", index=True),
            sa.Column("executed_at",           sa.DateTime(),   nullable=True),
            sa.Column("cancelled_at",          sa.DateTime(),   nullable=True),
            sa.Column("result_message",        sa.String(512),  nullable=True),
            sa.Column("created_at",            sa.DateTime(),   nullable=False, index=True),
        )

    # ── product_recommendation_pins ──────────────────────────────────────────
    if not _table_exists("product_recommendation_pins"):
        op.create_table(
            "product_recommendation_pins",
            sa.Column("id",                     sa.Integer(),    primary_key=True),
            sa.Column("admin_id",               sa.BigInteger(), nullable=False, index=True),
            sa.Column("section",                sa.String(64),   nullable=False, index=True),
            sa.Column("product_id",             sa.Integer(),    sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("recommended_product_id", sa.Integer(),    sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("display_order",          sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("created_at",             sa.DateTime(),   nullable=False),
            sa.UniqueConstraint("section", "product_id", "recommended_product_id",
                                name="uq_rec_pin_section_prod"),
        )

    # ── Seed bot_config keys ─────────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        # Restock Notifications
        ("rsn_status",              "enabled", "RSN: feature status (enabled/disabled)"),
        ("rsn_check_interval_min",  "5",       "RSN: background job check interval in minutes"),
        ("rsn_max_subs_per_user",   "20",      "RSN: max active subscriptions per user"),
        # Product Scheduler
        ("aps_status",              "enabled", "APS: product scheduler feature status"),
        ("aps_check_interval_sec",  "60",      "APS: background job check interval in seconds"),
        ("aps_max_pending",         "500",     "APS: max pending schedules allowed"),
        # Recommendation Engine
        ("rec_status",              "enabled", "REC: recommendation engine status"),
        ("rec_trending_days",       "30",      "REC: lookback days for trending products"),
        ("rec_max_results",         "10",      "REC: max recommendations per section"),
        # Customer Segmentation
        ("cseg_status",             "enabled", "CSEG: customer segmentation feature status"),
        ("cseg_auto_update_hours",  "24",      "CSEG: auto-segment recompute interval hours"),
    ]
    for key, value, description in seed:
        if not _config_key_exists(conn, key):
            conn.execute(
                sa.text("INSERT INTO bot_config (key, value, description) VALUES (:k, :v, :d)"),
                {"k": key, "v": value, "d": description},
            )


def downgrade() -> None:
    for tbl in ["product_recommendation_pins", "product_schedules",
                "restock_notification_logs", "restock_subscriptions"]:
        if _table_exists(tbl):
            op.drop_table(tbl)
    conn = op.get_bind()
    keys = [
        "rsn_status", "rsn_check_interval_min", "rsn_max_subs_per_user",
        "aps_status", "aps_check_interval_sec", "aps_max_pending",
        "rec_status", "rec_trending_days", "rec_max_results",
        "cseg_status", "cseg_auto_update_hours",
    ]
    for k in keys:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": k})
