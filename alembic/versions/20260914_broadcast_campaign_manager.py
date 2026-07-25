"""Enterprise Broadcast Campaign Manager, Template Library & Automation Rules.

Revision ID: 20260914_broadcast_campaign_manager
Revises:     20260913_broadcast_analytics
Create Date: 2026-09-14

New tables:
  - broadcast_templates       — reusable message templates with favorites/groups
  - broadcast_campaigns       — campaign manager (multi-step, scheduled, drip, A/B)
  - campaign_executions       — per-run execution history for campaigns
  - broadcast_automation_rules — event-driven automation triggers
  - automation_trigger_logs   — dedup log for automation triggers

Seeds:
  - Pre-built default templates (11 templates)
  - bot_config keys for Campaign Manager settings
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision      = "20260914_broadcast_campaign_manager"
down_revision = "20260913_broadcast_analytics"
branch_labels = None
depends_on    = None


def _table_exists(table: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(table)


def _col_exists(table: str, col: str) -> bool:
    from sqlalchemy import inspect
    return col in [c["name"] for c in inspect(op.get_bind()).get_columns(table)]


def _key_exists(conn, key: str) -> bool:
    return bool(conn.execute(
        sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
    ).fetchone())


def upgrade() -> None:
    # ── broadcast_templates ──────────────────────────────────────────────────
    if not _table_exists("broadcast_templates"):
        op.create_table(
            "broadcast_templates",
            sa.Column("id",            sa.Integer(),    primary_key=True),
            sa.Column("name",          sa.String(100),  nullable=False),
            sa.Column("category",      sa.String(64),   nullable=True, index=True),
            sa.Column("group_name",    sa.String(64),   nullable=True),
            sa.Column("message_text",  sa.Text(),       nullable=False),
            sa.Column("media_type",    sa.String(16),   nullable=False, server_default="text"),
            sa.Column("button_text",   sa.String(64),   nullable=True),
            sa.Column("button_url",    sa.String(512),  nullable=True),
            sa.Column("parse_mode",    sa.String(16),   nullable=False, server_default="HTML"),
            sa.Column("variables_json", sa.Text(),      nullable=True),
            sa.Column("is_default",    sa.Boolean(),    server_default="false", nullable=False),
            sa.Column("is_favorite",   sa.Boolean(),    server_default="false", nullable=False),
            sa.Column("usage_count",   sa.Integer(),    server_default="0",     nullable=False),
            sa.Column("created_by",    sa.BigInteger(), nullable=True),
            sa.Column("created_at",    sa.DateTime(),   nullable=False),
            sa.Column("updated_at",    sa.DateTime(),   nullable=False),
        )

    # ── broadcast_campaigns ──────────────────────────────────────────────────
    if not _table_exists("broadcast_campaigns"):
        op.create_table(
            "broadcast_campaigns",
            sa.Column("id",               sa.Integer(),    primary_key=True),
            sa.Column("name",             sa.String(100),  nullable=False),
            sa.Column("campaign_type",    sa.String(32),   nullable=False, server_default="single"),
            sa.Column("status",           sa.String(32),   nullable=False, server_default="draft", index=True),
            sa.Column("template_id",      sa.Integer(),
                      sa.ForeignKey("broadcast_templates.id", ondelete="SET NULL"), nullable=True),
            # scheduling
            sa.Column("start_date",               sa.DateTime(),  nullable=True),
            sa.Column("end_date",                 sa.DateTime(),  nullable=True),
            sa.Column("timezone",                 sa.String(64),  nullable=False, server_default="UTC"),
            sa.Column("schedule_type",            sa.String(16),  nullable=True),
            sa.Column("schedule_interval_hours",  sa.Integer(),   nullable=True),
            sa.Column("schedule_days_json",       sa.Text(),      nullable=True),
            # targeting
            sa.Column("target_segment",       sa.String(32), nullable=False, server_default="all"),
            sa.Column("audience_filters_json", sa.Text(),    nullable=True),
            # message
            sa.Column("message_text",  sa.Text(),       nullable=True),
            sa.Column("media_type",    sa.String(16),   nullable=False, server_default="text"),
            sa.Column("file_id",       sa.String(256),  nullable=True),
            sa.Column("button_text",   sa.String(64),   nullable=True),
            sa.Column("button_url",    sa.String(512),  nullable=True),
            sa.Column("parse_mode",    sa.String(16),   nullable=False, server_default="HTML"),
            sa.Column("variables_json", sa.Text(),      nullable=True),
            # A/B testing
            sa.Column("ab_test_enabled",  sa.Boolean(), server_default="false", nullable=False),
            sa.Column("ab_variant_b_text", sa.Text(),   nullable=True),
            sa.Column("ab_winner",         sa.String(1), nullable=True),
            sa.Column("ab_split_percent",  sa.Integer(), server_default="50", nullable=False),
            sa.Column("ab_ctr_a",          sa.Integer(), server_default="0",  nullable=False),
            sa.Column("ab_ctr_b",          sa.Integer(), server_default="0",  nullable=False),
            # multi-step / drip steps
            sa.Column("steps_json", sa.Text(), nullable=True),
            # stats
            sa.Column("total_runs",      sa.Integer(), server_default="0", nullable=False),
            sa.Column("total_sent",      sa.Integer(), server_default="0", nullable=False),
            sa.Column("total_delivered", sa.Integer(), server_default="0", nullable=False),
            sa.Column("total_failed",    sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_run_at",  sa.DateTime(), nullable=True),
            sa.Column("next_run_at",  sa.DateTime(), nullable=True, index=True),
            sa.Column("is_archived",  sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_by",   sa.BigInteger(), nullable=True),
            sa.Column("created_at",   sa.DateTime(),   nullable=False),
            sa.Column("updated_at",   sa.DateTime(),   nullable=False),
        )

    # ── campaign_executions ──────────────────────────────────────────────────
    if not _table_exists("campaign_executions"):
        op.create_table(
            "campaign_executions",
            sa.Column("id",           sa.Integer(), primary_key=True),
            sa.Column("campaign_id",  sa.Integer(),
                      sa.ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("step_index",   sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status",       sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at",   sa.DateTime(), nullable=True),
            sa.Column("finished_at",  sa.DateTime(), nullable=True),
            sa.Column("total_recipients", sa.Integer(), server_default="0", nullable=False),
            sa.Column("sent",         sa.Integer(), server_default="0", nullable=False),
            sa.Column("delivered",    sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed",       sa.Integer(), server_default="0", nullable=False),
            sa.Column("ab_variant",   sa.String(1), nullable=True),
            sa.Column("ab_sent_a",    sa.Integer(), server_default="0", nullable=False),
            sa.Column("ab_sent_b",    sa.Integer(), server_default="0", nullable=False),
            sa.Column("error_log",    sa.Text(),    nullable=True),
            sa.Column("created_at",   sa.DateTime(), nullable=False),
        )

    # ── broadcast_automation_rules ───────────────────────────────────────────
    if not _table_exists("broadcast_automation_rules"):
        op.create_table(
            "broadcast_automation_rules",
            sa.Column("id",          sa.Integer(),   primary_key=True),
            sa.Column("name",        sa.String(100), nullable=False),
            sa.Column("trigger",     sa.String(64),  nullable=False, index=True),
            sa.Column("is_enabled",  sa.Boolean(),   server_default="true", nullable=False),
            sa.Column("template_id", sa.Integer(),
                      sa.ForeignKey("broadcast_templates.id", ondelete="SET NULL"), nullable=True),
            sa.Column("campaign_id", sa.Integer(),
                      sa.ForeignKey("broadcast_campaigns.id",  ondelete="SET NULL"), nullable=True),
            # custom message (when no template)
            sa.Column("message_text",   sa.Text(),      nullable=True),
            sa.Column("media_type",     sa.String(16),  nullable=False, server_default="text"),
            sa.Column("button_text",    sa.String(64),  nullable=True),
            sa.Column("button_url",     sa.String(512), nullable=True),
            sa.Column("parse_mode",     sa.String(16),  nullable=False, server_default="HTML"),
            sa.Column("variables_json", sa.Text(),      nullable=True),
            # trigger conditions
            sa.Column("conditions_json",   sa.Text(),   nullable=True),
            sa.Column("delay_minutes",     sa.Integer(), server_default="0", nullable=False),
            sa.Column("target_segment",    sa.String(32), nullable=False, server_default="trigger_user"),
            sa.Column("dedup_window_hours", sa.Integer(), server_default="24", nullable=False),
            # stats
            sa.Column("trigger_count",    sa.Integer(),  server_default="0", nullable=False),
            sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
            sa.Column("created_by",   sa.BigInteger(), nullable=True),
            sa.Column("created_at",   sa.DateTime(),   nullable=False),
            sa.Column("updated_at",   sa.DateTime(),   nullable=False),
        )

    # ── automation_trigger_logs ───────────────────────────────────────────────
    if not _table_exists("automation_trigger_logs"):
        op.create_table(
            "automation_trigger_logs",
            sa.Column("id",              sa.Integer(),    primary_key=True),
            sa.Column("rule_id",         sa.Integer(),
                      sa.ForeignKey("broadcast_automation_rules.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("user_telegram_id", sa.BigInteger(), nullable=True, index=True),
            sa.Column("trigger_key",     sa.String(128),  nullable=True),
            sa.Column("sent",            sa.Boolean(),    server_default="false", nullable=False),
            sa.Column("triggered_at",    sa.DateTime(),   nullable=False),
        )

    # ── Seed pre-built default templates ─────────────────────────────────────
    from datetime import datetime
    conn = op.get_bind()
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    default_templates = [
        {
            "name": "🎯 Coupon Broadcast",
            "category": "coupon",
            "group_name": "Promotions",
            "message_text": (
                "🎟 <b>Exclusive Coupon Just For You, {first_name}!</b>\n\n"
                "Use code: <code>{coupon_code}</code>\n"
                "Discount: <b>{discount}%</b>\n\n"
                "⏰ Valid for a limited time only. Don't miss out!"
            ),
        },
        {
            "name": "🔥 Flash Sale",
            "category": "flash_sale",
            "group_name": "Promotions",
            "message_text": (
                "🔥 <b>FLASH SALE — Limited Time!</b>\n\n"
                "Hey {first_name}, a special deal is LIVE right now!\n\n"
                "🏷 <b>{product_name}</b>\n"
                "Was: <s>{old_price}</s>  ➜  Now: <b>{new_price}</b>\n\n"
                "⚡ Grab it before it's gone!"
            ),
        },
        {
            "name": "🎁 Giveaway",
            "category": "giveaway",
            "group_name": "Promotions",
            "message_text": (
                "🎁 <b>GIVEAWAY TIME, {first_name}!</b>\n\n"
                "We're giving away <b>{product_name}</b> to lucky users.\n"
                "Earn <b>{bonus}</b> bonus points for every entry!\n\n"
                "Enter now for your chance to win — limited entries only!"
            ),
        },
        {
            "name": "📢 New Product",
            "category": "new_product",
            "group_name": "Products",
            "message_text": (
                "📢 <b>NEW PRODUCT ALERT, {first_name}!</b>\n\n"
                "We just added: <b>{product_name}</b>\n"
                "Category: {category_name}\n\n"
                "🛒 Be the first to grab it — tap below to order now!"
            ),
        },
        {
            "name": "📈 Price Drop",
            "category": "price_drop",
            "group_name": "Products",
            "message_text": (
                "📉 <b>Price Drop Alert for {first_name}!</b>\n\n"
                "Good news! <b>{product_name}</b> just got cheaper.\n\n"
                "Was: <s>{old_price}</s>\n"
                "Now: <b>{new_price}</b> 🎉\n\n"
                "⏰ Limited time — order now before the price goes back up!"
            ),
        },
        {
            "name": "📦 Restock",
            "category": "restock",
            "group_name": "Products",
            "message_text": (
                "📦 <b>BACK IN STOCK, {first_name}!</b>\n\n"
                "<b>{product_name}</b> is available again!\n"
                "Category: {category_name}\n\n"
                "🚀 Order now before it sells out again!"
            ),
        },
        {
            "name": "💰 Wallet Bonus",
            "category": "wallet_bonus",
            "group_name": "Wallet",
            "message_text": (
                "💰 <b>Wallet Bonus Alert, {first_name}!</b>\n\n"
                "Your current balance: <b>{wallet_balance}</b>\n\n"
                "Top up now and get <b>{bonus}</b> extra bonus credited instantly!\n\n"
                "🎁 Limited-time offer — don't miss it!"
            ),
        },
        {
            "name": "🎉 Referral Reward",
            "category": "referral_reward",
            "group_name": "Referral",
            "message_text": (
                "🎉 <b>Referral Reward for {first_name}!</b>\n\n"
                "You've earned <b>{bonus}</b> from your referrals! 🙌\n\n"
                "The more friends you invite, the more you earn.\n"
                "Share your referral link and keep the rewards coming!"
            ),
        },
        {
            "name": "🚨 Maintenance",
            "category": "maintenance",
            "group_name": "System",
            "message_text": (
                "🚨 <b>Scheduled Maintenance Notice</b>\n\n"
                "Dear {first_name},\n\n"
                "Our bot will undergo scheduled maintenance shortly.\n"
                "Please complete any pending orders or transactions before then.\n\n"
                "We apologize for the inconvenience and will be back shortly. 🙏"
            ),
        },
        {
            "name": "👋 Welcome Message",
            "category": "welcome",
            "group_name": "Engagement",
            "message_text": (
                "👋 <b>Welcome, {first_name}!</b>\n\n"
                "We're thrilled to have you here.\n\n"
                "Explore our store, browse products, and enjoy exclusive deals — "
                "made just for members like you. 🎉"
            ),
        },
        {
            "name": "🙏 Thank You Message",
            "category": "thank_you",
            "group_name": "Engagement",
            "message_text": (
                "🙏 <b>Thank You, {first_name}!</b>\n\n"
                "Your order <b>#{order_id}</b> has been processed successfully.\n\n"
                "We appreciate your trust and look forward to serving you again. 💙"
            ),
        },
    ]

    for t in default_templates:
        conn.execute(
            sa.text(
                "INSERT INTO broadcast_templates "
                "(name, category, group_name, message_text, media_type, parse_mode, "
                " is_default, is_favorite, usage_count, created_at, updated_at) "
                "VALUES (:name, :category, :group_name, :message_text, 'text', 'HTML', "
                " true, false, 0, :now, :now)"
            ),
            {**t, "now": now},
        )

    # ── bot_config keys ───────────────────────────────────────────────────────
    seed = [
        # Feature status / toggles
        ("broadcast_campaign_manager_status",   "enabled",
         "Broadcast Campaign Manager feature status: enabled / maintenance / disabled."),
        ("broadcast_campaigns_enabled",         "true",
         "Enable the Broadcast Campaign Manager — create, schedule, and run campaigns."),
        ("broadcast_templates_enabled",         "true",
         "Enable the Broadcast Template Library — create and reuse message templates."),
        ("broadcast_automation_enabled",        "true",
         "Enable Broadcast Automation Rules — trigger broadcasts on events."),
        ("broadcast_ab_testing_enabled",        "true",
         "Enable A/B Testing for broadcast campaigns."),
        ("broadcast_recurring_campaigns_enabled", "true",
         "Enable recurring (daily/weekly/monthly/custom) campaigns."),
        # Limits
        ("broadcast_campaign_max_running",      "3",
         "Maximum number of campaigns that can run simultaneously."),
        ("broadcast_campaign_max_total",        "100",
         "Maximum total campaigns (excluding archived). 0 = unlimited."),
        ("broadcast_template_max",              "200",
         "Maximum number of saved templates. 0 = unlimited."),
        ("broadcast_automation_max_rules",      "50",
         "Maximum number of automation rules. 0 = unlimited."),
        ("broadcast_campaign_dedup_window_min", "60",
         "Minutes within which duplicate campaign triggers are suppressed."),
    ]
    for key, value, desc in seed:
        if not _key_exists(conn, key):
            conn.execute(
                sa.text("INSERT INTO bot_config (key, value, description) VALUES (:k, :v, :d)"),
                {"k": key, "v": value, "d": desc},
            )


def downgrade() -> None:
    for tbl in (
        "automation_trigger_logs",
        "broadcast_automation_rules",
        "campaign_executions",
        "broadcast_campaigns",
        "broadcast_templates",
    ):
        try:
            op.drop_table(tbl)
        except Exception:
            pass

    conn = op.get_bind()
    for key in (
        "broadcast_campaign_manager_status", "broadcast_campaigns_enabled",
        "broadcast_templates_enabled", "broadcast_automation_enabled",
        "broadcast_ab_testing_enabled", "broadcast_recurring_campaigns_enabled",
        "broadcast_campaign_max_running", "broadcast_campaign_max_total",
        "broadcast_template_max", "broadcast_automation_max_rules",
        "broadcast_campaign_dedup_window_min",
    ):
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
