"""V25 — User-facing Order Timeline callback handler.

Callback pattern: ``user_timeline_<order_id>``

Renders the full Order Timeline to the user when they tap
"📋 View Timeline" in the order detail view.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session
from database.models import User, Order, DisputeStatus
from services import order_timeline as tl

logger = logging.getLogger(__name__)


async def user_timeline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the order timeline to the user."""
    query = update.callback_query
    await query.answer()

    # Parse order_id from "user_timeline_<order_id>"
    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.message.reply_text("❌ Invalid order.")
        return

    if not tl.show_to_users():
        try:
            await query.edit_message_text(
                "⏳ Order timeline is currently unavailable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Order",
                                         callback_data=f"user_order_detail_{order_id}")
                ]]),
            )
        except BadRequest:
            pass
        return

    user_tg_id = update.effective_user.id
    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_tg_id).first()
        if not user:
            await query.message.reply_text("❌ User not found.")
            return
        order = s.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            try:
                await query.edit_message_text("❌ Order not found.")
            except BadRequest:
                pass
            return

    # Generate the timeline text
    try:
        timeline_text = tl.render_user_timeline(order_id)
    except Exception:
        logger.exception("render_user_timeline failed for order %s", order_id)
        timeline_text = "⚠️ Could not load timeline."

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Order",
                             callback_data=f"user_order_detail_{order_id}"),
        InlineKeyboardButton("🔙 My Orders", callback_data="order_history"),
    ]])

    try:
        await query.edit_message_text(
            timeline_text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
