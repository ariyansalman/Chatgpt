"""giftcardtype_enum: Ensure giftcardtype PostgreSQL enum is complete and
gift_cards.card_type column uses it correctly.

Revision ID: 20260811_giftcardtype_enum
Revises: 20260811_v21_six_features

Background
----------
Migration 20260809_sales_marketing created gift_cards.card_type as VARCHAR(16)
with server_default='fixed'.  The SQLAlchemy ORM model declares the same
column as Column(Enum(GiftCardType), ...), which maps to a native PostgreSQL
ENUM type named ``giftcardtype`` (values: fixed, percent, custom).

When init_db() calls Base.metadata.create_all() on startup, SQLAlchemy
attempts to reconcile the native enum type with the running database.  If
``giftcardtype`` does not exist, or exists but is missing the expected label
values, Postgres raises:

    psycopg2.errors.InvalidTextRepresentation:
        invalid input value for enum giftcardtype: "fixed"

This migration fixes the problem permanently:
  1. Creates the ``giftcardtype`` enum type with all three labels if absent.
  2. Adds any missing label values if the type already exists.
  3. Converts gift_cards.card_type from VARCHAR(16) → giftcardtype if needed,
     using a USING cast that preserves every existing row.
  4. Restores the correct server_default so new rows default to 'fixed'.

All steps are guarded so the migration is fully idempotent and safe to re-run.
"""
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision     = "20260811_giftcardtype_enum"
down_revision = "20260811_v21_six_features"
branch_labels = None
depends_on    = None

# The three labels that GiftCardType defines (values, not names).
GIFTCARDTYPE_LABELS = ["fixed", "percent", "custom"]


def upgrade():
    bind = op.get_bind()

    # This migration is PostgreSQL-only; SQLite stores enums as VARCHAR and
    # is not affected by native enum type issues.
    if bind.dialect.name != "postgresql":
        logger.info("giftcardtype_enum: non-PostgreSQL dialect — skipping.")
        return

    # ── Step 1: Check whether the giftcardtype enum type already exists ──────
    row = bind.execute(sa.text(
        "SELECT 1 FROM pg_type WHERE typname = 'giftcardtype' AND typtype = 'e'"
    )).fetchone()
    enum_exists = row is not None

    # ALTER TYPE … ADD VALUE must run outside an open transaction on Postgres.
    # COMMIT here so the subsequent DDL statements each run in autocommit mode.
    bind.execute(sa.text("COMMIT"))

    if not enum_exists:
        # ── Step 2a: Create the enum type from scratch ────────────────────
        try:
            labels_sql = ", ".join(f"'{v}'" for v in GIFTCARDTYPE_LABELS)
            bind.execute(sa.text(
                f"CREATE TYPE giftcardtype AS ENUM ({labels_sql})"
            ))
            logger.info("giftcardtype_enum: created giftcardtype enum with labels %s",
                        GIFTCARDTYPE_LABELS)
        except Exception as exc:
            # Another process may have created it concurrently — log and
            # continue; the ADD VALUE loop below will ensure completeness.
            logger.warning("giftcardtype_enum: CREATE TYPE giftcardtype: %s", exc)
    else:
        logger.info("giftcardtype_enum: giftcardtype enum already exists — ensuring labels.")

    # ── Step 2b: Add any missing label values ────────────────────────────────
    # Fetch the labels currently registered in the DB.
    existing_rows = bind.execute(sa.text("""
        SELECT enumlabel
        FROM   pg_enum
        JOIN   pg_type ON pg_enum.enumtypid = pg_type.oid
        WHERE  pg_type.typname = 'giftcardtype'
    """)).fetchall()
    existing_labels = {r[0] for r in existing_rows}

    for label in GIFTCARDTYPE_LABELS:
        if label in existing_labels:
            logger.debug("giftcardtype_enum: label '%s' already present — skipping.", label)
            continue
        try:
            bind.execute(sa.text(
                f"ALTER TYPE giftcardtype ADD VALUE IF NOT EXISTS '{label}'"
            ))
            logger.info("giftcardtype_enum: added label '%s' to giftcardtype.", label)
        except Exception as exc:
            logger.warning("giftcardtype_enum: could not add label '%s': %s", label, exc)

    # ── Step 3: Check current column type of gift_cards.card_type ────────────
    col_row = bind.execute(sa.text("""
        SELECT data_type, udt_name
        FROM   information_schema.columns
        WHERE  table_name  = 'gift_cards'
          AND  column_name = 'card_type'
    """)).fetchone()

    if col_row is None:
        # The gift_cards table doesn't exist yet — create_all will handle it
        # correctly now that the enum type is present.
        logger.info("giftcardtype_enum: gift_cards table not found — "
                    "create_all will build it with the correct enum type.")
        return

    data_type = col_row[0]   # e.g. 'character varying' or 'USER-DEFINED'
    udt_name  = col_row[1]   # e.g. 'varchar'            or 'giftcardtype'

    if udt_name == "giftcardtype":
        logger.info("giftcardtype_enum: card_type is already giftcardtype — no column change needed.")
    else:
        # ── Step 4: Convert VARCHAR → giftcardtype ────────────────────────
        # The USING cast converts existing string values ('fixed', 'percent',
        # 'custom') to their enum counterparts.  Rows with any other value
        # would raise here — but the only server_default ever used was 'fixed'
        # and all application paths only write valid GiftCardType values.
        logger.info(
            "giftcardtype_enum: card_type is '%s' (udt=%s) — converting to giftcardtype.",
            data_type, udt_name,
        )
        try:
            bind.execute(sa.text("""
                ALTER TABLE gift_cards
                    ALTER COLUMN card_type TYPE giftcardtype
                    USING card_type::giftcardtype
            """))
            logger.info("giftcardtype_enum: card_type column converted to giftcardtype.")
        except Exception as exc:
            logger.error(
                "giftcardtype_enum: failed to convert card_type to giftcardtype: %s", exc
            )
            raise

    # ── Step 5: Ensure server_default is set correctly ───────────────────────
    try:
        bind.execute(sa.text(
            "ALTER TABLE gift_cards ALTER COLUMN card_type SET DEFAULT 'fixed'"
        ))
        logger.info("giftcardtype_enum: server_default reset to 'fixed'.")
    except Exception as exc:
        logger.warning("giftcardtype_enum: could not reset server_default: %s", exc)


def downgrade():
    """Non-destructive: converting back from enum to VARCHAR would require
    rewriting the table and risks losing data.  This downgrade intentionally
    leaves the schema in its upgraded state, matching the project-wide policy
    for enum migrations."""
    pass
