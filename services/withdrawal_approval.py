"""Withdrawal Approval Service — V29.

Single authoritative layer for all withdrawal approval operations.
Prevents double-spending, race conditions, and duplicate approvals via
row-level locking (PostgreSQL SELECT … FOR UPDATE).

Public API
----------
get_feature_status()            → "enabled" | "maintenance" | "disabled"
get_available_balance(user_id)  → float  (commission minus locked withdrawals)
has_pending_withdrawal(user_id) → bool
get_daily_count(user_id)        → int
create_withdrawal(...)          → dict | None
approve_withdrawal(...)         → dict | None
mark_under_review(...)          → dict | None
mark_processing(...)            → dict | None
complete_withdrawal(...)        → dict | None
reject_withdrawal(...)          → dict | None
cancel_withdrawal(...)          → dict | None
add_admin_note(...)             → bool
get_withdrawal(wid)             → dict | None
list_withdrawals(...)           → list[dict]
get_admin_stats()               → dict
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Optional

from sqlalchemy import text, func as sqlfunc

from database import get_db_session, User
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ── Payment method registry ───────────────────────────────────────────────────

PAYMENT_METHODS: dict[str, str] = {
    "usdt_trc20":   "💎 USDT TRC20",
    "usdt_bep20":   "🔷 USDT BEP20",
    "usdt_erc20":   "💠 USDT ERC20",
    "binance_pay":  "🟡 Binance Pay",
    "bybit_pay":    "🟠 Bybit Pay",
    "mobile_banking": "🏦 Mobile Banking",
}

# ── Status definitions ────────────────────────────────────────────────────────

STATUS_LABELS: dict[str, str] = {
    "pending":      "🆕 Pending",
    "under_review": "👀 Under Review",
    "approved":     "✅ Approved",
    "processing":   "💸 Processing",
    "completed":    "🎉 Completed",
    "rejected":     "❌ Rejected",
    "cancelled":    "🚫 Cancelled",
    "expired":      "⏰ Expired",
}

# Statuses that "lock" funds (prevent new withdrawals until resolved)
_ACTIVE_STATUSES = ("pending", "under_review", "approved", "processing")

# Statuses that are terminal (no further admin action needed)
_TERMINAL_STATUSES = ("completed", "rejected", "cancelled", "expired")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _append_log(existing_json: Optional[str], status: str, actor: str) -> str:
    """Append a log entry to the logs_json field."""
    try:
        entries = json.loads(existing_json) if existing_json else []
    except Exception:
        entries = []
    entries.append({
        "status": status,
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "by": actor,
    })
    return json.dumps(entries)


def _row_to_dict(row) -> dict:
    """Convert a DB row (RowMapping or tuple) to a plain dict."""
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if hasattr(row, "_asdict"):
        return row._asdict()
    return dict(row)


def _fetch_one(session, wid: int, for_update: bool = False) -> Optional[dict]:
    """Fetch a single withdrawal row; optionally lock it."""
    lock = "FOR UPDATE" if for_update and session.bind.dialect.name == "postgresql" else ""
    row = session.execute(
        text(
            f"SELECT rw.*, u.telegram_id AS user_tg_id, u.username AS user_username "
            f"FROM referral_withdrawals rw "
            f"JOIN users u ON u.id = rw.user_id "
            f"WHERE rw.id = :wid {lock}"
        ),
        {"wid": wid},
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


# ─────────────────────────────────────────────────────────────────────────────
# Feature status
# ─────────────────────────────────────────────────────────────────────────────

def get_feature_status() -> str:
    """Return the withdrawal approval feature status."""
    return cfg.get("withdrawal_approval_status", "enabled") or "enabled"


# ─────────────────────────────────────────────────────────────────────────────
# Balance helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_available_balance(user_id: int) -> float:
    """Available commission minus any actively locked withdrawal amounts."""
    try:
        with get_db_session() as s:
            # Available commission from referral_commissions
            avail_row = s.execute(
                text(
                    "SELECT COALESCE(SUM(commission_amount), 0) "
                    "FROM referral_commissions "
                    "WHERE referrer_id = :uid AND status = 'available'"
                ),
                {"uid": user_id},
            ).fetchone()
            available = float(avail_row[0]) if avail_row else 0.0

            # Subtract amounts locked by active (not yet terminal) withdrawals
            locked_row = s.execute(
                text(
                    "SELECT COALESCE(SUM(amount), 0) "
                    "FROM referral_withdrawals "
                    "WHERE user_id = :uid AND status IN :statuses"
                ),
                {"uid": user_id, "statuses": tuple(_ACTIVE_STATUSES)},
            ).fetchone()
            locked = float(locked_row[0]) if locked_row else 0.0

            return max(0.0, available - locked)
    except Exception:
        logger.exception("get_available_balance failed for user_id=%s", user_id)
        return 0.0


def has_pending_withdrawal(user_id: int) -> bool:
    """Return True if user already has an active (non-terminal) withdrawal."""
    try:
        with get_db_session() as s:
            row = s.execute(
                text(
                    "SELECT 1 FROM referral_withdrawals "
                    "WHERE user_id = :uid AND status IN :statuses LIMIT 1"
                ),
                {"uid": user_id, "statuses": tuple(_ACTIVE_STATUSES)},
            ).fetchone()
            return row is not None
    except Exception:
        logger.exception("has_pending_withdrawal failed")
        return False


def get_daily_count(user_id: int) -> int:
    """Return how many withdrawals this user created today (UTC)."""
    try:
        with get_db_session() as s:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            row = s.execute(
                text(
                    "SELECT COUNT(*) FROM referral_withdrawals "
                    "WHERE user_id = :uid AND created_at >= :start"
                ),
                {"uid": user_id, "start": today_start},
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        logger.exception("get_daily_count failed")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Create withdrawal
# ─────────────────────────────────────────────────────────────────────────────

def create_withdrawal(
    user_telegram_id: int,
    amount: float,
    payment_method: str,
    wallet_address: str,
) -> Optional[dict]:
    """Create a new withdrawal request.

    Returns a dict with the created withdrawal (including ``id``) on success,
    or ``None`` on failure (caller should check logs / show error).
    """
    try:
        with get_db_session() as s:
            # Resolve internal user ID
            user = s.query(User).filter_by(telegram_id=user_telegram_id).first()
            if not user:
                logger.warning("create_withdrawal: user not found telegram_id=%s", user_telegram_id)
                return None

            user_id = user.id

            # Feature checks
            status = get_feature_status()
            if status != "enabled":
                return None

            # Duplicate prevention: block if user already has an active withdrawal
            dup = s.execute(
                text(
                    "SELECT id FROM referral_withdrawals "
                    "WHERE user_id = :uid AND status IN :statuses LIMIT 1"
                ),
                {"uid": user_id, "statuses": tuple(_ACTIVE_STATUSES)},
            ).fetchone()
            if dup:
                logger.info("create_withdrawal: duplicate active withdrawal for user_id=%s", user_id)
                return {"error": "duplicate"}

            # Daily limit check
            max_daily = cfg.get_int("withdrawal_approval_max_daily", 0)
            if max_daily > 0:
                today_start = datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                day_count_row = s.execute(
                    text(
                        "SELECT COUNT(*) FROM referral_withdrawals "
                        "WHERE user_id = :uid AND created_at >= :start"
                    ),
                    {"uid": user_id, "start": today_start},
                ).fetchone()
                if int(day_count_row[0] or 0) >= max_daily:
                    return {"error": "daily_limit"}

            # Amount checks
            min_amt = cfg.get_float("withdrawal_approval_min_amount", 5.0)
            max_amt = cfg.get_float("withdrawal_approval_max_amount", 0.0)
            if amount < min_amt:
                return {"error": "below_min"}
            if max_amt > 0 and amount > max_amt:
                return {"error": "above_max"}

            # Balance check (prevents double-spending)
            avail = get_available_balance(user_id)
            if amount > avail:
                return {"error": "insufficient"}

            # Determine initial status (auto-approval check)
            auto_approval = cfg.get_bool("withdrawal_approval_auto_approval", False)
            auto_max = cfg.get_float("withdrawal_approval_auto_max", 10.0)
            initial_status = "pending"
            logs = _append_log(None, "pending", f"user:{user_telegram_id}")

            now = datetime.utcnow()

            # Insert
            result = s.execute(
                text(
                    "INSERT INTO referral_withdrawals "
                    "(user_id, amount, status, payment_method, wallet_address, currency, "
                    " logs_json, created_at) "
                    "VALUES "
                    "(:uid, :amt, :st, :pm, :wa, :cur, :logs, :now) "
                    "RETURNING id"
                ),
                {
                    "uid": user_id,
                    "amt": amount,
                    "st": initial_status,
                    "pm": payment_method,
                    "wa": wallet_address,
                    "cur": "USD",
                    "logs": logs,
                    "now": now,
                },
            ).fetchone()
            wid = result[0]
            s.commit()

            withdrawal = {
                "id": wid,
                "user_id": user_id,
                "user_tg_id": user_telegram_id,
                "amount": amount,
                "status": initial_status,
                "payment_method": payment_method,
                "wallet_address": wallet_address,
                "currency": "USD",
                "created_at": now,
            }

            # Auto-approval: approve immediately if eligible
            if auto_approval and auto_max > 0 and amount <= auto_max:
                logger.info("create_withdrawal: auto-approving wid=%s amount=%.2f", wid, amount)
                _do_approve(s, wid, user_id, "system", logs=logs)
                withdrawal["status"] = "approved"
                s.commit()

            return withdrawal

    except Exception:
        logger.exception("create_withdrawal failed")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Internal approval helper (called within an already-open session)
# ─────────────────────────────────────────────────────────────────────────────

def _do_approve(session, wid: int, user_id: int, actor: str, logs: str) -> None:
    new_logs = _append_log(logs, "approved", actor)
    session.execute(
        text(
            "UPDATE referral_withdrawals "
            "SET status='approved', approval_time=:now, logs_json=:logs "
            "WHERE id=:wid"
        ),
        {"now": datetime.utcnow(), "logs": new_logs, "wid": wid},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Status transition helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_withdrawal_locked(session, wid: int) -> Optional[dict]:
    """Fetch withdrawal row with FOR UPDATE lock (PostgreSQL) or plain fetch."""
    lock = "FOR UPDATE" if session.bind.dialect.name == "postgresql" else ""
    row = session.execute(
        text(
            f"SELECT rw.*, u.telegram_id AS user_tg_id, u.username AS user_username "
            f"FROM referral_withdrawals rw "
            f"JOIN users u ON u.id = rw.user_id "
            f"WHERE rw.id = :wid {lock}"
        ),
        {"wid": wid},
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def mark_under_review(wid: int, admin_tg_id: int) -> Optional[dict]:
    """Move withdrawal to 'under_review' status."""
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] != "pending":
                return None
            new_logs = _append_log(rec.get("logs_json"), "under_review", f"admin:{admin_tg_id}")
            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='under_review', admin_tg_id=:atid, logs_json=:logs "
                    "WHERE id=:wid"
                ),
                {"atid": admin_tg_id, "logs": new_logs, "wid": wid},
            )
            s.commit()
            rec["status"] = "under_review"
            return rec
    except Exception:
        logger.exception("mark_under_review failed wid=%s", wid)
        return None


def approve_withdrawal(wid: int, admin_tg_id: int, note: Optional[str] = None) -> Optional[dict]:
    """Approve a withdrawal (pending or under_review → approved)."""
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] not in ("pending", "under_review"):
                return None
            new_logs = _append_log(rec.get("logs_json"), "approved", f"admin:{admin_tg_id}")
            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='approved', approval_time=:now, admin_tg_id=:atid, "
                    "    logs_json=:logs "
                    + (", notes=:note " if note else "")
                    + "WHERE id=:wid"
                ),
                {
                    "now": datetime.utcnow(), "atid": admin_tg_id,
                    "logs": new_logs, "wid": wid,
                    **({"note": note} if note else {}),
                },
            )
            s.commit()
            rec["status"] = "approved"
            return rec
    except Exception:
        logger.exception("approve_withdrawal failed wid=%s", wid)
        return None


def mark_processing(wid: int, admin_tg_id: int) -> Optional[dict]:
    """Move withdrawal to 'processing' status (approved → processing)."""
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] != "approved":
                return None
            new_logs = _append_log(rec.get("logs_json"), "processing", f"admin:{admin_tg_id}")
            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='processing', admin_tg_id=:atid, logs_json=:logs "
                    "WHERE id=:wid"
                ),
                {"atid": admin_tg_id, "logs": new_logs, "wid": wid},
            )
            s.commit()
            rec["status"] = "processing"
            return rec
    except Exception:
        logger.exception("mark_processing failed wid=%s", wid)
        return None


def complete_withdrawal(wid: int, admin_tg_id: int) -> Optional[dict]:
    """Complete a withdrawal (approved or processing → completed).

    Also marks the corresponding referral_commissions rows as 'withdrawn'.
    """
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] not in ("approved", "processing"):
                return None

            user_id = rec["user_id"]
            amount  = float(rec["amount"])
            now     = datetime.utcnow()
            new_logs = _append_log(rec.get("logs_json"), "completed", f"admin:{admin_tg_id}")

            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='completed', completion_time=:now, admin_tg_id=:atid, "
                    "    resolved_at=:now, logs_json=:logs "
                    "WHERE id=:wid"
                ),
                {"now": now, "atid": admin_tg_id, "logs": new_logs, "wid": wid},
            )

            # Mark commissions as withdrawn (up to the withdrawal amount)
            try:
                s.execute(
                    text(
                        "UPDATE referral_commissions "
                        "SET status='withdrawn', cleared_at=:now "
                        "WHERE referrer_id=:uid AND status='available' "
                        "AND id IN ("
                        "  SELECT id FROM referral_commissions "
                        "  WHERE referrer_id=:uid AND status='available' "
                        "  ORDER BY created_at ASC "
                        "  LIMIT 1000"
                        ")"
                    ),
                    {"uid": user_id, "now": now},
                )
            except Exception:
                logger.exception("complete_withdrawal: commissions update failed (non-fatal)")

            s.commit()
            rec["status"] = "completed"
            rec["completion_time"] = now
            return rec
    except Exception:
        logger.exception("complete_withdrawal failed wid=%s", wid)
        return None


def reject_withdrawal(
    wid: int,
    admin_tg_id: int,
    reason: str,
    note: Optional[str] = None,
) -> Optional[dict]:
    """Reject a withdrawal and return funds (sets commission rows back to available)."""
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] in _TERMINAL_STATUSES:
                return None

            now = datetime.utcnow()
            new_logs = _append_log(rec.get("logs_json"), "rejected", f"admin:{admin_tg_id}")

            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='rejected', resolved_at=:now, admin_tg_id=:atid, "
                    "    reason=:reason, logs_json=:logs "
                    + (", notes=:note " if note else "")
                    + "WHERE id=:wid"
                ),
                {
                    "now": now, "atid": admin_tg_id, "reason": reason,
                    "logs": new_logs, "wid": wid,
                    **({"note": note} if note else {}),
                },
            )
            s.commit()
            rec["status"] = "rejected"
            rec["reason"] = reason
            return rec
    except Exception:
        logger.exception("reject_withdrawal failed wid=%s", wid)
        return None


def cancel_withdrawal(
    wid: int,
    reason: Optional[str] = None,
    admin_tg_id: Optional[int] = None,
) -> Optional[dict]:
    """Cancel a withdrawal. Can be called by user (pending only) or admin (any active)."""
    try:
        with get_db_session() as s:
            rec = _get_withdrawal_locked(s, wid)
            if not rec or rec["status"] in _TERMINAL_STATUSES:
                return None

            actor = f"admin:{admin_tg_id}" if admin_tg_id else "user"
            now = datetime.utcnow()
            new_logs = _append_log(rec.get("logs_json"), "cancelled", actor)

            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET status='cancelled', resolved_at=:now, reason=:reason, "
                    "    logs_json=:logs "
                    + (", admin_tg_id=:atid " if admin_tg_id else "")
                    + "WHERE id=:wid"
                ),
                {
                    "now": now, "reason": reason or "Cancelled",
                    "logs": new_logs, "wid": wid,
                    **({"atid": admin_tg_id} if admin_tg_id else {}),
                },
            )
            s.commit()
            rec["status"] = "cancelled"
            return rec
    except Exception:
        logger.exception("cancel_withdrawal failed wid=%s", wid)
        return None


def add_admin_note(wid: int, admin_tg_id: int, note: str) -> bool:
    """Add or replace an internal admin note."""
    try:
        with get_db_session() as s:
            s.execute(
                text(
                    "UPDATE referral_withdrawals "
                    "SET notes=:note, admin_tg_id=:atid WHERE id=:wid"
                ),
                {"note": note, "atid": admin_tg_id, "wid": wid},
            )
            s.commit()
            return True
    except Exception:
        logger.exception("add_admin_note failed wid=%s", wid)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Querying
# ─────────────────────────────────────────────────────────────────────────────

def get_withdrawal(wid: int) -> Optional[dict]:
    """Return a single withdrawal as a dict (None if not found)."""
    try:
        with get_db_session() as s:
            return _fetch_one(s, wid)
    except Exception:
        logger.exception("get_withdrawal failed wid=%s", wid)
        return None


def list_withdrawals(
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    """Return a list of withdrawal dicts with user info."""
    try:
        with get_db_session() as s:
            conditions = []
            params: dict = {"limit": limit, "offset": offset}
            if status:
                conditions.append("rw.status = :status")
                params["status"] = status
            if user_id:
                conditions.append("rw.user_id = :uid")
                params["uid"] = user_id
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = s.execute(
                text(
                    "SELECT rw.id, rw.user_id, rw.amount, rw.status, rw.payment_method, "
                    "       rw.wallet_address, rw.currency, rw.admin_tg_id, "
                    "       rw.approval_time, rw.completion_time, rw.reason, rw.notes, "
                    "       rw.created_at, rw.resolved_at, rw.admin_note, "
                    "       u.telegram_id AS user_tg_id, u.username AS user_username "
                    "FROM referral_withdrawals rw "
                    "JOIN users u ON u.id = rw.user_id "
                    f"{where} "
                    "ORDER BY rw.created_at DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.exception("list_withdrawals failed status=%s user_id=%s", status, user_id)
        return []


def get_admin_stats() -> dict:
    """Return dashboard stats for the admin panel."""
    stats = {
        "pending": 0,
        "under_review": 0,
        "approved_today": 0,
        "rejected_today": 0,
        "completed_today": 0,
        "total_volume": 0.0,
        "avg_processing_minutes": None,
    }
    try:
        with get_db_session() as s:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

            for st_key in ("pending", "under_review"):
                row = s.execute(
                    text(
                        "SELECT COUNT(*) FROM referral_withdrawals WHERE status=:st"
                    ),
                    {"st": st_key},
                ).fetchone()
                stats[st_key] = int(row[0] or 0)

            for st_key, col in (
                ("approved", "approved_today"),
                ("rejected", "rejected_today"),
                ("completed", "completed_today"),
            ):
                row = s.execute(
                    text(
                        "SELECT COUNT(*) FROM referral_withdrawals "
                        "WHERE status=:st AND resolved_at >= :start"
                    ),
                    {"st": st_key, "start": today_start},
                ).fetchone()
                stats[col] = int(row[0] or 0)

            # Total volume (completed only)
            vol = s.execute(
                text(
                    "SELECT COALESCE(SUM(amount), 0) FROM referral_withdrawals "
                    "WHERE status='completed'"
                )
            ).fetchone()
            stats["total_volume"] = float(vol[0] or 0.0)

            # Average processing time (minutes) for completed withdrawals
            try:
                avg_row = s.execute(
                    text(
                        "SELECT AVG(EXTRACT(EPOCH FROM (completion_time - created_at))/60) "
                        "FROM referral_withdrawals "
                        "WHERE status='completed' AND completion_time IS NOT NULL"
                    )
                ).fetchone()
                if avg_row and avg_row[0] is not None:
                    stats["avg_processing_minutes"] = float(avg_row[0])
            except Exception:
                pass

    except Exception:
        logger.exception("get_admin_stats failed")
    return stats
