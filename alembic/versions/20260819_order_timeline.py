"""V25 — Order Timeline: seed BotConfig keys.

Revision ID: 20260819_order_timeline
Revises: 20260818_supplier_auto_assign

The OrderStatusHistory table was created in an earlier migration
(20260705_premium_core). This migration only seeds BotConfig keys for
the new admin-configurable Order Timeline feature.

BotConfig rows seeded (idempotent — skipped if key already present):
    ots_status                  str   "enabled"
    ots_show_to_users           bool  True
    ots_show_processing_time    bool  True
    ots_show_estimated_delivery bool  False
    ots_allow_manual_status     bool  True
    ots_notify_users            bool  True
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260819_order_timeline"
down_revision = "20260818_supplier_auto_assign"
branch_labels = None
depends_on    = None

_NEW_KEYS = [
    ("ots_status",                  "str",  "enabled"),
    ("ots_show_to_users",           "bool", "True"),
    ("ots_show_processing_time",    "bool", "True"),
    ("ots_show_estimated_delivery", "bool", "False"),
    ("ots_allow_manual_status",     "bool", "True"),
    ("ots_notify_users",            "bool", "True"),
]


def upgrade():
    bind = op.get_bind()
    for key, vtype, default in _NEW_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"),
            {"k": key},
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category) "
                    "VALUES (:k, :vt, :v, 'ops')"
                ),
                {"k": key, "vt": vtype, "v": default},
            )
            logger.info("order_timeline: seeded BotConfig key %r", key)
        else:
            logger.info("order_timeline: BotConfig key %r already exists — skip", key)


def downgrade():
    bind = op.get_bind()
    for key, _, _ in _NEW_KEYS:
        bind.execute(
            sa.text("DELETE FROM bot_config WHERE key = :k"),
            {"k": key},
        )
        logger.info("order_timeline: removed BotConfig key %r", key)
