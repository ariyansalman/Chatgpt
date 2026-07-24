"""Dashboard Widget System — data collection and layout management (V30).

All DB queries share a single session for efficiency.
Layout is persisted per-admin in admin_dashboard_layouts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text
from database import (
    get_db_session, User, Product, Order, Transaction, Coupon,
    OrderStatus, TransactionStatus,
)

logger = logging.getLogger(__name__)

# ─── Widget Catalogue ─────────────────────────────────────────────────────────

WIDGET_DEFS: list[tuple[str, str, str]] = [
    # (id,                label,                      category)
    ("revenue_today",     "💰 Today's Revenue",       "revenue"),
    ("wallet_balance",    "💵 Wallet Balance",         "revenue"),
    ("revenue_weekly",    "📈 Weekly Revenue",         "revenue"),
    ("revenue_monthly",   "📊 Monthly Revenue",        "revenue"),
    ("orders_today",      "🛒 Today's Orders",         "orders"),
    ("orders_pending",    "📦 Pending Orders",         "orders"),
    ("orders_completed",  "✅ Completed Orders",       "orders"),
    ("orders_cancelled",  "❌ Cancelled Orders",       "orders"),
    ("users_total",       "👤 Total Users",            "users"),
    ("users_online",      "🟢 Active (24 h)",          "users"),
    ("users_new_today",   "🆕 New Users Today",        "users"),
    ("deposits_today",    "💳 Today's Deposits",       "finance"),
    ("withdrawals_today", "💸 Today's Withdrawals",    "finance"),
    ("referral_earnings", "👥 Referral Earnings",      "finance"),
    ("failed_payments",   "🚨 Failed Payments",        "finance"),
    ("best_products",     "🏆 Best Selling Products",  "products"),
    ("low_stock",         "⚠️ Low Stock Products",     "products"),
    ("active_coupons",    "🎁 Active Coupons",         "store"),
    ("top_customers",     "⭐ Top Customers",          "store"),
    ("system_alerts",     "🔔 System Alerts",          "system"),
]

WIDGET_IDS: list[str] = [w[0] for w in WIDGET_DEFS]
WIDGET_LABELS: dict[str, str] = {w[0]: w[1] for w in WIDGET_DEFS}

DEFAULT_LAYOUT: dict[str, Any] = {
    "order": WIDGET_IDS.copy(),
    "hidden": [],
    "collapsed": [],
    "pinned": [],
}

_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return "▁" * 7
    mx = max(values) or 1.0
    return "".join(_SPARK[min(7, int(v / mx * 7))] for v in values)


def _trend(current: float, previous: float) -> str:
    if previous == 0:
        return "" if current == 0 else " 🆕"
    pct = (current - previous) / previous * 100
    if pct > 5:
        return f" ↑{pct:.0f}%"
    if pct < -5:
        return f" ↓{abs(pct):.0f}%"
    return " →"


# ─── Stats Collection ─────────────────────────────────────────────────────────

def _period_bounds(period: str) -> tuple[datetime, datetime, datetime, datetime]:
    """Return (p_start, p_end, prev_start, prev_end) for the given period key."""
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)

    if period == "yday":
        p_start, p_end = today - timedelta(days=1), today
    elif period == "7d":
        p_start, p_end = today - timedelta(days=7), now
    elif period == "30d":
        p_start, p_end = today - timedelta(days=30), now
    elif period == "90d":
        p_start, p_end = today - timedelta(days=90), now
    else:  # "today" default
        p_start, p_end = today, now

    span = p_end - p_start
    prev_start = p_start - span
    prev_end = p_start
    return p_start, p_end, prev_start, prev_end


def collect_stats(period: str = "today") -> dict[str, Any]:  # noqa: C901
    """Collect all widget data in one DB session."""
    p_start, p_end, prev_start, prev_end = _period_bounds(period)
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    result: dict[str, Any] = {"period": period}

    with get_db_session() as s:

        # ── Helpers ───────────────────────────────────────────────────────────
        def txn_sum(start: datetime, end: datetime) -> float:
            return float(s.query(
                func.coalesce(func.sum(Transaction.amount), 0)
            ).filter(
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.created_at >= start,
                Transaction.created_at < end,
            ).scalar() or 0.0)

        def order_count(start: datetime | None = None,
                        end: datetime | None = None,
                        status=None) -> int:
            q = s.query(func.count(Order.id))
            if status is not None:
                q = q.filter(Order.status == status)
            if start is not None:
                q = q.filter(Order.created_at >= start)
            if end is not None:
                q = q.filter(Order.created_at < end)
            return int(q.scalar() or 0)

        def order_revenue(start: datetime, end: datetime) -> float:
            return float(s.query(
                func.coalesce(func.sum(Order.total_amount), 0)
            ).filter(
                Order.status == OrderStatus.COMPLETED,
                Order.created_at >= start,
                Order.created_at < end,
            ).scalar() or 0.0)

        # ── Revenue ───────────────────────────────────────────────────────────
        result["revenue_today"]      = order_revenue(p_start, p_end)
        result["revenue_today_prev"] = order_revenue(prev_start, prev_end)
        result["revenue_weekly"]     = order_revenue(today - timedelta(days=7), now)
        result["revenue_monthly"]    = order_revenue(today - timedelta(days=30), now)

        # 7-day daily sparkline
        spark = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            spark.append(order_revenue(d, d + timedelta(days=1)))
        result["revenue_sparkline"] = _sparkline(spark)
        result["revenue_trend"] = _trend(result["revenue_today"], result["revenue_today_prev"])

        # ── Wallet balance ────────────────────────────────────────────────────
        result["wallet_balance"] = float(
            s.query(func.coalesce(func.sum(User.wallet_balance), 0)).scalar() or 0.0
        )

        # ── Orders ────────────────────────────────────────────────────────────
        result["orders_today"]     = order_count(p_start, p_end)
        result["orders_today_prev"]= order_count(prev_start, prev_end)
        result["orders_pending"]   = order_count(status=OrderStatus.PROCESSING)
        result["orders_completed"] = order_count(p_start, p_end, OrderStatus.COMPLETED)
        result["orders_cancelled"] = order_count(p_start, p_end, OrderStatus.CANCELLED)
        result["orders_total"]     = order_count()
        result["orders_trend"]     = _trend(result["orders_today"], result["orders_today_prev"])

        # Average order value
        result["avg_order_value"] = (
            result["revenue_today"] / result["orders_completed"]
            if result["orders_completed"] > 0 else 0.0
        )

        # ── Users ─────────────────────────────────────────────────────────────
        result["users_total"]     = int(s.query(func.count(User.id)).scalar() or 0)
        result["users_new_today"] = int(s.query(func.count(User.id)).filter(
            User.created_at >= p_start, User.created_at < p_end,
        ).scalar() or 0)
        result["users_new_prev"]  = int(s.query(func.count(User.id)).filter(
            User.created_at >= prev_start, User.created_at < prev_end,
        ).scalar() or 0)
        try:
            result["users_online"] = int(s.query(func.count(User.id)).filter(
                User.last_seen_at >= now - timedelta(hours=24),
            ).scalar() or 0)
        except Exception:
            result["users_online"] = 0
        result["users_trend"] = _trend(result["users_new_today"], result["users_new_prev"])

        # ── Deposits (completed transactions) ─────────────────────────────────
        result["deposits_today"]      = txn_sum(p_start, p_end)
        result["deposits_today_prev"] = txn_sum(prev_start, prev_end)
        result["deposits_trend"]      = _trend(result["deposits_today"], result["deposits_today_prev"])

        # ── Withdrawals ───────────────────────────────────────────────────────
        try:
            from database.models import ReferralWithdrawal
            result["withdrawals_today"] = float(s.query(
                func.coalesce(func.sum(ReferralWithdrawal.amount), 0)
            ).filter(
                ReferralWithdrawal.status.in_(["completed", "approved"]),
                ReferralWithdrawal.created_at >= p_start,
                ReferralWithdrawal.created_at < p_end,
            ).scalar() or 0.0)
        except Exception:
            result["withdrawals_today"] = 0.0

        # ── Failed payments ───────────────────────────────────────────────────
        result["failed_payments"] = int(s.query(func.count(Transaction.id)).filter(
            Transaction.status == TransactionStatus.FAILED,
            Transaction.created_at >= p_start,
            Transaction.created_at < p_end,
        ).scalar() or 0)

        # ── Referral earnings ─────────────────────────────────────────────────
        try:
            from database.models import ReferralCommission
            result["referral_earnings"] = float(s.query(
                func.coalesce(func.sum(ReferralCommission.commission_amount), 0)
            ).filter(
                ReferralCommission.created_at >= p_start,
                ReferralCommission.created_at < p_end,
            ).scalar() or 0.0)
        except Exception:
            result["referral_earnings"] = 0.0

        # ── Best selling products ─────────────────────────────────────────────
        try:
            from database.models import OrderItem
            rows = (
                s.query(Product.name, func.count(OrderItem.id).label("cnt"))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(
                    Order.status == OrderStatus.COMPLETED,
                    Order.created_at >= p_start,
                    Order.created_at < p_end,
                )
                .group_by(Product.id, Product.name)
                .order_by(func.count(OrderItem.id).desc())
                .limit(5).all()
            )
            result["best_products"] = [(r.name, int(r.cnt)) for r in rows]
        except Exception:
            result["best_products"] = []

        # ── Top customers ─────────────────────────────────────────────────────
        try:
            rows = (
                s.query(
                    User.username,
                    User.telegram_id,
                    func.coalesce(func.sum(Order.total_amount), 0).label("spent"),
                )
                .join(Order, Order.user_id == User.id)
                .filter(
                    Order.status == OrderStatus.COMPLETED,
                    Order.created_at >= p_start,
                    Order.created_at < p_end,
                )
                .group_by(User.id, User.username, User.telegram_id)
                .order_by(func.sum(Order.total_amount).desc())
                .limit(5).all()
            )
            result["top_customers"] = [
                (r.username or f"ID:{r.telegram_id}", float(r.spent)) for r in rows
            ]
        except Exception:
            result["top_customers"] = []

        # ── Active coupons ────────────────────────────────────────────────────
        try:
            result["active_coupons"] = int(s.query(func.count(Coupon.id)).filter(
                Coupon.is_active == True,  # noqa: E712
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            ).scalar() or 0)
        except Exception:
            result["active_coupons"] = 0

        # ── Low stock ─────────────────────────────────────────────────────────
        try:
            low_threshold = 5
            result["low_stock"] = int(s.query(func.count(Product.id)).filter(
                Product.is_active == True,  # noqa: E712
                Product.stock_count <= low_threshold,
                Product.stock_count > 0,
            ).scalar() or 0)
            # Low-stock products list for the widget
            low_rows = (s.query(Product.name, Product.stock_count)
                        .filter(
                            Product.is_active == True,  # noqa: E712
                            Product.stock_count <= low_threshold,
                            Product.stock_count > 0,
                        )
                        .order_by(Product.stock_count.asc())
                        .limit(5).all())
            result["low_stock_list"] = [(r.name, r.stock_count) for r in low_rows]
        except Exception:
            result["low_stock"] = 0
            result["low_stock_list"] = []

        # ── System alerts ─────────────────────────────────────────────────────
        alerts: list[str] = []
        if result.get("orders_pending", 0) > 10:
            alerts.append(f"⚠️ {result['orders_pending']} pending orders")
        if result.get("failed_payments", 0) > 0:
            alerts.append(f"🚨 {result['failed_payments']} failed payment(s)")
        if result.get("low_stock", 0) > 0:
            alerts.append(f"📦 {result['low_stock']} low-stock item(s)")
        try:
            open_tickets = int(s.execute(
                text("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
            ).scalar() or 0)
            result["open_tickets"] = open_tickets
            if open_tickets > 0:
                alerts.append(f"🎫 {open_tickets} open ticket(s)")
        except Exception:
            result["open_tickets"] = 0
        result["system_alerts"] = alerts

    return result


# ─── Layout Management ────────────────────────────────────────────────────────

_TABLE_CHECKED: bool | None = None  # simple in-process flag to skip repeated probes


def _table_exists(s) -> bool:
    global _TABLE_CHECKED
    if _TABLE_CHECKED is not None:
        return _TABLE_CHECKED
    try:
        s.execute(text("SELECT 1 FROM admin_dashboard_layouts LIMIT 1"))
        _TABLE_CHECKED = True
    except Exception:
        _TABLE_CHECKED = False
    return bool(_TABLE_CHECKED)


def get_layout(admin_tg_id: int) -> dict[str, Any]:
    """Load admin's layout from DB or return a copy of the default."""
    default = {k: list(v) if isinstance(v, list) else v
               for k, v in DEFAULT_LAYOUT.items()}
    try:
        with get_db_session() as s:
            if not _table_exists(s):
                return default
            row = s.execute(
                text("SELECT layout_json FROM admin_dashboard_layouts "
                     "WHERE admin_tg_id = :tid"),
                {"tid": admin_tg_id},
            ).fetchone()
            if not (row and row[0]):
                return default
            data: dict[str, Any] = json.loads(row[0])
            # Ensure all schema keys present
            data.setdefault("order", WIDGET_IDS.copy())
            data.setdefault("hidden", [])
            data.setdefault("collapsed", [])
            data.setdefault("pinned", [])
            # Keep list clean: only known widgets, add any new ones
            data["order"] = [w for w in data["order"] if w in WIDGET_IDS]
            for wid in WIDGET_IDS:
                if wid not in data["order"]:
                    data["order"].append(wid)
            return data
    except Exception:
        logger.debug("get_layout failed (non-fatal)", exc_info=True)
        return default


def save_layout(admin_tg_id: int, layout: dict[str, Any]) -> None:
    global _TABLE_CHECKED
    try:
        with get_db_session() as s:
            if not _table_exists(s):
                return
            s.execute(text("""
                INSERT INTO admin_dashboard_layouts (admin_tg_id, layout_json, updated_at)
                VALUES (:tid, :lj, NOW())
                ON CONFLICT (admin_tg_id) DO UPDATE
                    SET layout_json = EXCLUDED.layout_json,
                        updated_at  = NOW()
            """), {"tid": admin_tg_id, "lj": json.dumps(layout)})
    except Exception:
        logger.debug("save_layout failed (non-fatal)", exc_info=True)


def toggle_widget(admin_tg_id: int, widget_id: str) -> bool:
    """Toggle widget visibility. Returns True if now visible."""
    layout = get_layout(admin_tg_id)
    if widget_id in layout["hidden"]:
        layout["hidden"].remove(widget_id)
        visible = True
    else:
        layout["hidden"].append(widget_id)
        visible = False
    save_layout(admin_tg_id, layout)
    return visible


def move_widget(admin_tg_id: int, widget_id: str, direction: str) -> None:
    """Move widget up or down in the display order."""
    layout = get_layout(admin_tg_id)
    order = layout["order"]
    if widget_id not in order:
        return
    idx = order.index(widget_id)
    if direction == "up" and idx > 0:
        order[idx], order[idx - 1] = order[idx - 1], order[idx]
    elif direction == "dn" and idx < len(order) - 1:
        order[idx], order[idx + 1] = order[idx + 1], order[idx]
    layout["order"] = order
    save_layout(admin_tg_id, layout)


def toggle_collapse(admin_tg_id: int, widget_id: str) -> bool:
    """Toggle collapsed state. Returns True if now collapsed."""
    layout = get_layout(admin_tg_id)
    if widget_id in layout["collapsed"]:
        layout["collapsed"].remove(widget_id)
        collapsed = False
    else:
        layout["collapsed"].append(widget_id)
        collapsed = True
    save_layout(admin_tg_id, layout)
    return collapsed


def toggle_pin(admin_tg_id: int, widget_id: str) -> bool:
    """Toggle pinned-at-top state. Returns True if now pinned."""
    layout = get_layout(admin_tg_id)
    if widget_id in layout["pinned"]:
        layout["pinned"].remove(widget_id)
        pinned = False
    else:
        layout["pinned"].append(widget_id)
        pinned = True
    save_layout(admin_tg_id, layout)
    return pinned


def reset_layout(admin_tg_id: int) -> None:
    save_layout(admin_tg_id, {k: list(v) if isinstance(v, list) else v
                               for k, v in DEFAULT_LAYOUT.items()})
