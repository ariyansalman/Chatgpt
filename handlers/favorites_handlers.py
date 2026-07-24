"""User-facing Favorites (Bookmark) handlers — V22.

Callback namespace: ``fav:*``

Callbacks handled:
    fav:add:<pid>                 — add product to favorites
    fav:rm:<pid>                  — remove from favorites
    fav:list:<sort>:<page>        — view favorites list (sort: new/old/price/alpha)
    fav:buy:<pid>                 — buy directly from favorites
    fav:view:<pid>                — view product page from favorites
    fav:clear                     — clear all favorites (confirm prompt)
    fav:clr_ok                    — confirm clear all

Search is handled via ConversationHandler (see build_fav_search_conv).
    fav:search                    — entry point (sets up search context)
    FAV_SEARCH state              — receives text → filters by name

Feature-status guards:
    disabled    → stale callbacks get a brief answer; button is hidden
    maintenance → maintenance notice shown (admins pass through)
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler,
    CommandHandler, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import services.favorites_service as svc
from utils import check_user_banned
from utils.helpers import is_admin

logger = logging.getLogger(__name__)

# ConversationHandler state
FAV_SEARCH = 100


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup,
                                      parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _sort_buttons(current: str, page: int) -> list[InlineKeyboardButton]:
    opts = [
        ("new",   "🆕 New"),
        ("old",   "📅 Old"),
        ("price", "💰 Price"),
        ("alpha", "🔤 A-Z"),
    ]
    return [
        InlineKeyboardButton(
            f"{'✅' if s == current else ''}{lbl}",
            callback_data=f"fav:list:{s}:0",
        )
        for s, lbl in opts
    ]


def _list_keyboard(items: list[dict], sort: str, page: int,
                   total: int) -> InlineKeyboardMarkup:
    rows = []

    # Per-product action rows
    for item in items:
        pid = item["product_id"]
        stock_icon = "✅" if item["is_active"] and item["stock_count"] > 0 else "❌"
        rows.append([
            InlineKeyboardButton(f"🛒 Buy",        callback_data=f"fav:buy:{pid}"),
            InlineKeyboardButton(f"📄 View",       callback_data=f"fav:view:{pid}"),
            InlineKeyboardButton(f"🗑 Remove",     callback_data=f"fav:rm:{pid}"),
        ])

    # Sort buttons
    rows.append(_sort_buttons(sort, page))

    # Search
    rows.append([
        InlineKeyboardButton("🔍 Search",  callback_data="fav:search"),
        InlineKeyboardButton("🏠 Menu",    callback_data="main_menu"),
    ])

    # Clear all
    if svc.allow_clear_all() and total > 0:
        rows.append([InlineKeyboardButton("❌ Clear All Favorites", callback_data="fav:clear")])

    # Pagination
    pages = svc.total_pages(total)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"fav:list:{sort}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"fav:list:{sort}:{page+1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _build_list_text(items: list[dict], sort: str, page: int, total: int,
                     search: str = "") -> str:
    if not items:
        if search:
            return (
                f"🔍 <b>Search: \"{search}\"</b>\n\n"
                "No favorites matched your search."
            )
        return (
            "❤️ <b>MY FAVORITES</b>\n\n"
            "Your favorites list is empty.\n"
            "Browse products and tap <b>❤️ Add to Favorites</b> to save them here."
        )

    sort_label = {"new": "Newest", "old": "Oldest",
                  "price": "Price ↑", "alpha": "A–Z"}.get(sort, sort)
    header = (
        f"❤️ <b>MY FAVORITES</b> — {total} saved  |  Sort: {sort_label}"
        + (f"\n🔍 Search: \"<i>{search}</i>\"" if search else "")
    )
    lines = [header, ""]

    for i, item in enumerate(items, start=page * svc._PAGE_SIZE + 1):
        price_str = f"${item['price']:.2f}"
        if item["discount_pct"]:
            price_str += f"  <s>${item['orig_price']:.2f}</s>  <b>-{item['discount_pct']}%</b>"
        avail = "✅" if item["is_active"] and item["stock_count"] > 0 else "❌ Unavailable"
        added = item["added_at"].strftime("%Y-%m-%d") if item["added_at"] else "—"
        updated = item["updated_at"].strftime("%Y-%m-%d") if item["updated_at"] else "—"
        lines.append(
            f"<b>{i}. {item['name']}</b>\n"
            f"   💰 {price_str}\n"
            f"   📦 Stock: {item['stock_count']}  {avail}\n"
            f"   📂 {item['category']}  |  🕐 Added: {added}\n"
            f"   🔄 Last updated: {updated}"
        )

    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Feature guard
# ─────────────────────────────────────────────────────────────────────────

def _check_status(tg_id: int) -> str | None:
    """Return a block message if feature is unavailable, else None."""
    if not svc.cfg.get_bool("feature_favorites_enabled", True):
        return "disabled"
    status = svc.feature_status()
    if status == "disabled":
        return "disabled"
    if status == "maintenance" and not is_admin(tg_id):
        return "maintenance"
    return None


# ─────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────

async def fav_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()

    block = _check_status(tg_id)
    if block == "disabled":
        await query.answer("Favorites is currently unavailable.", show_alert=True)
        return
    if block == "maintenance":
        await _safe_edit(
            query,
            "⚠️ <b>Favorites is currently under maintenance.</b>\n"
            "Please try again later.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]),
        )
        return

    data = query.data
    parts = data.split(":")

    if len(parts) < 2:
        return
    action = parts[1]

    # ── fav:add:<pid> ──────────────────────────────────────────────────────
    if action == "add" and len(parts) >= 3 and parts[2].isdigit():
        pid = int(parts[2])
        ok, msg = svc.add_favorite(tg_id, pid)
        count = svc.get_count(tg_id)
        counter = f" [{count}]" if svc.show_counter() else ""
        await _safe_edit(
            query, msg,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"❤️ My Favorites{counter}", callback_data="fav:list:new:0")],
                [InlineKeyboardButton("🔙 Back to Product", callback_data=f"product_{pid}")],
            ]),
        )
        return

    # ── fav:rm:<pid> ───────────────────────────────────────────────────────
    if action == "rm" and len(parts) >= 3 and parts[2].isdigit():
        pid = int(parts[2])
        ok, msg = svc.remove_favorite(tg_id, pid)
        count = svc.get_count(tg_id)
        rows = [
            [InlineKeyboardButton("❤️ My Favorites", callback_data="fav:list:new:0")],
        ]
        # "Back to product" button only makes sense if we came from a product page;
        # when removing from the list we go back to the list.
        rows.append([InlineKeyboardButton("🔙 Back to List", callback_data="fav:list:new:0")])
        await _safe_edit(query, msg, InlineKeyboardMarkup(rows))
        return

    # ── fav:list:<sort>:<page> ─────────────────────────────────────────────
    if action == "list":
        sort = parts[2] if len(parts) > 2 else "new"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        search = context.user_data.get("fav_search", "")
        items, total = svc.get_favorites_page(tg_id, sort, page, search)
        text = _build_list_text(items, sort, page, total, search)
        kb = _list_keyboard(items, sort, page, total)
        await _safe_edit(query, text, kb)
        return

    # ── fav:buy:<pid> ──────────────────────────────────────────────────────
    if action == "buy" and len(parts) >= 3 and parts[2].isdigit():
        pid = int(parts[2])
        from handlers.payment_handlers import buy_product_start
        from utils.update_proxy import with_data
        try:
            await buy_product_start(with_data(update, f"buy_{pid}"), context)
        except Exception:
            logger.exception("favorites: fav:buy redirect failed")
            await query.answer("Redirecting…", show_alert=False)
        return

    # ── fav:view:<pid> ─────────────────────────────────────────────────────
    if action == "view" and len(parts) >= 3 and parts[2].isdigit():
        pid = int(parts[2])
        from handlers.user_handlers import product_detail_callback
        from utils.update_proxy import with_data
        try:
            await product_detail_callback(with_data(update, f"product_{pid}"), context)
        except Exception:
            logger.exception("favorites: fav:view redirect failed")
        return

    # ── fav:search ─────────────────────────────────────────────────────────
    if action == "search":
        # Handled by ConversationHandler; this branch is fallback
        await _safe_edit(
            query,
            "🔍 Please type a product name to search your favorites.\n\n"
            "Send /cancel to return to the list.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="fav:list:new:0"),
            ]]),
        )
        context.user_data["fav_search_active"] = True
        return

    # ── fav:clear ──────────────────────────────────────────────────────────
    if action == "clear":
        count = svc.get_count(tg_id)
        if count == 0:
            await _safe_edit(query, "ℹ️ Your favorites list is already empty.",
                             InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="fav:list:new:0")]]))
            return
        await _safe_edit(
            query,
            f"❌ <b>Clear All Favorites?</b>\n\n"
            f"This will permanently remove <b>{count}</b> saved product(s).\n"
            f"This action cannot be undone.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, clear all", callback_data="fav:clr_ok")],
                [InlineKeyboardButton("🚫 Cancel",          callback_data="fav:list:new:0")],
            ]),
        )
        return

    # ── fav:clr_ok ─────────────────────────────────────────────────────────
    if action == "clr_ok":
        n = svc.clear_all_favorites(tg_id)
        context.user_data.pop("fav_search", None)
        await _safe_edit(
            query,
            f"🗑 <b>All {n} favorite(s) cleared.</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
                [InlineKeyboardButton("⬅️ Back to Menu",       callback_data="main_menu")],
            ]),
        )
        return

    # ── fav:clearsearch ────────────────────────────────────────────────────
    if action == "clearsearch":
        context.user_data.pop("fav_search", None)
        items, total = svc.get_favorites_page(tg_id, "new", 0, "")
        text = _build_list_text(items, "new", 0, total)
        kb = _list_keyboard(items, "new", 0, total)
        await _safe_edit(query, text, kb)
        return

    await query.answer("Unknown action.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────
# Search ConversationHandler
# ─────────────────────────────────────────────────────────────────────────

async def _search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """fav:search callback — entry point for the search conversation."""
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    if check_user_banned(tg_id):
        return ConversationHandler.END

    block = _check_status(tg_id)
    if block:
        await _safe_edit(query, "⚠️ Favorites is currently unavailable.",
                         InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]))
        return ConversationHandler.END

    await _safe_edit(
        query,
        "🔍 <b>Search Favorites</b>\n\n"
        "Type a product name (or part of it) to filter your saved items.\n"
        "Send /cancel to go back.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="fav:list:new:0")]]),
    )
    return FAV_SEARCH


async def _search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the search query text, stores it, and shows filtered results."""
    tg_id = update.effective_user.id
    query_text = (update.message.text or "").strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    context.user_data["fav_search"] = query_text

    items, total = svc.get_favorites_page(tg_id, "new", 0, query_text)
    text = _build_list_text(items, "new", 0, total, query_text)
    kb = _list_keyboard(items, "new", 0, total)

    # Add "Clear Search" button
    clear_row = [InlineKeyboardButton("✖ Clear Search", callback_data="fav:clearsearch")]
    kb_rows = list(kb.inline_keyboard)
    kb_rows.insert(0, clear_row)
    kb = InlineKeyboardMarkup(kb_rows)

    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def _search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("fav_search", None)
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "Search cancelled. Going back to your favorites.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❤️ My Favorites", callback_data="fav:list:new:0"),
            ]]),
        )
    return ConversationHandler.END


def build_fav_search_conv() -> ConversationHandler:
    """Build the search ConversationHandler. Register BEFORE fav_dispatch."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(_search_entry, pattern=r"^fav:search$")],
        states={
            FAV_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _search_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _search_cancel),
            CallbackQueryHandler(lambda u, c: ConversationHandler.END,
                                 pattern=r"^fav:list:"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────
# Main menu entry point (also called from main_menu shortcut)
# ─────────────────────────────────────────────────────────────────────────

async def my_favorites_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from main menu ❤️ My Favorites button."""
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()
    block = _check_status(tg_id)
    if block == "disabled":
        await query.answer("Favorites is currently unavailable.", show_alert=True)
        return
    if block == "maintenance":
        await _safe_edit(
            query,
            "⚠️ <b>Favorites is currently under maintenance.</b>\n"
            "Please try again later.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]),
        )
        return

    search = context.user_data.get("fav_search", "")
    items, total = svc.get_favorites_page(tg_id, "new", 0, search)
    text = _build_list_text(items, "new", 0, total, search)
    kb = _list_keyboard(items, "new", 0, total)
    await _safe_edit(query, text, kb)
