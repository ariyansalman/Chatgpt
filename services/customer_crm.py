"""Customer Notes & CRM Service — V33.

Public API (synchronous, best-effort — all DB errors are caught)
-----------------------------------------------------------------
--- Notes ---
add_note(user_id, admin_id, admin_name, content)  → int | None   (note.id)
edit_note(note_id, admin_id, new_content)          → bool
delete_note(note_id, admin_id)                     → bool
pin_note(note_id, admin_id)                        → bool  (toggles)
archive_note(note_id, admin_id)                    → bool  (toggles)
get_notes(user_id, include_archived)               → list[dict]
search_notes(query, limit)                         → list[dict]

--- Tags ---
create_tag(name, color, admin_id)                  → int | None  (tag.id)
delete_tag(tag_id)                                 → bool
assign_tag(user_id, tag_id, admin_id)              → bool
remove_tag(user_id, tag_id)                        → bool
get_tags()                                         → list[dict]
get_user_tags(user_id)                             → list[dict]
search_by_tag(tag_id, limit, offset)               → list[dict]

--- Profile (priority / status) ---
get_or_create_profile(user_id, admin_id)           → dict
set_priority(user_id, priority, admin_id)          → bool
set_crm_status(user_id, status, admin_id, custom)  → bool

--- Reminders ---
add_reminder(user_id, admin_id, reason, remind_at) → int | None
complete_reminder(reminder_id, admin_id)            → bool
delete_reminder(reminder_id, admin_id)              → bool
get_reminders(user_id, pending_only)               → list[dict]
get_pending_reminders_all(admin_id)                → list[dict]

--- Dashboard stats ---
get_crm_stats()                                    → dict

--- Timeline ---
get_customer_timeline(user_id, limit)              → list[dict]

--- Export ---
export_customer_notes_text(user_id)               → str

Async helpers
-------------
reminder_check_job(context)  — call from job_queue, alerts admins of due reminders
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─── Config helpers ───────────────────────────────────────────────────────────

def _status() -> str:
    return str(cfg.get("crm_status", "enabled") or "enabled")

def _is_active() -> bool:
    return _status() in ("enabled", "maintenance")

def _allow_multiple_notes() -> bool:
    return cfg.get_bool("crm_allow_multiple_notes", True)

def _allow_tags() -> bool:
    return cfg.get_bool("crm_allow_tags", True)

def _allow_priority() -> bool:
    return cfg.get_bool("crm_allow_priority", True)

def _allow_reminders() -> bool:
    return cfg.get_bool("crm_allow_reminders", True)

def _allow_status() -> bool:
    return cfg.get_bool("crm_allow_internal_status", True)

def _max_notes() -> int:
    return cfg.get_int("crm_max_notes", 0)

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

def _fmt(dt: datetime | None) -> str:
    if not dt:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M")

# ─── Notes ────────────────────────────────────────────────────────────────────

def add_note(
    user_id: int,
    admin_id: int,
    admin_name: str | None,
    content: str,
) -> int | None:
    """Add an admin note on a user. Returns note.id or None on error/limit."""
    if not _is_active():
        return None
    content = (content or "").strip()
    if not content:
        return None
    try:
        from database.models import CustomerNote, CustomerProfile
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return None

            # Respect max_notes limit
            max_n = _max_notes()
            if max_n > 0:
                active_count = s.execute(text(
                    "SELECT COUNT(*) FROM customer_notes "
                    "WHERE user_id = :uid AND is_archived = FALSE"
                ), {"uid": user_id}).scalar() or 0
                if active_count >= max_n:
                    return None  # caller must inform admin

            note = CustomerNote(
                user_id=user_id,
                admin_id=admin_id,
                admin_name=admin_name,
                content=content,
                is_pinned=False,
                is_archived=False,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            s.add(note)
            s.flush()
            note_id = note.id

            # Keep notes_count in sync on profile
            if _table_ok(s, "customer_profiles"):
                s.execute(text("""
                    INSERT INTO customer_profiles (user_id, notes_count, updated_at, updated_by)
                    VALUES (:uid, 1, NOW(), :aid)
                    ON CONFLICT (user_id) DO UPDATE
                    SET notes_count = customer_profiles.notes_count + 1,
                        updated_at  = NOW(),
                        updated_by  = :aid
                """), {"uid": user_id, "aid": admin_id})

            return note_id
    except Exception:
        logger.debug("add_note failed", exc_info=True)
    return None


def edit_note(note_id: int, admin_id: int, new_content: str) -> bool:
    new_content = (new_content or "").strip()
    if not new_content:
        return False
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return False
            s.execute(text("""
                UPDATE customer_notes
                SET content = :content, updated_at = NOW()
                WHERE id = :nid
            """), {"content": new_content, "nid": note_id})
            return True
    except Exception:
        logger.debug("edit_note failed", exc_info=True)
    return False


def delete_note(note_id: int, user_id: int, admin_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return False
            s.execute(text(
                "DELETE FROM customer_notes WHERE id = :nid AND user_id = :uid"
            ), {"nid": note_id, "uid": user_id})
            # Decrement counter
            if _table_ok(s, "customer_profiles"):
                s.execute(text("""
                    UPDATE customer_profiles
                    SET notes_count = GREATEST(0, notes_count - 1),
                        updated_at  = NOW()
                    WHERE user_id = :uid
                """), {"uid": user_id})
            return True
    except Exception:
        logger.debug("delete_note failed", exc_info=True)
    return False


def pin_note(note_id: int, user_id: int) -> bool:
    """Toggle is_pinned for a note. Returns True on success."""
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return False
            s.execute(text("""
                UPDATE customer_notes
                SET is_pinned = NOT is_pinned, updated_at = NOW()
                WHERE id = :nid AND user_id = :uid
            """), {"nid": note_id, "uid": user_id})
            return True
    except Exception:
        logger.debug("pin_note failed", exc_info=True)
    return False


def archive_note(note_id: int, user_id: int, admin_id: int) -> bool:
    """Toggle is_archived. When archiving, decrements notes_count."""
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return False
            # Get current state
            row = s.execute(text(
                "SELECT is_archived FROM customer_notes WHERE id = :nid AND user_id = :uid"
            ), {"nid": note_id, "uid": user_id}).fetchone()
            if not row:
                return False
            was_archived = bool(row[0])
            s.execute(text("""
                UPDATE customer_notes
                SET is_archived = NOT is_archived, updated_at = NOW()
                WHERE id = :nid AND user_id = :uid
            """), {"nid": note_id, "uid": user_id})
            # Adjust counter
            if _table_ok(s, "customer_profiles"):
                delta = 1 if was_archived else -1   # un-archive adds, archive removes
                s.execute(text("""
                    UPDATE customer_profiles
                    SET notes_count = GREATEST(0, notes_count + :delta),
                        updated_at  = NOW()
                    WHERE user_id = :uid
                """), {"delta": delta, "uid": user_id})
            return True
    except Exception:
        logger.debug("archive_note failed", exc_info=True)
    return False


def get_notes(
    user_id: int,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return []
            cond = "" if include_archived else "AND is_archived = FALSE"
            rows = s.execute(text(f"""
                SELECT id, admin_id, admin_name, content, is_pinned,
                       is_archived, created_at, updated_at
                FROM   customer_notes
                WHERE  user_id = :uid {cond}
                ORDER  BY is_pinned DESC, created_at DESC
            """), {"uid": user_id}).fetchall()
            return [
                {
                    "id": r[0], "admin_id": r[1], "admin_name": r[2],
                    "content": r[3], "is_pinned": r[4], "is_archived": r[5],
                    "created_at": r[6], "updated_at": r[7],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_notes failed", exc_info=True)
    return []


def search_notes(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search across note content and user identifiers."""
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_notes"):
                return []
            q = f"%{query.strip()}%"
            rows = s.execute(text("""
                SELECT cn.id, cn.user_id, u.telegram_id, u.username,
                       cn.admin_name, cn.content, cn.is_pinned,
                       cn.created_at
                FROM   customer_notes cn
                JOIN   users u ON u.id = cn.user_id
                WHERE  (cn.content ILIKE :q
                     OR u.username ILIKE :q
                     OR cn.admin_name ILIKE :q)
                  AND  cn.is_archived = FALSE
                ORDER  BY cn.created_at DESC
                LIMIT  :lim
            """), {"q": q, "lim": limit}).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "telegram_id": r[2],
                    "username": r[3], "admin_name": r[4], "content": r[5],
                    "is_pinned": r[6], "created_at": r[7],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("search_notes failed", exc_info=True)
    return []

# ─── Tags ─────────────────────────────────────────────────────────────────────

def create_tag(name: str, color: str | None, admin_id: int) -> int | None:
    name = (name or "").strip()[:64]
    if not name:
        return None
    try:
        from database.models import CustomerTag
        with _get_db() as s:
            if not _table_ok(s, "customer_tags"):
                return None
            existing = s.execute(text(
                "SELECT id FROM customer_tags WHERE name ILIKE :name"
            ), {"name": name}).fetchone()
            if existing:
                return int(existing[0])  # idempotent
            tag = CustomerTag(
                name=name, color=color, created_by=admin_id,
                created_at=datetime.utcnow(),
            )
            s.add(tag)
            s.flush()
            return tag.id
    except Exception:
        logger.debug("create_tag failed", exc_info=True)
    return None


def delete_tag(tag_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tags"):
                return False
            s.execute(text("DELETE FROM customer_tags WHERE id = :tid"), {"tid": tag_id})
            return True
    except Exception:
        logger.debug("delete_tag failed", exc_info=True)
    return False


def assign_tag(user_id: int, tag_id: int, admin_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tag_assignments"):
                return False
            s.execute(text("""
                INSERT INTO customer_tag_assignments (user_id, tag_id, assigned_by, assigned_at)
                VALUES (:uid, :tid, :aid, NOW())
                ON CONFLICT (user_id, tag_id) DO NOTHING
            """), {"uid": user_id, "tid": tag_id, "aid": admin_id})
            return True
    except Exception:
        logger.debug("assign_tag failed", exc_info=True)
    return False


def remove_tag(user_id: int, tag_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tag_assignments"):
                return False
            s.execute(text(
                "DELETE FROM customer_tag_assignments WHERE user_id = :uid AND tag_id = :tid"
            ), {"uid": user_id, "tid": tag_id})
            return True
    except Exception:
        logger.debug("remove_tag failed", exc_info=True)
    return False


def get_tags() -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tags"):
                return []
            rows = s.execute(text(
                "SELECT id, name, color, created_by, created_at "
                "FROM customer_tags ORDER BY name ASC"
            )).fetchall()
            return [
                {"id": r[0], "name": r[1], "color": r[2],
                 "created_by": r[3], "created_at": r[4]}
                for r in rows
            ]
    except Exception:
        logger.debug("get_tags failed", exc_info=True)
    return []


def get_user_tags(user_id: int) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tag_assignments"):
                return []
            rows = s.execute(text("""
                SELECT ct.id, ct.name, ct.color, cta.assigned_at
                FROM   customer_tag_assignments cta
                JOIN   customer_tags ct ON ct.id = cta.tag_id
                WHERE  cta.user_id = :uid
                ORDER  BY ct.name ASC
            """), {"uid": user_id}).fetchall()
            return [
                {"id": r[0], "name": r[1], "color": r[2], "assigned_at": r[3]}
                for r in rows
            ]
    except Exception:
        logger.debug("get_user_tags failed", exc_info=True)
    return []


def search_by_tag(tag_id: int, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_tag_assignments"):
                return []
            rows = s.execute(text("""
                SELECT u.id, u.telegram_id, u.username, cta.assigned_at
                FROM   customer_tag_assignments cta
                JOIN   users u ON u.id = cta.user_id
                WHERE  cta.tag_id = :tid
                ORDER  BY cta.assigned_at DESC
                LIMIT  :lim OFFSET :off
            """), {"tid": tag_id, "lim": limit, "off": offset}).fetchall()
            return [
                {"user_id": r[0], "telegram_id": r[1],
                 "username": r[2], "assigned_at": r[3]}
                for r in rows
            ]
    except Exception:
        logger.debug("search_by_tag failed", exc_info=True)
    return []

# ─── Profile (priority / status) ──────────────────────────────────────────────

def get_or_create_profile(user_id: int, admin_id: int | None = None) -> dict[str, Any]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_profiles"):
                return {}
            row = s.execute(text("""
                SELECT id, priority, crm_status, custom_status,
                       notes_count, updated_at, updated_by
                FROM   customer_profiles WHERE user_id = :uid
            """), {"uid": user_id}).fetchone()
            if row:
                return {
                    "id": row[0], "priority": row[1], "crm_status": row[2],
                    "custom_status": row[3], "notes_count": row[4],
                    "updated_at": row[5], "updated_by": row[6],
                }
            # Create default profile
            s.execute(text("""
                INSERT INTO customer_profiles (user_id, updated_by)
                VALUES (:uid, :aid)
                ON CONFLICT (user_id) DO NOTHING
            """), {"uid": user_id, "aid": admin_id})
            return {
                "id": None, "priority": "low", "crm_status": "new_customer",
                "custom_status": None, "notes_count": 0,
                "updated_at": None, "updated_by": admin_id,
            }
    except Exception:
        logger.debug("get_or_create_profile failed", exc_info=True)
    return {}


def set_priority(user_id: int, priority: str, admin_id: int) -> bool:
    valid = {"low", "medium", "high", "critical"}
    if priority not in valid:
        return False
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_profiles"):
                return False
            s.execute(text("""
                INSERT INTO customer_profiles (user_id, priority, updated_at, updated_by)
                VALUES (:uid, :prio, NOW(), :aid)
                ON CONFLICT (user_id) DO UPDATE
                SET priority   = :prio,
                    updated_at = NOW(),
                    updated_by = :aid
            """), {"uid": user_id, "prio": priority, "aid": admin_id})
            return True
    except Exception:
        logger.debug("set_priority failed", exc_info=True)
    return False


def set_crm_status(
    user_id: int,
    status: str,
    admin_id: int,
    custom_label: str | None = None,
) -> bool:
    valid = {
        "new_customer", "returning", "vip", "reseller",
        "wholesale", "blocked", "suspended", "verified", "custom",
    }
    if status not in valid:
        return False
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_profiles"):
                return False
            s.execute(text("""
                INSERT INTO customer_profiles
                    (user_id, crm_status, custom_status, updated_at, updated_by)
                VALUES (:uid, :st, :cs, NOW(), :aid)
                ON CONFLICT (user_id) DO UPDATE
                SET crm_status    = :st,
                    custom_status = :cs,
                    updated_at    = NOW(),
                    updated_by    = :aid
            """), {"uid": user_id, "st": status,
                   "cs": custom_label, "aid": admin_id})
            return True
    except Exception:
        logger.debug("set_crm_status failed", exc_info=True)
    return False

# ─── Reminders ────────────────────────────────────────────────────────────────

def add_reminder(
    user_id: int,
    admin_id: int,
    reason: str,
    remind_at: datetime,
) -> int | None:
    reason = (reason or "").strip()
    if not reason:
        return None
    try:
        from database.models import CustomerReminder
        with _get_db() as s:
            if not _table_ok(s, "customer_reminders"):
                return None
            rem = CustomerReminder(
                user_id=user_id,
                admin_id=admin_id,
                reason=reason,
                remind_at=remind_at,
                is_completed=False,
                created_at=datetime.utcnow(),
            )
            s.add(rem)
            s.flush()
            return rem.id
    except Exception:
        logger.debug("add_reminder failed", exc_info=True)
    return None


def complete_reminder(reminder_id: int, admin_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_reminders"):
                return False
            s.execute(text("""
                UPDATE customer_reminders
                SET is_completed = TRUE, completed_at = NOW()
                WHERE id = :rid AND admin_id = :aid
            """), {"rid": reminder_id, "aid": admin_id})
            return True
    except Exception:
        logger.debug("complete_reminder failed", exc_info=True)
    return False


def delete_reminder(reminder_id: int, admin_id: int) -> bool:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_reminders"):
                return False
            s.execute(text(
                "DELETE FROM customer_reminders WHERE id = :rid AND admin_id = :aid"
            ), {"rid": reminder_id, "aid": admin_id})
            return True
    except Exception:
        logger.debug("delete_reminder failed", exc_info=True)
    return False


def get_reminders(user_id: int, pending_only: bool = False) -> list[dict[str, Any]]:
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_reminders"):
                return []
            cond = "AND is_completed = FALSE" if pending_only else ""
            rows = s.execute(text(f"""
                SELECT id, admin_id, reason, remind_at, is_completed, completed_at, created_at
                FROM   customer_reminders
                WHERE  user_id = :uid {cond}
                ORDER  BY is_completed ASC, remind_at ASC
            """), {"uid": user_id}).fetchall()
            return [
                {
                    "id": r[0], "admin_id": r[1], "reason": r[2],
                    "remind_at": r[3], "is_completed": r[4],
                    "completed_at": r[5], "created_at": r[6],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_reminders failed", exc_info=True)
    return []


def get_pending_reminders_all(admin_id: int | None = None) -> list[dict[str, Any]]:
    """All pending reminders due now or overdue, optionally filtered by admin."""
    try:
        with _get_db() as s:
            if not _table_ok(s, "customer_reminders"):
                return []
            cond = "AND cr.admin_id = :aid" if admin_id else ""
            params: dict = {"aid": admin_id} if admin_id else {}
            rows = s.execute(text(f"""
                SELECT cr.id, cr.user_id, u.telegram_id, u.username,
                       cr.admin_id, cr.reason, cr.remind_at, cr.created_at
                FROM   customer_reminders cr
                JOIN   users u ON u.id = cr.user_id
                WHERE  cr.is_completed = FALSE
                  AND  cr.remind_at <= NOW()
                  {cond}
                ORDER  BY cr.remind_at ASC
                LIMIT  50
            """), params).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "telegram_id": r[2],
                    "username": r[3], "admin_id": r[4], "reason": r[5],
                    "remind_at": r[6], "created_at": r[7],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_pending_reminders_all failed", exc_info=True)
    return []

# ─── Dashboard stats ──────────────────────────────────────────────────────────

def get_crm_stats() -> dict[str, Any]:
    stats: dict[str, Any] = {
        "total_notes": 0, "vip_count": 0, "wholesale_count": 0,
        "with_reminders": 0, "pending_reminders": 0,
        "completed_reminders": 0, "total_tags": 0, "total_profiles": 0,
    }
    try:
        with _get_db() as s:
            ok_n  = _table_ok(s, "customer_notes")
            ok_p  = _table_ok(s, "customer_profiles")
            ok_r  = _table_ok(s, "customer_reminders")
            ok_t  = _table_ok(s, "customer_tags")

            if ok_n:
                stats["total_notes"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM customer_notes WHERE is_archived = FALSE"
                    )).scalar() or 0
                )
            if ok_p:
                stats["vip_count"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM customer_profiles WHERE crm_status = 'vip'"
                    )).scalar() or 0
                )
                stats["wholesale_count"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM customer_profiles WHERE crm_status = 'wholesale'"
                    )).scalar() or 0
                )
                stats["total_profiles"] = (
                    s.execute(text("SELECT COUNT(*) FROM customer_profiles")).scalar() or 0
                )
            if ok_r:
                stats["pending_reminders"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM customer_reminders WHERE is_completed = FALSE"
                    )).scalar() or 0
                )
                stats["completed_reminders"] = (
                    s.execute(text(
                        "SELECT COUNT(*) FROM customer_reminders WHERE is_completed = TRUE"
                    )).scalar() or 0
                )
                stats["with_reminders"] = (
                    s.execute(text(
                        "SELECT COUNT(DISTINCT user_id) FROM customer_reminders "
                        "WHERE is_completed = FALSE"
                    )).scalar() or 0
                )
            if ok_t:
                stats["total_tags"] = (
                    s.execute(text("SELECT COUNT(*) FROM customer_tags")).scalar() or 0
                )
    except Exception:
        logger.debug("get_crm_stats failed", exc_info=True)
    return stats

# ─── Timeline ─────────────────────────────────────────────────────────────────

def get_customer_timeline(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Merge events from activity_logs, customer_notes, and customer_reminders."""
    events: list[dict[str, Any]] = []
    try:
        with _get_db() as s:
            # Activity logs (registrations, logins, purchases, deposits, etc.)
            if _table_ok(s, "activity_logs"):
                rows = s.execute(text("""
                    SELECT action, details, created_at FROM activity_logs
                    WHERE user_id = :uid ORDER BY created_at DESC LIMIT :lim
                """), {"uid": user_id, "lim": limit}).fetchall()
                for r in rows:
                    events.append({
                        "type": "activity", "action": r[0],
                        "detail": r[1], "when": r[2],
                    })

            # Admin notes
            if _table_ok(s, "customer_notes"):
                rows = s.execute(text("""
                    SELECT 'note', admin_name, content, created_at
                    FROM   customer_notes
                    WHERE  user_id = :uid AND is_archived = FALSE
                    ORDER  BY created_at DESC LIMIT :lim
                """), {"uid": user_id, "lim": limit}).fetchall()
                for r in rows:
                    events.append({
                        "type": "note", "action": f"Note by {r[1] or 'admin'}",
                        "detail": (r[2] or "")[:80], "when": r[3],
                    })

            # Reminders (completed ones shown as events)
            if _table_ok(s, "customer_reminders"):
                rows = s.execute(text("""
                    SELECT reason, completed_at FROM customer_reminders
                    WHERE  user_id = :uid AND is_completed = TRUE
                    ORDER  BY completed_at DESC LIMIT :lim
                """), {"uid": user_id, "lim": limit}).fetchall()
                for r in rows:
                    events.append({
                        "type": "reminder_done",
                        "action": "Reminder completed",
                        "detail": (r[0] or "")[:80],
                        "when": r[1],
                    })

    except Exception:
        logger.debug("get_customer_timeline failed", exc_info=True)

    # Sort all events newest-first and trim
    events.sort(key=lambda e: (e.get("when") or datetime.min), reverse=True)
    return events[:limit]

# ─── Export ───────────────────────────────────────────────────────────────────

def export_customer_notes_text(user_id: int, username: str | None = None) -> str:
    """Return a plaintext export of all notes for a user."""
    notes = get_notes(user_id, include_archived=True)
    profile = get_or_create_profile(user_id)
    tags = get_user_tags(user_id)

    header = (
        f"=== Customer CRM Export ===\n"
        f"User ID:  {user_id}\n"
        f"Username: {'@' + username if username else 'N/A'}\n"
        f"Priority: {profile.get('priority', 'N/A')}\n"
        f"Status:   {profile.get('crm_status', 'N/A')}"
    )
    if profile.get("custom_status"):
        header += f" ({profile['custom_status']})"
    header += f"\nTags:     {', '.join(t['name'] for t in tags) or 'None'}\n"
    header += f"Exported: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
    header += "=" * 27 + "\n\n"

    if not notes:
        return header + "No notes found."

    lines = []
    for n in notes:
        pin = "📌 " if n["is_pinned"] else ""
        arc = "[ARCHIVED] " if n["is_archived"] else ""
        when = _fmt(n.get("created_at"))
        edited = _fmt(n.get("updated_at")) if n.get("updated_at") != n.get("created_at") else None
        lines.append(
            f"{pin}{arc}[{when}] by {n.get('admin_name') or 'Admin'}"
            + (f"  (edited {edited})" if edited else "")
        )
        lines.append(n.get("content", ""))
        lines.append("-" * 40)

    return header + "\n".join(lines)

# ─── Async: reminder check job ────────────────────────────────────────────────

async def reminder_check_job(context) -> None:
    """Hourly job: DM each admin their overdue reminders once.

    Only runs when the feature is enabled.  Each triggered reminder is NOT
    auto-completed — the admin must explicitly mark it done.
    """
    if not _is_active() or not _allow_reminders():
        return
    try:
        from utils.settings import settings
        admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", None)
        if not admin_id:
            return

        due = get_pending_reminders_all()
        if not due:
            return

        # Group by admin_id; for simplicity, notify the primary admin
        grouped: dict[int, list] = {}
        for rem in due:
            a = rem["admin_id"]
            grouped.setdefault(a, []).append(rem)

        for aid, rems in grouped.items():
            lines = ["📅 <b>CRM Follow-up Reminders Due</b>\n"]
            for r in rems[:10]:  # cap at 10 per message
                uname = f"@{r['username']}" if r.get("username") else f"TG#{r['telegram_id']}"
                when  = _fmt(r.get("remind_at"))
                lines.append(
                    f"• {uname}  ⏰ {when}\n"
                    f"  {(r.get('reason') or '')[:80]}"
                )
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text="\n".join(lines),
                    parse_mode="HTML",
                )
            except Exception:
                pass  # admin may have blocked the bot
    except Exception:
        logger.debug("reminder_check_job failed (non-fatal)", exc_info=True)
