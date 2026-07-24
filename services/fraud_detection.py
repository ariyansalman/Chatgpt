"""Smart Fraud Detection System — V31.

Public API
----------
run_checks(user_id)           → FraudResult   (full scan, persists log + risk)
quick_check(user_id, event)   → FraudResult   (single-event lightweight check)
get_risk(user_id)             → dict           (cached risk state from DB)
is_frozen(user_id)            → bool
is_suspended(user_id)         → bool
is_whitelisted(user_id)       → bool
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─── Check catalogue ──────────────────────────────────────────────────────────
# (code, label, base_score_delta)
CHECK_DEFS: list[tuple[str, str, int]] = [
    ("duplicate_txid",         "Duplicate Transaction ID",             50),
    ("duplicate_wallet",       "Duplicate Wallet Across Accounts",     40),
    ("duplicate_deposit",      "Duplicate Deposit Detected",           30),
    ("duplicate_withdrawal",   "Duplicate Withdrawal Detected",        30),
    ("multiple_accounts",      "Multiple Telegram Accounts",           45),
    ("wallet_abuse",           "Same Wallet Multiple Accounts",        40),
    ("payment_method_abuse",   "Same Payment Method Multiple Accounts",35),
    ("suspicious_login",       "Suspicious Login / Account Pattern",   20),
    ("excessive_failed",       "Excessive Failed Payments",            25),
    ("rapid_deposits",         "Rapid Deposits",                       30),
    ("rapid_withdrawals",      "Rapid Withdrawals",                    35),
    ("unusual_purchase",       "Unusual Purchase Activity",            20),
    ("referral_abuse",         "Suspicious Referral Activity",         40),
    ("coupon_abuse",           "Multiple Coupon Abuse",                35),
    ("account_farm",           "Excessive Account Creation / Farm",    30),
]

CHECK_SCORES: dict[str, int] = {c: s for c, _, s in CHECK_DEFS}

RISK_LEVELS = ("low", "medium", "high", "critical")


def _risk_level(score: int) -> str:
    t_med  = int(cfg.get("fds_risk_threshold_medium",  "30") or 30)
    t_high = int(cfg.get("fds_risk_threshold_high",    "60") or 60)
    t_crit = int(cfg.get("fds_risk_threshold_critical","90") or 90)
    if score >= t_crit:
        return "critical"
    if score >= t_high:
        return "high"
    if score >= t_med:
        return "medium"
    return "low"


def _level_emoji(level: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(level, "⚪")


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class FraudResult:
    user_id: int
    risk_score: int = 0
    risk_level: str = "low"
    flags: list[str] = field(default_factory=list)          # triggered check codes
    details: dict[str, Any] = field(default_factory=dict)   # check_code → detail str
    actions_taken: list[str] = field(default_factory=list)
    is_frozen: bool = False
    is_suspended: bool = False
    is_whitelisted: bool = False

    @property
    def is_clean(self) -> bool:
        return self.risk_level == "low" and not self.flags

    @property
    def emoji(self) -> str:
        return _level_emoji(self.risk_level)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _get_session():
    from database import get_db_session
    return get_db_session()


def _table_ok(s, table: str) -> bool:
    try:
        s.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False


def _upsert_risk(s, user_id: int, score: int, level: str,
                 flags: list[str], frozen: bool, suspended: bool) -> None:
    s.execute(text("""
        INSERT INTO fraud_user_risk
            (user_id, risk_score, risk_level, flags_json,
             is_frozen, is_suspended, last_checked_at, updated_at)
        VALUES
            (:uid, :score, :level, :flags,
             :frozen, :suspended, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            risk_score      = GREATEST(fraud_user_risk.risk_score,  EXCLUDED.risk_score),
            risk_level      = EXCLUDED.risk_level,
            flags_json      = EXCLUDED.flags_json,
            is_frozen       = EXCLUDED.is_frozen OR fraud_user_risk.is_frozen,
            is_suspended    = EXCLUDED.is_suspended OR fraud_user_risk.is_suspended,
            last_checked_at = NOW(),
            updated_at      = NOW()
    """), {
        "uid": user_id, "score": score, "level": level,
        "flags": json.dumps(flags),
        "frozen": frozen, "suspended": suspended,
    })


def _insert_log(s, user_id: int, check_type: str, delta: int,
                level: str, details: str, action: str) -> None:
    s.execute(text("""
        INSERT INTO fraud_logs
            (user_id, check_type, risk_score_delta, risk_level, details, action_taken)
        VALUES (:uid, :ct, :delta, :level, :det, :act)
    """), {
        "uid": user_id, "ct": check_type, "delta": delta,
        "level": level, "det": details, "act": action,
    })


# ─── Individual checks ────────────────────────────────────────────────────────

def _check_duplicate_txid(s, user_id: int) -> tuple[int, str]:
    """Score > 0 if this user submitted a TXID already used by another user."""
    try:
        row = s.execute(text("""
            SELECT COUNT(DISTINCT t.user_id)
            FROM   transactions t
            WHERE  t.txid IS NOT NULL
              AND  t.txid IN (
                       SELECT txid FROM transactions
                       WHERE  user_id = :uid AND txid IS NOT NULL
                   )
              AND  t.user_id != :uid
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count:
            return CHECK_SCORES["duplicate_txid"], f"{count} other account(s) used same TXID(s)"
    except Exception:
        pass
    return 0, ""


def _check_duplicate_wallet(s, user_id: int) -> tuple[int, str]:
    """Score > 0 if this user's crypto addresses appear on other accounts."""
    try:
        row = s.execute(text("""
            SELECT COUNT(DISTINCT t2.user_id)
            FROM   transactions t1
            JOIN   transactions t2
                   ON  t2.crypto_address = t1.crypto_address
                   AND t2.user_id        != t1.user_id
                   AND t1.crypto_address IS NOT NULL
                   AND t1.crypto_address != ''
            WHERE  t1.user_id = :uid
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count:
            return CHECK_SCORES["duplicate_wallet"], f"Wallet shared with {count} other account(s)"
    except Exception:
        pass
    return 0, ""


def _check_blacklisted_wallet(s, user_id: int) -> tuple[int, str]:
    """Extra score if user's wallet is on the admin blacklist."""
    try:
        if not _table_ok(s, "fraud_wallet_blacklist"):
            return 0, ""
        row = s.execute(text("""
            SELECT fwb.reason
            FROM   transactions t
            JOIN   fraud_wallet_blacklist fwb
                   ON fwb.wallet_address = t.crypto_address
            WHERE  t.user_id = :uid
            LIMIT  1
        """), {"uid": user_id}).fetchone()
        if row:
            return CHECK_SCORES["duplicate_wallet"], f"Wallet on admin blacklist: {row[0] or 'N/A'}"
    except Exception:
        pass
    return 0, ""


def _check_duplicate_deposit(s, user_id: int) -> tuple[int, str]:
    """Same amount deposited twice in 10 minutes (same payment method)."""
    try:
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM (
                SELECT amount, payment_method,
                       COUNT(*) FILTER (
                           WHERE created_at >= NOW() - INTERVAL '10 minutes'
                       ) AS recent_count
                FROM   transactions
                WHERE  user_id = :uid
                  AND  status NOT IN ('cancelled','failed')
                GROUP  BY amount, payment_method
                HAVING COUNT(*) FILTER (
                           WHERE created_at >= NOW() - INTERVAL '10 minutes'
                       ) >= 2
            ) sub
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count:
            return CHECK_SCORES["duplicate_deposit"], f"{count} duplicate deposit(s) in last 10 min"
    except Exception:
        pass
    return 0, ""


def _check_duplicate_withdrawal(s, user_id: int) -> tuple[int, str]:
    """Same withdrawal amount submitted twice within 1 hour."""
    try:
        if not _table_ok(s, "referral_withdrawals"):
            return 0, ""
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM (
                SELECT amount, COUNT(*) AS cnt
                FROM   referral_withdrawals
                WHERE  user_id = (
                           SELECT id FROM users WHERE telegram_id = :uid
                           UNION
                           SELECT :uid
                           LIMIT 1
                       )
                  AND  created_at >= NOW() - INTERVAL '1 hour'
                GROUP  BY amount
                HAVING COUNT(*) >= 2
            ) sub
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count:
            return CHECK_SCORES["duplicate_withdrawal"], f"{count} duplicate withdrawal(s) in 1h"
    except Exception:
        pass
    return 0, ""


def _check_excessive_failed(s, user_id: int) -> tuple[int, str]:
    max_fails = int(cfg.get("fds_max_failed_payments", "5") or 5)
    try:
        row = s.execute(text("""
            SELECT COUNT(*) FROM transactions
            WHERE  user_id = :uid
              AND  status   = 'failed'
              AND  created_at >= NOW() - INTERVAL '24 hours'
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count >= max_fails:
            return CHECK_SCORES["excessive_failed"], f"{count} failed payments in 24h (limit {max_fails})"
    except Exception:
        pass
    return 0, ""


def _check_rapid_deposits(s, user_id: int) -> tuple[int, str]:
    max_dep = int(cfg.get("fds_max_daily_deposits", "10") or 10)
    try:
        row = s.execute(text("""
            SELECT COUNT(*) FROM transactions
            WHERE  user_id = :uid
              AND  created_at >= NOW() - INTERVAL '24 hours'
              AND  status NOT IN ('cancelled')
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count > max_dep:
            return CHECK_SCORES["rapid_deposits"], f"{count} deposits in 24h (limit {max_dep})"
    except Exception:
        pass
    return 0, ""


def _check_rapid_withdrawals(s, user_id: int) -> tuple[int, str]:
    max_w = int(cfg.get("fds_max_daily_withdrawals", "3") or 3)
    try:
        if not _table_ok(s, "referral_withdrawals"):
            return 0, ""
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM   referral_withdrawals rw
            JOIN   users u ON u.id = rw.user_id
            WHERE  u.telegram_id = :uid
              AND  rw.created_at >= NOW() - INTERVAL '24 hours'
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count > max_w:
            return CHECK_SCORES["rapid_withdrawals"], f"{count} withdrawals in 24h (limit {max_w})"
    except Exception:
        pass
    return 0, ""


def _check_unusual_purchase(s, user_id: int) -> tuple[int, str]:
    max_orders = int(cfg.get("fds_max_daily_orders", "20") or 20)
    try:
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM   orders o
            JOIN   users  u ON u.id = o.user_id
            WHERE  u.telegram_id = :uid
              AND  o.created_at >= NOW() - INTERVAL '24 hours'
        """), {"uid": user_id}).fetchone()
        count = int(row[0]) if row else 0
        if count > max_orders:
            return CHECK_SCORES["unusual_purchase"], f"{count} orders in 24h (limit {max_orders})"
    except Exception:
        pass
    return 0, ""


def _check_referral_abuse(s, user_id: int) -> tuple[int, str]:
    """Detect self-referral: user referred themselves, or referred-by chain loops."""
    try:
        # Self-referral: user's own telegram_id referred accounts that then referred back
        # Simpler: user has referral_earnings AND is themselves referred by someone they also referred
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM   users u1
            JOIN   users u2 ON u2.referred_by_id = u1.id
            WHERE  u1.telegram_id = :uid
              AND  u1.referred_by_id = u2.id
        """), {"uid": user_id}).fetchone()
        if int(row[0] if row else 0) > 0:
            return CHECK_SCORES["referral_abuse"], "Circular referral chain detected"

        # High referral earnings with very new referred accounts (farm indicator)
        row2 = s.execute(text("""
            SELECT COUNT(u2.id)
            FROM   users u1
            JOIN   users u2 ON u2.referred_by_id = u1.id
            WHERE  u1.telegram_id = :uid
              AND  u2.created_at  >= NOW() - INTERVAL '7 days'
              AND  u2.has_purchased = FALSE
        """), {"uid": user_id}).fetchone()
        new_inactive = int(row2[0] if row2 else 0)
        if new_inactive >= 10:
            return CHECK_SCORES["referral_abuse"], f"{new_inactive} new inactive referred accounts in 7d"
    except Exception:
        pass
    return 0, ""


def _check_coupon_abuse(s, user_id: int) -> tuple[int, str]:
    """Detect rapid / bulk coupon redemptions."""
    try:
        row = s.execute(text("""
            SELECT COUNT(*)
            FROM   coupon_redemptions cr
            JOIN   users u ON u.id = cr.user_id
            WHERE  u.telegram_id = :uid
              AND  cr.created_at >= NOW() - INTERVAL '24 hours'
        """), {"uid": user_id}).fetchone()
        count = int(row[0] if row else 0)
        if count >= 5:
            return CHECK_SCORES["coupon_abuse"], f"{count} coupon redemptions in 24h"
    except Exception:
        pass
    return 0, ""


def _check_account_farm(s, user_id: int) -> tuple[int, str]:
    """Many accounts created rapidly and referred by this user (bot farm)."""
    try:
        row = s.execute(text("""
            SELECT COUNT(u2.id)
            FROM   users u1
            JOIN   users u2 ON u2.referred_by_id = u1.id
            WHERE  u1.telegram_id = :uid
              AND  u2.created_at  >= NOW() - INTERVAL '48 hours'
        """), {"uid": user_id}).fetchone()
        count = int(row[0] if row else 0)
        if count >= 20:
            return CHECK_SCORES["account_farm"], f"{count} referred accounts created in 48h"
    except Exception:
        pass
    return 0, ""


def _check_multiple_accounts(s, user_id: int) -> tuple[int, str]:
    """Same user might have multiple accounts: checks similar username patterns."""
    try:
        # Look for users created on the same day with similar username prefix
        row = s.execute(text("""
            SELECT COUNT(u2.id)
            FROM   users u1, users u2
            WHERE  u1.telegram_id = :uid
              AND  u2.id != u1.id
              AND  u2.username IS NOT NULL
              AND  u1.username IS NOT NULL
              AND  LEFT(LOWER(u2.username), 6) = LEFT(LOWER(u1.username), 6)
              AND  ABS(EXTRACT(EPOCH FROM (u2.created_at - u1.created_at))) < 3600
        """), {"uid": user_id}).fetchone()
        count = int(row[0] if row else 0)
        if count >= 3:
            return CHECK_SCORES["multiple_accounts"], f"{count} similar accounts created near same time"
    except Exception:
        pass
    return 0, ""


def _check_wallet_abuse(s, user_id: int) -> tuple[int, str]:
    """Same wallet address used across multiple Telegram accounts."""
    try:
        row = s.execute(text("""
            SELECT COUNT(DISTINCT t2.user_id)
            FROM   referral_withdrawals rw1
            JOIN   users u1 ON u1.id = rw1.user_id
            JOIN   referral_withdrawals rw2
                   ON  rw2.wallet_address = rw1.wallet_address
                   AND rw2.user_id        != rw1.user_id
                   AND rw1.wallet_address IS NOT NULL
            WHERE  u1.telegram_id = :uid
        """), {"uid": user_id}).fetchone()
        if not _table_ok(s, "referral_withdrawals"):
            return 0, ""
        count = int(row[0] if row else 0)
        if count:
            return CHECK_SCORES["wallet_abuse"], f"Withdrawal wallet shared with {count} other account(s)"
    except Exception:
        pass
    return 0, ""


def _check_payment_method_abuse(s, user_id: int) -> tuple[int, str]:
    """Same manual payment method account details used across many users."""
    try:
        row = s.execute(text("""
            SELECT COUNT(DISTINCT t2.user_id)
            FROM   transactions t1
            JOIN   transactions t2
                   ON  t2.manual_method_id = t1.manual_method_id
                   AND t2.user_id          != t1.user_id
            WHERE  t1.user_id          = :uid
              AND  t1.manual_method_id IS NOT NULL
        """), {"uid": user_id}).fetchone()
        count = int(row[0] if row else 0)
        if count >= 5:
            return CHECK_SCORES["payment_method_abuse"], f"Manual payment method shared with {count} accounts"
    except Exception:
        pass
    return 0, ""


# ─── Full scan ────────────────────────────────────────────────────────────────

# (check_code, config_gate_key, check_function)
_CHECKS: list[tuple[str, str | None, Any]] = [
    ("duplicate_txid",       "fds_check_dup_txid",       _check_duplicate_txid),
    ("duplicate_wallet",     "fds_check_dup_wallet",     _check_duplicate_wallet),
    ("duplicate_wallet",     "fds_check_dup_wallet",     _check_blacklisted_wallet),
    ("duplicate_deposit",    "fds_check_dup_deposit",    _check_duplicate_deposit),
    ("duplicate_withdrawal", "fds_check_dup_withdrawal", _check_duplicate_withdrawal),
    ("excessive_failed",     None,                        _check_excessive_failed),
    ("rapid_deposits",       None,                        _check_rapid_deposits),
    ("rapid_withdrawals",    None,                        _check_rapid_withdrawals),
    ("unusual_purchase",     None,                        _check_unusual_purchase),
    ("referral_abuse",       "fds_check_referral_abuse", _check_referral_abuse),
    ("coupon_abuse",         "fds_check_coupon_abuse",   _check_coupon_abuse),
    ("account_farm",         "fds_check_referral_abuse", _check_account_farm),
    ("multiple_accounts",    None,                        _check_multiple_accounts),
    ("wallet_abuse",         "fds_check_dup_wallet",     _check_wallet_abuse),
    ("payment_method_abuse", None,                        _check_payment_method_abuse),
]


def _apply_actions(result: FraudResult) -> list[str]:
    """Determine and apply automatic actions based on risk level."""
    actions: list[str] = []
    level = result.risk_level

    auto_freeze   = cfg.get_bool("fds_auto_freeze",   True)
    auto_suspend  = cfg.get_bool("fds_auto_suspend",  False)

    if level in ("high", "critical"):
        actions.append("blocked_withdrawal")
        actions.append("blocked_coupon")
        actions.append("blocked_referral_reward")
        actions.append("flagged_for_review")

    if level == "critical" and auto_freeze:
        result.is_frozen = True
        actions.append("wallet_frozen")

    if level == "critical" and auto_suspend:
        result.is_suspended = True
        actions.append("account_suspended")

    if level == "medium":
        actions.append("flagged_for_review")

    return actions


async def _notify_admin(result: FraudResult) -> None:
    if not cfg.get_bool("fds_admin_alerts", True):
        return
    if result.risk_level not in ("high", "critical"):
        return
    try:
        from services.notifications import notify_admins
        from telegram.ext import Application
        from database import get_db_session
        from database.models import User as UserModel

        with get_db_session() as s:
            user_row = s.execute(
                text("SELECT telegram_id, username FROM users WHERE telegram_id = :uid"),
                {"uid": result.user_id}
            ).fetchone()
        uname = f"@{user_row[1]}" if (user_row and user_row[1]) else f"ID:{result.user_id}"

        from utils.notify_format import render as _render, utc_now_str as _ts
        text_msg = _render("🚨", "Fraud Alert", [
            ("User", uname),
            ("Risk", f"{result.emoji} {result.risk_level.upper()} (score: {result.risk_score})"),
            ("Flags", ", ".join(result.flags) or "none"),
            ("Actions", ", ".join(result.actions_taken) or "none"),
        ], _ts())
        # notify_admins needs a Bot instance — use Application singleton if available
        try:
            from telegram.ext._application import Application as _App  # noqa: F401
            import telegram.ext._application as _app_mod
            _bot = getattr(_app_mod, "_application_instance", None)
            if _bot:
                await notify_admins(_bot.bot, event="fraud_alert", text=text_msg)
        except Exception:
            pass
    except Exception:
        logger.debug("fraud notify_admin failed (non-fatal)", exc_info=True)


def run_checks(user_id: int) -> FraudResult:  # noqa: C901
    """Run all enabled fraud checks for a user and persist results."""
    result = FraudResult(user_id=user_id)

    if cfg.get("fds_status", "enabled") != "enabled":
        return result

    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return result

            # Check if whitelisted — skip all checks
            wl_row = s.execute(
                text("SELECT is_whitelisted, is_blacklisted, is_frozen, is_suspended "
                     "FROM fraud_user_risk WHERE user_id = "
                     "(SELECT id FROM users WHERE telegram_id = :uid LIMIT 1)"),
                {"uid": user_id}
            ).fetchone()
            if wl_row:
                result.is_whitelisted = bool(wl_row[0])
                result.is_frozen      = bool(wl_row[2])
                result.is_suspended   = bool(wl_row[3])
                if result.is_whitelisted:
                    return result  # skip checks for whitelisted users

            total_score = 0
            triggered_flags: list[str] = []
            details_map: dict[str, str] = {}

            for check_code, gate_key, check_fn in _CHECKS:
                if gate_key and not cfg.get_bool(gate_key, True):
                    continue
                try:
                    delta, detail = check_fn(s, user_id)
                    if delta > 0:
                        total_score += delta
                        if check_code not in triggered_flags:
                            triggered_flags.append(check_code)
                        if detail and check_code not in details_map:
                            details_map[check_code] = detail
                        _insert_log(s, user_id if not _is_tg_id(s, user_id) else _tg_to_db_id(s, user_id),
                                    check_code, delta,
                                    _risk_level(total_score), detail, "")
                except Exception:
                    logger.debug("check %s failed for uid %s", check_code, user_id, exc_info=True)

            result.risk_score = total_score
            result.risk_level = _risk_level(total_score)
            result.flags      = triggered_flags
            result.details    = details_map

            actions = _apply_actions(result)
            result.actions_taken = actions

            db_uid = _tg_to_db_id(s, user_id)
            if db_uid:
                _upsert_risk(s, db_uid, total_score, result.risk_level,
                             triggered_flags, result.is_frozen, result.is_suspended)

            # Update action_taken on the most recent log entries
            if triggered_flags and db_uid and actions:
                action_str = ", ".join(actions)
                s.execute(text("""
                    UPDATE fraud_logs SET action_taken = :act
                    WHERE  user_id = :uid
                      AND  created_at >= NOW() - INTERVAL '5 seconds'
                      AND  (action_taken IS NULL OR action_taken = '')
                """), {"act": action_str, "uid": db_uid})

    except Exception:
        logger.exception("run_checks failed for user %s", user_id)

    return result


def quick_check(user_id: int, event: str) -> FraudResult:
    """Lightweight single-event check (e.g. 'deposit', 'withdrawal', 'coupon')."""
    result = FraudResult(user_id=user_id)
    if cfg.get("fds_status", "enabled") != "enabled":
        return result

    event_checks: dict[str, list[str]] = {
        "deposit":    ["duplicate_txid", "duplicate_deposit", "rapid_deposits",
                       "excessive_failed"],
        "withdrawal": ["duplicate_withdrawal", "rapid_withdrawals", "wallet_abuse",
                       "duplicate_wallet"],
        "coupon":     ["coupon_abuse"],
        "referral":   ["referral_abuse", "account_farm"],
        "order":      ["unusual_purchase"],
    }
    codes_to_run = set(event_checks.get(event, []))
    if not codes_to_run:
        return result

    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return result
            wl = s.execute(
                text("SELECT is_whitelisted FROM fraud_user_risk WHERE user_id = "
                     "(SELECT id FROM users WHERE telegram_id = :uid LIMIT 1)"),
                {"uid": user_id}
            ).fetchone()
            if wl and wl[0]:
                return result

            total = 0
            flags: list[str] = []
            for check_code, gate_key, check_fn in _CHECKS:
                if check_code not in codes_to_run:
                    continue
                if gate_key and not cfg.get_bool(gate_key, True):
                    continue
                try:
                    delta, detail = check_fn(s, user_id)
                    if delta > 0:
                        total += delta
                        if check_code not in flags:
                            flags.append(check_code)
                except Exception:
                    pass

            result.risk_score = total
            result.risk_level = _risk_level(total)
            result.flags      = flags
            result.actions_taken = _apply_actions(result)
    except Exception:
        logger.debug("quick_check failed for %s / %s", user_id, event, exc_info=True)

    return result


# ─── State queries ────────────────────────────────────────────────────────────

def get_risk(user_id: int) -> dict[str, Any]:
    """Return the stored risk record for a user (by telegram_id or db id)."""
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return {}
            row = s.execute(text("""
                SELECT fur.risk_score, fur.risk_level, fur.is_frozen,
                       fur.is_suspended, fur.is_whitelisted, fur.is_blacklisted,
                       fur.flags_json, fur.last_checked_at
                FROM   fraud_user_risk fur
                JOIN   users u ON u.id = fur.user_id
                WHERE  u.telegram_id = :uid OR u.id = :uid
                LIMIT  1
            """), {"uid": user_id}).fetchone()
            if row:
                return {
                    "risk_score":     row[0],
                    "risk_level":     row[1],
                    "is_frozen":      row[2],
                    "is_suspended":   row[3],
                    "is_whitelisted": row[4],
                    "is_blacklisted": row[5],
                    "flags":          json.loads(row[6] or "[]"),
                    "last_checked":   row[7],
                }
    except Exception:
        pass
    return {}


def is_frozen(user_id: int) -> bool:
    r = get_risk(user_id)
    return bool(r.get("is_frozen"))


def is_suspended(user_id: int) -> bool:
    r = get_risk(user_id)
    return bool(r.get("is_suspended"))


def is_whitelisted(user_id: int) -> bool:
    r = get_risk(user_id)
    return bool(r.get("is_whitelisted"))


# ─── Admin actions ────────────────────────────────────────────────────────────

def admin_set_state(db_user_id: int, admin_tg_id: int,
                    field: str, value: bool, note: str = "") -> bool:
    """Admin action: set freeze/suspend/whitelist/blacklist for a user (by DB id)."""
    allowed = {"is_frozen", "is_suspended", "is_whitelisted", "is_blacklisted"}
    if field not in allowed:
        return False
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return False
            s.execute(text(f"""
                INSERT INTO fraud_user_risk (user_id, {field}, updated_at)
                VALUES (:uid, :val, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET {field} = :val, updated_at = NOW()
            """), {"uid": db_user_id, "val": value})
            s.execute(text("""
                INSERT INTO fraud_logs
                    (user_id, check_type, risk_score_delta, risk_level,
                     details, action_taken, resolved_by, resolved_at)
                VALUES
                    (:uid, 'admin_action', 0, 'low',
                     :detail, :action, :admin, NOW())
            """), {
                "uid": db_user_id,
                "detail": note or f"Admin set {field}={value}",
                "action": f"admin:{field}={value}",
                "admin": admin_tg_id,
            })
            return True
    except Exception:
        logger.exception("admin_set_state failed")
        return False


def admin_clear_risk(db_user_id: int, admin_tg_id: int) -> bool:
    """Clear all flags and reset risk score to 0 for a user."""
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return False
            s.execute(text("""
                UPDATE fraud_user_risk
                SET    risk_score = 0, risk_level = 'low', flags_json = '[]',
                       is_frozen = FALSE, is_suspended = FALSE, updated_at = NOW()
                WHERE  user_id = :uid
            """), {"uid": db_user_id})
            s.execute(text("""
                INSERT INTO fraud_logs
                    (user_id, check_type, risk_score_delta, risk_level,
                     details, action_taken, resolved_by, resolved_at)
                VALUES (:uid, 'admin_action', 0, 'low',
                        'Admin cleared risk score and flags', 'risk_cleared',
                        :admin, NOW())
            """), {"uid": db_user_id, "admin": admin_tg_id})
            return True
    except Exception:
        logger.exception("admin_clear_risk failed")
        return False


def get_fraud_stats() -> dict[str, Any]:
    """Aggregate counts for the admin dashboard widget."""
    stats: dict[str, Any] = {
        "total_alerts": 0, "high_risk": 0, "critical_risk": 0,
        "frozen": 0, "suspended": 0,
        "today": 0, "weekly": 0, "monthly": 0,
    }
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return stats
            rows = s.execute(text("""
                SELECT
                    COUNT(*)                                                  AS total,
                    COUNT(*) FILTER (WHERE risk_level = 'high')              AS high,
                    COUNT(*) FILTER (WHERE risk_level = 'critical')          AS critical,
                    COUNT(*) FILTER (WHERE is_frozen   = TRUE)               AS frozen,
                    COUNT(*) FILTER (WHERE is_suspended = TRUE)              AS suspended
                FROM fraud_user_risk
                WHERE risk_level != 'low'
            """)).fetchone()
            if rows:
                stats["total_alerts"]  = int(rows[0])
                stats["high_risk"]     = int(rows[1])
                stats["critical_risk"] = int(rows[2])
                stats["frozen"]        = int(rows[3])
                stats["suspended"]     = int(rows[4])

            log_rows = s.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 day')   AS today,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS weekly,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS monthly
                FROM fraud_logs
                WHERE check_type != 'admin_action'
            """)).fetchone()
            if log_rows:
                stats["today"]   = int(log_rows[0])
                stats["weekly"]  = int(log_rows[1])
                stats["monthly"] = int(log_rows[2])
    except Exception:
        logger.debug("get_fraud_stats failed (non-fatal)", exc_info=True)
    return stats


def get_flagged_users(level: str = "all", limit: int = 20,
                      offset: int = 0) -> list[dict[str, Any]]:
    """List users with risk records, filtered by level."""
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_user_risk"):
                return []
            level_filter = "" if level == "all" else f"AND fur.risk_level = '{level}'"
            rows = s.execute(text(f"""
                SELECT u.telegram_id, u.username, fur.risk_score, fur.risk_level,
                       fur.is_frozen, fur.is_suspended, fur.is_whitelisted,
                       fur.flags_json, fur.last_checked_at, fur.user_id
                FROM   fraud_user_risk fur
                JOIN   users u ON u.id = fur.user_id
                WHERE  fur.risk_level != 'low'
                  {level_filter}
                ORDER  BY fur.risk_score DESC, fur.updated_at DESC
                LIMIT  :lim OFFSET :off
            """), {"lim": limit, "off": offset}).fetchall()
            return [
                {
                    "telegram_id":  r[0], "username": r[1],
                    "risk_score":   r[2], "risk_level": r[3],
                    "is_frozen":    r[4], "is_suspended": r[5],
                    "is_whitelisted": r[6],
                    "flags":        json.loads(r[7] or "[]"),
                    "last_checked": r[8], "db_user_id": r[9],
                }
                for r in rows
            ]
    except Exception:
        logger.debug("get_flagged_users failed (non-fatal)", exc_info=True)
    return []


def get_user_logs(db_user_id: int, limit: int = 15,
                  offset: int = 0) -> list[dict[str, Any]]:
    """Fetch fraud log entries for a specific user."""
    try:
        with _get_session() as s:
            if not _table_ok(s, "fraud_logs"):
                return []
            rows = s.execute(text("""
                SELECT check_type, risk_score_delta, risk_level,
                       details, action_taken, created_at
                FROM   fraud_logs
                WHERE  user_id = :uid
                ORDER  BY created_at DESC
                LIMIT  :lim OFFSET :off
            """), {"uid": db_user_id, "lim": limit, "off": offset}).fetchall()
            return [
                {
                    "check_type": r[0], "delta": r[1], "level": r[2],
                    "details": r[3], "action": r[4], "created_at": r[5],
                }
                for r in rows
            ]
    except Exception:
        pass
    return []


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _is_tg_id(s, uid: int) -> bool:
    """Heuristic: telegram_ids are very large; db ids are small."""
    return uid > 1_000_000


def _tg_to_db_id(s, tg_id: int) -> int | None:
    try:
        row = s.execute(
            text("SELECT id FROM users WHERE telegram_id = :uid LIMIT 1"),
            {"uid": tg_id}
        ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None
