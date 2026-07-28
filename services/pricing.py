"""Centralized pricing service (V10).

Single source of truth for price computation across product screen, cart,
Buy-Now, checkout, and order creation. Prevents accidental double discounts
and produces a snapshot suitable for persistence on OrderItem.

Precedence (documented):
    1. Base price = variant.price if variant else product.price
    2. Active sale/promotion price (product.discount_price if set & < base)
    3. Bulk pricing (product.bulk_price_qty / bulk_price if configured)
    4. Reseller tier discount_pct
    5. Coupon discount (applied to order subtotal, not per line)

Coupon is intentionally computed at the order level, not here — this
function reports the pre-coupon effective unit price and lets the caller
apply coupon math against subtotal to keep behaviour identical to the
existing coupon handler.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional
import json
import logging

import requests

from database import get_db_session
from database.models import (
    Product, ProductVariant, ResellerTier, UserReseller, User, Settings, FlashSale,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# V12 (Multi-Currency): USD <-> BDT exchange rate handling.
#
# The store's canonical numbers (Product.price, wallet_balance, Order.total_
# amount) stay in each product's native currency / USD respectively — this
# section only answers "what is 1 USD worth in BDT right now?" so the rest
# of the app (utils/currency.py, wallet_handlers.py, user_handlers.py) can
# convert amounts for display or for a same-currency price quote.
#
# Two supported sources, chosen via Settings.exchange_rate_mode:
#   - "fixed" (default): admin sets Settings.usd_to_bdt_rate manually.
#   - "api":   fetched from Settings.exchange_rate_api_url on a short TTL,
#              cached in-process, and persisted to
#              Settings.exchange_rate_last_value/_last_synced so a failed
#              fetch can fall back to the last known-good rate instead of
#              jumping straight to the static fixed rate.
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_USD_TO_BDT_RATE = 110.0
_RATE_CACHE_TTL_SECONDS = 300  # 5 minutes
_rate_cache = {"rate": None, "ts": None}


def _fetch_rate_from_api(api_url: str) -> Optional[float]:
    """Fetch the USD->BDT rate from a configured exchange-rate API.

    Accepts a few common response shapes:
      - {"rates": {"BDT": 110.5, ...}}   (exchangerate-api.com / open.er-api.com style)
      - {"BDT": 110.5}                    (flat mapping)
      - {"conversion_rate": 110.5}        (single-pair endpoints)
    Returns None on any network error, bad response, or missing rate —
    callers must fall back to a cached / fixed rate on None.
    """
    try:
        resp = requests.get(api_url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        rate = None
        if isinstance(data, dict):
            rates = data.get("rates")
            if isinstance(rates, dict) and "BDT" in rates:
                rate = rates["BDT"]
            elif "BDT" in data:
                rate = data["BDT"]
            elif "conversion_rate" in data:
                rate = data["conversion_rate"]
        return float(rate) if rate else None
    except Exception:
        logger.warning("Exchange rate API fetch failed for %s", api_url, exc_info=True)
        return None


def get_usd_to_bdt_rate(force_refresh: bool = False) -> float:
    """Return the current USD->BDT rate (1 USD = <rate> BDT).

    Cached in-process for `_RATE_CACHE_TTL_SECONDS` to avoid hammering the
    API (or the DB) on every price render. Pass `force_refresh=True` to
    bypass the cache, e.g. right after an admin updates the config.
    """
    now = datetime.utcnow()
    if not force_refresh and _rate_cache["rate"] and _rate_cache["ts"] and \
            (now - _rate_cache["ts"]).total_seconds() < _RATE_CACHE_TTL_SECONDS:
        return _rate_cache["rate"]

    with get_db_session() as s:
        cfg = s.query(Settings).first()
        mode = ((cfg.exchange_rate_mode if cfg else None) or "fixed").lower()
        fixed_rate = float(cfg.usd_to_bdt_rate) if cfg and cfg.usd_to_bdt_rate else DEFAULT_USD_TO_BDT_RATE
        api_url = (cfg.exchange_rate_api_url if cfg else None) or None
        last_good = float(cfg.exchange_rate_last_value) if cfg and cfg.exchange_rate_last_value else None

    rate = fixed_rate
    if mode == "api" and api_url:
        fetched = _fetch_rate_from_api(api_url)
        if fetched and fetched > 0:
            rate = fetched
            try:
                with get_db_session() as s:
                    row = s.query(Settings).first()
                    if row:
                        row.exchange_rate_last_value = fetched
                        row.exchange_rate_last_synced = datetime.utcnow()
            except Exception:
                logger.warning("Failed to persist fetched exchange rate", exc_info=True)
        elif last_good and last_good > 0:
            # API is down right now — prefer the last known-good live rate
            # over the static fixed fallback.
            rate = last_good

    _rate_cache["rate"] = rate
    _rate_cache["ts"] = now
    return rate


def clear_rate_cache() -> None:
    _rate_cache["rate"] = None
    _rate_cache["ts"] = None


def convert_currency(amount: float, from_currency: str, to_currency: str) -> float:
    """Convert `amount` between the two supported currencies (USD, BDT)."""
    from_currency = (from_currency or "USD").upper()
    to_currency = (to_currency or "USD").upper()
    if from_currency == to_currency:
        return round(float(amount), 2)

    rate = get_usd_to_bdt_rate()  # 1 USD = rate BDT
    if from_currency == "USD" and to_currency == "BDT":
        return round(float(amount) * rate, 2)
    if from_currency == "BDT" and to_currency == "USD":
        return round(float(amount) / rate, 2) if rate else round(float(amount), 2)

    # Unsupported pair — return unconverted rather than raising, callers
    # display prices even if a future currency isn't wired into the rate math yet.
    return round(float(amount), 2)


@dataclass
class PriceQuote:
    base_price: float
    sale_price: Optional[float]
    bulk_price: Optional[float]
    reseller_discount_pct: float
    effective_unit_price: float
    quantity: int
    subtotal: float
    reseller_tier_id: Optional[int]
    reseller_tier_name: Optional[str]
    currency: str = "USD"  # V12: currency the product's price fields are stored in
    # V15: Flash Sales — only populated when a flash sale actually won out
    # over any static product.sale_price for this line.
    flash_sale_id: Optional[int] = None
    flash_sale_percent: Optional[float] = None
    flash_sale_ends_at: Optional[str] = None

    def as_meta_json(self) -> str:
        return json.dumps(asdict(self), default=float, sort_keys=True)


# ══════════════════════════════════════════════════════════════════════════
# V15 (Flash Sales): time-boxed % discounts on a product or a whole category.
#
# A flash sale never stacks with a product's static ``sale_price`` — whenever
# both could apply, whichever works out cheaper for the customer wins (see
# the precedence block inside ``quote()``). Bulk pricing / reseller tiers /
# coupons still apply normally on top, exactly as they would on any other
# effective price.
# ══════════════════════════════════════════════════════════════════════════

def get_active_flash_sale(session, product: "Product") -> Optional["FlashSale"]:
    """Return the currently-live FlashSale applicable to ``product``, or None.

    Checks a product-level sale first; falls back to a category-level sale
    covering ``product.category_id`` when no product-level sale is live.
    """
    if product is None:
        return None
    now = datetime.utcnow()
    fs = (
        session.query(FlashSale)
        .filter(
            FlashSale.product_id == product.id,
            FlashSale.is_active == True,  # noqa: E712
            FlashSale.start_time <= now,
            FlashSale.end_time > now,
        )
        .order_by(FlashSale.discount_percent.desc())
        .first()
    )
    if fs is not None:
        return fs

    if product.category_id:
        fs = (
            session.query(FlashSale)
            .filter(
                FlashSale.category_id == product.category_id,
                FlashSale.is_active == True,  # noqa: E712
                FlashSale.start_time <= now,
                FlashSale.end_time > now,
            )
            .order_by(FlashSale.discount_percent.desc())
            .first()
        )
    return fs


def flash_sale_price(base_price: float, flash_sale: "FlashSale") -> float:
    """Apply a flash sale's discount percent to ``base_price``."""
    pct = max(0.0, min(100.0, float(flash_sale.discount_percent or 0.0)))
    return round(float(base_price) * (1.0 - pct / 100.0), 4)


def get_flash_sale_for_display(product_id: int) -> Optional[dict]:
    """Standalone helper (opens its own session) for banners/badges in the
    user-facing handlers. Returns a plain dict (safe to use after the DB
    session closes), or None when nothing is live for this product."""
    with get_db_session() as s:
        product = s.get(Product, product_id)
        if not product:
            return None
        fs = get_active_flash_sale(s, product)
        if not fs:
            return None
        base = float(product.price)
        return {
            "id": fs.id,
            "discount_percent": float(fs.discount_percent),
            "original_price": base,
            "sale_price": flash_sale_price(base, fs),
            "end_time": fs.end_time,
            "start_time": fs.start_time,
            "label": fs.label,
            "scope": "product" if fs.product_id else "category",
        }


def _get_reseller(session, user_id: int):
    """Return (tier, discount_pct, min_qty) or (None, 0, 1)."""
    ur = session.query(UserReseller).filter_by(user_id=user_id).first()
    if not ur:
        return None, 0.0, 1
    tier = session.get(ResellerTier, ur.tier_id)
    if not tier or not tier.is_active:
        return None, 0.0, 1
    return tier, float(tier.discount_pct or 0.0), int(tier.min_quantity or 1)


def quote(user_id: Optional[int],
          product_id: int,
          quantity: int,
          variant_id: Optional[int] = None) -> PriceQuote:
    """Compute an authoritative price quote for the given line."""
    if quantity <= 0:
        raise ValueError("quantity must be > 0")

    with get_db_session() as s:
        product = s.get(Product, product_id)
        if product is None:
            raise ValueError(f"product {product_id} not found")

        variant = s.get(ProductVariant, variant_id) if variant_id else None
        base = float(variant.price if variant else product.price)

        # Sale price (product.discount_price if lower than base and > 0)
        sale = getattr(product, "discount_price", None)
        if sale is not None and sale > 0 and sale < base:
            after_sale = float(sale)
        else:
            after_sale = base
            sale = None

        # V15: Flash sale — time-boxed discount on this product or its
        # category. Never stacks with the static sale price above; whichever
        # is cheaper for the customer wins.
        flash_sale = get_active_flash_sale(s, product)
        flash_sale_applied = None
        if flash_sale is not None:
            fs_price = flash_sale_price(base, flash_sale)
            if fs_price < after_sale:
                after_sale = fs_price
                flash_sale_applied = flash_sale

        # Bulk pricing: product.bulk_price_qty / product.bulk_price
        bulk_qty = getattr(product, "bulk_price_qty", None)
        bulk_price = getattr(product, "bulk_price", None)
        if bulk_qty and bulk_price and quantity >= int(bulk_qty) and bulk_price < after_sale:
            after_bulk = float(bulk_price)
        else:
            after_bulk = after_sale
            bulk_price = None

        # Reseller tier
        tier = None
        r_pct = 0.0
        if user_id is not None:
            user = s.query(User).filter_by(telegram_id=user_id).first() \
                   or s.get(User, user_id)
            if user:
                tier, r_pct, _min_qty = _get_reseller(s, user.id)

        effective = after_bulk * (1.0 - (r_pct / 100.0)) if r_pct > 0 else after_bulk
        effective = round(max(effective, 0.0), 4)
        subtotal = round(effective * quantity, 4)

        return PriceQuote(
            base_price=base,
            sale_price=sale,
            bulk_price=bulk_price,
            reseller_discount_pct=r_pct,
            effective_unit_price=effective,
            quantity=quantity,
            subtotal=subtotal,
            reseller_tier_id=(tier.id if tier else None),
            reseller_tier_name=(tier.name if tier else None),
            currency=(getattr(product, "currency", None) or "USD"),
            flash_sale_id=(flash_sale_applied.id if flash_sale_applied else None),
            flash_sale_percent=(float(flash_sale_applied.discount_percent) if flash_sale_applied else None),
            flash_sale_ends_at=(flash_sale_applied.end_time.isoformat() if flash_sale_applied else None),
        )
