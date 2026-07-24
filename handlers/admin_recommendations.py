"""V45 — Admin Recommendation Engine Management.

Callback namespace: arec:*

Allows admins to:
  • View recommendation statistics
  • Pin/unpin manual recommendations for any section
  • Preview what recommendations would be shown for a product
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from services.recommendation_service import (
    get_trending, get_best_sellers, get_recommendation_stats,
    pin_recommendation, unpin_recommendation, list_pins,
    get_related, get_also_bought, get_fbt,
)
from utils.permissions import has_permission
from utils.audit import log_admin_action
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

AREC_PICK_SECTION  = 9840
AREC_PICK_PRODUCT  = 9841
AREC_PICK_RECPROD  = 9842

_SECTIONS = {
    "home":     "🏠 Home / Featured",
    "trending": "🔥 Trending",
    "fbt":      "🛒 Frequently Bought Together",
    "related":  "📦 Related Products",
    "custom":   "⭐ Custom Section",
}


def _back(to: str = "arec:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


async def _check(update: Update) -> bool:
    uid = update.effective_user.id
    if not has_permission(uid, "admin"):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


# ─── Dashboard ────────────────────────────────────────────────────────────────

async def arec_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    stats = await asyncio.to_thread(get_recommendation_stats)
    text = (
        "🎯 <b>RECOMMENDATION ENGINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Total pinned recommendations: <b>{stats['total_pins']}</b>\n"
    )
    if stats["sections"]:
        text += "\n<b>Pinned by section:</b>\n"
        for sec, cnt in stats["sections"].items():
            text += f"  • {sec}: {cnt}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Trending Now",      callback_data="arec:trending")],
        [InlineKeyboardButton("🏆 Best Sellers",      callback_data="arec:bestsellers")],
        [InlineKeyboardButton("📌 Manage Pins",       callback_data="arec:pins")],
        [InlineKeyboardButton("➕ Add Pin",            callback_data="arec:add_pin")],
        [InlineKeyboardButton("🔍 Preview (by product)", callback_data="arec:preview_start")],
        [_back("acc:root")],
    ])
    await _edit(update, text, kb)


# ─── Trending ─────────────────────────────────────────────────────────────────

async def arec_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    items = await asyncio.to_thread(get_trending, 15)
    if not items:
        text = "🔥 <b>Trending Now</b>\n\nNo trending products found."
    else:
        lines = ["🔥 <b>Trending Now</b> (last 30 days)\n"]
        for i, p in enumerate(items, 1):
            lines.append(
                f"{i}. <b>{p['name'][:35]}</b> — "
                f"{p.get('order_count', 0)} orders | ${p['price']:.2f}"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[_back()]])
    await _edit(update, text, kb)


# ─── Best Sellers ─────────────────────────────────────────────────────────────

async def arec_bestsellers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    items = await asyncio.to_thread(get_best_sellers, 15)
    if not items:
        text = "🏆 <b>Best Sellers</b>\n\nNo data yet."
    else:
        lines = ["🏆 <b>Best Sellers</b> (all-time)\n"]
        for i, p in enumerate(items, 1):
            lines.append(
                f"{i}. <b>{p['name'][:35]}</b> — "
                f"{p['sales_count']} sold | ${p['price']:.2f}"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[_back()]])
    await _edit(update, text, kb)


# ─── Manage Pins ─────────────────────────────────────────────────────────────

async def arec_pins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    pins = await asyncio.to_thread(list_pins)
    if not pins:
        text = "📌 <b>Pinned Recommendations</b>\n\nNo pinned recommendations yet."
    else:
        lines = [f"📌 <b>Pinned Recommendations</b> ({len(pins)} total)\n"]
        for pin in pins[:20]:
            lines.append(
                f"  [{pin['section']}] #{pin['pin_id']}: "
                f"<b>{pin['name'][:30]}</b>"
            )
        text = "\n".join(lines)

    kb_rows = [
        [InlineKeyboardButton(f"🗑 Remove #{pin['pin_id']}: {pin['name'][:15]}",
                               callback_data=f"arec:unpin:{pin['pin_id']}")]
        for pin in pins[:8]
    ]
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def arec_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    pin_id = int(q.data.split(":")[-1])
    admin_id = update.effective_user.id
    ok = await asyncio.to_thread(unpin_recommendation, pin_id, admin_id)
    if ok:
        await q.answer("✅ Pin removed.", show_alert=True)
    else:
        await q.answer("❌ Pin not found.", show_alert=True)
    await arec_pins(with_data(update, "arec:pins"), context)


# ─── Add Pin ConvHandler ──────────────────────────────────────────────────────

async def arec_add_pin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return ConversationHandler.END
    sections_text = "\n".join(f"  • <code>{s}</code> — {l}" for s, l in _SECTIONS.items())
    await q.edit_message_text(
        f"📌 <b>Add Pinned Recommendation</b>\n\n"
        f"Step 1/3: Enter the <b>section name</b>:\n{sections_text}\n\n"
        "Or type any custom section name:",
        parse_mode="HTML"
    )
    return AREC_PICK_SECTION


async def arec_got_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    section = update.message.text.strip()[:64]
    context.user_data["arec_section"] = section
    await update.message.reply_text(
        f"✅ Section: <code>{section}</code>\n\n"
        "Step 2/3: Enter the <b>source product ID</b> (or <code>0</code> for global):",
        parse_mode="HTML"
    )
    return AREC_PICK_PRODUCT


async def arec_got_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a numeric ID (or 0 for global).")
        return AREC_PICK_PRODUCT
    product_id = int(text) if int(text) > 0 else None
    context.user_data["arec_product_id"] = product_id
    await update.message.reply_text(
        "Step 3/3: Enter the <b>product ID to recommend</b>:",
        parse_mode="HTML"
    )
    return AREC_PICK_RECPROD


async def arec_got_recprod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a numeric product ID.")
        return AREC_PICK_RECPROD
    rec_product_id = int(text)
    section = context.user_data.get("arec_section", "home")
    product_id = context.user_data.get("arec_product_id")
    admin_id = update.effective_user.id

    ok = await asyncio.to_thread(
        pin_recommendation, admin_id, section, product_id, rec_product_id, 0
    )
    if ok:
        await update.message.reply_text(
            f"✅ <b>Pinned!</b>\n\n"
            f"Section: <code>{section}</code>\n"
            f"Product ID: <b>{rec_product_id}</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Could not pin — already exists or product not found.")
    return ConversationHandler.END


async def arec_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── Preview (what would show for a product) ─────────────────────────────────

async def arec_preview_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick inline preview — user enters product ID via follow-up."""
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    await _edit(update,
                "🔍 <b>Recommendation Preview</b>\n\nEnter a product ID to preview recommendations.",
                InlineKeyboardMarkup([[_back()]]))


# ─── Register ─────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    add_pin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(arec_add_pin_start, pattern=r"^arec:add_pin$")],
        states={
            AREC_PICK_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, arec_got_section)],
            AREC_PICK_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, arec_got_product)],
            AREC_PICK_RECPROD: [MessageHandler(filters.TEXT & ~filters.COMMAND, arec_got_recprod)],
        },
        fallbacks=[CommandHandler("cancel", arec_conv_cancel)],
        per_message=False,
        allow_reentry=True,
        name="arec_add_pin",
    )
    application.add_handler(add_pin_conv)
    application.add_handler(CallbackQueryHandler(arec_menu,         pattern=r"^arec:menu$"))
    application.add_handler(CallbackQueryHandler(arec_trending,     pattern=r"^arec:trending$"))
    application.add_handler(CallbackQueryHandler(arec_bestsellers,  pattern=r"^arec:bestsellers$"))
    application.add_handler(CallbackQueryHandler(arec_pins,         pattern=r"^arec:pins$"))
    application.add_handler(CallbackQueryHandler(arec_unpin,        pattern=r"^arec:unpin:"))
    application.add_handler(CallbackQueryHandler(arec_preview_start,pattern=r"^arec:preview_start$"))
    logger.info("V45: Recommendation Engine admin handlers registered.")
