"""V25 — User-facing Product FAQ callback handler.

Callback patterns
-----------------
pfaq:view:<product_id>              Show all FAQs for a product
pfaq:search:<product_id>            Start search conversation (via ConversationHandler)
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

from database import get_db_session
from database.models import Product
from services import product_faq as svc

logger = logging.getLogger(__name__)

_USER_SEARCH_QUERY = 9550


async def pfaq_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show FAQs for a product to the user."""
    query = update.callback_query
    await query.answer()

    try:
        product_id = int(query.data.split(":")[2])
    except (ValueError, IndexError):
        await query.message.reply_text("❌ Invalid product.")
        return

    status = svc.cfg.get_str("pfaq_status", "enabled")
    if status == "disabled":
        try:
            await query.edit_message_text(
                "❓ FAQ is not available.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back",
                                         callback_data=f"prod_{product_id}")
                ]]),
            )
        except BadRequest:
            pass
        return

    if status == "maintenance":
        try:
            await query.edit_message_text(
                "⚠️ Product FAQ is currently under maintenance. Please check back soon.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back",
                                         callback_data=f"prod_{product_id}")
                ]]),
            )
        except BadRequest:
            pass
        return

    faq_text = svc.render_user_faqs(product_id)
    if not faq_text:
        faq_text = "❓ <b>FAQ</b>\n\nNo FAQ available for this product."

    kb: list = []
    if svc.allow_search():
        kb.append([InlineKeyboardButton(
            "🔍 Search FAQs",
            callback_data=f"pfaq:search:{product_id}",
        )])
    kb.append([InlineKeyboardButton("🔙 Back to Product",
                                     callback_data=f"prod_{product_id}")])

    try:
        await query.edit_message_text(
            faq_text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─── User search conversation ─────────────────────────────────────────────

async def user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        product_id = int(query.data.split(":")[2])
    except (ValueError, IndexError):
        return ConversationHandler.END
    context.user_data["pfaq_user_search_pid"] = product_id
    await query.message.reply_text(
        "🔍 Enter your search term to find FAQs:\n\n/cancel to go back",
    )
    return _USER_SEARCH_QUERY


async def user_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = update.message.text.strip()
    product_id = context.user_data.pop("pfaq_user_search_pid", None)
    if not product_id:
        await update.message.reply_text("Session expired. Please try again.")
        return ConversationHandler.END

    results = svc.search_faqs(product_id, term)
    if not results:
        await update.message.reply_text(
            f"🔍 No FAQs found for \u201c<i>{term[:80]}</i>\u201d.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to FAQ",
                                     callback_data=f"pfaq:view:{product_id}")
            ]]),
        )
        return ConversationHandler.END

    lines = [f"🔍 <b>{len(results)} result(s) for «{term[:60]}»</b>", ""]
    for r in results[:10]:
        cat_label = svc.CATEGORIES.get(r["category"], "")
        if cat_label:
            lines.append(f"<i>{cat_label}</i>")
        lines.append(f"<b>Q: {r['question']}</b>")
        lines.append(f"💬 {r['answer']}")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).rstrip(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to FAQ",
                                 callback_data=f"pfaq:view:{product_id}")
        ]]),
    )
    return ConversationHandler.END


async def user_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pfaq_user_search_pid", None)
    await update.message.reply_text("Search cancelled.")
    return ConversationHandler.END


def build_user_faq_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(user_search_start, pattern=r"^pfaq:search:\d+$"),
        ],
        states={
            _USER_SEARCH_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_search_query),
            ],
        },
        fallbacks=[CommandHandler("cancel", user_search_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
