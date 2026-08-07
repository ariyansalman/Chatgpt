"""
Dynamic LOCAL Payment Providers — configuration service.
════════════════════════════════════════════════════════

Single source of truth for "which local payment providers exist (bKash,
Nagad, Rocket, Upay, SureCash, Tap, CellFin, … unlimited) and how they are
presented". Everything lives in the ``local_payment_providers`` table and is
managed entirely from the Admin Panel (handlers/admin_local_payments.py).

This module is CONFIGURATION ONLY. It deliberately does not:
  • create payments, verify deposits, or call any gateway API
  • credit wallets or touch WalletLedger / Transaction rows (it only READS
    transactions for statistics)
  • invent new callback_data — every provider routes through an EXISTING
    callback: ``pay_<gateway_key>`` for code-backed gateways, or
    ``pay_pm_<manual_method_id>`` for admin-created providers, which reuse
    the existing ManualPaymentMethod deposit + manual verification flow.

Backward compatibility: when the table is empty (or missing) every helper
returns "no opinion" and the local payment menu behaves exactly as before.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

ACCOUNT_TYPES = ("personal", "agent", "merchant")

ACCOUNT_TYPE_LABEL = {
    "personal": "👤 Personal",
    "agent": "🏪 Agent",
    "merchant": "🏢 Merchant",
}

# Providers offered as one-tap presets in the ➕ ADD LOCAL PAYMENT wizard.
# These are only *suggestions* for the admin — nothing here is hardcoded into
# the user-facing menu, which is always generated from the database.
PRESETS = (
    ("bkash",    "bKash",    "BKASH",    "🩷"),
    ("nagad",    "Nagad",    "NAGAD",    "🟠"),
    ("rocket",   "Rocket",   "ROCKET",   "🔵"),
    ("upay",     "Upay",     "UPAY",     "🟣"),
    ("surecash", "SureCash", "SURECASH", "🟢"),
    ("tap",      "Tap",      "TAP",      "🔷"),
    ("cellfin",  "CellFin",  "CELLFIN",  "🟡"),
)


# ──────────────────────────────────────────────────────────────────────────
# Table bootstrap (idempotent, additive)
# ──────────────────────────────────────────────────────────────────────────

def ensure_table() -> bool:
    """Create ``local_payment_providers`` if it does not exist yet. Safe to
    call repeatedly; never alters existing tables."""
    try:
        from database.db import engine  # type: ignore
        from database.models import LocalPaymentProvider
        LocalPaymentProvider.__table__.create(bind=engine, checkfirst=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("local_payment_providers table bootstrap skipped: %s", e)
        return False


def slugify(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return key or "provider"


def unique_key(base: str) -> str:
    """Return a provider_key that is not taken yet."""
    from database import get_db_session
    from database.models import LocalPaymentProvider

    base = slugify(base)
    try:
        with get_db_session() as session:
            taken = {k for (k,) in session.query(LocalPaymentProvider.provider_key).all()}
    except Exception:  # noqa: BLE001
        return base
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


# ──────────────────────────────────────────────────────────────────────────
# Read helpers
# ──────────────────────────────────────────────────────────────────────────

class ProviderView:
    """Plain detached snapshot of one row (safe outside the DB session)."""

    FIELDS = (
        "id", "provider_key", "name", "display_name", "emoji",
        "wallet_number", "account_type", "account_holder", "instructions",
        "min_deposit", "max_deposit", "bonus_percent", "exchange_rate",
        "auto_rate", "rate_currency", "gateway_key", "manual_method_id",
        "display_order", "is_default", "is_enabled", "is_visible",
        "maintenance_mode", "admin_notes",
    )

    def __init__(self, row):
        for f in self.FIELDS:
            setattr(self, f, getattr(row, f, None))
        self.callback_key = row.callback_key
        self.live = row.is_live()

    @property
    def account_type_label(self) -> str:
        return ACCOUNT_TYPE_LABEL.get(self.account_type or "personal", "👤 Personal")

    @property
    def badge(self) -> str:
        return "⭐" if self.is_default else ""

    @property
    def status_icon(self) -> str:
        if self.maintenance_mode:
            return "🛠"
        if not self.is_enabled:
            return "🚫"
        if not self.is_visible:
            return "🙈"
        return "✅"

    @property
    def effective_rate(self) -> Optional[float]:
        """Live rate when Auto Rate is ON, otherwise the manual rate."""
        if self.auto_rate:
            rate = fetch_auto_rate(self.rate_currency or "BDT")
            if rate:
                return rate
        return float(self.exchange_rate) if self.exchange_rate else None


def fetch_auto_rate(currency: str = "BDT") -> Optional[float]:
    """Read-only lookup of the USD → local currency rate from the existing
    exchange rate service. Never writes anything."""
    try:
        from services.exchange_rate_service import get_rate
        return get_rate("USD", (currency or "BDT").upper())
    except Exception as e:  # noqa: BLE001
        logger.debug("auto rate lookup failed: %s", e)
        return None


def list_providers(only_live: bool = False) -> List[ProviderView]:
    """All configured providers ordered by display_order then id."""
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider
        with get_db_session() as session:
            q = session.query(LocalPaymentProvider).order_by(
                LocalPaymentProvider.display_order, LocalPaymentProvider.id
            )
            rows = [ProviderView(r) for r in q.all()]
    except Exception as e:  # noqa: BLE001
        logger.warning("list_providers failed (returning empty): %s", e)
        return []
    if only_live:
        rows = [r for r in rows if r.live]
    return rows


def get_provider(provider_id: int) -> Optional[ProviderView]:
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider
        with get_db_session() as session:
            row = session.query(LocalPaymentProvider).filter_by(id=provider_id).first()
            return ProviderView(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("get_provider failed: %s", e)
        return None


def get_default() -> Optional[ProviderView]:
    for p in list_providers(only_live=True):
        if p.is_default:
            return p
    return None


# ──────────────────────────────────────────────────────────────────────────
# Write helpers (configuration only)
# ──────────────────────────────────────────────────────────────────────────

def update_fields(provider_id: int, **fields) -> bool:
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider
        with get_db_session() as session:
            row = session.query(LocalPaymentProvider).filter_by(id=provider_id).first()
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


def toggle_field(provider_id: int, field: str) -> bool:
    p = get_provider(provider_id)
    if not p:
        return False
    return update_fields(provider_id, **{field: not bool(getattr(p, field, False))})


def set_default(provider_id: int) -> bool:
    """⭐ Exactly one provider can be the default."""
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider
        with get_db_session() as session:
            rows = session.query(LocalPaymentProvider).all()
            found = False
            for r in rows:
                should = (r.id == provider_id)
                r.is_default = should
                found = found or should
            session.commit()
            return found
    except Exception as e:  # noqa: BLE001
        logger.warning("set_default failed: %s", e)
        return False


def create_provider(data: Dict) -> Optional[int]:
    """Create a provider row. When it is not bound to an existing code
    gateway, a ManualPaymentMethod is created so the user-facing deposit flow
    is the EXISTING ``pay_pm_<id>`` manual flow."""
    ensure_table()
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider
        name = data.get("name") or "Provider"
        with get_db_session() as session:
            row = LocalPaymentProvider(
                provider_key=data.get("provider_key") or unique_key(name),
                name=name,
                display_name=(data.get("display_name") or name).upper(),
                emoji=data.get("emoji") or "💳",
                wallet_number=data.get("wallet_number"),
                account_type=(data.get("account_type") or "personal"),
                account_holder=data.get("account_holder"),
                instructions=data.get("instructions"),
                min_deposit=data.get("min_deposit", 1.0),
                max_deposit=data.get("max_deposit"),
                bonus_percent=data.get("bonus_percent", 0.0) or 0.0,
                exchange_rate=data.get("exchange_rate"),
                auto_rate=bool(data.get("auto_rate", False)),
                rate_currency=(data.get("rate_currency") or "BDT").upper(),
                gateway_key=data.get("gateway_key"),
                display_order=int(data.get("display_order", 0) or 0),
                is_enabled=bool(data.get("is_enabled", True)),
                is_visible=bool(data.get("is_visible", True)),
                maintenance_mode=bool(data.get("maintenance_mode", False)),
                admin_notes=data.get("admin_notes"),
            )
            session.add(row)
            session.commit()
            _sync_manual_method(session, row)
            session.commit()
            new_id = row.id
        if data.get("is_default"):
            set_default(new_id)
        return new_id
    except Exception as e:  # noqa: BLE001
        logger.warning("create_provider failed: %s", e)
        return None


def delete_provider(provider_id: int, drop_manual_method: bool = True) -> bool:
    try:
        from database import get_db_session
        from database.models import LocalPaymentProvider, ManualPaymentMethod
        with get_db_session() as session:
            row = session.query(LocalPaymentProvider).filter_by(id=provider_id).first()
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
        logger.warning("delete_provider failed: %s", e)
        return False


def move(provider_id: int, direction: int) -> bool:
    """Move a provider up (-1) or down (+1) in display order."""
    rows = list_providers()
    ids = [r.id for r in rows]
    if provider_id not in ids:
        return False
    idx = ids.index(provider_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(ids):
        return False
    ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
    for order, pid in enumerate(ids):
        update_fields(pid, display_order=order)
    return True


# ──────────────────────────────────────────────────────────────────────────
# ManualPaymentMethod sync — keeps the EXISTING deposit flow in charge
# ──────────────────────────────────────────────────────────────────────────

def _instruction_text(row) -> str:
    lines = []
    if row.wallet_number:
        type_label = ACCOUNT_TYPE_LABEL.get(row.account_type or "personal", "Personal")
        lines.append(f"{type_label} number: {row.wallet_number}")
    if row.account_holder:
        lines.append(f"Account holder: {row.account_holder}")
    rate = None
    try:
        rate = ProviderView(row).effective_rate
    except Exception:  # noqa: BLE001
        rate = row.exchange_rate
    if rate:
        lines.append(f"Rate: 1 USD = {float(rate):.2f} {row.rate_currency or 'BDT'}")
    if row.bonus_percent:
        lines.append(f"Bonus: +{float(row.bonus_percent):.2f}%")
    if row.instructions:
        lines.append(row.instructions)
    return "\n\n".join(lines) or f"Send your payment for {row.display_name}."


def _sync_manual_method(session, row) -> None:
    """Mirror the provider config onto its ManualPaymentMethod so the
    unchanged ``pay_pm_<id>`` flow renders the right card. Providers bound to
    a code gateway keep using ``pay_<gateway_key>`` and are skipped."""
    from database.models import ManualPaymentMethod

    if row.gateway_key:
        return

    mm = None
    if row.manual_method_id:
        mm = session.query(ManualPaymentMethod).filter_by(id=row.manual_method_id).first()
    if not mm:
        mm = ManualPaymentMethod(
            name=row.display_name,
            emoji=row.emoji or "💳",
            instructions=_instruction_text(row),
        )
        session.add(mm)
        session.flush()
        row.manual_method_id = mm.id

    mm.name = row.display_name
    mm.emoji = row.emoji or "💳"
    mm.instructions = _instruction_text(row)
    mm.account_number = row.wallet_number
    mm.account_label = row.account_holder or row.name
    mm.min_amount = float(row.min_deposit or 0) or 1.0
    mm.max_amount = float(row.max_deposit) if row.max_deposit else None
    mm.sort_order = int(row.display_order or 0)
    # Same is_active flag the existing manual flow already honours.
    mm.is_active = bool(row.is_enabled and row.is_visible and not row.maintenance_mode)


# ──────────────────────────────────────────────────────────────────────────
# Statistics (read-only)
# ──────────────────────────────────────────────────────────────────────────

def stats(provider_id: int) -> Dict[str, float]:
    out = {"total": 0, "volume": 0.0, "success": 0, "failed": 0}
    p = get_provider(provider_id)
    if not p or not p.manual_method_id:
        return out
    try:
        from database import get_db_session
        from database.models import Transaction
        with get_db_session() as session:
            rows = (session.query(Transaction)
                    .filter(Transaction.manual_method_id == p.manual_method_id)
                    .all())
        for t in rows:
            out["total"] += 1
            status = getattr(t.status, "name", str(t.status)).upper()
            if status in ("COMPLETED", "CONFIRMED", "APPROVED", "SUCCESS"):
                out["success"] += 1
                out["volume"] += float(t.amount or 0)
            elif status in ("FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
                out["failed"] += 1
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("stats failed: %s", e)
        return out


# ──────────────────────────────────────────────────────────────────────────
# User-facing buttons — consumed by services/payment_selection_ui.py
# ──────────────────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True when the admin manages local payments from the panel. While this
    is False the legacy hardcoded local menu keeps rendering unchanged."""
    return bool(list_providers())


def local_buttons() -> List[Dict]:
    """Every live provider as a presentation dict for the local payment
    screen. ``key`` is always an EXISTING callback suffix."""
    out: List[Dict] = []
    for p in list_providers(only_live=True):
        if not p.callback_key:
            continue
        out.append({
            "key": p.callback_key,
            "label": (p.display_name or p.name or "").upper(),
            "emoji": p.emoji or "💳",
            "_order": p.display_order,
            "_default": bool(p.is_default),
        })
    out.sort(key=lambda g: (0 if g.get("_default") else 1, g.get("_order", 0)))
    return out
