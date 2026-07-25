"""Admin analytics dashboard — sales, revenue, users, top products,
cohort retention, customer lifetime value (LTV), and churn rate."""

from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
from database import (
    get_db_session, User, Order, OrderItem, Product,
    Transaction, TransactionStatus, OrderStatus,
    CouponRedemption, ReferralReward,
)
from utils.helpers import format_price
from config.settings import settings
from services.customer_analytics import (
    compute_cohort_retention, compute_ltv, compute_churn_rate,
)
from telegram.error import BadRequest


def _is_admin(update: Update) -> bool:
    return update.effective_user.id == settings.ADMIN_TELEGRAM_ID


def _stats(session, since: datetime | None):
    q_orders = session.query(Order).filter(Order.status == OrderStatus.COMPLETED)
    q_txn = session.query(Transaction).filter(Transaction.status == TransactionStatus.COMPLETED)
    if since:
        q_orders = q_orders.filter(Order.created_at >= since)
        q_txn = q_txn.filter(Transaction.created_at >= since)
    orders_count = q_orders.count()
    revenue = q_orders.with_entities(func.coalesce(func.sum(Order.total_amount), 0.0)).scalar() or 0.0
    topups = q_txn.with_entities(func.coalesce(func.sum(Transaction.amount), 0.0)).scalar() or 0.0
    return orders_count, float(revenue), float(topups)


def _top_products(session, since: datetime | None, limit=5):
    q = (session.query(
            Product.name,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"),
            func.coalesce(func.sum(OrderItem.quantity * OrderItem.price), 0).label("rev"),
         )
         .join(OrderItem, OrderItem.product_id == Product.id)
         .join(Order, Order.id == OrderItem.order_id)
         .filter(Order.status == OrderStatus.COMPLETED))
    if since:
        q = q.filter(Order.created_at >= since)
    q = q.group_by(Product.id, Product.name).order_by(func.sum(OrderItem.quantity).desc()).limit(limit)
    return q.all()


async def admin_analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    query = update.callback_query
    await query.answer()

    now = datetime.utcnow()
    d1 = now - timedelta(days=1)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    with get_db_session() as session:
        total_users = session.query(func.count(User.id)).scalar() or 0
        banned = session.query(func.count(User.id)).filter(User.is_banned == True).scalar() or 0
        new_7d = session.query(func.count(User.id)).filter(User.created_at >= d7).scalar() or 0
        buyers = session.query(func.count(User.id)).filter(User.has_purchased == True).scalar() or 0

        o_all, rev_all, top_all = _stats(session, None)
        o_1d, rev_1d, top_1d = _stats(session, d1)
        o_7d, rev_7d, top_7d = _stats(session, d7)
        o_30d, rev_30d, top_30d = _stats(session, d30)

        coupon_redemptions = session.query(func.count(CouponRedemption.id)).scalar() or 0
        coupon_discount = session.query(func.coalesce(func.sum(CouponRedemption.discount_applied), 0.0)).scalar() or 0.0
        ref_rewards = session.query(func.count(ReferralReward.id)).scalar() or 0
        ref_paid = session.query(func.coalesce(func.sum(ReferralReward.amount), 0.0)).scalar() or 0.0

        from utils.bot_config import cfg as _cfg
        _low_th = _cfg.get_int("low_stock_threshold", 5)
        low_stock = (session.query(Product)
                     .filter(Product.is_active == True, Product.stock_count <= _low_th)
                     .order_by(Product.stock_count.asc()).limit(5).all())

        top_products = _top_products(session, d30, limit=5)

        churn = compute_churn_rate(session)
        ltv = compute_ltv(session)

    def line(orders, rev, topups):
        return f"orders {orders} · revenue {format_price(rev)} · top-ups {format_price(topups)}"

    msg = (
        "📊 Analytics Dashboard\n"
        "─────────────────\n"
        f"👥 Users: {total_users} (buyers {buyers}, banned {banned}, new 7d {new_7d})\n\n"
        "💰 Sales\n"
        f"• 24h: {line(o_1d, rev_1d, top_1d)}\n"
        f"• 7d : {line(o_7d, rev_7d, top_7d)}\n"
        f"• 30d: {line(o_30d, rev_30d, top_30d)}\n"
        f"• All: {line(o_all, rev_all, top_all)}\n\n"
        "🏷 Coupons\n"
        f"• {coupon_redemptions} redemptions · {format_price(coupon_discount)} discount given\n\n"
        "👑 Referrals\n"
        f"• {ref_rewards} rewards · {format_price(ref_paid)} paid out\n"
    )

    if top_products:
        msg += "\n🏆 Top Products (30d)\n"
        for i, (name, qty, rev) in enumerate(top_products, 1):
            msg += f"{i}. {name} — {int(qty)} sold · {format_price(float(rev))}\n"

    if low_stock:
        msg += f"\n⚠️ Low Stock (≤{_low_th})\n"
        for p in low_stock:
            msg += f"• {p.name}: {p.stock_count}\n"

    msg += (
        "\n📈 Growth\n"
        f"• Avg LTV: {format_price(ltv.overall_avg_ltv)} · "
        f"Median LTV: {format_price(ltv.overall_median_ltv)} ({ltv.paying_customers} buyers)\n"
        f"• Churn ({churn.inactive_days}d): {churn.churn_rate_pct}% "
        f"({churn.churned_customers}/{churn.paying_customers} buyers inactive)\n"
    )

    kb = [
        [InlineKeyboardButton("📈 Cohort Retention", callback_data="admin_analytics_cohort")],
        [InlineKeyboardButton("💎 LTV Breakdown", callback_data="admin_analytics_ltv"),
         InlineKeyboardButton("📉 Churn Detail", callback_data="admin_analytics_churn")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")],
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────
# Cohort retention view
# ─────────────────────────────────────────────────────────────────────

async def admin_cohort_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monthly signup-cohort retention table (month-0 / month-1 / ... %)."""
    if not _is_admin(update):
        return
    query = update.callback_query
    await query.answer()

    with get_db_session() as session:
        rows = compute_cohort_retention(session, num_cohorts=6, num_periods=6)

    msg = "📈 Cohort Retention (by signup month)\n─────────────────\n"
    if not rows:
        msg += "No signup data yet."
    else:
        header = "Cohort   Size  " + "  ".join(f"M{i}" for i in range(len(rows[0].retention_pct)))
        msg += f"<code>{header}</code>\n"
        for r in rows:
            cells = []
            for pct in r.retention_pct:
                cells.append("  - " if pct is None else f"{pct:>4.0f}%")
            line = f"{r.cohort_label:<8} {r.size:>4}  " + " ".join(cells)
            msg += f"<code>{line}</code>\n"
        msg += (
            "\n<i>M0 = signup month itself, M1 = following month, etc. "
            "Each cell is the % of that cohort with ≥1 completed order "
            "in that month.</i>"
        )

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics_cohort")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_analytics")],
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────
# LTV breakdown view
# ─────────────────────────────────────────────────────────────────────

async def admin_ltv_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer Lifetime Value — overall, per-cohort, and top spenders."""
    if not _is_admin(update):
        return
    query = update.callback_query
    await query.answer()

    with get_db_session() as session:
        ltv = compute_ltv(session, num_cohorts=6, top_n=5)
        top_ids = [uid for uid, _amt in ltv.top_customers]
        users_by_id = {}
        if top_ids:
            for u in session.query(User).filter(User.id.in_(top_ids)).all():
                users_by_id[u.id] = u

    msg = (
        "💎 Customer Lifetime Value (LTV)\n─────────────────\n"
        f"• Avg LTV (per paying customer): {format_price(ltv.overall_avg_ltv)}\n"
        f"• Median LTV: {format_price(ltv.overall_median_ltv)}\n"
        f"• Paying customers: {ltv.paying_customers}\n"
        f"• Total lifetime revenue: {format_price(ltv.total_ltv)}\n"
    )

    if ltv.per_cohort_avg_ltv:
        msg += "\n📅 Avg LTV per Signup Cohort (all users incl. non-buyers)\n"
        for label, avg, size in ltv.per_cohort_avg_ltv:
            msg += f"• {label} ({size} signups): {format_price(avg)}\n"

    if ltv.top_customers:
        msg += "\n🏆 Top Customers by Lifetime Spend\n"
        for i, (uid, amount) in enumerate(ltv.top_customers, 1):
            u = users_by_id.get(uid)
            name = f"@{u.username}" if (u and u.username) else f"user#{uid}"
            msg += f"{i}. {name} — {format_price(amount)}\n"

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics_ltv")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_analytics")],
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────
# Churn rate view
# ─────────────────────────────────────────────────────────────────────

async def admin_churn_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Churn rate — % of past buyers inactive beyond the churn window."""
    if not _is_admin(update):
        return
    query = update.callback_query
    await query.answer()

    with get_db_session() as session:
        churn = compute_churn_rate(session)

    msg = (
        "📉 Churn Rate\n─────────────────\n"
        f"• Inactivity window: {churn.inactive_days} days "
        "(configurable via Bot Configuration → Segmentation)\n"
        f"• Paying customers: {churn.paying_customers}\n"
        f"• Churned (no order in window): {churn.churned_customers}\n"
        f"• Churn rate: {churn.churn_rate_pct}%\n"
        f"• Retention rate: {churn.retained_rate_pct}%\n"
    )

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics_churn")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_analytics")],
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
