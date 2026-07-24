"""Product badges — Section 14.

Badges are derived at render-time from real data:
  * FEATURED    — admin flag ``Product.is_featured``
  * BEST_SELLER — top-N by ``sales_count`` within the configurable window
  * NEW         — created_at newer than ``new_product_days`` (bot config)
  * SALE        — effective price < base price

We compute them in one small helper so listings and detail views agree.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Set

from database import get_db_session
from database.models import Product
from utils.bot_config import cfg


BADGE_LABELS = {
    "featured":    "⭐ Featured",
    "best_seller": "🔥 Best Seller",
    "new":         "🆕 New",
    "sale":        "💸 Sale",
}


@dataclass
class BadgeContext:
    best_seller_ids: Set[int]
    new_cutoff: datetime


def build_context() -> BadgeContext:
    top_n = max(1, cfg.get_int("best_seller_top_n", 10))
    with get_db_session() as s:
        rows = (
            s.query(Product.id)
            .filter(Product.is_active == True)  # noqa: E712
            .filter(Product.sales_count > 0)
            .order_by(Product.sales_count.desc())
            .limit(top_n)
            .all()
        )
    best_ids = {r[0] for r in rows}
    days = max(1, cfg.get_int("new_product_days", 7))
    cutoff = datetime.utcnow() - timedelta(days=days)
    return BadgeContext(best_seller_ids=best_ids, new_cutoff=cutoff)


def badges_for(product: Product, ctx: BadgeContext | None = None) -> List[str]:
    if ctx is None:
        ctx = build_context()
    out: List[str] = []
    if getattr(product, "is_featured", False):
        out.append(BADGE_LABELS["featured"])
    if product.id in ctx.best_seller_ids:
        out.append(BADGE_LABELS["best_seller"])
    if product.created_at and product.created_at >= ctx.new_cutoff:
        out.append(BADGE_LABELS["new"])
    sale = getattr(product, "sale_price", None)
    if sale and sale > 0 and sale < (product.price or 0):
        out.append(BADGE_LABELS["sale"])
    return out


def badge_line(product: Product, ctx: BadgeContext | None = None) -> str:
    """Return badges joined with spaces, or empty string when none apply."""
    b = badges_for(product, ctx)
    return "  ".join(b)
