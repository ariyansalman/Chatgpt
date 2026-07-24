"""V24 — Supplier Auto Assignment: new table + supplier columns + BotConfig keys.

Revision ID: 20260818_supplier_auto_assign
Revises: 20260817_inventory_reservation

Changes:
    1. ALTER TABLE suppliers — add columns:
          priority          INTEGER  NOT NULL DEFAULT 10
          total_delivered   INTEGER  NOT NULL DEFAULT 0
          total_failed      INTEGER  NOT NULL DEFAULT 0
          last_activity     TIMESTAMP NULL

    2. CREATE TABLE supplier_products — supplier-product assignment map
          id                SERIAL PRIMARY KEY
          supplier_id       INTEGER NOT NULL REFERENCES suppliers(id)
          product_id        INTEGER NOT NULL REFERENCES products(id)
          variant_id        INTEGER NULL REFERENCES product_variants(id)
          priority          INTEGER NOT NULL DEFAULT 10
          is_auto_assign    BOOLEAN NOT NULL DEFAULT TRUE
          is_active         BOOLEAN NOT NULL DEFAULT TRUE
          max_daily_qty     INTEGER NULL
          notes             TEXT NULL
          created_at        TIMESTAMP DEFAULT now()
          updated_at        TIMESTAMP DEFAULT now()
          UNIQUE(supplier_id, product_id, variant_id)

    3. Seed BotConfig rows (idempotent):
          sas_enabled          bool  True
          sas_fallback_to_any  bool  True
"""
from __future__ import annotations

import logging
import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision      = "20260818_supplier_auto_assign"
down_revision = "20260817_inventory_reservation"
branch_labels = None
depends_on    = None

_NEW_BOT_CONFIG_KEYS = [
    ("sas_enabled",         "bool", "True"),
    ("sas_fallback_to_any", "bool", "True"),
]


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 1. Add new columns to suppliers ────────────────────────────────────
    existing_cols = {col["name"] for col in inspector.get_columns("suppliers")}

    if "priority" not in existing_cols:
        op.add_column("suppliers", sa.Column("priority", sa.Integer(),
                                             nullable=False, server_default="10"))
        logger.info("supplier_auto_assign: added suppliers.priority")

    if "total_delivered" not in existing_cols:
        op.add_column("suppliers", sa.Column("total_delivered", sa.Integer(),
                                             nullable=False, server_default="0"))
        logger.info("supplier_auto_assign: added suppliers.total_delivered")

    if "total_failed" not in existing_cols:
        op.add_column("suppliers", sa.Column("total_failed", sa.Integer(),
                                             nullable=False, server_default="0"))
        logger.info("supplier_auto_assign: added suppliers.total_failed")

    if "last_activity" not in existing_cols:
        op.add_column("suppliers", sa.Column("last_activity", sa.DateTime(),
                                             nullable=True))
        logger.info("supplier_auto_assign: added suppliers.last_activity")

    # ── 2. Create supplier_products table ──────────────────────────────────
    existing_tables = inspector.get_table_names()
    if "supplier_products" not in existing_tables:
        op.create_table(
            "supplier_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("supplier_id", sa.Integer(),
                      sa.ForeignKey("suppliers.id"), nullable=False, index=True),
            sa.Column("product_id", sa.Integer(),
                      sa.ForeignKey("products.id"), nullable=False, index=True),
            sa.Column("variant_id", sa.Integer(),
                      sa.ForeignKey("product_variants.id"), nullable=True, index=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("is_auto_assign", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("max_daily_qty", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("supplier_id", "product_id", "variant_id",
                                name="uq_supplier_product_variant"),
        )
        logger.info("supplier_auto_assign: created supplier_products table")

    # Create index on priority for fast ordering
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("supplier_products")} \
        if "supplier_products" in inspector.get_table_names() else set()
    if "ix_supplier_products_priority" not in existing_indexes:
        try:
            op.create_index("ix_supplier_products_priority",
                            "supplier_products", ["priority"])
        except Exception:
            pass  # Already exists in some DBs

    # ── 3. Seed BotConfig rows ─────────────────────────────────────────────
    for key, vtype, default in _NEW_BOT_CONFIG_KEYS:
        existing = bind.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"),
            {"k": key},
        ).fetchone()
        if not existing:
            bind.execute(
                sa.text(
                    "INSERT INTO bot_config (key, value_type, value, category) "
                    "VALUES (:k, :vt, :v, 'inventory')"
                ),
                {"k": key, "vt": vtype, "v": default},
            )
            logger.info("supplier_auto_assign: seeded BotConfig key %r", key)
        else:
            logger.info("supplier_auto_assign: BotConfig key %r already exists — skip", key)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Remove BotConfig rows
    for key, _, _ in _NEW_BOT_CONFIG_KEYS:
        bind.execute(
            sa.text("DELETE FROM bot_config WHERE key = :k"),
            {"k": key},
        )
        logger.info("supplier_auto_assign: removed BotConfig key %r", key)

    # Drop table
    if "supplier_products" in inspector.get_table_names():
        op.drop_table("supplier_products")
        logger.info("supplier_auto_assign: dropped supplier_products table")

    # Remove columns from suppliers
    existing_cols = {col["name"] for col in inspector.get_columns("suppliers")}
    for col in ("priority", "total_delivered", "total_failed", "last_activity"):
        if col in existing_cols:
            op.drop_column("suppliers", col)
            logger.info("supplier_auto_assign: dropped suppliers.%s", col)
