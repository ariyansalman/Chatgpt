"""Profit analytics sub-panel.

Revenue = sum(OrderItem.price) on orders with lifecycle_status in
(DELIVERED, COMPLETED). Wallet top-ups and rejected/cancelled orders are
excluded. COGS uses OrderItem.total_cost_snapshot when set (recorded at
delivery time), else falls back to sum of ProductKey.cost_per_unit_snapshot
for keys assigned to that order, else 0.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import func

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_db_session
from database.models import (
    Order, OrderItem, OrderLifecycleStatus, ProductKey, Product,
)
from ._acc_helpers import require_admin, back_root, send, fmt_money


REVENUE_STATUSES = (OrderLifecycleStatus.DELIVERED,
                    OrderLifecycleStatus.COMPLETED)


def _range_metrics(days: int) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    with get_db_session() as s:
        q = (s.query(OrderItem)
              .join(Order, Order.id == OrderItem.order_id)
              .filter(Order.lifecycle_status.in_(REVENUE_STATUSES),
                      Order.created_at >= since))
        revenue = 0.0
        cogs = 0.0
        for oi in q.all():
            revenue += float(oi.price or 0)
            if oi.total_cost_snapshot is not None:
                cogs += float(oi.total_cost_snapshot)
            else:
                # fallback: sum of ProductKey snapshots on this order/product
                keys = s.query(ProductKey).filter(
                    ProductKey.order_id == oi.order_id,
                    ProductKey.product_id == oi.product_id,
                ).all()
                cogs += sum(float(k.cost_per_unit_snapshot or 0) for k in keys)
    return {"revenue": revenue, "cogs": cogs, "profit": revenue - cogs}


@require_admin
async def profit_menu(update, context: ContextTypes.DEFAULT_TYPE):
    d1 = _range_metrics(1)
    d7 = _range_metrics(7)
    d30 = _range_metrics(30)
    t = [
        "📈 <b>PROFIT ANALYTICS</b>",
        "Only DELIVERED/COMPLETED orders. Wallet top-ups excluded.",
        "",
        "<b>Today</b>",
        f"  Revenue: {fmt_money(d1['revenue'])}",
        f"  COGS:    {fmt_money(d1['cogs'])}",
        f"  Profit:  <b>{fmt_money(d1['profit'])}</b>",
        "",
        "<b>Last 7 days</b>",
        f"  Revenue: {fmt_money(d7['revenue'])}",
        f"  COGS:    {fmt_money(d7['cogs'])}",
        f"  Profit:  <b>{fmt_money(d7['profit'])}</b>",
        "",
        "<b>Last 30 days</b>",
        f"  Revenue: {fmt_money(d30['revenue'])}",
        f"  COGS:    {fmt_money(d30['cogs'])}",
        f"  Profit:  <b>{fmt_money(d30['profit'])}</b>",
        "",
        _top_products_block(),
    ]
    from telegram import InlineKeyboardButton
    kb = [[InlineKeyboardButton("🔄 Refresh", callback_data="acc:sec:profit"), back_root()]]
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


def _top_products_block() -> str:
    since = datetime.utcnow() - timedelta(days=30)
    with get_db_session() as s:
        rows = (s.query(OrderItem.product_id,
                        func.sum(OrderItem.price).label("rev"))
                 .join(Order, Order.id == OrderItem.order_id)
                 .filter(Order.lifecycle_status.in_(REVENUE_STATUSES),
                         Order.created_at >= since)
                 .group_by(OrderItem.product_id)
                 .order_by(func.sum(OrderItem.price).desc())
                 .limit(5).all())
        out = ["<b>Top products (30d)</b>"]
        if not rows:
            out.append("  (no sales yet)")
        for pid, rev in rows:
            p = s.get(Product, pid)
            out.append(f"  {p.name if p else pid}: {fmt_money(rev)}")
    return "\n".join(out)


async def route(action, rest, update, context):
    if action == "refresh":
        await profit_menu(update, context)
