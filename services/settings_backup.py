"""Settings Backup & Restore Service — V34.

Creates a comprehensive JSON snapshot of all configurable settings:
  • bot_config table (all key-value pairs)
  • Products (name, description, price, stock, category, type, images)
  • Categories and subcategories
  • Payment gateway configurations (non-sensitive fields)
  • Feature management states
  • Referral, coupon, wallet, broadcast, language settings (via bot_config)

Supports:
  • gzip compression (configurable)
  • SHA-256 checksum verification
  • Restore (settings-only or full including products/categories)
  • Import from uploaded JSON file
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import get_db_session
from database.models import (
    BotConfig, Product, Category, Subcategory,
    PaymentGatewayConfig,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/telegram-store")) / "settings"
SCHEMA_VERSION = "1.0"


def _ensure_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _safe_error(msg: str) -> str:
    return re.sub(r"://[^:@/]+:[^@/]+@", "://***:***@", msg or "")[:500]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ──────────────────────────────────────────────────────────────────────────
# Data collectors
# ──────────────────────────────────────────────────────────────────────────

def _collect_bot_config() -> dict:
    """Return all bot_config rows as {key: value}."""
    try:
        with get_db_session() as s:
            rows = s.query(BotConfig).all()
            return {r.key: r.value for r in rows}
    except Exception:
        logger.exception("settings_backup: failed to collect bot_config")
        return {}


def _collect_categories() -> list:
    """Return categories with their subcategories."""
    try:
        with get_db_session() as s:
            cats = s.query(Category).order_by(Category.id).all()
            result = []
            for c in cats:
                subs = []
                try:
                    for sc in (s.query(Subcategory)
                               .filter_by(category_id=c.id)
                               .order_by(Subcategory.id).all()):
                        subs.append({
                            "id": sc.id,
                            "name": sc.name,
                            "description": getattr(sc, "description", None),
                        })
                except Exception:
                    pass
                result.append({
                    "id": c.id,
                    "name": c.name,
                    "description": getattr(c, "description", None),
                    "is_active": getattr(c, "is_active", True),
                    "subcategories": subs,
                })
            return result
    except Exception:
        logger.exception("settings_backup: failed to collect categories")
        return []


def _collect_products() -> list:
    """Return active products (non-sensitive fields only)."""
    try:
        with get_db_session() as s:
            products = (s.query(Product)
                        .filter(Product.is_active == True)  # noqa: E712
                        .order_by(Product.id).all())
            result = []
            for p in products:
                entry: dict = {
                    "id": p.id,
                    "name": p.name,
                    "description": getattr(p, "description", None),
                    "price": float(p.price) if p.price is not None else 0.0,
                    "stock": p.stock,
                    "is_active": p.is_active,
                    "product_type": p.product_type.value if hasattr(p.product_type, "value") else str(p.product_type),
                    "category_id": p.category_id,
                    "subcategory_id": getattr(p, "subcategory_id", None),
                    "image_url": getattr(p, "image_url", None),
                    "min_stock_alert": getattr(p, "min_stock_alert", None),
                    "max_per_user": getattr(p, "max_per_user", None),
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                result.append(entry)
            return result
    except Exception:
        logger.exception("settings_backup: failed to collect products")
        return []


def _collect_payment_gateways() -> list:
    """Return non-sensitive payment gateway settings."""
    try:
        with get_db_session() as s:
            gws = s.query(PaymentGatewayConfig).all()
            result = []
            for gw in gws:
                # Only export safe, non-credential fields
                entry: dict = {
                    "id": gw.id,
                    "gateway_name": gw.gateway_name,
                    "is_active": gw.is_active,
                    "display_name": getattr(gw, "display_name", None),
                    "min_amount": getattr(gw, "min_amount", None),
                    "max_amount": getattr(gw, "max_amount", None),
                    "fee_percent": getattr(gw, "fee_percent", None),
                    "currency": getattr(gw, "currency", None),
                    "order_expiry_minutes": getattr(gw, "order_expiry_minutes", None),
                }
                result.append(entry)
            return result
    except Exception:
        logger.exception("settings_backup: failed to collect payment gateways")
        return []


def _build_backup_payload(created_by: Optional[int] = None, note: str = "") -> dict:
    """Assemble the complete backup payload dict."""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": created_by,
        "note": note,
        "data": {
            "bot_config": _collect_bot_config(),
            "categories": _collect_categories(),
            "products": _collect_products(),
            "payment_gateways": _collect_payment_gateways(),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def create_settings_backup(admin_id: Optional[int] = None,
                           note: str = "",
                           triggered_by: str = "manual") -> object:
    """Create a JSON settings backup and persist a SettingsBackupRecord.

    Returns the SettingsBackupRecord ORM object.
    """
    from database.models import SettingsBackupRecord
    _ensure_dir()
    compress = cfg.get_bool("backup_compression", True)
    ext = ".json.gz" if compress else ".json"
    fname = f"settings_{_timestamp()}{ext}"
    fpath = BACKUP_DIR / fname

    with get_db_session() as s:
        rec = SettingsBackupRecord(
            backup_type="settings",
            filename=fname,
            status="RUNNING",
            note=note or None,
            created_by=admin_id,
            triggered_by=triggered_by,
            created_at=datetime.utcnow(),
        )
        s.add(rec)
        s.commit()
        s.refresh(rec)
        rec_id = rec.id

    try:
        payload = _build_backup_payload(created_by=admin_id, note=note)
        raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
        if compress:
            data = gzip.compress(raw, compresslevel=9)
        else:
            data = raw
        fpath.write_bytes(data)
        checksum = _sha256(data)
        size = fpath.stat().st_size

        with get_db_session() as s:
            r = s.get(SettingsBackupRecord, rec_id)
            r.status = "SUCCESS"
            r.size_bytes = size
            r.checksum = checksum
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r

    except Exception as e:
        logger.exception("create_settings_backup failed")
        try:
            if fpath.exists():
                fpath.unlink()
        except Exception:
            pass
        with get_db_session() as s:
            r = s.get(SettingsBackupRecord, rec_id)
            r.status = "FAILED"
            r.error_summary = _safe_error(str(e))
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r


def verify_backup(backup_id: int) -> dict:
    """Verify a backup by re-computing and comparing the checksum.

    Returns {"ok": bool, "reason": str}.
    """
    from database.models import SettingsBackupRecord
    try:
        with get_db_session() as s:
            rec = s.get(SettingsBackupRecord, backup_id)
            if not rec:
                return {"ok": False, "reason": "Record not found."}
            if rec.status != "SUCCESS":
                return {"ok": False, "reason": f"Backup status is {rec.status}."}
            filename = rec.filename
            expected = rec.checksum

        fpath = BACKUP_DIR / filename
        if not fpath.exists():
            return {"ok": False, "reason": "Backup file not found on disk."}

        data = fpath.read_bytes()
        actual = _sha256(data)
        if expected and actual != expected:
            return {"ok": False, "reason": "Checksum mismatch — file may be corrupted."}

        # Parse the JSON to confirm it's valid
        try:
            raw = gzip.decompress(data) if filename.endswith(".gz") else data
            payload = json.loads(raw)
            version = payload.get("schema_version", "?")
        except Exception as parse_err:
            return {"ok": False, "reason": f"JSON parse error: {parse_err}"}

        return {"ok": True, "reason": f"Checksum OK. Schema v{version}."}
    except Exception as e:
        return {"ok": False, "reason": _safe_error(str(e))}


def restore_settings_backup(backup_id: int,
                             admin_id: Optional[int] = None,
                             restore_products: bool = False,
                             restore_categories: bool = False) -> dict:
    """Restore settings from a backup.

    Args:
        backup_id: ID of the SettingsBackupRecord to restore.
        admin_id: Admin performing the restore (for audit).
        restore_products: If True, also restore product prices/stock.
        restore_categories: If True, also restore category names.

    Returns:
        {"ok": bool, "restored_keys": int, "errors": list}
    """
    from database.models import SettingsBackupRecord

    errors: list = []
    restored_keys = 0

    try:
        with get_db_session() as s:
            rec = s.get(SettingsBackupRecord, backup_id)
            if not rec or rec.status != "SUCCESS":
                return {"ok": False, "restored_keys": 0,
                        "errors": ["Backup not found or status is not SUCCESS."]}
            filename = rec.filename

        fpath = BACKUP_DIR / filename
        if not fpath.exists():
            return {"ok": False, "restored_keys": 0,
                    "errors": ["Backup file not found on disk."]}

        data = fpath.read_bytes()
        raw = gzip.decompress(data) if filename.endswith(".gz") else data
        payload = json.loads(raw)
        backup_data = payload.get("data", {})

        # ── Restore bot_config ────────────────────────────────────────────
        bot_cfg_data = backup_data.get("bot_config", {})
        if bot_cfg_data:
            try:
                with get_db_session() as s:
                    for key, value in bot_cfg_data.items():
                        row = s.query(BotConfig).filter_by(key=key).first()
                        if row:
                            row.value = str(value)
                            restored_keys += 1
                        # Don't create new keys that don't exist — safe restore only
                    s.commit()
                # Flush the bot_config cache so restored values take effect
                try:
                    cfg._cache.clear()
                except Exception:
                    pass
            except Exception as e:
                errors.append(f"bot_config restore error: {_safe_error(str(e))}")

        # ── Restore categories (optional) ─────────────────────────────────
        if restore_categories:
            cats = backup_data.get("categories", [])
            try:
                with get_db_session() as s:
                    for cat in cats:
                        row = s.query(Category).filter_by(id=cat["id"]).first()
                        if row:
                            row.name = cat.get("name", row.name)
                            if cat.get("description") is not None:
                                row.description = cat.get("description")
                    s.commit()
            except Exception as e:
                errors.append(f"categories restore error: {_safe_error(str(e))}")

        # ── Restore product prices / stock (optional) ─────────────────────
        if restore_products:
            prods = backup_data.get("products", [])
            try:
                with get_db_session() as s:
                    for prod in prods:
                        row = s.query(Product).filter_by(id=prod["id"]).first()
                        if row:
                            if prod.get("price") is not None:
                                row.price = prod["price"]
                            if prod.get("stock") is not None:
                                row.stock = prod["stock"]
                    s.commit()
            except Exception as e:
                errors.append(f"products restore error: {_safe_error(str(e))}")

        # ── Update restore metadata ───────────────────────────────────────
        try:
            with get_db_session() as s:
                r = s.get(SettingsBackupRecord, backup_id)
                if r:
                    r.restore_count = (r.restore_count or 0) + 1
                    r.last_restored_at = datetime.utcnow()
                    r.last_restored_by = admin_id
                    s.commit()
        except Exception:
            pass

        return {"ok": not errors, "restored_keys": restored_keys, "errors": errors}

    except Exception as e:
        return {"ok": False, "restored_keys": 0,
                "errors": [_safe_error(str(e))]}


def delete_backup(backup_id: int) -> dict:
    """Delete a settings backup record and its file.

    Returns {"ok": bool, "reason": str}.
    """
    from database.models import SettingsBackupRecord
    try:
        with get_db_session() as s:
            rec = s.get(SettingsBackupRecord, backup_id)
            if not rec:
                return {"ok": False, "reason": "Record not found."}
            filename = rec.filename
            s.delete(rec)
            s.commit()

        fpath = BACKUP_DIR / filename
        try:
            if fpath.exists():
                fpath.unlink()
        except Exception:
            pass
        return {"ok": True, "reason": "Backup deleted."}
    except Exception as e:
        return {"ok": False, "reason": _safe_error(str(e))}


def get_backup_file_path(backup_id: int) -> Optional[Path]:
    """Return the Path to the backup file if it exists, else None."""
    from database.models import SettingsBackupRecord
    try:
        with get_db_session() as s:
            rec = s.get(SettingsBackupRecord, backup_id)
            if not rec or rec.status != "SUCCESS":
                return None
            fpath = BACKUP_DIR / rec.filename
            return fpath if fpath.exists() else None
    except Exception:
        return None


def import_backup_from_bytes(data: bytes, admin_id: Optional[int] = None) -> object:
    """Import a backup from raw bytes (from file upload).

    Saves the file to disk and creates a SettingsBackupRecord.
    Returns the record (status SUCCESS or FAILED).
    """
    from database.models import SettingsBackupRecord
    _ensure_dir()

    # Detect compression
    is_gz = data[:2] == b"\x1f\x8b"
    ext = ".json.gz" if is_gz else ".json"
    fname = f"import_{_timestamp()}{ext}"
    fpath = BACKUP_DIR / fname

    with get_db_session() as s:
        rec = SettingsBackupRecord(
            backup_type="settings",
            filename=fname,
            status="RUNNING",
            note="Imported by admin",
            created_by=admin_id,
            triggered_by="import",
            created_at=datetime.utcnow(),
        )
        s.add(rec)
        s.commit()
        s.refresh(rec)
        rec_id = rec.id

    try:
        # Validate JSON before saving
        raw = gzip.decompress(data) if is_gz else data
        payload = json.loads(raw)
        if "data" not in payload:
            raise ValueError("Invalid backup format: missing 'data' key.")

        fpath.write_bytes(data)
        checksum = _sha256(data)
        size = fpath.stat().st_size

        with get_db_session() as s:
            r = s.get(SettingsBackupRecord, rec_id)
            r.status = "SUCCESS"
            r.size_bytes = size
            r.checksum = checksum
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r

    except Exception as e:
        logger.exception("import_backup_from_bytes failed")
        try:
            if fpath.exists():
                fpath.unlink()
        except Exception:
            pass
        with get_db_session() as s:
            r = s.get(SettingsBackupRecord, rec_id)
            r.status = "FAILED"
            r.error_summary = _safe_error(str(e))
            r.completed_at = datetime.utcnow()
            s.commit()
            s.refresh(r)
            return r


def cleanup_old_backups() -> int:
    """Delete oldest backups beyond backup_max_count. Returns count deleted."""
    from database.models import SettingsBackupRecord
    keep = max(1, cfg.get_int("backup_max_count", 30))
    deleted = 0
    try:
        with get_db_session() as s:
            rows = (s.query(SettingsBackupRecord)
                    .filter(SettingsBackupRecord.status == "SUCCESS")
                    .order_by(SettingsBackupRecord.created_at.desc()).all())
            for r in rows[keep:]:
                fpath = BACKUP_DIR / r.filename
                try:
                    if fpath.exists():
                        fpath.unlink()
                    deleted += 1
                except Exception:
                    logger.exception("failed to unlink %s", fpath)
                r.status = "PRUNED"
            s.commit()
    except Exception:
        logger.exception("cleanup_old_backups failed")
    return deleted
