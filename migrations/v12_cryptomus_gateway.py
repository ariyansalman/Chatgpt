"""Migration v12 (Cryptomus Gateway): USDT/crypto payments via Cryptomus.

Adds:
  - PaymentMethod.CRYPTOMUS = "cryptomus" to the `paymentmethod` enum used by
    `transactions.payment_method`. On PostgreSQL this enum is a native TYPE,
    and Postgres does NOT let you add a value to an existing native enum
    type via plain DDL column changes — it needs an explicit
    `ALTER TYPE ... ADD VALUE`. SQLite has no native enum type (SQLAlchemy
    falls back to a plain VARCHAR there), so nothing extra is needed on
    SQLite; this step is skipped automatically.
  - `payment_gateway_configs.merchant_uuid` / `.api_key` columns (also
    handled by database/db.py's auto-heal, kept here for parity / explicit
    production rollouts — see migrations/v9_subscription_billing.py).
  - The "cryptomus" row itself in `payment_gateway_configs` (idempotent
    get-or-create), so the admin panel and services/cryptomus_payment.py
    have something to read/write from the first run.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.
Note: `database/db.py` also auto-heals missing COLUMNS on every app start,
but it can't fix enum-value drift on PostgreSQL — that's what step 1 here
is for. Running this manually is required on an EXISTING PostgreSQL
database; a fresh `create_all()` (new install) already includes "cryptomus"
in the native type and needs no migration at all.

Usage:
    python -m migrations.v12_cryptomus_gateway
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect, text

from database.db import engine
from database import Base, get_db_session  # ensures all models are imported
from database.models import PaymentGatewayConfig

logger = logging.getLogger(__name__)

ENUM_TYPE_NAME = "paymentmethod"  # SQLAlchemy's default native enum type name
NEW_ENUM_VALUE = "cryptomus"


def _has_column(inspector, table, column) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_column(conn, table, column, sql_type, default_sql=None):
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
    conn.execute(text(ddl))


def _add_postgres_enum_value() -> None:
    """ALTER TYPE ... ADD VALUE must run outside an open transaction block
    on PostgreSQL (it's non-transactional DDL pre-PG12, and even on PG12+ the
    new label can't be used in the same transaction it was added in) — so
    this uses its own autocommit connection, separate from the
    `engine.begin()` block used for the plain column changes below.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(
                f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{NEW_ENUM_VALUE}'"
            ))
            print(f"• Added '{NEW_ENUM_VALUE}' to {ENUM_TYPE_NAME} enum (or already present).")
        except Exception as e:
            # Older PostgreSQL (<12) doesn't support "IF NOT EXISTS" on
            # ADD VALUE — retry without it and treat "already exists" as OK.
            try:
                conn.execute(text(
                    f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE '{NEW_ENUM_VALUE}'"
                ))
                print(f"• Added '{NEW_ENUM_VALUE}' to {ENUM_TYPE_NAME} enum.")
            except Exception as e2:
                msg = str(e2).lower()
                if "already exists" in msg or "duplicate" in msg:
                    print(f"• '{NEW_ENUM_VALUE}' already present in {ENUM_TYPE_NAME} enum, skipping.")
                else:
                    logger.warning(
                        "Could not add '%s' to %s enum (%s / %s) — if this "
                        "is a fresh database the type may not exist yet under "
                        "that name; safe to ignore on first install.",
                        NEW_ENUM_VALUE, ENUM_TYPE_NAME, e, e2,
                    )


def run():
    inspector = inspect(engine)
    dialect = engine.dialect.name

    # ── 1. Native enum value (PostgreSQL only) ──────────────────────────
    if dialect == "postgresql":
        _add_postgres_enum_value()
    else:
        print(f"• Dialect '{dialect}' has no native enum type — skipping ALTER TYPE step.")

    # ── 2. payment_gateway_configs.merchant_uuid / .api_key columns ─────
    with engine.begin() as conn:
        if "payment_gateway_configs" not in inspector.get_table_names():
            print("⚠ payment_gateway_configs table missing — run app once to create schema first.")
            return

        varchar_uuid = "VARCHAR(120)"
        varchar_key = "VARCHAR(255)"
        for name, sql_type in (("merchant_uuid", varchar_uuid), ("api_key", varchar_key)):
            inspector = inspect(engine)  # refresh after each DDL
            if not _has_column(inspector, "payment_gateway_configs", name):
                print(f"• Adding payment_gateway_configs.{name} …")
                _add_column(conn, "payment_gateway_configs", name, sql_type)

    # ── 3. Ensure the "cryptomus" row exists ────────────────────────────
    with get_db_session() as session:
        row = session.query(PaymentGatewayConfig).filter_by(gateway="cryptomus").first()
        if not row:
            print("• Creating 'cryptomus' row in payment_gateway_configs …")
            row = PaymentGatewayConfig(gateway="cryptomus", is_enabled=False)
            session.add(row)
            session.commit()

    print("[OK] v12 Cryptomus gateway migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
