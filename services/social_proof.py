"""Social proof counters shown on every product detail page.

    ⭐ 4.8 (120 reviews) • 500+ sold

Both numbers come from live aggregate queries against the existing
``Review`` and ``Order``/``OrderItem`` models — no new columns needed:

  * average rating + review count  <- Review (excluding hidden reviews)
  * total sold                     <- sum(OrderItem.quantity) for orders
                                       whose Order.status == COMPLETED

On a catalog with a lot of products/orders these two aggregate queries per
page view can add up, so results are cached in-process for a configurable
TTL (default 5 minutes). Admins can tune or disable this from the panel:

    BotConfig -> "catalog" category
      * social_proof_cache_enabled  (bool, default True)
      * social_proof_cache_seconds  (int,  default 300)

Usage:
    from services.social_proof import get_social_proof
    proof = get_social_proof(product.id)
    line = proof.format()   # "" if there's nothing to show yet
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Dict, Tuple

from sqlalchemy import func

from database import get_db_session
from database.models import Order, OrderItem, OrderStatus, Review
from utils.bot_config import cfg


@dataclass
class SocialProof:
    avg_rating: float
    review_count: int
    sold_count: int

    def format(self) -> str:
        """Render as '⭐ 4.8 (120 reviews) • 500+ sold', skipping empty parts."""
        parts = []
        if self.review_count > 0:
            noun = "review" if self.review_count == 1 else "reviews"
            parts.append(f"⭐ {self.avg_rating:.1f} ({self.review_count} {noun})")
        if self.sold_count > 0:
            parts.append(f"{self._sold_label()} sold")
        return " • ".join(parts)

    def _sold_label(self) -> str:
        """Bucket into a '500+' style label once past 10 units (avoids a
        counter that looks stale/exact and matches common storefront UX)."""
        n = self.sold_count
        if n < 10:
            return str(n)
        for step in (1000, 500, 100, 50, 10):
            if n >= step:
                return f"{step}+"
        return str(n)


# ---------------------------------------------------------------------------
# In-process cache: {product_id: (computed_at_epoch, SocialProof)}
# ---------------------------------------------------------------------------
_cache: Dict[int, Tuple[float, SocialProof]] = {}
_lock = Lock()


def _compute(product_id: int) -> SocialProof:
    with get_db_session() as s:
        rating_row = (
            s.query(func.avg(Review.rating), func.count(Review.id))
            .filter(Review.product_id == product_id, Review.is_hidden == False)  # noqa: E712
            .first()
        )
        avg = float(rating_row[0]) if rating_row and rating_row[0] is not None else 0.0
        review_count = int(rating_row[1] or 0)

        sold_row = (
            s.query(func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .filter(OrderItem.product_id == product_id, Order.status == OrderStatus.COMPLETED)
            .first()
        )
        sold = int(sold_row[0] or 0)
    return SocialProof(avg_rating=avg, review_count=review_count, sold_count=sold)


def get_social_proof(product_id: int, *, force_refresh: bool = False) -> SocialProof:
    """Return this product's rating/sold numbers, using the cache when enabled.

    Cache is admin-configurable (see module docstring). If disabled, or TTL
    is 0, always computes fresh.
    """
    enabled = cfg.get_bool("social_proof_cache_enabled", True)
    ttl = max(0, cfg.get_int("social_proof_cache_seconds", 300))

    if force_refresh or not enabled or ttl == 0:
        proof = _compute(product_id)
        if enabled and ttl > 0:
            with _lock:
                _cache[product_id] = (time.time(), proof)
        return proof

    with _lock:
        cached = _cache.get(product_id)
    if cached and (time.time() - cached[0]) < ttl:
        return cached[1]

    proof = _compute(product_id)
    with _lock:
        _cache[product_id] = (time.time(), proof)
    return proof


def invalidate(product_id: int) -> None:
    """Drop one product's cached entry — call after a new review is posted
    or an order completes, so the counter updates immediately instead of
    waiting out the TTL."""
    with _lock:
        _cache.pop(product_id, None)


def clear_cache() -> None:
    """Drop the entire cache (e.g. after an admin changes the TTL/toggle)."""
    with _lock:
        _cache.clear()
