"""Favorites (Bookmark) Service — V22.

Separate from UserWishlist (price-drop alerts). Favorites is a pure
"save for later" bookmarking system with sort, search, and stats.

BotConfig keys consumed:
    feature_favorites_enabled     — master bool toggle
    favorites_status              — "enabled" / "maintenance" / "disabled"
    favorites_max                 — int (10/20/50/100/0=unlimited)
    favorites_counter             — bool (show counter on product buttons)
    favorites_allow_clear_all     — bool (show Clear All button for users)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from database import get_db_session, User, Product
from database.models import UserFavorite
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

_PAGE_SIZE = 5   # favorites shown per page


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def feature_status() -> str:
    return cfg.get_str("favorites_status", "enabled").lower()


def is_enabled() -> bool:
    if not cfg.get_bool("feature_favorites_enabled", True):
        return False
    return feature_status() == "enabled"


def max_favorites() -> int:
    """0 = unlimited."""
    return max(0, cfg.get_int("favorites_max", 50))


def show_counter() -> bool:
    return cfg.get_bool("favorites_counter", True)


def allow_clear_all() -> bool:
    return cfg.get_bool("favorites_allow_clear_all", True)


# ─────────────────────────────────────────────────────────────────────────
# User resolver (telegram_id → users.id)
# ─────────────────────────────────────────────────────────────────────────

def _get_user_id(session, telegram_id: int) -> Optional[int]:
    u = session.query(User).filter_by(telegram_id=telegram_id).first()
    return u.id if u else None


# ─────────────────────────────────────────────────────────────────────────
# Favorites CRUD
# ─────────────────────────────────────────────────────────────────────────

def is_favorited(telegram_id: int, product_id: int) -> bool:
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return False
            return bool(
                s.query(UserFavorite)
                 .filter_by(user_id=uid, product_id=product_id)
                 .first()
            )
    except Exception:
        return False


def get_count(telegram_id: int) -> int:
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return 0
            return s.query(UserFavorite).filter_by(user_id=uid).count()
    except Exception:
        return 0


def add_favorite(telegram_id: int, product_id: int) -> tuple[bool, str]:
    """Add product to favorites. Returns (success, message)."""
    status = feature_status()
    if not cfg.get_bool("feature_favorites_enabled", True):
        return False, "Favorites is disabled."
    if status == "disabled":
        return False, "Favorites is disabled."
    if status == "maintenance":
        return False, "⚠️ Favorites is currently under maintenance.\nPlease try again later."

    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return False, "User account not found. Please send /start first."

            product = s.query(Product).filter_by(id=product_id).first()
            if not product:
                return False, "Product not found."

            existing = s.query(UserFavorite).filter_by(
                user_id=uid, product_id=product_id
            ).first()
            if existing:
                return False, f"<b>{product.name}</b> is already in your favorites."

            mx = max_favorites()
            if mx > 0:
                count = s.query(UserFavorite).filter_by(user_id=uid).count()
                if count >= mx:
                    return False, (
                        f"You've reached the favorites limit (<b>{mx}</b>).\n"
                        f"Remove a product first to add new ones."
                    )

            s.add(UserFavorite(
                user_id=uid,
                product_id=product_id,
                created_at=datetime.utcnow(),
            ))
            s.commit()
            new_count = s.query(UserFavorite).filter_by(user_id=uid).count()
            return True, f"❤️ <b>{product.name}</b> added to your favorites!\n📌 Total saved: <b>{new_count}</b>"
    except Exception as exc:
        logger.exception("favorites: add_favorite failed")
        return False, f"Error: {exc}"


def remove_favorite(telegram_id: int, product_id: int) -> tuple[bool, str]:
    """Remove product from favorites. Returns (success, message)."""
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return False, "User not found."
            row = s.query(UserFavorite).filter_by(
                user_id=uid, product_id=product_id
            ).first()
            if not row:
                return False, "Product was not in your favorites."
            product = s.query(Product).filter_by(id=product_id).first()
            pname = product.name if product else f"Product #{product_id}"
            s.delete(row)
            s.commit()
            return True, f"💔 <b>{pname}</b> removed from favorites."
    except Exception as exc:
        logger.exception("favorites: remove_favorite failed")
        return False, f"Error: {exc}"


def clear_all_favorites(telegram_id: int) -> int:
    """Remove all favorites for this user. Returns count removed."""
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return 0
            n = s.query(UserFavorite).filter_by(user_id=uid).delete(
                synchronize_session=False
            )
            s.commit()
            return n
    except Exception:
        logger.exception("favorites: clear_all_favorites failed")
        return 0


# ─────────────────────────────────────────────────────────────────────────
# List / search / sort
# ─────────────────────────────────────────────────────────────────────────

_SORT_MAP = {
    "new":   lambda q: q.order_by(UserFavorite.created_at.desc()),
    "old":   lambda q: q.order_by(UserFavorite.created_at.asc()),
    "price": lambda q: q,   # applied after join; handled inline
    "alpha": lambda q: q,   # applied after join; handled inline
}


def get_favorites_page(
    telegram_id: int,
    sort: str = "new",
    page: int = 0,
    search: str = "",
) -> tuple[list[dict], int]:
    """Return (items, total_count) for pagination.

    Each item dict has keys: id, product_id, name, price, sale_price,
    discount_pct, stock_count, category, is_active, created_at, added_at.
    """
    try:
        with get_db_session() as s:
            uid = _get_user_id(s, telegram_id)
            if not uid:
                return [], 0

            # Join with Product to get current data + handle deleted products
            query = (
                s.query(UserFavorite, Product)
                 .join(Product, UserFavorite.product_id == Product.id)
                 .filter(UserFavorite.user_id == uid)
            )

            if search:
                query = query.filter(
                    Product.name.ilike(f"%{search}%")
                )

            if sort == "new":
                query = query.order_by(UserFavorite.created_at.desc())
            elif sort == "old":
                query = query.order_by(UserFavorite.created_at.asc())
            elif sort == "price":
                query = query.order_by(Product.price.asc())
            elif sort == "alpha":
                query = query.order_by(Product.name.asc())
            else:
                query = query.order_by(UserFavorite.created_at.desc())

            total = query.count()
            rows = query.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

            items = []
            for fav, product in rows:
                cat_name = product.category.name if product.category else "—"
                eff_price = (product.sale_price
                             if product.sale_price and product.sale_price < product.price
                             else product.price)
                if product.sale_price and product.sale_price < product.price:
                    disc_pct = round(
                        (product.price - product.sale_price) / product.price * 100
                    )
                else:
                    disc_pct = None
                items.append({
                    "fav_id":      fav.id,
                    "product_id":  product.id,
                    "name":        product.name,
                    "price":       eff_price,
                    "orig_price":  product.price,
                    "sale_price":  product.sale_price,
                    "discount_pct": disc_pct,
                    "stock_count": product.stock_count,
                    "is_active":   product.is_active,
                    "category":    cat_name,
                    "updated_at":  product.created_at,
                    "added_at":    fav.created_at,
                })
            return items, total
    except Exception:
        logger.exception("favorites: get_favorites_page failed")
        return [], 0


def total_pages(total: int) -> int:
    if total == 0:
        return 0
    return (total + _PAGE_SIZE - 1) // _PAGE_SIZE


# ─────────────────────────────────────────────────────────────────────────
# Admin statistics
# ─────────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    now = datetime.utcnow()
    stats = {
        "total":         0,
        "daily":         0,
        "weekly":        0,
        "monthly":       0,
        "most_favorited": [],   # list of (name, count)
        "top_users":      [],   # list of (telegram_id, count)
    }
    try:
        with get_db_session() as s:
            from sqlalchemy import func

            stats["total"]   = s.query(UserFavorite).count()
            stats["daily"]   = s.query(UserFavorite).filter(
                UserFavorite.created_at >= now - timedelta(days=1)
            ).count()
            stats["weekly"]  = s.query(UserFavorite).filter(
                UserFavorite.created_at >= now - timedelta(days=7)
            ).count()
            stats["monthly"] = s.query(UserFavorite).filter(
                UserFavorite.created_at >= now - timedelta(days=30)
            ).count()

            # Most favorited products
            rows = (
                s.query(UserFavorite.product_id,
                        func.count(UserFavorite.id).label("cnt"))
                 .group_by(UserFavorite.product_id)
                 .order_by(func.count(UserFavorite.id).desc())
                 .limit(5)
                 .all()
            )
            most = []
            for pid, cnt in rows:
                p = s.query(Product).filter_by(id=pid).first()
                most.append((p.name if p else f"#{pid}", cnt))
            stats["most_favorited"] = most

            # Top users (by telegram_id via join)
            top_rows = (
                s.query(User.telegram_id,
                        func.count(UserFavorite.id).label("cnt"))
                 .join(UserFavorite, User.id == UserFavorite.user_id)
                 .group_by(User.telegram_id)
                 .order_by(func.count(UserFavorite.id).desc())
                 .limit(5)
                 .all()
            )
            stats["top_users"] = [(str(tid), cnt) for tid, cnt in top_rows]
    except Exception:
        logger.exception("favorites: get_stats failed")
    return stats
