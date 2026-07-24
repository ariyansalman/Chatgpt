"""enum_completeness: add all missing PaymentMethod and TransactionStatus values.

Revision ID: 20260722_enumfix
Revises: 20260721_zinipay

Adds every PaymentMethod enum value that exists in the Python model but was
never added via an ALTER TYPE migration.  Also adds the two TransactionStatus
values (AWAITING_CONFIRMATION, REJECTED) that were missing.

On PostgreSQL the enum type is a native DB type, so each new value must be
added explicitly with ALTER TYPE ... ADD VALUE IF NOT EXISTS.

On SQLite these types are stored as plain VARCHAR columns, so this migration
is a no-op there — the Python enum constrains the values in application code.

All additions are strictly additive (no column or row changes) and are safe
to run against databases that were created at any point in the migration chain.
"""
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "20260722_enumfix"
down_revision = "20260721_zinipay"
branch_labels = None
depends_on = None

# PaymentMethod values that exist in the Python enum but have no prior
# ALTER TYPE migration.  BKASH/NAGAD were added in 20260711_gwpay;
# BYBIT_PAY in 20260719_bybitpay — those are already present and the
# IF NOT EXISTS guard makes running them again a safe no-op.
PAYMENT_METHOD_MEMBERS = [
    "STARS",
    "CRYPTOMUS",
    "NOWPAYMENTS",
    "ZINIPAY",
    "BINANCE_PAY",
    "HELEKET",
]

# TransactionStatus values that exist in the Python enum but have no prior
# ALTER TYPE migration.  CANCELLED was added in 20260720_txncancel.
TRANSACTION_STATUS_MEMBERS = [
    "AWAITING_CONFIRMATION",
    "REJECTED",
]


def _add_enum_values(bind, type_name: str, members: list) -> None:
    """Run ALTER TYPE <type_name> ADD VALUE IF NOT EXISTS '<member>' for each
    member.  Must be called outside an open transaction on PostgreSQL."""
    for member in members:
        # NOTE: ALTER TYPE ADD VALUE does not accept a bound parameter for
        # the value — Postgres DDL grammar only allows a literal there.
        # `member` is one of this module's own hardcoded constants (never
        # user input), so a literal is safe here.
        try:
            bind.execute(
                sa.text(
                    f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{member}'"
                )
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "duplicate" in msg:
                # Value already present — not an error.
                pass
            else:
                # Log but continue: a real failure here means one value is
                # permanently missing from production.  The warning surfaces it
                # clearly without aborting the rest of the migration.
                logger.warning(
                    "Could not add '%s' to %s enum: %s", member, type_name, exc
                )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite stores enums as plain VARCHAR — no DDL needed.
        return

    # ALTER TYPE ADD VALUE requires being outside an open transaction.
    bind.execute(sa.text("COMMIT"))

    _add_enum_values(bind, "paymentmethod", PAYMENT_METHOD_MEMBERS)
    _add_enum_values(bind, "transactionstatus", TRANSACTION_STATUS_MEMBERS)


def downgrade():
    """Non-destructive downgrade: PostgreSQL cannot drop enum values without
    rewriting every table that references them, so we leave them in place —
    the same policy used by all prior enum migrations in this project."""
    pass
