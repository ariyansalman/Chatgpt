"""V45 — Restock Notification Service.

Manages user subscriptions for out-of-stock product notifications and sends
Telegram alerts when stock is restored. Integrates with the existing
notification architecture.

Public API (all sync — wrap in asyncio.to_thread from async handlers):
  subscribe(user_id, product_id) -> bool
  unsubscribe(user_id, product_id) -> bool
  is_subscribed(user_id, product_id) -> bool
  get_subscribers(product_id) -> list[dict]
  get_user_subscriptions(user_id) -> list[dict]
  get_all_subscriptions(page, per_page) -> dict
  get_stats() -> dict
  process_restock_notifications(context) -> int  (background job)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func

from database import get_db_session
from database.models import (
    RestockSubscription, RestockNotificationLog, Product, User,
)
from utils.bot_config import cfg
from utils.helpers import sanitize_message

logger = logging.getLogger(__name__)


# ─── Subscribe / Unsubscribe ──────────────────────────────────────────────────

def subscribe(user_id: int, product_id: int) -> bool:
    """Subscribe a user to restock notifications for a product.

    Returns True if a new subscription was created, False if already existed.
    """
    try:
        with get_db_session() as s:
            existing = (s.query(RestockSubscription)
                        .filter_by(user_id=user_id, product_id=product_id)
                        .first())
            if existing:
                return False
            sub = RestockSubscription(user_id=user_id, product_id=product_id)
            s.add(sub)
            s.commit()
            return True
    except Exception:
        logger.exception("subscribe failed uid=%s pid=%s", user_id, product_id)
        return False


def unsubscribe(user_id: int, product_id: int) -> bool:
    """Unsubscribe a user from restock notifications.

    Returns True if the subscription was removed, False if it didn't exist.
    """
    try:
        with get_db_session() as s:
            sub = (s.query(RestockSubscription)
                   .filter_by(user_id=user_id, product_id=product_id)
                   .first())
            if not sub:
                return False
            s.delete(sub)
            s.commit()
            return True
    except Exception:
        logger.exception("unsubscribe failed uid=%s pid=%s", user_id, product_id)
        return False


def is_subscribed(user_id: int, product_id: int) -> bool:
    """Check whether a user is subscribed to restock alerts for a product."""
    try:
        with get_db_session() as s:
            return bool(s.query(RestockSubscription)
                        .filter_by(user_id=user_id, product_id=product_id)
                        .first())
    except Exception:
        return False


# ─── Query helpers ────────────────────────────────────────────────────────────

def get_subscribers(product_id: int) -> list[dict]:
    """Return subscriber records for a product."""
    try:
        with get_db_session() as s:
            rows = (s.query(RestockSubscription, User)
                    .join(User, User.id == RestockSubscription.user_id)
                    .filter(RestockSubscription.product_id == product_id)
                    .order_by(RestockSubscription.subscribed_at.desc())
                    .all())
            return [
                {
                    "sub_id": sub.id,
                    "user_id": sub.user_id,
                    "product_id": sub.product_id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "subscribed_at": sub.subscribed_at,
                    "notified": sub.notified,
                    "notified_at": sub.notified_at,
                }
                for sub, user in rows
            ]
    except Exception:
        logger.exception("get_subscribers failed pid=%s", product_id)
        return []


def get_user_subscriptions(user_id: int) -> list[dict]:
    """Return all active subscriptions for a user."""
    try:
        with get_db_session() as s:
            rows = (s.query(RestockSubscription, Product)
                    .join(Product, Product.id == RestockSubscription.product_id)
                    .filter(RestockSubscription.user_id == user_id)
                    .order_by(RestockSubscription.subscribed_at.desc())
                    .all())
            return [
                {
                    "sub_id": sub.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "product_price": product.price,
                    "stock_count": product.stock_count,
                    "subscribed_at": sub.subscribed_at,
                    "notified": sub.notified,
                }
                for sub, product in rows
            ]
    except Exception:
        logger.exception("get_user_subscriptions failed uid=%s", user_id)
        return []


def get_all_subscriptions(page: int = 1, per_page: int = 20) -> dict:
    """Paginated list of all subscriptions (admin view)."""
    try:
        with get_db_session() as s:
            q = (s.query(RestockSubscription, Product, User)
                 .join(Product, Product.id == RestockSubscription.product_id)
                 .join(User, User.id == RestockSubscription.user_id)
                 .order_by(RestockSubscription.subscribed_at.desc()))
            total = q.count()
            rows = q.offset((page - 1) * per_page).limit(per_page).all()
            items = [
                {
                    "sub_id": sub.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "user_id": user.id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "subscribed_at": sub.subscribed_at,
                    "notified": sub.notified,
                }
                for sub, product, user in rows
            ]
            return {"items": items, "total": total, "page": page, "per_page": per_page,
                    "pages": max(1, (total + per_page - 1) // per_page)}
    except Exception:
        logger.exception("get_all_subscriptions failed")
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 1}


def get_pending_notifications() -> list[dict]:
    """Return products that have come back in stock and have pending subscribers."""
    try:
        with get_db_session() as s:
            subs = (s.query(RestockSubscription, Product, User)
                    .join(Product, Product.id == RestockSubscription.product_id)
                    .join(User, User.id == RestockSubscription.user_id)
                    .filter(
                        RestockSubscription.notified == False,
                        Product.stock_count > 0,
                        Product.is_active == True,
                    )
                    .all())
            return [
                {
                    "sub_id": sub.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "product_price": product.price,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "user_id": user.id,
                }
                for sub, product, user in subs
            ]
    except Exception:
        logger.exception("get_pending_notifications failed")
        return []


def get_notification_logs(product_id: Optional[int] = None,
                          page: int = 1, per_page: int = 20) -> dict:
    """Paginated notification log (admin view)."""
    try:
        with get_db_session() as s:
            q = s.query(RestockNotificationLog).order_by(
                RestockNotificationLog.sent_at.desc())
            if product_id:
                q = q.filter(RestockNotificationLog.product_id == product_id)
            total = q.count()
            rows = q.offset((page - 1) * per_page).limit(per_page).all()
            items = [
                {
                    "id": r.id,
                    "product_id": r.product_id,
                    "product_name": r.product_name_snapshot,
                    "telegram_id": r.telegram_id,
                    "status": r.status,
                    "error": r.error_message,
                    "sent_at": r.sent_at,
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "per_page": per_page,
                    "pages": max(1, (total + per_page - 1) // per_page)}
    except Exception:
        logger.exception("get_notification_logs failed")
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 1}


def get_stats() -> dict:
    """Admin dashboard stats for restock notifications."""
    try:
        with get_db_session() as s:
            total_subs = s.query(RestockSubscription).count()
            pending_subs = s.query(RestockSubscription).filter_by(notified=False).count()
            notified_subs = s.query(RestockSubscription).filter_by(notified=True).count()
            total_sent = s.query(RestockNotificationLog).filter_by(status="sent").count()
            total_failed = s.query(RestockNotificationLog).filter_by(status="failed").count()
            # Products with subscribers
            products_watched = (s.query(func.count(func.distinct(RestockSubscription.product_id)))
                                .scalar() or 0)
            return {
                "total_subscriptions": total_subs,
                "pending": pending_subs,
                "notified": notified_subs,
                "sent_notifications": total_sent,
                "failed_notifications": total_failed,
                "products_watched": products_watched,
            }
    except Exception:
        logger.exception("get_stats failed")
        return {"total_subscriptions": 0, "pending": 0, "notified": 0,
                "sent_notifications": 0, "failed_notifications": 0, "products_watched": 0}


def mark_subscription_notified(sub_id: int) -> None:
    try:
        with get_db_session() as s:
            sub = s.query(RestockSubscription).get(sub_id)
            if sub:
                sub.notified = True
                sub.notified_at = datetime.utcnow()
                s.commit()
    except Exception:
        logger.exception("mark_subscription_notified failed sid=%s", sub_id)


def log_notification(product_id: int, product_name: str,
                     telegram_id: int, status: str, error: Optional[str] = None) -> None:
    try:
        with get_db_session() as s:
            entry = RestockNotificationLog(
                product_id=product_id,
                product_name_snapshot=product_name[:255],
                telegram_id=telegram_id,
                status=status,
                error_message=(error[:512] if error else None),
                sent_at=datetime.utcnow(),
            )
            s.add(entry)
            s.commit()
    except Exception:
        logger.exception("log_notification failed pid=%s", product_id)


def bulk_notify_subscribers(product_id: int) -> int:
    """Admin-triggered: mark all subscribers of a product as 'pending notify'.

    Returns the count of subscribers affected.
    """
    try:
        with get_db_session() as s:
            count = (s.query(RestockSubscription)
                     .filter_by(product_id=product_id, notified=True)
                     .update({"notified": False, "notified_at": None}))
            s.commit()
            return count
    except Exception:
        logger.exception("bulk_notify_subscribers failed pid=%s", product_id)
        return 0


# ─── Background job ───────────────────────────────────────────────────────────

async def process_restock_notifications(context) -> int:
    """Background job: send Telegram alerts for pending restock subscriptions.

    Called by the job queue every N minutes. Returns count of notifications sent.
    """
    pending = await asyncio.to_thread(get_pending_notifications)
    if not pending:
        return 0

    sent = 0
    for item in pending:
        try:
            msg = sanitize_message(
                f"🔔 <b>Back in Stock!</b>\n\n"
                f"📦 <b>{item['product_name']}</b> is now available.\n"
                f"💰 Price: <b>${item['product_price']:.2f}</b>\n\n"
                f"Tap /start to purchase before it sells out!"
            )
            await context.bot.send_message(
                chat_id=item["telegram_id"],
                text=msg,
                parse_mode="HTML",
            )
            mark_subscription_notified(item["sub_id"])
            log_notification(item["product_id"], item["product_name"],
                             item["telegram_id"], "sent")
            sent += 1
        except Exception as e:
            log_notification(item["product_id"], item["product_name"],
                             item["telegram_id"], "failed", str(e))
            logger.warning("restock notify failed uid=%s pid=%s: %s",
                           item["user_id"], item["product_id"], e)

    if sent:
        logger.info("restock_service: sent %d notifications", sent)
    return sent
