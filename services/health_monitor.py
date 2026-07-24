"""API Health Monitor Service — V27.

Background service that probes every configured external API/service,
stores results in ``api_health_log``, and fires Telegram alerts to the
admin when a service transitions to offline or repeatedly fails.

Designed to run as an APScheduler job inside the bot process (no extra
daemon required).  All database writes are fire-and-forget — failures are
logged but never propagate to the caller.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp

from config.settings import settings
from database import get_db_session
from database.models import ApiHealthLog, WebhookLog, AdminAuditLog
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ── Service catalogue ──────────────────────────────────────────────────────
#
# Each entry: key → (label, url_template, extra_headers, needs_env_key)
# ``url_template`` may contain ``{key}`` which is replaced with the
# relevant env/settings value if ``needs_env_key`` is set.
# Services with no public ping URL (mobile banking) are checked via a
# sentinel config-flag rather than a real HTTP call.

SERVICES: Dict[str, Dict] = {
    "telegram": {
        "label":   "Telegram Bot API",
        "url":     f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe",
        "timeout": 10,
        "method":  "GET",
    },
    "nowpayments": {
        "label":   "NOWPayments",
        "url":     "https://api.nowpayments.io/v1/status",
        "headers": {"x-api-key": getattr(settings, "NOWPAYMENTS_API_KEY", "")},
        "timeout": 10,
        "method":  "GET",
    },
    "binance": {
        "label":   "Binance Pay",
        "url":     "https://api.binance.com/api/v3/time",
        "timeout": 10,
        "method":  "GET",
    },
    "bybit": {
        "label":   "Bybit Pay",
        "url":     "https://api.bybit.com/v3/public/time",
        "timeout": 10,
        "method":  "GET",
    },
    "trc20": {
        "label":   "USDT TRC20 (Tron)",
        "url":     "https://api.trongrid.io/v1/blocks?limit=1",
        "timeout": 10,
        "method":  "GET",
    },
    "bep20": {
        "label":   "USDT BEP20 (BSC)",
        "url":     "https://api.bscscan.com/api?module=proxy&action=eth_blockNumber",
        "timeout": 10,
        "method":  "GET",
    },
    "erc20": {
        "label":   "USDT ERC20 (Ethereum)",
        "url":     "https://cloudflare-eth.com/",
        "timeout": 10,
        "method":  "POST",
        "json":    {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
    },
    "database": {
        "label":   "PostgreSQL Database",
        "url":     None,          # handled via sentinel DB query
        "timeout": 5,
        "method":  "db",
    },
    "mobile_banking": {
        "label":   "Mobile Banking",
        "url":     None,          # no public ping; check internal config flag
        "timeout": 0,
        "method":  "config",
        "config_key": "mobile_banking_enabled",
    },
}

STATUS_ONLINE  = "online"
STATUS_SLOW    = "slow"
STATUS_WARNING = "warning"
STATUS_OFFLINE = "offline"

STATUS_ICONS = {
    STATUS_ONLINE:  "🟢",
    STATUS_SLOW:    "🟡",
    STATUS_WARNING: "🟠",
    STATUS_OFFLINE: "🔴",
}

# In-memory last-known status per service. This is only a fast-path cache
# for the currently running process — it is NOT the source of truth for
# duplicate-alert prevention, because it is lost on every restart/redeploy
# (which is exactly when the old implementation used to re-fire alerts for
# an already-known bad status). The authoritative previous status is always
# re-derived from the persisted ``api_health_log`` table; see
# ``_get_prior_status`` / ``_run_one`` below.
_last_status: Dict[str, str] = {}

# Serializes full health-check runs so a scheduled tick and an admin-triggered
# "🔁 Refresh" (or two overlapping ticks, if a run ever takes longer than the
# configured interval) can never execute concurrently. Concurrent runs were
# the other main source of duplicate alerts — both would read the same
# "previous status" before either had written a new result.
_health_check_lock = asyncio.Lock()


# ── Single-service probe ───────────────────────────────────────────────────

async def _probe_service(key: str, cfg_svc: Dict) -> Tuple[str, int, Optional[str], Optional[int]]:
    """Probe one service. Returns (status, response_time_ms, error_msg, http_code)."""
    slow_ms = cfg.get_int("health_slow_threshold_ms", 2000)
    warn_ms = cfg.get_int("health_warn_threshold_ms", 5000)
    timeout = cfg_svc.get("timeout", 10)

    method = cfg_svc.get("method", "GET")

    # ── Database check ────────────────────────────────────────────────────
    if method == "db":
        t0 = time.monotonic()
        try:
            import sqlalchemy as sa
            with get_db_session() as s:
                s.execute(sa.text("SELECT 1"))
            ms = int((time.monotonic() - t0) * 1000)
            status = (STATUS_ONLINE if ms < slow_ms else
                      STATUS_SLOW   if ms < warn_ms else STATUS_WARNING)
            return status, ms, None, None
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return STATUS_OFFLINE, ms, str(exc)[:256], None

    # ── Config flag check ─────────────────────────────────────────────────
    if method == "config":
        ck = cfg_svc.get("config_key", "")
        enabled = cfg.get_bool(ck, False) if ck else False
        status = STATUS_ONLINE if enabled else STATUS_OFFLINE
        msg = None if enabled else "Feature disabled in BotConfig"
        return status, 0, msg, None

    # ── HTTP probe ────────────────────────────────────────────────────────
    url = cfg_svc.get("url")
    if not url:
        return STATUS_OFFLINE, 0, "No URL configured", None

    headers = cfg_svc.get("headers", {})
    body    = cfg_svc.get("json")

    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            if method == "POST" and body is not None:
                resp = await asyncio.wait_for(
                    session.post(url, json=body, headers=headers),
                    timeout=timeout)
            else:
                resp = await asyncio.wait_for(
                    session.get(url, headers=headers),
                    timeout=timeout)
            http_code = resp.status
        ms = int((time.monotonic() - t0) * 1000)

        if http_code >= 500:
            return STATUS_OFFLINE, ms, f"HTTP {http_code}", http_code
        if http_code >= 400:
            return STATUS_WARNING, ms, f"HTTP {http_code}", http_code

        status = (STATUS_ONLINE if ms < slow_ms else
                  STATUS_SLOW   if ms < warn_ms else STATUS_WARNING)
        return status, ms, None, http_code

    except asyncio.TimeoutError:
        ms = int((time.monotonic() - t0) * 1000)
        return STATUS_OFFLINE, ms, "Timeout", None
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return STATUS_OFFLINE, ms, str(exc)[:256], None


# ── Read prior status BEFORE the new result is persisted ──────────────────

def _get_prior_status(service_name: str) -> Optional[str]:
    """Most recent persisted status for a service, read *before* this
    check's result is written — i.e. "what admins were last told this
    service's state was". This is what makes transition-detection survive
    process restarts: a fresh process has an empty ``_last_status`` cache,
    but the database still remembers the last real status.
    """
    try:
        with get_db_session() as s:
            row = (s.query(ApiHealthLog)
                   .filter_by(service_name=service_name)
                   .order_by(ApiHealthLog.checked_at.desc())
                   .first())
            return row.status if row else None
    except Exception:
        logger.exception("health_monitor: failed to read prior status for %s", service_name)
        return None


# ── Persist health check result ────────────────────────────────────────────

def _persist(service_name: str, status: str, ms: int,
             error: Optional[str], http_code: Optional[int]) -> None:
    try:
        with get_db_session() as s:
            s.add(ApiHealthLog(
                service_name     = service_name,
                status           = status,
                response_time_ms = ms,
                error_message    = error,
                http_status      = http_code,
                checked_at       = datetime.utcnow(),
            ))
            s.commit()
    except Exception:
        logger.exception("health_monitor: failed to persist result for %s", service_name)


# ── Duplicate-alert / cooldown guard (persisted — survives restarts) ──────

def _recent_duplicate_alert(service_name: str, status: str, cooldown_min: int) -> bool:
    """True if an alert for this exact (service, new-status) pair was
    already sent within the cooldown window.

    Backed by ``admin_audit_logs`` (module="health_monitor") rather than an
    in-memory timestamp, so the cooldown holds even across scheduler
    restarts, redeploys, or an admin manually hitting "🔁 Refresh" moments
    after a scheduled check already alerted on the same status.
    """
    if cooldown_min <= 0:
        return False
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=cooldown_min)
        with get_db_session() as s:
            row = (s.query(AdminAuditLog)
                   .filter(AdminAuditLog.module == "health_monitor",
                           AdminAuditLog.target_id == service_name,
                           AdminAuditLog.new_value == status,
                           AdminAuditLog.created_at >= cutoff)
                   .order_by(AdminAuditLog.created_at.desc())
                   .first())
            return row is not None
    except Exception:
        logger.exception("health_monitor: cooldown check failed for %s", service_name)
        # Fail open: a rare duplicate alert is far less harmful than silently
        # losing real alerts because the DB hiccupped.
        return False


def _record_alert_history(service_name: str, prev: Optional[str], status: str,
                          error: Optional[str], at: datetime) -> None:
    """Record that an alert was actually sent — the audit trail doubles as
    the authoritative record used by ``_recent_duplicate_alert`` above.
    Stores: API name (target_id), previous status (old_value), new status
    (new_value), timestamp (created_at, automatic), error message
    (details), and — for recoveries — the recovery time is simply this
    same timestamp on a ``health_monitor.recovered`` row.
    """
    try:
        from utils.audit import log_admin_action
        action = "health_monitor.recovered" if status == STATUS_ONLINE else "health_monitor.alert"
        log_admin_action(
            admin_telegram_id=settings.ADMIN_TELEGRAM_ID,
            action=action,
            target_type="api_service",
            target_id=service_name,
            details=error or ("Service recovered" if status == STATUS_ONLINE else "—"),
            old_value=prev or "unknown",
            new_value=status,
            module="health_monitor",
        )
    except Exception:
        logger.exception("health_monitor: failed to record alert history for %s", service_name)


# ── Alert admin on status change ───────────────────────────────────────────

async def _alert_if_changed(service_name: str, label: str, status: str,
                             error: Optional[str], context,
                             prev: Optional[str]) -> None:
    # Keep the fast in-memory cache in sync for this process (diagnostics /
    # cheap short-circuit only — never the sole guard against duplicates).
    _last_status[service_name] = status

    if not cfg.get_bool("webhook_monitor_admin_alerts", True):
        return

    # No state change at all — this is the normal, common case on every
    # poll while a service just sits at OK (or sits at a known bad status).
    if prev == status:
        return
    if prev is None and status == STATUS_ONLINE:
        return  # first-ever check and already healthy — nothing to announce

    # Persisted cooldown — absorbs restart re-detection, overlapping runs
    # that slipped past the job-level lock, and manual-refresh spam.
    cooldown_min = cfg.get_int("health_alert_cooldown_minutes", 60)
    if _recent_duplicate_alert(service_name, status, cooldown_min):
        logger.info(
            "health_monitor: suppressing duplicate alert for %s (%s within %sm cooldown)",
            service_name, status, cooldown_min)
        return

    icon = STATUS_ICONS.get(status, "❓")
    now = datetime.utcnow()
    from utils.notify_format import render as _render, utc_now_str as _ts
    if status == STATUS_ONLINE:
        msg = _render("✅", f"{label} Recovered", [
            ("Status", f"{icon} {status.upper()}"),
            ("Previous status", f"{STATUS_ICONS.get(prev, '❓')} {(prev or '—').upper()}"),
        ], _ts())
    else:
        msg = _render("🚨", f"API Alert: {label}", [
            ("Status", f"{icon} {status.upper()}"),
            ("Previous status", f"{STATUS_ICONS.get(prev, '❓')} {(prev or '—').upper()}"),
            ("Error", error or "—"),
        ], _ts())

    try:
        from services.notifications import notify_admins as _notify_admins
        sent = await _notify_admins(context.bot, "system_alert", msg)
        if not sent:
            return  # opted out / not delivered anywhere — don't record history
    except Exception:
        logger.exception("health_monitor: failed to send admin alert for %s", service_name)
        return  # don't record history for an alert that never actually sent

    _record_alert_history(service_name, prev, status, error, now)


# ── Main health check job ──────────────────────────────────────────────────

async def health_check_job(context) -> None:
    """APScheduler job — probe all services and store results.

    Guarded by ``_health_check_lock`` so a scheduled tick can never overlap
    with another tick or an admin-triggered manual refresh; overlapping runs
    were the main source of racy duplicate alerts.
    """
    monitor_status = cfg.get("webhook_monitor_status", "enabled")
    if monitor_status != "enabled":
        return

    if _health_check_lock.locked():
        logger.info("health_monitor: a check is already running — skipping this tick")
        return

    async with _health_check_lock:
        tasks = [
            _run_one(key, svc, context)
            for key, svc in SERVICES.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Prune old records
        _prune_old_records()


async def _run_one(key: str, svc: Dict, context) -> None:
    try:
        status, ms, error, http_code = await _probe_service(key, svc)
        # Read the prior status BEFORE overwriting it, so the transition
        # check always compares against what was last actually persisted —
        # not against a possibly-empty in-memory cache.
        prev = _get_prior_status(key)
        _persist(key, status, ms, error, http_code)
        await _alert_if_changed(key, svc["label"], status, error, context, prev)
    except Exception:
        logger.exception("health_monitor: unhandled error for service %s", key)


def _prune_old_records() -> None:
    try:
        days = cfg.get_int("webhook_log_retention_days", 30)
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db_session() as s:
            import sqlalchemy as sa
            s.execute(sa.text(
                "DELETE FROM api_health_log WHERE checked_at < :c"),
                {"c": cutoff})
            s.execute(sa.text(
                "DELETE FROM webhook_log WHERE received_at < :c"),
                {"c": cutoff})
            s.commit()
    except Exception:
        logger.exception("health_monitor: prune failed")


# ── Public helpers used by admin panel ────────────────────────────────────

def get_latest_statuses() -> List[Dict]:
    """Return the most recent health-check result for each service."""
    results = []
    try:
        import sqlalchemy as sa
        with get_db_session() as s:
            for key, svc in SERVICES.items():
                row = (s.query(ApiHealthLog)
                       .filter_by(service_name=key)
                       .order_by(ApiHealthLog.checked_at.desc())
                       .first())
                if row:
                    results.append({
                        "key":    key,
                        "label":  svc["label"],
                        "status": row.status,
                        "ms":     row.response_time_ms,
                        "error":  row.error_message,
                        "http":   row.http_status,
                        "at":     row.checked_at,
                    })
                else:
                    results.append({
                        "key":    key,
                        "label":  svc["label"],
                        "status": "unknown",
                        "ms":     None,
                        "error":  "Never checked",
                        "http":   None,
                        "at":     None,
                    })
    except Exception:
        logger.exception("get_latest_statuses: query failed")
    return results


def get_service_history(service_key: str, limit: int = 20) -> List[ApiHealthLog]:
    """Return the last N health-check rows for a single service."""
    try:
        with get_db_session() as s:
            rows = (s.query(ApiHealthLog)
                    .filter_by(service_name=service_key)
                    .order_by(ApiHealthLog.checked_at.desc())
                    .limit(limit)
                    .all())
            return [
                {
                    "status": r.status,
                    "ms":     r.response_time_ms,
                    "error":  r.error_message,
                    "http":   r.http_status,
                    "at":     r.checked_at,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("get_service_history: query failed for %s", service_key)
        return []


# ── Webhook logging utility (called by webhook_server.py) ─────────────────

def log_webhook_event(
    provider: str,
    webhook_uuid: str,
    status: str,
    processing_time_ms: int = 0,
    error_message: Optional[str] = None,
    order_id: Optional[int] = None,
    user_id: Optional[int] = None,
    payment_id: Optional[str] = None,
    transaction_id: Optional[str] = None,
    raw_payload: Optional[str] = None,
) -> Optional[int]:
    """Write one WebhookLog row. Returns the new row id or None on error.

    Duplicate UUIDs (same event delivered twice) silently return None —
    the caller should treat this as "already processed".
    """
    try:
        with get_db_session() as s:
            # Idempotency: check if already exists
            existing = s.query(WebhookLog).filter_by(webhook_uuid=webhook_uuid).first()
            if existing:
                return None  # duplicate suppressed

            row = WebhookLog(
                webhook_uuid       = webhook_uuid,
                provider           = provider,
                received_at        = datetime.utcnow(),
                processing_time_ms = processing_time_ms,
                status             = status,
                error_message      = error_message,
                retry_count        = 0,
                order_id           = order_id,
                user_id            = user_id,
                payment_id         = payment_id,
                transaction_id     = transaction_id,
                raw_payload        = raw_payload,
            )
            s.add(row)
            s.commit()
            return row.id
    except Exception:
        logger.exception("log_webhook_event: failed to write log for provider=%s uuid=%s",
                         provider, webhook_uuid)
        return None


def update_webhook_status(webhook_uuid: str, status: str,
                          error_message: Optional[str] = None,
                          processing_time_ms: Optional[int] = None,
                          order_id: Optional[int] = None,
                          user_id: Optional[int] = None,
                          payment_id: Optional[str] = None,
                          transaction_id: Optional[str] = None) -> None:
    """Update status / enrichment fields on an existing WebhookLog row."""
    try:
        with get_db_session() as s:
            row = s.query(WebhookLog).filter_by(webhook_uuid=webhook_uuid).first()
            if not row:
                return
            row.status = status
            if error_message is not None:
                row.error_message = error_message
            if processing_time_ms is not None:
                row.processing_time_ms = processing_time_ms
            if order_id is not None:
                row.order_id = order_id
            if user_id is not None:
                row.user_id = user_id
            if payment_id is not None:
                row.payment_id = payment_id
            if transaction_id is not None:
                row.transaction_id = transaction_id
            s.commit()
    except Exception:
        logger.exception("update_webhook_status: failed for uuid=%s", webhook_uuid)


def make_webhook_uuid(provider: str, raw_id: str) -> str:
    """Stable, collision-safe UUID from provider + raw event ID."""
    combined = f"{provider}:{raw_id}"
    return hashlib.sha256(combined.encode()).hexdigest()[:64]
