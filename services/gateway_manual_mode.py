"""Auto/Manual mode toggle for the bKash / Nagad native gateways.

Unlike Telegram Stars (services/telegram_stars.py), which only ever needs
its own dedicated ``PaymentGatewayConfig`` row, bKash/Nagad's API
credentials continue to live in ``bot_config`` (see services/bkash_payment.py,
services/nagad_payment.py, handlers/admin_payment_methods.py). This module
adds a SECOND, narrower concern on top of that: whether the gateway is
currently running its automated API checkout flow ("auto") or has been
switched to a manual, admin-reviewed flow ("manual") — mirroring a plain
``ManualPaymentMethod`` (merchant number + instructions, TrxID/screenshot
verified by hand) but scoped to the bKash/Nagad row itself.

This state is stored on ``PaymentGatewayConfig`` (gateway="bkash"/"nagad")
so it doesn't collide with the existing "mode" bot_config key, which means
something different there (sandbox vs live API mode).
"""
from __future__ import annotations

from typing import Optional

from database import get_db_session
from database.models import PaymentGatewayConfig

GATEWAYS = ("bkash", "nagad")
DEFAULT_MODE = "auto"


def _get_or_create_config(session, gateway: str) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway=gateway).first()
    if not row:
        row = PaymentGatewayConfig(gateway=gateway, is_enabled=False, mode=DEFAULT_MODE)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def get_mode(gateway: str) -> str:
    """Return "auto" or "manual" for the given gateway ("bkash"/"nagad")."""
    if gateway not in GATEWAYS:
        return DEFAULT_MODE
    with get_db_session() as session:
        row = _get_or_create_config(session, gateway)
        return (row.mode or DEFAULT_MODE).lower()


def is_manual(gateway: str) -> bool:
    return get_mode(gateway) == "manual"


def set_mode(gateway: str, mode: str) -> None:
    mode = (mode or DEFAULT_MODE).lower()
    if mode not in ("auto", "manual"):
        raise ValueError("mode must be 'auto' or 'manual'")
    with get_db_session() as session:
        row = _get_or_create_config(session, gateway)
        row.mode = mode
        session.commit()


def toggle_mode(gateway: str) -> str:
    """Flip auto<->manual and return the new mode."""
    current = get_mode(gateway)
    new_mode = "manual" if current == "auto" else "auto"
    set_mode(gateway, new_mode)
    return new_mode


def get_manual_details(gateway: str) -> dict:
    """Return {"merchant_number": ..., "instructions": ...} for manual mode."""
    with get_db_session() as session:
        row = _get_or_create_config(session, gateway)
        return {
            "merchant_number": row.manual_merchant_number or "",
            "instructions": row.manual_instructions or "",
        }


def set_manual_merchant_number(gateway: str, value: Optional[str]) -> None:
    with get_db_session() as session:
        row = _get_or_create_config(session, gateway)
        row.manual_merchant_number = (value or "").strip()[:120] or None
        session.commit()


def set_manual_instructions(gateway: str, value: Optional[str]) -> None:
    with get_db_session() as session:
        row = _get_or_create_config(session, gateway)
        row.manual_instructions = (value or "").strip() or None
        session.commit()
