"""Database connection and session management.

Production database: Supabase PostgreSQL, selected via the ``DATABASE_URL``
environment variable. Turso/libSQL support has been fully removed — this is
now the ONLY place that builds a SQLAlchemy engine for the app, and
alembic/env.py reuses ``resolve_database_target()`` below so there is a
single source of truth for how the app decides which database to talk to.

A local SQLite file (``sqlite:///bot_database.db``) remains available as a
zero-config *local development* fallback only — set via the same
``DATABASE_URL`` variable. It is unrelated to the Turso migration and needs
no cloud credentials.
"""

import logging
import os
import time
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager
from config.settings import settings
from database.models import Base

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    """Normalize legacy ``postgres://`` URLs (as issued by many hosts,
    including Supabase/Heroku-style connection strings) to the
    ``postgresql://`` scheme SQLAlchemy/psycopg2 expect."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def resolve_database_target():
    """Decide which database to connect to and how.

    Returns a tuple: (sqlalchemy_url, connect_args, is_sqlite_family)

    - sqlalchemy_url: the URL to hand to create_engine() / Alembic
    - connect_args: dict of DBAPI connect_args
    - is_sqlite_family: True only for the local SQLite dev fallback. Used to
      pick SQLite-only engine options (StaticPool/check_same_thread) and
      Alembic's batch mode, neither of which apply to PostgreSQL.
    """
    # Read DATABASE_URL directly from the environment (with the same
    # sqlite fallback default as config.settings.Settings) rather than via
    # the cached `settings.DATABASE_URL` class attribute. `settings` is a
    # module-level singleton computed once at first import of
    # config.settings, so anything that later reassigns
    # os.environ["DATABASE_URL"] (as the test suite's reload-based
    # per-test DB isolation does) would otherwise have NO effect here —
    # `resolve_database_target()`/`_build_engine()` would keep silently
    # reconnecting to whatever DATABASE_URL happened to be set the first
    # time this process ever imported config.settings. In a deployment
    # where DATABASE_URL already points at the real production Postgres
    # (the normal case), that meant `pytest` could end up running against
    # the live database instead of the intended isolated SQLite fixture.
    # SUPABASE_DB_URL takes priority over Replit-managed DATABASE_URL
    db_url = _normalize_url(
        (os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL", "sqlite:///bot_database.db")).strip()
    )
    is_sqlite = db_url.startswith("sqlite")

    if is_sqlite:
        # Local dev only. check_same_thread=False + StaticPool let the same
        # SQLite file be shared across the threads asyncio.to_thread() spins
        # up for DB work.
        return db_url, {"check_same_thread": False}, True

    return db_url, {}, False


def _build_engine():
    url, connect_args, is_sqlite_family = resolve_database_target()

    if is_sqlite_family:
        engine = create_engine(
            url,
            echo=False,
            connect_args=connect_args,
            poolclass=StaticPool,
        )
        return engine, True

    # PostgreSQL (Supabase) — production-safe pool sized for a Telegram bot
    # process with many concurrent users but a modest number of *simultaneous*
    # DB round trips (Telegram itself rate-limits how fast updates arrive).
    # pool_pre_ping avoids handing out dead connections after Supabase idles
    # one out; pool_recycle proactively retires connections before typical
    # pooler-side idle timeouts (e.g. Supavisor) can kill them from under us.
    pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "5"))
    pool_timeout = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "300"))

    logger.info(
        "Connecting to PostgreSQL at %s (pool_size=%s, max_overflow=%s)",
        url.split("@")[-1] if "@" in url else "<hidden>",
        pool_size, max_overflow,
    )

    engine = create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )
    return engine, False


# Create database engine
engine, IS_SQLITE_FAMILY = _build_engine()

# Create session factory.
# expire_on_commit=False keeps loaded attributes usable AFTER commit within
# the same ``with get_db_session()`` block. Callers still MUST NOT hold ORM
# instances past the ``with`` scope (session closes on exit); copy the
# scalar fields you need into a plain dict before leaving the block.
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Session = scoped_session(SessionFactory)


# ---------------------------------------------------------------------------
# Idempotent schema drift auto-fix — LOCAL SQLITE DEV ONLY.
# ---------------------------------------------------------------------------
# create_all only adds MISSING tables — it never adds new COLUMNS to existing
# tables. This scan compares live schema vs. the ORM metadata and issues
# plain ``ALTER TABLE ... ADD COLUMN`` statements for anything missing.
#
# For PostgreSQL production this is intentionally DISABLED: schema changes
# there must go through Alembic migrations (see alembic/versions/), not an
# automatic ALTER TABLE run on every startup. It remains available for the
# local SQLite dev fallback where Alembic's batch mode + ad-hoc iteration is
# less practical. To force it on for PostgreSQL anyway (e.g. a one-off
# recovery), set ENABLE_SCHEMA_AUTOFIX=true — not recommended for normal use.
# ---------------------------------------------------------------------------

def _autofix_missing_columns() -> None:
    if not IS_SQLITE_FAMILY and os.getenv("ENABLE_SCHEMA_AUTOFIX", "").strip().lower() not in ("1", "true", "yes"):
        logger.info("Schema auto-fix skipped (PostgreSQL production — managed by Alembic).")
        return

    try:
        insp = inspect(engine)
        live_tables = set(insp.get_table_names())

        for table_name, table in Base.metadata.tables.items():
            if table_name not in live_tables:
                continue

            live_cols = {c["name"] for c in inspect(engine).get_columns(table_name)}

            for col in table.columns:
                if col.name in live_cols:
                    continue

                coltype = col.type.compile(dialect=engine.dialect)
                default_sql = ""

                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default_sql = f" DEFAULT {'TRUE' if val else 'FALSE'}"
                    elif isinstance(val, (int, float)):
                        default_sql = f" DEFAULT {val}"
                    elif isinstance(val, str):
                        escaped = val.replace("'", "''")
                        default_sql = f" DEFAULT '{escaped}'"

                if_not_exists = "" if IS_SQLITE_FAMILY else "IF NOT EXISTS "
                stmt = (
                    f'ALTER TABLE "{table_name}" '
                    f'ADD COLUMN {if_not_exists}"{col.name}" '
                    f'{coltype}{default_sql}'
                )

                try:
                    with engine.begin() as conn:
                        conn.execute(text(stmt))
                    logger.warning(
                        "Schema auto-fix: added %s.%s",
                        table_name,
                        col.name,
                    )
                except Exception as e:
                    logger.warning(
                        "Schema auto-fix skipped %s.%s: %s",
                        table_name,
                        col.name,
                        e,
                    )

    except Exception:
        logger.exception("Schema auto-fix scan failed")


def init_db():
    """Initialize the database by creating all tables and (SQLite dev only)
    healing column drift. On PostgreSQL, schema is expected to already be
    current via ``alembic upgrade head`` — create_all is still safe/idempotent
    to run here since it only creates missing tables, never touches existing
    ones."""
    t0 = time.monotonic()
    Base.metadata.create_all(engine)
    _autofix_missing_columns()
    logger.info("Database tables created / verified in %.3fs", time.monotonic() - t0)
    print("[OK] Database tables created / verified successfully")


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
