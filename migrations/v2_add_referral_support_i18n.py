"""One-shot migration script to add V2 columns/tables to an existing DB.

Run once after upgrading the code:
    python -m migrations.v2_add_referral_support_i18n

Safe to re-run: uses IF NOT EXISTS / column-exists checks.
Works for SQLite (default) and PostgreSQL. Detects the dialect from the engine.
"""

from sqlalchemy import inspect, text
from database.db import engine
from database import Base  # noqa: F401  ensures models are registered


def _has_column(inspector, table: str, column: str) -> bool:
    try:
        cols = [c["name"] for c in inspector.get_columns(table)]
        return column in cols
    except Exception:
        return False


def run():
    insp = inspect(engine)
    dialect = engine.dialect.name
    print(f"[migrate] dialect={dialect}")

    with engine.begin() as conn:
        # 1) Upgrade users.telegram_id to BIGINT (Postgres only; SQLite is dynamically typed)
        if dialect == "postgresql":
            try:
                conn.execute(text("ALTER TABLE users ALTER COLUMN telegram_id TYPE BIGINT"))
                print("[migrate] users.telegram_id -> BIGINT")
            except Exception as e:
                print(f"[migrate] telegram_id alter skipped: {e}")

        # 2) Add new user columns
        additions = [
            ("users", "language", "VARCHAR(8) DEFAULT 'en'"),
            ("users", "referred_by_id", "INTEGER"),
            ("users", "referral_earnings", "FLOAT DEFAULT 0"),
            ("users", "has_purchased", "BOOLEAN DEFAULT 0" if dialect == "sqlite" else "BOOLEAN DEFAULT FALSE"),
            ("settings", "referral_reward_amount", "FLOAT DEFAULT 0.10"),
            ("settings", "referral_required_channel", "VARCHAR(255)"),
            ("settings", "referral_enabled", "BOOLEAN DEFAULT 1" if dialect == "sqlite" else "BOOLEAN DEFAULT TRUE"),
        ]
        for table, col, ddl in additions:
            if _has_column(insp, table, col):
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                print(f"[migrate] +{table}.{col}")
            except Exception as e:
                print(f"[migrate] failed to add {table}.{col}: {e}")

    # 3) Create the new tables via SQLAlchemy metadata (idempotent)
    Base.metadata.create_all(bind=engine)
    print("[migrate] create_all done (referral_rewards, support_tickets, ticket_messages)")


if __name__ == "__main__":
    run()
