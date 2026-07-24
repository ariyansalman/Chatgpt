"""Admin manual redelivery flow.

Callback data:
  admin_redeliver_<order_id>              -> list items with resend buttons
  admin_redeliver_do_<order_id>_<item_id> -> confirm resend that item
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import Order, OrderItem, Product
from services.redelivery import prepare_redelivery, mark_redelivery
from utils.permissions import has_permission
from config.settings import settings as app_settings
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    admin_id = getattr(app_settings, "ADMIN_TELEGRAM_ID", None)
    return admin_id is not None and int(user_id) == int(admin_id)


async def admin_redelivery_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await q.answer("⛔ Access denied", show_alert=True); return
    try:
        order_id = int(q.data.split("_")[2])
    except (ValueError, IndexError):
        await q.answer("Bad callback", show_alert=True); return

    lines = [f"📦 <b>Redelivery — Order #{order_id}</b>", "",
             "Pick a line to resend. Redelivery <b>never</b> consumes new "
             "inventory — the previously delivered asset is resent."]
    kb: list[list[InlineKeyboardButton]] = []
    with get_db_session() as s:
        order = s.query(Order).filter_by(id=order_id).first()
        if not order:
            try:
                await q.edit_message_text("❌ Order not found."); return
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        items = s.query(OrderItem).filter_by(order_id=order_id).all()
        for it in items:
            p = s.query(Product).filter_by(id=it.product_id).first()
            label = f"{p.name if p else 'Item'} × {it.quantity}"
            kb.append([InlineKeyboardButton(
                f"🔁 Resend: {label[:40]}",
                callback_data=f"admin_redeliver_do_{order_id}_{it.id}"
            )])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"view_order_{order_id}")])
    try:
        await q.edit_message_text("\n".join(lines),
                                  reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# Simple in-memory lock to prevent double-tap creating parallel resends per
# order-item. Best-effort — process-local; PTB is single-process.
_redelivery_locks: set[tuple[int, int]] = set()


async def admin_redelivery_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await q.answer("⛔ Access denied", show_alert=True); return
    try:
        parts = q.data.split("_")
        order_id = int(parts[3]); item_id = int(parts[4])
    except (ValueError, IndexError):
        await q.answer("Bad callback", show_alert=True); return

    key = (order_id, item_id)
    if key in _redelivery_locks:
        await q.answer("⏳ Already processing…", show_alert=True); return
    _redelivery_locks.add(key)
    try:
        payload = prepare_redelivery(order_id, item_id)
        if payload.error:
            try:
                await q.edit_message_text(
                    f"❌ Cannot resend: {payload.error}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back",
                            callback_data=f"admin_redeliver_{order_id}")
                    ]]))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Send to user. Find the user's telegram chat id.
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            user_tg_id = order.user.telegram_id if order and order.user else None

        if not user_tg_id:
            try:
                await q.edit_message_text("❌ Order has no linked user.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Header — guard against oversized text_summary (e.g. a V11-dispatcher
        # delivery for a large multi-quantity order) the same way the main
        # purchase flow does, instead of risking a Message_too_long failure.
        from services.purchase_success import is_delivery_oversized, send_delivery_as_file
        _header_summary = payload.text_summary
        _oversized_summary = is_delivery_oversized(payload.text_summary) and not payload.keys
        if _oversized_summary:
            _header_summary = "📎 Delivered as attached .txt file below."
        try:
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=(f"🔁 <b>Delivery resent</b> — Order #{order_id}\n"
                      f"📦 {payload.product_name}\n\n{_header_summary}"),
                parse_mode="HTML",
            )
            if _oversized_summary:
                await send_delivery_as_file(
                    context.bot, user_tg_id, order_id, payload.product_name,
                    payload.text_summary,
                    caption=f"📎 Resent delivery for order #{order_id}",
                )
            # Asset-specific follow-up
            if payload.keys:
                joined = "\n".join(payload.keys)
                if is_delivery_oversized(joined):
                    # Previously this silently truncated to 3800 chars,
                    # dropping keys past the cutoff on redelivery. Now sends
                    # the full set as a .txt file, same as bulk purchase.
                    await send_delivery_as_file(
                        context.bot, user_tg_id, order_id, payload.product_name,
                        joined,
                        caption=f"🔐 {len(payload.keys)} key(s) for order #{order_id}",
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_tg_id,
                        text=f"🔐 <code>{joined}</code>",
                        parse_mode="HTML",
                    )
            elif payload.download_link:
                await context.bot.send_message(
                    chat_id=user_tg_id,
                    text=f"🔗 {payload.download_link}",
                )
            elif payload.telegram_file_id:
                sender = {
                    "document": context.bot.send_document,
                    "photo": context.bot.send_photo,
                    "video": context.bot.send_video,
                    "audio": context.bot.send_audio,
                }.get(payload.telegram_file_type or "document",
                      context.bot.send_document)
                await sender(chat_id=user_tg_id, **{
                    payload.telegram_file_type or "document":
                        payload.telegram_file_id
                })
        except Exception as e:
            logger.exception("Redelivery send failed for order %s", order_id)
            try:
                await q.edit_message_text(
                    f"⚠️ Redelivery send failed: {e}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back",
                            callback_data=f"admin_redeliver_{order_id}")
                    ]]))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        mark_redelivery(order_id, item_id, update.effective_user.id)

        try:
            await q.edit_message_text(
                f"✅ Delivery resent for order #{order_id} (item #{item_id}).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Order",
                        callback_data=f"view_order_{order_id}")
                ]]))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    finally:
        _redelivery_locks.discard(key)


def register(application):
    application.add_handler(CallbackQueryHandler(
        admin_redelivery_menu, pattern=r"^admin_redeliver_\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_redelivery_do, pattern=r"^admin_redeliver_do_\d+_\d+$"))
