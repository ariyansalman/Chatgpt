"""Admin Advanced Analytics Dashboard — V21.

Revenue by period, order stats, payment breakdowns, wallet, coupons,
referrals, top products/categories/customers, CSV export.

Callback namespace: ``aana:*``
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, text as sqltxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import (
    get_db_session, User, Product, Category, Order, OrderItem,
    Transaction, TransactionStatus, OrderStatus,
    Coupon, CouponRedemption, WalletLedger,
)
from utils.helpers import format_price
from utils.bot_config import cfg
from utils.permissions import has_permission
from config.settings import settings

logger = logging.getLogger(__name__)


def _is_admin(uid: int) -> bool:
    return uid == settings.ADMIN_TELEGRAM_ID or has_permission(uid, "view_analytics")


def _enabled() -> bool:
    return cfg.get_bool("feature_advanced_analytics_enabled", True)


async def _safe_edit(query, text: str, kb=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(data="aana:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


# ── Period helpers ────────────────────────────────────────────────────────

def _period_since(period: str) -> datetime | None:
    now = datetime.utcnow()
    mapping = {
        "1d": now - timedelta(days=1),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "90d": now - timedelta(days=90),
        "all": None,
    }
    return mapping.get(period)


# ── Main menu ─────────────────────────────────────────────────────────────

async def aana_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await _safe_edit(query, "📊 <b>Advanced Analytics</b>\n\n❌ Feature disabled.", _back_kb("acc:root"))
        return

    kb = [
        [InlineKeyboardButton("💰 Revenue Report", callback_data="aana:revenue:30d"),
         InlineKeyboardButton("🛒 Order Stats",    callback_data="aana:orders:30d")],
        [InlineKeyboardButton("💳 Payment Methods", callback_data="aana:payments:30d"),
         InlineKeyboardButton("👛 Wallet Stats",    callback_data="aana:wallet:30d")],
        [InlineKeyboardButton("🎟 Coupon Stats",    callback_data="aana:coupons:30d"),
         InlineKeyboardButton("👥 Referral Stats",  callback_data="aana:referrals:30d")],
        [InlineKeyboardButton("🏆 Top Products",    callback_data="aana:topprod:30d"),
         InlineKeyboardButton("📂 Top Categories",  callback_data="aana:topcat:30d")],
        [InlineKeyboardButton("👑 Top Customers",   callback_data="aana:topcust:30d")],
        [InlineKeyboardButton("📥 Export CSV",      callback_data="aana:export:revenue"),
         InlineKeyboardButton("🔄 Refresh",         callback_data="aana:menu")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    await _safe_edit(query,
        "📊 <b>Advanced Analytics Dashboard</b>\n\n"
        "Select a report to view. Default period: last 30 days.",
        InlineKeyboardMarkup(kb))


# ── Revenue report ────────────────────────────────────────────────────────

async def aana_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        q_ord = s.query(Order).filter(Order.status == OrderStatus.COMPLETED)
        if since:
            q_ord = q_ord.filter(Order.created_at >= since)

        total_rev = float(q_ord.with_entities(
            func.coalesce(func.sum(Order.total_amount), 0.0)).scalar() or 0.0)
        total_orders = q_ord.count()
        avg_order = total_rev / total_orders if total_orders else 0.0

        # Revenue by day (last 7 periods)
        daily_data = []
        for i in range(6, -1, -1):
            day_start = datetime.utcnow().replace(hour=0, minute=0, second=0) - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            rev = s.query(func.coalesce(func.sum(Order.total_amount), 0.0)).filter(
                Order.status == OrderStatus.COMPLETED,
                Order.created_at >= day_start,
                Order.created_at < day_end,
            ).scalar() or 0.0
            daily_data.append((day_start.strftime("%m/%d"), float(rev)))

        # Top-up revenue
        q_txn = s.query(func.coalesce(func.sum(Transaction.amount), 0.0)).filter(
            Transaction.status == TransactionStatus.COMPLETED)
        if since:
            q_txn = q_txn.filter(Transaction.created_at >= since)
        total_topup = float(q_txn.scalar() or 0.0)

    # ASCII mini-chart
    chart = _mini_bar_chart(daily_data)

    text = (
        f"💰 <b>Revenue Report — {period}</b>\n"
        f"{'─' * 30}\n"
        f"Total Revenue: <b>{format_price(total_rev)}</b>\n"
        f"Total Orders:  <b>{total_orders:,}</b>\n"
        f"Avg Order Val: <b>{format_price(avg_order)}</b>\n"
        f"Total Top-ups: <b>{format_price(total_topup)}</b>\n\n"
        f"<b>Daily Revenue (last 7 days):</b>\n<pre>{chart}</pre>"
    )

    kb = _period_kb("revenue", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


def _mini_bar_chart(data: list, width: int = 20) -> str:
    if not data:
        return "(no data)"
    max_val = max(v for _, v in data) or 1
    lines = []
    for label, val in data:
        bar_len = int((val / max_val) * width)
        bar = "█" * bar_len
        lines.append(f"{label} {bar} {format_price(val)}")
    return "\n".join(lines)


def _period_kb(report: str, current: str) -> list:
    periods = [("1d", "24h"), ("7d", "7d"), ("30d", "30d"), ("90d", "90d"), ("all", "All")]
    row = []
    for pval, plabel in periods:
        mark = "✅ " if pval == current else ""
        row.append(InlineKeyboardButton(f"{mark}{plabel}", callback_data=f"aana:{report}:{pval}"))
    return [row]


# ── Order stats ───────────────────────────────────────────────────────────

async def aana_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        def count_status(st):
            q = s.query(func.count(Order.id)).filter(Order.status == st)
            if since:
                q = q.filter(Order.created_at >= since)
            return q.scalar() or 0

        total = count_status(OrderStatus.COMPLETED) + count_status(OrderStatus.PROCESSING) + \
                count_status(OrderStatus.CANCELLED) + count_status(OrderStatus.REFUNDED)
        completed = count_status(OrderStatus.COMPLETED)
        processing = count_status(OrderStatus.PROCESSING)
        cancelled = count_status(OrderStatus.CANCELLED)
        try:
            refunded = count_status(OrderStatus.REFUNDED)
        except Exception:
            refunded = 0

        # Items per order avg
        avg_items = s.query(func.coalesce(func.avg(
            s.query(func.count(OrderItem.id)).filter(OrderItem.order_id == Order.id).correlate(Order).scalar_subquery()
        ), 0)).scalar() or 0

    conv_rate = f"{(completed / total * 100):.1f}%" if total else "0%"

    text = (
        f"🛒 <b>Order Stats — {period}</b>\n"
        f"{'─' * 30}\n"
        f"Total Orders:    <b>{total:,}</b>\n"
        f"✅ Completed:    <b>{completed:,}</b>\n"
        f"⏳ Processing:   <b>{processing:,}</b>\n"
        f"❌ Cancelled:    <b>{cancelled:,}</b>\n"
        f"↩️ Refunded:     <b>{refunded:,}</b>\n\n"
        f"Completion Rate: <b>{conv_rate}</b>\n"
    )
    kb = _period_kb("orders", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Payment method breakdown ──────────────────────────────────────────────

async def aana_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        q = s.query(
            Transaction.payment_method,
            func.count(Transaction.id).label("cnt"),
            func.coalesce(func.sum(Transaction.amount), 0.0).label("total"),
        ).filter(Transaction.status == TransactionStatus.COMPLETED)
        if since:
            q = q.filter(Transaction.created_at >= since)
        rows = q.group_by(Transaction.payment_method).order_by(func.sum(Transaction.amount).desc()).all()

    lines = [f"💳 <b>Payment Breakdown — {period}</b>\n{'─' * 30}"]
    for pm, cnt, total in rows:
        lines.append(f"• <b>{pm}</b>: {cnt} txn · {format_price(float(total))}")
    if not rows:
        lines.append("No completed transactions.")

    kb = _period_kb("payments", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Wallet stats ──────────────────────────────────────────────────────────

async def aana_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        total_balance = s.query(func.coalesce(func.sum(User.wallet_balance), 0.0)).scalar() or 0.0
        users_with_wallet = s.query(func.count(User.id)).filter(User.wallet_balance > 0).scalar() or 0

        # Wallet ledger stats
        try:
            q_led = s.query(
                WalletLedger.reason,
                func.count(WalletLedger.id).label("cnt"),
                func.coalesce(func.sum(WalletLedger.amount), 0.0).label("total"),
            )
            if since:
                q_led = q_led.filter(WalletLedger.created_at >= since)
            ledger_rows = q_led.group_by(WalletLedger.reason).all()
        except Exception:
            ledger_rows = []

    lines = [
        f"👛 <b>Wallet Stats — {period}</b>\n{'─' * 30}",
        f"Total Balances: <b>{format_price(float(total_balance))}</b>",
        f"Users with wallet: <b>{users_with_wallet:,}</b>\n",
        "<b>Ledger Breakdown:</b>",
    ]
    for reason, cnt, total in ledger_rows:
        lines.append(f"• {reason}: {cnt} entries · {format_price(float(total))}")
    if not ledger_rows:
        lines.append("No ledger entries.")

    kb = _period_kb("wallet", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Coupon stats ──────────────────────────────────────────────────────────

async def aana_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        total_coupons = s.query(func.count(Coupon.id)).scalar() or 0
        active_coupons = s.query(func.count(Coupon.id)).filter(Coupon.is_active == True).scalar() or 0  # noqa: E712

        q_red = s.query(
            func.count(CouponRedemption.id),
            func.coalesce(func.sum(CouponRedemption.discount_applied), 0.0),
        )
        if since:
            q_red = q_red.filter(CouponRedemption.created_at >= since)
        redemptions, discount_given = q_red.one()
        redemptions = int(redemptions or 0)
        discount_given = float(discount_given or 0.0)

        # Top coupons
        top_q = (s.query(
            Coupon.code,
            func.count(CouponRedemption.id).label("uses"),
            func.coalesce(func.sum(CouponRedemption.discount_applied), 0.0).label("disc"),
        )
        .join(CouponRedemption, CouponRedemption.coupon_id == Coupon.id))
        if since:
            top_q = top_q.filter(CouponRedemption.created_at >= since)
        top_coupons = top_q.group_by(Coupon.id, Coupon.code).order_by(func.count(CouponRedemption.id).desc()).limit(5).all()

    lines = [
        f"🎟 <b>Coupon Stats — {period}</b>\n{'─' * 30}",
        f"Total Coupons:  <b>{total_coupons}</b>",
        f"Active:         <b>{active_coupons}</b>",
        f"Redemptions:    <b>{redemptions:,}</b>",
        f"Discount Given: <b>{format_price(discount_given)}</b>\n",
        "<b>Top Coupons:</b>",
    ]
    for code, uses, disc in top_coupons:
        lines.append(f"• <code>{code}</code>: {uses} uses · {format_price(float(disc))} off")
    if not top_coupons:
        lines.append("No redemptions.")

    kb = _period_kb("coupons", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Referral stats ────────────────────────────────────────────────────────

async def aana_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        try:
            q_clicks = s.execute(sqltxt(
                "SELECT COUNT(*) FROM referral_clicks"
                + (f" WHERE clicked_at >= '{since.isoformat()}'" if since else "")
            )).scalar() or 0
        except Exception:
            q_clicks = 0
        try:
            q_comm = s.execute(sqltxt(
                "SELECT COUNT(*), COALESCE(SUM(commission_amount),0) FROM referral_commissions"
                + (f" WHERE created_at >= '{since.isoformat()}'" if since else "")
            )).fetchone()
            comm_count, comm_total = (int(q_comm[0] or 0), float(q_comm[1] or 0.0)) if q_comm else (0, 0.0)
        except Exception:
            comm_count = comm_total = 0
        try:
            q_wdraw = s.execute(sqltxt(
                "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM referral_withdrawals WHERE status='pending'"
            )).fetchone()
            wdraw_pend, wdraw_amt = (int(q_wdraw[0] or 0), float(q_wdraw[1] or 0.0)) if q_wdraw else (0, 0.0)
        except Exception:
            wdraw_pend = wdraw_amt = 0

    text = (
        f"👥 <b>Referral Stats — {period}</b>\n{'─' * 30}\n"
        f"Link Clicks:        <b>{q_clicks:,}</b>\n"
        f"Commissions Earned: <b>{comm_count:,}</b>  ({format_price(comm_total)})\n"
        f"Pending Withdrawals:<b>{wdraw_pend}</b>  ({format_price(wdraw_amt)})\n"
    )
    kb = _period_kb("referrals", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Top products ──────────────────────────────────────────────────────────

async def aana_top_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        q = (s.query(
            Product.name,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"),
            func.coalesce(func.sum(OrderItem.quantity * OrderItem.price), 0.0).label("rev"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status == OrderStatus.COMPLETED))
        if since:
            q = q.filter(Order.created_at >= since)
        rows = q.group_by(Product.id, Product.name).order_by(
            func.sum(OrderItem.quantity).desc()).limit(10).all()

    lines = [f"🏆 <b>Top Products — {period}</b>\n{'─' * 30}"]
    for i, (name, qty, rev) in enumerate(rows, 1):
        lines.append(f"{i}. <b>{name}</b> — {int(qty)} sold · {format_price(float(rev))}")
    if not rows:
        lines.append("No sales data.")

    kb = _period_kb("topprod", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Top categories ────────────────────────────────────────────────────────

async def aana_top_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        q = (s.query(
            Category.name,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"),
            func.coalesce(func.sum(OrderItem.quantity * OrderItem.price), 0.0).label("rev"),
        )
        .join(Product, Product.category_id == Category.id)
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status == OrderStatus.COMPLETED))
        if since:
            q = q.filter(Order.created_at >= since)
        rows = q.group_by(Category.id, Category.name).order_by(
            func.sum(OrderItem.quantity).desc()).limit(10).all()

    lines = [f"📂 <b>Top Categories — {period}</b>\n{'─' * 30}"]
    for i, (name, qty, rev) in enumerate(rows, 1):
        lines.append(f"{i}. <b>{name}</b> — {int(qty)} sold · {format_price(float(rev))}")
    if not rows:
        lines.append("No category sales.")

    kb = _period_kb("topcat", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Top customers ─────────────────────────────────────────────────────────

async def aana_top_customers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    period = parts[2] if len(parts) > 2 else "30d"
    since = _period_since(period)

    with get_db_session() as s:
        q = (s.query(
            User.telegram_id,
            User.username,
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total_amount), 0.0).label("spent"),
        )
        .join(Order, Order.user_id == User.id)
        .filter(Order.status == OrderStatus.COMPLETED))
        if since:
            q = q.filter(Order.created_at >= since)
        rows = q.group_by(User.id, User.telegram_id, User.username)\
                .order_by(func.sum(Order.total_amount).desc()).limit(10).all()

    lines = [f"👑 <b>Top Customers — {period}</b>\n{'─' * 30}"]
    for i, (tgid, username, oc, spent) in enumerate(rows, 1):
        name = f"@{username}" if username else str(tgid)
        lines.append(f"{i}. <b>{name}</b> — {oc} orders · {format_price(float(spent))}")
    if not rows:
        lines.append("No customer data.")

    kb = _period_kb("topcust", period)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aana:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── CSV export ────────────────────────────────────────────────────────────

async def aana_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Generating export…")
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    report = parts[2] if len(parts) > 2 else "revenue"

    with get_db_session() as s:
        if report == "revenue":
            rows = s.query(
                Order.id, Order.created_at, Order.total_amount, Order.status
            ).order_by(Order.created_at.desc()).limit(2000).all()
            header = ["order_id", "created_at", "total_amount", "status"]
            data_rows = [(r[0], str(r[1]), r[2], r[3].value if hasattr(r[3], 'value') else str(r[3])) for r in rows]
        elif report == "orders":
            rows = s.query(
                Order.id, Order.created_at, Order.total_amount, Order.status, Order.user_id,
            ).order_by(Order.created_at.desc()).limit(2000).all()
            header = ["order_id", "created_at", "amount", "status", "user_id"]
            data_rows = [(r[0], str(r[1]), r[2], str(r[3]), r[4]) for r in rows]
        else:
            rows = s.query(
                Transaction.id, Transaction.created_at, Transaction.amount,
                Transaction.status, Transaction.payment_method,
            ).order_by(Transaction.created_at.desc()).limit(2000).all()
            header = ["txn_id", "created_at", "amount", "status", "method"]
            data_rows = [(r[0], str(r[1]), r[2], str(r[3]), str(r[4])) for r in rows]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(data_rows)
    buf.seek(0)
    fname = f"analytics_{report}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    try:
        await query.message.reply_document(
            InputFile(io.BytesIO(buf.getvalue().encode()), filename=fname),
            caption=f"📥 <b>{report.capitalize()} export</b> — {len(data_rows)} rows",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("CSV export send failed: %s", e)


# ── Dispatcher (handles all aana:* callbacks not covered by conversations) ─

async def aana_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return await aana_menu(update, context)
    section = parts[1] if len(parts) > 1 else "menu"
    routes = {
        "menu": aana_menu,
        "revenue": aana_revenue,
        "orders": aana_orders,
        "payments": aana_payments,
        "wallet": aana_wallet,
        "coupons": aana_coupons,
        "referrals": aana_referrals,
        "topprod": aana_top_products,
        "topcat": aana_top_categories,
        "topcust": aana_top_customers,
        "export": aana_export,
    }
    handler = routes.get(section, aana_menu)
    await handler(update, context)
