"""Marketing Automation (V14) — abandoned-cart reminders + win-back offers.

Two independent, periodically-run campaigns, meant to be driven from the
bot's JobQueue (see ``bot.py``), mirroring the pattern already used by
``services/subscription_service.py``:

  * :func:`send_cart_abandonment_reminders` — finds users with items still
    sitting in their cart 30 minutes / 24 hours after the last cart change,
    and nudges them with an auto-generated discount coupon. The 24h touch is
    an escalation (bigger discount) for anyone the first reminder didn't
    convert.
  * :func:`send_winback_offers` — finds users with no activity for 7 / 30
    days and sends a win-back offer with its own auto-generated coupon.
    "Activity" is the most recent of: last bot interaction
    (``User.last_seen_at``), account creation, or last order.

Every send is deduplicated via ``MarketingTouch`` (see
``database/models.py``): one row per (user, campaign_type, reference_at), so
a periodic re-run of these jobs can never double-message someone for the
same abandoned cart / same inactivity window, while a user who becomes
active again (new cart activity, or a fresh purchase) naturally becomes
eligible again the next time they qualify.

Integrates with ``handlers/admin_broadcast_center.py``, which surfaces a
"🛒 Marketing Automation" panel (ON/OFF toggles, live stats, and manual
"Run now" triggers) inside the existing Broadcast section of the Admin
Control Center — no parallel admin system is introduced.
"""
from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from database import (
    get_db_session, User, Cart, Product, Order, Coupon, DiscountType,
    MarketingTouch, MarketingCampaignType,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

CART_30M = MarketingCampaignType.CART_30M
CART_24H = MarketingCampaignType.CART_24H
WINBACK_7D = MarketingCampaignType.WINBACK_7D
WINBACK_30D = MarketingCampaignType.WINBACK_30D

_CODE_ALPHABET = string.ascii_uppercase + string.digits


# ─────────────────────────────────────────────────────────────────────────
# Coupon generation — one single-use, per-user coupon per touch.
# ─────────────────────────────────────────────────────────────────────────
def _random_suffix(n: int = 6) -> str:
    # secrets.choice (CSPRNG) rather than random.choices — these codes are
    # single-use discount coupons and must not be practically guessable.
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def _create_auto_coupon(session, prefix: str, discount_percent: int,
                        valid_for: timedelta) -> Coupon:
    """Create a single-use percent coupon, retrying on a rare code clash."""
    for _ in range(5):
        code = f"{prefix}-{_random_suffix()}"
        if session.query(Coupon).filter_by(code=code).first():
            continue
        coupon = Coupon(
            code=code,
            discount_type=DiscountType.PERCENT,
            discount_value=float(discount_percent),
            max_uses=1,
            per_user_limit=1,
            expires_at=datetime.utcnow() + valid_for,
            is_active=True,
        )
        session.add(coupon)
        session.flush()  # get coupon.id without committing yet
        return coupon
    raise RuntimeError("Failed to generate a unique auto-coupon code")


def _claim_touch(session, user_id: int, campaign_type: MarketingCampaignType,
                 reference_at: datetime) -> Optional[MarketingTouch]:
    """Atomically claim the (user, campaign_type, reference_at) dedup slot.

    Uses a SAVEPOINT so a duplicate-claim IntegrityError only rolls back
    this one insert, not the caller's whole transaction (which may already
    hold other claimed touches / coupons from earlier targets in the same
    loop) — same technique as ``services/idempotency.py``.

    Returns the claimed touch row on success (coupon_code not yet set), or
    None if someone already claimed this exact moment.
    """
    touch = MarketingTouch(user_id=user_id, campaign_type=campaign_type,
                           reference_at=reference_at, coupon_code=None)
    nested = session.begin_nested()
    session.add(touch)
    try:
        nested.commit()
    except IntegrityError:
        nested.rollback()
        return None
    return touch


# ─────────────────────────────────────────────────────────────────────────
# Abandoned cart detection
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class CartTarget:
    user_id: int
    telegram_id: int
    cart_ref_at: datetime
    item_count: int
    subtotal: float
    sample_names: List[str] = field(default_factory=list)


def _find_stale_carts(session, older_than: timedelta) -> List[CartTarget]:
    """Users whose most-recently-touched cart row is older than ``older_than``."""
    cutoff = datetime.utcnow() - older_than
    rows = (
        session.query(Cart.user_id, func.max(Cart.updated_at).label("ref_at"))
        .group_by(Cart.user_id)
        .having(func.max(Cart.updated_at) <= cutoff)
        .all()
    )
    targets: List[CartTarget] = []
    for user_id, ref_at in rows:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.is_banned:
            continue
        items = session.query(Cart).filter_by(user_id=user_id).all()
        if not items:
            continue
        subtotal = 0.0
        names = []
        for it in items:
            product = session.query(Product).filter_by(id=it.product_id).first()
            if product:
                subtotal += float(product.price) * int(it.quantity or 1)
                names.append(product.name)
        targets.append(CartTarget(
            user_id=user_id, telegram_id=user.telegram_id, cart_ref_at=ref_at,
            item_count=sum(int(it.quantity or 1) for it in items),
            subtotal=round(subtotal, 2), sample_names=names[:3],
        ))
    return targets


def _cart_reminder_text(discount_percent: int, coupon_code: str, expires_at: datetime,
                        target: CartTarget, escalation: bool) -> str:
    items_line = ", ".join(target.sample_names) or "your selected items"
    more = target.item_count - len(target.sample_names)
    if more > 0:
        items_line += f" (+{more} more)"
    header = "⏰ <b>Still thinking it over?</b>" if not escalation else \
             "🔥 <b>Last chance — your cart is about to go cold!</b>"
    return (
        f"{header}\n\n"
        f"You left <b>{items_line}</b> in your cart"
        + (f" (~${target.subtotal:.2f})" if target.subtotal else "") + ".\n\n"
        f"Here's <b>{discount_percent}% off</b> to help you finish checking out:\n"
        f"🎟 Code: <code>{coupon_code}</code>\n"
        f"⌛ Valid until {expires_at:%Y-%m-%d %H:%M} UTC\n\n"
        "Tap 🛒 Cart in the menu to complete your order."
    )


async def send_cart_abandonment_reminders(bot: Bot) -> Dict[str, int]:
    """Run both cart-reminder stages. Returns {'stage_30m': n, 'stage_24h': n}."""
    result = {"stage_30m": 0, "stage_24h": 0}
    if not cfg.get_bool("marketing_cart_reminders_enabled", True):
        return result

    validity = timedelta(hours=max(1, cfg.get_int("marketing_cart_coupon_validity_hours", 48)))

    stages = [
        ("stage_30m", CART_30M,
         timedelta(minutes=max(1, cfg.get_int("marketing_cart_reminder_30m_minutes", 30))),
         cfg.get_int("marketing_cart_reminder_30m_discount_percent", 10),
         "CART10", False),
        ("stage_24h", CART_24H,
         timedelta(hours=max(1, cfg.get_int("marketing_cart_reminder_24h_hours", 24))),
         cfg.get_int("marketing_cart_reminder_24h_discount_percent", 15),
         "CARTVIP", True),
    ]

    for result_key, campaign_type, delay, discount_percent, prefix, escalation in stages:
        with get_db_session() as s:
            targets = _find_stale_carts(s, delay)
            to_send = []
            for t in targets:
                touch = _claim_touch(s, t.user_id, campaign_type, t.cart_ref_at)
                if touch is None:
                    continue  # already sent for this exact cart moment
                coupon = _create_auto_coupon(s, prefix, discount_percent, validity)
                touch.coupon_code = coupon.code
                to_send.append((t, coupon.code, coupon.expires_at))
            s.commit()

        for target, coupon_code, expires_at in to_send:
            try:
                text = _cart_reminder_text(discount_percent, coupon_code, expires_at,
                                           target, escalation)
                await bot.send_message(
                    chat_id=target.telegram_id, text=text, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🛒 View Cart", callback_data="cart")
                    ]]),
                )
                result[result_key] += 1
            except Exception:
                logger.exception("Failed to send %s cart reminder to user_id=%s",
                                 result_key, target.user_id)
    return result


# ─────────────────────────────────────────────────────────────────────────
# Win-back offers for inactive users
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class InactiveTarget:
    user_id: int
    telegram_id: int
    last_active_at: datetime
    has_purchased: bool


def _last_order_map(session) -> Dict[int, datetime]:
    rows = (
        session.query(Order.user_id, func.max(Order.created_at))
        .group_by(Order.user_id)
        .all()
    )
    return {uid: dt for uid, dt in rows}


def _find_inactive_users(session, older_than: timedelta,
                         exclude_more_inactive_than: Optional[timedelta] = None
                         ) -> List[InactiveTarget]:
    """Users whose last known activity is older than ``older_than``.

    If ``exclude_more_inactive_than`` is given, users already past that
    (larger) threshold are skipped here — they belong to the next, more
    senior win-back tier instead, so tiers never overlap.
    """
    now = datetime.utcnow()
    cutoff = now - older_than
    order_map = _last_order_map(session)

    targets: List[InactiveTarget] = []
    users = session.query(User).filter_by(is_banned=False).all()
    for user in users:
        last_order_at = order_map.get(user.id)
        candidates = [user.last_seen_at, user.created_at, last_order_at]
        last_active_at = max((d for d in candidates if d is not None), default=user.created_at)
        if last_active_at > cutoff:
            continue  # not inactive enough yet for this tier
        if exclude_more_inactive_than is not None and last_active_at <= (now - exclude_more_inactive_than):
            continue  # belongs to a more senior tier
        targets.append(InactiveTarget(
            user_id=user.id, telegram_id=user.telegram_id,
            last_active_at=last_active_at, has_purchased=bool(user.has_purchased),
        ))
    return targets


def _winback_text(discount_percent: int, coupon_code: str, expires_at: datetime,
                  tier_days: int) -> str:
    return (
        "💌 <b>We miss you!</b>\n\n"
        f"It's been a while since your last visit. Come back and enjoy "
        f"<b>{discount_percent}% off</b> your next order:\n"
        f"🎟 Code: <code>{coupon_code}</code>\n"
        f"⌛ Valid until {expires_at:%Y-%m-%d %H:%M} UTC\n\n"
        "We'd love to see you again! 🛍"
    )


async def send_winback_offers(bot: Bot) -> Dict[str, int]:
    """Run both win-back tiers. Returns {'tier_7d': n, 'tier_30d': n}."""
    result = {"tier_7d": 0, "tier_30d": 0}
    if not cfg.get_bool("marketing_winback_enabled", True):
        return result

    validity = timedelta(days=max(1, cfg.get_int("marketing_winback_coupon_validity_days", 7)))
    days_7 = max(1, cfg.get_int("marketing_winback_7d_days", 7))
    days_30 = max(days_7 + 1, cfg.get_int("marketing_winback_30d_days", 30))

    tiers = [
        ("tier_7d", WINBACK_7D, timedelta(days=days_7), timedelta(days=days_30),
         cfg.get_int("marketing_winback_7d_discount_percent", 10), "WELCOMEBACK", days_7),
        ("tier_30d", WINBACK_30D, timedelta(days=days_30), None,
         cfg.get_int("marketing_winback_30d_discount_percent", 20), "COMEBACK", days_30),
    ]

    for result_key, campaign_type, older_than, exclude_more, discount_percent, prefix, tier_days in tiers:
        with get_db_session() as s:
            targets = _find_inactive_users(s, older_than, exclude_more)
            to_send = []
            for t in targets:
                touch = _claim_touch(s, t.user_id, campaign_type, t.last_active_at)
                if touch is None:
                    continue  # already sent for this exact inactivity window
                coupon = _create_auto_coupon(s, prefix, discount_percent, validity)
                touch.coupon_code = coupon.code
                to_send.append((t, coupon.code, coupon.expires_at))
            s.commit()

        for target, coupon_code, expires_at in to_send:
            try:
                text = _winback_text(discount_percent, coupon_code, expires_at, tier_days)
                await bot.send_message(
                    chat_id=target.telegram_id, text=text, parse_mode=ParseMode.HTML,
                )
                result[result_key] += 1
            except Exception:
                logger.exception("Failed to send %s win-back offer to user_id=%s",
                                 result_key, target.user_id)
    return result


# ─────────────────────────────────────────────────────────────────────────
# Admin panel helpers (used by handlers/admin_broadcast_center.py)
# ─────────────────────────────────────────────────────────────────────────
def get_stats() -> dict:
    """Snapshot for the admin "🛒 Marketing Automation" panel."""
    with get_db_session() as s:
        pending_carts = s.query(Cart.user_id).distinct().count()
        today = datetime.utcnow() - timedelta(days=1)
        week = datetime.utcnow() - timedelta(days=7)
        sent_today = s.query(MarketingTouch).filter(MarketingTouch.sent_at >= today).count()
        sent_week = s.query(MarketingTouch).filter(MarketingTouch.sent_at >= week).count()
        by_type = dict(
            s.query(MarketingTouch.campaign_type, func.count(MarketingTouch.id))
            .filter(MarketingTouch.sent_at >= week)
            .group_by(MarketingTouch.campaign_type)
            .all()
        )
    return {
        "pending_carts": pending_carts,
        "sent_today": sent_today,
        "sent_week": sent_week,
        "by_type_week": {k.value: v for k, v in by_type.items()},
        "cart_reminders_enabled": cfg.get_bool("marketing_cart_reminders_enabled", True),
        "winback_enabled": cfg.get_bool("marketing_winback_enabled", True),
    }


# ─────────────────────────────────────────────────────────────────────────
# JobQueue entry points
# ─────────────────────────────────────────────────────────────────────────
async def cart_reminder_job(context) -> None:
    try:
        outcome = await send_cart_abandonment_reminders(context.bot)
        if outcome["stage_30m"] or outcome["stage_24h"]:
            logger.info("Abandoned-cart reminders sent: 30m=%d 24h=%d",
                       outcome["stage_30m"], outcome["stage_24h"])
    except Exception:
        logger.exception("marketing_automation cart_reminder_job failed")


async def winback_job(context) -> None:
    try:
        outcome = await send_winback_offers(context.bot)
        if outcome["tier_7d"] or outcome["tier_30d"]:
            logger.info("Win-back offers sent: 7d=%d 30d=%d",
                       outcome["tier_7d"], outcome["tier_30d"])
    except Exception:
        logger.exception("marketing_automation winback_job failed")
