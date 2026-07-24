"""payment_order_fix: extend TransactionStatus enum with CANCELLED.

Revision ID: 20260720_txncancel
Revises: 20260719_bybitpay

Fully additive / non-destructive — part of the pending-payment-order fix:
expired, user-cancelled, or admin-cancelled orders now move to a single,
unambiguous terminal ``CANCELLED`` status instead of being left ``PENDING``
(which used to block the user from creating a new payment order) or mixed
across ``EXPIRED``/``FAILED``.

- On PostgreSQL: extends the native ``transactionstatus`` enum with the new
  member value via ALTER TYPE ... ADD VALUE IF NOT EXISTS.
- On SQLite: TransactionStatus is stored as a plain VARCHAR (no native enum
  type), so this is a no-op there.
- No destructive changes to existing rows/tables. Existing ``expired`` /
  ``failed`` rows are left as-is; only new cancellations use the new value.
"""
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "20260720_txncancel"
down_revision = "20260719_bybitpay"
branch_labels = None
depends_on = None

NEW_ENUM_MEMBERS = ["CANCELLED"]


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ADD VALUE requires no open transaction (Postgres restriction).
        bind.execute(sa.text("COMMIT"))
        for member in NEW_ENUM_MEMBERS:
            # NOTE: `ALTER TYPE ... ADD VALUE` does not accept a bound
            # parameter for the value — PostgreSQL's DDL grammar only
            # accepts a literal there, so `.bindparams(val=member)` fails
            # with a syntax error on every run. `member` is one of this
            # module's own hardcoded constants above (never user input),
            # so a literal is safe here.
            try:
                bind.execute(
                    sa.text(f"ALTER TYPE transactionstatus ADD VALUE IF NOT EXISTS '{member}'")
                )
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    pass
                else:
                    # Enum type may be named differently in old installs —
                    # log it instead of swallowing silently, so a real
                    # failure here doesn't leave a member permanently
                    # missing from production without anyone noticing.
                    logger.warning("Could not add '%s' to transactionstatus enum: %s", member, e)


def downgrade():
    """Non-destructive downgrade — Postgres cannot drop enum values without
    rewriting every table that uses them, so the enum member is left in
    place intentionally (matches the pattern used by 20260719_bybit_pay.py)."""
    pass
