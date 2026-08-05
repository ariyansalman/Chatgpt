"""Migration v16 (Bybit Pay Gateway): adds the Bybit Pay automated payment
gateway, verified via the official Bybit V5 REST API — UID (Internal)
Transfer via GET /v5/asset/deposit/query-internal-record, and on-chain
deposit (USDT TRC20/BEP20/ERC20) via GET /v5/asset/deposit/query-record.

Adds:
  - PaymentMethod.BYBIT_PAY = "bybit_pay" to the `paymentmethod` enum used
    by `transactions.payment_method`. On PostgreSQL this enum is a native
    TYPE and needs an explicit `ALTER TYPE ... ADD VALUE` (see
    migrations/v12_cryptomus_gateway.py for the full rationale). SQLite has
    no native enum type, so nothing extra is needed there.
  - `payment_gateway_configs.bybit_uid` / `bybit_wallet_trc20` /
    `bybit_wallet_bep20` / `bybit_wallet_erc20` / `bybit_allowed_networks` /
    `bybit_min_amount` / `bybit_max_amount` / `bybit_order_expiry_minutes` /
    `bybit_bonus_percent` / `bybit_instructions` columns — admin-panel
    display/limit/wallet-address settings only. The Bybit API Key/Secret are
    NEVER stored here; they come from the BYBIT_API_KEY / BYBIT_API_SECRET
    environment variables only (config/settings.py, services/bybit_pay.py).
    Also handled automatically by database/db.py's schema auto-heal on
    every app start (SQLite dev); kept here for explicit production
    rollouts on an existing PostgreSQL database.
  - `bybit_pay_transactions` table — verified Bybit Pay transactions
    (UID Transfer or on-chain deposit), with a UNIQUE constraint on
    `transaction_id` for duplicate-payment protection. Created automatically
    by `Base.metadata.create_all()` on every app start; this migration is a
    no-op for it on databases that already ran `init_db()` once, and only
    matters for tooling that calls migrations directly without ever calling
    `init_db()`.
  - The "bybit_pay" row itself in `payment_gateway_configs` (idempotent
    get-or-create), so the admin panel and services/bybit_pay.py have
    something to read/write from the first run.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v16_bybit_pay
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect, text

from database.db import engine
from database import Base, get_db_session  # ensures all models are imported
from database.models import PaymentGatewayConfig

logger = logging.getLogger(__name__)

ENUM_TYPE_NAME = "paymentmethod"  # SQLAlchemy's default native enum type name
NEW_ENUM_VALUES = ("bybit_pay",)

NEW_GATEWAY_CONFIG_COLUMNS = (
    ("bybit_uid", "VARCHAR(64)", None),
    ("bybit_wallet_trc20", "VARCHAR(255)", None),
    ("bybit_wallet_bep20", "VARCHAR(255)", None),
    ("bybit_wallet_erc20", "VARCHAR(255)", None),
    ("bybit_allowed_networks", "VARCHAR(64)", "'TRC20,BEP20,ERC20'"),
    ("bybit_min_amount", "FLOAT", None),
    ("bybit_max_amount", "FLOAT", None),
    ("bybit_order_expiry_minutes", "INTEGER", "60"),
    ("bybit_bonus_percent", "FLOAT", "0.0"),
    ("bybit_instructions", "TEXT", None),
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

    # ── 3. bybit_pay_transactions table (create_all covers new installs) ──
    Base.metadata.tables["bybit_pay_transactions"].create(bind=engine, checkfirst=True)

    # ── 4. Ensure the "bybit_pay" row exists ────────────────────────────
    with get_db_session() as session:
        row = session.query(PaymentGatewayConfig).filter_by(gateway="bybit_pay").first()
        if not row:
            print("• Creating 'bybit_pay' row in payment_gateway_configs …")
            row = PaymentGatewayConfig(
                gateway="bybit_pay", is_enabled=False,
                bybit_allowed_networks="TRC20,BEP20,ERC20",
                bybit_order_expiry_minutes=60,
                bybit_bonus_percent=0.0,
            )
            session.add(row)
        session.commit()

    print("[OK] v16 Bybit Pay gateway migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
