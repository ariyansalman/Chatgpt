"""Model <-> live-database schema verification and auto-heal.

This module exists because of one recurring failure mode: a new column gets
added to a SQLAlchemy model in ``database/models.py``, but the running
PostgreSQL database never gets the matching ``ALTER TABLE`` — either because
``alembic upgrade head`` was never run against that environment, or because
a hand-maintained list of "pending migrations" (see ``bot.py``'s
``_apply_pending_migrations``) fell out of sync with the models. The bot
then boots successfully and crashes on the first query that touches the
missing column, e.g.::

    psycopg2.errors.UndefinedColumn: column transactions.verification_in_progress
    does not exist

Everything here is read-only introspection plus strictly additive DDL
(``ADD COLUMN`` / ``CREATE TABLE``, always ``IF NOT EXISTS``). Nothing in
this module ever drops a column, drops/recreates a table, or touches data.

Typical use — from ``bot.py`` at startup, BEFORE the Application is built::

    from database.db import engine
    from database.schema_check import ensure_schema_synced, SchemaOutOfSyncError

    try:
        ensure_schema_synced(engine)
    except SchemaOutOfSyncError as exc:
        print(str(exc))   # clear, actionable message — not a stack trace
        sys.exit(1)        # refuse to start rather than crash later

Can also be run standalone for ops/CI use::

    python -m database.schema_check          # report only, exit 1 if drift
    python -m database.schema_check --fix     # report + auto-heal, then re-check
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from database.models import Base

logger = logging.getLogger(__name__)


@dataclass
class SchemaDiff:
    """Everything the ORM models declare that the live database is missing."""

    missing_tables: list[str] = field(default_factory=list)
    # table_name -> [column_name, ...]
    missing_columns: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Truthy iff there IS drift (i.e. something is missing)."""
        return bool(self.missing_tables or self.missing_columns)

    def total_missing_columns(self) -> int:
        return sum(len(cols) for cols in self.missing_columns.values())

    def describe(self) -> str:
        lines: list[str] = []
        for t in self.missing_tables:
            lines.append(f"  - table `{t}` does not exist")
        for t, cols in self.missing_columns.items():
            for c in cols:
                lines.append(f"  - column `{t}.{c}` does not exist")
        return "\n".join(lines) if lines else "  (no drift)"


class SchemaOutOfSyncError(RuntimeError):
    """Raised when the live DB schema still doesn't match the ORM models
    after every auto-heal attempt has been made. Callers should catch this
    at startup, print ``str(exc)`` (already a complete, human-readable
    report), and refuse to start the application instead of letting it
    crash later on the first query that touches a missing table/column.
    """


def compute_schema_diff(engine: Engine) -> SchemaDiff:
    """Compare ``Base.metadata`` (the ORM models) against the live database.

    Read-only. Safe to call as often as needed (e.g. before AND after an
    auto-heal attempt, to confirm it actually worked).
    """
    diff = SchemaDiff()
    insp = inspect(engine)
    live_tables = set(insp.get_table_names())

    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            diff.missing_tables.append(table_name)
            continue

        live_cols = {c["name"] for c in insp.get_columns(table_name)}
        missing = [col.name for col in table.columns if col.name not in live_cols]
        if missing:
            diff.missing_columns[table_name] = missing

    return diff


def _compile_add_column_sql(engine: Engine, table_name: str, column) -> str:
    """Build an idempotent ``ALTER TABLE ... ADD COLUMN`` statement for one
    missing column, deriving type/default straight from the ORM column so it
    can never drift from what the model actually declares.
    """
    is_sqlite = engine.dialect.name == "sqlite"
    coltype = column.type.compile(dialect=engine.dialect)

    default_sql = ""
    if column.default is not None and getattr(column.default, "is_scalar", False):
        val = column.default.arg
        if isinstance(val, bool):
            default_sql = f" DEFAULT {'TRUE' if val else 'FALSE'}"
        elif isinstance(val, (int, float)):
            default_sql = f" DEFAULT {val}"
        elif isinstance(val, str):
            escaped = val.replace("'", "''")
            default_sql = f" DEFAULT '{escaped}'"

    # A NOT NULL column being added to a table that may already have rows
    # needs a static default so the ADD COLUMN itself doesn't fail against
    # existing rows — mirrors what the hand-written Alembic revisions for
    # this project already do. If we can't resolve one (e.g. a Python-side
    # callable default like `datetime.utcnow`, not a server default), add
    # the column as nullable instead of risking a failed/blocked ALTER —
    # correctness of a NOT NULL constraint here is secondary to the app
    # being able to start at all, and this path only runs when Alembic
    # itself wasn't able to apply the real migration.
    if not column.nullable and default_sql:
        nullable_sql = " NOT NULL"
    elif not column.nullable:
        nullable_sql = ""
        logger.warning(
            "[schema-check] %s.%s is NOT NULL with no static default — "
            "adding as nullable; run `alembic upgrade head` to apply the "
            "real migration and enforce NOT NULL.",
            table_name, column.name,
        )
    else:
        nullable_sql = ""

    if_not_exists = "" if is_sqlite else "IF NOT EXISTS "
    return (
        f'ALTER TABLE "{table_name}" '
        f'ADD COLUMN {if_not_exists}"{column.name}" '
        f'{coltype}{default_sql}{nullable_sql}'
    )


def apply_missing_columns(engine: Engine, diff: SchemaDiff) -> list[str]:
    """Add every missing column via safe, idempotent ``ADD COLUMN`` DDL.

    Never drops or alters an existing column, never touches data beyond the
    server-side default backfill Postgres performs automatically as part of
    ``ADD COLUMN ... DEFAULT ...``. Returns the list of ``table.column``
    entries that were successfully applied.
    """
    applied: list[str] = []
    for table_name, columns in diff.missing_columns.items():
        table = Base.metadata.tables[table_name]
        for col_name in columns:
            column = table.columns[col_name]
            stmt = _compile_add_column_sql(engine, table_name, column)
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                logger.warning("[schema-check] added missing column %s.%s", table_name, col_name)
                applied.append(f"{table_name}.{col_name}")
            except Exception:
                logger.exception("[schema-check] failed to add column %s.%s", table_name, col_name)
    return applied


def create_missing_tables(engine: Engine, diff: SchemaDiff) -> list[str]:
    """Create any table the models declare that doesn't exist yet at all.

    Uses ``checkfirst=True`` (idempotent) and only ever creates — never
    drops or recreates an existing table, per the "do not recreate the
    table" requirement.
    """
    created: list[str] = []
    for table_name in diff.missing_tables:
        table = Base.metadata.tables[table_name]
        try:
            table.create(bind=engine, checkfirst=True)
            logger.warning("[schema-check] created missing table %s", table_name)
            created.append(table_name)
        except Exception:
            logger.exception("[schema-check] failed to create table %s", table_name)
    return created


def try_alembic_upgrade_head() -> tuple[bool, str]:
    """Attempt ``alembic upgrade head`` programmatically, using the
    project's own ``alembic.ini`` / ``alembic/env.py`` (which already know
    how to resolve the same database the app itself connects to). This is
    the preferred way to close schema drift, since it also keeps
    ``alembic_version`` accurate — the ``ADD COLUMN`` fallback below does
    not know or care about Alembic revisions.

    Returns (success, message). Never raises — any failure (alembic not
    installed, no alembic.ini found, migration error, etc.) is reported
    back as ``success=False`` so the caller can fall back to safe ALTER
    TABLE statements instead.
    """
    try:
        import os
        from alembic import command
        from alembic.config import Config

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        ini_path = os.path.join(repo_root, "alembic.ini")
        if not os.path.exists(ini_path):
            return False, f"no alembic.ini found at {ini_path}"

        cfg = Config(ini_path)
        # alembic/env.py resolves its own DB URL via database.db.resolve_database_target(),
        # so we don't need to (and shouldn't) override sqlalchemy.url here.
        command.upgrade(cfg, "head")
        return True, "alembic upgrade head succeeded"
    except Exception as exc:  # pragma: no cover - defensive, environment-dependent
        return False, f"alembic upgrade head failed: {exc}"


def format_refusal_message(diff: SchemaDiff) -> str:
    """Build the clear, actionable message printed when the app refuses to
    start because the schema is still out of sync after auto-heal attempts.
    """
    return (
        "\n"
        "==================== DATABASE SCHEMA OUT OF SYNC ====================\n"
        "The application models (database/models.py) declare tables/columns\n"
        "that do not exist in the connected database, and automatic\n"
        "migration could not fully resolve the difference:\n\n"
        f"{diff.describe()}\n\n"
        "The bot has been stopped BEFORE starting to avoid crashing later\n"
        "with errors like `psycopg2.errors.UndefinedColumn`.\n\n"
        "To fix this, run the migrations manually against the target\n"
        "database and restart the bot:\n\n"
        "    alembic upgrade head\n\n"
        "If that still doesn't resolve it, check that DATABASE_URL /\n"
        "SUPABASE_DB_URL points at the database you expect, and that the\n"
        "DB user has permission to run ALTER TABLE / CREATE TABLE.\n"
        "======================================================================\n"
    )


def ensure_schema_synced(engine: Engine, *, auto_fix: bool = True) -> SchemaDiff:
    """The main entry point: verify the live schema matches the ORM models,
    auto-healing drift where possible, and refusing to continue if it can't
    be fully resolved.

    Order of attempts when drift is found and ``auto_fix`` is True:
      1. ``alembic upgrade head`` (keeps alembic_version consistent too)
      2. safe ``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS``
         for anything alembic didn't end up covering

    Raises ``SchemaOutOfSyncError`` (with a complete, human-readable report)
    if drift remains after every attempt, or immediately if ``auto_fix`` is
    False. Returns the (empty) diff on success, purely for callers that want
    to log something.
    """
    diff = compute_schema_diff(engine)
    if not diff:
        logger.info("[schema-check] ORM models and live database schema are in sync.")
        return diff

    logger.warning(
        "[schema-check] schema drift detected (%d missing table(s), %d missing column(s)):\n%s",
        len(diff.missing_tables), diff.total_missing_columns(), diff.describe(),
    )

    if not auto_fix:
        raise SchemaOutOfSyncError(format_refusal_message(diff))

    ok, msg = try_alembic_upgrade_head()
    logger.info("[schema-check] %s", msg)
    diff = compute_schema_diff(engine)

    if diff:
        logger.warning("[schema-check] drift remains after alembic; applying safe ADD COLUMN/CREATE TABLE fallback")
        create_missing_tables(engine, diff)
        # Re-check: a just-created table needs a fresh diff before we look
        # for missing columns on tables that didn't exist a moment ago.
        diff = compute_schema_diff(engine)
        apply_missing_columns(engine, diff)
        diff = compute_schema_diff(engine)

    if diff:
        raise SchemaOutOfSyncError(format_refusal_message(diff))

    logger.info("[schema-check] schema drift resolved automatically.")
    return diff


def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    from database.db import engine as _engine

    fix = "--fix" in sys.argv
    diff = compute_schema_diff(_engine)
    if not diff:
        print("[OK] ORM models and live database schema are in sync.")
        return 0

    print(f"Schema drift detected:\n{diff.describe()}")
    if not fix:
        print("\nRun with --fix to auto-heal, or run `alembic upgrade head`.")
        return 1

    try:
        ensure_schema_synced(_engine, auto_fix=True)
    except SchemaOutOfSyncError as exc:
        print(str(exc))
        return 1

    print("[OK] Schema drift resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
