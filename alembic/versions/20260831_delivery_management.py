"""Delivery Management System — V36

Adds:
  • delivery_records    — comprehensive delivery log (all types / methods)
  • New bot_config keys for Delivery Management System settings

Revision ID: 20260831_delivery_management
Revises:     20260830_bulk_product_user
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260831_delivery_management"
down_revision = "20260830_bulk_product_user"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # ── Delivery Management System ────────────────────────────────────────────
    ("delivery_manager_status",         "str",  "enabled", "delivery_manager",
     "Delivery Manager Status",
     "enabled = fully operational; maintenance = read-only; disabled = off."),
    ("delivery_auto_enabled",           "bool", "true",    "delivery_manager",
     "Automatic Delivery",
     "When ON, orders are delivered automatically after payment confirmation."),
    ("delivery_manual_enabled",         "bool", "true",    "delivery_manager",
     "Manual Delivery",
     "When ON, admins can manually deliver, replace, or resend deliveries."),
    ("delivery_retry_enabled",          "bool", "true",    "delivery_manager",
     "Retry Failed Deliveries",
     "When ON, failed deliveries are automatically retried."),
    ("delivery_max_retries",            "int",  "3",       "delivery_manager",
     "Maximum Retry Count",
     "Maximum number of automatic retry attempts for a failed delivery."),
    ("delivery_retry_delay_seconds",    "int",  "300",     "delivery_manager",
     "Delivery Retry Delay (seconds)",
     "Seconds to wait between retry attempts for failed deliveries."),
    ("delivery_notifications_enabled",  "bool", "true",    "delivery_manager",
     "Delivery Notifications",
     "When ON, users receive a Telegram message when their order is delivered."),
    ("delivery_secure_links_enabled",   "bool", "true",    "delivery_manager",
     "Secure Download Links",
     "When ON, file deliveries use signed tokens instead of direct Telegram file_ids."),
    ("delivery_one_time_download",      "bool", "false",   "delivery_manager",
     "One-Time Downloads",
     "When ON, download links expire after the first successful download."),
    ("delivery_link_expiry_hours",      "int",  "24",      "delivery_manager",
     "Download Link Expiry (hours)",
     "Hours before a secure download link expires. 0 = no expiry."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── delivery_records ──────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS delivery_records (
            id                SERIAL PRIMARY KEY,
            secure_id         VARCHAR(36)  NOT NULL UNIQUE,
            order_id          INTEGER      REFERENCES orders(id) ON DELETE SET NULL,
            order_item_id     INTEGER      REFERENCES order_items(id) ON DELETE SET NULL,
            user_id           BIGINT       NOT NULL,
            product_id        INTEGER      REFERENCES products(id) ON DELETE SET NULL,
            delivery_type     VARCHAR(32)  NOT NULL,
            delivery_method   VARCHAR(16)  NOT NULL DEFAULT 'automatic',
            delivered_content TEXT,
            template_snapshot TEXT,
            status            VARCHAR(16)  NOT NULL DEFAULT 'pending',
            admin_id          BIGINT,
            admin_note        VARCHAR(500),
            retry_count       INTEGER      NOT NULL DEFAULT 0,
            max_retries       INTEGER      NOT NULL DEFAULT 3,
            last_error        VARCHAR(1000),
            download_token    VARCHAR(64)  UNIQUE,
            download_limit    INTEGER,
            download_count    INTEGER      NOT NULL DEFAULT 0,
            is_one_time       BOOLEAN      NOT NULL DEFAULT FALSE,
            link_expires_at   TIMESTAMP WITHOUT TIME ZONE,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            prepared_at       TIMESTAMP WITHOUT TIME ZONE,
            processed_at      TIMESTAMP WITHOUT TIME ZONE,
            delivered_at      TIMESTAMP WITHOUT TIME ZONE,
            completed_at      TIMESTAMP WITHOUT TIME ZONE,
            expires_at        TIMESTAMP WITHOUT TIME ZONE
        )
    """))

    for col_name, idx_name in [
        ("user_id",         "ix_dr_user_id"),
        ("order_id",        "ix_dr_order_id"),
        ("product_id",      "ix_dr_product_id"),
        ("status",          "ix_dr_status"),
        ("delivery_type",   "ix_dr_delivery_type"),
        ("delivery_method", "ix_dr_delivery_method"),
        ("download_token",  "ix_dr_download_token"),
        ("created_at",      "ix_dr_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON delivery_records ({col_name})"
        ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, type, value, category)
            VALUES (:key, :type, :value, :category)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val, "category": cat})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS delivery_records"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
