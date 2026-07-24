"""recently_viewed: Ensure recently_viewed table exists + add admin config rows.

Revision ID: 20260815_recently_viewed
Revises: 20260814_favorites

The recently_viewed table was first created via the V18 startup SQL block,
but may be missing on fresh installations that rely solely on Alembic.
This migration is fully idempotent: it creates the table only when absent.

Table: recently_viewed
Columns:
    id          INTEGER  PRIMARY KEY
    user_id     INTEGER  NOT NULL  FK → users.id  INDEX
    product_id  INTEGER  NOT NULL  FK → products.id  INDEX
    viewed_at   DATETIME NOT NULL  DEFAULT now

UniqueConstraint on (user_id, product_id) prevents duplicates.
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260815_recently_viewed"
down_revision = "20260814_favorites"
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

    if _table_exists(bind, "recently_viewed"):
        logger.info("recently_viewed: table already exists — skipping creation.")
    else:
        op.create_table(
            "recently_viewed",
            sa.Column("id",         sa.Integer(),  primary_key=True, nullable=False),
            sa.Column("user_id",    sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer(),
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("viewed_at",  sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "product_id", name="uq_rv_user_product"),
        )
        logger.info("recently_viewed: created recently_viewed table.")


def downgrade():
    bind = op.get_bind()
    if _table_exists(bind, "recently_viewed"):
        op.drop_table("recently_viewed")
        logger.info("recently_viewed: dropped recently_viewed table.")
