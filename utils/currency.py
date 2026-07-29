"""Multi-currency display helpers.

DB values always stay in USD. This module reads Settings once (cached briefly)
and appends a converted amount in the shop's secondary display currency when
the admin has configured one, e.g.:

    $12.50 (~৳1,375.00)
"""

from datetime import datetime
from database import get_db_session, Settings

_CACHE = {"data": None, "ts": None}
_TTL = 30  # seconds


def _get_currency():
    """Return (code, symbol, rate) or (None, None, 0.0). Cached briefly."""
    now = datetime.utcnow()
    if _CACHE["data"] is not None and _CACHE["ts"] and (now - _CACHE["ts"]).total_seconds() < _TTL:
        return _CACHE["data"]

    code, symbol, rate = None, None, 0.0
    try:
        with get_db_session() as session:
            s = session.query(Settings).first()
            if s:
                code = (s.secondary_currency_code or "").strip() or None
                symbol = (s.secondary_currency_symbol or "").strip() or code
                rate = float(s.secondary_currency_rate or 0.0)
    except Exception:
        pass

    _CACHE["data"] = (code, symbol, rate)
    _CACHE["ts"] = now
    return _CACHE["data"]


def clear_currency_cache():
    _CACHE["data"] = None
    _CACHE["ts"] = None


def format_price_multi(price_usd: float) -> str:
    """Format `$X.XX` and append `(~<sym><Y.YY>)` if a secondary currency is set."""
    base = f"${price_usd:.2f}"
    code, symbol, rate = _get_currency()
    if code and rate and rate > 0:
        converted = price_usd * rate
        base += f" (~{symbol or code}{converted:,.2f})"
    return base


def convert_usd(price_usd: float):
    """Return (converted_amount, code, symbol) or (None, None, None) if not set."""
    code, symbol, rate = _get_currency()
    if code and rate and rate > 0:
        return price_usd * rate, code, (symbol or code)
    return None, None, None


# ══════════════════════════════════════════════════════════════════════════
# V12 (Multi-Currency): per-user display-currency toggle.
#
# Independent of the admin's `secondary_currency_*` settings above (which
# append a *global* hint like "(~৳1,375.00)" to every price). This lets each
# individual user pick whether prices are actually shown in USD or BDT,
# stored on User.preferred_currency. Wallet balances / order totals remain
# USD internally — only the rendered text changes.
# ══════════════════════════════════════════════════════════════════════════

_SYMBOLS = {"USD": "$", "BDT": "৳"}
SUPPORTED_DISPLAY_CURRENCIES = ("USD", "BDT")


def get_user_currency(telegram_id: int) -> str:
    """Return the user's preferred display currency ("USD" or "BDT")."""
    try:
        with get_db_session() as session:
            from database.models import User
            u = session.query(User).filter_by(telegram_id=telegram_id).first()
            if u and u.preferred_currency in SUPPORTED_DISPLAY_CURRENCIES:
                return u.preferred_currency
    except Exception:
        pass
    return "USD"


def toggle_user_currency(telegram_id: int) -> str:
    """Flip the user's preferred display currency and return the new value."""
    with get_db_session() as session:
        from database.models import User
        u = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not u:
            return "USD"
        new_currency = "BDT" if (u.preferred_currency or "USD") == "USD" else "USD"
        u.preferred_currency = new_currency
        return new_currency


def format_amount_in(amount_usd: float, currency: str) -> str:
    """Format a USD-denominated amount in the given display currency."""
    currency = (currency or "USD").upper()
    symbol = _SYMBOLS.get(currency, currency)
    if currency == "USD":
        return f"${amount_usd:.2f}"
    from services.pricing import convert_currency
    converted = convert_currency(amount_usd, "USD", currency)
    return f"{symbol}{converted:,.2f}"


def format_price_for_user(amount_usd: float, telegram_id: int) -> str:
    """Format a USD-denominated amount in whatever currency `telegram_id` prefers."""
    return format_amount_in(amount_usd, get_user_currency(telegram_id))
