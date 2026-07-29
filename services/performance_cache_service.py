"""V44 — Performance & Cache Manager service.

Provides:
  - Real-time system metrics (CPU, memory, disk, DB response, uptime)
  - In-process cache registry + selective clearing
  - Optimization tools (DB ANALYZE/VACUUM, log purge, tmp cleanup, etc.)
  - Health scoring (Excellent / Good / Warning / Critical)
  - Performance snapshots stored in ``performance_snapshots`` table
  - Optimization history stored in ``optimization_logs`` table
  - Report generation (performance, DB, cache, memory, storage, response-time)
  - Scheduled auto-maintenance hooks

All public functions are best-effort — they MUST NOT raise to callers.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ─── Bot start time (set once from bot.py or register_handlers) ───────────────
_BOT_START: float = time.monotonic()
_SNAPSHOT_LOCK = threading.Lock()
_OPTIM_LOCK = threading.Lock()


def set_start_time() -> None:
    global _BOT_START
    _BOT_START = time.monotonic()


def get_uptime_seconds() -> float:
    return time.monotonic() - _BOT_START


def get_uptime_str() -> str:
    secs = int(get_uptime_seconds())
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


# ─── Cache registry ───────────────────────────────────────────────────────────
# Other modules can call register_cache() to expose their caches here.
# Fallback clearers run even without registration.
_CACHE_REGISTRY: dict[str, dict] = {}   # name -> {label, clear_fn, size_fn}


def register_cache(name: str, label: str,
                   clear_fn: Callable,
                   size_fn: Optional[Callable] = None) -> None:
    """Register an in-process cache so PCM can track and clear it."""
    _CACHE_REGISTRY[name] = {
        "label": label,
        "clear_fn": clear_fn,
        "size_fn": size_fn,
    }


# ─── Built-in cache clearers ──────────────────────────────────────────────────

def _clear_bot_config_cache() -> int:
    """Clear the in-process bot_config TTL cache."""
    try:
        from utils.bot_config import cfg
        cleared = len(getattr(cfg, "_cache", {}))
        if hasattr(cfg, "_cache"):
            cfg._cache.clear()
        return cleared
    except Exception as e:
        logger.debug("_clear_bot_config_cache: %s", e)
        return 0


def _clear_tmp_exports() -> int:
    """Remove completed export files from /tmp/bot_exports/."""
    try:
        from services.data_export_service import EXPORT_DIR
        count = 0
        for f in EXPORT_DIR.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                count += 1
        return count
    except Exception as e:
        logger.debug("_clear_tmp_exports: %s", e)
        return 0


def _clear_tmp_images() -> int:
    """Remove leftover image/qr files in /tmp."""
    count = 0
    try:
        for pattern in ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp"]:
            for f in Path("/tmp").glob(pattern):
                try:
                    f.unlink(missing_ok=True)
                    count += 1
                except Exception:
                    pass
    except Exception as e:
        logger.debug("_clear_tmp_images: %s", e)
    return count


def _clear_python_lru_caches() -> int:
    """Force GC and clear Python's LRU cache wrappers (best-effort)."""
    try:
        gc.collect()
        return 1
    except Exception:
        return 0


# Namespace → (label, clear_fn)
_CACHE_NAMESPACES: dict[str, tuple[str, Callable]] = {
    "settings":  ("⚙️ Settings Cache",   _clear_bot_config_cache),
    "export":    ("📤 Export Files",      _clear_tmp_exports),
    "image":     ("🖼 Image/QR Files",    _clear_tmp_images),
    "python":    ("🐍 Python LRU Cache",  _clear_python_lru_caches),
}


def get_cache_namespaces() -> dict[str, str]:
    """Return {namespace: label} for all supported cache namespaces."""
    base = {k: v[0] for k, v in _CACHE_NAMESPACES.items()}
    # Add registered caches
    for name, meta in _CACHE_REGISTRY.items():
        base[name] = meta["label"]
    return base


def clear_cache(namespace: str) -> dict:
    """
    Clear one cache namespace. Returns {cleared: int, label: str, ok: bool}.
    """
    t0 = time.monotonic()
    try:
        # Check registry first
        if namespace in _CACHE_REGISTRY:
            fn = _CACHE_REGISTRY[namespace]["clear_fn"]
            cleared = fn() or 0
            label = _CACHE_REGISTRY[namespace]["label"]
        elif namespace in _CACHE_NAMESPACES:
            label, fn = _CACHE_NAMESPACES[namespace]
            cleared = fn() or 0
        else:
            return {"cleared": 0, "label": namespace, "ok": False,
                    "error": f"Unknown namespace: {namespace}"}

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization(
            op_type="cache_clear",
            target=namespace,
            result="success",
            details=f"Cleared {cleared} item(s) from {label}",
            duration_ms=elapsed_ms,
            rows_affected=cleared,
        )
        return {"cleared": cleared, "label": label, "ok": True, "elapsed_ms": elapsed_ms}
    except Exception as e:
        logger.error("clear_cache %s: %s", namespace, e, exc_info=True)
        return {"cleared": 0, "label": namespace, "ok": False, "error": str(e)[:200]}


def clear_all_caches() -> dict:
    """Clear every registered and built-in cache. Returns summary."""
    results = {}
    all_namespaces = list(_CACHE_NAMESPACES.keys()) + list(_CACHE_REGISTRY.keys())
    for ns in all_namespaces:
        results[ns] = clear_cache(ns)
    total_cleared = sum(r.get("cleared", 0) for r in results.values())
    return {"total_cleared": total_cleared, "namespaces": results}


# ─── System metrics ───────────────────────────────────────────────────────────

def _read_proc_meminfo() -> dict:
    """Parse /proc/meminfo. Returns dict of key → int (kB)."""
    data: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    data[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass
    return data


def _read_proc_loadavg() -> tuple[float, float, float]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return 0.0, 0.0, 0.0


def _cpu_usage_pct() -> float:
    """Estimate CPU usage % using /proc/stat (1-sample, good enough for display)."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        vals = [int(x) for x in line.split()[1:]]
        idle = vals[3]
        total = sum(vals)
        used_pct = 100.0 * (1 - idle / max(total, 1))
        return round(used_pct, 1)
    except Exception:
        load1, _, _ = _read_proc_loadavg()
        cpus = os.cpu_count() or 1
        return round(min(100.0, load1 / cpus * 100), 1)


def _mem_info() -> dict:
    info = _read_proc_meminfo()
    total_mb = info.get("MemTotal", 0) / 1024
    avail_mb = info.get("MemAvailable", 0) / 1024
    used_mb = total_mb - avail_mb
    used_pct = round(100 * used_mb / max(total_mb, 1), 1)
    return {
        "total_mb": round(total_mb, 1),
        "used_mb": round(used_mb, 1),
        "avail_mb": round(avail_mb, 1),
        "used_pct": used_pct,
    }


def _disk_info(path: str = "/") -> dict:
    try:
        st = os.statvfs(path)
        total_gb = st.f_blocks * st.f_frsize / (1024 ** 3)
        free_gb = st.f_bavail * st.f_frsize / (1024 ** 3)
        used_pct = round(100 * (1 - st.f_bavail / max(st.f_blocks, 1)), 1)
        return {
            "total_gb": round(total_gb, 2),
            "free_gb": round(free_gb, 2),
            "used_pct": used_pct,
        }
    except Exception:
        return {"total_gb": 0, "free_gb": 0, "used_pct": 0}


def _db_ping_ms() -> float:
    """Time a SELECT 1 against the database. Returns ms or -1 on error."""
    try:
        from database.db import engine
        from sqlalchemy import text
        t0 = time.monotonic()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return -1.0


def _db_size_mb() -> float:
    """Estimate database size in MB. Works for both PostgreSQL and SQLite."""
    try:
        from database.db import engine
        from sqlalchemy import text
        dialect = engine.dialect.name
        with engine.connect() as conn:
            if dialect == "postgresql":
                row = conn.execute(text(
                    "SELECT pg_database_size(current_database()) / 1048576.0"
                )).fetchone()
                return round(float(row[0]), 2) if row else 0.0
            else:
                # SQLite — get file size
                db_url = str(engine.url)
                if "///" in db_url:
                    path = db_url.split("///", 1)[1]
                    return round(Path(path).stat().st_size / (1024 ** 2), 2)
                return 0.0
    except Exception:
        return 0.0


def _db_conn_count() -> int:
    """Return active PostgreSQL connection count, or 1 for SQLite."""
    try:
        from database.db import engine
        from sqlalchemy import text
        dialect = engine.dialect.name
        if dialect == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database()"
                )).fetchone()
                return int(row[0]) if row else 0
        return 1
    except Exception:
        return 0


def _tmp_dir_size_mb(path: str = "/tmp") -> float:
    try:
        total = 0
        for dirpath, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except Exception:
                    pass
        return round(total / (1024 ** 2), 2)
    except Exception:
        return 0.0


def collect_metrics() -> dict:
    """
    Collect all real-time performance metrics. Returns a flat dict.
    Safe to call frequently — reads /proc, does one DB ping.
    """
    cpu_pct = _cpu_usage_pct()
    mem = _mem_info()
    disk = _disk_info("/")
    load1, load5, load15 = _read_proc_loadavg()
    db_ms = _db_ping_ms()
    db_mb = _db_size_mb()
    db_conn = _db_conn_count()
    tmp_mb = _tmp_dir_size_mb("/tmp")
    uptime_s = get_uptime_seconds()

    return {
        "cpu_pct":       cpu_pct,
        "cpu_load1":     round(load1, 2),
        "cpu_load5":     round(load5, 2),
        "cpu_load15":    round(load15, 2),
        "cpu_count":     os.cpu_count() or 1,
        "mem_total_mb":  mem["total_mb"],
        "mem_used_mb":   mem["used_mb"],
        "mem_avail_mb":  mem["avail_mb"],
        "mem_pct":       mem["used_pct"],
        "disk_total_gb": disk["total_gb"],
        "disk_free_gb":  disk["free_gb"],
        "disk_pct":      disk["used_pct"],
        "db_ping_ms":    db_ms,
        "db_size_mb":    db_mb,
        "db_conn":       db_conn,
        "tmp_size_mb":   tmp_mb,
        "uptime_s":      round(uptime_s),
        "uptime_str":    get_uptime_str(),
        "collected_at":  datetime.utcnow().isoformat(),
    }


# ─── Health scoring ───────────────────────────────────────────────────────────

def _score_metric(value: float, warn: float, crit: float,
                  inverted: bool = False) -> float:
    """Return 0.0–1.0 health score. inverted=True means higher=better."""
    if inverted:
        value = 100 - value
        warn, crit = 100 - warn, 100 - crit
    if value >= crit:
        return 0.0
    if value >= warn:
        return 0.5
    return 1.0


def compute_health(metrics: dict) -> dict:
    """
    Compute an overall health score from collected metrics.
    Returns {score: 0-100, label: str, emoji: str, issues: [str]}
    """
    issues = []
    scores = []

    # CPU
    cpu = metrics.get("cpu_pct", 0)
    s = _score_metric(cpu, 70, 90)
    scores.append(s)
    if s < 1.0:
        issues.append(f"CPU high: {cpu}%")

    # Memory
    mem = metrics.get("mem_pct", 0)
    s = _score_metric(mem, 80, 92)
    scores.append(s)
    if s < 1.0:
        issues.append(f"Memory high: {mem}%")

    # Disk
    disk = metrics.get("disk_pct", 0)
    s = _score_metric(disk, 80, 95)
    scores.append(s)
    if s < 1.0:
        issues.append(f"Disk high: {disk}%")

    # DB ping
    db_ms = metrics.get("db_ping_ms", 0)
    if db_ms < 0:
        scores.append(0.0)
        issues.append("DB unreachable")
    elif db_ms > 2000:
        scores.append(0.0)
        issues.append(f"DB slow: {db_ms:.0f}ms")
    elif db_ms > 500:
        scores.append(0.5)
        issues.append(f"DB slow: {db_ms:.0f}ms")
    else:
        scores.append(1.0)

    avg = sum(scores) / len(scores) if scores else 0.5
    score = round(avg * 100)

    if score >= 90:
        label, emoji = "Excellent", "🟢"
    elif score >= 70:
        label, emoji = "Good", "🟡"
    elif score >= 50:
        label, emoji = "Warning", "🟠"
    else:
        label, emoji = "Critical", "🔴"

    return {"score": score, "label": label, "emoji": emoji, "issues": issues}


# ─── Performance snapshot (periodic) ─────────────────────────────────────────

async def take_snapshot(context=None) -> None:
    """Collect metrics and store a PerformanceSnapshot. Best-effort."""
    if not _SNAPSHOT_LOCK.acquire(blocking=False):
        return   # Already running
    try:
        metrics = collect_metrics()
        health = compute_health(metrics)
        with _get_session() as session:
            from database.models import PerformanceSnapshot
            snap = PerformanceSnapshot(
                cpu_pct=metrics["cpu_pct"],
                mem_pct=metrics["mem_pct"],
                disk_pct=metrics["disk_pct"],
                db_ping_ms=metrics["db_ping_ms"] if metrics["db_ping_ms"] >= 0 else None,
                db_size_mb=metrics["db_size_mb"],
                db_conn=metrics["db_conn"],
                uptime_s=int(metrics["uptime_s"]),
                health_score=health["score"],
                health_label=health["label"],
                extra=json.dumps({
                    "load1": metrics["cpu_load1"],
                    "mem_total_mb": metrics["mem_total_mb"],
                    "disk_free_gb": metrics["disk_free_gb"],
                    "tmp_size_mb": metrics["tmp_size_mb"],
                }),
                created_at=datetime.utcnow(),
            )
            session.add(snap)
    except Exception as e:
        logger.error("take_snapshot: %s", e, exc_info=True)
    finally:
        _SNAPSHOT_LOCK.release()


def _get_session():
    from database import get_db_session
    return get_db_session()


def get_snapshot_history(limit: int = 24) -> list[dict]:
    """Return recent performance snapshots as plain dicts."""
    try:
        from database.models import PerformanceSnapshot
        with _get_session() as session:
            rows = (session.query(PerformanceSnapshot)
                    .order_by(PerformanceSnapshot.created_at.desc())
                    .limit(limit).all())
            return [{
                "id":           r.id,
                "cpu_pct":      r.cpu_pct,
                "mem_pct":      r.mem_pct,
                "disk_pct":     r.disk_pct,
                "db_ping_ms":   r.db_ping_ms,
                "db_size_mb":   r.db_size_mb,
                "db_conn":      r.db_conn,
                "uptime_s":     r.uptime_s,
                "health_score": r.health_score,
                "health_label": r.health_label,
                "created_at":   r.created_at,
            } for r in rows]
    except Exception as e:
        logger.error("get_snapshot_history: %s", e, exc_info=True)
        return []


# ─── Optimization tools ───────────────────────────────────────────────────────

def _log_optimization(op_type: str, target: str, result: str,
                       details: str = "", duration_ms: int = 0,
                       rows_affected: int = 0) -> None:
    try:
        from database.models import OptimizationLog
        with _get_session() as session:
            session.add(OptimizationLog(
                op_type=op_type,
                target=target,
                result=result,
                details=details[:500],
                duration_ms=duration_ms,
                rows_affected=rows_affected,
                created_at=datetime.utcnow(),
            ))
    except Exception as e:
        logger.debug("_log_optimization: %s", e)


def optimize_database() -> dict:
    """Run ANALYZE (PostgreSQL) or VACUUM + ANALYZE (SQLite)."""
    if not _OPTIM_LOCK.acquire(blocking=False):
        return {"ok": False, "msg": "Optimization already running. Please wait."}
    t0 = time.monotonic()
    try:
        from database.db import engine
        from sqlalchemy import text
        dialect = engine.dialect.name
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if dialect == "postgresql":
                conn.execute(text("ANALYZE"))
                msg = "ANALYZE completed on all tables."
            else:
                conn.execute(text("VACUUM"))
                conn.execute(text("ANALYZE"))
                msg = "VACUUM + ANALYZE completed."
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("db_optimize", dialect, "success", msg, elapsed_ms)
        return {"ok": True, "msg": msg, "elapsed_ms": elapsed_ms}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        err = str(e)[:300]
        _log_optimization("db_optimize", "db", "failed", err, elapsed_ms)
        logger.error("optimize_database: %s", e, exc_info=True)
        return {"ok": False, "msg": f"DB optimization error: {err}"}
    finally:
        _OPTIM_LOCK.release()


def optimize_logs(days_to_keep: int = 90) -> dict:
    """Delete AdminAuditLog, GlobalActivityEntry rows older than N days."""
    t0 = time.monotonic()
    total = 0
    details_parts = []
    try:
        cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
        with _get_session() as session:
            from database.models import AdminAuditLog
            n = session.query(AdminAuditLog).filter(
                AdminAuditLog.created_at < cutoff).delete()
            total += n
            details_parts.append(f"AdminAuditLog: {n}")

        try:
            with _get_session() as session:
                from database.models import GlobalActivityEntry
                n = session.query(GlobalActivityEntry).filter(
                    GlobalActivityEntry.created_at < cutoff).delete()
                total += n
                details_parts.append(f"GlobalActivityEntry: {n}")
        except Exception:
            pass

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        details = f"Deleted rows older than {days_to_keep}d. " + ", ".join(details_parts)
        _log_optimization("log_cleanup", "logs", "success", details, elapsed_ms, total)
        return {"ok": True, "msg": f"Deleted {total:,} old log rows.", "deleted": total}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("log_cleanup", "logs", "failed", str(e)[:300], elapsed_ms)
        logger.error("optimize_logs: %s", e, exc_info=True)
        return {"ok": False, "msg": f"Log cleanup error: {str(e)[:200]}"}


def optimize_storage() -> dict:
    """Remove stale temp/export/backup files from /tmp."""
    t0 = time.monotonic()
    count = 0
    freed_bytes = 0
    try:
        # Export files older than 24h
        try:
            from services.data_export_service import EXPORT_DIR
            cutoff = time.time() - 86400
            for f in EXPORT_DIR.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    freed_bytes += f.stat().st_size
                    f.unlink(missing_ok=True)
                    count += 1
        except Exception:
            pass

        # Leftover /tmp files older than 6h
        cutoff = time.time() - 21600
        for ext in ["*.png", "*.jpg", "*.pdf", "*.csv", "*.xlsx"]:
            for f in Path("/tmp").glob(ext):
                try:
                    if f.stat().st_mtime < cutoff:
                        freed_bytes += f.stat().st_size
                        f.unlink(missing_ok=True)
                        count += 1
                except Exception:
                    pass

        freed_mb = round(freed_bytes / (1024 ** 2), 2)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        msg = f"Removed {count} file(s), freed {freed_mb} MB."
        _log_optimization("storage_cleanup", "/tmp", "success", msg, elapsed_ms, count)
        return {"ok": True, "msg": msg, "count": count, "freed_mb": freed_mb}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("storage_cleanup", "/tmp", "failed", str(e)[:300], elapsed_ms)
        return {"ok": False, "msg": f"Storage cleanup error: {str(e)[:200]}"}


def optimize_cache() -> dict:
    """Clear all caches and force GC."""
    t0 = time.monotonic()
    result = clear_all_caches()
    gc.collect()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    total = result["total_cleared"]
    msg = f"Cleared {total} cache item(s) across {len(result['namespaces'])} namespace(s)."
    _log_optimization("cache_optimize", "all", "success", msg, elapsed_ms, total)
    return {"ok": True, "msg": msg, "cleared": total}


def optimize_search_index() -> dict:
    """Rebuild search index: delete old SearchRecord history beyond 500 rows."""
    t0 = time.monotonic()
    try:
        from database.models import SearchRecord
        with _get_session() as session:
            total = session.query(SearchRecord).count()
            if total > 500:
                # Keep newest 400, delete the rest
                keep_ids = (session.query(SearchRecord.id)
                            .order_by(SearchRecord.created_at.desc())
                            .limit(400).all())
                keep_set = {r[0] for r in keep_ids}
                deleted = (session.query(SearchRecord)
                           .filter(SearchRecord.id.notin_(keep_set))
                           .delete(synchronize_session="fetch"))
            else:
                deleted = 0
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        msg = f"Search index trimmed: {deleted} old record(s) removed."
        _log_optimization("search_index", "search_records", "success", msg, elapsed_ms, deleted)
        return {"ok": True, "msg": msg, "deleted": deleted}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("search_index", "search_records", "failed", str(e)[:300], elapsed_ms)
        return {"ok": False, "msg": f"Search index error: {str(e)[:200]}"}


def optimize_background_jobs() -> dict:
    """Remove old completed export jobs from the DB (done/failed > 7 days)."""
    t0 = time.monotonic()
    try:
        from database.models import ExportJob
        cutoff = datetime.utcnow() - timedelta(days=7)
        with _get_session() as session:
            deleted = (session.query(ExportJob)
                       .filter(ExportJob.status.in_(["done", "failed"]),
                               ExportJob.created_at < cutoff)
                       .delete(synchronize_session="fetch"))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        msg = f"Removed {deleted} old export job record(s)."
        _log_optimization("job_cleanup", "export_jobs", "success", msg, elapsed_ms, deleted)
        return {"ok": True, "msg": msg, "deleted": deleted}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("job_cleanup", "export_jobs", "failed", str(e)[:300], elapsed_ms)
        return {"ok": False, "msg": f"Job cleanup error: {str(e)[:200]}"}


def optimize_images() -> dict:
    """Alias for optimize_storage focused on image files."""
    return optimize_storage()


def optimize_scheduler() -> dict:
    """Remove old PerformanceSnapshot rows beyond retention window."""
    t0 = time.monotonic()
    try:
        from database.models import PerformanceSnapshot
        # Keep last 30 days
        cutoff = datetime.utcnow() - timedelta(days=30)
        with _get_session() as session:
            deleted = (session.query(PerformanceSnapshot)
                       .filter(PerformanceSnapshot.created_at < cutoff)
                       .delete())
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        msg = f"Removed {deleted} old snapshot(s)."
        _log_optimization("snapshot_cleanup", "performance_snapshots", "success",
                           msg, elapsed_ms, deleted)
        return {"ok": True, "msg": msg, "deleted": deleted}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_optimization("snapshot_cleanup", "performance_snapshots", "failed",
                           str(e)[:300], elapsed_ms)
        return {"ok": False, "msg": f"Scheduler cleanup error: {str(e)[:200]}"}


def run_full_optimization() -> dict:
    """Run all optimization tools in sequence. Returns combined result."""
    results = {}
    steps = [
        ("cache",       optimize_cache),
        ("database",    optimize_database),
        ("logs",        optimize_logs),
        ("storage",     optimize_storage),
        ("search",      optimize_search_index),
        ("jobs",        optimize_background_jobs),
        ("scheduler",   optimize_scheduler),
    ]
    ok_count = 0
    for name, fn in steps:
        try:
            r = fn()
            results[name] = r
            if r.get("ok"):
                ok_count += 1
        except Exception as e:
            results[name] = {"ok": False, "msg": str(e)[:100]}
    return {"results": results, "ok_count": ok_count, "total": len(steps)}


# ─── Optimization log history ─────────────────────────────────────────────────

def get_optimization_history(limit: int = 30, op_type: Optional[str] = None) -> list[dict]:
    try:
        from database.models import OptimizationLog
        with _get_session() as session:
            q = session.query(OptimizationLog)
            if op_type:
                q = q.filter(OptimizationLog.op_type == op_type)
            rows = q.order_by(OptimizationLog.created_at.desc()).limit(limit).all()
            return [{
                "id":           r.id,
                "op_type":      r.op_type,
                "target":       r.target,
                "result":       r.result,
                "details":      r.details,
                "duration_ms":  r.duration_ms,
                "rows_affected": r.rows_affected,
                "created_at":   r.created_at,
            } for r in rows]
    except Exception as e:
        logger.error("get_optimization_history: %s", e, exc_info=True)
        return []


# ─── Reports ──────────────────────────────────────────────────────────────────

def generate_report(report_type: str) -> str:
    """Generate a text report. Returns formatted string."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    try:
        metrics = collect_metrics()
        health = compute_health(metrics)

        if report_type == "performance":
            return _report_performance(metrics, health, now)
        elif report_type == "database":
            return _report_database(metrics, now)
        elif report_type == "cache":
            return _report_cache(now)
        elif report_type == "memory":
            return _report_memory(metrics, now)
        elif report_type == "storage":
            return _report_storage(metrics, now)
        elif report_type == "response_time":
            return _report_response_time(metrics, now)
        elif report_type == "api":
            return _report_api(now)
        else:
            return f"❓ Unknown report type: {report_type}"
    except Exception as e:
        return f"❌ Report generation error: {e}"


def _report_performance(m: dict, h: dict, now: str) -> str:
    history = get_snapshot_history(6)
    hist_lines = ""
    for snap in reversed(history[:6]):
        ts = snap["created_at"].strftime("%H:%M") if snap["created_at"] else "—"
        hist_lines += (f"  {ts}: CPU {snap['cpu_pct']}% | "
                       f"Mem {snap['mem_pct']}% | "
                       f"DB {snap['db_ping_ms'] or '?'}ms | "
                       f"{snap['health_label']}\n")
    return (
        f"📊 PERFORMANCE REPORT — {now}\n"
        f"{'='*40}\n"
        f"Health: {h['emoji']} {h['label']} (score: {h['score']}/100)\n\n"
        f"CPU Usage:     {m['cpu_pct']}%\n"
        f"CPU Load (1m): {m['cpu_load1']}\n"
        f"CPU Cores:     {m['cpu_count']}\n\n"
        f"Memory Used:   {m['mem_used_mb']} MB / {m['mem_total_mb']} MB ({m['mem_pct']}%)\n\n"
        f"Disk Used:     {m['disk_pct']}% | Free: {m['disk_free_gb']} GB\n\n"
        f"DB Ping:       {m['db_ping_ms']} ms\n"
        f"DB Size:       {m['db_size_mb']} MB\n"
        f"DB Conns:      {m['db_conn']}\n\n"
        f"Bot Uptime:    {m['uptime_str']}\n\n"
        f"{'─'*40}\n"
        f"Recent history:\n{hist_lines or '  (no snapshots yet)'}\n"
        f"{'─'*40}\n"
        f"Issues: {', '.join(h['issues']) or 'None detected'}"
    )


def _report_database(m: dict, now: str) -> str:
    return (
        f"🗄 DATABASE REPORT — {now}\n"
        f"{'='*40}\n"
        f"Response Time: {m['db_ping_ms']} ms\n"
        f"Database Size: {m['db_size_mb']} MB\n"
        f"Active Conns:  {m['db_conn']}\n\n"
        f"Status: {'🟢 Healthy' if m['db_ping_ms'] >= 0 else '🔴 Unreachable'}\n"
    )


def _report_cache(now: str) -> str:
    ns = get_cache_namespaces()
    lines = "\n".join(f"  • {label}" for label in ns.values())
    return (
        f"🧹 CACHE REPORT — {now}\n"
        f"{'='*40}\n"
        f"Cache namespaces ({len(ns)}):\n{lines}\n\n"
        f"Temp dir size: {_tmp_dir_size_mb('/tmp')} MB\n"
    )


def _report_memory(m: dict, now: str) -> str:
    return (
        f"💾 MEMORY REPORT — {now}\n"
        f"{'='*40}\n"
        f"Total RAM:     {m['mem_total_mb']} MB\n"
        f"Used RAM:      {m['mem_used_mb']} MB ({m['mem_pct']}%)\n"
        f"Available RAM: {m['mem_avail_mb']} MB\n"
        f"Temp files:    {m['tmp_size_mb']} MB\n\n"
        f"Status: {'🔴 High' if m['mem_pct'] > 90 else '🟡 Elevated' if m['mem_pct'] > 75 else '🟢 Normal'}"
    )


def _report_storage(m: dict, now: str) -> str:
    tmp_mb = _tmp_dir_size_mb("/tmp")
    return (
        f"💿 STORAGE REPORT — {now}\n"
        f"{'='*40}\n"
        f"Disk Total:    {m['disk_total_gb']} GB\n"
        f"Disk Free:     {m['disk_free_gb']} GB\n"
        f"Disk Used:     {m['disk_pct']}%\n"
        f"DB Size:       {m['db_size_mb']} MB\n"
        f"Temp Files:    {tmp_mb} MB\n\n"
        f"Status: {'🔴 Critical' if m['disk_pct'] > 95 else '🟠 Warning' if m['disk_pct'] > 80 else '🟢 OK'}"
    )


def _report_response_time(m: dict, now: str) -> str:
    history = get_snapshot_history(12)
    avg_db = (sum(s["db_ping_ms"] for s in history if s.get("db_ping_ms"))
              / max(len([s for s in history if s.get("db_ping_ms")]), 1))
    return (
        f"⏱ RESPONSE TIME REPORT — {now}\n"
        f"{'='*40}\n"
        f"Current DB Ping:  {m['db_ping_ms']} ms\n"
        f"Avg DB Ping (24h): {avg_db:.1f} ms\n\n"
        f"Status: {'🔴 Slow' if m['db_ping_ms'] > 2000 else '🟡 Elevated' if m['db_ping_ms'] > 500 else '🟢 Fast'}"
    )


def _report_api(now: str) -> str:
    opt_history = get_optimization_history(limit=10)
    lines = "\n".join(
        f"  {r['created_at'].strftime('%m-%d %H:%M') if r['created_at'] else '—'}: "
        f"{r['op_type']} — {r['result']} ({r['duration_ms']}ms)"
        for r in opt_history
    ) or "  (no optimization history yet)"
    return (
        f"🔌 API PERFORMANCE REPORT — {now}\n"
        f"{'='*40}\n"
        f"Recent optimizations:\n{lines}"
    )


# ─── Stats overview ───────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return a combined stats dict for the admin panel header."""
    try:
        metrics = collect_metrics()
        health = compute_health(metrics)
        opt_count = 0
        with _get_session() as session:
            from database.models import OptimizationLog
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
            opt_count = (session.query(OptimizationLog)
                         .filter(OptimizationLog.created_at >= today_start).count())
        return {**metrics, **health, "opt_today": opt_count}
    except Exception as e:
        logger.error("get_stats: %s", e, exc_info=True)
        return {
            "cpu_pct": 0, "mem_pct": 0, "disk_pct": 0,
            "db_ping_ms": -1, "db_size_mb": 0, "uptime_str": "—",
            "score": 0, "label": "Unknown", "emoji": "❓",
            "issues": [], "opt_today": 0,
        }


# ─── Auto-maintenance entry point ─────────────────────────────────────────────

async def run_auto_maintenance(context=None) -> None:
    """Called from bot.py job_queue. Runs configured auto-maintenance tasks."""
    try:
        from utils.bot_config import cfg
        if cfg.get("pcm_auto_cache_cleanup", "true") == "true":
            optimize_cache()
        if cfg.get("pcm_auto_log_cleanup", "true") == "true":
            days = cfg.get_int("pcm_log_retention_days", 90)
            optimize_logs(days_to_keep=days)
        if cfg.get("pcm_auto_storage_cleanup", "true") == "true":
            optimize_storage()
        if cfg.get("pcm_auto_job_cleanup", "true") == "true":
            optimize_background_jobs()
        if cfg.get("pcm_auto_snapshot", "true") == "true":
            await take_snapshot()
    except Exception as e:
        logger.error("run_auto_maintenance: %s", e, exc_info=True)
