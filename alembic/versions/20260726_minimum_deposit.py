"""minimum_deposit: add minimum_deposit_enabled bot_config key.

Revision ID: 20260726_minimum_deposit
Revises: 20260725_binance_bybit_complete

Adds the ``minimum_deposit_enabled`` key to the ``bot_configs`` table
(if not already present) so the admin panel can toggle the global minimum
deposit check on/off independently of the amount stored in ``topup_min_amount``.

Also updates the label/description of ``topup_min_amount`` to reflect its
new role as the configurable minimum deposit amount.

Both operations are fully idempotent — safe to run multiple times.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260726_minimum_deposit"
down_revision = "20260725_binance_bybit_complete"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # Ensure bot_configs table exists (it always should by this point,
    # but guard defensively so the migration doesn't error on a fresh DB).
    from sqlalchemy import inspect as sa_inspect
    if not sa_inspect(bind).has_table("bot_configs"):
        logger.warning("bot_configs table not found — skipping minimum_deposit migration.")
        return

    # ── Insert minimum_deposit_enabled if not present ─────────────────────
    result = bind.execute(
        sa.text("SELECT COUNT(*) FROM bot_configs WHERE key = 'minimum_deposit_enabled'")
    )
    if result.scalar() == 0:
        bind.execute(
            sa.text(
                "INSERT INTO bot_configs (key, value, value_type, category, label, description) "
                "VALUES (:key, :value, :vtype, :cat, :label, :desc)"
            ),
            {
                "key": "minimum_deposit_enabled",
                "value": "false",
                "vtype": "bool",
                "cat": "payments",
                "label": "Global Minimum Deposit: Enabled",
                "desc": (
                    "If ON, users cannot deposit less than the configured minimum amount. "
                    "If OFF, any positive amount is accepted across all payment gateways."
                ),
            },
        )
        logger.info("Inserted bot_config key: minimum_deposit_enabled (default: false)")

    # ── Update label/description of topup_min_amount ───────────────────────
    bind.execute(
        sa.text(
            "UPDATE bot_configs SET label = :label, description = :desc "
            "WHERE key = 'topup_min_amount'"
        ),
        {
            "label": "Global Minimum Deposit Amount (USD)",
            "desc": (
                "The minimum deposit amount enforced when 'Minimum Deposit: Enabled' is ON. "
                "Ignored when the toggle is OFF. Applies to all payment gateways."
            ),
        },
    )
    logger.info("Updated bot_config description for topup_min_amount")


def downgrade():
    """Non-destructive — leave the key in place on downgrade."""
    pass
