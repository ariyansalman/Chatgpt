"""Advanced Broadcast Audience Service.

Resolves smart audience filters into lists of (user_id, telegram_id, user_row)
for Advanced Broadcast Types.  Supports 27 targeting segments with AND-combined
extra filters, message-variable substitution (15 placeholders), audience-count
preview, and send-time deduplication.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from database import get_db_session
from database.models import (
    User, Order, OrderItem,
    ScheduledBroadcast,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ── Segment registry (key → human label) ─────────────────────────────────────

SEGMENTS: Dict[str, str] = {
    "all":              "👥 All Users",
    "new":              "🆕 New Users",
    "active":           "🟢 Active Users",
    "inactive":         "😴 Inactive Users",
    "vip":              "💎 VIP Users",
    "referred":         "🤝 Referred Users",
    "wallet_yes":       "💰 Users With Wallet Balance",
    "wallet_no":        "💸 Users Without Wallet Balance",
    "buyers":           "🛒 Buyers Only",
    "non_buyers":       "🚫 Non-Buyers",
    "product_buyers":   "📦 Product Buyers",
    "category_buyers":  "📂 Category Buyers",
    "sub_expiring":     "⏳ Subscription Expiring",
    "sub_expired":      "❌ Expired Subscription",
    "failed_payment":   "💳 Failed Payment Users",
    "pending_payment":  "⌛ Pending Payment Users",
    "language":         "🌐 Language-Based Users",
    "tag":              "🏷 Tag-Based Users",
    "selected_users":   "👤 Selected Users",
    "selected_ids":     "🆔 Selected User IDs",
    "top_customers":    "⭐ Top Customers",
    "repeat_customers": "🔁 Repeat Customers",
    "high_spenders":    "💵 High Spending Users",
    "low_spenders":     "Low Spending Users",
    "reg_today":        "📅 Registered Today",
    "reg_week":         "📅 Registered This Week",
    "reg_month":        "📅 Registered This Month",
}

# ── Variable registry ────────────────────────────────────────────────────────

VARIABLE_KEYS: List[str] = [
    "first_name", "last_name", "username", "telegram_id",
    "wallet_balance", "product_name", "category_name",
    "coupon_code", "discount", "bonus",
    "old_price", "new_price",
    "subscription_expiry", "order_id", "custom_field",
]

# ── Internal helpers ──────────────────────────────────────────────────────────

def _order_stats(s) -> Dict[int, Dict[str, Any]]:
    """Return {user_id: {spend, count, last_order}} for all users."""
    from sqlalchemy import func
    try:
        from database.models import OrderStatus  # noqa: try import
        completed_filter = Order.status == OrderStatus.COMPLETED
    except Exception:
        completed_filter = Order.status == "completed"

    rows = (s.query(
                Order.user_id,
                func.sum(Order.total_amount).label("spend"),
                func.count(Order.id).label("cnt"),
                func.max(Order.created_at).label("last_order"),
            )
            .filter(completed_filter)
            .group_by(Order.user_id)
            .all())
    return {
        r.user_id: {
            "spend":      float(r.spend or 0),
            "count":      int(r.cnt or 0),
            "last_order": r.last_order,
        }
        for r in rows
    }


def _user_tag_ids(s, tag_name: str) -> set:
    """Return set of user_ids that carry the named tag."""
    try:
        from database.models import CustomerTag, CustomerTagAssignment
        tag = s.query(CustomerTag).filter_by(name=tag_name).first()
        if not tag:
            return set()
        rows = s.query(CustomerTagAssignment.user_id).filter_by(tag_id=tag.id).all()
        return {r.user_id for r in rows}
    except Exception:
        logger.debug("_user_tag_ids: tag tables unavailable", exc_info=True)
        return set()


def _subscription_user_ids(s, *, expiring_days: Optional[int] = None, expired: bool = False) -> set:
    """Return user_ids with subscriptions expiring soon or already expired."""
    try:
        from database.models import Subscription
        now = datetime.utcnow()
        if expiring_days is not None:
            cutoff = now + timedelta(days=expiring_days)
            rows = (s.query(Subscription.user_id)
                    .filter(Subscription.expires_at > now,
                            Subscription.expires_at <= cutoff)
                    .all())
        elif expired:
            rows = (s.query(Subscription.user_id)
                    .filter(Subscription.expires_at <= now)
                    .all())
        else:
            return set()
        return {r.user_id for r in rows}
    except Exception:
        logger.debug("_subscription_user_ids: subscription table unavailable", exc_info=True)
        return set()


def _transaction_user_ids(s, status_str: str) -> set:
    """Return user_ids with transactions of a given status string."""
    try:
        from database.models import Transaction, TransactionStatus
        try:
            st = TransactionStatus[status_str.upper()]
        except KeyError:
            st = status_str
        rows = s.query(Transaction.user_id).filter(Transaction.status == st).all()
        return {r.user_id for r in rows}
    except Exception:
        logger.debug("_transaction_user_ids: Transaction unavailable", exc_info=True)
        return set()


def _product_buyer_ids(s, product_id: int) -> set:
    """Return user_ids who bought a specific product."""
    try:
        from database.models import OrderStatus
        completed_filter = Order.status == OrderStatus.COMPLETED
    except Exception:
        completed_filter = Order.status == "completed"
    try:
        rows = (s.query(Order.user_id)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .filter(completed_filter, OrderItem.product_id == product_id)
                .distinct()
                .all())
        return {r.user_id for r in rows}
    except Exception:
        logger.debug("_product_buyer_ids error", exc_info=True)
        return set()


def _category_buyer_ids(s, category_id: int) -> set:
    """Return user_ids who bought from a specific category."""
    try:
        from database.models import Product, OrderStatus
        completed_filter = Order.status == OrderStatus.COMPLETED
    except Exception:
        from database.models import Product
        completed_filter = Order.status == "completed"
    try:
        rows = (s.query(Order.user_id)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .join(Product, Product.id == OrderItem.product_id)
                .filter(completed_filter, Product.category_id == category_id)
                .distinct()
                .all())
        return {r.user_id for r in rows}
    except Exception:
        logger.debug("_category_buyer_ids error", exc_info=True)
        return set()


# ── Primary segment resolver ──────────────────────────────────────────────────

def _base_users(s, segment: str, extra: dict) -> List[User]:
    """Return base User list for the primary segment (no extra filters yet)."""
    now       = datetime.utcnow()
    stats     = None  # lazy-computed

    def get_stats():
        nonlocal stats
        if stats is None:
            stats = _order_stats(s)
        return stats

    base = s.query(User).filter_by(is_banned=False)

    if segment == "all":
        return base.all()

    if segment == "new":
        days = cfg.get_int("abt_new_user_days", 7)
        return base.filter(User.created_at >= now - timedelta(days=days)).all()

    if segment == "active":
        days = cfg.get_int("abt_active_user_days", 7)
        return base.filter(User.last_seen_at >= now - timedelta(days=days)).all()

    if segment == "inactive":
        days = cfg.get_int("abt_inactive_user_days", 30)
        return base.filter(User.last_seen_at < now - timedelta(days=days)).all()

    if segment == "vip":
        threshold = cfg.get_float("seg_vip_spend_threshold", 100.0)
        st = get_stats()
        ids = {uid for uid, d in st.items() if d["spend"] >= threshold}
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "referred":
        return base.filter(User.referred_by_id.isnot(None)).all()

    if segment == "wallet_yes":
        return base.filter(User.wallet_balance > 0).all()

    if segment == "wallet_no":
        return base.filter(User.wallet_balance <= 0).all()

    if segment == "buyers":
        return base.filter(User.has_purchased == True).all()

    if segment == "non_buyers":
        return base.filter(User.has_purchased == False).all()

    if segment == "product_buyers":
        product_id = extra.get("product_id")
        if not product_id:
            return []
        ids = _product_buyer_ids(s, int(product_id))
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "category_buyers":
        category_id = extra.get("category_id")
        if not category_id:
            return []
        ids = _category_buyer_ids(s, int(category_id))
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "sub_expiring":
        days = cfg.get_int("abt_sub_expiring_days", 7)
        ids  = _subscription_user_ids(s, expiring_days=days)
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "sub_expired":
        ids = _subscription_user_ids(s, expired=True)
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "failed_payment":
        ids = _transaction_user_ids(s, "FAILED")
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "pending_payment":
        ids = _transaction_user_ids(s, "PENDING")
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "language":
        lang = extra.get("language", "en")
        return base.filter(User.language == lang).all()

    if segment == "tag":
        tag_name = extra.get("tag_name", "")
        ids = _user_tag_ids(s, tag_name)
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "selected_users":
        usernames = [u.strip().lstrip("@") for u in
                     extra.get("selected_usernames", "").split(",") if u.strip()]
        return base.filter(User.username.in_(usernames)).all() if usernames else []

    if segment == "selected_ids":
        raw = extra.get("selected_ids", "")
        try:
            tids = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            tids = []
        return base.filter(User.telegram_id.in_(tids)).all() if tids else []

    if segment == "top_customers":
        n  = cfg.get_int("abt_top_customers_count", 50)
        st = get_stats()
        top_ids = sorted(st, key=lambda u: st[u]["spend"], reverse=True)[:n]
        return base.filter(User.id.in_(top_ids)).all() if top_ids else []

    if segment == "repeat_customers":
        min_orders = cfg.get_int("abt_repeat_customer_orders", 3)
        st  = get_stats()
        ids = {uid for uid, d in st.items() if d["count"] >= min_orders}
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "high_spenders":
        threshold = cfg.get_float("abt_high_spend_threshold", 50.0)
        st  = get_stats()
        ids = {uid for uid, d in st.items() if d["spend"] >= threshold}
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "low_spenders":
        threshold = cfg.get_float("abt_low_spend_threshold", 10.0)
        st  = get_stats()
        ids = {uid for uid, d in st.items() if 0 < d["spend"] < threshold}
        return base.filter(User.id.in_(ids)).all() if ids else []

    if segment == "reg_today":
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base.filter(User.created_at >= today).all()

    if segment == "reg_week":
        return base.filter(User.created_at >= now - timedelta(days=7)).all()

    if segment == "reg_month":
        return base.filter(User.created_at >= now - timedelta(days=30)).all()

    # Fallback: all
    logger.warning("Unknown segment '%s', falling back to 'all'", segment)
    return base.all()


def _apply_extra_filters(users: List[User], extra: dict, stats: Optional[dict] = None) -> List[User]:
    """Apply AND-combined extra filters to a list of User objects."""
    if not extra:
        return users

    now = datetime.utcnow()

    # Wallet filters
    if "min_wallet" in extra:
        mn = float(extra["min_wallet"])
        users = [u for u in users if (u.wallet_balance or 0) >= mn]
    if "max_wallet" in extra:
        mx = float(extra["max_wallet"])
        users = [u for u in users if (u.wallet_balance or 0) <= mx]

    # Activity filter (seen in last N days)
    if "active_days" in extra:
        d = int(extra["active_days"])
        cutoff = now - timedelta(days=d)
        users = [u for u in users if u.last_seen_at and u.last_seen_at >= cutoff]

    # Language filter (when used as extra, not primary)
    if "language" in extra and extra.get("language"):
        lang = extra["language"]
        users = [u for u in users if u.language == lang]

    # Has purchased filter
    if "has_purchased" in extra:
        want = bool(extra["has_purchased"])
        users = [u for u in users if bool(u.has_purchased) == want]

    # Min orders (needs stats)
    if "min_orders" in extra and stats is not None:
        mn = int(extra["min_orders"])
        users = [u for u in users if stats.get(u.id, {}).get("count", 0) >= mn]

    return users


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_audience(
    segment: str,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> List[Tuple[int, str]]:
    """Return deduplicated list of (telegram_id, username) for the audience.

    Args:
        segment:       primary segment key
        extra_filters: dict of extra AND-filters to apply on top

    Returns:
        List of (telegram_id, username or "")  — no duplicates, banned excluded.
    """
    extra = extra_filters or {}
    with get_db_session() as s:
        users = _base_users(s, segment, extra)
        if extra:
            # compute order stats only if needed
            need_stats = {"min_orders"}
            if need_stats & set(extra.keys()):
                order_stats = _order_stats(s)
            else:
                order_stats = None
            users = _apply_extra_filters(users, extra, order_stats)

        # Deduplication by telegram_id
        seen: set = set()
        result = []
        for u in users:
            if u.telegram_id and u.telegram_id not in seen:
                seen.add(u.telegram_id)
                result.append((u.telegram_id, u.username or ""))
        return result


def count_audience(
    segment: str,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the number of unique users for a given segment + filters."""
    return len(resolve_audience(segment, extra_filters))


def estimate_delivery_seconds(user_count: int) -> float:
    """Estimate how long a broadcast to `user_count` users will take in seconds."""
    delay_ms  = cfg.get_int("broadcast_delay_ms", 50)
    max_speed = cfg.get_int("broadcast_max_speed", 20)
    # Time per message = max(delay_ms, 1000/max_speed) milliseconds
    ms_per_msg = max(delay_ms, 1000.0 / max(1, max_speed))
    return user_count * ms_per_msg / 1000.0


# ── Variable substitution ─────────────────────────────────────────────────────

def substitute_variables(
    template: str,
    user: Optional[User] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> str:
    """Replace {placeholder} variables in a template string.

    Args:
        template:  raw message template
        user:      a User ORM object for per-user values (optional)
        overrides: broadcast-level values (coupon_code, discount, etc.)

    Returns:
        The message with all known placeholders substituted.
    """
    vals: Dict[str, str] = {}
    ov = overrides or {}

    if user:
        full_name = (getattr(user, "first_name", None) or
                     getattr(user, "username", None) or "there")
        # User model stores username; first_name may not exist
        name_parts = (user.username or "").split("_")
        vals["first_name"]  = ov.get("first_name", name_parts[0] if name_parts else "there")
        vals["last_name"]   = ov.get("last_name",  name_parts[-1] if len(name_parts) > 1 else "")
        vals["username"]    = ov.get("username",   f"@{user.username}" if user.username else "—")
        vals["telegram_id"] = ov.get("telegram_id", str(user.telegram_id or ""))
        vals["wallet_balance"] = ov.get(
            "wallet_balance", f"${user.wallet_balance:.2f}" if user.wallet_balance else "$0.00")
    else:
        vals["first_name"]     = ov.get("first_name",     "there")
        vals["last_name"]      = ov.get("last_name",       "")
        vals["username"]       = ov.get("username",        "@user")
        vals["telegram_id"]    = ov.get("telegram_id",     "0")
        vals["wallet_balance"] = ov.get("wallet_balance",  "$0.00")

    # Broadcast-level overrides (provided by admin via the compose form)
    for key in ("product_name", "category_name", "coupon_code", "discount",
                "bonus", "old_price", "new_price", "subscription_expiry",
                "order_id", "custom_field"):
        vals[key] = str(ov.get(key, f"{{{key}}}"))

    # Replace all {key} patterns
    for k, v in vals.items():
        template = template.replace("{" + k + "}", v)
    return template


def preview_for_first_user(
    segment: str,
    template: str,
    extra_filters: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Return (rendered_message, username_or_none) for the first matching user."""
    extra = extra_filters or {}
    with get_db_session() as s:
        users = _base_users(s, segment, extra)
        if extra:
            users = _apply_extra_filters(users, extra, None)
        if not users:
            # fallback: substitute with placeholder values
            return substitute_variables(template, None, overrides), None
        u = users[0]
        return substitute_variables(template, u, overrides), u.username


# ── Send deduplication guard ──────────────────────────────────────────────────

def is_duplicate_delivery(broadcast_id: int, telegram_id: int) -> bool:
    """Return True if this user has already received this broadcast."""
    try:
        from database.models import BroadcastLog, BroadcastRetryQueue
        with get_db_session() as s:
            # Check retry queue for a successful send
            row = (s.query(BroadcastRetryQueue)
                   .filter_by(broadcast_id=broadcast_id,
                              telegram_id=telegram_id,
                              status="sent")
                   .first())
            return row is not None
    except Exception:
        return False
