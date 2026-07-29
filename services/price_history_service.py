"""Price History Service — V23.

Records every price change per product and surfaces summary stats
for both the user-facing history view and the admin manager.

BotConfig keys consumed:
    price_history_enabled          — master bool toggle
    price_history_status           — "enabled" / "maintenance" / "disabled"
    price_history_max_records      — int (10/20/50/100/0=unlimited)
    price_history_allow_users      — bool (show 📈 button on product pages)
    price_history_show_difference  — bool (show price diff line)
    price_history_show_pct_change  — bool (show % change line)
    price_history_record_admin_name— bool (persist changed_by_name)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from database import get_db_session, Product
from database.models import ProductPriceHistory
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

_PAGE_SIZE = 10   # records shown per page in admin manager


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def feature_status() -> str:
    return cfg.get_str("price_history_status", "enabled").lower()


def is_enabled() -> bool:
    if not cfg.get_bool("price_history_enabled", True):
        return False
    return feature_status() == "enabled"


def allow_users() -> bool:
    return cfg.get_bool("price_history_allow_users", True)


def max_records() -> int:
    """0 = unlimited."""
    return max(0, cfg.get_int("price_history_max_records", 50))


def show_difference() -> bool:
    return cfg.get_bool("price_history_show_difference", True)


def show_pct_change() -> bool:
    return cfg.get_bool("price_history_show_pct_change", True)


def record_admin_name() -> bool:
    return cfg.get_bool("price_history_record_admin_name", True)


def total_pages(count: int, page_size: int = 5) -> int:
    if count <= 0:
        return 1
    return (count + page_size - 1) // page_size


# ─────────────────────────────────────────────────────────────────────────
# Write — record a price change
# ─────────────────────────────────────────────────────────────────────────

def record_price_change(
    product_id: int,
    old_price: float,
    new_price: float,
    changed_by_telegram_id: Optional[int] = None,
    changed_by_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    """Insert one price-history record. Returns True on success.

    Silently skips when the price did not change (prevents duplicates).
    Also silently skips when the feature is fully disabled.
    Enforces the max_records cap by evicting the oldest record first.
    """
    if not cfg.get_bool("price_history_enabled", True):
        return False

    old_price = round(float(old_price or 0), 10)
    new_price = round(float(new_price or 0), 10)

    # Prevent duplicate records
    if old_price == new_price:
        return False

    diff = round(new_price - old_price, 10)
    pct  = round(((new_price - old_price) / old_price) * 100, 2) if old_price != 0 else None

    # Respect name-recording config
    stored_name = changed_by_name if record_admin_name() else None

    try:
        with get_db_session() as s:
            # Enforce max_records cap per product
            cap = max_records()
            if cap > 0:
                count = (
                    s.query(ProductPriceHistory)
                     .filter_by(product_id=product_id)
                     .count()
                )
                if count >= cap:
                    oldest = (
                        s.query(ProductPriceHistory)
                         .filter_by(product_id=product_id)
                         .order_by(ProductPriceHistory.changed_at.asc())
                         .first()
                    )
                    if oldest:
                        s.delete(oldest)

            s.add(ProductPriceHistory(
                product_id=product_id,
                old_price=old_price,
                new_price=new_price,
                difference=diff,
                pct_change=pct,
                changed_by_telegram_id=changed_by_telegram_id,
                changed_by_name=stored_name,
                reason=reason,
                changed_at=datetime.utcnow(),
            ))
        return True
    except Exception:
        logger.exception("price_history: record_price_change failed")
        return False


# ─────────────────────────────────────────────────────────────────────────
# Read — user-facing product history
# ─────────────────────────────────────────────────────────────────────────

def get_product_history(
    product_id: int,
    page: int = 0,
    page_size: int = 5,
) -> tuple[list[dict], int]:
    """Return (records, total_count) newest-first for the given product."""
    try:
        with get_db_session() as s:
            q = (
                s.query(ProductPriceHistory)
                 .filter_by(product_id=product_id)
                 .order_by(ProductPriceHistory.changed_at.desc())
            )
            total = q.count()
            rows = q.offset(page * page_size).limit(page_size).all()
            records = [_row_to_dict(r) for r in rows]
        return records, total
    except Exception:
        logger.exception("price_history: get_product_history failed")
        return [], 0


def get_product_summary(product_id: int) -> dict:
    """Return summary stats: current, previous, highest, lowest, average, last_change, total_changes."""
    stats = {
        "current_price":   None,
        "previous_price":  None,
        "highest_price":   None,
        "lowest_price":    None,
        "average_price":   None,
        "last_change":     None,
        "total_changes":   0,
    }
    try:
        with get_db_session() as s:
            # Current product price
            p = s.query(Product).filter_by(id=product_id).first()
            if p:
                stats["current_price"] = p.price

            rows = (
                s.query(ProductPriceHistory)
                 .filter_by(product_id=product_id)
                 .order_by(ProductPriceHistory.changed_at.desc())
                 .all()
            )
            stats["total_changes"] = len(rows)
            if not rows:
                return stats

            stats["last_change"] = rows[0].changed_at

            all_new = [r.new_price for r in rows]
            all_old = [r.old_price for r in rows if r.old_price != 0]
            all_prices = all_new + all_old

            stats["highest_price"] = max(all_prices) if all_prices else None
            stats["lowest_price"]  = min(all_prices) if all_prices else None
            stats["average_price"] = (sum(all_prices) / len(all_prices)) if all_prices else None

            # Previous = the new_price of the second-most-recent record
            if len(rows) >= 2:
                stats["previous_price"] = rows[1].new_price
            else:
                stats["previous_price"] = rows[0].old_price if rows[0].old_price else None

    except Exception:
        logger.exception("price_history: get_product_summary failed")
    return stats


def _row_to_dict(r: ProductPriceHistory) -> dict:
    return {
        "id":                      r.id,
        "product_id":              r.product_id,
        "old_price":               r.old_price,
        "new_price":               r.new_price,
        "difference":              r.difference,
        "pct_change":              r.pct_change,
        "changed_by_telegram_id":  r.changed_by_telegram_id,
        "changed_by_name":         r.changed_by_name,
        "reason":                  r.reason,
        "changed_at":              r.changed_at,
    }


# ─────────────────────────────────────────────────────────────────────────
# Read — admin manager
# ─────────────────────────────────────────────────────────────────────────

def admin_get_history(
    product_id: Optional[int] = None,
    search_name: str = "",
    page: int = 0,
) -> tuple[list[dict], int]:
    """Paginated history for admin manager (all products or filtered)."""
    try:
        with get_db_session() as s:
            q = s.query(ProductPriceHistory).join(
                Product, ProductPriceHistory.product_id == Product.id
            )
            if product_id:
                q = q.filter(ProductPriceHistory.product_id == product_id)
            if search_name:
                q = q.filter(Product.name.ilike(f"%{search_name}%"))
            q = q.order_by(ProductPriceHistory.changed_at.desc())
            total = q.count()
            rows = q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()
            records = []
            for r in rows:
                d = _row_to_dict(r)
                prod = s.query(Product).filter_by(id=r.product_id).first()
                d["product_name"] = prod.name if prod else f"#{r.product_id}"
                records.append(d)
        return records, total
    except Exception:
        logger.exception("price_history: admin_get_history failed")
        return [], 0


def get_stats() -> dict:
    """Admin statistics overview."""
    now = datetime.utcnow()
    stats: dict = {
        "total":   0,
        "daily":   0,
        "weekly":  0,
        "monthly": 0,
        "most_changed": [],   # list of (product_name, count)
    }
    try:
        with get_db_session() as s:
            stats["total"]   = s.query(ProductPriceHistory).count()
            stats["daily"]   = s.query(ProductPriceHistory).filter(
                ProductPriceHistory.changed_at >= now - timedelta(days=1)
            ).count()
            stats["weekly"]  = s.query(ProductPriceHistory).filter(
                ProductPriceHistory.changed_at >= now - timedelta(days=7)
            ).count()
            stats["monthly"] = s.query(ProductPriceHistory).filter(
                ProductPriceHistory.changed_at >= now - timedelta(days=30)
            ).count()

            rows = (
                s.query(
                    ProductPriceHistory.product_id,
                    func.count(ProductPriceHistory.id).label("cnt"),
                )
                .group_by(ProductPriceHistory.product_id)
                .order_by(func.count(ProductPriceHistory.id).desc())
                .limit(5)
                .all()
            )
            most = []
            for pid, cnt in rows:
                p = s.query(Product).filter_by(id=pid).first()
                most.append((p.name if p else f"#{pid}", cnt))
            stats["most_changed"] = most
    except Exception:
        logger.exception("price_history: get_stats failed")
    return stats
