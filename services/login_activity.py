"""Login Activity & Device Management Service — V32.

Public API (synchronous, best-effort — all DB errors are caught)
-----------------------------------------------------------------
record_login(tg_user, db_user_id, session_id)
    → tuple[int, bool, bool] | None   (record_id, is_new_device, is_suspicious)

get_login_history(db_user_id, limit, offset)  → list[dict]
get_login_history_count(db_user_id)           → int
get_active_sessions(db_user_id)               → list[dict]
terminate_session(session_id, db_user_id)     → bool
terminate_all_other_sessions(db_user_id, keep_session_id) → int
get_user_devices(db_user_id)                  → list[dict]
get_last_login(db_user_id)                    → dict | None

get_login_stats()                             → dict
get_all_sessions(limit, offset, active_only)  → list[dict]
get_all_sessions_count(active_only)           → int
get_all_login_records(limit, offset, suspicious_only)   → list[dict]
get_all_login_records_count(suspicious_only)  → int
get_all_devices(limit, offset)                → list[dict]
get_all_devices_count()                       → int
get_user_login_history_admin(db_user_id, limit, offset) → list[dict]
force_logout_user(db_user_id)                 → int
trust_device(device_id, db_user_id)          → bool

Async helpers (schedule with asyncio.create_task)
-------------------------------------------------
send_new_login_alert(bot, telegram_id, is_new_device, is_suspicious,
                     language_code, created_at)
cleanup_expired_sessions_job(context)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ─── Config helpers ───────────────────────────────────────────────────────────

def _status() -> str:
    return str(cfg.get("lam_status", "enabled") or "enabled")


def _is_active() -> bool:
    return _status() in ("enabled", "maintenance")


def _track_history() -> bool:
    return cfg.get_bool("lam_track_history", True)


def _track_devices() -> bool:
    return cfg.get_bool("lam_track_devices", True)


def _max_history() -> int:
    return cfg.get_int("lam_max_history", 50)


def _session_expiry_days() -> int:
    return cfg.get_int("lam_session_expiry_days", 30)


def _notify_new_login() -> bool:
    return cfg.get_bool("lam_notify_new_login", True)


def _notify_new_device() -> bool:
    return cfg.get_bool("lam_notify_new_device", True)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _get_db():
    from database import get_db_session
    return get_db_session()


def _table_ok(s, table: str) -> bool:
    try:
        s.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False


def _device_hash(db_user_id: int, language_code: str | None) -> str:
    """Stable privacy-safe device fingerprint from available Telegram fields."""
    raw = f"{db_user_id}:{language_code or 'unknown'}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ─── record_login ─────────────────────────────────────────────────────────────

def record_login(
    tg_user,
    db_user_id: int,
    session_id: int | None = None,
) -> tuple[int, bool, bool] | None:
    """Write a LoginRecord and upsert a UserDevice.

    Returns (record_id, is_new_device, is_suspicious) on success, None on error
    or when the feature is disabled.  Non-blocking — all DB errors are swallowed.
    """
    if not _is_active():
        return None
    if not _track_history():
        return None

    try:
        from database.models import LoginRecord, UserDevice

        language_code = getattr(tg_user, "language_code", None)
        username      = getattr(tg_user, "username", None)
        telegram_id   = tg_user.id

        dh             = _device_hash(db_user_id, language_code)
        is_new_device  = False
        is_suspicious  = False

        with _get_db() as s:
            if not _table_ok(s, "login_records"):
                return None

            # ── Device tracking ────────────────────────────────────────────
            if _track_devices() and _table_ok(s, "user_devices"):
                existing_dev = (
                    s.query(UserDevice)
                    .filter_by(user_id=db_user_id, device_hash=dh)
                    .first()
                )
                if existing_dev:
                    existing_dev.last_seen_at = datetime.utcnow()
                    existing_dev.login_count  = (existing_dev.login_count or 0) + 1
                    if language_code:
                        existing_dev.language_code = language_code
                else:
                    is_new_device = True
                    s.add(UserDevice(
                        user_id=db_user_id,
                        device_hash=dh,
                        device_name=None,
                        os_name=None,
                        app_version=None,
                        language_code=language_code,
                        first_seen_at=datetime.utcnow(),
                        last_seen_at=datetime.utcnow(),
                        is_trusted=False,
                        login_count=1,
                    ))
                    s.flush()

            # ── Active session lookup ──────────────────────────────────────
            if not session_id and _table_ok(s, "user_sessions"):
                row = s.execute(text(
                    "SELECT id FROM user_sessions "
                    "WHERE user_id = :uid AND is_active = TRUE "
                    "ORDER BY last_active_at DESC LIMIT 1"
                ), {"uid": db_user_id}).fetchone()
                if row:
                    session_id = int(row[0])

            # ── Suspicious detection: > 4 logins in last 60 minutes ───────
            if _table_ok(s, "login_records"):
                recent = s.execute(text(
                    "SELECT COUNT(*) FROM login_records "
                    "WHERE user_id = :uid "
                    "  AND created_at >= NOW() - INTERVAL '1 hour'"
                ), {"uid": db_user_id}).scalar() or 0
                if recent >= 5:
                    is_suspicious = True

            # ── Write LoginRecord ──────────────────────────────────────────
            record = LoginRecord(
                user_id=db_user_id,
                telegram_id=telegram_id,
                username=username,
                session_id=session_id,
                login_method="telegram",
                device_name=None,
                os_name=None,
                app_version=None,
                language_code=language_code,
                ip_address=None,
                country=None,
                city=None,
                is_suspicious=is_suspicious,
                is_new_device=is_new_device,
                is_new_location=False,
                alert_sent=False,
                created_at=datetime.utcnow(),
            )
            s.add(record)
            s.flush()
            record_id = record.id

            # Mark alert_sent if we will send one
            if (_notify_new_login() or (is_new_device and _notify_new_device())
                    or is_suspicious):
                record.alert_sent = True

            # ── History cap ────────────────────────────────────────────────
            max_hist = _max_history()
            if max_hist > 0:
                oldest = (
                    s.query(LoginRecord.created_at)
                    .filter(LoginRecord.user_id == db_user_id)
                    .order_by(LoginRecord.created_at.desc())
                    .offset(max_hist - 1)
                    .limit(1)
                    .scalar()
                )
                if oldest:
                    s.query(LoginRecord).filter(
                        LoginRecord.user_id == db_user_id,
                        LoginRecord.created_at < oldest,
                    ).delete(synchronize_session=False)

            return record_id, is_new_device, is_suspicious

    except Exception:
        logger.debug("record_login failed (non-fatal)", exc_info=True)
    return None


# ─── User-facing: login history ───────────────────────────────────────────────

def get_login_history(
    db_user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "login_records"):
                return []
            rows = s.execute(text("""
                SELECT id, telegram_id, username, login_method,
                       device_name, os_name, language_code,
                       ip_address, country, city,
                       is_suspicious, is_new_device, created_at
                FROM   login_records
                WHERE  user_id = :uid
                ORDER  BY created_at DESC
                LIMIT  :lim OFFSET :off
            """), {"uid": db_user_id, "lim": limit, "off": offset}).fetchall()
            return [
                {
                    "id":           r[0],  "telegram_id":  r[1],
                    "username":     r[2],  "login_method": r[3],
                    "device_name":  r[4],  "os_name":      r[5],
                    "language_code":r[6],  "ip_address":   r[7],
                    "country":      r[8],  "city":         r[9],
                    "is_suspicious":r[10], "is_new_device":r[11],
                    "created_at":   r[12],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_login_history failed", exc_info=True)
    return []


def get_login_history_count(db_user_id: int) -> int:
    try:
        with _get_db() as s:
            if not _table_ok(s, "login_records"):
                return 0
            return s.execute(
                text("SELECT COUNT(*) FROM login_records WHERE user_id = :uid"),
                {"uid": db_user_id},
            ).scalar() or 0
    except Exception:
        return 0


def get_last_login(db_user_id: int) -> dict[str, Any] | None:
    rows = get_login_history(db_user_id, limit=1, offset=0)
    return rows[0] if rows else None


# ─── User-facing: sessions ────────────────────────────────────────────────────

def get_active_sessions(db_user_id: int) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return []
            rows = s.execute(text("""
                SELECT id, session_token, device_info, created_at, last_active_at
                FROM   user_sessions
                WHERE  user_id = :uid AND is_active = TRUE
                ORDER  BY last_active_at DESC
            """), {"uid": db_user_id}).fetchall()
            return [
                {
                    "id":             r[0], "token":          r[1],
                    "device_info":    r[2], "created_at":     r[3],
                    "last_active_at": r[4],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_active_sessions failed", exc_info=True)
    return []


def terminate_session(session_id: int, db_user_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return False
            s.execute(text("""
                UPDATE user_sessions
                SET    is_active = FALSE, terminated_at = NOW()
                WHERE  id = :sid AND user_id = :uid AND is_active = TRUE
            """), {"sid": session_id, "uid": db_user_id})
            return True
    except Exception:
        logger.debug("terminate_session failed", exc_info=True)
    return False


def terminate_all_other_sessions(
    db_user_id: int,
    keep_session_id: int | None = None,
) -> int:
    """Terminate all active sessions except keep_session_id. Returns count."""
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return 0
            if keep_session_id:
                result = s.execute(text("""
                    UPDATE user_sessions
                    SET    is_active = FALSE, terminated_at = NOW()
                    WHERE  user_id = :uid AND is_active = TRUE AND id != :keep
                """), {"uid": db_user_id, "keep": keep_session_id})
            else:
                result = s.execute(text("""
                    UPDATE user_sessions
                    SET    is_active = FALSE, terminated_at = NOW()
                    WHERE  user_id = :uid AND is_active = TRUE
                """), {"uid": db_user_id})
            return result.rowcount or 0
    except Exception:
        logger.debug("terminate_all_other_sessions failed", exc_info=True)
    return 0


# ─── User-facing: devices ─────────────────────────────────────────────────────

def get_user_devices(db_user_id: int) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_devices"):
                return []
            rows = s.execute(text("""
                SELECT id, device_hash, device_name, os_name, language_code,
                       first_seen_at, last_seen_at, is_trusted, login_count
                FROM   user_devices
                WHERE  user_id = :uid
                ORDER  BY last_seen_at DESC
            """), {"uid": db_user_id}).fetchall()
            return [
                {
                    "id":           r[0], "device_hash":  r[1],
                    "device_name":  r[2], "os_name":      r[3],
                    "language_code":r[4], "first_seen_at":r[5],
                    "last_seen_at": r[6], "is_trusted":   r[7],
                    "login_count":  r[8],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_user_devices failed", exc_info=True)
    return []


def trust_device(device_id: int, db_user_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_devices"):
                return False
            s.execute(text("""
                UPDATE user_devices SET is_trusted = TRUE
                WHERE id = :did AND user_id = :uid
            """), {"did": device_id, "uid": db_user_id})
            return True
    except Exception:
        logger.debug("trust_device failed", exc_info=True)
    return False


# ─── Admin stats ──────────────────────────────────────────────────────────────

def get_login_stats() -> dict[str, Any]:
    stats: dict[str, Any] = {
        "active_sessions": 0, "logged_out_sessions": 0,
        "today_logins": 0, "weekly_logins": 0, "monthly_logins": 0,
        "suspicious_logins": 0, "new_device_logins": 0,
        "total_devices": 0, "new_devices_today": 0,
    }
    try:
        with _get_db() as s:
            ok_lr = _table_ok(s, "login_records")
            ok_ud = _table_ok(s, "user_devices")
            ok_us = _table_ok(s, "user_sessions")

            if ok_us:
                stats["active_sessions"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM user_sessions WHERE is_active = TRUE"
                    )).scalar() or 0
                )
                stats["logged_out_sessions"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM user_sessions WHERE is_active = FALSE"
                    )).scalar() or 0
                )

            if ok_lr:
                stats["today_logins"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM login_records "
                        "WHERE created_at >= CURRENT_DATE"
                    )).scalar() or 0
                )
                stats["weekly_logins"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM login_records "
                        "WHERE created_at >= NOW() - INTERVAL '7 days'"
                    )).scalar() or 0
                )
                stats["monthly_logins"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM login_records "
                        "WHERE created_at >= NOW() - INTERVAL '30 days'"
                    )).scalar() or 0
                )
                stats["suspicious_logins"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM login_records "
                        "WHERE is_suspicious = TRUE"
                    )).scalar() or 0
                )
                stats["new_device_logins"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM login_records "
                        "WHERE is_new_device = TRUE"
                    )).scalar() or 0
                )

            if ok_ud:
                stats["total_devices"] = (
                    s.execute(
                        text("SELECT COUNT(*) FROM user_devices")
                    ).scalar() or 0
                )
                stats["new_devices_today"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM user_devices "
                        "WHERE first_seen_at >= CURRENT_DATE"
                    )).scalar() or 0
                )

    except Exception:
        logger.debug("get_login_stats failed", exc_info=True)
    return stats


# ─── Admin: paginated data ────────────────────────────────────────────────────

def get_all_sessions(
    limit: int = 20,
    offset: int = 0,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return []
            cond = "AND us.is_active = TRUE" if active_only else ""
            rows = s.execute(text(f"""
                SELECT us.id, us.user_id, u.telegram_id, u.username,
                       us.device_info, us.created_at, us.last_active_at, us.is_active
                FROM   user_sessions us
                JOIN   users u ON u.id = us.user_id
                WHERE  1=1 {cond}
                ORDER  BY us.last_active_at DESC
                LIMIT  :lim OFFSET :off
            """), {"lim": limit, "off": offset}).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "telegram_id": r[2],
                    "username": r[3], "device_info": r[4],
                    "created_at": r[5], "last_active_at": r[6],
                    "is_active": r[7],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_all_sessions failed", exc_info=True)
    return []


def get_all_sessions_count(active_only: bool = True) -> int:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return 0
            cond = "WHERE is_active = TRUE" if active_only else ""
            return s.execute(
                text(f"SELECT COUNT(*) FROM user_sessions {cond}")
            ).scalar() or 0
    except Exception:
        return 0


def get_all_login_records(
    limit: int = 20,
    offset: int = 0,
    suspicious_only: bool = False,
) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "login_records"):
                return []
            cond = "AND lr.is_suspicious = TRUE" if suspicious_only else ""
            rows = s.execute(text(f"""
                SELECT lr.id, lr.user_id, lr.telegram_id, lr.username,
                       lr.login_method, lr.device_name, lr.language_code,
                       lr.ip_address, lr.country,
                       lr.is_suspicious, lr.is_new_device, lr.created_at
                FROM   login_records lr
                WHERE  1=1 {cond}
                ORDER  BY lr.created_at DESC
                LIMIT  :lim OFFSET :off
            """), {"lim": limit, "off": offset}).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "telegram_id": r[2],
                    "username": r[3], "login_method": r[4],
                    "device_name": r[5], "language_code": r[6],
                    "ip_address": r[7], "country": r[8],
                    "is_suspicious": r[9], "is_new_device": r[10],
                    "created_at": r[11],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_all_login_records failed", exc_info=True)
    return []


def get_all_login_records_count(suspicious_only: bool = False) -> int:
    try:
        with _get_db() as s:
            if not _table_ok(s, "login_records"):
                return 0
            cond = "WHERE is_suspicious = TRUE" if suspicious_only else ""
            return s.execute(
                text(f"SELECT COUNT(*) FROM login_records {cond}")
            ).scalar() or 0
    except Exception:
        return 0


def get_user_login_history_admin(
    db_user_id: int,
    limit: int = 15,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return get_login_history(db_user_id, limit=limit, offset=offset)


def get_all_devices(
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_devices"):
                return []
            rows = s.execute(text("""
                SELECT ud.id, ud.user_id, u.telegram_id, u.username,
                       ud.device_hash, ud.device_name, ud.os_name,
                       ud.language_code, ud.first_seen_at, ud.last_seen_at,
                       ud.is_trusted, ud.login_count
                FROM   user_devices ud
                JOIN   users u ON u.id = ud.user_id
                ORDER  BY ud.last_seen_at DESC
                LIMIT  :lim OFFSET :off
            """), {"lim": limit, "off": offset}).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "telegram_id": r[2],
                    "username": r[3], "device_hash": r[4],
                    "device_name": r[5], "os_name": r[6],
                    "language_code": r[7], "first_seen_at": r[8],
                    "last_seen_at": r[9], "is_trusted": r[10],
                    "login_count": r[11],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_all_devices failed", exc_info=True)
    return []


def get_all_devices_count() -> int:
    try:
        with _get_db() as s:
            if not _table_ok(s, "user_devices"):
                return 0
            return s.execute(
                text("SELECT COUNT(*) FROM user_devices")
            ).scalar() or 0
    except Exception:
        return 0


def force_logout_user(db_user_id: int) -> int:
    """Terminate ALL active sessions for a user. Returns count terminated."""
    return terminate_all_other_sessions(db_user_id, keep_session_id=None)


# ─── Async: cleanup job ───────────────────────────────────────────────────────

async def cleanup_expired_sessions_job(context) -> None:
    """Periodic background job: expire sessions older than lam_session_expiry_days."""
    try:
        expiry_days = _session_expiry_days()
        if expiry_days <= 0:
            return
        with _get_db() as s:
            if not _table_ok(s, "user_sessions"):
                return
            s.execute(text(
                f"UPDATE user_sessions "
                f"SET is_active = FALSE, terminated_at = NOW() "
                f"WHERE is_active = TRUE "
                f"  AND last_active_at < NOW() - INTERVAL '{expiry_days} days'"
            ))
    except Exception:
        logger.debug("cleanup_expired_sessions_job failed (non-fatal)", exc_info=True)


# ─── Async: new login alert ───────────────────────────────────────────────────

async def send_new_login_alert(
    bot,
    telegram_id: int,
    is_new_device: bool = False,
    is_suspicious: bool = False,
    language_code: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """User-facing "🔔 New Login Detected" / "⚠️ Suspicious Login Detected"
    Telegram notification — INTENTIONALLY DISABLED per product decision.

    This is a no-op by design: no message is ever sent to the user from
    here. Nothing else is affected —
      • record_login() (session creation, device fingerprinting, the
        is_new_device / is_suspicious flags, and the LoginRecord row
        including alert_sent) still runs exactly as before, from
        bot.py, independently of this function.
      • Login history, active-session management, and admin-facing
        Login Activity logs/reports are untouched.
    The bot.send_message() call that used to deliver this notification
    has been removed entirely (not just gated behind a flag) so no code
    path here can send a login alert to a user.
    """
    return
