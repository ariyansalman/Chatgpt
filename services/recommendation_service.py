"""V45 — Product Recommendation Engine.

Provides recommendation lists for user-facing flows and an admin panel to
pin manual recommendations. All computation is done against existing tables
(orders, order_items, recently_viewed, products) — no new analytics tables.

Recommendation types:
  trending        — products with most orders in the last 30 days
  best_sellers    — products ranked by all-time sales_count
  related         — products in the same category
  also_bought     — products bought by users who also bought product X
  fbt             — frequently bought together (co-occurrence in same order)
  for_you         — personalised: based on user's order + view history
  recently_viewed — (delegates to existing RecentlyViewed rows)
  pinned          — admin-pinned recommendations for a product/section

Public API (sync):
  get_trending(limit) -> list[dict]
  get_best_sellers(limit) -> list[dict]
  get_related(product_id, limit) -> list[dict]
  get_also_bought(product_id, limit) -> list[dict]
  get_fbt(product_id, limit) -> list[dict]
  get_for_you(user_id, limit) -> list[dict]
  get_recently_viewed(user_id, limit) -> list[dict]
  get_pinned(section, product_id) -> list[dict]

Admin API:
  pin_recommendation(admin_id, section, product_id, recommended_product_id,
                     display_order) -> bool
  unpin_recommendation(pin_id, admin_id) -> bool
  list_pins(section, product_id) -> list[dict]
  get_recommendation_stats() -> dict
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, distinct

from database import get_db_session
from database.models import (
    Product, Order, OrderItem, OrderStatus, RecentlyViewed,
    ProductRecommendationPin,
)
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _product_dict(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "price": product.price,
        "sale_price": product.sale_price,
        "stock_count": product.stock_count,
        "is_active": product.is_active,
        "sales_count": product.sales_count,
        "currency": getattr(product, "currency", "USD"),
        "product_emoji": getattr(product, "product_emoji", None),
        "category_id": product.category_id,
    }


def _active_products_query(s):
    return s.query(Product).filter(Product.is_active == True, Product.stock_count > 0)


# ─── Trending ─────────────────────────────────────────────────────────────────

def get_trending(limit: int = 10) -> list[dict]:
    """Products with the most completed orders in the last 30 days."""
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        with get_db_session() as s:
            rows = (
                s.query(Product, func.count(OrderItem.id).label("cnt"))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(
                    Order.status == OrderStatus.COMPLETED,
                    Order.created_at >= cutoff,
                    Product.is_active == True,
                    Product.stock_count > 0,
                )
                .group_by(Product.id)
                .order_by(func.count(OrderItem.id).desc())
                .limit(limit)
                .all()
            )
            return [{"order_count": cnt, **_product_dict(p)} for p, cnt in rows]
    except Exception:
        logger.exception("get_trending failed")
        return []


# ─── Best Sellers ─────────────────────────────────────────────────────────────

def get_best_sellers(limit: int = 10) -> list[dict]:
    """Products ranked by all-time sales_count (denormalised counter)."""
    try:
        with get_db_session() as s:
            rows = (_active_products_query(s)
                    .order_by(Product.sales_count.desc(), Product.id.asc())
                    .limit(limit).all())
            return [_product_dict(p) for p in rows]
    except Exception:
        logger.exception("get_best_sellers failed")
        return []


# ─── Related Products ─────────────────────────────────────────────────────────

def get_related(product_id: int, limit: int = 8) -> list[dict]:
    """Products in the same category, excluding the given product."""
    try:
        with get_db_session() as s:
            product = s.query(Product).get(product_id)
            if not product or not product.category_id:
                return []
            rows = (
                _active_products_query(s)
                .filter(
                    Product.category_id == product.category_id,
                    Product.id != product_id,
                )
                .order_by(Product.sales_count.desc(), Product.id.asc())
                .limit(limit).all()
            )
            return [_product_dict(p) for p in rows]
    except Exception:
        logger.exception("get_related failed pid=%s", product_id)
        return []


# ─── Customers Also Bought ────────────────────────────────────────────────────

def get_also_bought(product_id: int, limit: int = 8) -> list[dict]:
    """Products bought by users who also bought product_id."""
    try:
        with get_db_session() as s:
            # Users who bought this product
            buyer_subq = (
                s.query(distinct(Order.user_id))
                .join(OrderItem, OrderItem.order_id == Order.id)
                .filter(
                    OrderItem.product_id == product_id,
                    Order.status == OrderStatus.COMPLETED,
                )
                .subquery()
            )
            # Other products they bought
            rows = (
                s.query(Product, func.count(OrderItem.id).label("cnt"))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(
                    Order.user_id.in_(buyer_subq),
                    Order.status == OrderStatus.COMPLETED,
                    OrderItem.product_id != product_id,
                    Product.is_active == True,
                    Product.stock_count > 0,
                )
                .group_by(Product.id)
                .order_by(func.count(OrderItem.id).desc())
                .limit(limit).all()
            )
            return [{"co_buy_count": cnt, **_product_dict(p)} for p, cnt in rows]
    except Exception:
        logger.exception("get_also_bought failed pid=%s", product_id)
        return []


# ─── Frequently Bought Together ───────────────────────────────────────────────

def get_fbt(product_id: int, limit: int = 6) -> list[dict]:
    """Products most frequently in the same order as product_id."""
    try:
        with get_db_session() as s:
            # Orders that contain this product
            order_subq = (
                s.query(distinct(OrderItem.order_id))
                .filter(OrderItem.product_id == product_id)
                .subquery()
            )
            rows = (
                s.query(Product, func.count(OrderItem.id).label("cnt"))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .filter(
                    OrderItem.order_id.in_(order_subq),
                    OrderItem.product_id != product_id,
                    Product.is_active == True,
                    Product.stock_count > 0,
                )
                .group_by(Product.id)
                .order_by(func.count(OrderItem.id).desc())
                .limit(limit).all()
            )
            return [{"fbt_count": cnt, **_product_dict(p)} for p, cnt in rows]
    except Exception:
        logger.exception("get_fbt failed pid=%s", product_id)
        return []


# ─── For You (personalised) ───────────────────────────────────────────────────

def get_for_you(user_id: int, limit: int = 10) -> list[dict]:
    """Personalised: products from categories the user has bought/viewed,
    excluding products they already own."""
    try:
        with get_db_session() as s:
            # Categories user has purchased from
            purchased_cats = (
                s.query(distinct(Product.category_id))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.user_id == user_id, Order.status == OrderStatus.COMPLETED,
                        Product.category_id.isnot(None))
                .all()
            )
            cat_ids = [r[0] for r in purchased_cats]

            # Categories user has recently viewed
            viewed_cats = (
                s.query(distinct(Product.category_id))
                .join(RecentlyViewed, RecentlyViewed.product_id == Product.id)
                .filter(RecentlyViewed.user_id == user_id,
                        Product.category_id.isnot(None))
                .all()
            )
            cat_ids.extend([r[0] for r in viewed_cats])
            cat_ids = list(set(filter(None, cat_ids)))

            # Products already purchased by this user
            owned_subq = (
                s.query(distinct(OrderItem.product_id))
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.user_id == user_id, Order.status == OrderStatus.COMPLETED)
                .subquery()
            )

            if cat_ids:
                rows = (
                    _active_products_query(s)
                    .filter(
                        Product.category_id.in_(cat_ids),
                        Product.id.notin_(owned_subq),
                    )
                    .order_by(Product.sales_count.desc())
                    .limit(limit).all()
                )
            else:
                # No history — fall back to trending
                return get_trending(limit)

            if len(rows) < limit:
                # Pad with trending if needed
                existing_ids = {p["id"] for p in [_product_dict(r) for r in rows]}
                trending = get_trending(limit * 2)
                for t in trending:
                    if t["id"] not in existing_ids:
                        rows_extra = [t]
                        if len(rows) + len(rows_extra) >= limit:
                            break

            return [_product_dict(p) for p in rows][:limit]
    except Exception:
        logger.exception("get_for_you failed uid=%s", user_id)
        return get_trending(limit)


# ─── Recently Viewed ──────────────────────────────────────────────────────────

def get_recently_viewed(user_id: int, limit: int = 10) -> list[dict]:
    """Products the user has recently viewed (uses existing RecentlyViewed model)."""
    try:
        with get_db_session() as s:
            rows = (
                s.query(Product)
                .join(RecentlyViewed, RecentlyViewed.product_id == Product.id)
                .filter(RecentlyViewed.user_id == user_id, Product.is_active == True)
                .order_by(RecentlyViewed.viewed_at.desc())
                .limit(limit).all()
            )
            return [_product_dict(p) for p in rows]
    except Exception:
        logger.exception("get_recently_viewed failed uid=%s", user_id)
        return []


# ─── Admin-Pinned Recommendations ────────────────────────────────────────────

def pin_recommendation(admin_id: int, section: str, product_id: Optional[int],
                       recommended_product_id: int, display_order: int = 0) -> bool:
    """Pin a product recommendation. section examples: 'home', 'trending', product.<id>."""
    try:
        with get_db_session() as s:
            # Prevent duplicate pins
            existing = (s.query(ProductRecommendationPin)
                        .filter_by(section=section, product_id=product_id,
                                   recommended_product_id=recommended_product_id)
                        .first())
            if existing:
                return False
            pin = ProductRecommendationPin(
                admin_id=admin_id,
                section=section,
                product_id=product_id,
                recommended_product_id=recommended_product_id,
                display_order=display_order,
            )
            s.add(pin)
            s.commit()
            log_admin_action(admin_id, "recommendation_pin",
                             target_type="product", target_id=recommended_product_id,
                             details=f"section={section} pid={product_id}",
                             module="recommendations")
            return True
    except Exception:
        logger.exception("pin_recommendation failed")
        return False


def unpin_recommendation(pin_id: int, admin_id: int) -> bool:
    try:
        with get_db_session() as s:
            pin = s.query(ProductRecommendationPin).get(pin_id)
            if not pin:
                return False
            s.delete(pin)
            s.commit()
            log_admin_action(admin_id, "recommendation_unpin",
                             target_type="recommendation_pin", target_id=pin_id,
                             module="recommendations")
            return True
    except Exception:
        logger.exception("unpin_recommendation failed pid=%s", pin_id)
        return False


def list_pins(section: Optional[str] = None,
              product_id: Optional[int] = None) -> list[dict]:
    try:
        with get_db_session() as s:
            q = (s.query(ProductRecommendationPin, Product)
                 .join(Product, Product.id == ProductRecommendationPin.recommended_product_id)
                 .order_by(ProductRecommendationPin.display_order.asc(),
                            ProductRecommendationPin.created_at.desc()))
            if section:
                q = q.filter(ProductRecommendationPin.section == section)
            if product_id is not None:
                q = q.filter(ProductRecommendationPin.product_id == product_id)
            rows = q.all()
            return [
                {
                    "pin_id": pin.id,
                    "section": pin.section,
                    "product_id": pin.product_id,
                    "display_order": pin.display_order,
                    "created_at": pin.created_at,
                    **_product_dict(product),
                }
                for pin, product in rows
            ]
    except Exception:
        logger.exception("list_pins failed")
        return []


def get_pinned(section: str, product_id: Optional[int] = None) -> list[dict]:
    """Return pinned recommendations for a section (user-facing)."""
    return list_pins(section=section, product_id=product_id)


def get_recommendation_stats() -> dict:
    try:
        with get_db_session() as s:
            total_pins = s.query(ProductRecommendationPin).count()
            sections = (s.query(ProductRecommendationPin.section,
                                func.count(ProductRecommendationPin.id))
                        .group_by(ProductRecommendationPin.section)
                        .all())
            return {
                "total_pins": total_pins,
                "sections": {sec: cnt for sec, cnt in sections},
            }
    except Exception:
        logger.exception("get_recommendation_stats failed")
        return {"total_pins": 0, "sections": {}}
