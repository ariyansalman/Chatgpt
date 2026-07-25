"""Price drop alert service.

Called from admin_conversations.py whenever an admin reduces a product's price.
Sends Telegram notifications to all users subscribed to that product.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


def get_subscriber_count(product_id: int) -> int:
    """Return how many users are subscribed to price-drop alerts for a product."""
    try:
        from database import get_db_session
        from database.models import PriceDropAlert
        with get_db_session() as session:
            return session.query(PriceDropAlert).filter_by(product_id=product_id).count()
    except Exception:
        logger.exception("price_alerts.get_subscriber_count failed")
        return 0


def get_total_subscriptions() -> int:
    """Total active price-alert subscriptions across all products."""
    try:
        from database import get_db_session
        from database.models import PriceDropAlert
        with get_db_session() as session:
            return session.query(PriceDropAlert).count()
    except Exception:
        return 0


def _collect_subscribers(product_id: int) -> Tuple[str, List[Tuple[int, int]]]:
    """Return (product_name, [(alert_id, telegram_id), ...]).

    Also marks last_notified_price = new_price on each alert row so we don't
    spam on subsequent non-price-change events.
    """
    from database import get_db_session
    from database.models import PriceDropAlert, User, Product
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            return "", []
        product_name = product.name
        alerts = session.query(PriceDropAlert).filter_by(product_id=product_id).all()
        pairs: List[Tuple[int, int]] = []
        for alert in alerts:
            user = session.query(User).filter_by(id=alert.user_id).first()
            if user:
                pairs.append((alert.id, user.telegram_id))
        return product_name, pairs


async def notify_price_drop_async(
    bot,
    product_id: int,
    old_price: float,
    new_price: float,
) -> int:
    """Async version — call from within an async handler that has a bot instance.

    Returns the number of users successfully notified.
    """
    from utils.bot_config import cfg
    if not cfg.get_bool("feature_price_alerts_enabled", True):
        return 0
    if not cfg.get_bool("feature_price_alerts_auto_notify", True):
        return 0
    if new_price >= old_price:
        return 0  # price did not drop

    try:
        product_name, pairs = _collect_subscribers(product_id)
    except Exception:
        logger.exception("price_alerts: failed to collect subscribers")
        return 0

    if not pairs:
        return 0

    drop_pct = round((old_price - new_price) / old_price * 100, 1)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton("🔕 Unsubscribe Alert", callback_data=f"uf:pa:u:{product_id}")],
    ])
    message = (
        f"🔔 <b>Price Drop Alert!</b>\n\n"
        f"📦 <b>{product_name}</b>\n"
        f"💰 New Price: <b>${new_price:.2f}</b>\n"
        f"📉 Was: ${old_price:.2f}  ▸  saved {drop_pct}%!\n\n"
        f"Tap below to buy now while the price is low."
    )

    # Update last_notified_price in DB
    try:
        from database import get_db_session
        from database.models import PriceDropAlert
        with get_db_session() as session:
            session.query(PriceDropAlert).filter_by(product_id=product_id).update(
                {"last_notified_price": new_price}
            )
    except Exception:
        logger.exception("price_alerts: failed to update last_notified_price")

    notified = 0
    for _alert_id, tg_id in pairs:
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=message,
                reply_markup=kb,
                parse_mode="HTML",
            )
            notified += 1
        except Exception as e:
            logger.debug("price_alerts: could not notify %s: %s", tg_id, e)

    logger.info("price_alerts: notified %d/%d subscribers for product %d", notified, len(pairs), product_id)
    return notified
