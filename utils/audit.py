"""Admin audit-log helper.

Append-only. Never store secrets. Failures MUST NOT break the caller —
audit is best-effort observability, not part of the transactional flow.
"""

from __future__ import annotations

import logging
from typing import Optional

from database import get_db_session, AdminAuditLog

logger = logging.getLogger(__name__)


def log_admin_action(
    admin_telegram_id: int,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[object] = None,
    details: Optional[str] = None,
    # V21 enhanced audit params (all optional — backward-compatible)
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    ip_address: Optional[str] = None,
    module: Optional[str] = None,
) -> None:
    """Record a privileged admin action.

    Keep ``details`` short and free of secrets/PII. On any DB error the call
    is swallowed and logged — the caller must never see it fail.

    V21 additions (all optional, backward-compatible):
      old_value  — serialized previous value (e.g. old config value)
      new_value  — serialized new value
      ip_address — IPv4/IPv6 if available from context
      module     — module/feature area (e.g. "auto_refund", "multilang")
    """
    try:
        entry = AdminAuditLog(
            admin_telegram_id=int(admin_telegram_id),
            action=action[:64],
            target_type=(target_type or None) and str(target_type)[:32],
            target_id=(target_id is not None) and str(target_id)[:64] or None,
            details=(details or None) and str(details)[:2000],
        )
        # Set V21 enhanced columns only if they exist on the model
        if old_value is not None:
            try:
                entry.old_value = str(old_value)[:2000]
            except Exception:
                pass
        if new_value is not None:
            try:
                entry.new_value = str(new_value)[:2000]
            except Exception:
                pass
        if ip_address is not None:
            try:
                entry.ip_address = str(ip_address)[:45]
            except Exception:
                pass
        if module is not None:
            try:
                entry.module = str(module)[:64]
            except Exception:
                pass
        with get_db_session() as s:
            s.add(entry)
            s.commit()
    except Exception:
        logger.exception("audit log write failed for action=%s", action)