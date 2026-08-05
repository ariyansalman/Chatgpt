"""Migration v3: add manual payment methods + expand transactions for manual proof.

Adds:
  - manual_payment_methods table
  - transactions.manual_method_id (nullable FK, no strict FK for sqlite portability)
  - transactions.proof            (text, nullable)
  - transactions.admin_note       (text, nullable)

Safe to run multiple times (uses IF NOT EXISTS / column checks).
Usage:
    python -m migrations.v3_add_manual_payments
"""

from sqlalchemy import inspect, text
from database.db import engine


def _has_column(inspector, table, column):
    return any(c["name"] == column for c in inspector.get_columns(table))


def run():
    inspector = inspect(engine)
    dialect = engine.dialect.name

    with engine.begin() as conn:
        # 1) manual_payment_methods table
        if "manual_payment_methods" not in inspector.get_table_names():
            print("• Creating manual_payment_methods table…")
            if dialect == "sqlite":
                conn.execute(text("""
                    CREATE TABLE manual_payment_methods (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(120) NOT NULL,
                        emoji VARCHAR(12) DEFAULT '💳',
                        instructions TEXT NOT NULL,
                        min_amount FLOAT DEFAULT 0.0,
                        is_active BOOLEAN DEFAULT 1,
                        sort_order INTEGER DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            else:
                conn.execute(text("""
                    CREATE TABLE manual_payment_methods (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(120) NOT NULL,
                        emoji VARCHAR(12) DEFAULT '💳',
                        instructions TEXT NOT NULL,
                        min_amount DOUBLE PRECISION DEFAULT 0.0,
                        is_active BOOLEAN DEFAULT TRUE,
                        sort_order INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
        else:
            print("• manual_payment_methods already exists — skipping.")

        # 2) transactions new columns
        inspector = inspect(engine)  # refresh
        if "transactions" in inspector.get_table_names():
            if not _has_column(inspector, "transactions", "manual_method_id"):
                print("• Adding transactions.manual_method_id …")
                conn.execute(text("ALTER TABLE transactions ADD COLUMN manual_method_id INTEGER"))
            if not _has_column(inspector, "transactions", "proof"):
                print("• Adding transactions.proof …")
                conn.execute(text("ALTER TABLE transactions ADD COLUMN proof TEXT"))
            if not _has_column(inspector, "transactions", "admin_note"):
                print("• Adding transactions.admin_note …")
                conn.execute(text("ALTER TABLE transactions ADD COLUMN admin_note TEXT"))
        else:
            print("⚠ transactions table missing — run app once to create schema.")

    print("✅ Migration v3 complete.")


if __name__ == "__main__":
    run()
