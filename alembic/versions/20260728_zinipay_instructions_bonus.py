"""zinipay_v2: per-provider instructions + deposit bonus columns.

Revision ID: 20260728_zinipay_v2
Revises: 20260723_zinipay_wallets

Adds 8 new nullable columns to payment_gateway_configs:
  Per-provider payment instructions:
    - zinipay_bkash_instructions   — instructions shown to bKash payers
    - zinipay_nagad_instructions   — instructions shown to Nagad payers
    - zinipay_rocket_instructions  — instructions shown to Rocket payers
    - zinipay_upay_instructions    — instructions shown to Upay payers
  Deposit bonus settings:
    - zinipay_bonus_percent        — deposit bonus percentage (0.0 = no bonus)
    - zinipay_bonus_enabled        — whether the bonus is currently active
    - zinipay_bonus_min_deposit    — minimum deposit USD to qualify (NULL = no min)
    - zinipay_bonus_max_amount     — maximum bonus USD per deposit (NULL = no cap)

All columns are strictly additive and nullable, so existing rows remain valid.
The inline ADD COLUMN IF NOT EXISTS block in bot.py also applies these changes
at startup for databases that don't use Alembic migrations.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260728_zinipay_v2"
down_revision = "20260723_zinipay_wallets"
branch_labels = None
depends_on = None

_TABLE = "payment_gateway_configs"

_COLUMNS = [
    # Per-provider instructions (nullable TEXT)
    ("zinipay_bkash_instructions",  sa.Text(),    None,  True),
    ("zinipay_nagad_instructions",  sa.Text(),    None,  True),
    ("zinipay_rocket_instructions", sa.Text(),    None,  True),
    ("zinipay_upay_instructions",   sa.Text(),    None,  True),
    # Bonus settings
    ("zinipay_bonus_percent",     sa.Float(),   0.0,   True),
    ("zinipay_bonus_enabled",     sa.Boolean(), False, False),  # NOT NULL
    ("zinipay_bonus_min_deposit", sa.Float(),   None,  True),
    ("zinipay_bonus_max_amount",  sa.Float(),   None,  True),
]


def upgrade():
    for col_name, col_type, default, nullable in _COLUMNS:
        kwargs: dict = {"nullable": nullable}
        if default is not None:
            if isinstance(default, bool):
                kwargs["server_default"] = sa.true() if default else sa.false()
            elif isinstance(default, float):
                kwargs["server_default"] = sa.text(str(default))
            else:
                kwargs["server_default"] = sa.text(f"'{default}'")
        try:
            op.add_column(_TABLE, sa.Column(col_name, col_type, **kwargs))
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass  # Column already present — safe to skip.
            else:
                raise


def downgrade():
    for col_name, _, _, _ in reversed(_COLUMNS):
        try:
            op.drop_column(_TABLE, col_name)
        except Exception:
            pass
