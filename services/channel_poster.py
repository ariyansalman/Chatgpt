"""services/channel_poster.py — auto-post new products & restocks to a
Telegram channel (V18: Channel Auto-Post).

Configuration lives in the existing ``bot_config`` key/value settings table
(``utils.bot_config``), the same mechanism already used for things like
``restock_broadcast_enabled``. Two keys back this feature:

  - ``channel_autopost_enabled``     (bool) — master ON/OFF switch.
  - ``channel_autopost_channel_id``  (str)  — numeric channel id
    (e.g. ``-1001234567890``) or public ``@username`` to post to.

Both are editable from the admin panel under
⚙️ Bot Settings → 📢 Broadcast, with no extra UI code needed (the generic
bot_config editor renders bool toggles and free-text fields automatically).

Every public function here is best-effort: any failure (missing channel,
bot not an admin there, network error, etc.) is logged and swallowed so a
channel-posting problem can never break product creation or restocking.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from database import get_db_session, Product
from utils.helpers import format_price
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


def _channel_target() -> Optional[str]:
    """Return the configured channel id/@username, or ``None`` when
    auto-post is off or no channel has been configured yet."""
    if not cfg.get_bool("channel_autopost_enabled", False):
        return None
    channel = (cfg.get_str("channel_autopost_channel_id", "") or "").strip()
    return channel or None


async def _bot_username(bot) -> Optional[str]:
    """Resolve the bot's own @username for building the deep-link, the same
    way ``handlers/referral_handlers.py`` builds referral links."""
    try:
        me = await bot.get_me()
        return me.username
    except Exception:
        logger.exception("channel_poster: could not resolve bot username")
        return None


def _build_caption(name: str, price: float, description: Optional[str],
                    heading: str, stock_line: str = "") -> str:
    lines = [heading, "", f"📦 {name}", f"💰 {format_price(price)}"]
    if description:
        desc = description.strip()
        if len(desc) > 300:
            desc = desc[:300].rstrip() + "…"
        lines += ["", desc]
    if stock_line:
        lines += ["", stock_line]
    return "\n".join(lines)


async def _post(bot, product_id: int, heading: str, stock_line: str = "") -> bool:
    """Shared post routine used by both public entry points below.

    Returns True when a channel message was actually sent, False otherwise
    (auto-post off, no channel configured, product missing, or send failed).
    """
    channel = _channel_target()
    if not channel:
        return False

    with get_db_session() as session:
        product = session.query(Product).filter_by(
            id=product_id, is_active=True).first()
        if not product:
            return False
        name = product.name
        price = product.price
        description = product.description
        image_path = product.image_path

    username = await _bot_username(bot)
    if username:
        buy_url = f"https://t.me/{username}?start=product_{product_id}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 Buy Now", url=buy_url)]])
    else:
        # Deep-link couldn't be resolved (rare) — still post, just without
        # a working Buy Now button rather than skipping the post entirely.
        keyboard = None

    caption = _build_caption(name, price, description, heading, stock_line)

    try:
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as image:
                await bot.send_photo(
                    chat_id=channel, photo=image,
                    caption=caption, reply_markup=keyboard,
                )
        else:
            await bot.send_message(
                chat_id=channel, text=caption, reply_markup=keyboard,
            )
        return True
    except TelegramError:
        logger.exception(
            "channel_poster: failed to post product_id=%s to channel=%s",
            product_id, channel,
        )
        return False
    except Exception:
        logger.exception(
            "channel_poster: unexpected error posting product_id=%s", product_id)
        return False


async def post_new_product(bot, product_id: int) -> bool:
    """Best-effort auto-post for a brand-new product. Call this right after
    a product is successfully created (and, when key-backed, after its
    initial inventory has been saved so the image/price/etc. are final).
    """
    return await _post(bot, product_id, heading="🆕 New Product Added!")


async def post_restock(bot, product_id: int,
                        variant_id: Optional[int] = None,
                        available: Optional[int] = None) -> bool:
    """Best-effort auto-post for a restock event.

    Callers should invoke this on a genuine 0 → >0 available-stock
    transition (mirrors ``handlers/admin_broadcast_center.send_restock_broadcast``),
    the same trigger point already used for the eligible-users broadcast.
    ``available`` is optional — pass the freshly computed available count to
    show it in the post; omit it to skip the stock line.
    """
    stock_line = f"✅ In stock: {available}" if available is not None else ""
    return await _post(
        bot, product_id, heading="🔔 Back in Stock!", stock_line=stock_line)
