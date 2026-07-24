"""Phase 2 migration — adds:
  - settings.secondary_currency_code / _symbol / _rate
  - coupons table
  - coupon_redemptions table

Safe to run multiple times: uses `IF NOT EXISTS` / column-inspection guards.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text
from database.db import engine
from database.models import Base, Coupon, CouponRedemption  # noqa: F401


def _has_column(inspector, table, column):
    try:
        cols = [c["name"] for c in inspector.get_columns(table)]
        return column in cols
    except Exception:
        return False


def run():
    print("=" * 60)
    print("Phase 2 migration starting…")
    print("=" * 60)

    inspector = inspect(engine)

    # 1) Settings columns
    with engine.begin() as conn:
        for col, ddl in [
            ("secondary_currency_code",   "VARCHAR(8)"),
            ("secondary_currency_symbol", "VARCHAR(8)"),
            ("secondary_currency_rate",   "FLOAT DEFAULT 0.0"),
        ]:
            if "settings" in inspector.get_table_names() and not _has_column(inspector, "settings", col):
                print(f"  + ALTER settings ADD COLUMN {col}")
                conn.execute(text(f"ALTER TABLE settings ADD COLUMN {col} {ddl}"))
            else:
                print(f"  = settings.{col} already present, skipping")

    # 2) Create new tables (coupons, coupon_redemptions)
    print("  + create_all() for new tables…")
    Base.metadata.create_all(bind=engine, tables=[
        Coupon.__table__, CouponRedemption.__table__,
    ])

    print("=" * 60)
    print("Phase 2 migration complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    run()
