"""Refer & Earn handlers (English-only system UI)."""

import logging
from urllib.parse import quote
from telegram import Update
from telegram.ext import ContextTypes
from database import get_db_session, User, Settings
from utils import (
    create_refer_keyboard, create_main_menu_keyboard,
    check_user_banned, sanitize_message,
)
from utils.bot_config import cfg
from i18n import t, get_user_language
from telegram.error import BadRequest


def _commission_pct() -> float:
    """The admin-configured referral commission percentage
    (Admin Panel → 👥 Advanced Referrals → Referral Commission %).
    0 = referral commissions disabled."""
    return cfg.get_float("referral_commission_pct", 5.0)


def _fmt_pct(pct: float) -> str:
    return f"{pct:g}%"


async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    lang = get_user_language(telegram_id)

    if check_user_banned(telegram_id):
        try:
            await query.edit_message_text(t("common.banned", lang))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        earned = float(user.referral_earnings or 0.0) if user else 0.0
        count = session.query(User).filter_by(referred_by_id=user.id).count() if user else 0

        s = session.query(Settings).first()
        enabled = s.referral_enabled if s else True

    if not enabled:
        try:
            await query.edit_message_text(
                t("referral.disabled", lang),
                reply_markup=create_main_menu_keyboard(lang=lang, user_id=telegram_id),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{telegram_id}"
    share_url = f"https://t.me/share/url?url={quote(link)}&text={quote(t('referral.share_text', lang))}"

    pct_str = _fmt_pct(_commission_pct())
    text = (
        "👥 <b>Referral Program</b>\n\n"
        f"Invite new customers and earn a <b>{pct_str} commission</b> on every successful purchase they make.\n\n"
        "<b>How It Works</b>\n\n"
        "1️⃣ Share your personal referral link.\n"
        "2️⃣ A new customer joins using your link.\n"
        "3️⃣ They complete a successful purchase.\n"
        f"4️⃣ You automatically receive a {pct_str} commission.\n\n"
        "📊 <b>Referral Statistics</b>\n\n"
        f"👥 Total Referrals: <b>{count}</b>\n"
        f"💰 Total Commission Earned: <b>${earned:.2f}</b>\n\n"
        "🔗 <b>Your Referral Link</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Your commission is credited automatically after every eligible completed order."
    )

    try:
        await query.edit_message_text(
            text,
            reply_markup=create_refer_keyboard(lang, share_url),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def process_referral_reward(
    context: ContextTypes.DEFAULT_TYPE,
    buyer_telegram_id: int,
    order_id: int = None,
    order_amount: float = 0.0,
):
    """Credit the referrer a commission on every eligible completed order,
    at the percentage configured in the Admin Panel
    (👥 Advanced Referrals → Referral Commission %; 0 = disabled).

    Fires for every completed order placed by a referred user (not just the first).
    Idempotent per order_id: will never credit the same order twice.
    Skips if the referral programme is disabled or the buyer has no referrer.
    """
    from database import ReferralReward
    referrer_telegram_id = None
    commission = 0.0
    try:
        with get_db_session() as session:
            buyer = session.query(User).filter_by(telegram_id=buyer_telegram_id).first()
            if not buyer or not buyer.referred_by_id:
                return

            s = session.query(Settings).first()
            if s and not s.referral_enabled:
                return

            # Commission % on the completed order amount — admin-configurable,
            # 0 = referral commissions disabled.
            commission = round(float(order_amount or 0.0) * (_commission_pct() / 100.0), 4)
            if commission <= 0:
                return

            # Idempotency: one commission credit per order_id
            if order_id:
                existing = session.query(ReferralReward).filter_by(
                    referred_id=buyer.id, order_id=order_id
                ).first()
                if existing:
                    return

            referrer = session.query(User).filter_by(id=buyer.referred_by_id).first()
            if not referrer:
                return

            # Mark buyer as having purchased (backward compat)
            if not buyer.has_purchased:
                buyer.has_purchased = True

            referrer.wallet_balance = (referrer.wallet_balance or 0.0) + commission
            referrer.referral_earnings = (referrer.referral_earnings or 0.0) + commission
            session.add(ReferralReward(
                referrer_id=referrer.id,
                referred_id=buyer.id,
                order_id=order_id,
                amount=commission,
            ))
            session.commit()
            referrer_telegram_id = referrer.telegram_id
    except Exception as e:
        logging.getLogger(__name__).warning("[referral] commission failed: %s", e)
        return

    if referrer_telegram_id:
        try:
            await context.bot.send_message(
                chat_id=referrer_telegram_id,
                text=(
                    "💰 <b>Referral Commission Received</b>\n\n"
                    f"You earned <b>${commission:.2f}</b> (5% commission) "
                    "from a purchase by your referred customer.\n\n"
                    "The amount has been credited to your wallet."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logging.getLogger(__name__).warning("[referral] notify failed: %s", e)

        # Activity Feed: referral reward (best-effort, non-blocking)
        try:
            import asyncio as _asyncio
            from services.activity_feed import post_event as _af_post, EVENT_REFERRAL_REWARD
            _asyncio.create_task(_af_post(context.bot, EVENT_REFERRAL_REWARD, {
                "referrer_telegram_id": referrer_telegram_id,
                "amount": commission,
                "order_id": order_id or "—",
            }))
        except Exception:
            pass


# ─── Admin: referral settings ───────────────────────────────────────────────
from telegram.ext import ConversationHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utils import is_admin

REFERRAL_AMOUNT_INPUT = 9001


async def admin_referral_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings()
            session.add(s)
        s.referral_enabled = not bool(s.referral_enabled)
        session.commit()
        state = "ENABLED ✅" if s.referral_enabled else "DISABLED ❌"
    try:
        await q.edit_message_text(
            f"👑 Referral program is now <b>{state}</b>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_settings")]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_referral_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    with get_db_session() as session:
        s = session.query(Settings).first()
        current = s.referral_reward_amount if s else 0.10
    try:
        await q.edit_message_text(
            f"👑 Current referral reward: <b>${current:.2f}</b>\n\n"
            f"Send the new reward amount in USDT (e.g. <code>0.10</code>):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_settings")]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return REFERRAL_AMOUNT_INPUT


async def admin_referral_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        amount = float((update.message.text or "").strip())
        if amount < 0 or amount > 1000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a number like 0.10")
        return REFERRAL_AMOUNT_INPUT
    with get_db_session() as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings()
            session.add(s)
        s.referral_reward_amount = amount
        session.commit()
    await update.message.reply_text(
        f"✅ Referral reward set to <b>${amount:.2f}</b>.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_settings")]]),
        parse_mode="HTML",
    )
    return ConversationHandler.END
