"""V37 — File & License Key Manager Service.

Manages ManagedFile and ManagedKey records.
Handles bulk import, generate, reserve, recycle, export, deliver operations.

Public API
──────────
Files:
  create_file(filename, file_type, telegram_file_id, ...) → ManagedFile
  archive_file(file_id) → bool
  delete_file(file_id) → bool
  record_download(file_id, user_id, order_id) → bool
  get_file_stats() → dict

Keys:
  add_key(key_type, key_value, product_id, notes, created_by) → ManagedKey|None
  bulk_import_keys(key_type, raw_text, product_id, created_by) → (added, dupes, errors)
  generate_keys(key_type, count, prefix, length, product_id, created_by) → int
  deliver_key(key_id, user_id, order_id, method, admin_id) → bool
  recycle_key(key_id) → bool
  delete_key(key_id) → bool
  bulk_delete_keys(key_type, status) → int
  export_keys(key_type, status) → list[str]
  get_key_stats() → dict
"""
from __future__ import annotations

import hashlib
import logging
import random
import string
from datetime import datetime
from typing import List, Optional, Tuple

from database import get_db_session
from database.models import (
    ManagedFile, ManagedKey, ManagedKeyDelivery, FileDownloadLog,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _is_enabled() -> bool:
    return cfg.get("file_license_manager_status", "enabled") == "enabled"


# ─────────────────────────────────────────────────────────────────────────────
# File operations
# ─────────────────────────────────────────────────────────────────────────────

def create_file(
    filename: str,
    file_type: str,
    telegram_file_id: Optional[str] = None,
    file_size: Optional[int] = None,
    description: Optional[str] = None,
    product_id: Optional[int] = None,
    max_downloads: Optional[int] = None,
    auto_delete_days: Optional[int] = None,
    created_by: Optional[int] = None,
) -> Optional[ManagedFile]:
    """Create and store a ManagedFile record."""
    try:
        with get_db_session() as s:
            mf = ManagedFile(
                filename=filename[:255],
                file_type=file_type[:16],
                telegram_file_id=telegram_file_id,
                file_size=file_size,
                description=description,
                product_id=product_id,
                max_downloads=max_downloads,
                auto_delete_days=auto_delete_days,
                created_by=created_by,
                status="active",
            )
            s.add(mf)
            s.commit()
            s.refresh(mf)
            return mf
    except Exception:
        logger.exception("create_file failed")
        return None


def archive_file(file_id: int) -> bool:
    """Archive a managed file."""
    try:
        with get_db_session() as s:
            f = s.query(ManagedFile).filter_by(id=file_id).first()
            if f:
                f.status = "archived"
                f.updated_at = datetime.utcnow()
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("archive_file failed for id=%s", file_id)
        return False


def delete_file(file_id: int) -> bool:
    """Permanently delete a managed file."""
    try:
        with get_db_session() as s:
            f = s.query(ManagedFile).filter_by(id=file_id).first()
            if f:
                s.delete(f)
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("delete_file failed for id=%s", file_id)
        return False


def record_download(
    file_id: int,
    user_id: Optional[int] = None,
    order_id: Optional[int] = None,
) -> bool:
    """Record a file download event and increment the counter."""
    try:
        with get_db_session() as s:
            f = s.query(ManagedFile).filter_by(id=file_id).first()
            if not f:
                return False
            # Check max_downloads limit
            if f.max_downloads and f.download_count >= f.max_downloads:
                return False
            f.download_count += 1
            log = FileDownloadLog(
                file_id=file_id,
                user_id=user_id,
                order_id=order_id,
            )
            s.add(log)
            s.commit()
            return True
    except Exception:
        logger.exception("record_download failed for file_id=%s", file_id)
        return False


def get_file_stats() -> dict:
    """Return aggregate file manager statistics."""
    try:
        with get_db_session() as s:
            total    = s.query(ManagedFile).count()
            active   = s.query(ManagedFile).filter_by(status="active").count()
            archived = s.query(ManagedFile).filter_by(status="archived").count()
            expired  = s.query(ManagedFile).filter_by(status="expired").count()
            downloads = s.query(FileDownloadLog).count()
            return {
                "total": total,
                "active": active,
                "archived": archived,
                "expired": expired,
                "total_downloads": downloads,
            }
    except Exception:
        return {"total": 0, "active": 0, "archived": 0, "expired": 0, "total_downloads": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Key operations
# ─────────────────────────────────────────────────────────────────────────────

def add_key(
    key_type: str,
    key_value: str,
    product_id: Optional[int] = None,
    notes: Optional[str] = None,
    created_by: Optional[int] = None,
) -> Optional[ManagedKey]:
    """Add a single key. Returns None if duplicate detected."""
    try:
        fp = _fingerprint(key_value)
        with get_db_session() as s:
            existing = s.query(ManagedKey).filter_by(key_fingerprint=fp).first()
            if existing:
                return None  # duplicate
            mk = ManagedKey(
                key_type=key_type[:32],
                key_value=key_value,
                key_fingerprint=fp,
                product_id=product_id,
                notes=notes,
                created_by=created_by,
                status="unused",
            )
            s.add(mk)
            s.commit()
            s.refresh(mk)
            return mk
    except Exception:
        logger.exception("add_key failed")
        return None


def bulk_import_keys(
    key_type: str,
    raw_text: str,
    product_id: Optional[int] = None,
    created_by: Optional[int] = None,
) -> Tuple[int, int, int]:
    """Parse newline/comma-separated keys and bulk-insert. Returns (added, dupes, errors)."""
    lines = [
        line.strip()
        for line in raw_text.replace(",", "\n").splitlines()
        if line.strip()
    ]
    added = dupes = errors = 0
    for line in lines:
        try:
            result = add_key(key_type, line, product_id=product_id, created_by=created_by)
            if result is None:
                dupes += 1
            else:
                added += 1
        except Exception:
            errors += 1
    return added, dupes, errors


def generate_keys(
    key_type: str,
    count: int,
    prefix: str = "",
    length: int = 16,
    product_id: Optional[int] = None,
    created_by: Optional[int] = None,
) -> int:
    """Generate `count` random alphanumeric keys. Returns number actually added."""
    alphabet = string.ascii_uppercase + string.digits
    added = 0
    attempts = 0
    while added < count and attempts < count * 5:
        attempts += 1
        raw = prefix + "".join(random.choices(alphabet, k=length))
        # Format as XXXX-XXXX-XXXX-XXXX if length >= 16 and no prefix
        if not prefix and length >= 16:
            segments = [raw[i:i+4] for i in range(0, min(len(raw), 16), 4)]
            raw = "-".join(segments)
        result = add_key(key_type, raw, product_id=product_id, created_by=created_by)
        if result is not None:
            added += 1
    return added


def deliver_key(
    key_id: int,
    user_id: int,
    order_id: Optional[int] = None,
    method: str = "automatic",
    admin_id: Optional[int] = None,
) -> bool:
    """Mark a key as used and record delivery. Returns True on success."""
    try:
        with get_db_session() as s:
            mk = s.query(ManagedKey).filter_by(id=key_id, status="unused").first()
            if not mk:
                mk = s.query(ManagedKey).filter_by(id=key_id, status="reserved").first()
            if not mk:
                return False
            mk.status = "used"
            mk.used_by_user_id = user_id
            mk.used_at = datetime.utcnow()
            mk.order_id = order_id
            delivery = ManagedKeyDelivery(
                key_id=key_id,
                user_id=user_id,
                order_id=order_id,
                delivery_method=method,
                admin_id=admin_id,
            )
            s.add(delivery)
            s.commit()
            return True
    except Exception:
        logger.exception("deliver_key failed for key_id=%s", key_id)
        return False


def recycle_key(key_id: int) -> bool:
    """Reset a used key back to unused (recycle). Returns True on success."""
    try:
        with get_db_session() as s:
            mk = s.query(ManagedKey).filter_by(id=key_id).first()
            if not mk:
                return False
            mk.status = "unused"
            mk.used_by_user_id = None
            mk.used_at = None
            mk.order_id = None
            mk.reserved_by = None
            mk.reserved_at = None
            s.commit()
            return True
    except Exception:
        logger.exception("recycle_key failed for key_id=%s", key_id)
        return False


def reserve_key(key_id: int, admin_telegram_id: int) -> bool:
    """Reserve a key for manual assignment. Returns True on success."""
    try:
        with get_db_session() as s:
            mk = s.query(ManagedKey).filter_by(id=key_id, status="unused").first()
            if not mk:
                return False
            mk.status = "reserved"
            mk.reserved_by = admin_telegram_id
            mk.reserved_at = datetime.utcnow()
            s.commit()
            return True
    except Exception:
        logger.exception("reserve_key failed for key_id=%s", key_id)
        return False


def delete_key(key_id: int) -> bool:
    """Permanently delete a key."""
    try:
        with get_db_session() as s:
            mk = s.query(ManagedKey).filter_by(id=key_id).first()
            if mk:
                s.delete(mk)
                s.commit()
                return True
        return False
    except Exception:
        logger.exception("delete_key failed for key_id=%s", key_id)
        return False


def bulk_delete_keys(key_type: Optional[str] = None, status: Optional[str] = None) -> int:
    """Bulk delete keys matching type and/or status. Returns count deleted."""
    try:
        with get_db_session() as s:
            q = s.query(ManagedKey)
            if key_type:
                q = q.filter(ManagedKey.key_type == key_type)
            if status:
                q = q.filter(ManagedKey.status == status)
            rows = q.all()
            count = len(rows)
            for r in rows:
                s.delete(r)
            s.commit()
            return count
    except Exception:
        logger.exception("bulk_delete_keys failed")
        return 0


def export_keys(key_type: Optional[str] = None, status: Optional[str] = None) -> List[str]:
    """Export key values matching filters. Returns list of strings."""
    try:
        with get_db_session() as s:
            q = s.query(ManagedKey)
            if key_type:
                q = q.filter(ManagedKey.key_type == key_type)
            if status:
                q = q.filter(ManagedKey.status == status)
            return [mk.key_value for mk in q.order_by(ManagedKey.created_at.desc()).all()]
    except Exception:
        logger.exception("export_keys failed")
        return []


def get_key_stats() -> dict:
    """Return aggregate key statistics."""
    try:
        with get_db_session() as s:
            total    = s.query(ManagedKey).count()
            unused   = s.query(ManagedKey).filter_by(status="unused").count()
            used     = s.query(ManagedKey).filter_by(status="used").count()
            reserved = s.query(ManagedKey).filter_by(status="reserved").count()
            expired  = s.query(ManagedKey).filter_by(status="expired").count()
            recycled = s.query(ManagedKey).filter_by(status="recycled").count()
            delivered = s.query(ManagedKeyDelivery).count()
            return {
                "total": total,
                "unused": unused,
                "used": used,
                "reserved": reserved,
                "expired": expired,
                "recycled": recycled,
                "total_deliveries": delivered,
            }
    except Exception:
        return {
            "total": 0, "unused": 0, "used": 0,
            "reserved": 0, "expired": 0, "recycled": 0,
            "total_deliveries": 0,
        }


def auto_archive_expired_files() -> int:
    """Archive files past their expiry date. Returns count archived."""
    try:
        if not cfg.get_bool("flm_auto_delete_expired", False):
            return 0
        now = datetime.utcnow()
        with get_db_session() as s:
            rows = (s.query(ManagedFile)
                    .filter(
                        ManagedFile.status == "active",
                        ManagedFile.expires_at.isnot(None),
                        ManagedFile.expires_at <= now,
                    ).all())
            for f in rows:
                f.status = "expired"
                f.updated_at = now
            s.commit()
            return len(rows)
    except Exception:
        logger.exception("auto_archive_expired_files failed")
        return 0
