"""Anti-Spam & Auto-Moderation Service — V40.

Provides:
  • In-memory rate-limit buckets (per user, per action type)
  • Spam violation detection for all major abuse patterns
  • Automatic action escalation: warning → cooldown → mute → ban
  • Whitelist / blacklist checks
  • Moderation status queries (muted? banned? cooldown? captcha?)
  • Admin action functions (mute, unmute, ban, unban, whitelist, etc.)
  • Statistics aggregation for the admin panel

Designed to be called from a PTB handler group -1 middleware that fires
before all regular handlers so blocked users never reach feature handlers.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from database import get_db_session
from database.models import (
    UserModerationStatus, SpamLog, ModerationActionLog,
    BlacklistEntry, WhitelistEntry,
    SpamViolationType, ModerationActionType,
    ModerationStatusType, BlacklistEntryType, WhitelistEntryType,
)

logger = logging.getLogger(__name__)

# ─── In-memory rate-limit store ──────────────────────────────────────────────
# Buckets: { tg_id: deque of timestamps }
_buckets: Dict[str, Dict[int, deque]] = defaultdict(lambda: defaultdict(deque))
# Captcha pending (in-memory): set of tg_id
_captcha_pending: set = set()
# Cache for whitelist/blacklist to avoid per-message DB calls
_wl_cache: Dict[int, bool] = {}    # tg_id → True if whitelisted
_bl_cache: Dict[int, bool] = {}    # tg_id → True if blacklisted (user)
_word_blacklist: List[str] = []    # updated on load
_wl_cache_ts: float = 0.0
_BL_CACHE_TTL = 120  # seconds

# ─── Config defaults (overridden via BotConfig) ───────────────────────────────
DEFAULT_CFG = {
    "antispam_status":            "enabled",
    "antispam_max_cmds_per_min":  10,
    "antispam_max_clicks_per_min":20,
    "antispam_max_msgs_per_min":  15,
    "antispam_cooldown_secs":     60,
    "antispam_max_warnings":      3,
    "antispam_auto_mute":         True,
    "antispam_auto_ban":          False,
    "antispam_mute_secs":         300,     # 5 min default mute
    "antispam_ban_secs":          86400,   # 24 hr temp ban
    "antispam_flood_window_secs": 10,
    "antispam_flood_threshold":   8,
    "antispam_captcha_on_new":    False,
}


def _cfg(key: str) -> Any:
    try:
        from utils.bot_config import cfg
        val = cfg.get(key)
        if val is not None:
            return val
    except Exception:
        pass
    return DEFAULT_CFG.get(key)


def _cfg_int(key: str) -> int:
    try:
        return int(_cfg(key))
    except (TypeError, ValueError):
        return int(DEFAULT_CFG.get(key, 0))


def _cfg_bool(key: str) -> bool:
    v = _cfg(key)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


# ─── Cache helpers ────────────────────────────────────────────────────────────

def _refresh_list_caches() -> None:
    global _wl_cache, _bl_cache, _word_blacklist, _wl_cache_ts
    now = time.monotonic()
    if now - _wl_cache_ts < _BL_CACHE_TTL:
        return
    try:
        with get_db_session() as s:
            _wl_cache = {
                r.telegram_id: True
                for r in s.query(WhitelistEntry).filter_by(is_active=True).all()
            }
            _bl_cache = {
                int(r.value): True
                for r in s.query(BlacklistEntry)
                .filter_by(entry_type=BlacklistEntryType.USER.value, is_active=True).all()
            }
            _word_blacklist = [
                r.value.lower()
                for r in s.query(BlacklistEntry)
                .filter_by(entry_type=BlacklistEntryType.WORD.value, is_active=True).all()
            ]
            _wl_cache_ts = now
    except Exception:
        logger.warning("_refresh_list_caches failed")


def is_whitelisted(tg_id: int) -> bool:
    _refresh_list_caches()
    return tg_id in _wl_cache


def is_blacklisted_user(tg_id: int) -> bool:
    _refresh_list_caches()
    return tg_id in _bl_cache


def contains_blacklisted_word(text: str) -> bool:
    _refresh_list_caches()
    text_lower = text.lower()
    return any(w in text_lower for w in _word_blacklist)


# ─── Moderation status queries ────────────────────────────────────────────────

def _get_or_create_status(session, tg_id: int) -> UserModerationStatus:
    ms = session.query(UserModerationStatus).filter_by(telegram_id=tg_id).first()
    if ms is None:
        ms = UserModerationStatus(telegram_id=tg_id)
        session.add(ms)
        session.flush()
    return ms


def get_user_status(tg_id: int) -> Dict[str, Any]:
    """Return current moderation state for a user."""
    with get_db_session() as s:
        ms = s.query(UserModerationStatus).filter_by(telegram_id=tg_id).first()
        if not ms:
            return {"status": "active", "is_muted": False, "is_banned": False,
                    "needs_captcha": False, "warning_count": 0}
        now = datetime.utcnow()
        # Auto-expire mutes / bans
        muted  = ms.is_muted  and (not ms.mute_expires_at  or ms.mute_expires_at  > now)
        banned = ms.is_banned and (not ms.ban_expires_at   or ms.ban_expires_at   > now)
        cooldown = ms.is_in_cooldown and (not ms.cooldown_expires or ms.cooldown_expires > now)
        return {
            "status":         ms.status,
            "is_muted":       muted,
            "mute_expires_at":ms.mute_expires_at,
            "is_banned":      banned,
            "ban_expires_at": ms.ban_expires_at,
            "is_in_cooldown": cooldown,
            "cooldown_expires": ms.cooldown_expires,
            "needs_captcha":  ms.needs_captcha,
            "warning_count":  ms.warning_count,
            "total_violations": ms.total_violations,
            "under_review":   ms.under_review,
        }


def is_blocked(tg_id: int) -> Tuple[bool, str]:
    """Return (True, reason) if user is banned / muted / blacklisted."""
    if is_blacklisted_user(tg_id):
        return True, "blacklisted"
    st = get_user_status(tg_id)
    if st["is_banned"]:
        return True, "banned"
    return False, ""


def can_interact(tg_id: int) -> Tuple[bool, str]:
    """Return (True, "") if user may interact; (False, reason) if blocked, muted or on cooldown."""
    if is_blacklisted_user(tg_id):
        return False, "blacklisted"
    st = get_user_status(tg_id)
    if st["is_banned"]:
        return False, "banned"
    if st["is_muted"]:
        return False, "muted"
    if st["is_in_cooldown"]:
        return False, "cooldown"
    if st["needs_captcha"]:
        return False, "captcha"
    return True, ""


# ─── Rate limiting ────────────────────────────────────────────────────────────

def _push_event(tg_id: int, bucket: str) -> int:
    """Push current timestamp into user's bucket; return count in current window."""
    window  = _cfg_int("antispam_flood_window_secs") or 10
    now     = time.monotonic()
    dq      = _buckets[bucket][tg_id]
    dq.append(now)
    cutoff  = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def check_rate_limit(tg_id: int, event_type: str = "msg") -> bool:
    """Return True if the user exceeds their rate limit for event_type.

    event_type: 'msg' | 'cmd' | 'click'
    """
    limits = {
        "msg":   _cfg_int("antispam_max_msgs_per_min")  or 15,
        "cmd":   _cfg_int("antispam_max_cmds_per_min")  or 10,
        "click": _cfg_int("antispam_max_clicks_per_min") or 20,
    }
    limit = limits.get(event_type, 15)
    count = _push_event(tg_id, event_type)
    return count > limit


def check_flood(tg_id: int) -> bool:
    """Return True if user is flooding (many events in short flood window)."""
    threshold = _cfg_int("antispam_flood_threshold") or 8
    count = _push_event(tg_id, "flood")
    return count > threshold


# ─── Violation recording & escalation ────────────────────────────────────────

def _log_violation(tg_id: int, username: Optional[str],
                   violation_type: str, action_taken: str,
                   detail: str = "", raw_data: Optional[str] = None) -> None:
    try:
        with get_db_session() as s:
            s.add(SpamLog(
                telegram_id=tg_id,
                username=username,
                violation_type=violation_type,
                action_taken=action_taken,
                detail=detail[:500] if detail else None,
                raw_data=raw_data,
            ))
            s.commit()
    except Exception:
        logger.warning("_log_violation DB failed")


def _log_action(tg_id: int, action_type: str, duration_secs: Optional[int] = None,
                expires_at: Optional[datetime] = None, reason: str = "",
                actor_type: str = "system", actor_id: Optional[int] = None) -> None:
    try:
        with get_db_session() as s:
            s.add(ModerationActionLog(
                target_tg_id=tg_id,
                action_type=action_type,
                duration_secs=duration_secs,
                expires_at=expires_at,
                reason=(reason or "")[:255],
                actor_type=actor_type,
                actor_id=actor_id,
            ))
            s.commit()
    except Exception:
        logger.warning("_log_action DB failed")


def record_violation(tg_id: int, username: Optional[str],
                     violation_type: str, detail: str = "") -> Dict[str, Any]:
    """Record a violation and apply automatic escalation.

    Returns dict with 'action' (what was done) and 'message' (to show user).
    """
    if not _cfg_bool("antispam_status") and _cfg("antispam_status") != "enabled":
        return {"action": "none", "message": ""}

    if is_whitelisted(tg_id):
        return {"action": "none", "message": ""}

    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.username = username
        ms.warning_count    = (ms.warning_count or 0) + 1
        ms.total_violations = (ms.total_violations or 0) + 1
        ms.last_violation_at = datetime.utcnow()
        warn_count = ms.warning_count
        s.commit()

    max_warnings = _cfg_int("antispam_max_warnings") or 3
    auto_mute    = _cfg_bool("antispam_auto_mute")
    auto_ban     = _cfg_bool("antispam_auto_ban")
    mute_secs    = _cfg_int("antispam_mute_secs")  or 300
    ban_secs     = _cfg_int("antispam_ban_secs")   or 86400
    cooldown_s   = _cfg_int("antispam_cooldown_secs") or 60

    action   = ModerationActionType.WARNING.value
    message  = f"⚠️ Warning {warn_count}/{max_warnings}: {_violation_label(violation_type)}"

    if warn_count >= max_warnings * 3 and auto_ban:
        _apply_temp_ban(tg_id, username, ban_secs, reason=f"Auto-ban: {violation_type}")
        action  = ModerationActionType.TEMP_BAN.value
        message = f"🚫 You have been temporarily banned for {ban_secs//60} minutes due to repeated violations."

    elif warn_count >= max_warnings and auto_mute:
        _apply_mute(tg_id, username, mute_secs, reason=f"Auto-mute: {violation_type}")
        action  = ModerationActionType.MUTE.value
        message = f"🔇 You have been muted for {mute_secs//60} minutes due to repeated violations."

    elif warn_count >= max_warnings:
        _apply_cooldown(tg_id, cooldown_s)
        action  = ModerationActionType.COOLDOWN.value
        message = f"⏳ Slow down! You are on a {cooldown_s}s cooldown."

    _log_violation(tg_id, username, violation_type, action, detail)
    _log_action(tg_id, action, reason=f"auto:{violation_type}")

    return {"action": action, "message": message, "warning_count": warn_count}


def _violation_label(vt: str) -> str:
    return {
        SpamViolationType.FLOOD.value:             "Message flood",
        SpamViolationType.REPEATED_COMMAND.value:  "Repeated commands",
        SpamViolationType.REPEATED_MESSAGE.value:  "Repeated messages",
        SpamViolationType.RAPID_CLICKS.value:      "Rapid button clicks",
        SpamViolationType.FAKE_REFERRAL.value:     "Fake referral attempt",
        SpamViolationType.DUPLICATE_PAYMENT.value: "Duplicate payment attempt",
        SpamViolationType.FAILED_PAYMENTS.value:   "Too many failed payments",
        SpamViolationType.REFERRAL_ABUSE.value:    "Referral abuse",
        SpamViolationType.COUPON_ABUSE.value:      "Coupon abuse",
        SpamViolationType.BOT_ABUSE.value:         "Bot abuse",
        SpamViolationType.BLACKLISTED_WORD.value:  "Blacklisted content",
        SpamViolationType.MANUAL.value:            "Manual moderation action",
    }.get(vt, vt)


# ─── Action functions ─────────────────────────────────────────────────────────

def _apply_mute(tg_id: int, username: Optional[str], duration_secs: int,
                reason: str = "", actor_type: str = "system",
                actor_id: Optional[int] = None) -> None:
    expires = datetime.utcnow() + timedelta(seconds=duration_secs)
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.username        = username or ms.username
        ms.is_muted        = True
        ms.mute_expires_at = expires
        ms.status          = ModerationStatusType.MUTED.value
        s.commit()
    _log_action(tg_id, ModerationActionType.MUTE.value,
                duration_secs=duration_secs, expires_at=expires,
                reason=reason, actor_type=actor_type, actor_id=actor_id)


def _apply_temp_ban(tg_id: int, username: Optional[str], duration_secs: int,
                    reason: str = "", actor_type: str = "system",
                    actor_id: Optional[int] = None) -> None:
    expires = datetime.utcnow() + timedelta(seconds=duration_secs)
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.username       = username or ms.username
        ms.is_banned      = True
        ms.ban_type       = "temp"
        ms.ban_expires_at = expires
        ms.status         = ModerationStatusType.BANNED.value
        s.commit()
    _log_action(tg_id, ModerationActionType.TEMP_BAN.value,
                duration_secs=duration_secs, expires_at=expires,
                reason=reason, actor_type=actor_type, actor_id=actor_id)
    # Invalidate cache
    _bl_cache.pop(tg_id, None)


def _apply_cooldown(tg_id: int, duration_secs: int) -> None:
    expires = datetime.utcnow() + timedelta(seconds=duration_secs)
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.is_in_cooldown  = True
        ms.cooldown_expires = expires
        ms.status           = ModerationStatusType.COOLDOWN.value
        s.commit()


# ─── Admin-callable moderation functions ─────────────────────────────────────

def admin_mute(tg_id: int, duration_secs: int, reason: str = "",
               actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        u = s.query(UserModerationStatus).filter_by(telegram_id=tg_id).first()
        username = u.username if u else None
    _apply_mute(tg_id, username, duration_secs, reason=reason,
                actor_type="admin", actor_id=actor_id)


def admin_unmute(tg_id: int, reason: str = "", actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.is_muted        = False
        ms.mute_expires_at = None
        ms.status          = ModerationStatusType.ACTIVE.value
        s.commit()
    _log_action(tg_id, ModerationActionType.UNMUTE.value,
                reason=reason, actor_type="admin", actor_id=actor_id)


def admin_ban(tg_id: int, permanent: bool = False, duration_secs: int = 86400,
              reason: str = "", actor_id: Optional[int] = None) -> None:
    if permanent:
        expires = None
        ban_type = "perm"
    else:
        expires  = datetime.utcnow() + timedelta(seconds=duration_secs)
        ban_type = "temp"
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.is_banned      = True
        ms.ban_type       = ban_type
        ms.ban_expires_at = expires
        ms.status         = ModerationStatusType.BANNED.value
        s.commit()
    action = ModerationActionType.PERM_BAN.value if permanent else ModerationActionType.TEMP_BAN.value
    _log_action(tg_id, action, duration_secs=None if permanent else duration_secs,
                expires_at=expires, reason=reason, actor_type="admin", actor_id=actor_id)
    _bl_cache.pop(tg_id, None)


def admin_unban(tg_id: int, reason: str = "", actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        if ms:
            ms.is_banned      = False
            ms.ban_type       = None
            ms.ban_expires_at = None
            ms.status         = ModerationStatusType.ACTIVE.value
            s.commit()
    _log_action(tg_id, ModerationActionType.UNBAN.value,
                reason=reason, actor_type="admin", actor_id=actor_id)
    _bl_cache.pop(tg_id, None)


def admin_clear_warnings(tg_id: int, actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        ms = _get_or_create_status(s, tg_id)
        ms.warning_count = 0
        ms.status        = ModerationStatusType.ACTIVE.value
        s.commit()
    _log_action(tg_id, ModerationActionType.CLEAR_WARNINGS.value,
                actor_type="admin", actor_id=actor_id)


def admin_add_whitelist(tg_id: int, entry_type: str = "trusted",
                        reason: str = "", actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        exists = (s.query(WhitelistEntry)
                  .filter_by(telegram_id=tg_id, entry_type=entry_type).first())
        if exists:
            exists.is_active = True
        else:
            s.add(WhitelistEntry(
                telegram_id=tg_id, entry_type=entry_type,
                reason=reason, added_by=actor_id, is_active=True,
            ))
        s.commit()
    _wl_cache[tg_id] = True
    _log_action(tg_id, ModerationActionType.WHITELIST_ADD.value,
                reason=reason, actor_type="admin", actor_id=actor_id)


def admin_add_blacklist_word(word: str, reason: str = "",
                              actor_id: Optional[int] = None) -> None:
    word = word.lower().strip()
    with get_db_session() as s:
        exists = (s.query(BlacklistEntry)
                  .filter_by(entry_type=BlacklistEntryType.WORD.value, value=word).first())
        if exists:
            exists.is_active = True
        else:
            s.add(BlacklistEntry(
                entry_type=BlacklistEntryType.WORD.value,
                value=word, reason=reason, added_by=actor_id, is_active=True,
            ))
        s.commit()
    global _wl_cache_ts
    _wl_cache_ts = 0  # force cache refresh


def admin_add_blacklist_user(tg_id: int, reason: str = "",
                              actor_id: Optional[int] = None) -> None:
    with get_db_session() as s:
        exists = (s.query(BlacklistEntry)
                  .filter_by(entry_type=BlacklistEntryType.USER.value,
                              value=str(tg_id)).first())
        if exists:
            exists.is_active = True
        else:
            s.add(BlacklistEntry(
                entry_type=BlacklistEntryType.USER.value,
                value=str(tg_id), reason=reason, added_by=actor_id, is_active=True,
            ))
        s.commit()
    _bl_cache[tg_id] = True
    _log_action(tg_id, ModerationActionType.BLACKLIST_ADD.value,
                reason=reason, actor_type="admin", actor_id=actor_id)


# ─── Statistics ───────────────────────────────────────────────────────────────

def get_stats() -> Dict[str, int]:
    try:
        with get_db_session() as s:
            now = datetime.utcnow()
            return {
                "spam_attempts": s.query(SpamLog).count(),
                "spam_today":    s.query(SpamLog)
                                  .filter(SpamLog.created_at >= now.replace(hour=0, minute=0, second=0))
                                  .count(),
                "blocked_users": s.query(BlacklistEntry)
                                   .filter_by(entry_type=BlacklistEntryType.USER.value, is_active=True)
                                   .count(),
                "muted_users":   s.query(UserModerationStatus).filter_by(is_muted=True).count(),
                "banned_users":  s.query(UserModerationStatus).filter_by(is_banned=True).count(),
                "total_warnings":s.query(UserModerationStatus)
                                   .filter(UserModerationStatus.warning_count > 0).count(),
                "captcha_pending": s.query(UserModerationStatus).filter_by(needs_captcha=True).count(),
                "whitelisted":   s.query(WhitelistEntry).filter_by(is_active=True).count(),
            }
    except Exception:
        logger.exception("get_stats failed")
        return {}


def get_recent_violations(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db_session() as s:
        rows = (s.query(SpamLog)
                .order_by(SpamLog.created_at.desc())
                .limit(limit).all())
        return [
            {
                "id":             r.id,
                "telegram_id":    r.telegram_id,
                "username":       r.username or "",
                "violation_type": r.violation_type,
                "action_taken":   r.action_taken,
                "detail":         r.detail or "",
                "created_at":     r.created_at,
            }
            for r in rows
        ]


def get_all_blacklist(entry_type: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_session() as s:
        q = s.query(BlacklistEntry).filter_by(is_active=True)
        if entry_type:
            q = q.filter_by(entry_type=entry_type)
        rows = q.order_by(BlacklistEntry.created_at.desc()).all()
        return [{"id": r.id, "type": r.entry_type, "value": r.value,
                 "reason": r.reason or "", "created_at": r.created_at} for r in rows]


def get_all_whitelist() -> List[Dict[str, Any]]:
    with get_db_session() as s:
        rows = s.query(WhitelistEntry).filter_by(is_active=True).all()
        return [{"id": r.id, "telegram_id": r.telegram_id, "type": r.entry_type,
                 "reason": r.reason or ""} for r in rows]


def search_user_violations(tg_id: int) -> Dict[str, Any]:
    """Return complete moderation history for a user."""
    status = get_user_status(tg_id)
    with get_db_session() as s:
        logs   = (s.query(SpamLog)
                  .filter_by(telegram_id=tg_id)
                  .order_by(SpamLog.created_at.desc())
                  .limit(20).all())
        actions= (s.query(ModerationActionLog)
                  .filter_by(target_tg_id=tg_id)
                  .order_by(ModerationActionLog.created_at.desc())
                  .limit(10).all())
        return {
            "status":  status,
            "violations": [
                {"type": l.violation_type, "action": l.action_taken,
                 "detail": l.detail, "when": l.created_at}
                for l in logs
            ],
            "actions": [
                {"type": a.action_type, "reason": a.reason,
                 "expires": a.expires_at, "actor": a.actor_type, "when": a.created_at}
                for a in actions
            ],
        }


# ─── PTB Middleware ───────────────────────────────────────────────────────────

async def antispam_middleware(update, context) -> None:
    """Handler group=-1 middleware — blocks banned/muted users and tracks rate limits.

    Import and register in bot.py:
        from services.anti_spam import antispam_middleware
        application.add_handler(TypeHandler(Update, antispam_middleware), group=-1)
    """
    from telegram.ext import TypeHandler  # noqa (just for documentation)

    if not update.effective_user:
        return

    # Guard: only run when feature is enabled
    status = _cfg("antispam_status")
    if status not in ("enabled", None):
        return

    tg_id    = update.effective_user.id
    username = update.effective_user.username

    # Whitelist bypass
    if is_whitelisted(tg_id):
        return

    # Check blacklist / ban / mute
    ok, reason = can_interact(tg_id)
    if not ok:
        msg_map = {
            "blacklisted": "🚫 Your account has been blacklisted.",
            "banned":      "🚫 Your account is banned from using this bot.",
            "muted":       "🔇 You are muted. Please wait before sending messages.",
            "cooldown":    "⏳ You are on cooldown. Please wait a moment.",
            "captcha":     "🤖 Please complete the verification first. Contact support.",
        }
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    msg_map.get(reason, "❌ Access denied."), show_alert=True
                )
            except Exception:
                pass
        elif update.message:
            try:
                await update.message.reply_text(msg_map.get(reason, "❌ Access denied."))
            except Exception:
                pass
        return  # Drop the update; do NOT call next handler

    # Determine event type
    if update.message:
        text = update.message.text or ""
        event_type = "cmd" if text.startswith("/") else "msg"

        # Flood detection
        if check_flood(tg_id):
            record_violation(tg_id, username,
                             SpamViolationType.FLOOD.value, "flood detected")

        # Rate limit
        elif check_rate_limit(tg_id, event_type):
            vtype = (SpamViolationType.REPEATED_COMMAND.value if event_type == "cmd"
                     else SpamViolationType.REPEATED_MESSAGE.value)
            record_violation(tg_id, username, vtype, f"rate limit: {event_type}")

        # Word blacklist
        elif text and contains_blacklisted_word(text):
            record_violation(tg_id, username,
                             SpamViolationType.BLACKLISTED_WORD.value,
                             f"word: {text[:50]}")

    elif update.callback_query:
        if check_rate_limit(tg_id, "click"):
            record_violation(tg_id, username,
                             SpamViolationType.RAPID_CLICKS.value, "click flood")
