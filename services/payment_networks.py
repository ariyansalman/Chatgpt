"""
Dynamic Payment Networks — configuration service.
═════════════════════════════════════════════════

Single source of truth for "which payment networks/coins exist and how are
they presented", stored in the ``payment_networks`` table and managed
entirely from the Admin Panel (handlers/admin_payment_networks.py).

This module is CONFIGURATION ONLY. It deliberately does not:
  • create payments, verify deposits, or call any gateway API
  • credit wallets or touch WalletLedger / Transaction rows (it only READS
    transactions for statistics)
  • invent new callback_data — every network routes through an EXISTING
    callback: ``pay_<gateway_key>`` for code-backed gateways, or
    ``pay_pm_<manual_method_id>`` for admin-created networks, which reuse
    the existing ManualPaymentMethod deposit + manual verification flow.

Backward compatibility: when the table is empty (or missing), every helper
returns "no opinion" and the payment stack behaves exactly as before.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

CATEGORIES = (
    "BINANCE PAY",
    "BYBIT PAY",
    "USDT NETWORKS",
    "OTHER COINS",
    "LOCAL PAYMENT",
)

CATEGORY_EMOJI = {
    "BINANCE PAY": "🟡",
    "BYBIT PAY": "⚫",
    "USDT NETWORKS": "🌐",
    "OTHER COINS": "🪙",
    "LOCAL PAYMENT": "🇧🇩",
}

# category → bucket used by services/payment_selection_ui.py
CATEGORY_BUCKET = {
    "BINANCE PAY": "top",
    "BYBIT PAY": "top",
    "USDT NETWORKS": "usdt",
    "OTHER COINS": "coins",
    "LOCAL PAYMENT": "mobile",
}

VERIFICATION_TYPES = ("api", "manual")


# ──────────────────────────────────────────────────────────────────────────
# Table bootstrap (idempotent, additive)
# ──────────────────────────────────────────────────────────────────────────

def ensure_table() -> bool:
    """Create ``payment_networks`` if it does not exist yet. Safe to call
    repeatedly; never alters existing tables."""
    try:
        from database.db import engine  # type: ignore
        from database.models import PaymentNetwork
        PaymentNetwork.__table__.create(bind=engine, checkfirst=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("payment_networks table bootstrap skipped: %s", e)
        return False


def slugify(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return key or "network"


def unique_key(base: str) -> str:
    """Return a network_key that is not taken yet."""
    from database import get_db_session
    from database.models import PaymentNetwork

    base = slugify(base)
    with get_db_session() as session:
        taken = {
            k for (k,) in session.query(PaymentNetwork.network_key).all()
        }
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


# ──────────────────────────────────────────────────────────────────────────
# Read helpers
# ──────────────────────────────────────────────────────────────────────────

class NetworkView:
    """Plain detached snapshot of one row (safe outside the DB session)."""

    FIELDS = (
        "id", "network_key", "name", "symbol", "display_name", "category",
        "emoji", "address", "memo", "instructions", "min_deposit",
        "max_deposit", "bonus_percent", "confirmations", "api_provider",
        "verification_type", "api_verification", "manual_verification",
        "gateway_key", "manual_method_id", "display_order", "is_enabled",
        "is_visible", "is_featured", "is_recommended", "maintenance_mode",
        "admin_notes",
    )

    def __init__(self, row):
        for f in self.FIELDS:
            setattr(self, f, getattr(row, f, None))
        self.callback_key = row.callback_key
        self.live = row.is_live()

    @property
    def badge(self) -> str:
        marks = []
        if self.is_featured:
            marks.append("⭐")
        if self.is_recommended:
            marks.append("👍")
        return "".join(marks)

    @property
    def status_icon(self) -> str:
        if self.maintenance_mode:
            return "🛠"
        if not self.is_enabled:
            return "🚫"
        if not self.is_visible:
            return "🙈"
        return "✅"


def list_networks(only_live: bool = False) -> List[NetworkView]:
    """All configured networks ordered by display_order then id."""
    try:
        from database import get_db_session
        from database.models import PaymentNetwork
        with get_db_session() as session:
            q = session.query(PaymentNetwork).order_by(
                PaymentNetwork.display_order, PaymentNetwork.id
            )
            rows = [NetworkView(r) for r in q.all()]
    except Exception as e:  # noqa: BLE001
        logger.warning("list_networks failed (returning empty): %s", e)
        return []
    if only_live:
        rows = [r for r in rows if r.live]
    return rows


def get_network(network_id: int) -> Optional[NetworkView]:
    try:
        from database import get_db_session
        from database.models import PaymentNetwork
        with get_db_session() as session:
            row = session.query(PaymentNetwork).filter_by(id=network_id).first()
            return NetworkView(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("get_network failed: %s", e)
        return None


def get_by_callback_key(key: str) -> Optional[NetworkView]:
    """Find the network whose EXISTING callback suffix matches ``key``."""
    for n in list_networks():
        if n.callback_key and n.callback_key == key:
            return n
    return None


# ──────────────────────────────────────────────────────────────────────────
# Write helpers (configuration only)
# ──────────────────────────────────────────────────────────────────────────

def update_fields(network_id: int, **fields) -> bool:
    try:
        from database import get_db_session
        from database.models import PaymentNetwork
        with get_db_session() as session:
            row = session.query(PaymentNetwork).filter_by(id=network_id).first()
            if not row:
                return False
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            session.commit()
            _sync_manual_method(session, row)
            session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("update_fields failed: %s", e)
        return False


def toggle_field(network_id: int, field: str) -> bool:
    n = get_network(network_id)
    if not n:
        return False
    return update_fields(network_id, **{field: not bool(getattr(n, field, False))})


def create_network(data: Dict) -> Optional[int]:
    """Create a network row. When it is not bound to an existing code
    gateway, a ManualPaymentMethod is created/reused so the user-facing
    deposit flow is the EXISTING ``pay_pm_<id>`` manual flow."""
    ensure_table()
    try:
        from database import get_db_session
        from database.models import PaymentNetwork
        with get_db_session() as session:
            row = PaymentNetwork(
                network_key=data.get("network_key") or unique_key(data.get("name", "network")),
                name=data.get("name") or "Network",
                symbol=data.get("symbol"),
                display_name=data.get("display_name") or data.get("name") or "Network",
                category=data.get("category") or "OTHER COINS",
                emoji=data.get("emoji") or "💳",
                address=data.get("address"),
                memo=data.get("memo"),
                instructions=data.get("instructions"),
                min_deposit=data.get("min_deposit", 1.0),
                max_deposit=data.get("max_deposit"),
                bonus_percent=data.get("bonus_percent", 0.0) or 0.0,
                confirmations=int(data.get("confirmations", 1) or 1),
                api_provider=data.get("api_provider"),
                verification_type=data.get("verification_type", "manual"),
                api_verification=bool(data.get("verification_type") == "api"),
                manual_verification=bool(data.get("verification_type") != "api"),
                gateway_key=data.get("gateway_key"),
                display_order=int(data.get("display_order", 0) or 0),
                is_enabled=bool(data.get("is_enabled", True)),
            )
            session.add(row)
            session.commit()
            _sync_manual_method(session, row)
            session.commit()
            return row.id
    except Exception as e:  # noqa: BLE001
        logger.warning("create_network failed: %s", e)
        return None


def delete_network(network_id: int, drop_manual_method: bool = True) -> bool:
    try:
        from database import get_db_session
        from database.models import PaymentNetwork, ManualPaymentMethod
        with get_db_session() as session:
            row = session.query(PaymentNetwork).filter_by(id=network_id).first()
            if not row:
                return False
            mm_id = row.manual_method_id
            session.delete(row)
            session.commit()
            if drop_manual_method and mm_id:
                # Only hide it — never destroy payment history references.
                mm = session.query(ManualPaymentMethod).filter_by(id=mm_id).first()
                if mm:
                    mm.is_active = False
                    session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("delete_network failed: %s", e)
        return False


def move(network_id: int, direction: int) -> bool:
    """Move a network up (-1) or down (+1) in display order."""
    rows = list_networks()
    ids = [r.id for r in rows]
    if network_id not in ids:
        return False
    idx = ids.index(network_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(ids):
        return False
    ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
    for order, nid in enumerate(ids):
        update_fields(nid, display_order=order)
    return True


def _sync_manual_method(session, row) -> None:
    """Keep the backing ManualPaymentMethod (existing deposit flow) in sync
    with this network's admin-edited presentation fields. Never changes how
    that flow verifies payments — only the text/limits it already supports."""
    from database.models import ManualPaymentMethod

    if row.gateway_key:
        return  # code-backed gateway: nothing to mirror

    lines = []
    if row.address:
        lines.append(f"Address:\n{row.address}")
    if row.memo:
        lines.append(f"Memo / Tag: {row.memo}")
    if row.confirmations:
        lines.append(f"Required confirmations: {row.confirmations}")
    if row.instructions:
        lines.append(row.instructions)
    instructions = "\n\n".join(lines) or f"Send your payment for {row.display_name}."

    mm = None
    if row.manual_method_id:
        mm = session.query(ManualPaymentMethod).filter_by(id=row.manual_method_id).first()
    if not mm:
        mm = ManualPaymentMethod(
            name=row.display_name,
            emoji=row.emoji or "💳",
            instructions=instructions,
        )
        session.add(mm)
        session.flush()
        row.manual_method_id = mm.id

    mm.name = row.display_name
    mm.emoji = row.emoji or "💳"
    mm.instructions = instructions
    mm.account_number = row.address
    mm.account_label = row.name
    mm.min_amount = float(row.min_deposit or 0) or 1.0
    mm.max_amount = float(row.max_deposit) if row.max_deposit else None
    mm.sort_order = int(row.display_order or 0)
    # Hidden from the legacy manual list whenever the network isn't live —
    # this is the same is_active flag the existing flow already honours.
    mm.is_active = bool(row.is_enabled and row.is_visible and not row.maintenance_mode)


# ──────────────────────────────────────────────────────────────────────────
# Statistics (read-only)
# ──────────────────────────────────────────────────────────────────────────

def stats(network_id: int) -> Dict[str, float]:
    out = {"total": 0, "volume": 0.0, "success": 0, "failed": 0}
    n = get_network(network_id)
    if not n:
        return out
    try:
        from database import get_db_session
        from database.models import Transaction, TransactionStatus
        with get_db_session() as session:
            q = session.query(Transaction)
            if n.manual_method_id:
                q = q.filter(Transaction.manual_method_id == n.manual_method_id)
            elif n.gateway_key:
                q = q.filter(Transaction.crypto_address.isnot(None))
                q = q.filter(Transaction.payment_method.isnot(None))
                # best-effort: match the gateway key inside the stored method name
                rows = [
                    t for t in q.all()
                    if n.gateway_key.split("_")[0] in str(getattr(t.payment_method, "value", "")).lower()
                ]
                return _tally(rows, TransactionStatus)
            else:
                return out
            return _tally(q.all(), TransactionStatus)
    except Exception as e:  # noqa: BLE001
        logger.warning("stats failed: %s", e)
        return out


def _tally(rows: Sequence, TransactionStatus) -> Dict[str, float]:
    out = {"total": 0, "volume": 0.0, "success": 0, "failed": 0}
    for t in rows:
        out["total"] += 1
        status = getattr(t.status, "name", str(t.status)).upper()
        if status in ("COMPLETED", "CONFIRMED", "APPROVED", "SUCCESS"):
            out["success"] += 1
            out["volume"] += float(t.amount or 0)
        elif status in ("FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
            out["failed"] += 1
    return out


# ──────────────────────────────────────────────────────────────────────────
# User-facing overlay — consumed by services/payment_selection_ui.py
# ──────────────────────────────────────────────────────────────────────────

def overlay_gateways(gateways: Optional[Sequence[dict]]) -> List[dict]:
    """Merge admin-managed networks into the gateway dicts the payment
    selection screens already render.

    • A network bound to an existing gateway_key overrides that gateway's
      emoji/label/ordering and can hide it — the callback_data stays
      ``pay_<gateway_key>``.
    • A network created from the Admin Panel is appended with the existing
      ``pay_pm_<manual_method_id>`` callback.
    Returns the original list untouched when nothing is configured.
    """
    base = [dict(g) for g in (gateways or [])]
    try:
        networks = list_networks()
    except Exception:  # noqa: BLE001
        return base
    if not networks:
        return base

    by_key = {g.get("key"): g for g in base}
    result: List[dict] = []
    consumed = set()

    for n in networks:
        if not n.callback_key:
            continue
        if not n.live:
            if n.gateway_key and n.gateway_key in by_key:
                consumed.add(n.gateway_key)   # admin hid this gateway
            continue
        src = by_key.get(n.gateway_key) if n.gateway_key else None
        if n.gateway_key:
            if src is None:
                continue  # gateway not currently available/configured in code
            consumed.add(n.gateway_key)
        entry = dict(src or {})
        entry.update({
            "key": n.callback_key,
            "label": n.display_name,
            "emoji": n.emoji or "💳",
            "_category": n.category,
            "_bucket": CATEGORY_BUCKET.get(n.category, "coins"),
            "_order": n.display_order,
            "_featured": bool(n.is_featured),
            "_recommended": bool(n.is_recommended),
        })
        result.append(entry)

    passthrough = [g for g in base if g.get("key") not in consumed]
    return passthrough + result


def decorate_label(gw: dict, label: str) -> str:
    """Append ⭐ / 👍 badges to a rendered button label."""
    suffix = ""
    if gw.get("_featured"):
        suffix += " ⭐"
    if gw.get("_recommended"):
        suffix += " 👍"
    return f"{label}{suffix}"
