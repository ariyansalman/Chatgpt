"""PostgreSQL backup service using pg_dump.

Local backups only. Cloud upload is not implemented — VPS operators must
arrange offsite copies themselves. Never deletes the newest successful
backup during retention cleanup.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import get_db_session
from database.models import BackupRecord
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/telegram-store"))


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    return dsn


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _safe_error(msg: str) -> str:
    """Strip DSN passwords from any error text before persisting."""
    return re.sub(r"://[^:@/]+:[^@/]+@", "://***:***@", msg or "")[:500]


def run_backup(triggered_by: str = "schedule",
               admin_id: Optional[int] = None) -> BackupRecord:
    """Run pg_dump; persist BackupRecord regardless of outcome."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dsn = _dsn()
    if not dsn.startswith(("postgres://", "postgresql://")):
        # SQLite (local dev) / other — no-op record so admins see the reason
        reason = "DATABASE_URL is not PostgreSQL — pg_dump unavailable"
        with get_db_session() as s:
            rec = BackupRecord(
                filename="", method="pg_dump",
                status="FAILED",
                error_summary=reason,
                triggered_by=triggered_by, admin_id=admin_id,
                created_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
            )
            s.add(rec); s.commit(); s.refresh(rec)
            return rec

    fname = f"pgdump_{_timestamp()}.sql.gz"
    fpath = BACKUP_DIR / fname
    with get_db_session() as s:
        rec = BackupRecord(
            filename=fname, method="pg_dump",
            status="RUNNING", triggered_by=triggered_by,
            admin_id=admin_id, created_at=datetime.utcnow(),
        )
        s.add(rec); s.commit(); s.refresh(rec)
        rec_id = rec.id

    try:
        # pg_dump reads password from DSN URL directly (PGPASSWORD not needed)
        # Pipe through gzip for a smaller artifact. Avoid shell=True so the
        # DSN (which may contain special shell characters) is never
        # interpreted by a shell — pass argv lists straight to execve.
        with open(fpath, "wb") as out_f:
            dump_proc = subprocess.Popen(
                ["pg_dump", "--format=plain", "--no-owner", "--no-privileges", dsn],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            gzip_proc = subprocess.Popen(
                ["gzip", "-9"], stdin=dump_proc.stdout, stdout=out_f,
                stderr=subprocess.PIPE,
            )
            dump_proc.stdout.close()
            _, gzip_err = gzip_proc.communicate(timeout=3600)
            _, dump_err = dump_proc.communicate(timeout=3600)
        if dump_proc.returncode != 0:
            raise RuntimeError(_safe_error(dump_err.decode("utf-8", "replace")))
        if gzip_proc.returncode != 0:
            raise RuntimeError(_safe_error(gzip_err.decode("utf-8", "replace")))
        size = fpath.stat().st_size if fpath.exists() else 0
        with get_db_session() as s:
            r = s.get(BackupRecord, rec_id)
            r.status = "SUCCESS"
            r.size_bytes = size
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r
    except Exception as e:
        logger.exception("backup failed")
        try:
            if fpath.exists():
                fpath.unlink()
        except Exception:
            pass
        with get_db_session() as s:
            r = s.get(BackupRecord, rec_id)
            r.status = "FAILED"
            r.error_summary = _safe_error(str(e))
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r


def cleanup_retention() -> int:
    """Delete old SUCCESS files/records honouring retention.
    Never deletes the newest successful backup.
    Returns number of files deleted.
    """
    keep = max(1, cfg.get_int("backup_retention_count", 14))
    deleted = 0
    with get_db_session() as s:
        rows = (s.query(BackupRecord)
                 .filter(BackupRecord.status == "SUCCESS")
                 .order_by(BackupRecord.created_at.desc()).all())
        # Always preserve the newest; drop everything past `keep`.
        for r in rows[keep:]:
            fp = BACKUP_DIR / r.filename
            try:
                if fp.exists():
                    fp.unlink()
                    deleted += 1
            except Exception:
                logger.exception("failed to unlink %s", fp)
            r.status = "PRUNED"
        s.commit()
    return deleted
