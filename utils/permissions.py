"""Multi-admin role-based access control + Telegram-native OTP 2FA.

Roles (least → most powerful): support_staff, moderator, super_admin.
Permissions (see database.models.AdminRole): manage_products, manage_orders,
manage_users, manage_broadcasts, manage_payments, view_analytics,
manage_settings, manage_admins.

The store owner configured via ``ADMIN_TELEGRAM_ID`` is always an implicit,
unremovable super_admin — even with zero rows in ``admin_roles`` — so this
feature can be deployed onto an existing store without locking the owner out.

2FA model: no SMS/email involved. ``/admin_login`` makes the bot DM the
admin a 6-digit code in the same Telegram chat; they type it back within
``OTP_TTL_MINUTES``; a verified session is then remembered for
``SESSION_TTL_HOURS`` so routine clicks aren't re-prompted constantly.
Codes are only ever stored as a SHA-256 hash, never in plaintext.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings
from database import get_db_session, AdminRole, AdminRoleType, ROLE_DEFAULT_PERMISSIONS

logger = logging.getLogger(__name__)

PERMISSIONS = (
    "manage_products", "manage_orders", "manage_users", "manage_broadcasts",
    "manage_payments", "view_analytics", "manage_settings", "manage_admins",
)

OTP_TTL_MINUTES = 5
OTP_RESEND_COOLDOWN_SECONDS = 30
OTP_MAX_ATTEMPTS = 5
SESSION_TTL_HOURS = 12


def is_2fa_enforced() -> bool:
    """Global on/off switch for admin 2FA enforcement (config.settings.ADMIN_2FA_ENABLED).

    When this returns False, /admin_login and the OTP-send flow still exist
    and work exactly as before — this only controls whether anything
    ACTUALLY REQUIRES a verified session to proceed. Every enforcement
    choke-point below (has_permission, require_permission, require_role,
    require_2fa) checks this first and skips the has_valid_session() check
    entirely when it's False, so an admin (is_admin() true) can use any
    admin feature immediately with no re-verification prompt.
    """
    return bool(getattr(settings, "ADMIN_2FA_ENABLED", False))


# ─────────────────────────── Role / permission lookups ───────────────────────────

class AdminInfo:
    """Plain snapshot of an admin's role + permissions, safe to use after the
    DB session that produced it has closed."""

    def __init__(self, telegram_id: int, role: AdminRoleType, permissions: dict,
                 is_bootstrap_owner: bool = False):
        self.telegram_id = telegram_id
        self.role = role
        self.permissions = permissions
        self.is_bootstrap_owner = is_bootstrap_owner

    def has(self, permission: str) -> bool:
        if self.role == AdminRoleType.SUPER_ADMIN:
            return True
        return bool(self.permissions.get(permission, False))

    def __repr__(self):
        return f"<AdminInfo {self.telegram_id} role={self.role.value}>"


def _owner_id() -> int:
    return getattr(settings, "ADMIN_TELEGRAM_ID", 0) or 0


# ─────────────────────────── Admin lookup cache ───────────────────────────
# get_admin() is called on every /start and most keyboard renders (to decide
# whether to show the "Admin Panel" button) — for the overwhelming majority
# of calls the answer is "not an admin", which was previously a full DB
# round trip on every single update. Admin role data changes rarely (a few
# times a day at most, via upsert_admin/deactivate_admin/set_permission
# below), so a short TTL cache is safe and is invalidated immediately by
# those mutation functions rather than waiting out the TTL.
_ADMIN_CACHE: dict[int, tuple[Optional[AdminInfo], float]] = {}
_ADMIN_CACHE_TTL = 30  # seconds


def _admin_cache_get(telegram_id: int):
    import time
    entry = _ADMIN_CACHE.get(telegram_id)
    if entry is None:
        return None, False
    info, ts = entry
    if time.monotonic() - ts >= _ADMIN_CACHE_TTL:
        return None, False
    return info, True


def _admin_cache_set(telegram_id: int, info: Optional[AdminInfo]) -> None:
    import time
    _ADMIN_CACHE[telegram_id] = (info, time.monotonic())


def clear_admin_cache(telegram_id: Optional[int] = None) -> None:
    """Invalidate the admin lookup cache — called whenever admin role/
    permission rows are mutated so changes take effect immediately instead
    of waiting out the TTL."""
    if telegram_id is None:
        _ADMIN_CACHE.clear()
    else:
        _ADMIN_CACHE.pop(telegram_id, None)


def get_admin(telegram_id: int) -> Optional[AdminInfo]:
    """Look up an admin's role/permissions. Returns None if they aren't an admin
    (or have been deactivated). Cached briefly — see _ADMIN_CACHE above."""
    cached, hit = _admin_cache_get(telegram_id)
    if hit:
        return cached

    owner_id = _owner_id()
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if row:
            if not row.is_active and telegram_id != owner_id:
                _admin_cache_set(telegram_id, None)
                return None
            # The bootstrap owner can never be demoted/deactivated out of their own bot.
            role = AdminRoleType.SUPER_ADMIN if telegram_id == owner_id else row.role
            perms = {p: getattr(row, p) for p in PERMISSIONS}
            info = AdminInfo(telegram_id, role, perms, is_bootstrap_owner=(telegram_id == owner_id))
            _admin_cache_set(telegram_id, info)
            return info

    if owner_id and telegram_id == owner_id:
        info = AdminInfo(telegram_id, AdminRoleType.SUPER_ADMIN,
                          ROLE_DEFAULT_PERMISSIONS[AdminRoleType.SUPER_ADMIN],
                          is_bootstrap_owner=True)
        _admin_cache_set(telegram_id, info)
        return info

    _admin_cache_set(telegram_id, None)
    return None


def is_admin(user_id: int) -> bool:
    """Backward-compatible, role-aware replacement for the old single-owner
    ``utils.helpers.is_admin``. True for ANY active admin of any tier."""
    return get_admin(user_id) is not None


def is_super_admin(user_id: int) -> bool:
    admin = get_admin(user_id)
    return bool(admin and admin.role == AdminRoleType.SUPER_ADMIN)


def has_permission(user_id: int, permission: str, check_2fa: bool = True) -> bool:
    """True iff the user is an admin, holds ``permission`` (or is super_admin),
    AND — unless ``check_2fa=False`` — currently has a verified OTP session.

    This is the single choke-point used both by ``require_permission`` and by
    the inline ``if not has_permission(...):`` checks throughout
    handlers/admin_*.py, so 2FA is enforced everywhere without needing a
    decorator on every one of the ~340 handler functions. Callers that only
    need the role/permission check without the session requirement (e.g. to
    decide whether to render a locked-vs-hidden menu button) can pass
    ``check_2fa=False``.
    """
    admin = get_admin(user_id)
    if not admin or not admin.has(permission):
        return False
    if check_2fa and is_2fa_enforced() and not has_valid_session(user_id):
        return False
    return True


def list_admins(include_inactive: bool = False):
    """Returns a list of plain dicts (safe after session close)."""
    with get_db_session() as session:
        q = session.query(AdminRole)
        if not include_inactive:
            q = q.filter_by(is_active=True)
        rows = q.order_by(AdminRole.role, AdminRole.telegram_id).all()
        return [
            dict(telegram_id=r.telegram_id, username=r.username, role=r.role.value,
                 is_active=r.is_active, last_login_at=r.last_login_at)
            for r in rows
        ]


def upsert_admin(telegram_id: int, role: AdminRoleType, added_by: int,
                  username: Optional[str] = None) -> AdminInfo:
    """Create or re-role an admin, resetting their permission flags to the
    role's defaults. Reactivates a previously-deactivated admin."""
    perms = ROLE_DEFAULT_PERMISSIONS[role]
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if row:
            row.role = role
            row.is_active = True
            row.username = username or row.username
            for p in PERMISSIONS:
                setattr(row, p, perms[p])
        else:
            row = AdminRole(telegram_id=telegram_id, username=username, role=role,
                             added_by=added_by, **perms)
            session.add(row)
        session.commit()
    clear_admin_cache(telegram_id)
    return get_admin(telegram_id)


def deactivate_admin(telegram_id: int) -> bool:
    """Soft-deletes an admin. Refuses to deactivate the bootstrap owner."""
    if telegram_id == _owner_id():
        return False
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row:
            return False
        row.is_active = False
        row.session_verified_until = None
        session.commit()
    clear_admin_cache(telegram_id)
    return True


def set_permission(telegram_id: int, permission: str, value: bool) -> bool:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission: {permission}")
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row:
            return False
        setattr(row, permission, value)
        session.commit()
    clear_admin_cache(telegram_id)
    return True


# ─────────────────────────────── OTP / 2FA ───────────────────────────────

def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def otp_cooldown_remaining(telegram_id: int) -> int:
    """Seconds left before another OTP can be sent. 0 = can send now."""
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row or not row.otp_last_sent_at:
            return 0
        elapsed = (datetime.utcnow() - row.otp_last_sent_at).total_seconds()
        remaining = OTP_RESEND_COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))


def generate_and_store_otp(telegram_id: int) -> str:
    """Generates a 6-digit code, stores only its hash, returns the plaintext
    code so the caller can send it to the admin's own chat.

    Lazily creates an AdminRole row for the bootstrap owner on first login if
    one doesn't exist yet (so the owner never needs a manual DB insert).
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.utcnow()
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row:
            if telegram_id != _owner_id():
                raise PermissionError("not a registered admin")
            perms = ROLE_DEFAULT_PERMISSIONS[AdminRoleType.SUPER_ADMIN]
            row = AdminRole(telegram_id=telegram_id, role=AdminRoleType.SUPER_ADMIN,
                             added_by=telegram_id, **perms)
            session.add(row)
            session.flush()
        row.otp_code_hash = _hash_otp(code)
        row.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
        row.otp_attempts = 0
        row.otp_last_sent_at = now
        session.commit()
    return code


def verify_otp(telegram_id: int, submitted_code: str) -> tuple[bool, str]:
    """Checks a submitted code. On success, opens a verified session for
    ``SESSION_TTL_HOURS``. Returns (ok, human_message)."""
    now = datetime.utcnow()
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row or not row.otp_code_hash:
            return False, "No pending code found. Send /admin_login to get a new one."
        if row.otp_expires_at and now > row.otp_expires_at:
            row.otp_code_hash = None
            session.commit()
            return False, "⌛ That code expired. Send /admin_login to get a new one."
        if row.otp_attempts >= OTP_MAX_ATTEMPTS:
            row.otp_code_hash = None
            session.commit()
            return False, "🚫 Too many wrong attempts. Send /admin_login to get a new one."
        if _hash_otp(submitted_code) != row.otp_code_hash:
            row.otp_attempts += 1
            session.commit()
            left = OTP_MAX_ATTEMPTS - row.otp_attempts
            return False, f"❌ Wrong code. {left} attempt(s) left."
        row.otp_code_hash = None
        row.otp_attempts = 0
        row.session_verified_until = now + timedelta(hours=SESSION_TTL_HOURS)
        row.last_login_at = now
        session.commit()
    return True, f"✅ Verified. Your admin session is active for {SESSION_TTL_HOURS} hours."


def has_valid_session(telegram_id: int) -> bool:
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if not row:
            return False
        return bool(row.session_verified_until and row.session_verified_until > datetime.utcnow())


def invalidate_session(telegram_id: int) -> None:
    with get_db_session() as session:
        row = session.query(AdminRole).filter_by(telegram_id=telegram_id).first()
        if row:
            row.session_verified_until = None
            session.commit()


# ────────────────────────────── Decorators ──────────────────────────────

_LOGIN_HINT = "🔒 Your admin session isn't verified (or has expired). Send /admin_login to get a code."


async def _deny(update: Update, text: str) -> None:
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
    except Exception:  # noqa: BLE001
        logger.exception("failed to send access-denial message")


def require_permission(permission: str, check_2fa: bool = True):
    """Gate a handler behind a specific permission flag (role-aware — any
    role that grants this flag, or super_admin, passes). Also enforces an
    active 2FA session unless ``check_2fa=False``."""
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission: {permission}")

    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            admin = get_admin(user_id)
            if not admin:
                await _deny(update, "⛔ You don't have permission to access this.")
                return
            if not admin.has(permission):
                await _deny(update, f"⛔ Your role ({admin.role.value}) doesn't include '{permission}' access.")
                return
            if check_2fa and is_2fa_enforced() and not has_valid_session(user_id):
                await _deny(update, _LOGIN_HINT)
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def require_role(*roles: AdminRoleType, check_2fa: bool = True):
    """Gate a handler behind specific role(s), e.g. @require_role(AdminRoleType.SUPER_ADMIN)."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            admin = get_admin(user_id)
            if not admin or admin.role not in roles:
                names = ", ".join(r.value for r in roles)
                await _deny(update, f"⛔ Restricted to: {names}")
                return
            if check_2fa and is_2fa_enforced() and not has_valid_session(user_id):
                await _deny(update, _LOGIN_HINT)
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def require_2fa(func):
    """Standalone 2FA-session gate (no permission check) — use for actions
    every admin tier can do, but which are still sensitive enough to need a
    fresh-ish verified session.

    A no-op pass-through of the session check while
    ``config.settings.ADMIN_2FA_ENABLED`` is False (the current default) —
    still requires ``is_admin()``, just not a fresh OTP session.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            await _deny(update, "⛔ You don't have permission to access this command.")
            return
        if is_2fa_enforced() and not has_valid_session(user_id):
            await _deny(update, _LOGIN_HINT)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
