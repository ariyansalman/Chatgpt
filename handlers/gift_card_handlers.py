"""Gift Card — user-facing redemption flow.

Callback namespace: gc:*

Conversation states:
    GC_CODE (5310) — waiting for user to enter gift card code

Flow:
    1. User taps "🎟 Redeem Gift Card" in wallet or main menu → gc:redeem
    2. Bot asks for the gift card code
    3. System validates: exists, active, not expired, uses not exceeded,
       user hasn't redeemed it already
    4a. FIXED / CUSTOM: credits wallet balance
    4b. PERCENT: creates a one-time auto-coupon and shows the code to user
    5. Success / error notification
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, User
from database.models import GiftCard, GiftCardRedemption, GiftCardType, Coupon, DiscountType
from services import wallet as _wallet_svc
from utils.bot_config import cfg
from utils.helpers import sanitize_message

logger = logging.getLogger(__name__)

GC_CODE = 5310


def _feature_enabled() -> bool:
    return cfg.get_bool("feature_gift_cards_enabled", True)


# ─────────────────────────────────────────────────────────────────────────────
# Entry — show redemption prompt
# ─────────────────────────────────────────────────────────────────────────────

async def redeem_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: gc:redeem — show gift card redemption prompt."""
    query = update.callback_query
    await query.answer()

    if not _feature_enabled():
        await query.answer("🎟 Gift Cards are currently disabled.", show_alert=True)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Cancel", callback_data="gc:cancel")],
    ])
    try:
        await query.edit_message_text(
            "🎟 <b>Redeem Gift Card</b>\n\n"
            "Enter your gift card code below:\n\n"
            "<i>The code is case-insensitive.</i>",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GC_CODE


async def redeem_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State GC_CODE — user enters the gift card code."""
    code = (update.message.text or "").strip().upper()

    if not code:
        await update.message.reply_text(
            "❌ Please enter a valid code.\nSend /cancel to abort."
        )
        return GC_CODE

    tg_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            await update.message.reply_text("❌ User not found. Please /start first.")
            return ConversationHandler.END

        card = s.query(GiftCard).filter_by(code=code, is_active=True).first()

        if not card:
            await update.message.reply_text(
                "❌ Invalid or expired gift card code.\n"
                "Please check the code and try again."
            )
            return GC_CODE

        now = datetime.utcnow()

        # Expiry check
        if card.expires_at and card.expires_at < now:
            await update.message.reply_text(
                "❌ This gift card has expired."
            )
            return GC_CODE

        # Max-uses check
        if card.max_uses > 0 and card.used_count >= card.max_uses:
            await update.message.reply_text(
                "❌ This gift card has reached its maximum number of uses."
            )
            return GC_CODE

        # Duplicate redemption check
        already = s.query(GiftCardRedemption).filter_by(
            card_id=card.id, user_id=user.id
        ).first()
        if already:
            await update.message.reply_text(
                "❌ You have already redeemed this gift card."
            )
            return GC_CODE

        # All checks passed — redeem
        card_type  = card.card_type
        card_value = card.value
        card_label = card.label or "Gift Card"
        card_id    = card.id

        # Record the redemption
        s.add(GiftCardRedemption(
            card_id=card_id,
            user_id=user.id,
            redeemed_at=now,
        ))
        card.used_count = (card.used_count or 0) + 1

        # If single-use and now fully used, deactivate
        if card.is_single_use:
            card.is_active = False

        user_id_db = user.id
        s.commit()

    # Apply the benefit
    if card_type in (GiftCardType.FIXED, GiftCardType.CUSTOM):
        # Credit wallet
        _wallet_svc.credit(user_id_db, card_value, reason=f"Gift Card: {card_label} ({code})")
        msg = (
            f"🎟 <b>Gift Card Redeemed!</b>\n\n"
            f"Code: <code>{code}</code>\n"
            f"Card: {card_label}\n"
            f"💰 <b>${card_value:.2f}</b> has been credited to your wallet!\n\n"
            f"Your new balance is now updated."
        )
    elif card_type == GiftCardType.PERCENT:
        # Create a one-time auto-coupon
        coupon_code = _generate_coupon_code(prefix="GC")
        with get_db_session() as s:
            existing = s.query(Coupon).filter_by(code=coupon_code).first()
            if not existing:
                coupon = Coupon(
                    code=coupon_code,
                    discount_type=DiscountType.PERCENT,
                    discount_value=card_value,
                    max_uses=1,
                    per_user_limit=1,
                    is_active=True,
                    created_at=datetime.utcnow(),
                )
                s.add(coupon)
                s.commit()
        msg = (
            f"🎟 <b>Gift Card Redeemed!</b>\n\n"
            f"Code: <code>{code}</code>\n"
            f"Card: {card_label}\n"
            f"🏷 <b>{card_value:.0f}% discount</b> coupon has been generated:\n\n"
            f"Coupon Code: <code>{coupon_code}</code>\n\n"
            f"Apply this coupon at checkout to get your discount!"
        )
    else:
        msg = "✅ Gift card redeemed successfully!"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
    ])
    await update.message.reply_text(sanitize_message(msg), reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def redeem_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel gift card redemption."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("🎟 Gift card redemption cancelled.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    elif update.message:
        await update.message.reply_text("🎟 Gift card redemption cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────

async def redeem_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's gift card redemption history: gc:history"""
    query = update.callback_query
    await query.answer()

    if not _feature_enabled():
        await query.answer("🎟 Gift Cards are currently disabled.", show_alert=True)
        return

    tg_id = update.effective_user.id
    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return
        redemptions = (
            s.query(GiftCardRedemption)
            .filter_by(user_id=user.id)
            .order_by(GiftCardRedemption.redeemed_at.desc())
            .limit(20)
            .all()
        )
        rows = []
        for r in redemptions:
            card = s.query(GiftCard).filter_by(id=r.card_id).first()
            rows.append({
                "code":  card.code if card else "?",
                "label": card.label if card else "?",
                "type":  card.card_type.value if card else "?",
                "value": card.value if card else 0,
                "when":  r.redeemed_at,
            })

    if not rows:
        text = "🎟 <b>Redemption History</b>\n\nYou haven't redeemed any gift cards yet."
    else:
        lines = ["🎟 <b>Redemption History</b>\n"]
        for r in rows:
            when = r["when"].strftime("%Y-%m-%d") if r["when"] else "?"
            if r["type"] == GiftCardType.PERCENT.value:
                val_str = f"{r['value']:.0f}% off"
            else:
                val_str = f"${r['value']:.2f}"
            lines.append(f"• <code>{r['code']}</code> — {r['label']} ({val_str}) on {when}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="wallet")]])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_coupon_code(prefix: str = "GC", length: int = 8) -> str:
    """Generate a random alphanumeric coupon code."""
    alphabet = string.ascii_uppercase + string.digits
    rand_part = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix}-{rand_part}"
