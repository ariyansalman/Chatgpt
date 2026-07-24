"""Notification Center & File/License Manager — V37

Adds:
  • admin_notifications         — centralized notification records
  • managed_files               — digital file manager
  • managed_keys                — license/product key manager
  • managed_key_deliveries      — key delivery log
  • file_download_logs          — file download tracking
  • New bot_config keys for both features

Revision ID: 20260901_notification_center
Revises:     20260831_delivery_management
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260901_notification_center"
down_revision = "20260831_delivery_management"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS: list[tuple] = [
    # ── Notification Center ───────────────────────────────────────────────────
    ("notification_center_status",       "str",  "enabled", "notification_center",
     "Notification Center Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("notification_center_sound",        "bool", "true",    "notification_center",
     "Enable Sound Notifications",
     "When ON, push notifications include sound."),
    ("notification_center_silent_mode",  "bool", "false",   "notification_center",
     "Silent Mode",
     "When ON, all notifications are stored silently — no Telegram messages sent."),
    ("notification_center_max",          "int",  "1000",    "notification_center",
     "Maximum Stored Notifications",
     "Maximum number of notifications to keep. Oldest are auto-deleted when exceeded."),
    ("notification_center_auto_delete",  "bool", "false",   "notification_center",
     "Auto Delete Old Notifications",
     "When ON, notifications older than the retention period are automatically deleted."),
    ("notification_center_retention_days", "int","30",      "notification_center",
     "Notification Retention (days)",
     "Notifications older than this many days are auto-deleted when auto-delete is on."),
    # Per-event enable keys
    ("notif_nc_new_user",            "bool", "true",  "notification_center", "Notify: New User Registration", ""),
    ("notif_nc_new_order",           "bool", "true",  "notification_center", "Notify: New Order", ""),
    ("notif_nc_payment_success",     "bool", "true",  "notification_center", "Notify: Successful Payment", ""),
    ("notif_nc_payment_failed",      "bool", "true",  "notification_center", "Notify: Failed Payment", ""),
    ("notif_nc_payment_pending",     "bool", "true",  "notification_center", "Notify: Pending Payment", ""),
    ("notif_nc_deposit",             "bool", "true",  "notification_center", "Notify: Deposit Received", ""),
    ("notif_nc_withdrawal_request",  "bool", "true",  "notification_center", "Notify: Withdrawal Request", ""),
    ("notif_nc_withdrawal_approved", "bool", "true",  "notification_center", "Notify: Withdrawal Approved", ""),
    ("notif_nc_withdrawal_rejected", "bool", "true",  "notification_center", "Notify: Withdrawal Rejected", ""),
    ("notif_nc_product_delivered",   "bool", "true",  "notification_center", "Notify: Product Delivered", ""),
    ("notif_nc_refund_request",      "bool", "true",  "notification_center", "Notify: Refund Request", ""),
    ("notif_nc_support_ticket",      "bool", "true",  "notification_center", "Notify: Support Ticket", ""),
    ("notif_nc_low_stock",           "bool", "true",  "notification_center", "Notify: Low Stock Alert", ""),
    ("notif_nc_out_of_stock",        "bool", "true",  "notification_center", "Notify: Product Out Of Stock", ""),
    ("notif_nc_coupon_used",         "bool", "true",  "notification_center", "Notify: Coupon Used", ""),
    ("notif_nc_referral_reward",     "bool", "true",  "notification_center", "Notify: Referral Reward", ""),
    ("notif_nc_broadcast_done",      "bool", "true",  "notification_center", "Notify: Broadcast Completed", ""),
    ("notif_nc_fraud_alert",         "bool", "true",  "notification_center", "Notify: Fraud Detection Alert", ""),
    ("notif_nc_api_failure",         "bool", "true",  "notification_center", "Notify: API Failure", ""),
    ("notif_nc_webhook_failure",     "bool", "true",  "notification_center", "Notify: Webhook Failure", ""),
    ("notif_nc_db_error",            "bool", "true",  "notification_center", "Notify: Database Error", ""),
    ("notif_nc_tg_api_error",        "bool", "true",  "notification_center", "Notify: Telegram API Error", ""),
    ("notif_nc_system_warning",      "bool", "true",  "notification_center", "Notify: System Warning", ""),
    # ── File & License Manager ───────────────────────────────────────────────
    ("file_license_manager_status",      "str",  "enabled", "file_license_manager",
     "File & License Manager Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("flm_max_upload_size_mb",           "int",  "50",      "file_license_manager",
     "Maximum Upload Size (MB)",
     "Maximum file size allowed for upload."),
    ("flm_allowed_types",                "str",  "pdf,zip,rar,txt,docx,image,video,software",
     "file_license_manager",
     "Allowed File Types",
     "Comma-separated list of allowed file types."),
    ("flm_auto_delete_expired",          "bool", "false",   "file_license_manager",
     "Auto Delete Expired Files",
     "When ON, files past their expiry date are automatically archived."),
    ("flm_auto_archive_used_keys",       "bool", "true",    "file_license_manager",
     "Auto Archive Used Keys",
     "When ON, keys are automatically marked as archived after delivery."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── admin_notifications ───────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id                SERIAL PRIMARY KEY,
            event_type        VARCHAR(64)   NOT NULL,
            category          VARCHAR(32)   NOT NULL DEFAULT 'system',
            severity          VARCHAR(16)   NOT NULL DEFAULT 'push',
            title             VARCHAR(255)  NOT NULL,
            body              TEXT          NOT NULL,
            source_type       VARCHAR(32),
            source_id         VARCHAR(64),
            is_read           BOOLEAN       NOT NULL DEFAULT FALSE,
            is_pinned         BOOLEAN       NOT NULL DEFAULT FALSE,
            is_archived       BOOLEAN       NOT NULL DEFAULT FALSE,
            admin_telegram_id BIGINT,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            read_at           TIMESTAMP WITHOUT TIME ZONE,
            archived_at       TIMESTAMP WITHOUT TIME ZONE
        )
    """))
    for col, idx in [
        ("event_type",        "ix_an_event_type"),
        ("category",          "ix_an_category"),
        ("severity",          "ix_an_severity"),
        ("is_read",           "ix_an_is_read"),
        ("is_pinned",         "ix_an_is_pinned"),
        ("is_archived",       "ix_an_is_archived"),
        ("admin_telegram_id", "ix_an_admin_telegram_id"),
        ("created_at",        "ix_an_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON admin_notifications ({col})"
        ))

    # ── managed_files ─────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS managed_files (
            id                SERIAL PRIMARY KEY,
            filename          VARCHAR(255) NOT NULL,
            description       TEXT,
            file_type         VARCHAR(16)  NOT NULL DEFAULT 'other',
            telegram_file_id  VARCHAR(256),
            file_size         BIGINT,
            product_id        INTEGER REFERENCES products(id) ON DELETE SET NULL,
            status            VARCHAR(16)  NOT NULL DEFAULT 'active',
            max_downloads     INTEGER,
            download_count    INTEGER      NOT NULL DEFAULT 0,
            auto_delete_days  INTEGER,
            expires_at        TIMESTAMP WITHOUT TIME ZONE,
            created_by        BIGINT,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("file_type",  "ix_mf_file_type"),
        ("status",     "ix_mf_status"),
        ("product_id", "ix_mf_product_id"),
        ("created_at", "ix_mf_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON managed_files ({col})"
        ))

    # ── managed_keys ──────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS managed_keys (
            id                SERIAL PRIMARY KEY,
            key_type          VARCHAR(32)  NOT NULL,
            key_value         TEXT         NOT NULL,
            key_fingerprint   VARCHAR(64)  UNIQUE,
            product_id        INTEGER REFERENCES products(id) ON DELETE SET NULL,
            status            VARCHAR(16)  NOT NULL DEFAULT 'unused',
            reserved_by       BIGINT,
            reserved_at       TIMESTAMP WITHOUT TIME ZONE,
            used_by_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            used_at           TIMESTAMP WITHOUT TIME ZONE,
            order_id          INTEGER REFERENCES orders(id) ON DELETE SET NULL,
            notes             TEXT,
            created_by        BIGINT,
            created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at        TIMESTAMP WITHOUT TIME ZONE
        )
    """))
    for col, idx in [
        ("key_type",        "ix_mk_key_type"),
        ("key_fingerprint", "ix_mk_key_fingerprint"),
        ("status",          "ix_mk_status"),
        ("product_id",      "ix_mk_product_id"),
        ("used_by_user_id", "ix_mk_used_by_user_id"),
        ("created_at",      "ix_mk_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON managed_keys ({col})"
        ))

    # ── managed_key_deliveries ────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS managed_key_deliveries (
            id              SERIAL PRIMARY KEY,
            key_id          INTEGER NOT NULL REFERENCES managed_keys(id) ON DELETE CASCADE,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            order_id        INTEGER REFERENCES orders(id) ON DELETE SET NULL,
            delivered_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            delivery_method VARCHAR(16) NOT NULL DEFAULT 'automatic',
            admin_id        BIGINT
        )
    """))
    for col, idx in [
        ("key_id",      "ix_mkd_key_id"),
        ("user_id",     "ix_mkd_user_id"),
        ("order_id",    "ix_mkd_order_id"),
        ("delivered_at","ix_mkd_delivered_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON managed_key_deliveries ({col})"
        ))

    # ── file_download_logs ────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS file_download_logs (
            id            SERIAL PRIMARY KEY,
            file_id       INTEGER NOT NULL REFERENCES managed_files(id) ON DELETE CASCADE,
            user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            order_id      INTEGER REFERENCES orders(id) ON DELETE SET NULL,
            downloaded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("file_id",       "ix_fdl_file_id"),
        ("user_id",       "ix_fdl_user_id"),
        ("downloaded_at", "ix_fdl_downloaded_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON file_download_logs ({col})"
        ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val,
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS file_download_logs"))
    conn.execute(sa.text("DROP TABLE IF EXISTS managed_key_deliveries"))
    conn.execute(sa.text("DROP TABLE IF EXISTS managed_keys"))
    conn.execute(sa.text("DROP TABLE IF EXISTS managed_files"))
    conn.execute(sa.text("DROP TABLE IF EXISTS admin_notifications"))
    for key, *_ in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": key})
