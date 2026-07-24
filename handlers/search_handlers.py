"""Product search handlers — /search command and Search menu button.

Results mirror the flat "🛍 Products" catalog: every matching active
product is rendered in ONE message with ONE inline keyboard (one button per
product, no pagination, no category filter), using the same real available
stock, per-user currency price, and out-of-stock emoji override.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, Product
from utils import (
    check_user_banned, get_user_currency,
    catalog_stock_emoji, format_product_button_text,
)
from telegram.error import BadRequest

SEARCH_QUERY = 1


def _escape_like(text: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters so a search for a literal
    '%', '_', or '\\' in a product name behaves as a literal match rather
    than a wildcard. Uses '\\' as the ILIKE ESCAPE character."""
    return (
        text.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
    )


def _price_display(price: float, currency: str, telegram_id: int) -> str:
    from services.pricing import convert_currency
    user_currency = get_user_currency(telegram_id)
    amount = convert_currency(price, currency or "USD", user_currency)
    if user_currency == "BDT":
        return f"৳{amount:,.2f}"
    return f"${amount:.2f}"


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — from the main menu button or the 🔍 Search button on
    the flat products catalog."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    from telegram.error import BadRequest
    text = "🔍 Search Products\n\nSend the product name or keyword you want to search for."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]])
    try:
        if query.message and query.message.photo:
            await query.message.delete()
            await query.message.reply_text(text, reply_markup=keyboard)
        else:
            await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    return SEARCH_QUERY


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search <query> — one-shot search."""
    if check_user_banned(update.effective_user.id):
        await update.message.reply_text("⛔ You have been banned from using this bot.")
        return

    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        await update.message.reply_text(
            "🔍 Usage: /search <keyword>\nExample: /search netflix"
        )
        return
    await _run_search(update.message, q, update.effective_user.id)


async def search_query_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle typed keyword after the Search button."""
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Please type a keyword.")
        return SEARCH_QUERY
    await _run_search(update.message, q, update.effective_user.id)
    return ConversationHandler.END


async def _run_search(message, keyword: str, telegram_id: int):
    """Shared search executor — sends the full, unpaginated result list to
    ``message`` as one message with one inline keyboard."""
    like = f"%{_escape_like(keyword)}%"
    with get_db_session() as session:
        products = (
            session.query(Product)
            .filter(Product.is_active == True)
            .filter(Product.name.ilike(like, escape="\\"))
            .all()
        )
        rows = [
            {
                "id": p.id,
                "name": p.name,
                "emoji": p.product_emoji,
                "price": p.price,
                "currency": p.currency,
                "sort_order": p.sort_order,
            }
            for p in products
        ]

    if not rows:
        await message.reply_text(
            f"🔍 Search Results\n\nSearch: {keyword}\nFound: 0\n\nNo products matched your search.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="search")],
                [InlineKeyboardButton("↩️ All Products", callback_data="products")],
            ]),
        )
        return

    from services.inventory import count_available_bulk
    stock_map = count_available_bulk([r["id"] for r in rows])
    for r in rows:
        r["stock"] = stock_map.get(r["id"], 0)
        r["price_display"] = _price_display(r["price"], r["currency"], telegram_id)

    rows.sort(key=lambda r: (
        0 if r["stock"] > 0 else 1,
        r["sort_order"] if r["sort_order"] is not None else 10 ** 9,
        r["id"],
    ))

    keyboard = [
        [InlineKeyboardButton(
            format_product_button_text(
                catalog_stock_emoji(r["emoji"], r["stock"]),
                r["name"], r["price_display"], r["stock"],
            ),
            callback_data=f"product_{r['id']}",
        )]
        for r in rows
    ]
    keyboard.append([
        InlineKeyboardButton("🔍 Search Again", callback_data="search"),
        InlineKeyboardButton("🔄 Refresh", callback_data="products_refresh"),
    ])
    keyboard.append([InlineKeyboardButton("↩️ All Products", callback_data="products")])

    await message.reply_text(
        f"🔍 Search Results\n\nSearch: {keyword}\nFound: {len(rows)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
