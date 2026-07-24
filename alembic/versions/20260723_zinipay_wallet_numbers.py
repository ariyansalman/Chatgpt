"""zinipay_wallet_numbers: add per-provider wallet number + rate columns to payment_gateway_configs.

Revision ID: 20260723_zinipay_wallets
Revises: 20260722_enumfix

Adds 8 new nullable columns to payment_gateway_configs so the admin can
configure, from the Telegram admin panel:
  - zinipay_bkash_number   — bKash merchant number shown to users
  - zinipay_nagad_number   — Nagad merchant number
  - zinipay_rocket_number  — Rocket merchant number
  - zinipay_upay_number    — Upay merchant number
  - zinipay_default_provider — which provider to highlight (default "bkash")
  - zinipay_usd_to_bdt_rate  — per-gateway USD→BDT rate override (NULL = global)
  - zinipay_auto_rate        — whether to auto-refresh the rate (boolean)
  - zinipay_instructions     — free-form payment instructions (TEXT)

All columns are strictly additive and nullable, so existing rows remain valid.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260723_zinipay_wallets"
down_revision = "20260722_enumfix"
branch_labels = None
depends_on = None

_TABLE = "payment_gateway_configs"

_COLUMNS = [
    ("zinipay_bkash_number",      sa.String(120),  None),
    ("zinipay_nagad_number",      sa.String(120),  None),
    ("zinipay_rocket_number",     sa.String(120),  None),
    ("zinipay_upay_number",       sa.String(120),  None),
    ("zinipay_default_provider",  sa.String(10),   "bkash"),
    ("zinipay_usd_to_bdt_rate",   sa.Float(),       None),
    ("zinipay_auto_rate",         sa.Boolean(),     False),
    ("zinipay_instructions",      sa.Text(),        None),
]


def upgrade():
    is_pg = op.get_bind().dialect.name == "postgresql"
    for col_name, col_type, default in _COLUMNS:
        kwargs = {"nullable": True}
        if default is not None:
            kwargs["server_default"] = (
                sa.true() if default is True
                else sa.false() if default is False
                else sa.text(f"'{default}'")
            )
        try:
            op.add_column(_TABLE, sa.Column(col_name, col_type, **kwargs))
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass  # Column already present from a previous run — safe to skip.
            else:
                raise


def downgrade():
    for col_name, _, _ in reversed(_COLUMNS):
        try:
            op.drop_column(_TABLE, col_name)
        except Exception:
            pass
