"""Alembic environment — wires SQLAlchemy models + the app's DB target.

Reuses database.db.resolve_database_target() so Alembic connects to exactly
the same database (PostgreSQL/Supabase in production, or the local SQLite
dev fallback) as the running bot, with zero duplicated connection logic.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from dotenv import load_dotenv

# Ensure project root is importable so `database.models` / `database.db` resolve
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

load_dotenv()

from database.models import Base  # noqa: E402
from database.db import resolve_database_target  # noqa: E402

config = context.config

db_url, connect_args, is_sqlite_family = resolve_database_target()
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=is_sqlite_family,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(db_url, poolclass=pool.NullPool, connect_args=connect_args)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite_family,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
