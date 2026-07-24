"""gateway_payments: extend PaymentMethod enum with BKASH + NAGAD.

Revision ID: 20260711_gwpay
Revises: 20260710_pi

Fully additive / non-destructive.
- On PostgreSQL: extends the native ``paymentmethod`` enum with the 2 new
  member values via ALTER TYPE ... ADD VALUE IF NOT EXISTS.
- On SQLite: PaymentMethod is stored as a plain VARCHAR (no native enum
  type), so this is a no-op there.
- No new tables/columns needed: bKash/Nagad reuse `transactions.crypto_address`
  (format "gateway_ref|pay_url", same convention as the CryptoBot column) and
  the existing `bot_config` key/value table for admin-set credentials.
"""
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "20260711_gwpay"
down_revision = "20260710_pi"
branch_labels = None
depends_on = None

NEW_ENUM_MEMBERS = ["BKASH", "NAGAD"]


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
                    sa.text(f"ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS '{member}'")
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
                    logger.warning("Could not add '%s' to paymentmethod enum: %s", member, e)


def downgrade():
    """Non-destructive downgrade — Postgres cannot drop enum values without
    rewriting every table that uses them, so the enum members are left in
    place intentionally (matches the pattern used by 20260708_product_types.py)."""
    pass
