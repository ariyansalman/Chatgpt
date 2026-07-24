"""Multi-Currency Wallet Service — V39.

Single choke-point for all multi-currency balance mutations.
Every credit / debit writes a CurrencyTransaction row atomically
with the UserCurrencyWallet.balance update so the ledger never drifts.

The legacy User.wallet_balance (USD primary wallet) is NOT touched here —
it continues to be managed by services/wallet.py.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from database import get_db_session
from database.models import (
    User, UserCurrencyWallet, CurrencyTransaction,
    WalletCurrencyConfig, WalletCurrencyStatus,
    CurrencyTransactionType, CurrencyTxStatus,
)

logger = logging.getLogger(__name__)

# ─── Default currencies seeded on first startup ───────────────────────────────
DEFAULT_CURRENCIES = [
    {"code": "USD",  "name": "US Dollar",        "symbol": "$",   "is_crypto": False, "sort_order": 1},
    {"code": "BDT",  "name": "Bangladeshi Taka",  "symbol": "৳",  "is_crypto": False, "sort_order": 2},
    {"code": "USDT", "name": "Tether",            "symbol": "₮",  "is_crypto": True,  "sort_order": 3},
    {"code": "BTC",  "name": "Bitcoin",           "symbol": "₿",  "is_crypto": True,  "sort_order": 4},
    {"code": "ETH",  "name": "Ethereum",          "symbol": "Ξ",  "is_crypto": True,  "sort_order": 5},
    {"code": "LTC",  "name": "Litecoin",          "symbol": "Ł",  "is_crypto": True,  "sort_order": 6},
    {"code": "BNB",  "name": "BNB",               "symbol": "BNB","is_crypto": True,  "sort_order": 7},
    {"code": "TRX",  "name": "TRON",              "symbol": "TRX","is_crypto": True,  "sort_order": 8},
]


class MCWalletError(Exception):
    pass


class MCWalletFrozenError(MCWalletError):
    pass


class MCWalletLimitError(MCWalletError):
    pass


# ─── Currency config helpers ──────────────────────────────────────────────────

def seed_default_currencies() -> None:
    """Insert default currencies if they don't already exist. Call once at startup."""
    try:
        with get_db_session() as s:
            existing = {r.code for r in s.query(WalletCurrencyConfig.code).all()}
            added = 0
            for c in DEFAULT_CURRENCIES:
                if c["code"] in existing:
                    continue
                s.add(WalletCurrencyConfig(
                    code=c["code"],
                    name=c["name"],
                    symbol=c["symbol"],
                    is_crypto=c["is_crypto"],
                    sort_order=c["sort_order"],
                    status=WalletCurrencyStatus.ENABLED.value,
                    is_enabled=True,
                ))
                added += 1
            if added:
                s.commit()
                logger.info("Seeded %d default currencies", added)
    except Exception:
        logger.exception("seed_default_currencies failed")


def get_all_currencies(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Return list of currency configs as plain dicts."""
    out = []
    with get_db_session() as s:
        q = s.query(WalletCurrencyConfig)
        if enabled_only:
            q = q.filter(WalletCurrencyConfig.is_enabled == True)  # noqa: E712
        rows = q.order_by(WalletCurrencyConfig.sort_order, WalletCurrencyConfig.code).all()
        for r in rows:
            out.append({
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "symbol": r.symbol,
                "is_crypto": r.is_crypto,
                "is_enabled": r.is_enabled,
                "status": r.status,
                "is_frozen": r.is_frozen,
                "min_balance": r.min_balance,
                "max_balance": r.max_balance,
                "min_deposit": r.min_deposit,
                "max_deposit": r.max_deposit,
                "deposit_fee_pct": r.deposit_fee_pct,
                "min_withdrawal": r.min_withdrawal,
                "max_withdrawal": r.max_withdrawal,
                "withdrawal_fee_pct": r.withdrawal_fee_pct,
                "withdrawal_fee_flat": r.withdrawal_fee_flat,
                "sort_order": r.sort_order,
                "notes": r.notes,
            })
    return out


def get_currency_config(code: str) -> Optional[Dict[str, Any]]:
    """Return a single currency config dict or None."""
    all_c = get_all_currencies()
    for c in all_c:
        if c["code"] == code.upper():
            return c
    return None


def add_currency(code: str, name: str, symbol: str, is_crypto: bool = False,
                 sort_order: int = 99) -> Dict[str, Any]:
    """Add a new currency. Raises MCWalletError if code already exists."""
    code = code.upper().strip()
    if not code or len(code) > 16:
        raise MCWalletError("Invalid currency code")
    with get_db_session() as s:
        existing = s.query(WalletCurrencyConfig).filter_by(code=code).first()
        if existing:
            raise MCWalletError(f"Currency {code} already exists")
        cfg = WalletCurrencyConfig(
            code=code, name=name.strip(), symbol=symbol.strip(),
            is_crypto=is_crypto, sort_order=sort_order,
            status=WalletCurrencyStatus.ENABLED.value, is_enabled=True,
        )
        s.add(cfg)
        s.commit()
        return get_currency_config(code)


def update_currency(code: str, **fields) -> Dict[str, Any]:
    """Update mutable fields of a currency config."""
    code = code.upper()
    allowed = {
        "name", "symbol", "is_crypto", "is_enabled", "status", "is_frozen",
        "min_balance", "max_balance", "min_deposit", "max_deposit",
        "deposit_fee_pct", "min_withdrawal", "max_withdrawal",
        "withdrawal_fee_pct", "withdrawal_fee_flat", "sort_order", "notes",
    }
    with get_db_session() as s:
        cfg = s.query(WalletCurrencyConfig).filter_by(code=code).first()
        if not cfg:
            raise MCWalletError(f"Currency {code} not found")
        for k, v in fields.items():
            if k in allowed:
                setattr(cfg, k, v)
        s.commit()
    return get_currency_config(code)


# ─── Wallet helpers ───────────────────────────────────────────────────────────

def _get_or_create_wallet(session, user_id: int, currency_code: str) -> UserCurrencyWallet:
    """Get or create a UserCurrencyWallet row; does NOT commit."""
    wallet = (session.query(UserCurrencyWallet)
              .filter_by(user_id=user_id, currency_code=currency_code)
              .first())
    if wallet is None:
        wallet = UserCurrencyWallet(user_id=user_id, currency_code=currency_code, balance=0.0)
        session.add(wallet)
        session.flush()
    return wallet


def _validate_currency_for_operation(session, currency_code: str,
                                      operation: str) -> WalletCurrencyConfig:
    """Check the currency is enabled and not frozen. Raises MCWalletError otherwise."""
    cfg = session.query(WalletCurrencyConfig).filter_by(code=currency_code).first()
    if not cfg:
        raise MCWalletError(f"Unknown currency: {currency_code}")
    if not cfg.is_enabled or cfg.status == WalletCurrencyStatus.DISABLED.value:
        raise MCWalletError(f"Currency {currency_code} is disabled")
    if cfg.status == WalletCurrencyStatus.MAINTENANCE.value:
        raise MCWalletError(f"Currency {currency_code} is in maintenance mode")
    if cfg.is_frozen or cfg.status == WalletCurrencyStatus.FROZEN.value:
        raise MCWalletFrozenError(f"Currency {currency_code} is frozen")
    return cfg


def _apply(user_id: int, currency_code: str, delta: float, *,
           tx_type: str, reason: str,
           actor_type: str = "system", actor_id: Optional[int] = None,
           ref_type: Optional[str] = None, ref_id: Optional[str] = None,
           fee: float = 0.0, skip_currency_checks: bool = False) -> Dict[str, Any]:
    """Apply a delta atomically to a user's currency wallet.

    Returns a dict with new_balance, wallet_id, tx_id.
    Uses SELECT … FOR UPDATE on PostgreSQL to prevent race conditions.
    """
    if delta == 0:
        raise MCWalletError("Delta must be non-zero")

    with get_db_session() as s:
        # Validate currency
        if not skip_currency_checks:
            _validate_currency_for_operation(s, currency_code, tx_type)

        # Get/create wallet with row lock
        wallet = _get_or_create_wallet(s, user_id, currency_code)

        dialect = s.bind.dialect.name if s.bind else "sqlite"
        if dialect == "postgresql":
            # Re-query with FOR UPDATE after flush created the row
            wallet = (s.query(UserCurrencyWallet)
                      .filter_by(id=wallet.id)
                      .with_for_update()
                      .first())

        # Check frozen
        if wallet.is_frozen and not skip_currency_checks:
            raise MCWalletFrozenError(f"Wallet for {currency_code} is frozen for this user")

        # Check balance
        old_balance = float(wallet.balance or 0.0)
        new_balance = old_balance + delta
        if new_balance < 0:
            raise MCWalletError(
                f"Insufficient {currency_code} balance "
                f"(have {old_balance:.8f}, need {abs(delta):.8f})"
            )

        wallet.balance = new_balance
        wallet.updated_at = datetime.utcnow()

        net = abs(delta) - fee
        tx = CurrencyTransaction(
            user_id=user_id,
            wallet_id=wallet.id,
            currency_code=currency_code,
            tx_type=tx_type,
            amount=abs(delta),
            fee=max(fee, 0.0),
            net_amount=max(net, 0.0),
            balance_before=old_balance,
            balance_after=new_balance,
            status=CurrencyTxStatus.COMPLETED.value,
            ref_type=(ref_type or None) and str(ref_type)[:32],
            ref_id=(ref_id is not None) and str(ref_id)[:64] or None,
            actor_type=(actor_type or "system")[:16],
            actor_id=actor_id,
            notes=(reason or "")[:255] or None,
        )
        s.add(tx)
        s.commit()
        return {"new_balance": new_balance, "wallet_id": wallet.id, "tx_id": tx.id,
                "old_balance": old_balance, "currency_code": currency_code}


def credit(user_id: int, currency_code: str, amount: float, *,
           reason: str, actor_type: str = "system", actor_id: Optional[int] = None,
           ref_type: Optional[str] = None, ref_id: Optional[str] = None,
           fee: float = 0.0) -> Dict[str, Any]:
    """Credit a user's currency wallet."""
    if amount <= 0:
        raise MCWalletError("Amount must be > 0")
    return _apply(user_id, currency_code.upper(), +float(amount),
                  tx_type=CurrencyTransactionType.DEPOSIT.value,
                  reason=reason, actor_type=actor_type, actor_id=actor_id,
                  ref_type=ref_type, ref_id=ref_id, fee=fee)


def debit(user_id: int, currency_code: str, amount: float, *,
          reason: str, actor_type: str = "system", actor_id: Optional[int] = None,
          ref_type: Optional[str] = None, ref_id: Optional[str] = None,
          fee: float = 0.0) -> Dict[str, Any]:
    """Debit a user's currency wallet."""
    if amount <= 0:
        raise MCWalletError("Amount must be > 0")
    return _apply(user_id, currency_code.upper(), -float(amount),
                  tx_type=CurrencyTransactionType.WITHDRAWAL.value,
                  reason=reason, actor_type=actor_type, actor_id=actor_id,
                  ref_type=ref_type, ref_id=ref_id, fee=fee)


def admin_adjust(user_id: int, currency_code: str, delta: float, *,
                 reason: str, actor_id: Optional[int] = None,
                 tx_type_override: Optional[str] = None) -> Dict[str, Any]:
    """Admin manual adjustment (positive = credit, negative = debit)."""
    tx_type = tx_type_override or (
        CurrencyTransactionType.MANUAL_CREDIT.value if delta > 0
        else CurrencyTransactionType.MANUAL_DEBIT.value
    )
    return _apply(user_id, currency_code.upper(), float(delta),
                  tx_type=tx_type, reason=reason,
                  actor_type="admin", actor_id=actor_id,
                  ref_type="admin_adjust", skip_currency_checks=True)


def transfer(user_id: int, from_currency: str, to_currency: str,
             from_amount: float, to_amount: float, *,
             reason: str = "wallet transfer",
             actor_type: str = "user", actor_id: Optional[int] = None) -> Dict[str, Any]:
    """Transfer between two currency wallets for the same user.

    from_amount is debited from from_currency.
    to_amount is credited to to_currency (may differ due to exchange rate).
    """
    if from_amount <= 0 or to_amount <= 0:
        raise MCWalletError("Transfer amounts must be > 0")
    if from_currency.upper() == to_currency.upper():
        raise MCWalletError("Cannot transfer to the same currency")

    # Debit first, then credit — if debit fails (insufficient balance) we stop
    out = _apply(user_id, from_currency.upper(), -float(from_amount),
                 tx_type=CurrencyTransactionType.TRANSFER_OUT.value,
                 reason=reason, actor_type=actor_type, actor_id=actor_id,
                 ref_type="transfer", ref_id=f"to:{to_currency.upper()}")
    _apply(user_id, to_currency.upper(), +float(to_amount),
           tx_type=CurrencyTransactionType.TRANSFER_IN.value,
           reason=reason, actor_type=actor_type, actor_id=actor_id,
           ref_type="transfer", ref_id=f"from:{from_currency.upper()}")
    return out


# ─── Balance / portfolio queries ──────────────────────────────────────────────

def get_user_wallets(user_id: int) -> List[Dict[str, Any]]:
    """Return all currency wallets for a user (including zero-balance ones for enabled currencies)."""
    result = []
    with get_db_session() as s:
        # All enabled currencies
        currencies = (s.query(WalletCurrencyConfig)
                      .filter(WalletCurrencyConfig.is_enabled == True)  # noqa: E712
                      .order_by(WalletCurrencyConfig.sort_order, WalletCurrencyConfig.code)
                      .all())
        # Existing wallets
        wallets = {w.currency_code: w
                   for w in s.query(UserCurrencyWallet).filter_by(user_id=user_id).all()}
        for c in currencies:
            w = wallets.get(c.code)
            result.append({
                "currency_code": c.code,
                "name": c.name,
                "symbol": c.symbol,
                "is_crypto": c.is_crypto,
                "balance": float(w.balance if w else 0.0),
                "is_frozen": (w.is_frozen if w else False) or c.is_frozen,
                "wallet_id": w.id if w else None,
                "currency_status": c.status,
            })
    return result


def get_user_wallet_balance(user_id: int, currency_code: str) -> float:
    """Return the balance for a specific currency (0.0 if wallet doesn't exist)."""
    with get_db_session() as s:
        w = s.query(UserCurrencyWallet).filter_by(
            user_id=user_id, currency_code=currency_code.upper()
        ).first()
        return float(w.balance if w else 0.0)


def get_wallet_transactions(user_id: int, currency_code: Optional[str] = None,
                            limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent transactions for a user, optionally filtered by currency."""
    out = []
    with get_db_session() as s:
        q = (s.query(CurrencyTransaction)
             .filter(CurrencyTransaction.user_id == user_id))
        if currency_code:
            q = q.filter(CurrencyTransaction.currency_code == currency_code.upper())
        rows = q.order_by(CurrencyTransaction.created_at.desc()).limit(limit).all()
        for r in rows:
            out.append({
                "id": r.id,
                "currency_code": r.currency_code,
                "tx_type": r.tx_type,
                "amount": float(r.amount or 0),
                "fee": float(r.fee or 0),
                "net_amount": float(r.net_amount or 0),
                "balance_before": float(r.balance_before or 0),
                "balance_after": float(r.balance_after or 0),
                "status": r.status,
                "notes": r.notes or "",
                "actor_type": r.actor_type,
                "created_at": r.created_at,
            })
    return out


def get_portfolio_value_usd(user_id: int) -> float:
    """Return the total portfolio value across all wallets, converted to USD using exchange rates."""
    from services.exchange_rate_service import convert_to_usd
    wallets = get_user_wallets(user_id)
    total = 0.0
    for w in wallets:
        bal = w["balance"]
        if bal <= 0:
            continue
        code = w["currency_code"]
        if code == "USD":
            total += bal
        else:
            usd_val = convert_to_usd(bal, code)
            total += usd_val if usd_val is not None else 0.0
    return total


def get_admin_wallet_stats() -> Dict[str, Any]:
    """Return aggregate wallet stats for admin dashboard."""
    from sqlalchemy import func as sqlfunc
    stats: Dict[str, Any] = {}
    try:
        with get_db_session() as s:
            # Total per-currency balances
            rows = (s.query(
                        UserCurrencyWallet.currency_code,
                        sqlfunc.sum(UserCurrencyWallet.balance),
                        sqlfunc.count(UserCurrencyWallet.id),
                    )
                    .group_by(UserCurrencyWallet.currency_code)
                    .all())
            per_currency = {}
            for code, total_bal, wallet_count in rows:
                per_currency[code] = {"total_balance": float(total_bal or 0),
                                      "wallet_count": wallet_count}
            stats["per_currency"] = per_currency
            stats["enabled_currencies"] = (
                s.query(WalletCurrencyConfig)
                .filter(WalletCurrencyConfig.is_enabled == True)  # noqa: E712
                .count()
            )
            stats["total_currencies"] = s.query(WalletCurrencyConfig).count()
            stats["total_wallets"] = s.query(UserCurrencyWallet).count()
            stats["frozen_wallets"] = (
                s.query(UserCurrencyWallet)
                .filter(UserCurrencyWallet.is_frozen == True)  # noqa: E712
                .count()
            )
    except Exception:
        logger.exception("get_admin_wallet_stats failed")
    return stats
