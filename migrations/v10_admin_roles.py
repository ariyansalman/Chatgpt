"""Migration v10 (Multi-Admin RBAC + 2FA): adds the `admin_roles` table.

Creates `admin_roles` with:
  - telegram_id, username
  - role                    ENUM(super_admin, moderator, support_staff)
  - manage_products / manage_orders / manage_users / manage_broadcasts /
    manage_payments / view_analytics / manage_settings / manage_admins  BOOLEAN
  - is_active, added_by, created_at
  - otp_code_hash, otp_expires_at, otp_attempts, otp_last_sent_at   (2FA)
  - session_verified_until, last_login_at                          (2FA)

Also seeds a row for the bootstrap owner (`settings.ADMIN_TELEGRAM_ID`) as
SUPER_ADMIN if one doesn't already exist, so the owner shows up in
`/admin_list` immediately instead of only being an implicit fallback.

`database/db.py` already auto-creates any missing table via
``Base.metadata.create_all`` on startup, so running this manually is
optional — kept for parity with earlier migrations (v2-v9) and explicit
production rollouts. Idempotent — safe to run multiple times. Works on
SQLite and PostgreSQL.

Usage:
    python -m migrations.v10_admin_roles
"""
from __future__ import annotations

import logging
from sqlalchemy import inspect

from database.db import engine, get_db_session
from database import Base, AdminRole, AdminRoleType, ROLE_DEFAULT_PERMISSIONS
from config.settings import settings

logger = logging.getLogger(__name__)


def run():
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    if "admin_roles" not in live_tables:
        # Create just this one table (cheap + avoids touching anything else).
        AdminRole.__table__.create(bind=engine, checkfirst=True)
        logger.info("[v10_admin_roles] created table admin_roles")
    else:
        logger.info("[v10_admin_roles] table admin_roles already exists — skipping create")

    owner_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if not owner_id:
        logger.info("[v10_admin_roles] no ADMIN_TELEGRAM_ID configured — skipping owner seed")
        return

    with get_db_session() as session:
        existing = session.query(AdminRole).filter_by(telegram_id=owner_id).first()
        if existing:
            logger.info("[v10_admin_roles] owner %s already has an admin_roles row", owner_id)
            return
        perms = ROLE_DEFAULT_PERMISSIONS[AdminRoleType.SUPER_ADMIN]
        session.add(AdminRole(
            telegram_id=owner_id,
            username=getattr(settings, "ADMIN_TELEGRAM_USERNAME", "") or None,
            role=AdminRoleType.SUPER_ADMIN,
            added_by=owner_id,
            **perms,
        ))
        session.commit()
        logger.info("[v10_admin_roles] seeded bootstrap owner %s as super_admin", owner_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
