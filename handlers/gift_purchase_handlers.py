"""Gift Purchase — allow users to buy products for another Telegram user.

Callback namespace: gp:*

Conversation states:
    GF_RECIPIENT  (5300) — waiting for recipient Telegram ID or @username
    GF_MESSAGE    (5301) — waiting for optional gift message

Flow:
    1. User taps "🎁 Gift" on product detail → gp:start:<product_id>
    2. Bot asks for recipient (Telegram ID or @username)
    3. Bot asks for optional message (or /skip)
    4. Confirmation shown — user taps "✅ Proceed to Checkout"
    5. Normal purchase flow begins; gift metadata stored in user_data
    6. After order completion, background job sends recipient notification
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, User, Product, Order, OrderStatus
from database.models import GiftPurchase, GiftPurchaseStatus
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

GF_RECIPIENT = 5300
GF_MESSAGE   = 5301


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _feature_enabled() -> bool:
    return cfg.get_bool("feature_gift_purchase_enabled", True)


def _safe_kb(buttons):
    return InlineKeyboardMarkup(buttons)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def gift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: gp:start:<product_id>"""
    query = update.callback_query
    await query.answer()

    if not _feature_enabled():
        await query.answer("🎁 Gift Purchase is currently disabled.", show_alert=True)
        return ConversationHandler.END

    try:
        product_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    with get_db_session() as s:
        product = s.query(Product).filter_by(id=product_id, is_active=True).first()
        if not product:
            await query.answer("❌ Product not found.", show_alert=True)
            return ConversationHandler.END
        pname = product.name
        pprice = product.price

    context.user_data["gift_product_id"]   = product_id
    context.user_data["gift_product_name"] = pname
    context.user_data["gift_product_price"] = pprice

    allow_anon = cfg.get_bool("feature_gift_allow_anonymous", True)
    anon_note  = "\n_You may send as Anonymous if you prefer._" if allow_anon else ""

    text = (
        f"🎁 <b>Gift Purchase</b>\n\n"
        f"You are gifting: <b>{pname}</b>\n\n"
        f"Please enter the recipient's <b>Telegram ID</b> (a number) or "
        f"<b>@username</b>:{anon_note}\n\n"
        f"Send /cancel to abort."
    )
    kb = _safe_kb([[InlineKeyboardButton("🚫 Cancel", callback_data="gp:cancel")]])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GF_RECIPIENT


async def gift_recipient_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State GF_RECIPIENT — user sends Telegram ID or @username."""
    text = (update.message.text or "").strip()

    if text.startswith("/cancel"):
        return await gift_cancel(update, context)

    recipient_tg_id  = None
    recipient_handle = None

    if text.lstrip("-").isdigit():
        recipient_tg_id = int(text)
    elif text.startswith("@"):
        recipient_handle = text.lstrip("@")
    else:
        # try plain username without @
        if text.replace("_", "").replace(".", "").isalpha():
            recipient_handle = text
        else:
            await update.message.reply_text(
                "❌ Invalid input. Please enter a numeric Telegram ID or @username.\n"
                "Send /cancel to abort."
            )
            return GF_RECIPIENT

    # Resolve handle to internal user if possible
    with get_db_session() as s:
        if recipient_tg_id:
            user = s.query(User).filter_by(telegram_id=recipient_tg_id).first()
            if not user:
                await update.message.reply_text(
                    f"⚠️ User {recipient_tg_id} has not started this bot yet.\n"
                    "They will receive the gift notification once they start the bot, "
                    "or as a direct message attempt.\n\n"
                    "Continue? Send /cancel to abort."
                )
        elif recipient_handle:
            user = s.query(User).filter(
                User.username.ilike(recipient_handle)
            ).first()
            if user:
                recipient_tg_id = user.telegram_id
            else:
                await update.message.reply_text(
                    f"⚠️ @{recipient_handle} hasn't started this bot yet.\n"
                    "They will receive a gift notification when they do.\n\n"
                    "Continue? Send /cancel to abort."
                )

    context.user_data["gift_recipient_tg_id"]  = recipient_tg_id
    context.user_data["gift_recipient_handle"] = recipient_handle

    kb = _safe_kb([
        [InlineKeyboardButton("⏭ Skip message", callback_data="gp:skip_msg")],
        [InlineKeyboardButton("🚫 Cancel",       callback_data="gp:cancel")],
    ])
    await update.message.reply_text(
        "✅ Recipient noted.\n\n"
        "Now enter a <b>gift message</b> (optional) — the recipient will see this, "
        "or tap <b>Skip message</b> to send without a message.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return GF_MESSAGE


async def gift_skip_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User skips the gift message."""
    query = update.callback_query
    await query.answer()
    context.user_data["gift_message"] = None
    return await _show_gift_confirmation(update, context)


async def gift_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State GF_MESSAGE — user sends the gift message text."""
    text = (update.message.text or "").strip()
    if text.startswith("/cancel"):
        return await gift_cancel(update, context)

    context.user_data["gift_message"] = text[:300]
    return await _show_gift_confirmation(update, context)


async def _show_gift_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show gift confirmation screen."""
    product_name  = context.user_data.get("gift_product_name", "?")
    product_price = context.user_data.get("gift_product_price", 0.0)
    product_id    = context.user_data.get("gift_product_id")
    tg_id         = context.user_data.get("gift_recipient_tg_id")
    handle        = context.user_data.get("gift_recipient_handle")
    message       = context.user_data.get("gift_message")

    allow_anon = cfg.get_bool("feature_gift_allow_anonymous", True)
    recipient_str = (
        f"@{handle}" if handle else
        f"Telegram ID: {tg_id}" if tg_id else "Unknown"
    )

    lines = [
        "🎁 <b>Gift Purchase Confirmation</b>\n",
        f"🛍 Product: <b>{product_name}</b>",
        f"💰 Price: <b>${product_price:.2f}</b>",
        f"👤 To: {recipient_str}",
    ]
    if allow_anon:
        anon = context.user_data.get("gift_anonymous", False)
        lines.append(f"🎭 Anonymous: {'Yes' if anon else 'No'}")
    if message:
        lines.append(f"💌 Message: <i>{message[:200]}</i>")

    lines.append("\n✅ Proceed to checkout to complete the gift purchase.")

    kb_rows = []
    if allow_anon:
        anon = context.user_data.get("gift_anonymous", False)
        lbl = "🎭 Send Anonymously: ON" if anon else "🎭 Send Anonymously: OFF"
        kb_rows.append([InlineKeyboardButton(lbl, callback_data="gp:toggle_anon")])

    kb_rows.append([
        InlineKeyboardButton("🛒 Proceed to Checkout", callback_data=f"product_{product_id}")
    ])
    kb_rows.append([InlineKeyboardButton("🚫 Cancel", callback_data="gp:cancel")])

    text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(text, reply_markup=_safe_kb(kb_rows), parse_mode="HTML")
    else:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=_safe_kb(kb_rows), parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return ConversationHandler.END


async def gift_toggle_anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle anonymous gift flag."""
    query = update.callback_query
    await query.answer()
    context.user_data["gift_anonymous"] = not context.user_data.get("gift_anonymous", False)
    return await _show_gift_confirmation(update, context)


async def gift_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the gift flow."""
    for k in ("gift_product_id", "gift_product_name", "gift_product_price",
              "gift_recipient_tg_id", "gift_recipient_handle",
              "gift_message", "gift_anonymous"):
        context.user_data.pop(k, None)

    msg = "🎁 Gift purchase cancelled."
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(msg)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    elif update.message:
        await update.message.reply_text(msg)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Record creation (called after purchase confirm)
# ─────────────────────────────────────────────────────────────────────────────

def create_gift_record_if_any(context: ContextTypes.DEFAULT_TYPE, order_id: int,
                               sender_user_id: int) -> int | None:
    """Create a GiftPurchase row if the current checkout is a gift.

    Returns the new GiftPurchase.id, or None if this isn't a gift purchase.
    Called by payment_handlers after order creation.
    """
    product_id   = context.user_data.get("gift_product_id")
    recipient_id = context.user_data.get("gift_recipient_tg_id")
    handle       = context.user_data.get("gift_recipient_handle")
    message      = context.user_data.get("gift_message")
    anonymous    = context.user_data.get("gift_anonymous", False)

    if not product_id or (not recipient_id and not handle):
        return None

    try:
        with get_db_session() as s:
            gp = GiftPurchase(
                order_id=order_id,
                sender_user_id=sender_user_id,
                recipient_telegram_id=recipient_id,
                recipient_username=handle,
                product_id=product_id,
                gift_message=message,
                is_anonymous=anonymous,
                status=GiftPurchaseStatus.PENDING.value,
                created_at=datetime.utcnow(),
            )
            s.add(gp)
            s.commit()
            gid = gp.id

        # Clear gift keys from user_data
        for k in ("gift_product_id", "gift_product_name", "gift_product_price",
                  "gift_recipient_tg_id", "gift_recipient_handle",
                  "gift_message", "gift_anonymous"):
            context.user_data.pop(k, None)

        logger.info("GiftPurchase #%d created for order #%d", gid, order_id)
        return gid
    except Exception:
        logger.exception("Failed to create GiftPurchase record")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Background job — notify recipients of completed gift orders
# ─────────────────────────────────────────────────────────────────────────────

async def process_completed_gifts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: send notifications for completed, unnotified gift orders.

    Runs every 60 seconds via bot.py job_queue. Finds GiftPurchase records
    where the underlying order is COMPLETED and status is still PENDING,
    sends the recipient a message, then marks the gift as NOTIFIED.
    """
    if not _feature_enabled():
        return

    try:
        pending_gifts = []
        with get_db_session() as s:
            rows = (
                s.query(GiftPurchase)
                .join(Order, GiftPurchase.order_id == Order.id)
                .filter(
                    GiftPurchase.status == GiftPurchaseStatus.PENDING.value,
                    Order.status == OrderStatus.COMPLETED,
                )
                .limit(20)
                .all()
            )
            for gp in rows:
                order  = s.query(Order).filter_by(id=gp.order_id).first()
                sender = s.query(User).filter_by(id=gp.sender_user_id).first()
                product = s.query(Product).filter_by(id=gp.product_id).first()

                # Collect delivery content from order items
                from database.models import OrderItem
                items = s.query(OrderItem).filter_by(order_id=gp.order_id).all()
                delivery_lines = []
                for item in items:
                    if item.delivered_asset:
                        delivery_lines.append(item.delivered_asset[:800])

                pending_gifts.append({
                    "id":                gp.id,
                    "recipient_tg_id":   gp.recipient_telegram_id,
                    "recipient_handle":  gp.recipient_username,
                    "is_anonymous":      gp.is_anonymous,
                    "gift_message":      gp.gift_message,
                    "sender_name":       (
                        sender.username or sender.first_name if sender else "Someone"
                    ),
                    "product_name":      product.name if product else "a product",
                    "delivery_lines":    delivery_lines,
                    "order_id":          gp.order_id,
                })

        for gift in pending_gifts:
            recipient_tg_id = gift["recipient_tg_id"]
            if not recipient_tg_id:
                # Try to resolve @username
                if gift["recipient_handle"]:
                    with get_db_session() as s:
                        u = s.query(User).filter(
                            User.username.ilike(gift["recipient_handle"])
                        ).first()
                        if u:
                            recipient_tg_id = u.telegram_id

            if not recipient_tg_id:
                # Cannot notify — mark as undeliverable
                with get_db_session() as s:
                    gp = s.query(GiftPurchase).filter_by(id=gift["id"]).first()
                    if gp:
                        gp.status = GiftPurchaseStatus.UNDELIVERABLE.value
                        gp.notified_at = datetime.utcnow()
                    s.commit()
                continue

            sender_display = (
                "Someone special 🎭" if gift["is_anonymous"]
                else f"@{gift['sender_name']}" if gift["sender_name"] else "Someone"
            )

            lines = [
                "🎁 <b>You received a gift!</b>\n",
                f"From: {sender_display}",
                f"Product: <b>{gift['product_name']}</b>",
            ]
            if gift["gift_message"]:
                lines.append(f'\n💌 <i>"{gift["gift_message"]}"</i>')
            if gift["delivery_lines"]:
                lines.append("\n📦 <b>Your delivery:</b>")
                for dl in gift["delivery_lines"][:3]:
                    lines.append(f"<code>{dl[:400]}</code>")
            lines.append(f"\n📋 Order reference: #{gift['order_id']}")

            try:
                await context.bot.send_message(
                    chat_id=recipient_tg_id,
                    text="\n".join(lines),
                    parse_mode="HTML",
                )
                new_status = GiftPurchaseStatus.NOTIFIED
            except Exception as notify_err:
                logger.warning("Gift notify failed for gift #%d: %s", gift["id"], notify_err)
                new_status = GiftPurchaseStatus.UNDELIVERABLE

            with get_db_session() as s:
                gp = s.query(GiftPurchase).filter_by(id=gift["id"]).first()
                if gp:
                    gp.status = new_status
                    gp.notified_at = datetime.utcnow()
                s.commit()

    except Exception:
        logger.exception("process_completed_gifts job failed")
