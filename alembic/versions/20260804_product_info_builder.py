"""V48: Product Information Builder

Revision ID: v48_product_info_builder
Revises: 20260921_auto_verify_retry_lock
Create Date: 2026-08-04

Adds three new tables for the admin-controlled per-product information
block system:
  - product_info_blocks     (per-product info blocks)
  - product_info_templates  (reusable block collections)
  - product_info_template_blocks

Also adds a nullable `pib_settings` JSON column to `products` for
per-product purchase-flow settings.

The existing `database.db._autofix_missing_columns()` routine will add
`pib_settings` to the products table automatically on startup even
without running this migration, so the bot works on new and existing
databases.
"""

from alembic import op
import sqlalchemy as sa

revision = 'v48_product_info_builder'
down_revision = '20260921_auto_verify_retry_lock'
branch_labels = None
depends_on = None


def upgrade():
    # ── product_info_blocks ───────────────────────────────────────────────
    op.create_table(
        'product_info_blocks',
        sa.Column('id',            sa.Integer,     primary_key=True),
        sa.Column('product_id',    sa.Integer,     sa.ForeignKey('products.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('title',         sa.String(200), nullable=True),
        sa.Column('emoji',         sa.String(32),  nullable=True),
        sa.Column('content',       sa.Text,        nullable=False, server_default=''),
        sa.Column('block_type',    sa.String(32),  nullable=False, server_default='text'),
        sa.Column('accent_color',  sa.String(16),  nullable=True,  server_default='none'),
        sa.Column('is_bold',       sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('is_italic',     sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('has_spoiler',   sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('is_visible',    sa.Boolean,     nullable=False, server_default='true',  index=True),
        sa.Column('display_order', sa.Integer,     nullable=False, server_default='0',     index=True),
        sa.Column('created_at',    sa.DateTime,    nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at',    sa.DateTime,    nullable=False, server_default=sa.func.now()),
    )

    # ── product_info_templates ────────────────────────────────────────────
    op.create_table(
        'product_info_templates',
        sa.Column('id',          sa.Integer,     primary_key=True),
        sa.Column('name',        sa.String(200), nullable=False),
        sa.Column('emoji',       sa.String(32),  nullable=True),
        sa.Column('description', sa.Text,        nullable=True),
        sa.Column('is_active',   sa.Boolean,     nullable=False, server_default='true'),
        sa.Column('created_at',  sa.DateTime,    nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at',  sa.DateTime,    nullable=False, server_default=sa.func.now()),
    )

    # ── product_info_template_blocks ──────────────────────────────────────
    op.create_table(
        'product_info_template_blocks',
        sa.Column('id',            sa.Integer,     primary_key=True),
        sa.Column('template_id',   sa.Integer,     sa.ForeignKey('product_info_templates.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('title',         sa.String(200), nullable=True),
        sa.Column('emoji',         sa.String(32),  nullable=True),
        sa.Column('content',       sa.Text,        nullable=False, server_default=''),
        sa.Column('block_type',    sa.String(32),  nullable=False, server_default='text'),
        sa.Column('accent_color',  sa.String(16),  nullable=True,  server_default='none'),
        sa.Column('is_bold',       sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('is_italic',     sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('has_spoiler',   sa.Boolean,     nullable=False, server_default='false'),
        sa.Column('is_visible',    sa.Boolean,     nullable=False, server_default='true'),
        sa.Column('display_order', sa.Integer,     nullable=False, server_default='0'),
    )

    # ── pib_settings column on products ──────────────────────────────────
    op.add_column('products',
        sa.Column('pib_settings', sa.Text, nullable=True))


def downgrade():
    op.drop_column('products', 'pib_settings')
    op.drop_table('product_info_template_blocks')
    op.drop_table('product_info_templates')
    op.drop_table('product_info_blocks')
