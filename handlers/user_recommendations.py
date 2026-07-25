"""V45 — User-facing Recommendation Handlers.

Callback namespace: urec:*

Shows product recommendations to users in the bot:
  • Trending Now
  • Best Sellers
  • Recommended For You
  • Recently Viewed
  • Related Products (when viewing a product)
  • Frequently Bought Together
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes

from services.recommendation_service import (
    get_trending, get_best_sellers, get_for_you,
    get_recently_viewed, get_related, get_fbt,
    get_also_bought, get_pinned,
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


def _fmt_product(p: dict, idx: int) -> str:
    name = p["name"][:35]
    price = p.get("sale_price") or p.get("price", 0)
    stock = "✅" if p.get("stock_count", 0) > 0 else "❌ OOS"
    return f"{idx}. <b>{name}</b> — ${price:.2f} {stock}"


def _product_btn(p: dict) -> InlineKeyboardButton:
    return InlineKeyboardButton(p["name"][:30], callback_data=f"product:{p['id']}")


# ─── Trending Now ─────────────────────────────────────────────────────────────

async def urec_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    items = await asyncio.to_thread(get_trending, 10)
    if not items:
        text = "🔥 <b>Trending Now</b>\n\nNo trending products at the moment."
    else:
        lines = ["🔥 <b>Trending Now</b>\n"]
        for i, p in enumerate(items, 1):
            lines.append(_fmt_product(p, i))
        text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Best Sellers ─────────────────────────────────────────────────────────────

async def urec_bestsellers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    items = await asyncio.to_thread(get_best_sellers, 10)
    if not items:
        text = "🏆 <b>Best Sellers</b>\n\nNo data yet."
    else:
        lines = ["🏆 <b>Best Sellers</b>\n"]
        for i, p in enumerate(items, 1):
            lines.append(_fmt_product(p, i))
        text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── For You ─────────────────────────────────────────────────────────────────

async def urec_for_you(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    telegram_id = update.effective_user.id
    user_id = await asyncio.to_thread(_get_user_db_id, telegram_id)
    if user_id:
        items = await asyncio.to_thread(get_for_you, user_id, 10)
    else:
        items = await asyncio.to_thread(get_trending, 10)
    if not items:
        text = "🎯 <b>Recommended For You</b>\n\nNo recommendations yet. Browse some products first!"
    else:
        lines = ["🎯 <b>Recommended For You</b>\n"]
        for i, p in enumerate(items, 1):
            lines.append(_fmt_product(p, i))
        text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Recently Viewed ─────────────────────────────────────────────────────────

async def urec_recently_viewed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    telegram_id = update.effective_user.id
    user_id = await asyncio.to_thread(_get_user_db_id, telegram_id)
    if not user_id:
        items = []
    else:
        items = await asyncio.to_thread(get_recently_viewed, user_id, 10)
    if not items:
        text = "👁 <b>Recently Viewed</b>\n\nYou haven't viewed any products yet."
    else:
        lines = ["👁 <b>Recently Viewed</b>\n"]
        for i, p in enumerate(items, 1):
            lines.append(_fmt_product(p, i))
        text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Related Products (product-level) ────────────────────────────────────────

async def urec_related(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """callback_data = urec:related:<product_id>"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[2])
    items = await asyncio.to_thread(get_related, product_id, 8)
    if not items:
        await q.answer("No related products found.", show_alert=True)
        return
    lines = ["📦 <b>Related Products</b>\n"]
    for i, p in enumerate(items, 1):
        lines.append(_fmt_product(p, i))
    text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"product:{product_id}")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Frequently Bought Together ──────────────────────────────────────────────

async def urec_fbt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """callback_data = urec:fbt:<product_id>"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[2])
    items = await asyncio.to_thread(get_fbt, product_id, 6)
    if not items:
        await q.answer("No data yet for this product.", show_alert=True)
        return
    lines = ["🛒 <b>Frequently Bought Together</b>\n"]
    for i, p in enumerate(items, 1):
        lines.append(_fmt_product(p, i))
    text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:4]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"product:{product_id}")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Also Bought ─────────────────────────────────────────────────────────────

async def urec_also_bought(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """callback_data = urec:also:<product_id>"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[2])
    items = await asyncio.to_thread(get_also_bought, product_id, 8)
    if not items:
        await q.answer("No data yet.", show_alert=True)
        return
    lines = ["🤝 <b>Customers Also Bought</b>\n"]
    for i, p in enumerate(items, 1):
        lines.append(_fmt_product(p, i))
    text = "\n".join(lines)
    kb_rows = [[_product_btn(p)] for p in items[:5]]
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"product:{product_id}")])
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass


# ─── Register ─────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    application.add_handler(CallbackQueryHandler(urec_trending,       pattern=r"^urec:trending$"))
    application.add_handler(CallbackQueryHandler(urec_bestsellers,    pattern=r"^urec:bestsellers$"))
    application.add_handler(CallbackQueryHandler(urec_for_you,        pattern=r"^urec:for_you$"))
    application.add_handler(CallbackQueryHandler(urec_recently_viewed,pattern=r"^urec:recently_viewed$"))
    application.add_handler(CallbackQueryHandler(urec_related,        pattern=r"^urec:related:"))
    application.add_handler(CallbackQueryHandler(urec_fbt,            pattern=r"^urec:fbt:"))
    application.add_handler(CallbackQueryHandler(urec_also_bought,    pattern=r"^urec:also:"))
    logger.info("V45: User Recommendation handlers registered.")
