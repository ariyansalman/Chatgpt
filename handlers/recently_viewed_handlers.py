"""User-facing Recently Viewed handlers — V23.

Callback namespace: ``rv:*``

Callbacks handled:
    rv:list:<page>            — paginated recently viewed list
    rv:buy:<pid>              — buy directly from recently viewed
    rv:view:<pid>             — view product page from recently viewed
    rv:rm:<pid>               — remove a single item from history
    rv:clear                  — clear all history (confirm prompt)
    rv:clr_ok                 — confirmed clear all

Search is handled via ConversationHandler (see build_rv_search_conv).
    rv:search                 — entry point (sets up search context)
    RV_SEARCH state           — receives text → filters by name

Feature-status guards:
    disabled    → stale callbacks get a brief answer; button is hidden
    maintenance → maintenance notice shown (admins pass through)
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, CommandHandler, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import services.recently_viewed_service as svc
from utils import check_user_banned
from utils.helpers import is_admin

logger = logging.getLogger(__name__)

# ConversationHandler state
RV_SEARCH = 201


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _check_status(tg_id: int) -> str:
    """Return '' (ok) | 'maintenance' | 'disabled'."""
    status = svc.feature_status()
    if not svc.is_enabled():
        if status == "maintenance" and is_admin(tg_id):
            return ""   # admins bypass maintenance
        return "disabled" if status == "disabled" else "maintenance"
    return ""


def _fmt_price(price: float) -> str:
    return f"${price:.2f}"


def _stock_icon(item: dict) -> str:
    if not item["is_active"]:
        return "⛔"
    return "✅" if item["stock_count"] > 0 else "❌"


def _discount_line(item: dict) -> str:
    """Return a discount string like ' (-15%)' or empty string."""
    if item["sale_price"] and item["sale_price"] < item["price"] and item["price"] > 0:
        pct = round((1 - item["sale_price"] / item["price"]) * 100)
        return f" <s>{_fmt_price(item['price'])}</s> → {_fmt_price(item['sale_price'])} (-{pct}%)"
    return f" {_fmt_price(item['price'])}"


def _when(viewed_at) -> str:
    if not viewed_at:
        return ""
    return viewed_at.strftime("%b %d, %H:%M")


# ─────────────────────────────────────────────────────────────────────────
# Keyboard builder
# ─────────────────────────────────────────────────────────────────────────

def _list_keyboard(
    items: list[dict],
    page: int,
    total: int,
    search: str = "",
) -> InlineKeyboardMarkup:
    rows = []

    for item in items:
        pid = item["product_id"]
        stock_icon = _stock_icon(item)
        can_buy = item["is_active"] and item["stock_count"] > 0
        row = []
        if can_buy:
            row.append(InlineKeyboardButton("🛒 Buy",    callback_data=f"rv:buy:{pid}"))
        row.append(InlineKeyboardButton("📄 View",       callback_data=f"rv:view:{pid}"))
        row.append(InlineKeyboardButton("🗑 Remove",     callback_data=f"rv:rm:{pid}"))
        rows.append(row)

    # Search + navigation
    rows.append([
        InlineKeyboardButton("🔍 Search", callback_data="rv:search"),
        InlineKeyboardButton("🏠 Menu",   callback_data="main_menu"),
    ])

    # Clear all
    if svc.allow_clear_all() and total > 0:
        rows.append([
            InlineKeyboardButton("🗑 Clear All History", callback_data="rv:clear")
        ])

    # Pagination
    pages = svc.total_pages(total)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"rv:list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"rv:list:{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────
# Text builder
# ─────────────────────────────────────────────────────────────────────────

def _build_list_text(
    items: list[dict],
    page: int,
    total: int,
    search: str = "",
) -> str:
    pages = svc.total_pages(total)
    header = "🕒 <b>Recently Viewed</b>"
    if search:
        header += f"  🔍 <i>\"{search}\"</i>"
    header += f"\n<i>{total} product(s)"
    if pages > 1:
        header += f"  •  Page {page + 1}/{pages}"
    header += "</i>\n"

    if not items:
        return header + "\nNo matching products found."

    lines = [header]
    for item in items:
        stock_icon = _stock_icon(item)
        cat = f" • <i>{item['category_name']}</i>" if item["category_name"] else ""
        price_str = _discount_line(item)
        when = _when(item["viewed_at"])
        stock_txt = (
            f"<b>{item['stock_count']}</b> in stock"
            if item["is_active"] and item["stock_count"] > 0
            else ("Out of stock" if item["is_active"] else "Inactive")
        )

        lines.append(
            f"{stock_icon} <b>{item['name']}</b>{cat}\n"
            f"   💰{price_str}\n"
            f"   📦 {stock_txt}   🕐 <i>{when}</i>"
        )

    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# List view
# ─────────────────────────────────────────────────────────────────────────

async def _show_list(query, tg_id: int, page: int, search: str = ""):
    items, total = svc.get_page(tg_id, page, search)

    if total == 0 and not search:
        text = (
            "🕒 <b>Recently Viewed</b>\n\n"
            "You haven't viewed any products yet.\n\n"
            "Browse the catalog and every product you open will appear here."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
            [InlineKeyboardButton("⬅️ Back to Menu",       callback_data="main_menu")],
        ])
        await _safe_edit(query, text, kb)
        return

    text = _build_list_text(items, page, total, search)
    kb = _list_keyboard(items, page, total, search)
    await _safe_edit(query, text, kb)


# ─────────────────────────────────────────────────────────────────────────
# Main entry (called from main menu "🕒 Recently Viewed" button)
# ─────────────────────────────────────────────────────────────────────────

async def my_recently_viewed_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Entry from main menu 🕒 Recently Viewed button (uf:rv)."""
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()
    block = _check_status(tg_id)
    if block == "disabled":
        await query.answer("🕒 Recently Viewed is currently unavailable.", show_alert=True)
        return
    if block == "maintenance":
        await _safe_edit(
            query,
            "⚠️ <b>Recently Viewed is currently under maintenance.</b>\n"
            "Please try again later.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")
            ]]),
        )
        return

    search = context.user_data.get("rv_search", "")
    await _show_list(query, tg_id, page=0, search=search)


# ─────────────────────────────────────────────────────────────────────────
# Search ConversationHandler
# ─────────────────────────────────────────────────────────────────────────

async def _search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _safe_edit(
        query,
        "🔍 <b>Search Recently Viewed</b>\n\nType a product name to filter your history.\n"
        "Send /cancel to go back.",
    )
    return RV_SEARCH


async def _search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = update.message.text.strip()
    context.user_data["rv_search"] = search
    tg_id = update.effective_user.id

    items, total = svc.get_page(tg_id, 0, search)
    text = _build_list_text(items, 0, total, search)
    kb = _list_keyboard(items, 0, total, search)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def _search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("rv_search", None)
    await update.message.reply_text(
        "Search cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🕒 Recently Viewed", callback_data="uf:rv"),
            InlineKeyboardButton("⬅️ Back to Menu",       callback_data="main_menu"),
        ]]),
    )
    return ConversationHandler.END


def build_rv_search_conv() -> ConversationHandler:
    """Build the search ConversationHandler. Register BEFORE rv_dispatch."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(_search_entry, pattern=r"^rv:search$")],
        states={
            RV_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _search_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _search_cancel),
            CallbackQueryHandler(
                lambda u, c: ConversationHandler.END,
                pattern=r"^rv:list:",
            ),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────
# Main dispatcher — handles all rv:* callbacks except rv:search
# ─────────────────────────────────────────────────────────────────────────

async def rv_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all rv:* callbacks."""
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()

    block = _check_status(tg_id)
    if block == "disabled":
        await query.answer("🕒 Recently Viewed is currently unavailable.", show_alert=True)
        return
    if block == "maintenance":
        await _safe_edit(
            query,
            "⚠️ <b>Recently Viewed is currently under maintenance.</b>\n"
            "Please try again later.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")
            ]]),
        )
        return

    data = query.data  # e.g. "rv:list:2" | "rv:rm:5" | "rv:clear" ...
    parts = data.split(":")

    # rv:list:<page>
    if len(parts) >= 3 and parts[1] == "list":
        try:
            page = int(parts[2])
        except (ValueError, IndexError):
            page = 0
        search = context.user_data.get("rv_search", "")
        await _show_list(query, tg_id, page, search)
        return

    # rv:rm:<pid>
    if len(parts) >= 3 and parts[1] == "rm":
        try:
            pid = int(parts[2])
        except (ValueError, IndexError):
            await query.answer("❌ Invalid product.", show_alert=True)
            return
        ok, msg = svc.remove_item(tg_id, pid)
        await query.answer(msg, show_alert=not ok)
        if ok:
            search = context.user_data.get("rv_search", "")
            await _show_list(query, tg_id, page=0, search=search)
        return

    # rv:view:<pid> — navigate to product detail
    if len(parts) >= 3 and parts[1] == "view":
        try:
            pid = int(parts[2])
        except (ValueError, IndexError):
            await query.answer("❌ Invalid product.", show_alert=True)
            return
        # Re-use the existing product detail callback by faking the data
        from handlers.user_handlers import product_detail_callback
        from utils.update_proxy import with_data
        try:
            await product_detail_callback(with_data(update, f"product_{pid}"), context)
        except Exception:
            logger.exception("recently_viewed: rv:view redirect failed")
            await query.answer("Redirecting…", show_alert=False)
        return

    # rv:buy:<pid> — go straight to purchase flow
    if len(parts) >= 3 and parts[1] == "buy":
        try:
            pid = int(parts[2])
        except (ValueError, IndexError):
            await query.answer("❌ Invalid product.", show_alert=True)
            return
        from handlers.payment_handlers import buy_product_start
        from utils.update_proxy import with_data
        try:
            await buy_product_start(with_data(update, f"buy_{pid}"), context)
        except Exception:
            logger.exception("recently_viewed: rv:buy redirect failed")
            await query.answer("Redirecting…", show_alert=False)
        return

    # rv:clear — ask for confirmation
    if len(parts) >= 2 and parts[1] == "clear":
        if not svc.allow_clear_all():
            await query.answer("Clear All is disabled.", show_alert=True)
            return
        count = svc.get_count(tg_id)
        if count == 0:
            await query.answer("History is already empty.", show_alert=True)
            return
        await _safe_edit(
            query,
            f"⚠️ <b>Clear Recently Viewed History</b>\n\n"
            f"This will permanently remove all <b>{count}</b> item(s) from your history.\n\n"
            f"Are you sure?",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Clear All", callback_data="rv:clr_ok"),
                    InlineKeyboardButton("❌ Cancel",          callback_data="rv:list:0"),
                ],
            ]),
        )
        return

    # rv:clr_ok — confirmed clear
    if len(parts) >= 2 and parts[1] == "clr_ok":
        ok, msg = svc.clear_all(tg_id)
        await query.answer(msg, show_alert=not ok)
        if ok:
            context.user_data.pop("rv_search", None)
            await _show_list(query, tg_id, page=0, search="")
        return

    # Fallback — show list
    await _show_list(query, tg_id, page=0, search="")
