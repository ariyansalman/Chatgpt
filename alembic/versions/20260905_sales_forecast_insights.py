"""Sales Forecast & Business Insights — V40

Adds:
  • business_reports          — stored generated reports
  • forecast_snapshots        — SMA forecast history
  • daily_analytics_snapshots — rolled-up daily metrics cache

Revision ID: 20260905_sales_forecast_insights
Revises:     20260904_exchange_rate_manager
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260905_sales_forecast_insights"
down_revision = "20260904_exchange_rate_manager"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS = [
    ("biz_analytics_status",      "str",   "enabled", "business_analytics",
     "📊 Business Analytics Status",
     "enabled = operational; maintenance = read-only; disabled = hidden."),
    ("biz_forecast_period_days",  "int",   30,        "business_analytics",
     "📊 Forecast Period (days)",
     "Number of past days used as baseline for sales forecasting."),
    ("biz_report_retention_days", "int",   90,        "business_analytics",
     "📊 Report Retention (days)",
     "How many days to keep generated reports. 0 = keep forever."),
    ("biz_auto_daily_report",     "bool",  False,     "business_analytics",
     "📊 Auto Daily Report",
     "Automatically generate a daily business report at midnight."),
    ("biz_auto_weekly_report",    "bool",  False,     "business_analytics",
     "📊 Auto Weekly Report",
     "Automatically generate a weekly business report on Mondays."),
    ("biz_auto_monthly_report",   "bool",  False,     "business_analytics",
     "📊 Auto Monthly Report",
     "Automatically generate a monthly business report on the 1st."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── business_reports ──────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS business_reports (
            id            SERIAL PRIMARY KEY,
            report_type   VARCHAR(32)  NOT NULL,
            period_start  TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            period_end    TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            title         VARCHAR(128) NOT NULL,
            summary_json  TEXT,
            notes         TEXT,
            generated_by  BIGINT,
            created_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("report_type", "ix_br_type"),
        ("created_at",  "ix_br_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON business_reports ({col})"
        ))

    # ── forecast_snapshots ────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS forecast_snapshots (
            id                    SERIAL PRIMARY KEY,
            period                VARCHAR(16)  NOT NULL,
            forecast_date         TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            predicted_revenue     FLOAT        NOT NULL DEFAULT 0.0,
            predicted_orders      INTEGER      NOT NULL DEFAULT 0,
            predicted_growth_pct  FLOAT,
            baseline_revenue      FLOAT,
            trend_direction       VARCHAR(16),
            confidence_pct        FLOAT,
            actual_revenue        FLOAT,
            actual_orders         INTEGER,
            model_version         VARCHAR(16)  NOT NULL DEFAULT 'v1_sma',
            created_at            TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("period",        "ix_fs_period"),
        ("forecast_date", "ix_fs_forecast_date"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON forecast_snapshots ({col})"
        ))

    # ── daily_analytics_snapshots ─────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS daily_analytics_snapshots (
            id                SERIAL PRIMARY KEY,
            snapshot_date     TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            revenue           FLOAT   NOT NULL DEFAULT 0.0,
            orders            INTEGER NOT NULL DEFAULT 0,
            new_users         INTEGER NOT NULL DEFAULT 0,
            active_users      INTEGER NOT NULL DEFAULT 0,
            avg_order_value   FLOAT,
            top_product_id    INTEGER,
            top_category_id   INTEGER,
            refund_amount     FLOAT   NOT NULL DEFAULT 0.0,
            gross_profit      FLOAT,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_das_date UNIQUE (snapshot_date)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_das_snapshot_date ON daily_analytics_snapshots (snapshot_date)"
    ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": str(val).lower() if isinstance(val, bool) else str(val),
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS daily_analytics_snapshots"))
    conn.execute(sa.text("DROP TABLE IF EXISTS forecast_snapshots"))
    conn.execute(sa.text("DROP TABLE IF EXISTS business_reports"))
    for row in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": row[0]})
