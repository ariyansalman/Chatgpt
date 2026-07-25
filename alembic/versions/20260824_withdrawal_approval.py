"""V29 — Withdrawal Approval System.

Revision ID: 20260824_withdrawal_approval
Revises: 20260823_product_clone_template

Changes
-------
• Extends ``referral_withdrawals`` with payment_method, wallet_address, currency,
  admin_tg_id, approval_time, completion_time, reason, notes, logs_json columns.
• Seeds BotConfig keys for the withdrawal approval feature.
"""
import json
import logging
from datetime import datetime

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260824_withdrawal_approval"
down_revision = "20260823_product_clone_template"
branch_labels = None
depends_on    = None


def _column_exists(bind, table: str, column: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return bool(row)


def _table_exists(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ),
        {"t": table},
    ).fetchone()
    return bool(row)


_NEW_COLUMNS = [
    # (column_name, sa_type, nullable, server_default)
    ("payment_method",   sa.String(32),   True,  None),
    ("wallet_address",   sa.Text,         True,  None),
    ("currency",         sa.String(16),   True,  None),
    ("admin_tg_id",      sa.BigInteger,   True,  None),
    ("approval_time",    sa.DateTime,     True,  None),
    ("completion_time",  sa.DateTime,     True,  None),
    ("reason",           sa.Text,         True,  None),
    ("notes",            sa.Text,         True,  None),
    ("logs_json",        sa.Text,         True,  None),
]

_BOT_CONFIG_KEYS = [
    # (key, value_type, default, category, label, description)
    (
        "withdrawal_approval_status", "str", "enabled", "wallets",
        "💸 Withdrawal Approval: Status",
        "Controls withdrawal approval feature: 'enabled', 'maintenance', or 'disabled'.",
    ),
    (
        "withdrawal_approval_auto_approval", "bool", "false", "wallets",
        "💸 Withdrawal Approval: Auto Approval",
        "If ON, withdrawals below the auto-approval threshold are approved automatically.",
    ),
    (
        "withdrawal_approval_auto_max", "float", "10.0", "wallets",
        "💸 Withdrawal Approval: Auto Approval Max Amount",
        "Withdrawals at or below this amount are auto-approved (0 = disabled).",
    ),
    (
        "withdrawal_approval_min_amount", "float", "5.0", "wallets",
        "💸 Withdrawal Approval: Minimum Amount",
        "Minimum withdrawal amount allowed.",
    ),
    (
        "withdrawal_approval_max_amount", "float", "0.0", "wallets",
        "💸 Withdrawal Approval: Maximum Amount",
        "Maximum withdrawal amount (0 = unlimited).",
    ),
    (
        "withdrawal_approval_max_daily", "int", "0", "wallets",
        "💸 Withdrawal Approval: Max Daily Withdrawals Per User",
        "Maximum withdrawal requests per user per day (0 = unlimited).",
    ),
    (
        "withdrawal_approval_processing_time", "str", "1-3 business days", "wallets",
        "💸 Withdrawal Approval: Processing Time",
        "Estimated processing time shown to users, e.g. '1-3 business days'.",
    ),
    (
        "withdrawal_approval_retry_failed", "bool", "true", "wallets",
        "💸 Withdrawal Approval: Retry Failed",
        "If ON, failed withdrawal processing can be retried by admins.",
    ),
]


def upgrade():
    bind = op.get_bind()

    # ── 1. Extend referral_withdrawals ────────────────────────────────────────
    if _table_exists(bind, "referral_withdrawals"):
        for col_name, col_type, nullable, server_default in _NEW_COLUMNS:
            if not _column_exists(bind, "referral_withdrawals", col_name):
                op.add_column(
                    "referral_withdrawals",
                    sa.Column(col_name, col_type, nullable=nullable,
                              server_default=server_default),
                )
                logger.info("withdrawal_approval: added column referral_withdrawals.%s", col_name)
            else:
                logger.info("withdrawal_approval: column referral_withdrawals.%s already exists — skip", col_name)
    else:
        logger.warning("withdrawal_approval: referral_withdrawals table not found — skipping column additions")

    # ── 2. Seed BotConfig keys ────────────────────────────────────────────────
    for key, vtype, default, category, label, description in _BOT_CONFIG_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category, label, description) "
                    "VALUES (:k, :vt, :v, :cat, :lbl, :desc)"
                ),
                {
                    "k": key, "vt": vtype, "v": default,
                    "cat": category, "lbl": label, "desc": description,
                },
            )
            logger.info("withdrawal_approval: seeded BotConfig key %r", key)
        else:
            logger.info("withdrawal_approval: BotConfig key %r already exists — skip", key)


def downgrade():
    bind = op.get_bind()
    if _table_exists(bind, "referral_withdrawals"):
        for col_name, _, _, _ in reversed(_NEW_COLUMNS):
            if _column_exists(bind, "referral_withdrawals", col_name):
                op.drop_column("referral_withdrawals", col_name)
    for key, *_ in _BOT_CONFIG_KEYS:
        bind.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
