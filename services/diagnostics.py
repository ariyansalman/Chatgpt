"""System Diagnostics Center Service — V34.

Runs comprehensive health checks across all system components:
  • Telegram Bot API connectivity
  • Database (PostgreSQL) connection & query health
  • NOWPayments API
  • Binance Pay API
  • Bybit Pay API
  • Wallet system
  • Orders subsystem
  • Referral subsystem
  • Scheduler (job queue)
  • Storage (backup directory)
  • Memory usage
  • CPU usage
  • Disk usage
  • Background jobs

Each check returns a CheckResult with:
  status:  "healthy" | "warning" | "critical"
  name:    Human-readable check name
  detail:  Short explanation
  value:   Optional metric value (e.g. "42 MB", "95%")
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/telegram-store"))


class CheckResult:
    __slots__ = ("name", "status", "detail", "value", "duration_ms")

    def __init__(self, name: str, status: str, detail: str,
                 value: str = "", duration_ms: float = 0.0):
        self.name = name
        self.status = status       # healthy | warning | critical
        self.detail = detail
        self.value = value
        self.duration_ms = duration_ms

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "value": self.value,
            "duration_ms": round(self.duration_ms, 1),
        }

    @property
    def emoji(self) -> str:
        return {"healthy": "🟢", "warning": "🟡", "critical": "🔴"}.get(self.status, "⚪")


# ──────────────────────────────────────────────────────────────────────────
# Individual checks
# ──────────────────────────────────────────────────────────────────────────

def _check_database() -> CheckResult:
    name = "Database"
    t0 = time.monotonic()
    try:
        from database.db import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        ms = (time.monotonic() - t0) * 1000
        if ms > 2000:
            return CheckResult(name, "warning", f"Slow response ({ms:.0f} ms)", f"{ms:.0f} ms", ms)
        return CheckResult(name, "healthy", "Connected, SELECT 1 OK", f"{ms:.0f} ms", ms)
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(name, "critical", f"Connection failed: {str(e)[:100]}", "", ms)


def _check_database_pool() -> CheckResult:
    name = "DB Pool"
    try:
        from database.db import engine
        pool = engine.pool
        checked_out = getattr(pool, "checkedout", lambda: None)()
        size = getattr(pool, "size", lambda: None)()
        overflow = getattr(pool, "overflow", lambda: None)()
        if checked_out is None:
            return CheckResult(name, "healthy", "Pool stats unavailable (not QueuePool)", "")
        if checked_out is not None and size is not None and checked_out >= size:
            return CheckResult(name, "warning",
                               f"Pool at capacity: {checked_out}/{size}",
                               f"{checked_out}/{size}")
        detail = f"checkedout={checked_out}, size={size}, overflow={overflow}"
        return CheckResult(name, "healthy", detail, f"{checked_out}/{size}")
    except Exception as e:
        return CheckResult(name, "warning", f"Could not inspect pool: {str(e)[:80]}", "")


def _check_orders() -> CheckResult:
    name = "Orders"
    try:
        from database import get_db_session, Order
        from database.models import OrderStatus
        with get_db_session() as s:
            total = s.query(Order).count()
            pending = s.query(Order).filter(
                Order.status == OrderStatus.PROCESSING
            ).count()
        ratio = pending / max(total, 1)
        if ratio > 0.5 and pending > 10:
            return CheckResult(name, "warning",
                               f"High pending ratio: {pending} processing / {total} total",
                               f"{pending} pending")
        return CheckResult(name, "healthy",
                           f"Total: {total}  Processing: {pending}",
                           f"{total} total")
    except Exception as e:
        return CheckResult(name, "critical", f"Query failed: {str(e)[:80]}", "")


def _check_wallet() -> CheckResult:
    name = "Wallet"
    try:
        from database import get_db_session
        from database.models import WalletLedger
        with get_db_session() as s:
            count = s.query(WalletLedger).count()
        return CheckResult(name, "healthy", f"Ledger rows: {count}", str(count))
    except Exception as e:
        return CheckResult(name, "critical", f"Query failed: {str(e)[:80]}", "")


def _check_referral() -> CheckResult:
    name = "Referral"
    try:
        from database import get_db_session
        from database.models import ReferralReward
        with get_db_session() as s:
            pending_cnt = (s.query(ReferralReward)
                           .filter_by(paid_out=False).count())
        if pending_cnt > 500:
            return CheckResult(name, "warning",
                               f"Large pending payout queue: {pending_cnt}",
                               str(pending_cnt))
        return CheckResult(name, "healthy", f"Pending payouts: {pending_cnt}", str(pending_cnt))
    except Exception as e:
        return CheckResult(name, "warning", f"Query failed: {str(e)[:80]}", "")


def _check_storage() -> CheckResult:
    name = "Storage (Backups)"
    try:
        bdir = BACKUP_DIR
        if not bdir.exists():
            return CheckResult(name, "warning", "Backup directory does not exist yet.", "")
        stat = os.statvfs(str(bdir))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
        used_pct = 100 * (1 - stat.f_bavail / max(stat.f_blocks, 1))
        if free_gb < 0.5:
            return CheckResult(name, "critical",
                               f"Very low disk space: {free_gb:.1f} GB free",
                               f"{used_pct:.0f}% used")
        if free_gb < 2.0:
            return CheckResult(name, "warning",
                               f"Low disk space: {free_gb:.1f} GB free",
                               f"{used_pct:.0f}% used")
        return CheckResult(name, "healthy",
                           f"{free_gb:.1f} GB free of {total_gb:.1f} GB",
                           f"{used_pct:.0f}% used")
    except Exception as e:
        return CheckResult(name, "warning", f"Could not stat storage: {str(e)[:80]}", "")


def _check_memory() -> CheckResult:
    name = "Memory"
    try:
        with open("/proc/meminfo") as f:
            lines = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
        total_kb = int(lines["MemTotal"].split()[0])
        avail_kb = int(lines["MemAvailable"].split()[0])
        used_pct = 100 * (1 - avail_kb / max(total_kb, 1))
        avail_mb = avail_kb / 1024
        total_mb = total_kb / 1024
        detail = f"{avail_mb:.0f} MB free of {total_mb:.0f} MB"
        if used_pct > 90:
            return CheckResult(name, "critical", detail, f"{used_pct:.0f}%")
        if used_pct > 75:
            return CheckResult(name, "warning", detail, f"{used_pct:.0f}%")
        return CheckResult(name, "healthy", detail, f"{used_pct:.0f}%")
    except Exception:
        # Non-Linux or /proc not available
        try:
            import resource
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            return CheckResult(name, "healthy", f"Process RSS: {mem_mb:.0f} MB", f"{mem_mb:.0f} MB")
        except Exception as e:
            return CheckResult(name, "warning", f"Could not read memory: {str(e)[:80]}", "")


def _check_cpu() -> CheckResult:
    name = "CPU"
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        load1 = float(la[0])
        # Count CPUs
        try:
            cpus = os.cpu_count() or 1
        except Exception:
            cpus = 1
        per_cpu = load1 / cpus
        if per_cpu > 2.0:
            return CheckResult(name, "critical",
                               f"High load: {load1:.2f} ({cpus} CPUs)",
                               f"{load1:.2f}")
        if per_cpu > 1.0:
            return CheckResult(name, "warning",
                               f"Elevated load: {load1:.2f} ({cpus} CPUs)",
                               f"{load1:.2f}")
        return CheckResult(name, "healthy",
                           f"1m load avg: {load1:.2f} ({cpus} CPUs)",
                           f"{load1:.2f}")
    except Exception:
        return CheckResult(name, "healthy", "Load avg not available (non-Linux)", "")


def _check_disk() -> CheckResult:
    name = "Disk (/)"
    try:
        stat = os.statvfs("/")
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
        used_pct = 100 * (1 - stat.f_bavail / max(stat.f_blocks, 1))
        detail = f"{free_gb:.1f} GB free of {total_gb:.1f} GB"
        if free_gb < 1.0:
            return CheckResult(name, "critical", detail, f"{used_pct:.0f}%")
        if free_gb < 5.0:
            return CheckResult(name, "warning", detail, f"{used_pct:.0f}%")
        return CheckResult(name, "healthy", detail, f"{used_pct:.0f}%")
    except Exception as e:
        return CheckResult(name, "warning", f"Could not stat disk: {str(e)[:80]}", "")


def _check_scheduler(job_queue=None) -> CheckResult:
    name = "Scheduler"
    try:
        if job_queue is None:
            return CheckResult(name, "warning", "Job queue not accessible at check time.", "")
        jobs = list(job_queue.jobs()) if hasattr(job_queue, "jobs") else []
        if not jobs:
            return CheckResult(name, "warning", "No scheduled jobs found.", "0 jobs")
        # Check if any job is severely overdue (next_t is more than 10 minutes ago)
        overdue = 0
        now = datetime.utcnow()
        for j in jobs:
            nxt = getattr(j, "next_t", None)
            if nxt is not None:
                try:
                    if hasattr(nxt, "timestamp"):
                        diff = (now - nxt.replace(tzinfo=None)).total_seconds()
                        if diff > 600:
                            overdue += 1
                except Exception:
                    pass
        if overdue:
            return CheckResult(name, "warning",
                               f"{overdue}/{len(jobs)} jobs appear overdue",
                               f"{len(jobs)} jobs")
        return CheckResult(name, "healthy",
                           f"{len(jobs)} job(s) scheduled",
                           f"{len(jobs)} jobs")
    except Exception as e:
        return CheckResult(name, "warning", f"Could not inspect scheduler: {str(e)[:80]}", "")


def _check_nowpayments() -> CheckResult:
    name = "NOWPayments"
    try:
        from utils.bot_config import cfg
        api_key = cfg.get("nowpayments_api_key", "")
        if not api_key:
            return CheckResult(name, "warning", "API key not configured.", "")
        import urllib.request
        t0 = time.monotonic()
        req = urllib.request.Request(
            "https://api.nowpayments.io/v1/status",
            headers={"x-api-key": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ms = (time.monotonic() - t0) * 1000
            body = json.loads(resp.read())
            if body.get("message") == "OK":
                return CheckResult(name, "healthy", "API reachable, status OK", f"{ms:.0f} ms", ms)
            return CheckResult(name, "warning", f"Unexpected status: {body}", f"{ms:.0f} ms", ms)
    except Exception as e:
        return CheckResult(name, "critical", f"API unreachable: {str(e)[:80]}", "")


def _check_binance_pay() -> CheckResult:
    name = "Binance Pay"
    try:
        from utils.bot_config import cfg
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        with get_db_session() as s:
            gw = s.query(PaymentGatewayConfig).filter_by(
                gateway_name="binance_pay").first()
            if not gw or not gw.is_active:
                return CheckResult(name, "healthy", "Not enabled — skipped.", "")
            api_key = getattr(gw, "binance_api_key", None)
        if not api_key:
            return CheckResult(name, "warning", "Active but API key not configured.", "")
        # Ping Binance API server (public endpoint)
        import urllib.request
        t0 = time.monotonic()
        urllib.request.urlopen("https://api.binance.com/api/v3/ping", timeout=10)
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(name, "healthy", f"Binance API reachable ({ms:.0f} ms)", f"{ms:.0f} ms", ms)
    except Exception as e:
        return CheckResult(name, "warning", f"Check failed: {str(e)[:80]}", "")


def _check_bybit_pay() -> CheckResult:
    name = "Bybit Pay"
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        with get_db_session() as s:
            gw = s.query(PaymentGatewayConfig).filter_by(
                gateway_name="bybit_pay").first()
            if not gw or not gw.is_active:
                return CheckResult(name, "healthy", "Not enabled — skipped.", "")
        import urllib.request
        t0 = time.monotonic()
        urllib.request.urlopen("https://api.bybit.com/v3/public/time", timeout=10)
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(name, "healthy", f"Bybit API reachable ({ms:.0f} ms)", f"{ms:.0f} ms", ms)
    except Exception as e:
        return CheckResult(name, "warning", f"Check failed: {str(e)[:80]}", "")


def _check_telegram_api() -> CheckResult:
    name = "Telegram API"
    try:
        import urllib.request
        t0 = time.monotonic()
        urllib.request.urlopen("https://api.telegram.org", timeout=10)
        ms = (time.monotonic() - t0) * 1000
        if ms > 3000:
            return CheckResult(name, "warning", f"Slow response ({ms:.0f} ms)", f"{ms:.0f} ms", ms)
        return CheckResult(name, "healthy", f"Reachable ({ms:.0f} ms)", f"{ms:.0f} ms", ms)
    except Exception as e:
        return CheckResult(name, "critical", f"Unreachable: {str(e)[:80]}", "")


def _check_background_jobs() -> CheckResult:
    """Check for long-running or stuck background jobs via DB."""
    name = "Background Jobs"
    try:
        from database import get_db_session
        from database.models import BackupRecord, IntegrityScan
        with get_db_session() as s:
            # Stuck backup jobs (RUNNING for > 2h)
            cutoff = datetime.utcnow() - timedelta(hours=2)
            stuck_backup = (s.query(BackupRecord)
                            .filter(BackupRecord.status == "RUNNING",
                                    BackupRecord.created_at < cutoff).count())
            stuck_scan = (s.query(IntegrityScan)
                          .filter(IntegrityScan.completed_at.is_(None),
                                  IntegrityScan.started_at < cutoff).count())
        if stuck_backup or stuck_scan:
            return CheckResult(name, "warning",
                               f"Stuck jobs: {stuck_backup} backup(s), {stuck_scan} scan(s)",
                               f"{stuck_backup + stuck_scan} stuck")
        return CheckResult(name, "healthy", "No stuck jobs detected.", "OK")
    except Exception as e:
        return CheckResult(name, "warning", f"Check failed: {str(e)[:80]}", "")


# ──────────────────────────────────────────────────────────────────────────
# Scan runners
# ──────────────────────────────────────────────────────────────────────────

_FULL_CHECKS = [
    _check_telegram_api,
    _check_database,
    _check_database_pool,
    _check_orders,
    _check_wallet,
    _check_referral,
    _check_scheduler,
    _check_nowpayments,
    _check_binance_pay,
    _check_bybit_pay,
    _check_storage,
    _check_memory,
    _check_cpu,
    _check_disk,
    _check_background_jobs,
]

_QUICK_CHECKS = [
    _check_telegram_api,
    _check_database,
    _check_orders,
    _check_memory,
    _check_cpu,
    _check_disk,
    _check_background_jobs,
]


def _run_checks(checks: list, job_queue=None) -> list:
    """Run a list of check functions and return CheckResult list."""
    results = []
    for fn in checks:
        try:
            if fn == _check_scheduler:
                result = fn(job_queue=job_queue)
            else:
                result = fn()
        except Exception as e:
            result = CheckResult(
                getattr(fn, "__name__", "unknown").replace("_check_", "").title(),
                "warning",
                f"Check raised exception: {str(e)[:80]}",
            )
        results.append(result)
    return results


def _overall_health(results: list) -> str:
    statuses = {r.status for r in results}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _persist_record(results: list, scan_type: str,
                    triggered_by: str, admin_id: Optional[int]) -> object:
    """Save a DiagnosticsRecord and return it."""
    from database.models import DiagnosticsRecord
    health = _overall_health(results)
    summary = json.dumps([r.to_dict() for r in results], default=str)
    healthy = sum(1 for r in results if r.status == "healthy")
    warnings = sum(1 for r in results if r.status == "warning")
    critical = sum(1 for r in results if r.status == "critical")

    try:
        from database import get_db_session
        with get_db_session() as s:
            rec = DiagnosticsRecord(
                scan_type=scan_type,
                triggered_by=triggered_by,
                admin_id=admin_id,
                started_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
                status="COMPLETED",
                overall_health=health,
                summary=summary,
                total_checks=len(results),
                healthy_count=healthy,
                warning_count=warnings,
                critical_count=critical,
            )
            s.add(rec)
            s.commit()
            s.refresh(rec)
            return rec
    except Exception:
        logger.exception("diagnostics: failed to persist record")
        return None


def run_full_scan(admin_id: Optional[int] = None,
                  triggered_by: str = "manual",
                  job_queue=None) -> tuple:
    """Run full diagnostics scan.

    Returns (DiagnosticsRecord, list[CheckResult]).
    """
    results = _run_checks(_FULL_CHECKS, job_queue=job_queue)
    rec = _persist_record(results, "full", triggered_by, admin_id)
    return rec, results


def run_quick_scan(admin_id: Optional[int] = None,
                   triggered_by: str = "manual",
                   job_queue=None) -> tuple:
    """Run quick diagnostics scan.

    Returns (DiagnosticsRecord, list[CheckResult]).
    """
    results = _run_checks(_QUICK_CHECKS, job_queue=job_queue)
    rec = _persist_record(results, "quick", triggered_by, admin_id)
    return rec, results


def load_scan_results(record_id: int) -> tuple:
    """Load a previously-saved DiagnosticsRecord and parse its results.

    Returns (DiagnosticsRecord, list[dict]) or (None, []).
    """
    from database.models import DiagnosticsRecord
    from database import get_db_session
    try:
        with get_db_session() as s:
            rec = s.get(DiagnosticsRecord, record_id)
            if not rec:
                return None, []
            summary_json = rec.summary or "[]"
            results = json.loads(summary_json)
            return rec, results
    except Exception:
        return None, []


def get_latest_record() -> Optional[object]:
    """Return the most recent DiagnosticsRecord or None."""
    from database.models import DiagnosticsRecord
    from database import get_db_session
    try:
        with get_db_session() as s:
            return (s.query(DiagnosticsRecord)
                    .order_by(DiagnosticsRecord.started_at.desc()).first())
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Cache management (simple in-process dict clear)
# ──────────────────────────────────────────────────────────────────────────

def clear_all_caches() -> list:
    """Clear all known in-process caches. Returns list of cleared cache names."""
    cleared = []
    try:
        from utils.bot_config import cfg
        cfg._cache.clear()
        cleared.append("bot_config")
    except Exception:
        pass
    try:
        from services import social_proof
        social_proof._cache.clear()   # type: ignore[attr-defined]
        cleared.append("social_proof")
    except Exception:
        pass
    return cleared


# ──────────────────────────────────────────────────────────────────────────
# Log export helper
# ──────────────────────────────────────────────────────────────────────────

def collect_recent_logs(lines: int = 200) -> str:
    """Return the last N lines from the application log file, if accessible."""
    log_candidates = [
        "/var/log/telegram-store/app.log",
        "/tmp/telegram-store.log",
        "logs/app.log",
        "app.log",
    ]
    for path in log_candidates:
        p = Path(path)
        if p.exists():
            try:
                all_lines = p.read_text(errors="replace").splitlines()
                return "\n".join(all_lines[-lines:])
            except Exception:
                continue
    return "(No log file found at known paths.)"


def collect_error_logs(lines: int = 100) -> str:
    """Return recent ERROR/CRITICAL lines from the application log file."""
    raw = collect_recent_logs(lines=2000)
    error_lines = [
        l for l in raw.splitlines()
        if any(kw in l for kw in ("ERROR", "CRITICAL", "EXCEPTION", "Traceback"))
    ]
    if not error_lines:
        return "(No ERROR/CRITICAL lines found in recent logs.)"
    return "\n".join(error_lines[-lines:])
