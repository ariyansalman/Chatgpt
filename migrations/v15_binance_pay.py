"""Migration v15 (Binance Pay Gateway): adds the Binance Pay automated
payment gateway, verified via the normal Binance HMAC API's transaction
history (GET /sapi/v1/pay/transactions) — NOT the Binance Pay Merchant API.

Adds:
  - PaymentMethod.BINANCE_PAY = "binance_pay" to the `paymentmethod` enum
    used by `transactions.payment_method`. On PostgreSQL this enum is a
    native TYPE and needs an explicit `ALTER TYPE ... ADD VALUE` (see
    migrations/v12_cryptomus_gateway.py for the full rationale). SQLite has
    no native enum type, so nothing extra is needed there.
  - `payment_gateway_configs.binance_pay_id` / `binance_allowed_currencies` /
    `binance_min_amount` / `binance_max_amount` / `binance_order_expiry_minutes` /
    `binance_bonus_percent` / `binance_instructions` columns — admin-panel
    display/limit settings only. The Binance API Key/Secret are NEVER stored
    here; they come from the BINANCE_API_KEY / BINANCE_API_SECRET
    environment variables only (config/settings.py, services/binance_pay.py).
    Also handled automatically by database/db.py's schema auto-heal on
    every app start; kept here for explicit production rollouts on an
    existing PostgreSQL database.
  - `binance_pay_transactions` table — verified Binance Pay transactions,
    with a UNIQUE constraint on `transaction_id` for duplicate-payment
    protection. Created automatically by `Base.metadata.create_all()` on
    every app start; this migration is a no-op for it on databases that
    already ran `init_db()` once, and only matters for tooling that calls
    migrations directly without ever calling `init_db()`.
  - The "binance_pay" row itself in `payment_gateway_configs` (idempotent
    get-or-create), so the admin panel and services/binance_pay.py have
    something to read/write from the first run.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v15_binance_pay
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect, text

from database.db import engine
from database import Base, get_db_session  # ensures all models are imported
from database.models import PaymentGatewayConfig

logger = logging.getLogger(__name__)

ENUM_TYPE_NAME = "paymentmethod"  # SQLAlchemy's default native enum type name
NEW_ENUM_VALUES = ("binance_pay",)

NEW_GATEWAY_CONFIG_COLUMNS = (
    ("binance_pay_id", "VARCHAR(64)", None),
    ("binance_allowed_currencies", "VARCHAR(120)", "'USDT,USDC'"),
    ("binance_min_amount", "FLOAT", None),
    ("binance_max_amount", "FLOAT", None),
    ("binance_order_expiry_minutes", "INTEGER", "60"),
    ("binance_bonus_percent", "FLOAT", "0.0"),
    ("binance_instructions", "TEXT", None),
)


def _has_column(inspector, table, column) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_column(conn, table, column, sql_type, default_sql=None):
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
    conn.execute(text(ddl))


def _add_postgres_enum_values() -> None:
    """ALTER TYPE ... ADD VALUE must run outside an open transaction block
    on PostgreSQL — see migrations/v12_cryptomus_gateway.py for details."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for value in NEW_ENUM_VALUES:
            try:
                conn.execute(text(
                    f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{value}'"
                ))
                print(f"• Added '{value}' to {ENUM_TYPE_NAME} enum (or already present).")
            except Exception as e:
                try:
                    conn.execute(text(f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE '{value}'"))
                    print(f"• Added '{value}' to {ENUM_TYPE_NAME} enum.")
                except Exception as e2:
                    msg = str(e2).lower()
                    if "already exists" in msg or "duplicate" in msg:
                        print(f"• '{value}' already present in {ENUM_TYPE_NAME} enum, skipping.")
                    else:
                        logger.warning(
                            "Could not add '%s' to %s enum (%s / %s) — if this "
                            "is a fresh database the type may not exist yet under "
                            "that name; safe to ignore on first install.",
                            value, ENUM_TYPE_NAME, e, e2,
                        )


def run():
    inspector = inspect(engine)
    dialect = engine.dialect.name

    # ── 1. Native enum value (PostgreSQL only) ──────────────────────────
    if dialect == "postgresql":
        _add_postgres_enum_values()
    else:
        print(f"• Dialect '{dialect}' has no native enum type — skipping ALTER TYPE step.")

    # ── 2. New payment_gateway_configs columns ───────────────────────────
    with engine.begin() as conn:
        if "payment_gateway_configs" not in inspector.get_table_names():
            print("⚠ payment_gateway_configs table missing — run app once to create schema first.")
            return

        inspector = inspect(engine)
        for column, sql_type, default_sql in NEW_GATEWAY_CONFIG_COLUMNS:
            if not _has_column(inspector, "payment_gateway_configs", column):
                print(f"• Adding payment_gateway_configs.{column} …")
                _add_column(conn, "payment_gateway_configs", column, sql_type, default_sql)

    # ── 3. binance_pay_transactions table (create_all covers new installs) ──
    Base.metadata.tables["binance_pay_transactions"].create(bind=engine, checkfirst=True)

    # ── 4. Ensure the "binance_pay" row exists ────────────────────────────
    with get_db_session() as session:
        row = session.query(PaymentGatewayConfig).filter_by(gateway="binance_pay").first()
        if not row:
            print("• Creating 'binance_pay' row in payment_gateway_configs …")
            row = PaymentGatewayConfig(
                gateway="binance_pay", is_enabled=False,
                binance_allowed_currencies="USDT,USDC",
                binance_order_expiry_minutes=60,
                binance_bonus_percent=0.0,
            )
            session.add(row)
        session.commit()

    print("[OK] v15 Binance Pay gateway migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
