"""bybit_pay: extend PaymentMethod enum with BYBIT_PAY.

Revision ID: 20260719_bybitpay
Revises: 20260718_delivfmt

Fully additive / non-destructive.
- On PostgreSQL: extends the native ``paymentmethod`` enum with the new
  member value via ALTER TYPE ... ADD VALUE IF NOT EXISTS.
- On SQLite: PaymentMethod is stored as a plain VARCHAR (no native enum
  type), so this is a no-op there.
- No destructive changes to existing tables. The new
  ``payment_gateway_configs.bybit_*`` columns and the
  ``bybit_pay_transactions`` table (see database/models.py,
  services/bybit_pay.py, handlers/admin_bybit.py) are created by
  ``Base.metadata.create_all()`` / the SQLite dev auto-heal on app start,
  same convention as 20260711_gateway_payments.py / Binance Pay's rollout —
  see migrations/v16_bybit_pay.py for the explicit production rollout
  script covering an existing PostgreSQL database.
"""
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "20260719_bybitpay"
down_revision = "20260718_delivfmt"
branch_labels = None
depends_on = None

NEW_ENUM_MEMBERS = ["BYBIT_PAY"]


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
    place intentionally (matches the pattern used by 20260711_gateway_payments.py)."""
    pass
