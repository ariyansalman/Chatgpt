"""binance_bybit_complete: complete Binance Pay & Bybit Pay payment system.

Revision ID: 20260725_binance_bybit_complete
Revises: 20260724_admin_api_keys

Fully additive / non-destructive:
- Adds admin_telegram_id column to pending_manual_verifications
  (so we can store who approved/rejected for audit purposes)
- Adds reject_reason column to pending_manual_verifications
  (separate from admin_note for easier querying)
- Adds payment_verification_log.ip_hash column (if table already exists
  without it — handles partial migrations from 20260724)
- Ensures all Binance/Bybit columns exist in payment_gateway_configs:
    binance_pay_id, binance_api_key, binance_api_secret,
    binance_min_amount, binance_max_amount, binance_order_expiry_minutes,
    binance_bonus_percent, binance_instructions, binance_allowed_currencies
    bybit_uid, bybit_api_key, bybit_api_secret,
    bybit_wallet_trc20, bybit_wallet_bep20, bybit_wallet_erc20,
    bybit_allowed_networks, bybit_min_amount, bybit_max_amount,
    bybit_order_expiry_minutes, bybit_bonus_percent, bybit_instructions
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "20260725_binance_bybit_complete"
down_revision = "20260724_admin_api_keys"
branch_labels = None
depends_on = None


def _col_exists(bind, table: str, column: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        cols = [c["name"] for c in sa_inspect(bind).get_columns(table)]
        return column in cols
    except Exception:
        return False


def _table_exists(bind, table: str) -> bool:
    from sqlalchemy import inspect as sa_inspect
    try:
        return sa_inspect(bind).has_table(table)
    except Exception:
        return False


def _safe_add_column(table: str, column_name: str, column_type):
    bind = op.get_bind()
    if not _col_exists(bind, table, column_name):
        try:
            op.add_column(table, sa.Column(column_name, column_type, nullable=True))
            logger.info("Added column %s.%s", table, column_name)
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                logger.info("Column %s.%s already exists — skipping", table, column_name)
            else:
                logger.warning("Could not add column %s.%s: %s", table, column_name, e)


def upgrade():
    bind = op.get_bind()

    # ── pending_manual_verifications: add audit columns ──────────────────
    if _table_exists(bind, "pending_manual_verifications"):
        _safe_add_column("pending_manual_verifications", "admin_telegram_id", sa.BigInteger())
        _safe_add_column("pending_manual_verifications", "reject_reason", sa.Text())

    # ── payment_verification_log: ensure ip_hash column ─────────────────
    if _table_exists(bind, "payment_verification_log"):
        _safe_add_column("payment_verification_log", "ip_hash", sa.String(64))

    # ── payment_gateway_configs: ensure all Binance Pay columns ─────────
    if _table_exists(bind, "payment_gateway_configs"):
        _binance_cols = [
            ("binance_pay_id", sa.String(64)),
            ("binance_api_key", sa.Text()),
            ("binance_api_secret", sa.Text()),
            ("binance_allowed_currencies", sa.String(120)),
            ("binance_min_amount", sa.Float()),
            ("binance_max_amount", sa.Float()),
            ("binance_order_expiry_minutes", sa.Integer()),
            ("binance_bonus_percent", sa.Float()),
            ("binance_instructions", sa.Text()),
        ]
        for col_name, col_type in _binance_cols:
            _safe_add_column("payment_gateway_configs", col_name, col_type)

        # ── Bybit Pay columns ────────────────────────────────────────────
        _bybit_cols = [
            ("bybit_uid", sa.String(64)),
            ("bybit_api_key", sa.Text()),
            ("bybit_api_secret", sa.Text()),
            ("bybit_wallet_trc20", sa.String(255)),
            ("bybit_wallet_bep20", sa.String(255)),
            ("bybit_wallet_erc20", sa.String(255)),
            ("bybit_allowed_networks", sa.String(120)),
            ("bybit_min_amount", sa.Float()),
            ("bybit_max_amount", sa.Float()),
            ("bybit_order_expiry_minutes", sa.Integer()),
            ("bybit_bonus_percent", sa.Float()),
            ("bybit_instructions", sa.Text()),
        ]
        for col_name, col_type in _bybit_cols:
            _safe_add_column("payment_gateway_configs", col_name, col_type)

    # ── binance_pay_transactions: ensure table exists ────────────────────
    if not _table_exists(bind, "binance_pay_transactions"):
        op.create_table(
            "binance_pay_transactions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("transaction_id", sa.String(128), nullable=False, unique=True, index=True),
            sa.Column("binance_order_id", sa.String(128), nullable=True, index=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("currency", sa.String(16), nullable=False),
            sa.Column("expected_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("received_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("transaction_time", sa.DateTime(), nullable=True),
            sa.Column("verified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("raw_transaction_data", sa.Text(), nullable=True),
        )

    # ── bybit_pay_transactions: ensure table exists ──────────────────────
    if not _table_exists(bind, "bybit_pay_transactions"):
        op.create_table(
            "bybit_pay_transactions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("transaction_id", sa.String(128), nullable=False, unique=True, index=True),
            sa.Column("bybit_record_id", sa.String(128), nullable=True, index=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("payment_type", sa.String(16), nullable=False),
            sa.Column("network", sa.String(16), nullable=True),
            sa.Column("currency", sa.String(16), nullable=False),
            sa.Column("expected_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("received_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("transaction_time", sa.DateTime(), nullable=True),
            sa.Column("verified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("raw_transaction_data", sa.Text(), nullable=True),
        )

    # ── verification_attempt_log: ensure table exists ────────────────────
    if not _table_exists(bind, "payment_verification_log"):
        op.create_table(
            "payment_verification_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("gateway", sa.String(32), nullable=False, index=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("submitted_txid", sa.String(256), nullable=False),
            sa.Column("outcome", sa.String(64), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("ip_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    # ── pending_manual_verifications: ensure table exists ────────────────
    if not _table_exists(bind, "pending_manual_verifications"):
        op.create_table(
            "pending_manual_verifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("gateway", sa.String(32), nullable=False, index=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("internal_order_id", sa.Integer(), nullable=False, index=True),
            sa.Column("submitted_txid", sa.String(256), nullable=False),
            sa.Column("amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(16), nullable=False),
            sa.Column("payment_type", sa.String(32), nullable=True),
            sa.Column("network", sa.String(16), nullable=True),
            sa.Column("auto_outcome", sa.String(64), nullable=True),
            sa.Column("auto_detail", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("admin_note", sa.Text(), nullable=True),
            sa.Column("admin_telegram_id", sa.BigInteger(), nullable=True),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )

    # ── Ensure BINANCE_PAY and BYBIT_PAY are in the paymentmethod enum ────
    if bind.dialect.name == "postgresql":
        try:
            bind.execute(sa.text("COMMIT"))
        except Exception:
            pass
        for member in ("BINANCE_PAY", "BYBIT_PAY"):
            try:
                bind.execute(
                    sa.text(f"ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS '{member}'")
                )
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    pass
                else:
                    logger.warning("Could not add '%s' to paymentmethod enum: %s", member, e)


def downgrade():
    """Non-destructive — intentionally leaves all new columns and tables."""
    pass
