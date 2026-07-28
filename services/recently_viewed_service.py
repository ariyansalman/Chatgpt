"""Recently Viewed Products Service — V23.

Tracks every product a user opens and surfaces a rich "🕒 Recently Viewed"
list with pagination, search and per-item actions.

BotConfig keys consumed:
    feature_recently_viewed_enabled       — master bool toggle
    recently_viewed_status                — "enabled" / "maintenance" / "disabled"
    feature_recently_viewed_max           — int (10/20/50/100/0=unlimited)
    feature_recently_viewed_clean_deleted — bool (hide inactive products)
    recently_viewed_allow_clear_all       — bool (show Clear All for users)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from database import get_db_session, User, Product
from database.models import RecentlyViewed
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

_PAGE_SIZE = 5   # items shown per page


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def feature_status() -> str:
    return cfg.get_str("recently_viewed_status", "enabled").lower()


def is_enabled() -> bool:
    if not cfg.get_bool("feature_recently_viewed_enabled", True):
        return False
    return feature_status() == "enabled"


def max_history() -> int:
    """0 = unlimited."""
    return max(0, cfg.get_int("feature_recently_viewed_max", 20))


def clean_deleted() -> bool:
    return cfg.get_bool("feature_recently_viewed_clean_deleted", True)


def allow_clear_all() -> bool:
    return cfg.get_bool("recently_viewed_allow_clear_all", True)


def total_pages(count: int) -> int:
    if count <= 0:
        return 1
    return (count + _PAGE_SIZE - 1) // _PAGE_SIZE


# ─────────────────────────────────────────────────────────────────────────
# User resolver (telegram_id → users.id)
# ─────────────────────────────────────────────────────────────────────────

def _get_user_id(session, telegram_id: int) -> Optional[int]:
    u = session.query(User).filter_by(telegram_id=telegram_id).first()
    return u.id if u else None


# ─────────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────────

def _build_query(session, user_id: int, search: str = ""):
    """Return a base SQLAlchemy query for this user's recently viewed products."""
    q = (
        session.query(RecentlyViewed, Product)
        .join(Product, RecentlyViewed.product_id == Product.id)
        .filter(RecentlyViewed.user_id == user_id)
    )
    if clean_deleted():
        q = q.filter(Product.is_active == True)  # noqa: E712
    if search:
        q = q.filter(Product.name.ilike(f"%{search}%"))
    return q.order_by(RecentlyViewed.viewed_at.desc())


def get_count(telegram_id: int, search: str = "") -> int:
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return 0
            return _build_query(s, uid, search).count()
    except Exception:
        logger.debug("recently_viewed: get_count failed", exc_info=True)
        return 0


def get_page(telegram_id: int, page: int = 0, search: str = "") -> tuple[list[dict], int]:
    """Return (items_list, total_count).

    Each item dict contains: product_id, name, price, sale_price,
    stock_count, is_active, category_name, viewed_at.
    """
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return [], 0

            q = _build_query(s, uid, search)
            total = q.count()

            rows = q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

            items = []
            for rv, product in rows:
                cat_name = ""
                try:
                    if product.category:
                        cat_name = product.category.name
                except Exception:
                    pass

                items.append({
                    "product_id":   product.id,
                    "name":         product.name,
                    "price":        product.price,
                    "sale_price":   product.sale_price,
                    "stock_count":  product.stock_count,
                    "is_active":    product.is_active,
                    "category_name": cat_name,
                    "viewed_at":    rv.viewed_at,
                    "image_path":   product.image_path,
                })
            return items, total
    except Exception:
        logger.exception("recently_viewed: get_page failed")
        return [], 0


# ─────────────────────────────────────────────────────────────────────────
# Write helpers
# ─────────────────────────────────────────────────────────────────────────

def remove_item(telegram_id: int, product_id: int) -> tuple[bool, str]:
    """Remove a single product from the user's recently-viewed history."""
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return False, "User not found."
            row = s.query(RecentlyViewed).filter_by(
                user_id=uid, product_id=product_id
            ).first()
            if not row:
                return False, "Item not found."
            s.delete(row)
        return True, "✅ Removed from recently viewed."
    except Exception:
        logger.exception("recently_viewed: remove_item failed")
        return False, "❌ Could not remove item."


def clear_all(telegram_id: int) -> tuple[bool, str]:
    """Delete all recently-viewed records for this user."""
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return False, "User not found."
            deleted = (
                s.query(RecentlyViewed)
                .filter_by(user_id=uid)
                .delete(synchronize_session=False)
            )
        return True, f"✅ Cleared {deleted} item(s) from history."
    except Exception:
        logger.exception("recently_viewed: clear_all failed")
        return False, "❌ Could not clear history."


# ─────────────────────────────────────────────────────────────────────────
# Admin statistics
# ─────────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    now = datetime.utcnow()
    stats: dict = {
        "total":   0,
        "daily":   0,
        "weekly":  0,
        "monthly": 0,
        "most_viewed":  [],   # list of (name, count)
        "top_users":    [],   # list of (telegram_id, count)
    }
    try:
        with get_db_session() as s:
            stats["total"]   = s.query(RecentlyViewed).count()
            stats["daily"]   = s.query(RecentlyViewed).filter(
                RecentlyViewed.viewed_at >= now - timedelta(days=1)
            ).count()
            stats["weekly"]  = s.query(RecentlyViewed).filter(
                RecentlyViewed.viewed_at >= now - timedelta(days=7)
            ).count()
            stats["monthly"] = s.query(RecentlyViewed).filter(
                RecentlyViewed.viewed_at >= now - timedelta(days=30)
            ).count()

            # Most viewed products
            rows = (
                s.query(RecentlyViewed.product_id,
                        func.count(RecentlyViewed.id).label("cnt"))
                 .group_by(RecentlyViewed.product_id)
                 .order_by(func.count(RecentlyViewed.id).desc())
                 .limit(5)
                 .all()
            )
            most: list = []
            for pid, cnt in rows:
                p = s.query(Product).filter_by(id=pid).first()
                most.append((p.name if p else f"#{pid}", cnt))
            stats["most_viewed"] = most

            # Top users (by telegram_id via join)
            top_rows = (
                s.query(User.telegram_id,
                        func.count(RecentlyViewed.id).label("cnt"))
                 .join(RecentlyViewed, User.id == RecentlyViewed.user_id)
                 .group_by(User.telegram_id)
                 .order_by(func.count(RecentlyViewed.id).desc())
                 .limit(5)
                 .all()
            )
            stats["top_users"] = [(str(tid), cnt) for tid, cnt in top_rows]
    except Exception:
        logger.exception("recently_viewed: get_stats failed")
    return stats
