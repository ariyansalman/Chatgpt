"""User-facing VIP Profile — V41.

Shows current tier, progress bar, points balance, history, and available rewards.
Callback: vip_profile
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from sqlalchemy import func as sqlfunc
from database import get_db_session, User, LoyaltyLedger
from database.models import (
    VipTier, UserVipTier, LoyaltyReward, LoyaltyRewardClaim,
)
from services import vip_service
from utils.bot_config import cfg
from i18n import get_user_language
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)


def _progress_bar(current: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "▓" * width
    pct = min(1.0, current / target)
    filled = int(pct * width)
    return "▓" * filled + "░" * (width - filled) + f" {pct*100:.0f}%"


async def vip_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)

    vip_status = cfg.get_str("vip_status", "enabled")
    if vip_status == "disabled":
        msg = "🏆 VIP Program is currently unavailable."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]])
        try:
            if q:
                await q.edit_message_text(msg, reply_markup=kb)
            else:
                await update.message.reply_text(msg, reply_markup=kb)
        except BadRequest:
            pass
        return
    if vip_status == "maintenance":
        msg = "🏆 VIP Program is under maintenance. Please check back soon."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]])
        try:
            if q:
                await q.edit_message_text(msg, reply_markup=kb)
            else:
                await update.message.reply_text(msg, reply_markup=kb)
        except BadRequest:
            pass
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            try:
                if q:
                    await q.edit_message_text("❌ Account not found.")
                else:
                    await update.message.reply_text("❌ Account not found.")
            except BadRequest:
                pass
            return

        current_tier = vip_service.get_user_tier(session, user.id)
        pts = user.loyalty_points or 0
        spent = float(user.total_spent or 0)

        # Find next tier (one level above current)
        current_level = current_tier.level if current_tier else -1
        next_tier = (
            session.query(VipTier)
            .filter(VipTier.level > current_level, VipTier.is_active == True)  # noqa: E712
            .order_by(VipTier.level)
            .first()
        )

        # Recent points history (last 5)
        recent_pts = (
            session.query(LoyaltyLedger)
            .filter_by(user_id=user.id)
            .order_by(LoyaltyLedger.created_at.desc())
            .limit(5)
            .all()
        )
        history_lines = []
        for entry in recent_pts:
            sign = "+" if entry.change >= 0 else ""
            when = entry.created_at.strftime("%m/%d") if entry.created_at else "?"
            reason = (entry.reason or "").replace("_", " ")[:20]
            history_lines.append(f"  {when} {sign}{entry.change} pts — {reason}")

        # Available rewards for this user
        available_rewards = (
            session.query(LoyaltyReward)
            .filter(
                LoyaltyReward.is_active == True,  # noqa: E712
                LoyaltyReward.min_tier_level <= current_level,
                LoyaltyReward.points_cost <= pts,
            )
            .order_by(LoyaltyReward.points_cost)
            .limit(5)
            .all()
        )
        reward_rows = [
            (r.id, r.name, r.reward_type, r.points_cost, r.value)
            for r in available_rewards
        ]

        tier_label = f"{current_tier.emoji} {current_tier.name}" if current_tier else "None"

        # Build progress text
        progress_text = ""
        if next_tier:
            progress_items = []
            if next_tier.min_orders > 0:
                from database.models import Order as _Order
                total_orders = session.query(sqlfunc.count()).select_from(
                    _Order
                ).filter_by(user_id=user.id).scalar() or 0
                bar = _progress_bar(total_orders, next_tier.min_orders)
                progress_items.append(f"  🛒 Orders: {bar} ({total_orders}/{next_tier.min_orders})")
            if next_tier.min_spending > 0:
                bar = _progress_bar(spent, next_tier.min_spending)
                progress_items.append(
                    f"  💳 Spending: {bar} (${spent:,.0f}/${next_tier.min_spending:,.0f})"
                )
            if progress_items:
                progress_text = (
                    f"\n\n📈 <b>Progress to {next_tier.emoji} {next_tier.name}</b>\n"
                    + "\n".join(progress_items)
                )

    _type_label = {"wallet": "💰 Wallet", "coupon": "🎟 Coupon",
                   "discount": "🏷 Discount", "product": "🎁 Product"}
    benefits = []
    if current_tier:
        if current_tier.discount_pct > 0:
            benefits.append(f"🏷 {current_tier.discount_pct:.1f}% product discount")
        if current_tier.cashback_pct > 0:
            benefits.append(f"💰 {current_tier.cashback_pct:.1f}% cashback")
        if current_tier.referral_bonus_pct > 0:
            benefits.append(f"👥 +{current_tier.referral_bonus_pct:.1f}% referral bonus")
        if current_tier.priority_support:
            benefits.append("🎫 Priority support")
        if current_tier.priority_delivery:
            benefits.append("🚀 Priority delivery")
        if current_tier.exclusive_flash_sales:
            benefits.append("⚡ Exclusive flash sales")

    text = (
        f"🏆 <b>My VIP Profile</b>\n\n"
        f"Tier: <b>{tier_label}</b>\n"
        f"Points: <b>{pts:,}</b>\n"
        f"Lifetime Spending: <b>${spent:,.2f}</b>"
        + progress_text
    )
    if benefits:
        text += "\n\n<b>Your Benefits</b>\n" + "\n".join(f"  {b}" for b in benefits)
    if history_lines:
        text += "\n\n<b>Recent Points Activity</b>\n" + "\n".join(history_lines)

    reward_buttons = []
    for rid, rname, rtype, rcost, rval in reward_rows:
        em = _type_label.get(rtype, "🎁")
        reward_buttons.append([InlineKeyboardButton(
            f"{em} {rname} — {rcost} pts",
            callback_data=f"vip_claim:{rid}",
        )])

    kb_rows = reward_buttons
    if not reward_rows and pts > 0:
        text += "\n\n<i>No rewards available for your points level yet.</i>"
    elif not reward_rows:
        text += "\n\n<i>Earn points by shopping to unlock rewards!</i>"
    else:
        text += "\n\n<b>Available Rewards</b> (tap to redeem):"

    kb_rows.append([
        InlineKeyboardButton("📋 Points History", callback_data="vip_pts_history"),
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
    ])
    kb = InlineKeyboardMarkup(kb_rows)
    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def vip_claim_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User claims a loyalty reward."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    try:
        reward_id = int(q.data.split(":")[-1])
    except Exception:
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            await q.answer("❌ Account not found.", show_alert=True)
            return
        reward = session.query(LoyaltyReward).filter_by(id=reward_id).first()
        if not reward:
            await q.answer("❌ Reward not found.", show_alert=True)
            return
        success, msg = vip_service.claim_reward(session, user, reward)
        if success:
            session.commit()
        await q.answer(msg, show_alert=True)

    # Refresh the profile view
    await vip_profile(with_data(update, "vip_profile"), context)


async def vip_pts_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show full points history for the user."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return
        entries = (
            session.query(LoyaltyLedger)
            .filter_by(user_id=user.id)
            .order_by(LoyaltyLedger.created_at.desc())
            .limit(20)
            .all()
        )
        lines = []
        for e in entries:
            sign = "+" if e.change >= 0 else ""
            when = e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "?"
            reason = (e.reason or "").replace("_", " ")[:30]
            lines.append(f"{sign}{e.change:+d} pts — {reason} ({when})")
        pts = user.loyalty_points or 0

    text = (
        f"📋 <b>Points History</b>\n"
        f"Balance: <b>{pts:,} pts</b>\n\n"
        + ("\n".join(lines) if lines else "No history yet.")
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="vip_profile")]])
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
