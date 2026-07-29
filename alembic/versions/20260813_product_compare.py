"""product_compare: Add product comparison tables.

Revision ID: 20260813_product_compare
Revises: 20260812_subscription_reminder

New tables:
    product_comparisons     — per-user compare list (max 4 products)
    product_compare_logs    — comparison session log for admin statistics

Both tables are idempotent (guarded by table-exists checks).
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260813_product_compare"
down_revision = "20260812_subscription_reminder"
branch_labels = None
depends_on    = None


def _table_exists(bind, name: str) -> bool:
    if bind.dialect.name == "postgresql":
        r = bind.execute(sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": name}).fetchone()
    else:
        r = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"
        ), {"t": name}).fetchone()
    return r is not None


def upgrade():
    bind = op.get_bind()

    # ── product_comparisons ────────────────────────────────────────────────
    if not _table_exists(bind, "product_comparisons"):
        op.create_table(
            "product_comparisons",
            sa.Column("id",               sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("user_telegram_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("product_id",       sa.Integer(),
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("added_at",         sa.DateTime(),   nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("user_telegram_id", "product_id",
                                name="uq_compare_user_product"),
        )
        logger.info("product_compare: created product_comparisons table.")
    else:
        logger.info("product_compare: product_comparisons already exists — skip.")

    # ── product_compare_logs ───────────────────────────────────────────────
    if not _table_exists(bind, "product_compare_logs"):
        op.create_table(
            "product_compare_logs",
            sa.Column("id",               sa.Integer(),    primary_key=True, nullable=False),
            sa.Column("user_telegram_id", sa.BigInteger(), nullable=False, index=True),
            sa.Column("product_ids_json", sa.Text(),       nullable=False),
            sa.Column("product_count",    sa.Integer(),    nullable=False,
                      server_default=sa.text("0")),
            sa.Column("purchased_from_compare", sa.Boolean(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("purchased_product_id",   sa.Integer(), nullable=True),
            sa.Column("viewed_at",        sa.DateTime(),   nullable=False,
                      server_default=sa.func.now()),
        )
        logger.info("product_compare: created product_compare_logs table.")
    else:
        logger.info("product_compare: product_compare_logs already exists — skip.")


def downgrade():
    bind = op.get_bind()
    for tname in ("product_compare_logs", "product_comparisons"):
        if _table_exists(bind, tname):
            op.drop_table(tname)
            logger.info("product_compare: dropped %s.", tname)
