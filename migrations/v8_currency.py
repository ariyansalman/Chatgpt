"""Migration v8 (Multi-Currency): add USD/BDT currency support.

Adds to `products`:
  - currency   VARCHAR(3)  NOT NULL  DEFAULT 'USD'
    (currency that `price` / `sale_price` are stored in for this product)

Adds to `orders`:
  - currency   VARCHAR(3)  NOT NULL  DEFAULT 'USD'
    (currency the buyer was viewing at checkout; `total_amount` itself
    always stays the real USD amount debited from the wallet)

Adds to `users`:
  - preferred_currency  VARCHAR(3)  NOT NULL  DEFAULT 'USD'
    (which currency this user wants prices displayed in)

Adds to `settings` (exchange-rate configuration, single-row table):
  - exchange_rate_mode         VARCHAR(8)   NOT NULL  DEFAULT 'fixed'   ('fixed' | 'api')
  - usd_to_bdt_rate            FLOAT        NOT NULL  DEFAULT 110.0
  - exchange_rate_api_url      VARCHAR(500) NULL
  - exchange_rate_last_value   FLOAT        NULL
  - exchange_rate_last_synced  DATETIME     NULL

All existing rows backfill to 'USD' / the fixed default rate, so nothing
that already worked in USD changes behaviour after this migration runs.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v8_currency
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect, text

from database.db import engine
from database import Base  # ensures all models are imported

logger = logging.getLogger(__name__)


def _has_column(inspector, table, column) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_column(conn, table, column, sql_type, default_sql=None):
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
    conn.execute(text(ddl))


def run():
    inspector = inspect(engine)
    dialect = engine.dialect.name
    varchar3 = "VARCHAR(3)"
    varchar8 = "VARCHAR(8)"
    varchar500 = "VARCHAR(500)"
    float_type = "FLOAT" if dialect == "sqlite" else "DOUBLE PRECISION"
    datetime_type = "DATETIME" if dialect == "sqlite" else "TIMESTAMP"

    with engine.begin() as conn:
        # ── products.currency ───────────────────────────────────────────
        if "products" not in inspector.get_table_names():
            print("⚠ products table missing — run app once to create schema first.")
            return
        if not _has_column(inspector, "products", "currency"):
            print("• Adding products.currency …")
            _add_column(conn, "products", "currency", varchar3, "'USD'")
            conn.execute(text(
                "UPDATE products SET currency = 'USD' WHERE currency IS NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_products_currency ON products (currency)"
            ))

        # ── orders.currency ─────────────────────────────────────────────
        inspector = inspect(engine)  # refresh after DDL
        if "orders" not in inspector.get_table_names():
            print("⚠ orders table missing — run app once to create schema first.")
            return
        if not _has_column(inspector, "orders", "currency"):
            print("• Adding orders.currency …")
            _add_column(conn, "orders", "currency", varchar3, "'USD'")
            conn.execute(text(
                "UPDATE orders SET currency = 'USD' WHERE currency IS NULL"
            ))

        # ── users.preferred_currency ────────────────────────────────────
        inspector = inspect(engine)
        if "users" not in inspector.get_table_names():
            print("⚠ users table missing — run app once to create schema first.")
            return
        if not _has_column(inspector, "users", "preferred_currency"):
            print("• Adding users.preferred_currency …")
            _add_column(conn, "users", "preferred_currency", varchar3, "'USD'")
            conn.execute(text(
                "UPDATE users SET preferred_currency = 'USD' WHERE preferred_currency IS NULL"
            ))

        # ── settings: exchange-rate configuration ───────────────────────
        inspector = inspect(engine)
        if "settings" not in inspector.get_table_names():
            print("⚠ settings table missing — run app once to create schema first.")
            return

        if not _has_column(inspector, "settings", "exchange_rate_mode"):
            print("• Adding settings.exchange_rate_mode …")
            _add_column(conn, "settings", "exchange_rate_mode", varchar8, "'fixed'")
            conn.execute(text(
                "UPDATE settings SET exchange_rate_mode = 'fixed' WHERE exchange_rate_mode IS NULL"
            ))

        if not _has_column(inspector, "settings", "usd_to_bdt_rate"):
            print("• Adding settings.usd_to_bdt_rate …")
            _add_column(conn, "settings", "usd_to_bdt_rate", float_type, "110.0")
            conn.execute(text(
                "UPDATE settings SET usd_to_bdt_rate = 110.0 WHERE usd_to_bdt_rate IS NULL"
            ))

        if not _has_column(inspector, "settings", "exchange_rate_api_url"):
            print("• Adding settings.exchange_rate_api_url …")
            _add_column(conn, "settings", "exchange_rate_api_url", varchar500)

        if not _has_column(inspector, "settings", "exchange_rate_last_value"):
            print("• Adding settings.exchange_rate_last_value …")
            _add_column(conn, "settings", "exchange_rate_last_value", float_type)

        if not _has_column(inspector, "settings", "exchange_rate_last_synced"):
            print("• Adding settings.exchange_rate_last_synced …")
            _add_column(conn, "settings", "exchange_rate_last_synced", datetime_type)

    print("✅ Migration v8 (currency) complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
