"""Enterprise Product Template System — extend product_templates table.

Adds per-template metadata columns so the new apt:* handler can filter,
sort, default-mark, archive, and surface usage statistics without parsing
the full JSON template_data blob on every query.

Revision ID: 20260918_product_template_system
Revises:     20260917_enterprise_admin_notifications
Create Date: 2026-09-18

All new columns have server defaults so existing rows are silently
back-filled and the V28 pct:* clone-template system is unaffected.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260918_product_template_system"
down_revision = "20260917_enterprise_admin_notifications"
branch_labels = None
depends_on = None

_TABLE = "product_templates"

_NEW_COLUMNS = [
    # ProductType enum name (KEY, REDEEM_LINK, …) — VARCHAR so no enum sync needed
    ("template_type",      sa.String(32),  None),
    ("delivery_method",    sa.String(32),  None),
    ("is_default",         sa.Boolean(),   False),
    ("is_archived",        sa.Boolean(),   False),
    # JSON list of string tags  e.g. ["software", "windows"]
    ("tags_json",          sa.Text(),      None),
    ("last_used_at",       sa.DateTime(),  None),
    ("products_created",   sa.Integer(),   0),
    ("default_price",      sa.Float(),     None),
    ("currency_code",      sa.String(10),  "USD"),
    # 'public' | 'hidden'
    ("visibility",         sa.String(16),  "public"),
    ("auto_delivery",      sa.Boolean(),   True),
    ("manual_review",      sa.Boolean(),   False),
    ("refund_policy",      sa.Text(),      None),
    ("replacement_policy", sa.Text(),      None),
    ("warranty_info",      sa.Text(),      None),
    # Telegram file_id or URL
    ("product_image",      sa.String(256), None),
    # JSON object of type-specific delivery field defaults
    ("custom_fields_json", sa.Text(),      None),
]


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return (result.scalar() or 0) > 0


def upgrade() -> None:
    for col_name, col_type, default_val in _NEW_COLUMNS:
        if _column_exists(_TABLE, col_name):
            continue
        kwargs: dict = {"nullable": True}
        if default_val is not None:
            kwargs["server_default"] = (
                sa.text("true")  if default_val is True  else
                sa.text("false") if default_val is False else
                sa.text(f"'{default_val}'") if isinstance(default_val, str) else
                sa.text(str(default_val))
            )
        op.add_column(_TABLE, sa.Column(col_name, col_type, **kwargs))
        # Back-fill non-null defaults for older DB engines
        if default_val is not None:
            if isinstance(default_val, bool):
                sql_val = "true" if default_val else "false"
            elif isinstance(default_val, str):
                sql_val = f"'{default_val}'"
            else:
                sql_val = str(default_val)
            op.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET {col_name} = {sql_val} "
                    f"WHERE {col_name} IS NULL"
                )
            )


def downgrade() -> None:
    for col_name, _, _ in _NEW_COLUMNS:
        if _column_exists(_TABLE, col_name):
            op.drop_column(_TABLE, col_name)
