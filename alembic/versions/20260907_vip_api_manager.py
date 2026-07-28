"""V41: VIP Tier Manager + API Key & Integration Manager.

Adds:
  vip_tiers               — tier definitions with upgrade requirements & benefits
  user_vip_tiers          — current tier assignment per user (1 row / user)
  vip_tier_history        — audit trail of tier promotions / demotions
  loyalty_rewards         — reward catalog users redeem points for
  loyalty_reward_claims   — claim records per user
  api_integrations        — centralised API/integration registry
  api_connection_logs     — per-check health log

Also seeds default VIP tiers and adds bot_config keys.

Revision ID: 20260907_vip_api_manager
Revises:     20260906_auto_moderation_antispam
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260907_vip_api_manager"
down_revision = "20260906_auto_moderation_antispam"
branch_labels = None
depends_on = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _table_exists(bind, name: str) -> bool:
    if bind.dialect.name == "postgresql":
        r = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": name}).fetchone()
    else:
        r = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"
        ), {"t": name}).fetchone()
    return r is not None


def _col_exists(bind, table: str, col: str) -> bool:
    if bind.dialect.name == "postgresql":
        r = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ), {"t": table, "c": col}).fetchone()
    else:
        rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(row[1] == col for row in rows)
    return r is not None


# ── upgrade ───────────────────────────────────────────────────────────────────

def upgrade():
    bind = op.get_bind()

    # ── vip_tiers ─────────────────────────────────────────────────────────────
    if not _table_exists(bind, "vip_tiers"):
        op.create_table(
            "vip_tiers",
            sa.Column("id",                          sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("name",                         sa.String(64),   nullable=False),
            sa.Column("emoji",                        sa.String(8),    nullable=False, server_default="⭐"),
            sa.Column("level",                        sa.Integer(),    nullable=False),
            sa.Column("min_orders",                   sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("min_spending",                 sa.Float(),      nullable=False, server_default="0"),
            sa.Column("min_referral_earnings",        sa.Float(),      nullable=False, server_default="0"),
            sa.Column("min_account_age_days",         sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("discount_pct",                 sa.Float(),      nullable=False, server_default="0"),
            sa.Column("cashback_pct",                 sa.Float(),      nullable=False, server_default="0"),
            sa.Column("referral_bonus_pct",           sa.Float(),      nullable=False, server_default="0"),
            sa.Column("extra_coupon_discount_pct",    sa.Float(),      nullable=False, server_default="0"),
            sa.Column("priority_support",             sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("priority_delivery",            sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("exclusive_products",           sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("exclusive_flash_sales",        sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("withdrawal_limit_multiplier",  sa.Float(),      nullable=False, server_default="1"),
            sa.Column("wallet_limit_multiplier",      sa.Float(),      nullable=False, server_default="1"),
            sa.Column("custom_benefits",              sa.Text(),       nullable=True),
            sa.Column("is_active",                    sa.Boolean(),    nullable=False, server_default="true"),
            sa.Column("is_default",                   sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("created_at",  sa.DateTime(), nullable=True,  server_default=sa.func.now()),
            sa.Column("updated_at",  sa.DateTime(), nullable=True,  server_default=sa.func.now()),
            sa.UniqueConstraint("level", name="uq_vt_level"),
        )
        op.create_index("ix_vip_tiers_level",     "vip_tiers", ["level"])
        op.create_index("ix_vip_tiers_is_active",  "vip_tiers", ["is_active"])
        logger.info("vip_api_manager: created vip_tiers")

        # Seed default tiers
        bind.execute(sa.text("""
            INSERT INTO vip_tiers
                (name, emoji, level, min_orders, min_spending, is_active, is_default)
            VALUES
                ('Bronze',   '🥉', 0, 0,   0,    true, true),
                ('Silver',   '🥈', 1, 5,   50,   true, false),
                ('Gold',     '🥇', 2, 20,  250,  true, false),
                ('Platinum', '💎', 3, 50,  1000, true, false),
                ('Diamond',  '👑', 4, 100, 5000, true, false),
                ('Elite',    '⭐', 5, 250, 15000,true, false)
            ON CONFLICT (level) DO NOTHING
        """))
        logger.info("vip_api_manager: seeded 6 default VIP tiers")
    else:
        logger.info("vip_api_manager: vip_tiers already exists — skipping")

    # ── user_vip_tiers ────────────────────────────────────────────────────────
    if not _table_exists(bind, "user_vip_tiers"):
        op.create_table(
            "user_vip_tiers",
            sa.Column("id",          sa.Integer(),   primary_key=True, nullable=False),
            sa.Column("user_id",     sa.Integer(),   sa.ForeignKey("users.id"), nullable=False),
            sa.Column("tier_id",     sa.Integer(),   sa.ForeignKey("vip_tiers.id"), nullable=False),
            sa.Column("assigned_at", sa.DateTime(),  nullable=False, server_default=sa.func.now()),
            sa.Column("assigned_by", sa.BigInteger(), nullable=True),
            sa.Column("reason",      sa.String(255), nullable=True),
            sa.UniqueConstraint("user_id", name="uq_uvt_user"),
        )
        op.create_index("ix_user_vip_tiers_user_id", "user_vip_tiers", ["user_id"])
        op.create_index("ix_user_vip_tiers_tier_id", "user_vip_tiers", ["tier_id"])
        logger.info("vip_api_manager: created user_vip_tiers")
    else:
        logger.info("vip_api_manager: user_vip_tiers already exists — skipping")

    # ── vip_tier_history ──────────────────────────────────────────────────────
    if not _table_exists(bind, "vip_tier_history"):
        op.create_table(
            "vip_tier_history",
            sa.Column("id",          sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("user_id",     sa.Integer(),    sa.ForeignKey("users.id"), nullable=False),
            sa.Column("old_tier_id", sa.Integer(),    sa.ForeignKey("vip_tiers.id"), nullable=True),
            sa.Column("new_tier_id", sa.Integer(),    sa.ForeignKey("vip_tiers.id"), nullable=False),
            sa.Column("reason",      sa.String(255),  nullable=True),
            sa.Column("changed_by",  sa.BigInteger(), nullable=True),
            sa.Column("created_at",  sa.DateTime(),   nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_vip_tier_history_user_id",    "vip_tier_history", ["user_id"])
        op.create_index("ix_vip_tier_history_created_at", "vip_tier_history", ["created_at"])
        logger.info("vip_api_manager: created vip_tier_history")
    else:
        logger.info("vip_api_manager: vip_tier_history already exists — skipping")

    # ── loyalty_rewards ───────────────────────────────────────────────────────
    if not _table_exists(bind, "loyalty_rewards"):
        op.create_table(
            "loyalty_rewards",
            sa.Column("id",                 sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("name",               sa.String(128),  nullable=False),
            sa.Column("description",        sa.Text(),       nullable=True),
            sa.Column("reward_type",        sa.String(32),   nullable=False, server_default="wallet"),
            sa.Column("points_cost",        sa.Integer(),    nullable=False, server_default="100"),
            sa.Column("value",              sa.Float(),      nullable=False, server_default="1"),
            sa.Column("min_tier_level",     sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("max_claims_per_user",sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("max_total_claims",   sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("total_claims",       sa.Integer(),    nullable=False, server_default="0"),
            sa.Column("is_active",          sa.Boolean(),    nullable=False, server_default="true"),
            sa.Column("expires_at",         sa.DateTime(),   nullable=True),
            sa.Column("created_at",         sa.DateTime(),   nullable=True,  server_default=sa.func.now()),
            sa.Column("updated_at",         sa.DateTime(),   nullable=True,  server_default=sa.func.now()),
        )
        op.create_index("ix_loyalty_rewards_is_active", "loyalty_rewards", ["is_active"])
        logger.info("vip_api_manager: created loyalty_rewards")
    else:
        logger.info("vip_api_manager: loyalty_rewards already exists — skipping")

    # ── loyalty_reward_claims ─────────────────────────────────────────────────
    if not _table_exists(bind, "loyalty_reward_claims"):
        op.create_table(
            "loyalty_reward_claims",
            sa.Column("id",             sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id",        sa.Integer(), sa.ForeignKey("users.id"),          nullable=False),
            sa.Column("reward_id",      sa.Integer(), sa.ForeignKey("loyalty_rewards.id"), nullable=False),
            sa.Column("points_spent",   sa.Integer(), nullable=False),
            sa.Column("value_received", sa.Float(),   nullable=False),
            sa.Column("created_at",     sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_loyalty_reward_claims_user_id",    "loyalty_reward_claims", ["user_id"])
        op.create_index("ix_loyalty_reward_claims_reward_id",  "loyalty_reward_claims", ["reward_id"])
        op.create_index("ix_loyalty_reward_claims_created_at", "loyalty_reward_claims", ["created_at"])
        logger.info("vip_api_manager: created loyalty_reward_claims")
    else:
        logger.info("vip_api_manager: loyalty_reward_claims already exists — skipping")

    # ── api_integrations ──────────────────────────────────────────────────────
    if not _table_exists(bind, "api_integrations"):
        op.create_table(
            "api_integrations",
            sa.Column("id",                  sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("name",                sa.String(128),  nullable=False),
            sa.Column("provider",            sa.String(64),   nullable=False),
            sa.Column("api_type",            sa.String(32),   nullable=False, server_default="custom"),
            sa.Column("api_key_masked",      sa.String(512),  nullable=True),
            sa.Column("api_key_hint",        sa.String(8),    nullable=True),
            sa.Column("api_secret_masked",   sa.String(512),  nullable=True),
            sa.Column("api_secret_hint",     sa.String(8),    nullable=True),
            sa.Column("webhook_url",         sa.String(512),  nullable=True),
            sa.Column("base_url",            sa.String(512),  nullable=True),
            sa.Column("extra_config",        sa.Text(),       nullable=True),
            sa.Column("status",              sa.String(16),   nullable=False, server_default="enabled"),
            sa.Column("connection_status",   sa.String(16),   nullable=False, server_default="unknown"),
            sa.Column("response_time_ms",    sa.Integer(),    nullable=True),
            sa.Column("last_check_at",       sa.DateTime(),   nullable=True),
            sa.Column("last_success_at",     sa.DateTime(),   nullable=True),
            sa.Column("last_error_at",       sa.DateTime(),   nullable=True),
            sa.Column("last_error_message",  sa.Text(),       nullable=True),
            sa.Column("version",             sa.String(32),   nullable=True),
            sa.Column("is_built_in",         sa.Boolean(),    nullable=False, server_default="false"),
            sa.Column("is_active",           sa.Boolean(),    nullable=False, server_default="true"),
            sa.Column("created_at",          sa.DateTime(),   nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at",          sa.DateTime(),   nullable=True, server_default=sa.func.now()),
        )
        op.create_index("ix_api_integrations_api_type",         "api_integrations", ["api_type"])
        op.create_index("ix_api_integrations_status",           "api_integrations", ["status"])
        op.create_index("ix_api_integrations_connection_status","api_integrations", ["connection_status"])
        op.create_index("ix_api_integrations_is_active",        "api_integrations", ["is_active"])
        logger.info("vip_api_manager: created api_integrations")
    else:
        logger.info("vip_api_manager: api_integrations already exists — skipping")

    # ── api_connection_logs ───────────────────────────────────────────────────
    if not _table_exists(bind, "api_connection_logs"):
        op.create_table(
            "api_connection_logs",
            sa.Column("id",               sa.Integer(),  primary_key=True, nullable=False),
            sa.Column("integration_id",   sa.Integer(),
                      sa.ForeignKey("api_integrations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status",           sa.String(16), nullable=False),
            sa.Column("response_time_ms", sa.Integer(),  nullable=True),
            sa.Column("http_status",      sa.Integer(),  nullable=True),
            sa.Column("error_message",    sa.Text(),     nullable=True),
            sa.Column("checked_at",       sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_api_connection_logs_integration_id", "api_connection_logs", ["integration_id"])
        op.create_index("ix_api_connection_logs_status",         "api_connection_logs", ["status"])
        op.create_index("ix_api_connection_logs_checked_at",     "api_connection_logs", ["checked_at"])
        logger.info("vip_api_manager: created api_connection_logs")
    else:
        logger.info("vip_api_manager: api_connection_logs already exists — skipping")

    # ── bot_config rows ───────────────────────────────────────────────────────
    new_keys = [
        # VIP
        ("vip_status",              "enabled",  "vip",  "🏆 VIP System Status",
         "enabled | maintenance | disabled"),
        ("vip_auto_upgrade",        "true",     "vip",  "🏆 Auto Upgrade",
         "Automatically upgrade users when they meet tier requirements."),
        ("vip_auto_downgrade",      "false",    "vip",  "🏆 Auto Downgrade",
         "Automatically downgrade users when they no longer meet tier requirements."),
        ("vip_points_expiration_days","0",      "vip",  "🏆 Points Expiration (days)",
         "0 = never expire. >0 = expire points older than N days."),
        ("vip_cashback_enabled",    "true",     "vip",  "🏆 Cashback Enabled",
         "Apply cashback from VIP tier on completed orders."),
        ("vip_referral_bonus_enabled","true",   "vip",  "🏆 Referral Bonus Enabled",
         "Apply extra referral bonus from VIP tier."),
        ("vip_reward_limit_per_day","0",        "vip",  "🏆 Reward Claim Limit/Day",
         "0 = unlimited. >0 = max reward claims per user per day."),
        # API Manager
        ("aim_status",              "enabled",  "api_manager", "🔑 API Manager Status",
         "enabled | maintenance | disabled"),
        ("aim_auto_health_check",   "true",     "api_manager", "🔑 Auto Health Check",
         "Automatically check API health at the configured interval."),
        ("aim_auto_retry",          "true",     "api_manager", "🔑 Auto Retry on Failure",
         "Automatically retry failed connections."),
        ("aim_retry_count",         "3",        "api_manager", "🔑 Retry Count",
         "Number of retries before marking an integration offline."),
        ("aim_timeout_seconds",     "10",       "api_manager", "🔑 Request Timeout (s)",
         "Timeout in seconds for health check HTTP requests."),
        ("aim_health_check_interval_minutes","15","api_manager","🔑 Health Check Interval (min)",
         "How often to run the background health check job."),
        ("aim_log_retention_days",  "30",       "api_manager", "🔑 Log Retention (days)",
         "How many days to keep connection log rows."),
    ]
    for key, default_val, category, label, description in new_keys:
        try:
            bind.execute(sa.text(
                "INSERT INTO bot_config (key, value, value_type, category, label, description) "
                "VALUES (:k, :v, 'str', :cat, :lab, :desc) "
                "ON CONFLICT (key) DO NOTHING"
            ), {"k": key, "v": default_val, "cat": category, "lab": label, "desc": description})
        except Exception as exc:
            logger.warning("vip_api_manager: bot_config insert %s skipped: %s", key, exc)

    logger.info("vip_api_manager: migration complete")


def downgrade():
    bind = op.get_bind()
    for tbl in ("api_connection_logs", "api_integrations",
                "loyalty_reward_claims", "loyalty_rewards",
                "vip_tier_history", "user_vip_tiers", "vip_tiers"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
    logger.info("vip_api_manager: downgrade complete")
