"""price_history: Add product_price_history table — V23.

Revision ID: 20260816_price_history
Revises: 20260815_recently_viewed

New table: product_price_history
Columns:
    id                      INTEGER   PRIMARY KEY
    product_id              INTEGER   NOT NULL  FK → products.id  INDEX
    old_price               FLOAT     NOT NULL  DEFAULT 0
    new_price               FLOAT     NOT NULL
    difference              FLOAT     NOT NULL  DEFAULT 0
    pct_change              FLOAT     NULL   (NULL when old_price is 0)
    changed_by_telegram_id  BIGINT    NULL   INDEX
    changed_by_name         VARCHAR(128) NULL
    reason                  VARCHAR(255) NULL
    changed_at              DATETIME  NOT NULL  DEFAULT now  INDEX

No unique constraint — multiple records per (product, price) are allowed
at different times. Duplicate prevention is enforced at the service layer
(only records when the price actually changed).

Idempotent (guarded by table-exists check).
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260816_price_history"
down_revision = "20260815_recently_viewed"
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

    if _table_exists(bind, "product_price_history"):
        logger.info("price_history: product_price_history already exists — skipping.")
        return

    op.create_table(
        "product_price_history",
        sa.Column("id",         sa.Integer(),    primary_key=True, nullable=False),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("old_price",  sa.Float(),      nullable=False, server_default="0"),
        sa.Column("new_price",  sa.Float(),      nullable=False),
        sa.Column("difference", sa.Float(),      nullable=False, server_default="0"),
        sa.Column("pct_change", sa.Float(),      nullable=True),
        sa.Column("changed_by_telegram_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("changed_by_name",        sa.String(128),  nullable=True),
        sa.Column("reason",                 sa.String(255),  nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now(), index=True),
    )
    logger.info("price_history: created product_price_history table.")


def downgrade():
    bind = op.get_bind()
    if _table_exists(bind, "product_price_history"):
        op.drop_table("product_price_history")
        logger.info("price_history: dropped product_price_history table.")
