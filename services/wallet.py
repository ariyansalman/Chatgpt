"""Wallet service — single choke-point for all wallet balance mutations.

Every credit / debit / adjust writes a ``WalletLedger`` row in the SAME
transaction as the ``User.wallet_balance`` update, so the ledger is the
authoritative history and never drifts from the live balance.

Existing legacy paths that still do ``user.wallet_balance += x`` keep
working; the ledger simply won't see those movements until they are
migrated. Use the helpers here for anything new (Admin Wallets panel,
refunds, promotions).
"""
from __future__ import annotations

import logging
from typing import Optional

from database import get_db_session, User
from database.models import WalletLedger

logger = logging.getLogger(__name__)


class WalletError(Exception):
    pass


def _apply(user_id: int, delta: float, *, reason: str,
           actor_type: str, actor_id: Optional[int],
           ref_type: Optional[str], ref_id: Optional[str]) -> float:
    """Apply delta atomically and return the new balance."""
    if delta == 0:
        raise WalletError("Delta must be non-zero")
    with get_db_session() as s:
        user = s.query(User).filter(User.id == user_id).with_for_update().first() \
            if s.bind.dialect.name == "postgresql" \
            else s.query(User).filter(User.id == user_id).first()
        if user is None:
            raise WalletError(f"User {user_id} not found")
        new_bal = float(user.wallet_balance or 0.0) + float(delta)
        if new_bal < 0:
            raise WalletError("Insufficient balance")
        user.wallet_balance = new_bal
        s.add(WalletLedger(
            user_id=user.id,
            delta=float(delta),
            balance_after=new_bal,
            reason=(reason or "")[:255] or None,
            actor_type=(actor_type or "system")[:16],
            actor_id=actor_id,
            ref_type=(ref_type or None) and str(ref_type)[:32],
            ref_id=(ref_id is not None) and str(ref_id)[:64] or None,
        ))
        s.commit()
        return new_bal



def credit_locked(session, user_id: int, amount: float, *, reason: str,
                  actor_type: str = "system", actor_id: Optional[int] = None,
                  ref_type: Optional[str] = None, ref_id: Optional[str] = None) -> float:
    """Credit using the caller's DB transaction so payment state + ledger stay atomic."""
    if amount <= 0:
        raise WalletError("Amount must be > 0")
    q = session.query(User).filter(User.id == user_id)
    user = q.with_for_update().first() if session.bind.dialect.name == "postgresql" else q.first()
    if user is None:
        raise WalletError(f"User {user_id} not found")
    new_bal = float(user.wallet_balance or 0.0) + float(amount)
    user.wallet_balance = new_bal
    session.add(WalletLedger(
        user_id=user.id, delta=float(amount), balance_after=new_bal,
        reason=(reason or "")[:255] or None, actor_type=(actor_type or "system")[:16],
        actor_id=actor_id, ref_type=(ref_type or None) and str(ref_type)[:32],
        ref_id=(ref_id is not None) and str(ref_id)[:64] or None,
    ))
    session.flush()
    return new_bal

def credit(user_id: int, amount: float, *, reason: str,
           actor_type: str = "system", actor_id: Optional[int] = None,
           ref_type: Optional[str] = None, ref_id: Optional[str] = None) -> float:
    if amount <= 0:
        raise WalletError("Amount must be > 0")
    return _apply(user_id, +float(amount), reason=reason,
                  actor_type=actor_type, actor_id=actor_id,
                  ref_type=ref_type, ref_id=ref_id)


def debit(user_id: int, amount: float, *, reason: str,
          actor_type: str = "system", actor_id: Optional[int] = None,
          ref_type: Optional[str] = None, ref_id: Optional[str] = None) -> float:
    if amount <= 0:
        raise WalletError("Amount must be > 0")
    return _apply(user_id, -float(amount), reason=reason,
                  actor_type=actor_type, actor_id=actor_id,
                  ref_type=ref_type, ref_id=ref_id)


def adjust(user_id: int, delta: float, *, reason: str,
           actor_type: str = "admin", actor_id: Optional[int] = None) -> float:
    """Admin manual adjustment (positive or negative)."""
    return _apply(user_id, float(delta), reason=reason,
                  actor_type=actor_type, actor_id=actor_id,
                  ref_type="admin_adjust", ref_id=None)


def ledger(user_id: int, limit: int = 20) -> list:
    """Return recent ledger rows for a user, newest first (plain dicts)."""
    out = []
    with get_db_session() as s:
        rows = (s.query(WalletLedger)
                .filter(WalletLedger.user_id == user_id)
                .order_by(WalletLedger.created_at.desc())
                .limit(limit).all())
        for r in rows:
            out.append({
                "id": r.id,
                "delta": float(r.delta or 0.0),
                "balance_after": float(r.balance_after or 0.0),
                "reason": r.reason or "",
                "actor_type": r.actor_type,
                "actor_id": r.actor_id,
                "created_at": r.created_at,
            })
    return out
