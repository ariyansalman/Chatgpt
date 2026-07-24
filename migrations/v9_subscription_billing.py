"""Migration v9 (Recurring Subscription Billing): auto-renewal support.

Adds to `subscriptions`:
  - next_billing_date   DATETIME  NULL  (when the next auto-charge is due)
  - billing_cycle_days  INTEGER   NULL  (length of one billing cycle)
  - billing_amount      FLOAT     NULL  (USD charged per cycle)
  - auto_renew          BOOLEAN   NOT NULL DEFAULT 1
  - failed_attempts     INTEGER   NOT NULL DEFAULT 0
  - last_billed_at      DATETIME  NULL
  - last_reminder_at    DATETIME  NULL
  - cancelled_at        DATETIME  NULL
  - cancelled_by        INTEGER   NULL
  - cancel_reason       VARCHAR(255) NULL

Adds to `admin_notification_prefs`:
  - subscription        BOOLEAN   NOT NULL DEFAULT 1

Existing subscription rows backfill with auto_renew=1, failed_attempts=0 and
NULL billing fields (treated as "no recurring cycle configured" by
``services/subscription_service.py`` until the next renewal recalculates
them). Nothing that already worked stops working.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.
Note: `database/db.py` also auto-heals missing columns on every app start,
so running this manually is optional but kept for parity with earlier
migrations (v2-v8) and explicit production rollouts.

Usage:
    python -m migrations.v9_subscription_billing
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
    float_type = "FLOAT" if dialect == "sqlite" else "DOUBLE PRECISION"
    datetime_type = "DATETIME" if dialect == "sqlite" else "TIMESTAMP"
    bool_type = "BOOLEAN" if dialect != "sqlite" else "BOOLEAN"
    varchar255 = "VARCHAR(255)"

    with engine.begin() as conn:
        if "subscriptions" not in inspector.get_table_names():
            print("⚠ subscriptions table missing — run app once to create schema first.")
            return

        cols = [
            ("next_billing_date", datetime_type, None),
            ("billing_cycle_days", "INTEGER", None),
            ("billing_amount", float_type, None),
            ("auto_renew", bool_type, "1"),
            ("failed_attempts", "INTEGER", "0"),
            ("last_billed_at", datetime_type, None),
            ("last_reminder_at", datetime_type, None),
            ("cancelled_at", datetime_type, None),
            ("cancelled_by", "INTEGER", None),
            ("cancel_reason", varchar255, None),
        ]
        for name, sql_type, default_sql in cols:
            inspector = inspect(engine)  # refresh after each DDL
            if not _has_column(inspector, "subscriptions", name):
                print(f"• Adding subscriptions.{name} …")
                _add_column(conn, "subscriptions", name, sql_type, default_sql)

        # Backfill NOT NULL-ish defaults explicitly (some dialects don't
        # apply DEFAULT to pre-existing rows on ADD COLUMN).
        conn.execute(text(
            "UPDATE subscriptions SET auto_renew = 1 WHERE auto_renew IS NULL"
        ))
        conn.execute(text(
            "UPDATE subscriptions SET failed_attempts = 0 WHERE failed_attempts IS NULL"
        ))

        # ── admin_notification_prefs.subscription ───────────────────────
        inspector = inspect(engine)
        if "admin_notification_prefs" in inspector.get_table_names():
            if not _has_column(inspector, "admin_notification_prefs", "subscription"):
                print("• Adding admin_notification_prefs.subscription …")
                _add_column(conn, "admin_notification_prefs", "subscription", bool_type, "1")
                conn.execute(text(
                    "UPDATE admin_notification_prefs SET subscription = 1 "
                    "WHERE subscription IS NULL"
                ))

    print("[OK] v9 subscription billing migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
