#!/usr/bin/env python3
"""One-time data migration: Turso (libSQL) -> Supabase PostgreSQL.

This is a MANUAL, ONE-TIME utility. It is never run automatically by the
bot, Render, or Alembic. Run it yourself, once, after you have:

  1. Created the Supabase project + database.
  2. Pointed DATABASE_URL at Supabase and run `alembic upgrade head` against
     it, so every table already exists with the current schema.
  3. Kept your OLD Turso credentials around (TURSO_DATABASE_URL,
     TURSO_AUTH_TOKEN) — they are ONLY needed for this script, never by the
     production bot.

Install the extra migration-only dependency first:

    pip install -r requirements-migration.txt

Usage:

    TURSO_DATABASE_URL=libsql://tgbot-ariyansalman.aws-ap-south-1.turso.io \\
    TURSO_AUTH_TOKEN=xxxxx \\
    DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/postgres \\
    python scripts/migrate_turso_to_postgres.py

Optional flags:

    --dry-run       Read from Turso and report counts, write nothing.
    --tables a,b,c  Only migrate these tables (comma-separated), in the
                     order given. Useful for retrying a single failed table.

What it does
------------
- Connects to Turso/libSQL directly (independent of database/db.py, which no
  longer knows how to talk to Turso at all).
- Connects to Supabase PostgreSQL via the exact same SQLAlchemy models the
  bot uses, so column types/constraints match production precisely.
- Migrates tables in the dependency order SQLAlchemy computes from the
  models' ForeignKeys (``Base.metadata.sorted_tables``) — this is what makes
  the migration foreign-key-safe without hand-maintaining a table order.
- Converts SQLite integer 0/1 -> Python bool for Boolean columns, and
  SQLite's stored datetime strings -> real `datetime` objects, based on each
  column's declared SQLAlchemy type — not by guessing per table.
- Is idempotent: rows are skipped by primary key if they already exist in
  PostgreSQL, so running this script twice never duplicates data.
- Resets PostgreSQL's auto-increment sequence for every integer primary key
  table to MAX(id) after inserting, so new rows created by the bot after
  cutover don't collide with migrated IDs.
- Never logs DATABASE_URL, TURSO_AUTH_TOKEN, or any other secret.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, inspect as sa_inspect, select, MetaData
from sqlalchemy import Boolean, DateTime, Date
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from database.models import Base  # noqa: E402


def _redact(url: str) -> str:
    """Never print credentials — only scheme + host."""
    if "@" in url:
        return url.split("@", 1)[1]
    return "<hidden>"


def get_turso_connection():
    """Open a raw DB-API connection to Turso/libSQL.

    Isolated here (not in database/db.py) so the production bot has zero
    Turso dependency. Requires `pip install -r requirements-migration.txt`.
    """
    turso_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    turso_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()

    if not turso_url or not turso_token:
        raise SystemExit(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must both be set in the "
            "environment to run this migration."
        )

    try:
        import libsql_experimental as libsql
    except ImportError:
        raise SystemExit(
            "Missing the Turso client library. Install migration-only deps with:\n"
            "    pip install -r requirements-migration.txt"
        )

    host = turso_url.replace("libsql://", "").replace("https://", "").rstrip("/")
    conn = libsql.connect(f"https://{host}", auth_token=turso_token)
    print(f"[OK] Connected to Turso at {host}")
    return conn


def get_postgres_engine():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    if not db_url.startswith("postgresql://"):
        raise SystemExit(
            "DATABASE_URL must be a postgresql:// URL pointing at Supabase "
            "for this migration."
        )
    engine = create_engine(db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        pass  # fail fast if unreachable
    print(f"[OK] Connected to PostgreSQL at {_redact(db_url)}")
    return engine


def _convert_value(value, sa_type):
    """Convert a raw SQLite/libSQL scalar to what the PostgreSQL column
    expects, based on the SQLAlchemy column type declared in models.py."""
    if value is None:
        return None
    if isinstance(sa_type, Boolean):
        return bool(value)
    if isinstance(sa_type, DateTime):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            text_val = value.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(text_val, fmt)
                except ValueError:
                    continue
            return None
        return value
    if isinstance(sa_type, Date):
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.strptime(value.strip(), "%Y-%m-%d").date()
            except ValueError:
                return None
        return value
    return value


def fetch_turso_rows(turso_conn, table_name: str):
    """Return (column_names, list_of_row_tuples) for a table, or (None, None)
    if the table doesn't exist in the Turso database (e.g. added after the
    Turso export was taken, or never used)."""
    cur = turso_conn.cursor()
    try:
        cur.execute(f'SELECT * FROM "{table_name}"')
    except Exception:
        return None, None
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return columns, rows


def existing_pg_ids(pg_conn, table, pk_col):
    """Primary keys already present in PostgreSQL, for idempotent skipping."""
    result = pg_conn.execute(select(pk_col))
    return {row[0] for row in result}


def reset_sequence(pg_conn, table_name: str, pk_name: str):
    """After explicit-ID inserts, point the PostgreSQL identity sequence at
    MAX(id) so the next auto-generated insert doesn't collide."""
    try:
        pg_conn.exec_driver_sql(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_name}'), "
            f"COALESCE((SELECT MAX(\"{pk_name}\") FROM \"{table_name}\"), 1), "
            f"(SELECT MAX(\"{pk_name}\") FROM \"{table_name}\") IS NOT NULL)"
        )
    except SQLAlchemyError:
        pass  # table has no serial/identity pk (e.g. composite key) — nothing to reset


def migrate_table(turso_conn, pg_engine, table, dry_run: bool) -> dict:
    """Migrate one table. Returns a summary dict."""
    table_name = table.name
    columns, rows = fetch_turso_rows(turso_conn, table_name)

    summary = {"table": table_name, "source_rows": 0, "migrated": 0, "skipped_existing": 0, "errors": 0}

    if columns is None:
        summary["note"] = "not found in Turso (skipped)"
        return summary

    summary["source_rows"] = len(rows)
    if not rows:
        return summary

    if dry_run:
        summary["note"] = "dry-run — nothing written"
        return summary

    model_columns = {c.name: c for c in table.columns}
    pk_cols = list(table.primary_key.columns)
    pk_col = pk_cols[0] if len(pk_cols) == 1 else None

    with pg_engine.begin() as pg_conn:
        already = existing_pg_ids(pg_conn, table, pk_col) if pk_col is not None else set()

        payload = []
        for raw_row in rows:
            row_dict = dict(zip(columns, raw_row))
            # Drop columns that don't exist on the current model (renamed/removed).
            row_dict = {k: v for k, v in row_dict.items() if k in model_columns}
            converted = {k: _convert_value(v, model_columns[k].type) for k, v in row_dict.items()}

            if pk_col is not None and converted.get(pk_col.name) in already:
                summary["skipped_existing"] += 1
                continue
            payload.append(converted)

        for row in payload:
            try:
                if pk_col is not None:
                    stmt = pg_insert(table).values(**row).on_conflict_do_nothing(
                        index_elements=[pk_col.name]
                    )
                else:
                    stmt = pg_insert(table).values(**row).on_conflict_do_nothing()
                pg_conn.execute(stmt)
                summary["migrated"] += 1
            except SQLAlchemyError as e:
                summary["errors"] += 1
                print(f"    [ERROR] {table_name}: {e.__class__.__name__} (row skipped)")

        if pk_col is not None and summary["migrated"] > 0:
            reset_sequence(pg_conn, table_name, pk_col.name)

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read counts only, write nothing.")
    parser.add_argument("--tables", type=str, default="", help="Comma-separated list of table names to migrate.")
    args = parser.parse_args()

    turso_conn = get_turso_connection()
    pg_engine = get_postgres_engine()

    all_tables = list(Base.metadata.sorted_tables)  # FK-safe order, computed by SQLAlchemy
    if args.tables.strip():
        wanted = {t.strip() for t in args.tables.split(",") if t.strip()}
        all_tables = [t for t in all_tables if t.name in wanted]

    print(f"\nMigrating {len(all_tables)} tables in dependency order"
          f"{' (DRY RUN)' if args.dry_run else ''}...\n")

    summaries = []
    for table in all_tables:
        print(f"-> {table.name}")
        summary = migrate_table(turso_conn, pg_engine, table, args.dry_run)
        summaries.append(summary)
        note = f" ({summary['note']})" if "note" in summary else ""
        print(f"   source={summary['source_rows']} migrated={summary['migrated']} "
              f"skipped_existing={summary['skipped_existing']} errors={summary['errors']}{note}")

    total_migrated = sum(s["migrated"] for s in summaries)
    total_skipped = sum(s["skipped_existing"] for s in summaries)
    total_errors = sum(s["errors"] for s in summaries)

    print("\n" + "=" * 50)
    print("MIGRATION SUMMARY")
    for s in summaries:
        if s["source_rows"] or s["migrated"]:
            print(f"{s['table']}: {s['migrated']} migrated (of {s['source_rows']} in Turso)")
    print(f"Skipped existing rows: {total_skipped}")
    print(f"Errors: {total_errors}")
    print("=" * 50)

    if total_errors:
        print("\nSome rows failed to migrate — see [ERROR] lines above. "
              "Re-run with --tables <name> to retry a specific table; "
              "already-migrated rows will be skipped safely (idempotent).")
        sys.exit(1)


if __name__ == "__main__":
    main()
