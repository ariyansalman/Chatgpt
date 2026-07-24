"""Safe manual redelivery service.

Reuses already-allocated inventory (never allocates new keys) and delegates to
the V11 ``services.delivery_service.dispatch`` for non-legacy product types
which itself is idempotent (checks ``OrderItem.delivered_asset``,
``ManualDeliveryTask``, ``Preorder``, ``Subscription``, ``ExternalDeliveryLog``
before doing any work).

For legacy KEY/FILE orders the previously-persisted ``delivered_asset`` on the
``OrderItem`` is returned as-is — no new ``ProductKey`` is consumed and no new
download link is issued. This means an admin pressing "Resend Delivery" 5
times will resend the same asset 5 times but will never re-allocate stock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from database import get_db_session
from database.models import (
    Order, OrderItem, Product, ProductType, DeliveryStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class RedeliveryPayload:
    order_id: int
    item_id: int
    product_name: str
    product_type: ProductType
    text_summary: str          # Safe message for user (never full secrets in logs)
    telegram_file_id: Optional[str] = None
    telegram_file_type: Optional[str] = None
    download_link: Optional[str] = None
    keys: Optional[List[str]] = None
    already_delivered: bool = False
    error: Optional[str] = None


def prepare_redelivery(order_id: int, item_id: int) -> RedeliveryPayload:
    """Return a delivery payload for the given order-item WITHOUT re-allocating.

    This never mutates inventory. It only reads what was previously assigned
    and returns it for the admin flow to resend.
    """
    with get_db_session() as s:
        order = s.query(Order).filter_by(id=order_id).first()
        if not order:
            return RedeliveryPayload(order_id, item_id, "", ProductType.KEY, "",
                                     error="Order not found")
        item = s.query(OrderItem).filter_by(id=item_id, order_id=order_id).first()
        if not item:
            return RedeliveryPayload(order_id, item_id, "", ProductType.KEY, "",
                                     error="Order item not found")
        product: Optional[Product] = item.product
        if product is None:
            return RedeliveryPayload(order_id, item_id, "", ProductType.KEY, "",
                                     error="Product no longer exists")

        payload = RedeliveryPayload(
            order_id=order_id,
            item_id=item_id,
            product_name=product.name,
            product_type=product.product_type,
            text_summary="",
        )

        ptype = product.product_type
        # ── Legacy KEY: reuse the delivered_asset (\n-joined keys) ─────
        if ptype == ProductType.KEY:
            if not item.delivered_asset:
                payload.error = "No keys were ever assigned to this order"
                return payload
            keys = [k for k in item.delivered_asset.splitlines() if k.strip()]
            payload.keys = keys
            payload.already_delivered = True
            payload.text_summary = (
                f"🔐 Keys ({len(keys)}) for {product.name} — resent from record."
            )
            return payload

        # ── Legacy FILE: reuse download_link ────────────────────────────
        if ptype == ProductType.FILE:
            link = item.delivered_asset or product.download_link
            if not link:
                payload.error = "No download link on record"
                return payload
            payload.download_link = link
            payload.already_delivered = True
            payload.text_summary = f"🔗 Download link for {product.name} — resent."
            return payload

        # ── Downloadable File (V11): re-send the same telegram_file_id ─
        if ptype == ProductType.DOWNLOADABLE_FILE:
            if not product.telegram_file_id:
                payload.error = "No file_id configured for this product"
                return payload
            payload.telegram_file_id = product.telegram_file_id
            payload.telegram_file_type = product.telegram_file_type or "document"
            payload.already_delivered = True
            payload.text_summary = f"📁 File for {product.name} — resent."
            return payload

        # ── AUTO_GENERATED, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER,
        #    MANUAL_DELIVERY, PREORDER, SUBSCRIPTION, BUNDLE, SERVICE,
        #    EXTERNAL_DELIVERY: delegate to dispatcher, which is idempotent
        #    and returns the same prior delivered_asset on replay.
        try:
            from services.delivery_service import dispatch as _dispatch
            res = _dispatch(order_id)
        except Exception as e:
            logger.exception("Redelivery dispatch failed for order %s", order_id)
            payload.error = f"Dispatcher error: {e}"
            return payload

        if res is None or not res.handled:
            payload.error = "Delivery type has no dispatcher handler"
            return payload
        if res.error:
            payload.error = res.error
            return payload
        payload.text_summary = res.user_message or f"Delivery resent for {product.name}."
        payload.already_delivered = bool(res.idempotent_replay)
        return payload


def mark_redelivery(order_id: int, item_id: int, admin_id: int) -> None:
    """Log the redelivery attempt to OrderStatusHistory."""
    try:
        from services.order_lifecycle import transition
        from database.models import OrderLifecycleStatus
        # Log as a history entry without changing lifecycle_status if already terminal.
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            if order is None:
                return
            # Just record a history row via the transition helper's audit path
            # by re-setting to the same lifecycle status (idempotent) so the
            # history table captures the actor + reason.
            current = order.lifecycle_status or OrderLifecycleStatus.DELIVERED
            order.delivery_status = DeliveryStatus.REDELIVERED
            s.commit()
        transition(
            order_id, current,
            actor_type="admin", admin_id=admin_id,
            reason=f"Manual redelivery of item #{item_id}",
        )
    except Exception:
        logger.exception("mark_redelivery bookkeeping failed")
