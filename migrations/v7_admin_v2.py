"""Raw idempotent migration: create admin_audit_logs.

Mirrors alembic revision 20260704_adm2. Safe to run repeatedly; safe on
production PostgreSQL because it only issues CREATE ... IF NOT EXISTS.

Usage:
    python -m migrations.v7_admin_v2
"""
from __future__ import annotations

import logging
from sqlalchemy import text

from database.db import engine

logger = logging.getLogger(__name__)


DDL = [
    """
    CREATE TABLE IF NOT EXISTS admin_audit_logs (
        id                 SERIAL PRIMARY KEY,
        admin_telegram_id  BIGINT      NOT NULL,
        action             VARCHAR(64) NOT NULL,
        target_type        VARCHAR(32),
        target_id          VARCHAR(64),
        details            TEXT,
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_admin_telegram_id ON admin_audit_logs (admin_telegram_id)",
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_action            ON admin_audit_logs (action)",
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_created_at        ON admin_audit_logs (created_at)",
]


def run() -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
    logger.info("v7_admin_v2 migration applied")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()