"""V28 — Product Clone & Template System.

Revision ID: 20260823_product_clone_template
Revises: 20260822_webhook_monitor

Changes
-------
• Creates ``product_templates`` table — named reusable product blueprints.
• Creates ``product_clone_log`` table — audit trail of every clone operation.
• Seeds new BotConfig keys for feature settings.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260823_product_clone_template"
down_revision = "20260822_webhook_monitor"
branch_labels = None
depends_on    = None


def _table_exists(bind, table: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": table},
    ).fetchone()
    return bool(row)


_BOT_CONFIG_KEYS = [
    # (key, value_type, default, category)
    ("product_clone_status",       "str",  "enabled",  "products"),
    ("product_clone_images",       "bool", "True",     "products"),
    ("product_clone_faq",          "bool", "True",     "products"),
    ("product_clone_coupons",      "bool", "False",    "products"),
    ("product_clone_stock",        "bool", "False",    "products"),
    ("product_clone_settings",     "bool", "True",     "products"),
    ("product_clone_custom_fields","bool", "True",     "products"),
    ("product_template_max",       "int",  "50",       "products"),
]


def upgrade():
    bind = op.get_bind()

    # ── 1. product_templates ───────────────────────────────────────────────
    if not _table_exists(bind, "product_templates"):
        op.create_table(
            "product_templates",
            sa.Column("id",            sa.Integer,     primary_key=True),
            sa.Column("name",          sa.String(120), nullable=False, index=True),
            sa.Column("description",   sa.String(512), nullable=True),
            sa.Column("template_data", sa.Text,        nullable=False),  # JSON blob
            sa.Column("use_count",     sa.Integer,     nullable=False, default=0),
            sa.Column("created_by",    sa.BigInteger,  nullable=True),
            sa.Column("created_at",    sa.DateTime,    nullable=True),
            sa.Column("updated_at",    sa.DateTime,    nullable=True),
        )
        op.create_index("ix_pt_name",       "product_templates", ["name"])
        op.create_index("ix_pt_created_by", "product_templates", ["created_by"])
        logger.info("product_clone: created table product_templates")
    else:
        logger.info("product_clone: table product_templates already exists — skip")

    # ── 2. product_clone_log ───────────────────────────────────────────────
    if not _table_exists(bind, "product_clone_log"):
        op.create_table(
            "product_clone_log",
            sa.Column("id",                sa.Integer,     primary_key=True),
            sa.Column("source_product_id", sa.Integer,
                      sa.ForeignKey("products.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("cloned_product_id", sa.Integer,
                      sa.ForeignKey("products.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("template_id",       sa.Integer,
                      sa.ForeignKey("product_templates.id", ondelete="SET NULL"),
                      nullable=True, index=True),  # non-null if created from template
            sa.Column("created_by",        sa.BigInteger,  nullable=True),
            sa.Column("clone_type",        sa.String(32),  nullable=False,
                      default="single"),  # single|bulk_category|from_template
            sa.Column("options_json",      sa.Text,        nullable=True),  # JSON of overrides
            sa.Column("created_at",        sa.DateTime,    nullable=True, index=True),
        )
        op.create_index("ix_pcl_created_at", "product_clone_log", ["created_at"])
        logger.info("product_clone: created table product_clone_log")
    else:
        logger.info("product_clone: table product_clone_log already exists — skip")

    # ── 3. Seed BotConfig keys ─────────────────────────────────────────────
    for key, vtype, default, category in _BOT_CONFIG_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category) "
                    "VALUES (:k, :vt, :v, :cat)"
                ),
                {"k": key, "vt": vtype, "v": default, "cat": category},
            )
            logger.info("product_clone: seeded BotConfig key %r", key)


def downgrade():
    bind = op.get_bind()
    for tbl in ("product_clone_log", "product_templates"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
    for key, _, _, _ in _BOT_CONFIG_KEYS:
        bind.execute(sa.text("DELETE FROM bot_config WHERE key = :k"), {"k": key})
