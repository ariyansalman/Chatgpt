"""V45 — User-facing Restock Notification Handlers.

Callback namespace: urns:*

Allows users to subscribe/unsubscribe from restock alerts on OOS products.
Integrates with the product detail flow — a "🔔 Notify Me" button appears
when a product has stock_count == 0.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes

from services.restock_service import (
    subscribe, unsubscribe, is_subscribed, get_user_subscriptions,
)
from database import get_db_session
from database.models import User

logger = logging.getLogger(__name__)


def _get_user_db_id(telegram_id: int) -> int | None:
    try:
        with get_db_session() as s:
            u = s.query(User).filter_by(telegram_id=telegram_id).first()
            return u.id if u else None
    except Exception:
        return None


async def urns_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe to restock notification: callback_data = urns:sub:<product_id>"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[2])
    telegram_id = update.effective_user.id
    user_id = await asyncio.to_thread(_get_user_db_id, telegram_id)
    if not user_id:
        await q.answer("❌ User not found.", show_alert=True)
        return
    already = await asyncio.to_thread(is_subscribed, user_id, product_id)
    if already:
        await q.answer("ℹ️ You're already subscribed to restock alerts for this product.",
                       show_alert=True)
        return
    success = await asyncio.to_thread(subscribe, user_id, product_id)
    if success:
        await q.answer("🔔 You'll be notified when this product is back in stock!",
                       show_alert=True)
        # Refresh button to show "unsubscribe" option
        try:
            kb = q.message.reply_markup
            new_rows = []
            for row in (kb.inline_keyboard if kb else []):
                new_row = []
                for btn in row:
                    if btn.callback_data == f"urns:sub:{product_id}":
                        new_row.append(InlineKeyboardButton(
                            "🔕 Cancel Alert", callback_data=f"urns:unsub:{product_id}"))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await q.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
        except BadRequest:
            pass
    else:
        await q.answer("❌ Could not subscribe. Please try again.", show_alert=True)


async def urns_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe: callback_data = urns:unsub:<product_id>"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[2])
    telegram_id = update.effective_user.id
    user_id = await asyncio.to_thread(_get_user_db_id, telegram_id)
    if not user_id:
        return
    success = await asyncio.to_thread(unsubscribe, user_id, product_id)
    if success:
        await q.answer("🔕 Unsubscribed from restock alerts.", show_alert=True)
        try:
            kb = q.message.reply_markup
            new_rows = []
            for row in (kb.inline_keyboard if kb else []):
                new_row = []
                for btn in row:
                    if btn.callback_data == f"urns:unsub:{product_id}":
                        new_row.append(InlineKeyboardButton(
                            "🔔 Notify Me When Available",
                            callback_data=f"urns:sub:{product_id}"))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await q.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
        except BadRequest:
            pass
    else:
        await q.answer("ℹ️ Subscription not found.", show_alert=True)


async def urns_my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's active restock subscriptions: callback_data = urns:my_alerts"""
    q = update.callback_query
    await q.answer()
    telegram_id = update.effective_user.id
    user_id = await asyncio.to_thread(_get_user_db_id, telegram_id)
    if not user_id:
        return

    subs = await asyncio.to_thread(get_user_subscriptions, user_id)
    if not subs:
        text = "🔔 <b>My Restock Alerts</b>\n\nYou have no active restock notifications."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="acc:account")]])
    else:
        lines = ["🔔 <b>My Restock Alerts</b>\n"]
        kb_rows = []
        for s in subs[:10]:
            status = "✅ Notified" if s["notified"] else "⏳ Waiting"
            stock_note = f" (stock: {s['stock_count']})" if s["stock_count"] > 0 else " (OOS)"
            lines.append(f"📦 <b>{s['product_name'][:35]}</b>{stock_note} — {status}")
            kb_rows.append([InlineKeyboardButton(
                f"🔕 Remove: {s['product_name'][:20]}",
                callback_data=f"urns:unsub:{s['product_id']}")])
        text = "\n".join(lines)
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:account")])
        kb = InlineKeyboardMarkup(kb_rows)

    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


def build_notify_button(product_id: int, user_db_id: int | None) -> InlineKeyboardButton | None:
    """Return a 'Notify Me' or 'Cancel Alert' button for an OOS product detail view.

    Pass user_db_id=None to always show 'Notify Me'.
    Returns None if user_db_id is available and is_subscribed check fails silently.
    """
    if user_db_id is None:
        return InlineKeyboardButton("🔔 Notify Me When Available",
                                    callback_data=f"urns:sub:{product_id}")
    try:
        subscribed = is_subscribed(user_db_id, product_id)
        if subscribed:
            return InlineKeyboardButton("🔕 Cancel Restock Alert",
                                        callback_data=f"urns:unsub:{product_id}")
        return InlineKeyboardButton("🔔 Notify Me When Available",
                                    callback_data=f"urns:sub:{product_id}")
    except Exception:
        return InlineKeyboardButton("🔔 Notify Me When Available",
                                    callback_data=f"urns:sub:{product_id}")


def register_handlers(application) -> None:
    application.add_handler(CallbackQueryHandler(urns_subscribe,   pattern=r"^urns:sub:\d+$"))
    application.add_handler(CallbackQueryHandler(urns_unsubscribe, pattern=r"^urns:unsub:\d+$"))
    application.add_handler(CallbackQueryHandler(urns_my_alerts,   pattern=r"^urns:my_alerts$"))
    logger.info("V45: User Restock Notification handlers registered.")
