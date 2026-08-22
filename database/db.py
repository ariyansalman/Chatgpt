"""Database connection and session management."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from contextlib import contextmanager
from config.settings import settings
from database.models import Base

# Create database engine.
#
# pool_pre_ping=True: issues a lightweight "SELECT 1" before handing out a
# pooled connection, and transparently reconnects if it's dead. This matters
# a lot on Supabase — it closes idle Postgres connections server-side after
# a few minutes, and this bot is a long-running worker that can easily go
# that long between DB hits (e.g. overnight with no orders).
#
# pool_recycle=280: proactively recycle connections before ~5 minutes, under
# most managed Postgres idle-timeout thresholds, as a second line of defense.
#
# These options are harmless no-ops for SQLite (used in local dev).
engine_kwargs = {"echo": False}
if not settings.DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update(pool_pre_ping=True, pool_recycle=280)

engine = create_engine(settings.DATABASE_URL, **engine_kwargs)

# Create session factory
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)


def init_db():
    """Initialize the database by creating all tables."""
    Base.metadata.create_all(engine)
    print("[OK] Database tables created successfully")
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Add columns/enum values introduced after initial deploy.

    Base.metadata.create_all() only creates tables that don't exist yet — it
    never ALTERs an existing table's columns, and on Postgres it never adds
    values to an existing native ENUM type. This patches both gaps for the
    Binance Pay / Bybit Pay addition, safely and idempotently (safe to run on
    every startup, on a fresh DB, or on one that's already migrated).
    """
    from sqlalchemy import inspect, text

    is_sqlite = settings.DATABASE_URL.startswith("sqlite")
    inspector = inspect(engine)

    # Add transactions.external_reference if missing.
    columns = {c["name"] for c in inspector.get_columns("transactions")}
    if "external_reference" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE transactions ADD COLUMN external_reference VARCHAR(255)"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_external_reference "
                "ON transactions (external_reference)"
            ))
        print("[OK] Migrated: transactions.external_reference added")

    # Add new PaymentMethod enum values on Postgres (SQLite stores the Enum
    # as a plain VARCHAR + CHECK constraint that SQLAlchemy re-derives from
    # the Python enum on every run, so no action is needed there).
    if not is_sqlite:
        # ALTER TYPE ... ADD VALUE cannot run inside a multi-statement
        # transaction block on Postgres, so use an autocommit connection.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Postgres can't parameterize ALTER TYPE ... ADD VALUE; both values
            # come from this fixed tuple, never from user input.
            for value in ("binance_pay", "bybit_pay"):
                conn.execute(text(f"ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS '{value}'"))
            for value in ("bkash_nagad",):
                conn.execute(text(f"ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS '{value}'"))


@contextmanager
def get_db_session():
    """Provide a transactional scope for database operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
