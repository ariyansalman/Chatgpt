"""favorites: Add user_favorites table.

Revision ID: 20260814_favorites
Revises: 20260813_product_compare

A pure "save for later" bookmarking system, separate from the existing
user_wishlists table (which drives price-drop alerts).

New table: user_favorites
Columns:
    id          INTEGER  PRIMARY KEY
    user_id     INTEGER  NOT NULL  FK → users.id  INDEX
    product_id  INTEGER  NOT NULL  FK → products.id  INDEX
    created_at  DATETIME NOT NULL  DEFAULT now
    note        VARCHAR(255) NULL   (reserved for future per-item notes)

UniqueConstraint on (user_id, product_id) prevents duplicates.
Idempotent (guarded by table-exists check).
"""
import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260814_favorites"
down_revision = "20260813_product_compare"
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

    if _table_exists(bind, "user_favorites"):
        logger.info("favorites: user_favorites already exists — skipping.")
        return

    op.create_table(
        "user_favorites",
        sa.Column("id",         sa.Integer(),     primary_key=True, nullable=False),
        sa.Column("user_id",    sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(),    nullable=False,
                  server_default=sa.func.now()),
        sa.Column("note",       sa.String(255),   nullable=True),
        sa.UniqueConstraint("user_id", "product_id", name="uq_user_favorite"),
    )
    logger.info("favorites: created user_favorites table.")


def downgrade():
    bind = op.get_bind()
    if _table_exists(bind, "user_favorites"):
        op.drop_table("user_favorites")
        logger.info("favorites: dropped user_favorites table.")
