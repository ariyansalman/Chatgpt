"""V25 — Product FAQ System: create product_faqs table + seed BotConfig.

Revision ID: 20260820_product_faq
Revises: 20260819_order_timeline

Changes
-------
• Creates ``product_faqs`` table (id, product_id FK, question, answer,
  category, sort_order, is_active, created_at, updated_at).
• Unique constraint: (product_id, lower(question)) enforced at application
  layer; DB has a non-unique index on product_id + sort_order for ordering.
• Idempotently seeds BotConfig keys for admin-configurable settings.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260820_product_faq"
down_revision = "20260819_order_timeline"
branch_labels = None
depends_on    = None

_BOT_CONFIG_KEYS = [
    ("pfaq_status",          "str",  "enabled"),
    ("pfaq_max_per_product", "str",  "20"),
    ("pfaq_show_counter",    "bool", "True"),
    ("pfaq_allow_search",    "bool", "True"),
    ("pfaq_expand_first",    "bool", "False"),
]


def upgrade():
    op.create_table(
        "product_faqs",
        sa.Column("id",         sa.Integer,     primary_key=True),
        sa.Column("product_id", sa.Integer,
                  sa.ForeignKey("products.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("question",   sa.Text,        nullable=False),
        sa.Column("answer",     sa.Text,        nullable=False),
        sa.Column("category",   sa.String(32),  nullable=False, default="general"),
        sa.Column("sort_order", sa.Integer,     nullable=False, default=0),
        sa.Column("is_active",  sa.Boolean,     nullable=False, default=True),
        sa.Column("created_at", sa.DateTime,    nullable=True),
        sa.Column("updated_at", sa.DateTime,    nullable=True),
    )
    op.create_index("ix_pfaq_product_sort",
                    "product_faqs", ["product_id", "sort_order"])

    # Seed BotConfig keys (idempotent)
    bind = op.get_bind()
    for key, vtype, default in _BOT_CONFIG_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category) "
                    "VALUES (:k, :vt, :v, 'ops')"
                ),
                {"k": key, "vt": vtype, "v": default},
            )
            logger.info("product_faq: seeded BotConfig key %r", key)
        else:
            logger.info("product_faq: BotConfig key %r already exists — skip", key)


def downgrade():
    op.drop_index("ix_pfaq_product_sort", table_name="product_faqs")
    op.drop_table("product_faqs")
    bind = op.get_bind()
    for key, _, _ in _BOT_CONFIG_KEYS:
        bind.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
        logger.info("product_faq: removed BotConfig key %r", key)
