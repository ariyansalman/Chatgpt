"""V20: Advanced Features — Referral Dashboard, Support Enhancements,
Maintenance Advanced, Announcement System, Low Stock Advanced.

Revision ID: 20260810_advanced_features
Revises: 20260809_sales_marketing
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa

revision = "20260810_advanced_features"
down_revision = "20260809_sales_marketing"
branch_labels = None
depends_on = None


def upgrade():
    # ── Support Tickets: category, assigned admin, readable ticket number ──────
    op.execute("""
        ALTER TABLE support_tickets
            ADD COLUMN IF NOT EXISTS category VARCHAR(32) DEFAULT 'general',
            ADD COLUMN IF NOT EXISTS assigned_admin_id BIGINT DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS ticket_number VARCHAR(20) DEFAULT NULL
    """)

    # ── Ticket Messages: file/image attachment support ─────────────────────────
    op.execute("""
        ALTER TABLE ticket_messages
            ADD COLUMN IF NOT EXISTS file_id VARCHAR(256) DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS file_type VARCHAR(16) DEFAULT NULL
    """)

    # ── Low Stock Alert State: per-product thresholds + silent mode ────────────
    op.execute("""
        ALTER TABLE low_stock_alert_state
            ADD COLUMN IF NOT EXISTS silent_mode BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS custom_threshold INTEGER DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS fast_sell_alert_sent BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS fast_sell_sales_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS fast_sell_window_start TIMESTAMP DEFAULT NULL
    """)

    # ── Advanced Referral: click tracking ─────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS referral_clicks (
            id          SERIAL PRIMARY KEY,
            referrer_id INTEGER REFERENCES users(id),
            clicked_at  TIMESTAMP DEFAULT NOW(),
            ip_hash     VARCHAR(64)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rclick_referrer ON referral_clicks(referrer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rclick_clicked_at ON referral_clicks(clicked_at)"
    )

    # ── Advanced Referral: per-purchase commissions ────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS referral_commissions (
            id                SERIAL PRIMARY KEY,
            referrer_id       INTEGER REFERENCES users(id),
            referred_id       INTEGER REFERENCES users(id),
            order_id          INTEGER REFERENCES orders(id),
            order_amount      FLOAT NOT NULL DEFAULT 0,
            commission_rate   FLOAT NOT NULL DEFAULT 0,
            commission_amount FLOAT NOT NULL DEFAULT 0,
            status            VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMP DEFAULT NOW(),
            cleared_at        TIMESTAMP DEFAULT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rcm_referrer ON referral_commissions(referrer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rcm_status ON referral_commissions(status)"
    )

    # ── Advanced Referral: withdrawal requests ────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS referral_withdrawals (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER REFERENCES users(id),
            amount       FLOAT NOT NULL DEFAULT 0,
            status       VARCHAR(16) NOT NULL DEFAULT 'pending',
            admin_note   TEXT,
            created_at   TIMESTAMP DEFAULT NOW(),
            resolved_at  TIMESTAMP DEFAULT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rw_user_id ON referral_withdrawals(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rw_status ON referral_withdrawals(status)"
    )

    # ── Announcements ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id                  SERIAL PRIMARY KEY,
            title               VARCHAR(255) NOT NULL,
            content             TEXT         NOT NULL,
            target              VARCHAR(32)  NOT NULL DEFAULT 'all',
            target_user_ids     TEXT         DEFAULT NULL,
            is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
            is_pinned           BOOLEAN      NOT NULL DEFAULT FALSE,
            is_scheduled        BOOLEAN      NOT NULL DEFAULT FALSE,
            scheduled_at        TIMESTAMP    DEFAULT NULL,
            expires_at          TIMESTAMP    DEFAULT NULL,
            sent_count          INTEGER      DEFAULT 0,
            is_sent             BOOLEAN      NOT NULL DEFAULT FALSE,
            sent_at             TIMESTAMP    DEFAULT NULL,
            announcement_type   VARCHAR(16)  NOT NULL DEFAULT 'popup',
            created_by          BIGINT       DEFAULT NULL,
            created_at          TIMESTAMP    DEFAULT NOW(),
            updated_at          TIMESTAMP    DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ann_is_active ON announcements(is_active)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ann_scheduled_at ON announcements(scheduled_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ann_is_pinned ON announcements(is_pinned)"
    )

    # ── Announcement reads (which users have seen which announcements) ─────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS announcement_reads (
            id                  SERIAL PRIMARY KEY,
            announcement_id     INTEGER REFERENCES announcements(id) ON DELETE CASCADE,
            user_id             INTEGER REFERENCES users(id),
            read_at             TIMESTAMP DEFAULT NOW(),
            UNIQUE (announcement_id, user_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_annr_ann_id ON announcement_reads(announcement_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_annr_user_id ON announcement_reads(user_id)"
    )


def downgrade():
    # Non-destructive: don't drop tables/columns on downgrade.
    # All new tables and columns use IF NOT EXISTS guards.
    pass
