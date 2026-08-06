"""Migration v17 (Payment Order Fix): adds TransactionStatus.CANCELLED so a
pending payment order that expires, is cancelled by the user, or is
cancelled/deleted by an admin always lands on one unambiguous terminal
status instead of being left PENDING (which used to block the user from
creating a new order) or scattered across EXPIRED/FAILED.

Adds:
  - TransactionStatus.CANCELLED = "cancelled" to the `transactionstatus`
    enum used by `transactions.status`. On PostgreSQL this enum is a native
    TYPE and needs an explicit `ALTER TYPE ... ADD VALUE` (see
    migrations/v12_cryptomus_gateway.py for the full rationale). SQLite has
    no native enum type, so nothing extra is needed there.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v17_payment_order_cancelled
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from database.db import engine

logger = logging.getLogger(__name__)

ENUM_TYPE_NAME = "transactionstatus"  # SQLAlchemy's default native enum type name
NEW_ENUM_VALUES = ("cancelled",)


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
    dialect = engine.dialect.name

    if dialect == "postgresql":
        _add_postgres_enum_values()
    else:
        print(f"• Dialect '{dialect}' has no native enum type — skipping ALTER TYPE step.")

    print("[OK] v17 payment-order CANCELLED status migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
