"""inventory_reservation: Add admin config rows for IRS — V23.

Revision ID: 20260817_inventory_reservation
Revises: 20260816_price_history

The StockReservation table already exists from an earlier migration.
This migration only ensures the new BotConfig keys exist in the database.

BotConfig rows seeded (idempotent — skipped if key already present):
    irs_enabled                — bool  True
    irs_status                 — str   "enabled"
    irs_allow_manual_release   — bool  True
    irs_max_per_user           — int   1
    irs_auto_release           — bool  True

``inventory_reservation_ttl_minutes`` already exists in the default
DEFAULTS list — no migration row needed for it.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260817_inventory_reservation"
down_revision = "20260816_price_history"
branch_labels = None
depends_on    = None

_NEW_KEYS = [
    ("irs_enabled",              "bool", "True"),
    ("irs_status",               "str",  "enabled"),
    ("irs_allow_manual_release", "bool", "True"),
    ("irs_max_per_user",         "int",  "1"),
    ("irs_auto_release",         "bool", "True"),
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
                    "VALUES (:k, :vt, :v, 'inventory')"
                ),
                {"k": key, "vt": vtype, "v": default},
            )
            logger.info("inventory_reservation: seeded BotConfig key %r", key)
        else:
            logger.info("inventory_reservation: BotConfig key %r already exists — skip", key)


def downgrade():
    bind = op.get_bind()
    for key, _, _ in _NEW_KEYS:
        bind.execute(
            sa.text("DELETE FROM bot_config WHERE key = :k"),
            {"k": key},
        )
        logger.info("inventory_reservation: removed BotConfig key %r", key)
