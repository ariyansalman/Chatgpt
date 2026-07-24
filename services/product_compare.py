"""Product Compare Service — V22.

Manages a per-user compare list (up to N products, configurable) and builds
the formatted comparison display message.

Callback namespace (user): ``cmp:*``
Admin panel namespace:      ``acc:pcmp:*`` (routed via admin_control_center)

BotConfig keys consumed:
    feature_product_compare_enabled   — master enable/disable
    product_compare_status            — "enabled" / "maintenance" / "disabled"
    product_compare_max               — int  (2 / 3 / 4)
    product_compare_counter           — bool (show compare counter on product btn)
    product_compare_best_value        — bool (highlight best value cells)
    product_compare_show_unavailable  — bool (include out-of-stock products)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from database import get_db_session, User, Product
from database.models import ProductCompare, ProductCompareLog, Review
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def feature_status() -> str:
    """Return "enabled", "maintenance", or "disabled"."""
    return cfg.get_str("product_compare_status", "enabled").lower()


def is_enabled() -> bool:
    """True when compare is fully enabled (not maintenance, not disabled)."""
    if not cfg.get_bool("feature_product_compare_enabled", True):
        return False
    return feature_status() == "enabled"


def max_products() -> int:
    return max(2, min(4, cfg.get_int("product_compare_max", 4)))


def show_counter() -> bool:
    return cfg.get_bool("product_compare_counter", True)


def highlight_best() -> bool:
    return cfg.get_bool("product_compare_best_value", True)


def show_unavailable() -> bool:
    return cfg.get_bool("product_compare_show_unavailable", True)


# ─────────────────────────────────────────────────────────────────────────
# Compare-list CRUD
# ─────────────────────────────────────────────────────────────────────────

def get_compare_list(telegram_id: int) -> list[int]:
    """Return ordered list of product IDs in the user's compare list."""
    try:
        with get_db_session() as s:
            rows = (s.query(ProductCompare)
                    .filter_by(user_telegram_id=telegram_id)
                    .order_by(ProductCompare.added_at)
                    .all())
            return [r.product_id for r in rows]
    except Exception:
        logger.exception("compare: get_compare_list failed for tg:%d", telegram_id)
        return []


def get_compare_count(telegram_id: int) -> int:
    try:
        with get_db_session() as s:
            return (s.query(ProductCompare)
                    .filter_by(user_telegram_id=telegram_id)
                    .count())
    except Exception:
        return 0


def add_to_compare(telegram_id: int, product_id: int) -> tuple[bool, str]:
    """Add a product to the compare list.

    Returns (success, message).
    """
    status = feature_status()
    if not cfg.get_bool("feature_product_compare_enabled", True):
        return False, "Product comparison is disabled."
    if status == "disabled":
        return False, "Product comparison is disabled."
    if status == "maintenance":
        return False, "⚠️ Product comparison is currently under maintenance.\nPlease try again later."

    try:
        with get_db_session() as s:
            # Validate product exists
            product = s.query(Product).filter_by(id=product_id).first()
            if not product:
                return False, "Product not found."

            if not show_unavailable() and (not product.is_active or product.stock_count <= 0):
                return False, "This product is currently unavailable for comparison."

            # Check if already in list
            existing = (s.query(ProductCompare)
                        .filter_by(user_telegram_id=telegram_id, product_id=product_id)
                        .first())
            if existing:
                return False, f"<b>{product.name}</b> is already in your comparison list."

            # Check max limit
            count = (s.query(ProductCompare)
                     .filter_by(user_telegram_id=telegram_id)
                     .count())
            cap = max_products()
            if count >= cap:
                return False, (
                    f"You can compare up to <b>{cap} products</b> at once.\n"
                    f"Remove a product first or clear the list."
                )

            s.add(ProductCompare(
                user_telegram_id=telegram_id,
                product_id=product_id,
                added_at=datetime.utcnow(),
            ))
            s.commit()
            new_count = count + 1
            return True, (
                f"✅ <b>{product.name}</b> added to comparison.\n"
                f"📊 Compare list: <b>{new_count}/{cap}</b> product(s)"
            )
    except Exception as exc:
        logger.exception("compare: add_to_compare failed")
        return False, f"Error: {exc}"


def remove_from_compare(telegram_id: int, product_id: int) -> tuple[bool, str]:
    """Remove a product from the compare list."""
    try:
        with get_db_session() as s:
            row = (s.query(ProductCompare)
                   .filter_by(user_telegram_id=telegram_id, product_id=product_id)
                   .first())
            if not row:
                return False, "Product was not in your comparison list."
            product = s.query(Product).filter_by(id=product_id).first()
            pname = product.name if product else f"Product #{product_id}"
            s.delete(row)
            s.commit()
            return True, f"✅ <b>{pname}</b> removed from comparison."
    except Exception as exc:
        logger.exception("compare: remove_from_compare failed")
        return False, f"Error: {exc}"


def clear_compare_list(telegram_id: int) -> int:
    """Remove all items from the user's compare list. Returns count removed."""
    try:
        with get_db_session() as s:
            count = (s.query(ProductCompare)
                     .filter_by(user_telegram_id=telegram_id)
                     .delete(synchronize_session=False))
            s.commit()
            return count
    except Exception:
        logger.exception("compare: clear_compare_list failed")
        return 0


def is_in_compare(telegram_id: int, product_id: int) -> bool:
    try:
        with get_db_session() as s:
            return bool(
                s.query(ProductCompare)
                .filter_by(user_telegram_id=telegram_id, product_id=product_id)
                .first()
            )
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
# Rating helper
# ─────────────────────────────────────────────────────────────────────────

def _product_rating(session, product_id: int) -> tuple[float, int]:
    """Return (avg_rating, review_count) for a product."""
    reviews = (session.query(Review)
               .filter_by(product_id=product_id, is_hidden=False, is_approved=True)
               .all())
    if not reviews:
        return 0.0, 0
    avg = sum(r.rating for r in reviews) / len(reviews)
    return round(avg, 1), len(reviews)


def _star_bar(rating: float) -> str:
    full = int(rating)
    half = 1 if rating - full >= 0.5 else 0
    empty = 5 - full - half
    return "⭐" * full + ("✨" if half else "") + "☆" * empty


# ─────────────────────────────────────────────────────────────────────────
# Comparison display builder
# ─────────────────────────────────────────────────────────────────────────

def build_comparison_message(telegram_id: int) -> tuple[str, list[int]]:
    """Build the formatted comparison text and return (text, product_ids).

    ``product_ids`` is the list of compared product IDs (for building buy buttons).
    Returns ("", []) if the list is empty.
    """
    product_ids = get_compare_list(telegram_id)
    if not product_ids:
        return "", []

    do_best = highlight_best()

    try:
        with get_db_session() as s:
            # Batch-fetch all products in a single query instead of one
            # query per id (N+1), then reorder to match product_ids so
            # display order is unchanged.
            fetched = {
                p.id: p
                for p in s.query(Product).filter(Product.id.in_(product_ids)).all()
            }
            products = [fetched[pid] for pid in product_ids if pid in fetched]

            if not products:
                return "⚠️ All compared products have been removed.", []

            # Gather data for each product
            data = []
            for p in products:
                cat_name = p.category.name if p.category else "—"

                # Effective price (sale > base)
                eff_price = p.sale_price if p.sale_price and p.sale_price < p.price else p.price

                # Discount
                if p.sale_price and p.sale_price < p.price:
                    disc_pct = round((p.price - p.sale_price) / p.price * 100)
                    disc_str = f"{disc_pct}% off"
                elif p.bundle_discount_percent:
                    disc_str = f"{int(p.bundle_discount_percent)}% bundle"
                else:
                    disc_str = "—"

                # Stock
                if p.stock_count > 0:
                    stock_str = str(p.stock_count)
                else:
                    stock_str = "Out of stock"

                # Availability
                avail_str = "✅ Available" if (p.is_active and p.stock_count > 0) else "❌ Unavailable"

                # Rating
                avg_r, n_r = _product_rating(s, p.id)
                if n_r:
                    rating_str = f"{_star_bar(avg_r)} {avg_r}/5"
                    reviews_str = str(n_r)
                else:
                    rating_str = "No ratings yet"
                    reviews_str = "0"

                # Delivery type
                try:
                    dtype = p.product_type.value.replace("_", " ").title()
                except Exception:
                    dtype = "—"

                # Warranty
                warranty_str = (p.warranty_info[:60] + "…"
                                if p.warranty_info and len(p.warranty_info) > 60
                                else p.warranty_info or "—")

                # Subscription duration (from type_config if available)
                sub_dur = "—"
                if p.type_config:
                    try:
                        tc = json.loads(p.type_config)
                        days = tc.get("duration_days") or tc.get("subscription_days")
                        if days:
                            sub_dur = f"{days} days"
                    except Exception:
                        pass

                updated = p.created_at
                updated_str = updated.strftime("%Y-%m-%d") if updated else "—"

                data.append({
                    "id": p.id,
                    "name": p.name,
                    "price": eff_price,
                    "price_str": f"${eff_price:.2f}",
                    "discount": disc_str,
                    "stock": p.stock_count,
                    "stock_str": stock_str,
                    "type": dtype,
                    "category": cat_name,
                    "rating": avg_r,
                    "rating_str": rating_str,
                    "reviews": n_r,
                    "reviews_str": reviews_str,
                    "sub_duration": sub_dur,
                    "warranty": warranty_str,
                    "availability": avail_str,
                    "updated": updated_str,
                })

            # Find best values (for highlighting)
            if do_best and len(data) > 1:
                # Best = lowest price, highest stock, highest rating
                min_price = min(d["price"] for d in data)
                max_stock = max(d["stock"] for d in data)
                max_rating = max(d["rating"] for d in data)
            else:
                min_price = max_stock = max_rating = None

            # Build message
            n = len(products)
            lines = [f"⚖️ <b>PRODUCT COMPARISON</b> ({n} product{'s' if n > 1 else ''})\n"]

            sep = "─" * 30

            # Product names header
            lines.append(sep)
            for i, d in enumerate(data, 1):
                lines.append(f"  <b>{i}. {d['name']}</b>")
            lines.append(sep)

            def _row(label: str, getter, best_val=None, best_key=None, lower_is_better=False):
                """Render one comparison row."""
                lines.append(f"\n<b>{label}</b>")
                for d in data:
                    val_raw = d[getter] if isinstance(getter, str) else getter(d)
                    val_str = d[getter + "_str"] if isinstance(getter, str) and (getter + "_str") in d else str(val_raw)
                    crown = ""
                    if do_best and best_val is not None:
                        if lower_is_better and val_raw == best_val:
                            crown = " 🏆"
                        elif not lower_is_better and val_raw == best_val:
                            crown = " 🏆"
                    lines.append(f"  • <b>{d['name'][:20]}</b>: {val_str}{crown}")

            _row("💰 Price",           "price", min_price, lower_is_better=True)
            _row("🏷 Discount",         "discount")
            _row("📦 Stock",            "stock", max_stock)
            _row("🚚 Delivery Type",    "type")
            _row("📂 Category",         "category")
            _row("⭐ Rating",            "rating", max_rating)
            _row("💬 Reviews",          "reviews")
            _row("⏳ Subscription",     "sub_duration")
            _row("🛡 Warranty",          "warranty")
            _row("✅ Availability",      "availability")
            _row("🕐 Last Updated",     "updated")

            lines.append(f"\n{sep}")
            if do_best:
                lines.append("🏆 = <i>Best value for this metric</i>")

            # Log this comparison view
            _log_comparison(telegram_id, [d["id"] for d in data])

            return "\n".join(lines), [d["id"] for d in data]

    except Exception:
        logger.exception("compare: build_comparison_message failed")
        return "❌ Could not build comparison. Please try again.", []


def _log_comparison(telegram_id: int, product_ids: list[int]) -> None:
    """Record that this user viewed a comparison session (for admin stats)."""
    try:
        with get_db_session() as s:
            s.add(ProductCompareLog(
                user_telegram_id=telegram_id,
                product_ids_json=json.dumps(product_ids),
                product_count=len(product_ids),
                viewed_at=datetime.utcnow(),
            ))
            s.commit()
    except Exception:
        logger.debug("compare: _log_comparison failed (non-critical)")


def mark_purchased_from_compare(telegram_id: int, product_id: int) -> None:
    """Mark the user's most recent compare session as having led to a purchase."""
    try:
        with get_db_session() as s:
            latest = (s.query(ProductCompareLog)
                      .filter_by(user_telegram_id=telegram_id)
                      .order_by(ProductCompareLog.viewed_at.desc())
                      .first())
            if latest and not latest.purchased_from_compare:
                latest.purchased_from_compare = True
                latest.purchased_product_id = product_id
                s.commit()
    except Exception:
        logger.debug("compare: mark_purchased_from_compare failed (non-critical)")


# ─────────────────────────────────────────────────────────────────────────
# Admin statistics
# ─────────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return admin dashboard counters."""
    stats = {
        "total_comparisons": 0,
        "most_compared": [],      # list of (product_name, count)
        "avg_compare_count": 0.0,
        "purchased_after": 0,
    }
    try:
        with get_db_session() as s:
            from sqlalchemy import func

            # Total comparison sessions logged
            stats["total_comparisons"] = s.query(ProductCompareLog).count()

            # Most compared products (by entries in ProductCompare + logs)
            from sqlalchemy import text as _t
            rows = (s.query(ProductCompare.product_id,
                            func.count(ProductCompare.id).label("cnt"))
                    .group_by(ProductCompare.product_id)
                    .order_by(func.count(ProductCompare.id).desc())
                    .limit(5)
                    .all())
            most = []
            for pid, cnt in rows:
                p = s.query(Product).filter_by(id=pid).first()
                pname = p.name if p else f"Product #{pid}"
                most.append((pname, cnt))
            stats["most_compared"] = most

            # Average products per comparison session
            logs = s.query(ProductCompareLog.product_count).all()
            if logs:
                avg = sum(r[0] for r in logs) / len(logs)
                stats["avg_compare_count"] = round(avg, 1)

            # Purchased after comparison
            stats["purchased_after"] = (
                s.query(ProductCompareLog)
                .filter_by(purchased_from_compare=True)
                .count()
            )
    except Exception:
        logger.exception("compare: get_stats failed")
    return stats
