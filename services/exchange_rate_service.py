"""Exchange Rate Manager Service — V39.

Manages exchange rates for currency pairs with:
  - Automatic fetching from free public APIs (CoinGecko for crypto, exchangerate-api for fiat)
  - Manual override rates set by admin
  - Configurable buy/sell spread (margin %)
  - Rate locking (no auto-update)
  - Per-pair update intervals
  - History and audit logging

Existing USD<->BDT logic in services/pricing.py is NOT replaced — this
module is additive and powers the new multi-currency wallet and exchange UI.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests

from database import get_db_session
from database.models import (
    ExchangeRatePair, ExchangeRateHistory, ExchangeRateLog,
    ExchangeRateSource, ExchangeRatePairStatus,
)

logger = logging.getLogger(__name__)

# ─── API endpoints ─────────────────────────────────────────────────────────────

# CoinGecko simple price endpoint (no API key required, generous rate limits)
_COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_COINGECKO_IDS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "LTC":  "litecoin",
    "BNB":  "binancecoin",
    "TRX":  "tron",
    "USDT": "tether",
    "USDC": "usd-coin",
    "XRP":  "ripple",
    "SOL":  "solana",
    "ADA":  "cardano",
    "DOGE": "dogecoin",
    "MATIC":"matic-network",
    "AVAX": "avalanche-2",
    "TON":  "the-open-network",
}

# Open Exchange Rates (no-key fallback): exchangerate-api.com
_FIAT_RATE_URL = "https://open.er-api.com/v6/latest/USD"

# In-process cache: (pair_key → (rate_dict, timestamp))
_rate_cache: Dict[str, Tuple[Dict[str, float], float]] = {}
_CACHE_TTL = 60  # seconds (1 minute per pair)

# Default currency pairs to seed on first startup
DEFAULT_PAIRS = [
    {"from_currency": "USD",  "to_currency": "BDT",  "display_name": "USD / BDT",  "rate_source": "auto_api", "auto_update_interval": 60},
    {"from_currency": "USD",  "to_currency": "USDT", "display_name": "USD / USDT", "rate_source": "auto_api", "auto_update_interval": 5},
    {"from_currency": "USDT", "to_currency": "BDT",  "display_name": "USDT / BDT", "rate_source": "auto_api", "auto_update_interval": 60},
    {"from_currency": "BTC",  "to_currency": "USD",  "display_name": "BTC / USD",  "rate_source": "auto_api", "auto_update_interval": 5},
    {"from_currency": "ETH",  "to_currency": "USD",  "display_name": "ETH / USD",  "rate_source": "auto_api", "auto_update_interval": 5},
    {"from_currency": "LTC",  "to_currency": "USD",  "display_name": "LTC / USD",  "rate_source": "auto_api", "auto_update_interval": 15},
    {"from_currency": "BNB",  "to_currency": "USD",  "display_name": "BNB / USD",  "rate_source": "auto_api", "auto_update_interval": 15},
    {"from_currency": "TRX",  "to_currency": "USD",  "display_name": "TRX / USD",  "rate_source": "auto_api", "auto_update_interval": 15},
]


# ─── Seed helpers ─────────────────────────────────────────────────────────────

def seed_default_pairs() -> None:
    """Insert default exchange rate pairs if they don't exist. Call once at startup."""
    try:
        with get_db_session() as s:
            added = 0
            for p in DEFAULT_PAIRS:
                exists = (s.query(ExchangeRatePair)
                          .filter_by(from_currency=p["from_currency"],
                                     to_currency=p["to_currency"])
                          .first())
                if exists:
                    continue
                s.add(ExchangeRatePair(
                    from_currency=p["from_currency"],
                    to_currency=p["to_currency"],
                    display_name=p.get("display_name"),
                    rate_source=p.get("rate_source", ExchangeRateSource.AUTO_API.value),
                    auto_update_interval=p.get("auto_update_interval", 60),
                    status=ExchangeRatePairStatus.ENABLED.value,
                    is_active=True,
                ))
                added += 1
            if added:
                s.commit()
                logger.info("Seeded %d default exchange rate pairs", added)
    except Exception:
        logger.exception("seed_default_pairs failed")


# ─── API fetchers ─────────────────────────────────────────────────────────────

def _fetch_crypto_usd(crypto_code: str) -> Optional[float]:
    """Fetch crypto price in USD from CoinGecko. Returns None on failure."""
    coin_id = _COINGECKO_IDS.get(crypto_code.upper())
    if not coin_id:
        return None
    cache_key = f"cg:{crypto_code}"
    cached = _rate_cache.get(cache_key)
    if cached and (time.monotonic() - cached[1]) < _CACHE_TTL:
        return cached[0].get("usd")
    try:
        resp = requests.get(
            _COINGECKO_PRICE_URL,
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=8
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get(coin_id, {}).get("usd", 0) or 0)
        if rate > 0:
            _rate_cache[cache_key] = ({"usd": rate}, time.monotonic())
            return rate
    except Exception as e:
        logger.warning("CoinGecko fetch failed for %s: %s", crypto_code, e)
    return None


def _fetch_fiat_rates() -> Optional[Dict[str, float]]:
    """Fetch USD-based fiat rates from open.er-api.com. Returns None on failure."""
    cache_key = "fiat_usd_base"
    cached = _rate_cache.get(cache_key)
    if cached and (time.monotonic() - cached[1]) < _CACHE_TTL * 5:
        return cached[0]
    try:
        resp = requests.get(_FIAT_RATE_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates", {})
        if rates:
            _rate_cache[cache_key] = (rates, time.monotonic())
            return rates
    except Exception as e:
        logger.warning("Fiat rate fetch failed: %s", e)
    return None


def _fetch_pair_rate(from_currency: str, to_currency: str,
                     api_url: Optional[str] = None,
                     api_response_path: Optional[str] = None) -> Optional[float]:
    """Fetch the mid-rate for a currency pair from public APIs.

    Returns how many `to_currency` units equal 1 `from_currency`.
    """
    from_c = from_currency.upper()
    to_c   = to_currency.upper()

    # Custom API endpoint
    if api_url:
        try:
            resp = requests.get(api_url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            rate = None
            if api_response_path:
                parts = api_response_path.split(".")
                obj = data
                for part in parts:
                    obj = obj.get(part, {}) if isinstance(obj, dict) else None
                rate = obj
            elif isinstance(data, dict):
                rate = data.get("rate") or data.get("value") or data.get(to_c)
            if rate:
                return float(rate)
        except Exception as e:
            logger.warning("Custom API fetch failed for %s/%s: %s", from_c, to_c, e)
        return None

    # Crypto → USD (or crypto → fiat via USD bridge)
    _CRYPTO = set(_COINGECKO_IDS.keys())
    _FIAT   = {"USD", "EUR", "GBP", "BDT", "JPY", "CNY", "INR", "AED", "SAR"}

    if from_c in _CRYPTO and to_c == "USD":
        return _fetch_crypto_usd(from_c)

    if from_c in _CRYPTO and to_c in _FIAT:
        usd_price = _fetch_crypto_usd(from_c)
        if usd_price is None:
            return None
        if to_c == "USD":
            return usd_price
        # Convert USD → to_c
        fiat_rates = _fetch_fiat_rates()
        if fiat_rates and to_c in fiat_rates:
            return usd_price * float(fiat_rates[to_c])
        return None

    if from_c == "USD" and to_c in _FIAT:
        fiat_rates = _fetch_fiat_rates()
        if fiat_rates and to_c in fiat_rates:
            return float(fiat_rates[to_c])
        return None

    if from_c in _FIAT and to_c in _FIAT:
        fiat_rates = _fetch_fiat_rates()
        if fiat_rates and from_c in fiat_rates and to_c in fiat_rates:
            return float(fiat_rates[to_c]) / float(fiat_rates[from_c])
        return None

    if from_c == "USDT" and to_c in (_FIAT | {"USD"}):
        # USDT ≈ USD
        if to_c == "USD":
            return 1.0
        fiat_rates = _fetch_fiat_rates()
        if fiat_rates and to_c in fiat_rates:
            return float(fiat_rates[to_c])
        return None

    if from_c == "USD" and to_c == "USDT":
        return 1.0  # USDT ≈ USD

    return None


# ─── Rate computation with margin ─────────────────────────────────────────────

def _apply_margin(mid_rate: float, margin_pct: float) -> Tuple[float, float]:
    """Return (buy_rate, sell_rate) with margin_pct spread around mid_rate."""
    if margin_pct <= 0:
        return mid_rate, mid_rate
    half = mid_rate * (margin_pct / 100.0 / 2.0)
    return mid_rate - half, mid_rate + half


# ─── Public rate access ────────────────────────────────────────────────────────

def get_rate(from_currency: str, to_currency: str,
             rate_type: str = "mid") -> Optional[float]:
    """Return the current rate for a pair ('mid', 'buy', or 'sell').

    Returns None if the pair is not configured or rate is unavailable.
    """
    from_c = from_currency.upper()
    to_c   = to_currency.upper()
    if from_c == to_c:
        return 1.0

    with get_db_session() as s:
        pair = (s.query(ExchangeRatePair)
                .filter_by(from_currency=from_c, to_currency=to_c,
                           is_active=True)
                .first())
        if pair is None:
            # Try inverse
            inv = (s.query(ExchangeRatePair)
                   .filter_by(from_currency=to_c, to_currency=from_c,
                              is_active=True)
                   .first())
            if inv:
                inv_rate = _effective_rate(inv, rate_type)
                if inv_rate and inv_rate > 0:
                    return 1.0 / inv_rate
            return None

        if pair.status != ExchangeRatePairStatus.ENABLED.value:
            return None

        return _effective_rate(pair, rate_type)


def _effective_rate(pair: ExchangeRatePair, rate_type: str = "mid") -> Optional[float]:
    """Return the effective rate from a pair row."""
    if pair.manual_override_rate and pair.is_locked:
        rate = float(pair.manual_override_rate)
        buy_r, sell_r = _apply_margin(rate, float(pair.margin_pct or 0))
        if rate_type == "buy":
            return buy_r
        if rate_type == "sell":
            return sell_r
        return rate

    mid = (float(pair.manual_override_rate)
           if pair.manual_override_rate
           else float(pair.mid_rate or 0))
    if mid <= 0:
        return None
    buy_r, sell_r = _apply_margin(mid, float(pair.margin_pct or 0))
    if rate_type == "buy":
        return float(pair.buy_rate or buy_r)
    if rate_type == "sell":
        return float(pair.sell_rate or sell_r)
    return float(pair.mid_rate or mid)


def convert_to_usd(amount: float, from_currency: str) -> Optional[float]:
    """Convert an amount from from_currency to USD using current rates."""
    if from_currency.upper() == "USD":
        return amount
    rate = get_rate(from_currency, "USD")
    if rate is None:
        rate = get_rate("USD", from_currency)
        if rate and rate > 0:
            rate = 1.0 / rate
    if rate and rate > 0:
        return amount * rate
    return None


def convert_amount(amount: float, from_currency: str, to_currency: str,
                   rate_type: str = "mid") -> Optional[float]:
    """Convert amount from from_currency to to_currency."""
    if from_currency.upper() == to_currency.upper():
        return amount
    rate = get_rate(from_currency, to_currency, rate_type)
    if rate and rate > 0:
        return amount * rate
    # Try via USD bridge
    usd_val = convert_to_usd(amount, from_currency)
    if usd_val is None:
        return None
    if to_currency.upper() == "USD":
        return usd_val
    usd_to_target = get_rate("USD", to_currency, rate_type)
    if usd_to_target and usd_to_target > 0:
        return usd_val * usd_to_target
    return None


# ─── Pair management ──────────────────────────────────────────────────────────

def get_all_pairs() -> List[Dict[str, Any]]:
    """Return all exchange rate pairs as plain dicts."""
    out = []
    with get_db_session() as s:
        rows = (s.query(ExchangeRatePair)
                .order_by(ExchangeRatePair.from_currency, ExchangeRatePair.to_currency)
                .all())
        for r in rows:
            out.append(_pair_to_dict(r))
    return out


def _pair_to_dict(r: ExchangeRatePair) -> Dict[str, Any]:
    return {
        "id": r.id,
        "from_currency": r.from_currency,
        "to_currency": r.to_currency,
        "display_name": r.display_name or f"{r.from_currency} / {r.to_currency}",
        "mid_rate": float(r.mid_rate or 0),
        "buy_rate": float(r.buy_rate or 0),
        "sell_rate": float(r.sell_rate or 0),
        "margin_pct": float(r.margin_pct or 0),
        "manual_override_rate": float(r.manual_override_rate) if r.manual_override_rate else None,
        "rate_source": r.rate_source,
        "auto_update_interval": r.auto_update_interval,
        "is_locked": r.is_locked,
        "status": r.status,
        "is_active": r.is_active,
        "previous_mid_rate": float(r.previous_mid_rate or 0),
        "last_updated": r.last_updated,
        "last_auto_update": r.last_auto_update,
        "last_update_source": r.last_update_source,
        "last_update_error": r.last_update_error,
        "updates_today": r.updates_today,
        "failed_updates_today": r.failed_updates_today,
    }


def add_pair(from_currency: str, to_currency: str,
             display_name: Optional[str] = None,
             rate_source: str = ExchangeRateSource.MANUAL.value,
             auto_update_interval: int = 60,
             mid_rate: Optional[float] = None,
             margin_pct: float = 0.0) -> Dict[str, Any]:
    """Add a new exchange rate pair."""
    from_c = from_currency.upper().strip()
    to_c   = to_currency.upper().strip()
    if from_c == to_c:
        raise ValueError("from_currency and to_currency must differ")
    with get_db_session() as s:
        exists = s.query(ExchangeRatePair).filter_by(
            from_currency=from_c, to_currency=to_c).first()
        if exists:
            raise ValueError(f"Pair {from_c}/{to_c} already exists")
        pair = ExchangeRatePair(
            from_currency=from_c,
            to_currency=to_c,
            display_name=display_name or f"{from_c} / {to_c}",
            rate_source=rate_source,
            auto_update_interval=auto_update_interval,
            mid_rate=mid_rate,
            margin_pct=margin_pct,
            status=ExchangeRatePairStatus.ENABLED.value,
            is_active=True,
        )
        if mid_rate:
            buy_r, sell_r = _apply_margin(float(mid_rate), float(margin_pct))
            pair.buy_rate, pair.sell_rate = buy_r, sell_r
        s.add(pair)
        s.commit()
        row = s.query(ExchangeRatePair).filter_by(from_currency=from_c, to_currency=to_c).first()
        return _pair_to_dict(row)


def update_pair_manual_rate(pair_id: int, rate: float, *,
                             actor_id: Optional[int] = None,
                             actor_type: str = "admin",
                             also_set_buy_sell: bool = True) -> Dict[str, Any]:
    """Set a manual override rate for a pair."""
    with get_db_session() as s:
        pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
        if not pair:
            raise ValueError(f"Pair #{pair_id} not found")
        old_rate = float(pair.mid_rate or 0)
        pair.manual_override_rate = float(rate)
        pair.mid_rate = float(rate)
        pair.last_update_source = ExchangeRateSource.MANUAL.value
        pair.last_updated = datetime.utcnow()
        pair.previous_mid_rate = old_rate or pair.previous_mid_rate
        if also_set_buy_sell:
            buy_r, sell_r = _apply_margin(float(rate), float(pair.margin_pct or 0))
            pair.buy_rate = buy_r
            pair.sell_rate = sell_r
        # Log
        log = ExchangeRateLog(
            pair_id=pair.id, action="manual_override",
            old_rate=old_rate, new_rate=float(rate),
            actor_type=actor_type, actor_id=actor_id,
        )
        s.add(log)
        # History
        hist = ExchangeRateHistory(
            pair_id=pair.id,
            from_currency=pair.from_currency,
            to_currency=pair.to_currency,
            mid_rate=float(rate),
            buy_rate=pair.buy_rate,
            sell_rate=pair.sell_rate,
            margin_pct=pair.margin_pct,
            source=ExchangeRateSource.MANUAL.value,
        )
        s.add(hist)
        s.commit()
        return _pair_to_dict(s.query(ExchangeRatePair).filter_by(id=pair_id).first())


def lock_pair(pair_id: int, locked: bool, *, actor_id: Optional[int] = None) -> Dict[str, Any]:
    """Lock or unlock a pair to prevent auto-updates."""
    with get_db_session() as s:
        pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
        if not pair:
            raise ValueError(f"Pair #{pair_id} not found")
        pair.is_locked = locked
        action = "lock" if locked else "unlock"
        s.add(ExchangeRateLog(
            pair_id=pair.id, action=action,
            old_rate=float(pair.mid_rate or 0), new_rate=float(pair.mid_rate or 0),
            actor_type="admin", actor_id=actor_id,
        ))
        s.commit()
        return _pair_to_dict(s.query(ExchangeRatePair).filter_by(id=pair_id).first())


def set_pair_status(pair_id: int, status: str, *, actor_id: Optional[int] = None) -> Dict[str, Any]:
    """Set the operational status of a pair."""
    valid = {s.value for s in ExchangeRatePairStatus}
    if status not in valid:
        raise ValueError(f"Invalid status: {status}")
    with get_db_session() as s:
        pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
        if not pair:
            raise ValueError(f"Pair #{pair_id} not found")
        pair.status = status
        pair.is_active = (status == ExchangeRatePairStatus.ENABLED.value)
        s.add(ExchangeRateLog(
            pair_id=pair.id, action=f"status_changed:{status}",
            old_rate=float(pair.mid_rate or 0), new_rate=float(pair.mid_rate or 0),
            actor_type="admin", actor_id=actor_id,
        ))
        s.commit()
        return _pair_to_dict(s.query(ExchangeRatePair).filter_by(id=pair_id).first())


def set_pair_margin(pair_id: int, margin_pct: float, *,
                    actor_id: Optional[int] = None) -> Dict[str, Any]:
    """Update the spread margin for a pair."""
    with get_db_session() as s:
        pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
        if not pair:
            raise ValueError(f"Pair #{pair_id} not found")
        pair.margin_pct = float(margin_pct)
        if pair.mid_rate:
            buy_r, sell_r = _apply_margin(float(pair.mid_rate), float(margin_pct))
            pair.buy_rate = buy_r
            pair.sell_rate = sell_r
        s.add(ExchangeRateLog(
            pair_id=pair.id, action="margin_updated",
            old_rate=float(pair.margin_pct or 0), new_rate=float(margin_pct),
            actor_type="admin", actor_id=actor_id,
        ))
        s.commit()
        return _pair_to_dict(s.query(ExchangeRatePair).filter_by(id=pair_id).first())


def get_pair_history(pair_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent rate history for a pair."""
    out = []
    with get_db_session() as s:
        rows = (s.query(ExchangeRateHistory)
                .filter_by(pair_id=pair_id)
                .order_by(ExchangeRateHistory.recorded_at.desc())
                .limit(limit).all())
        for r in rows:
            out.append({
                "id": r.id,
                "mid_rate": float(r.mid_rate or 0),
                "buy_rate": float(r.buy_rate or 0),
                "sell_rate": float(r.sell_rate or 0),
                "source": r.source,
                "recorded_at": r.recorded_at,
            })
    return out


# ─── Auto-update logic ────────────────────────────────────────────────────────

def refresh_pair(pair_id: int, *, force: bool = False, actor_id: Optional[int] = None,
                 actor_type: str = "system") -> Dict[str, Any]:
    """Fetch and apply the latest rate for a single pair.

    Skips locked pairs unless force=True.
    """
    with get_db_session() as s:
        pair = s.query(ExchangeRatePair).filter_by(id=pair_id, is_active=True).first()
        if not pair:
            raise ValueError(f"Pair #{pair_id} not found or inactive")
        if pair.is_locked and not force:
            return _pair_to_dict(pair)

        if pair.rate_source == ExchangeRateSource.MANUAL.value and not force:
            return _pair_to_dict(pair)

        new_rate = _fetch_pair_rate(
            pair.from_currency, pair.to_currency,
            api_url=pair.api_url, api_response_path=pair.api_response_path
        )
        if new_rate is None or new_rate <= 0:
            pair.last_update_error = f"API returned no rate at {datetime.utcnow().isoformat()}"
            pair.failed_updates_today = (pair.failed_updates_today or 0) + 1
            s.commit()
            return _pair_to_dict(pair)

        old_rate = float(pair.mid_rate or 0)
        buy_r, sell_r = _apply_margin(new_rate, float(pair.margin_pct or 0))

        pair.previous_mid_rate = old_rate or pair.previous_mid_rate
        pair.mid_rate = new_rate
        pair.buy_rate = buy_r
        pair.sell_rate = sell_r
        pair.last_updated = datetime.utcnow()
        pair.last_auto_update = datetime.utcnow()
        pair.last_update_source = ExchangeRateSource.AUTO_API.value
        pair.last_update_error = None
        pair.updates_today = (pair.updates_today or 0) + 1

        s.add(ExchangeRateLog(
            pair_id=pair.id, action="auto_update",
            old_rate=old_rate, new_rate=new_rate,
            actor_type=actor_type, actor_id=actor_id,
        ))
        s.add(ExchangeRateHistory(
            pair_id=pair.id,
            from_currency=pair.from_currency,
            to_currency=pair.to_currency,
            mid_rate=new_rate, buy_rate=buy_r, sell_rate=sell_r,
            margin_pct=pair.margin_pct,
            source=ExchangeRateSource.AUTO_API.value,
        ))
        s.commit()
        return _pair_to_dict(s.query(ExchangeRatePair).filter_by(id=pair_id).first())


def refresh_all_pairs(force: bool = False) -> Dict[str, Any]:
    """Refresh all active, unlocked auto-update pairs. Returns summary."""
    success = 0
    skipped = 0
    failed  = 0

    with get_db_session() as s:
        pairs = s.query(ExchangeRatePair).filter(
            ExchangeRatePair.is_active == True,  # noqa: E712
            ExchangeRatePair.status == ExchangeRatePairStatus.ENABLED.value,
        ).all()
        pair_ids = [p.id for p in pairs]

    for pid in pair_ids:
        try:
            with get_db_session() as s:
                pair = s.query(ExchangeRatePair).filter_by(id=pid).first()
                if not pair:
                    skipped += 1
                    continue
                if pair.is_locked and not force:
                    skipped += 1
                    continue
                if pair.rate_source == ExchangeRateSource.MANUAL.value and not force:
                    skipped += 1
                    continue
                # Check interval
                if not force and pair.last_auto_update:
                    interval_min = pair.auto_update_interval or 60
                    next_update = pair.last_auto_update + timedelta(minutes=interval_min)
                    if datetime.utcnow() < next_update:
                        skipped += 1
                        continue
            refresh_pair(pid, force=force)
            success += 1
        except Exception as e:
            logger.warning("refresh_pair(%d) failed: %s", pid, e)
            failed += 1

    return {"success": success, "skipped": skipped, "failed": failed,
            "total": success + skipped + failed}


def reset_daily_counters() -> None:
    """Reset updates_today / failed_updates_today. Call once per day."""
    try:
        with get_db_session() as s:
            s.query(ExchangeRatePair).update(
                {ExchangeRatePair.updates_today: 0,
                 ExchangeRatePair.failed_updates_today: 0},
                synchronize_session=False,
            )
            s.commit()
    except Exception:
        logger.exception("reset_daily_counters failed")


def get_dashboard_stats() -> Dict[str, Any]:
    """Return stats for the admin exchange rate dashboard."""
    try:
        with get_db_session() as s:
            total = s.query(ExchangeRatePair).count()
            active = s.query(ExchangeRatePair).filter(
                ExchangeRatePair.is_active == True  # noqa: E712
            ).count()
            updates_today = (
                s.query(ExchangeRatePair)
                .with_entities(
                    __import__('sqlalchemy').func.sum(ExchangeRatePair.updates_today)
                ).scalar() or 0
            )
            failed_today = (
                s.query(ExchangeRatePair)
                .with_entities(
                    __import__('sqlalchemy').func.sum(ExchangeRatePair.failed_updates_today)
                ).scalar() or 0
            )
            locked = s.query(ExchangeRatePair).filter(
                ExchangeRatePair.is_locked == True  # noqa: E712
            ).count()
        return {
            "total_pairs": total,
            "active_pairs": active,
            "updates_today": int(updates_today),
            "failed_updates_today": int(failed_today),
            "locked_pairs": locked,
        }
    except Exception:
        logger.exception("get_dashboard_stats failed")
        return {}


# ─── Scheduler job ────────────────────────────────────────────────────────────

async def exchange_rate_scheduler_job(context) -> None:
    """Telegram job-queue callback: refresh all due auto-update pairs."""
    try:
        result = refresh_all_pairs(force=False)
        if result["success"]:
            logger.info("Exchange rate refresh: %s", result)
    except Exception:
        logger.exception("exchange_rate_scheduler_job failed")
