"""Order lifecycle transitions with an audit-safe history log.

The legacy ``Order.status`` column stays authoritative for existing code
paths. This module writes to the new ``Order.lifecycle_status`` column plus
the ``order_status_history`` table so admin/user timelines have a rich
picture without breaking legacy queries.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from database import get_db_session
from database.models import (
    Order, OrderStatus, OrderLifecycleStatus,
    OrderStatusHistory,
)

logger = logging.getLogger(__name__)


def _dispatch_invoice_send(order_id: int, bot: Optional[Any] = None) -> None:
    """PDF invoice generation has been removed.

    This function is intentionally a no-op. PDF invoices are no longer
    generated or sent automatically.
    """
    # PDF invoices removed — do nothing.
    return

# Which lifecycle values map back to the 3-state legacy enum.
_LEGACY_MAP = {
    OrderLifecycleStatus.PENDING: OrderStatus.PROCESSING,
    OrderLifecycleStatus.AWAITING_PAYMENT: OrderStatus.PROCESSING,
    OrderLifecycleStatus.PAID: OrderStatus.PROCESSING,
    OrderLifecycleStatus.PROCESSING: OrderStatus.PROCESSING,
    OrderLifecycleStatus.DELIVERED: OrderStatus.COMPLETED,
    OrderLifecycleStatus.COMPLETED: OrderStatus.COMPLETED,
    OrderLifecycleStatus.CANCELLED: OrderStatus.CANCELLED,
    OrderLifecycleStatus.FAILED: OrderStatus.CANCELLED,
    OrderLifecycleStatus.REFUNDED: OrderStatus.CANCELLED,
}


def transition(order_id: int,
               new_status: OrderLifecycleStatus,
               *,
               actor_type: str = "system",
               admin_id: Optional[int] = None,
               reason: Optional[str] = None,
               sync_legacy: bool = True,
               bot: Optional[Any] = None,
               send_invoice: bool = True) -> bool:
    """Move ``order_id`` to ``new_status`` and append a history row.

    Idempotent — a transition to the current status is a no-op that still
    logs the reason (so admin notes are captured).

    The first time an order actually becomes COMPLETED (``order.completed_at``
    flips from None) the completed_at timestamp is set and a history row is
    written. ``send_invoice`` is accepted for backward compatibility but PDF
    invoice generation has been removed — the parameter is ignored.
    """
    just_completed = False
    with get_db_session() as s:
        order = s.query(Order).filter(Order.id == order_id).first()
        if order is None:
            return False
        prev = order.lifecycle_status
        order.lifecycle_status = new_status
        if sync_legacy:
            legacy = _LEGACY_MAP.get(new_status)
            if legacy is not None:
                order.status = legacy
                if legacy == OrderStatus.COMPLETED and order.completed_at is None:
                    order.completed_at = datetime.utcnow()
                    just_completed = True
        s.add(OrderStatusHistory(
            order_id=order.id,
            from_status=prev.name if prev else None,
            to_status=new_status.name,
            actor_type=actor_type,
            admin_id=admin_id,
            reason=(reason or "")[:2000] or None,
        ))
        s.commit()

    if just_completed and send_invoice:
        _dispatch_invoice_send(order_id, bot=bot)

    # V25 — Order Timeline: notify user of status change (best-effort)
    try:
        from services.order_timeline import dispatch_user_notify
        dispatch_user_notify(order_id, new_status, bot)
    except Exception:
        pass

    # V19 — Account & Order Features hooks (best-effort, never raise)
    if just_completed:
        _dispatch_v19_hooks(order_id)

    # Enterprise Admin Notification: order delivered (best-effort, non-blocking)
    #
    # Bugfix: this used to fire on every call with new_status==DELIVERED,
    # including idempotent re-transitions of an order that was already
    # delivered (e.g. delivery_queue.py or redelivery.py re-syncing
    # state). That created a duplicate Notification Center record — and
    # a duplicate DM — for the same order. Gate on ``just_completed`` so
    # it only fires the first time the order actually becomes DELIVERED,
    # and skip entirely when no bot was supplied (``bot=None`` call
    # sites exist purely for state-sync, not user-facing delivery).
    if new_status == OrderLifecycleStatus.DELIVERED and just_completed and bot is not None:
        try:
            import asyncio as _asyncio
            from services.notifications import notify_admins as _notify_admins
            from utils.notify_format import (
                render_order_notification as _render_order,
                dhaka_time_str as _dhaka_ts,
            )
            from utils.helpers import format_order_id as _fmt_order_id, format_price as _fmt_price
            from database.models import OrderItem, Product, User

            with get_db_session() as _s:
                _ord = _s.query(Order).filter_by(id=order_id).first()
                if _ord is None:
                    return True
                _total = _ord.total_amount
                _ord_created = _ord.created_at
                _cust = _s.query(User).filter_by(id=_ord.user_id).first()
                _cust_telegram_id = _cust.telegram_id if _cust else None
                _cust_username = (_cust.username or None) if _cust else None
                _items = (
                    _s.query(OrderItem, Product.name)
                    .join(Product, Product.id == OrderItem.product_id)
                    .filter(OrderItem.order_id == order_id)
                    .all()
                )
                if len(_items) == 1:
                    _oi, _pname = _items[0]
                    _product_display = _pname
                    _qty_display = _oi.quantity
                    _unit_price_display = _fmt_price(_oi.price)
                elif len(_items) > 1:
                    _product_display = ", ".join(f"{pname} ×{oi.quantity}" for oi, pname in _items)
                    _qty_display = sum(oi.quantity for oi, pname in _items)
                    _unit_price_display = None
                else:
                    _product_display = None
                    _qty_display = None
                    _unit_price_display = None

            if _cust_telegram_id is None:
                return True  # no customer to report — skip rather than send a blank line

            _order_display_id = _fmt_order_id(order_id, _ord_created)
            _display_name = f"@{_cust_username}" if _cust_username else f"User {_cust_telegram_id}"
            _merged_notif = _render_order(
                status="completed",
                order_id=_order_display_id,
                customer_name=_display_name,
                telegram_id=_cust_telegram_id,
                product_name=_product_display,
                quantity=_qty_display,
                unit_price=_unit_price_display,
                total_paid=_fmt_price(_total),
                delivery_status="instant",
                order_time=_dhaka_ts(_ord_created),
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(_notify_admins(
                    bot,
                    "order_delivered",
                    _merged_notif,
                ))
            except RuntimeError:
                pass
        except Exception:
            pass

    return True


def _dispatch_v19_hooks(order_id: int) -> None:
    """Fire-and-forget V19 hooks: receipt creation, download records, activity log.

    Called once when an order transitions to COMPLETED for the first time.
    Never raises — these are side effects and must not block/break the order flow.
    """
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_run_v19_hooks(order_id))
        except RuntimeError:
            asyncio.run(_run_v19_hooks(order_id))
    except Exception:
        logger.debug("V19 hooks dispatch failed for order %s", order_id, exc_info=True)


async def _run_v19_hooks(order_id: int) -> None:
    """Async V19 hook runner — receipt, downloads, activity."""
    try:
        from handlers.account_features import (
            create_receipt_record, record_download, log_activity,
        )
        from database import get_db_session, Order, OrderItem, User
        from database.models import ProductType

        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            if not order:
                return
            user = s.query(User).filter_by(id=order.user_id).first()
            if not user:
                return
            items = s.query(OrderItem).filter_by(order_id=order_id).all()

            # Snapshot data before session closes
            user_id_db = user.id
            order_total = float(order.total_amount or 0)
            item_data = []
            for item in items:
                product = item.product
                ptype_name = (
                    product.product_type.name
                    if product and product.product_type
                    else "KEY"
                )
                # Map product type to asset_type label for Download Center
                asset_type_map = {
                    "KEY": "key",
                    "REDEEM_LINK": "redeem_link",
                    "ACCOUNT_LOGIN": "account_login",
                    "DOWNLOADABLE_FILE": "downloadable_file",
                    "FILE": "file",
                    "VOUCHER": "voucher",
                    "SUBSCRIPTION": "subscription",
                    "AUTO_GENERATED": "key",
                    "MANUAL_DELIVERY": "key",
                    "EXTERNAL_DELIVERY": "key",
                    "BUNDLE": "other",
                    "SERVICE": "other",
                    "PREORDER": "other",
                }
                asset_type = asset_type_map.get(ptype_name, "key")
                pname = (product.name if product else f"Product #{item.product_id}")[:255]
                has_content = bool(item.delivered_asset)
                item_data.append({
                    "order_item_id": item.id,
                    "product_id": item.product_id,
                    "product_name": pname,
                    "asset_type": asset_type,
                    "has_content": has_content,
                })

        # Create receipt record (idempotent)
        create_receipt_record(
            order_id=order_id,
            transaction_id=None,
            user_id_db=user_id_db,
            receipt_type="purchase",
        )

        # Record downloads for items that have delivered content
        for idata in item_data:
            if idata["has_content"]:
                record_download(
                    user_id_db=user_id_db,
                    order_id=order_id,
                    order_item_id=idata["order_item_id"],
                    product_id=idata["product_id"],
                    product_name=idata["product_name"],
                    asset_type=idata["asset_type"],
                )

        # Log purchase activity
        log_activity(
            user_id_db=user_id_db,
            action="purchase",
            status="success",
            details=f"Order #{order_id} — ${order_total:.2f}",
            ref_type="order",
            ref_id=str(order_id),
        )

    except Exception:
        logger.debug("_run_v19_hooks failed for order %s", order_id, exc_info=True)


def render_timeline(order_id: int, limit: int = 20) -> str:
    """Return a human-friendly timeline string for the order."""
    lines = []
    with get_db_session() as s:
        rows = (
            s.query(OrderStatusHistory)
            .filter(OrderStatusHistory.order_id == order_id)
            .order_by(OrderStatusHistory.created_at.asc())
            .limit(limit)
            .all()
        )
        for r in rows:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            actor = r.actor_type or "system"
            note = f" — {r.reason}" if r.reason else ""
            lines.append(f"• {when}  [{actor}]  {r.from_status or '—'} → {r.to_status}{note}")
    if not lines:
        return "— no history yet —"
    return "\n".join(lines)