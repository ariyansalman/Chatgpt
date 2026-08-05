"""Flash Sale Manager — V38

Adds:
  • flash_sale_events           — enhanced flash sales (multi-product, categories, broadcasts)
  • flash_sale_price_snapshots  — original prices saved before a sale starts
  • flash_sale_broadcast_logs   — per-sale, per-type broadcast deduplication
  • New bot_config keys for the Flash Sale Manager

Revision ID: 20260902_flash_sale_manager
Revises:     20260901_notification_center
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260902_flash_sale_manager"
down_revision = "20260901_notification_center"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # ── Flash Sale Manager ────────────────────────────────────────────────────
    ("flash_sale_manager_status",  "str",  "enabled",  "flash_sale_manager",
     "Flash Sale Manager Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("fsm_auto_price_update",      "bool", "true",     "flash_sale_manager",
     "Auto Price Update",
     "Automatically apply sale prices when a sale starts and restore them when it ends."),
    ("fsm_auto_broadcast",         "bool", "true",     "flash_sale_manager",
     "Auto Broadcast",
     "Send scheduled broadcast messages at configured intervals before/after each sale."),
    ("fsm_countdown_timer",        "bool", "true",     "flash_sale_manager",
     "Show Countdown Timer",
     "Display live countdown on product pages and flash sale messages."),
    ("fsm_homepage_banner",        "bool", "true",     "flash_sale_manager",
     "Homepage Banner",
     "Show active flash sale banner on the store home page."),
    ("fsm_product_badge",          "bool", "true",     "flash_sale_manager",
     "Product Page Badge",
     "Display ⚡ Flash Sale badge on affected product pages."),
    ("fsm_stack_discounts",        "bool", "false",    "flash_sale_manager",
     "Stack Discounts",
     "Allow flash sale discounts to stack with coupon discounts."),
    ("fsm_allow_multiple_sales",   "bool", "false",    "flash_sale_manager",
     "Allow Multiple Active Sales per Product",
     "When ON, a product can be in multiple active flash sales simultaneously."),
    ("fsm_default_message_template", "str",
     "⚡ <b>FLASH SALE</b>\n\n🔥 <b>Limited Time Offer!</b>\n\n"
     "📦 <b>{product_name}</b>\n\n"
     "Price: <s>${old_price}</s> → <b>${sale_price}</b>\n"
     "🎁 Save: <b>{discount_percent}%</b>\n\n"
     "⏰ Ends in: <b>{countdown}</b>\n\n"
     "Tap below to buy instantly.",
     "flash_sale_manager",
     "Default Broadcast Message Template",
     "Template for flash sale broadcast messages. Supports {product_name}, "
     "{old_price}, {sale_price}, {discount_percent}, {countdown}, {badge}."),
    ("fsm_broadcast_24h",          "bool", "true",     "flash_sale_manager",
     "Broadcast: 24 Hours Remaining",   ""),
    ("fsm_broadcast_12h",          "bool", "false",    "flash_sale_manager",
     "Broadcast: 12 Hours Remaining",   ""),
    ("fsm_broadcast_6h",           "bool", "false",    "flash_sale_manager",
     "Broadcast: 6 Hours Remaining",    ""),
    ("fsm_broadcast_3h",           "bool", "false",    "flash_sale_manager",
     "Broadcast: 3 Hours Remaining",    ""),
    ("fsm_broadcast_1h",           "bool", "true",     "flash_sale_manager",
     "Broadcast: 1 Hour Remaining",     ""),
    ("fsm_broadcast_30m",          "bool", "false",    "flash_sale_manager",
     "Broadcast: 30 Minutes Remaining", ""),
    ("fsm_broadcast_10m",          "bool", "false",    "flash_sale_manager",
     "Broadcast: 10 Minutes Remaining", ""),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── flash_sale_events ─────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS flash_sale_events (
            id                  SERIAL PRIMARY KEY,
            name                VARCHAR(255) NOT NULL,
            description         TEXT,
            banner_file_id      VARCHAR(256),
            badge_text          VARCHAR(64),
            scope_type          VARCHAR(32)  NOT NULL DEFAULT 'single_product',
            product_ids_json    TEXT,
            category_ids_json   TEXT,
            discount_percent    FLOAT,
            fixed_sale_price    FLOAT,
            start_time          TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            end_time            TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            timezone            VARCHAR(64)  NOT NULL DEFAULT 'UTC',
            priority            INTEGER      NOT NULL DEFAULT 0,
            status              VARCHAR(16)  NOT NULL DEFAULT 'draft',
            is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
            broadcast_on_start  BOOLEAN      NOT NULL DEFAULT TRUE,
            broadcast_on_end    BOOLEAN      NOT NULL DEFAULT FALSE,
            broadcast_24h       BOOLEAN      NOT NULL DEFAULT TRUE,
            broadcast_12h       BOOLEAN      NOT NULL DEFAULT FALSE,
            broadcast_6h        BOOLEAN      NOT NULL DEFAULT FALSE,
            broadcast_3h        BOOLEAN      NOT NULL DEFAULT FALSE,
            broadcast_1h        BOOLEAN      NOT NULL DEFAULT TRUE,
            broadcast_30m       BOOLEAN      NOT NULL DEFAULT FALSE,
            broadcast_10m       BOOLEAN      NOT NULL DEFAULT FALSE,
            message_template    TEXT,
            show_on_homepage    BOOLEAN      NOT NULL DEFAULT TRUE,
            homepage_priority   INTEGER      NOT NULL DEFAULT 0,
            view_count          INTEGER      NOT NULL DEFAULT 0,
            click_count         INTEGER      NOT NULL DEFAULT 0,
            order_count         INTEGER      NOT NULL DEFAULT 0,
            revenue             FLOAT        NOT NULL DEFAULT 0,
            created_by          BIGINT,
            created_at          TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("scope_type", "ix_fse_scope_type"),
        ("start_time", "ix_fse_start_time"),
        ("end_time",   "ix_fse_end_time"),
        ("status",     "ix_fse_status"),
        ("is_active",  "ix_fse_is_active"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON flash_sale_events ({col})"
        ))

    # ── flash_sale_price_snapshots ────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS flash_sale_price_snapshots (
            id                   SERIAL PRIMARY KEY,
            flash_sale_event_id  INTEGER NOT NULL REFERENCES flash_sale_events(id) ON DELETE CASCADE,
            product_id           INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            original_price       FLOAT NOT NULL,
            original_sale_price  FLOAT,
            applied_sale_price   FLOAT NOT NULL,
            created_at           TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            UNIQUE (flash_sale_event_id, product_id)
        )
    """))
    for col, idx in [
        ("flash_sale_event_id", "ix_fsps_fse_id"),
        ("product_id",          "ix_fsps_product_id"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON flash_sale_price_snapshots ({col})"
        ))

    # ── flash_sale_broadcast_logs ─────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS flash_sale_broadcast_logs (
            id                   SERIAL PRIMARY KEY,
            flash_sale_event_id  INTEGER NOT NULL REFERENCES flash_sale_events(id) ON DELETE CASCADE,
            broadcast_type       VARCHAR(8) NOT NULL,
            sent_at              TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            recipients           INTEGER NOT NULL DEFAULT 0,
            error_message        TEXT,
            UNIQUE (flash_sale_event_id, broadcast_type)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_fsbl_fse_id "
        "ON flash_sale_broadcast_logs (flash_sale_event_id)"
    ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for row in _NEW_CONFIG_KEYS:
        key, typ, val, cat, label, desc = row
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val,
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS flash_sale_broadcast_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS flash_sale_price_snapshots"))
    conn.execute(sa.text("DROP TABLE IF EXISTS flash_sale_events"))
    for row in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": row[0]})
