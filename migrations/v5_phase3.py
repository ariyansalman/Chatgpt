"""Migration v5 (Phase 3): Loyalty program + Reviews.

Adds:
  - users.loyalty_points
  - settings.loyalty_enabled / earn_rate / redeem_rate / min_redeem
  - table: reviews
  - table: loyalty_ledger

Run with:
    python -m migrations.v5_phase3
"""

from sqlalchemy import inspect, text
from database.db import init_db, engine
from database import Base  # ensures all models are imported


def _add_col(conn, table, col_def):
    """SQLite-safe ADD COLUMN if not exists."""
    col_name = col_def.split()[0]
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(table)]
    if col_name in cols:
        print(f"  · {table}.{col_name} already present")
        return
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
    print(f"  ✓ added {table}.{col_name}")


def run():
    init_db()
    print("▶ Migration v5 (Phase 3)")

    with engine.begin() as conn:
        _add_col(conn, "users", "loyalty_points INTEGER DEFAULT 0")
        _add_col(conn, "settings", "loyalty_enabled BOOLEAN DEFAULT 1")
        _add_col(conn, "settings", "loyalty_earn_rate FLOAT DEFAULT 1.0")
        _add_col(conn, "settings", "loyalty_redeem_rate FLOAT DEFAULT 100.0")
        _add_col(conn, "settings", "loyalty_min_redeem INTEGER DEFAULT 100")

    # create_all only creates missing tables — safe.
    Base.metadata.create_all(bind=engine)
    print("  ✓ ensured tables: reviews, loyalty_ledger")
    print("✅ v5 migration complete.")


if __name__ == "__main__":
    run()
