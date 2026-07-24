"""Migration v14 (NOWPayments + ZiniPay Gateways): 2 more automated payment
gateways, added alongside Cryptomus/Heleket.

Adds:
  - PaymentMethod.NOWPAYMENTS = "nowpayments" and PaymentMethod.ZINIPAY =
    "zinipay" to the `paymentmethod` enum used by `transactions.payment_method`.
    On PostgreSQL this enum is a native TYPE and needs an explicit
    `ALTER TYPE ... ADD VALUE` (see migrations/v12_cryptomus_gateway.py for
    the full rationale). SQLite has no native enum type, so nothing extra is
    needed there.
  - `payment_gateway_configs.secondary_key` column — generic 2nd secret slot
    (used by NOWPayments for its IPN Secret; also handled automatically by
    database/db.py's schema auto-heal, kept here for explicit production
    rollouts on an existing PostgreSQL database).
  - The "nowpayments" and "zinipay" rows themselves in
    `payment_gateway_configs` (idempotent get-or-create), so the admin panel
    and services/nowpayments_payment.py / services/zinipay_payment.py have
    something to read/write from the first run.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v14_nowpayments_zinipay_gateways
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect, text

from database.db import engine
from database import Base, get_db_session  # ensures all models are imported
from database.models import PaymentGatewayConfig

logger = logging.getLogger(__name__)

ENUM_TYPE_NAME = "paymentmethod"  # SQLAlchemy's default native enum type name
NEW_ENUM_VALUES = ("nowpayments", "zinipay")


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

    # ── 1. Native enum values (PostgreSQL only) ─────────────────────────
    if dialect == "postgresql":
        _add_postgres_enum_values()
    else:
        print(f"• Dialect '{dialect}' has no native enum type — skipping ALTER TYPE step.")

    # ── 2. payment_gateway_configs.secondary_key column ──────────────────
    with engine.begin() as conn:
        if "payment_gateway_configs" not in inspector.get_table_names():
            print("⚠ payment_gateway_configs table missing — run app once to create schema first.")
            return

        inspector = inspect(engine)
        if not _has_column(inspector, "payment_gateway_configs", "secondary_key"):
            print("• Adding payment_gateway_configs.secondary_key …")
            _add_column(conn, "payment_gateway_configs", "secondary_key", "VARCHAR(255)")

    # ── 3. Ensure the "nowpayments" and "zinipay" rows exist ─────────────
    with get_db_session() as session:
        for gateway in ("nowpayments", "zinipay"):
            row = session.query(PaymentGatewayConfig).filter_by(gateway=gateway).first()
            if not row:
                print(f"• Creating '{gateway}' row in payment_gateway_configs …")
                row = PaymentGatewayConfig(gateway=gateway, is_enabled=False)
                session.add(row)
        session.commit()

    print("[OK] v14 NOWPayments + ZiniPay gateway migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
