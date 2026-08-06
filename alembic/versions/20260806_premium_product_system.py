"""premium_product_system: add Feature Box, Button Builder, Product Tags tables.

Revision ID: 20260806_premiumsystem
Revises:     20260921_autoverifylock
Create Date: 2026-08-06

Phase 1 of the Premium Product System. Purely additive — four new tables,
no existing table is touched, no existing column changes type or default,
and no existing callback_data, payment flow, or delivery logic is affected:

  product_feature_items    — per-product Feature Box rows (Feature 2)
  product_button_settings  — global label/emoji/visibility/order for the
                              shared product-page buttons (Feature 5)
  product_tags             — reusable tag catalog (Feature 7)
  product_tag_links        — many-to-many Product <-> ProductTag

All four are also covered by database/schema_check.py's model-driven
auto-heal, so a deployment that skips `alembic upgrade head` still gets the
tables created (CREATE TABLE IF NOT EXISTS) the next time the bot boots.
This migration is the authoritative, ordered path for environments that do
run alembic normally.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260806_premiumsystem"
down_revision = "20260921_autoverifylock"
branch_labels = None
depends_on = None

# Seed rows for product_button_settings — matches Feature 5's button list.
# Inserted with sane defaults so the admin panel has something to edit on
# first open; every existing product-page button keeps its current
# callback_data and current default label/emoji if the admin never touches
# this table (handlers fall back to these same defaults in code either way).
_DEFAULT_BUTTONS = [
    ("buy_now",   "Buy Now",    "🛒", True,  10),
    ("back",      "Back",       "🔙", True,  20),
    ("support",   "Support",    "☎️", True,  30),
    ("view_plans", "View Plans", "📋", True,  40),
    ("refresh",   "Refresh",    "🔄", True,  50),
    ("favorite",  "Favorite",   "❤️", True,  60),
    ("home",      "Home",       "🏠", True,  70),
]

# Seed rows for product_tags — matches Feature 7's tag list. Admin can
# rename, disable, reorder, or delete any of these, and add new ones.
_DEFAULT_TAGS = [
    ("featured",  "Featured",    "⭐", "yellow", 10),
    ("best_seller", "Best Seller", "🔥", "orange", 20),
    ("new",       "New",         "🆕", "green",  30),
    ("popular",   "Popular",     "📈", "blue",   40),
    ("premium",   "Premium",     "💎", "purple", 50),
    ("discount",  "Discount",    "🏷️", "red",    60),
    ("limited",   "Limited",     "⏳", "red",    70),
    ("digital",   "Digital",     "💾", "blue",   80),
    ("instant",   "Instant",     "⚡", "yellow", 90),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "product_feature_items" not in existing_tables:
        op.create_table(
            "product_feature_items",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer,
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("emoji", sa.String(32), nullable=True),
            sa.Column("title", sa.String(200), nullable=False, server_default=""),
            sa.Column("description", sa.String(500), nullable=True),
            sa.Column("is_visible", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime, nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_product_feature_items_product_id",
                        "product_feature_items", ["product_id"])
        op.create_index("ix_product_feature_items_is_visible",
                        "product_feature_items", ["is_visible"])
        op.create_index("ix_product_feature_items_display_order",
                        "product_feature_items", ["display_order"])

    if "product_button_settings" not in existing_tables:
        op.create_table(
            "product_button_settings",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("button_key", sa.String(32), nullable=False, unique=True, index=True),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("emoji", sa.String(32), nullable=True),
            sa.Column("is_visible", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime, nullable=False,
                      server_default=sa.func.now()),
        )
        button_settings_table = sa.table(
            "product_button_settings",
            sa.column("button_key", sa.String),
            sa.column("label", sa.String),
            sa.column("emoji", sa.String),
            sa.column("is_visible", sa.Boolean),
            sa.column("display_order", sa.Integer),
        )
        op.bulk_insert(button_settings_table, [
            {"button_key": k, "label": l, "emoji": e, "is_visible": v, "display_order": o}
            for k, l, e, v, o in _DEFAULT_BUTTONS
        ])

    if "product_tags" not in existing_tables:
        op.create_table(
            "product_tags",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("key", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("emoji", sa.String(32), nullable=True),
            sa.Column("color", sa.String(16), nullable=True, server_default="none"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime, nullable=False,
                      server_default=sa.func.now()),
        )
        tags_table = sa.table(
            "product_tags",
            sa.column("key", sa.String),
            sa.column("label", sa.String),
            sa.column("emoji", sa.String),
            sa.column("color", sa.String),
            sa.column("display_order", sa.Integer),
        )
        op.bulk_insert(tags_table, [
            {"key": k, "label": l, "emoji": e, "color": c, "display_order": o}
            for k, l, e, c, o in _DEFAULT_TAGS
        ])

    if "product_tag_links" not in existing_tables:
        op.create_table(
            "product_tag_links",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer,
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("tag_id", sa.Integer,
                      sa.ForeignKey("product_tags.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("created_at", sa.DateTime, nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("product_id", "tag_id", name="uq_product_tag_link"),
        )
        op.create_index("ix_product_tag_links_product_id",
                        "product_tag_links", ["product_id"])
        op.create_index("ix_product_tag_links_tag_id",
                        "product_tag_links", ["tag_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table in ("product_tag_links", "product_tags",
                  "product_button_settings", "product_feature_items"):
        if table in existing_tables:
            op.drop_table(table)
