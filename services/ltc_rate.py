"""Fetch a live LTC/USD exchange rate.

Primary source : CoinGecko public API (no API key required).
Fallback source: Bybit spot market ticker (LTCUSDT).

A short in-process TTL cache (60 s) prevents hammering the upstream APIs
when multiple orders are created in quick succession, while still ensuring
each order locks a fresh, current rate.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_CACHE_TTL = 60  # seconds
_cache: Tuple[Optional[Decimal], float] = (None, 0.0)


# ─── individual source fetchers ──────────────────────────────────────────────

def _fetch_coingecko() -> Decimal:
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "litecoin", "vs_currencies": "usd"},
        timeout=8,
    )
    resp.raise_for_status()
    price = resp.json()["litecoin"]["usd"]
    if not price:
        raise ValueError("CoinGecko returned empty price")
    return Decimal(str(price))


def _fetch_bybit_ticker() -> Decimal:
    resp = requests.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "spot", "symbol": "LTCUSDT"},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    price_str = data["result"]["list"][0]["lastPrice"]
    if not price_str:
        raise ValueError("Bybit ticker returned empty price")
    return Decimal(price_str)


# ─── public API ──────────────────────────────────────────────────────────────

def get_ltc_usd_rate() -> Decimal:
    """Return the current LTC/USD price (USD per 1 LTC).

    Tries CoinGecko first, then falls back to Bybit's spot ticker.
    Results are cached for up to 60 seconds so rapid consecutive orders
    reuse the same rate without extra network calls.

    Raises RuntimeError if both sources fail — the caller should surface
    an appropriate error to the user rather than silently failing.
    """
    global _cache
    rate, fetched_at = _cache
    if rate is not None and (time.monotonic() - fetched_at) < _CACHE_TTL:
        logger.debug("LTC/USD rate served from cache: %s", rate)
        return rate

    for source_name, fetch_fn in [("CoinGecko", _fetch_coingecko), ("Bybit ticker", _fetch_bybit_ticker)]:
        try:
            rate = fetch_fn()
            if rate <= 0:
                raise ValueError(f"Non-positive rate: {rate}")
            _cache = (rate, time.monotonic())
            logger.info("LTC/USD rate fetched from %s: %s", source_name, rate)
            return rate
        except Exception as exc:
            logger.warning("LTC/USD rate fetch failed (%s): %s", source_name, exc)

    raise RuntimeError(
        "Could not fetch LTC/USD rate from CoinGecko or Bybit. "
        "Please check the network and try again."
    )
