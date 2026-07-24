"""API Integration health-check service — V41.

Background job + manual test-connection logic for the API Manager.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

from database import get_db_session
from database.models import ApiIntegration, ApiConnectionLog
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

_STATUS_CONNECTED   = "connected"
_STATUS_SLOW        = "slow"
_STATUS_WARNING     = "warning"
_STATUS_OFFLINE     = "offline"
_STATUS_UNKNOWN     = "unknown"

# ms thresholds
_SLOW_THRESHOLD_MS    = 2000
_WARNING_THRESHOLD_MS = 5000


# ─── helpers ─────────────────────────────────────────────────────────────────

def _mask_key(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (masked_value, hint_4chars)."""
    if not raw:
        return None, None
    hint = raw[-4:] if len(raw) >= 4 else raw
    return raw, hint


def mask_for_display(hint: Optional[str]) -> str:
    """Return ****HINT or [not set] for UI display."""
    if not hint:
        return "—"
    return f"****{hint}"


def _classify_status(response_ms: Optional[int], error: bool) -> str:
    if error or response_ms is None:
        return _STATUS_OFFLINE
    if response_ms > _WARNING_THRESHOLD_MS:
        return _STATUS_WARNING
    if response_ms > _SLOW_THRESHOLD_MS:
        return _STATUS_SLOW
    return _STATUS_CONNECTED


def _status_emoji(status: str) -> str:
    return {
        _STATUS_CONNECTED: "🟢",
        _STATUS_SLOW:      "🟡",
        _STATUS_WARNING:   "🟠",
        _STATUS_OFFLINE:   "🔴",
        _STATUS_UNKNOWN:   "⚫",
    }.get(status, "⚫")


STATUS_EMOJI = _status_emoji  # exported for UI


# ─── test connection ──────────────────────────────────────────────────────────

def test_connection(integration: ApiIntegration) -> Tuple[str, Optional[int], Optional[str]]:
    """Attempt a live health check for an integration.

    Returns (connection_status, response_time_ms, error_message).
    Supports HTTP/HTTPS base_url ping and a generic 'unknown' fallback.
    """
    timeout = cfg.get_int("aim_timeout_seconds", 10)
    base_url = (integration.base_url or "").strip()

    if not base_url:
        return _STATUS_UNKNOWN, None, "No base URL configured — cannot test."

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        return _STATUS_UNKNOWN, None, f"Unsupported scheme: {parsed.scheme}"

    retries = cfg.get_int("aim_retry_count", 3)
    last_error: Optional[str] = None
    for attempt in range(max(1, retries)):
        t0: Optional[float] = None
        try:
            req = Request(base_url, headers={"User-Agent": "TelegramBot-HealthCheck/1.0"})
            t0 = time.monotonic()
            with urlopen(req, timeout=timeout) as resp:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                http_status = resp.status
            status = _classify_status(elapsed_ms, False)
            return status, elapsed_ms, None
        except HTTPError as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            # 4xx are still "reachable"
            if 400 <= e.code < 500:
                return _STATUS_CONNECTED, elapsed_ms, f"HTTP {e.code} (reachable)"
            last_error = f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            last_error = str(e.reason)
        except Exception as e:
            last_error = str(e)

    return _STATUS_OFFLINE, None, last_error


def run_health_check(integration_id: int) -> None:
    """Run a health check and persist the result."""
    with get_db_session() as session:
        integration = session.query(ApiIntegration).filter_by(id=integration_id).first()
        if not integration or integration.status == "disabled":
            return

        status, resp_ms, err = test_connection(integration)
        now = datetime.utcnow()
        integration.connection_status = status
        integration.response_time_ms = resp_ms
        integration.last_check_at = now
        if status in (_STATUS_CONNECTED, _STATUS_SLOW):
            integration.last_success_at = now
        if err:
            integration.last_error_at = now
            integration.last_error_message = err
        integration.updated_at = now

        log = ApiConnectionLog(
            integration_id=integration.id,
            status=status,
            response_time_ms=resp_ms,
            error_message=err,
            checked_at=now,
        )
        session.add(log)
        session.commit()


# ─── background job ───────────────────────────────────────────────────────────

async def health_check_job(context) -> None:
    """Scheduled job: health-check all enabled integrations."""
    aim_status = cfg.get_str("aim_status", "enabled")
    if aim_status != "enabled":
        return
    if not cfg.get_bool("aim_auto_health_check", True):
        return

    try:
        with get_db_session() as session:
            integrations = (
                session.query(ApiIntegration)
                .filter_by(is_active=True)
                .filter(ApiIntegration.status != "disabled")
                .all()
            )
            ids = [i.id for i in integrations]

        for iid in ids:
            try:
                run_health_check(iid)
            except Exception:
                logger.exception("health_check_job: failed for integration %s", iid)

        # Prune old logs
        retention_days = cfg.get_int("aim_log_retention_days", 30)
        if retention_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            with get_db_session() as session:
                session.query(ApiConnectionLog).filter(
                    ApiConnectionLog.checked_at < cutoff
                ).delete(synchronize_session=False)
                session.commit()
    except Exception:
        logger.exception("health_check_job: outer failure")


# ─── seed built-in entries ────────────────────────────────────────────────────

_BUILT_IN_INTEGRATIONS = [
    dict(name="Telegram Bot API",    provider="Telegram",    api_type="telegram",
         base_url="https://api.telegram.org",   is_built_in=True),
    dict(name="NOWPayments",         provider="NOWPayments", api_type="payment",
         base_url="https://api.nowpayments.io", is_built_in=True),
    dict(name="Binance Pay",         provider="Binance",     api_type="payment",
         base_url="https://bpay.binanceapi.com",is_built_in=True),
    dict(name="Bybit Pay",           provider="Bybit",       api_type="payment",
         base_url="https://api.bybit.com",      is_built_in=True),
    dict(name="Cryptomus",           provider="Cryptomus",   api_type="payment",
         base_url="https://api.cryptomus.com",  is_built_in=True),
    dict(name="Heleket",             provider="Heleket",     api_type="payment",
         base_url="https://api.heleket.com",    is_built_in=True),
]


def seed_built_in_integrations() -> None:
    """Insert built-in integration rows if not already present. Safe to call on every start."""
    try:
        with get_db_session() as session:
            for spec in _BUILT_IN_INTEGRATIONS:
                existing = (
                    session.query(ApiIntegration)
                    .filter_by(name=spec["name"], is_built_in=True)
                    .first()
                )
                if not existing:
                    session.add(ApiIntegration(**spec))
            session.commit()
    except Exception:
        logger.exception("seed_built_in_integrations failed")
