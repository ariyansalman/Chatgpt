"""User-facing command and callback handlers."""

import os
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_db_session, User, Category, Subcategory, Product, Order, OrderItem, Settings, ProductType, OrderStatus, DisputeStatus
from sqlalchemy.orm import selectinload, joinedload
from utils import (
    format_price, format_datetime, create_main_menu_keyboard, create_product_detail_keyboard,
    create_support_keyboard, check_user_banned,
    paginate_items, format_product_display,
    build_availability_text, create_back_support_keyboard, create_language_keyboard,
    get_user_currency, toggle_user_currency,
    format_price_for_user, format_amount_in, catalog_stock_emoji, format_product_button_text,
)
from utils.currency import SUPPORTED_DISPLAY_CURRENCIES
from utils.perf import perf_track
from i18n import t, get_user_language, set_user_language, resolve_initial_language, LANGUAGE_NAMES
from utils.bot_config import cfg
from telegram.error import BadRequest
from services import payment_ui as pui
from utils.helpers import format_order_id as _fmt_oid


def _format_countdown(end_time) -> str:
    """Human-friendly countdown like '2h 15m left' / '45m left' / 'ending now'."""
    if not end_time:
        return ""
    remaining = (end_time - datetime.utcnow()).total_seconds()
    if remaining <= 0:
        return "ending now"
    days, rem = divmod(int(remaining), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


def _flash_sale_banner(product_id) -> str:
    """Return a ready-to-prepend banner block for a product's product-detail
    caption when a flash sale is currently live, or '' when none applies."""
    try:
        from services.pricing import get_flash_sale_for_display
        fs = get_flash_sale_for_display(product_id)
    except Exception:
        fs = None
    if not fs:
        return ""
    label = fs["label"] or "🔥 FLASH SALE"
    countdown = _format_countdown(fs["end_time"])
    return (
        f"⚡ {label} — {fs['discount_percent']:.0f}% OFF\n"
        f"Was ${fs['original_price']:.2f} → Now ${fs['sale_price']:.2f}\n"
        f"⏰ {countdown}\n"
    )


def _with_currency_toggle(keyboard_markup, telegram_id: int, currency: str = None):
    """Append a "🌐 Currency: USD/BDT" toggle row under the main menu keyboard.

    Controlled by the admin-tunable ``show_currency_toggle_button`` config
    flag (default OFF). When hidden, the keyboard is returned untouched so
    no empty row is left behind and the rest of the buttons stay intact.

    ``currency`` lets a caller that already has the user's preferred
    currency loaded (e.g. from a User row it just queried) pass it straight
    through instead of triggering another DB round trip via
    get_user_currency(). Falls back to looking it up when omitted.
    """
    if not cfg.get_bool("show_currency_toggle_button", False):
        return keyboard_markup
    if currency is None:
        currency = get_user_currency(telegram_id)
    other = "BDT" if currency == "USD" else "USD"
    rows = list(keyboard_markup.inline_keyboard)
    rows.append([InlineKeyboardButton(f"🌐 Currency: {currency} (tap for {other})",
                                       callback_data="currency_toggle")])
    return InlineKeyboardMarkup(rows)


def _product_price_for_user(product, telegram_id):
    """Format a product's price in the viewer's preferred display currency,
    converting from the product's own stored currency if needed."""
    if telegram_id is None:
        return format_price(product.price)
    from services.pricing import convert_currency
    product_currency = getattr(product, "currency", None) or "USD"
    user_currency = get_user_currency(telegram_id)
    amount = convert_currency(product.price, product_currency, user_currency)
    symbol = "৳" if user_currency == "BDT" else "$"
    if user_currency == "USD":
        return f"${amount:.2f}"
    return f"{symbol}{amount:,.2f}"


def _build_home_message(lang: str, balance_str: str, total_orders: int = 0) -> str:
    """Compose the /start & Main Menu dashboard card.

    Layout:
      🛍️ Premium Digital Store

      ✨ Premium AI subscriptions, software licenses, and digital products.

      💳 Wallet Balance: $X.XX
      📦 Total Orders: N

      👇 Select an option below.

    Title / description / footer remain configurable from the Admin Panel via:
      home_title / home_subtitle / home_footer
    Falls back to the defaults above when no custom value is set. Wallet
    balance and total orders are always live values, loaded by the caller.
    """
    title    = cfg.get_str("home_title",    "").strip() or t("start.dashboard_title", lang)
    subtitle = cfg.get_str("home_subtitle", "").strip() or t("start.dashboard_subtitle", lang) or "✨ Premium AI subscriptions, software licenses, and digital products."
    footer   = cfg.get_str("home_footer",   "").strip() or t("start.dashboard_footer", lang)
    wallet_label = t("start.dashboard_wallet_label", lang)
    orders_label = t("start.dashboard_orders_label", lang)

    parts = [
        title,
        subtitle,
        f"{wallet_label}: {balance_str}\n{orders_label}: {total_orders}",
        footer,
    ]
    return "\n\n".join(parts)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show welcome message with wallet balance."""
    user = update.effective_user
    telegram_id = user.id
    username = user.username

    # Check if user is banned (banned message shown in whatever language they
    # already have saved; new/unknown users just get English, which is fine
    # since they're about to be told they can't use the bot anyway).
    if check_user_banned(telegram_id):
        await update.message.reply_text(t("common.banned", get_user_language(telegram_id)))
        return

    # Parse referral code / deep-linked product from /start payload
    referrer_telegram_id = None
    deep_link_product_id = None
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                referrer_telegram_id = int(payload[4:])
            except ValueError:
                referrer_telegram_id = None
        elif payload.startswith("product_"):
            # Used by the Channel Auto-Post "🛒 Buy Now" button
            # (services/channel_poster.py) to jump straight to a product.
            try:
                deep_link_product_id = int(payload[len("product_"):])
            except ValueError:
                deep_link_product_id = None

    # Get or create user and fetch settings in same session
    with get_db_session() as session:
        db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not db_user:
            referrer_id = None
            if referrer_telegram_id and referrer_telegram_id != telegram_id:
                referrer = session.query(User).filter_by(telegram_id=referrer_telegram_id).first()
                if referrer:
                    referrer_id = referrer.id
            db_user = User(
                telegram_id=telegram_id, username=username, referred_by_id=referrer_id,
                language=resolve_initial_language(user),
            )
            session.add(db_user)
            session.commit()
            session.refresh(db_user)

            # Activity Feed: new user registered (best-effort, non-blocking)
            try:
                import asyncio as _asyncio
                from services.activity_feed import post_event as _af_post, EVENT_USER_REGISTERED
                _asyncio.create_task(_af_post(context.bot, EVENT_USER_REGISTERED, {
                    "telegram_id": telegram_id,
                    "name": user.full_name or user.first_name or "",
                    "username": username or "",
                    "referred_by": str(referrer_id) if referrer_id else "",
                }))
            except Exception:
                pass

            # Enterprise Admin Notification: new user registration (best-effort)
            try:
                import asyncio as _asyncio
                from services.notifications import notify_admins as _notify_admins
                from utils.notify_format import render as _render_notif, utc_now_str as _ts
                _display_name = user.full_name or user.first_name or str(telegram_id)
                _uname_str = f"@{username}" if username else None
                _asyncio.create_task(_notify_admins(
                    context.bot,
                    "new_user",
                    _render_notif("👤", "New Registration", [
                        ("Name", _display_name),
                        ("Username", _uname_str),
                        ("Telegram ID", f"<code>{telegram_id}</code>"),
                        ("Referred By", f"<code>{referrer_id}</code>" if referrer_id else "Organic"),
                    ], _ts()),
                ))
            except Exception:
                pass

        wallet_balance = db_user.wallet_balance
        lang = db_user.language or "en"
        user_currency = db_user.preferred_currency if db_user.preferred_currency in SUPPORTED_DISPLAY_CURRENCIES else "USD"
        total_orders = session.query(Order).filter_by(user_id=db_user.id).count()

        store_settings = session.query(Settings).first()
        logo_path = store_settings.store_logo_path if store_settings else None

    # Send logo if available
    if logo_path and os.path.exists(logo_path):
        with open(logo_path, 'rb') as logo:
            await update.message.reply_photo(photo=logo)

    message = _build_home_message(lang, balance_str=format_amount_in(wallet_balance, user_currency), total_orders=total_orders)

    await update.message.reply_text(
        message,
        parse_mode="Markdown",
        reply_markup=_with_currency_toggle(
            create_main_menu_keyboard(lang=lang, user_id=telegram_id), telegram_id, currency=user_currency
        )
    )

    # Deep-linked product (from a Channel Auto-Post "🛒 Buy Now" button):
    # show the product right after the welcome message so the tap actually
    # lands somewhere useful instead of just the main menu.
    if deep_link_product_id is not None:
        await _send_product_detail(update, context, deep_link_product_id)


async def _send_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """Send a product-detail message from a plain /start deep link (no
    callback_query available). Mirrors ``product_detail_callback`` below but
    replies to the message instead of editing a callback's message."""
    telegram_id = update.effective_user.id

    # V23: Track this view in recently-viewed history (best-effort, non-blocking)
    try:
        from handlers.feature_handlers import track_recently_viewed
        track_recently_viewed(telegram_id, product_id)
    except Exception:
        pass

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id, is_active=True).first()
        if not product:
            return

        # Flat catalog: back navigation always returns to the complete
        # all-products list — there is no category/subcategory state.
        back_callback = "back_to_products"

        details = format_product_display(product, include_description=True)
        details = details.replace(
            f"💰 <b>Price:</b> {format_price(product.price)}",
            f"💰 <b>Price:</b> {_product_price_for_user(product, telegram_id)}",
        )
        try:
            from services.badges import badge_line
            _bl = badge_line(product)
            if _bl:
                details = f"{_bl}\n\n{details}"
        except Exception:
            pass

        try:
            from services.social_proof import get_social_proof
            _sp = get_social_proof(product.id).format()
            if _sp:
                details = f"{details}\n{_sp}"
        except Exception:
            pass

        # Urgency: "⚠️ Only 3 left!" when available stock is at/below the
        # admin-configured low_stock_threshold (default 5).
        try:
            from services.inventory import low_stock_warning
            _lsw = low_stock_warning(product.id)
            if _lsw:
                details = f"{details}\n{_lsw}"
        except Exception:
            pass

        banner = _flash_sale_banner(product.id)
        if banner:
            details = f"{banner}\n{details}"

        image_path = product.image_path
        stock_count = product.stock_count  # capture before session closes

    keyboard = create_product_detail_keyboard(
        product_id,
        back_callback,
        telegram_id=telegram_id,
        stock_count=stock_count,
    )
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as image:
            await update.message.reply_photo(photo=image, caption=details, reply_markup=keyboard, parse_mode='HTML')
    else:
        await update.message.reply_text(details, reply_markup=keyboard, parse_mode='HTML')


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu callback - return to main menu."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    lang = get_user_language(user_id)

    # Check if user is banned
    if check_user_banned(user_id):
        try:
            await query.edit_message_text(t("common.banned", lang))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # NOTE: "⬅️ Back to Menu" must always return the user to the Main Menu.
    # The main_menu_status gate (store closed/maintenance) intentionally does
    # NOT apply here — this callback is purely in-bot navigation back to the
    # existing Main Menu render, not a fresh /start or a store-availability
    # entry point. (Admins can still configure main_menu_status for whatever
    # other surface may consult it; it's just not enforced on this button.)

    with get_db_session() as session:
        db_user = session.query(User).filter_by(telegram_id=user_id).first()
        if not db_user:
            db_user = User(telegram_id=user_id, language=resolve_initial_language(update.effective_user))
            session.add(db_user)
            session.commit()
            session.refresh(db_user)

        wallet_balance = db_user.wallet_balance
        lang = db_user.language or "en"
        total_orders = session.query(Order).filter_by(user_id=db_user.id).count()

    message = _build_home_message(lang, balance_str=format_price_for_user(wallet_balance, user_id), total_orders=total_orders)

    try:
        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=_with_currency_toggle(create_main_menu_keyboard(lang=lang, user_id=user_id), user_id)
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def currency_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip the user's preferred display currency (USD <-> BDT) and refresh the main menu."""
    query = update.callback_query
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    new_currency = toggle_user_currency(user_id)
    await query.answer(t("common.prices_now_in", lang, currency=new_currency))
    await main_menu_callback(update, context)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /language command — same picker as the 🌐 Language button."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    await update.message.reply_text(
        f"{t('language.title', lang)}\n\n{t('language.prompt', lang)}",
        reply_markup=create_language_keyboard(lang=lang),
        parse_mode="HTML",
    )


async def language_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the language picker (🌐 Language button on the main menu)."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    try:
        await query.edit_message_text(
            f"{t('language.title', lang)}\n\n{t('language.prompt', lang)}",
            reply_markup=create_language_keyboard(lang=lang),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a language selection (callback_data='set_lang_en' / 'set_lang_bn')."""
    query = update.callback_query
    user_id = update.effective_user.id
    new_lang = query.data.replace("set_lang_", "", 1)
    new_lang = set_user_language(user_id, new_lang)
    # Answer the query with a toast — a callback query can only be answered
    # ONCE, so we must not call main_menu_callback() afterwards (it also
    # calls query.answer() and would raise BadRequest, preventing the
    # message from ever being refreshed in the new language).
    await query.answer(t("language.saved", new_lang, language=LANGUAGE_NAMES.get(new_lang, new_lang)))
    # Refresh the current message to show the main menu in the new language.
    with get_db_session() as session:
        db_user = session.query(User).filter_by(telegram_id=user_id).first()
        if not db_user:
            return
        wallet_balance = db_user.wallet_balance
        total_orders = session.query(Order).filter_by(user_id=db_user.id).count()
    message = _build_home_message(
        new_lang,
        balance_str=format_price_for_user(wallet_balance, user_id),
        total_orders=total_orders,
    )
    try:
        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=_with_currency_toggle(
                create_main_menu_keyboard(lang=new_lang, user_id=user_id), user_id
            ),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def _safe_edit_catalog(query, text, reply_markup):
    """Edit the current message in place for the flat catalog, handling the
    couple of edge cases Telegram throws at us:

    - The current message is a photo (can't turn a photo into plain text in
      place) -> delete it and send one fresh text message instead.
    - "Message is not modified" (e.g. Refresh with no actual changes) ->
      swallow it silently rather than raising/crashing.
    """
    from telegram.error import BadRequest
    message = getattr(query, "message", None)
    try:
        if message is not None and getattr(message, "photo", None):
            await message.delete()
            await message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _shorten_product_name(name: str, budget: int = 28) -> str:
    """Produce a compact display name for catalog buttons.

    Strips common duration tokens (e.g. "12 Months", "1 Year") and
    generic nouns ("Account", "Access", "Subscription", "Package") so
    the button stays tight and readable on mobile.  The full product
    name is never modified in the database and still appears on the
    Product Details page.

    Examples:
      "Gemini 12 Months Ready Account"  →  "Gemini Ready"
      "ChatGPT Go"                      →  "ChatGPT Go"
      "Canva Pro Annual Plan"            →  "Canva Pro"
    """
    # 1. Strip duration patterns  e.g. "12 Months", "3 Years", "1 Week"
    result = re.sub(
        r'\b\d+\s*(?:month|months|year|years|week|weeks|day|days)\b',
        '', name, flags=re.IGNORECASE
    )
    # 2. Strip standalone time-period words
    result = re.sub(
        r'\b(?:annual|monthly|yearly|lifetime|forever|permanent)\b',
        '', result, flags=re.IGNORECASE
    )
    # 3. Strip generic account/service nouns
    result = re.sub(
        r'\b(?:account|access|subscription|plan|pack|package|license|membership|trial|edition)\b',
        '', result, flags=re.IGNORECASE
    )
    # 4. Collapse whitespace
    result = re.sub(r'\s+', ' ', result).strip()
    # 5. Fall back to original if stripping ate everything
    if not result:
        result = name.strip()
    # 6. Hard truncate to budget with ellipsis
    if len(result) > budget:
        result = result[:max(1, budget - 1)].rstrip() + '…'
    return result


def build_all_products_keyboard(rows, page=0, total_pages=1,
                                  show_refresh=True, allow_pagination=True,
                                  show_stock=True):
    """Build the flat catalog inline keyboard with optional pagination controls.

    ``rows`` — list of dicts: id / name / emoji / price_display / stock.
    ``page`` — 0-based current page index.
    ``total_pages`` — total number of pages.
    ``show_refresh`` — whether to include the 🔄 Refresh button.
    ``allow_pagination`` — whether to include ⬅ Previous / ➡ Next buttons.
    ``show_stock`` — whether to append the stock count to each button label.
    """
    keyboard = []
    for r in rows:
        stock    = r.get("stock", 0)
        # Product emoji stays as-is (📦 fallback); ❌ only in stock indicator
        emoji    = (r.get("emoji") or "").strip() or "📦"
        price    = r["price_display"]
        raw_name = (r["name"] or "").strip()

        # Compact display name — strip duration/generic words for readability
        # Full name still shown on Product Details page (no DB change)
        display_name = _shorten_product_name(raw_name, budget=26)

        if show_stock:
            if stock <= 0:
                stock_indicator = "❌ Out of Stock"
            else:
                stock_indicator = f"📦 {stock} In Stock"
            suffix = f" • {price} • {stock_indicator}"
        else:
            suffix = f" • {price}"

        prefix = f"{emoji} "
        # Safety: trim display_name further if combined label exceeds 64 chars
        hard_budget = 64 - len(prefix) - len(suffix)
        if hard_budget < 4:
            hard_budget = 4
        if len(display_name) > hard_budget:
            display_name = display_name[:max(1, hard_budget - 1)].rstrip() + "…"

        label = f"{prefix}{display_name}{suffix}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"product_{r['id']}")])

    # Pagination row — only shown when there is more than one page
    if allow_pagination and total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Previous",
                                                callback_data=f"products_page_{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶️",
                                                callback_data=f"products_page_{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)

    # Bottom utility row
    bottom_row = []
    if show_refresh:
        bottom_row.append(InlineKeyboardButton("🔄 Refresh",
                                               callback_data=f"products_page_{page}"))
    bottom_row.append(InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"))
    keyboard.append(bottom_row)

    return keyboard


async def render_all_products_catalog(query, context, telegram_id,
                                       notice=None, page=0):
    """THE canonical renderer for the paginated flat "🛍 Products" screen.

    Reused by the Products button, Refresh, pagination, and Back-to-Products.
    Reads admin-configurable settings (products_per_page, pagination toggle,
    refresh button, stock display, counter display, feature status) from
    BotConfig so every option is adjustable from the Admin Panel without a
    restart.

    Pagination uses the ``products_page_{N}`` callback_data pattern already
    registered in bot.py under the ``^products`` handler pattern.
    """
    # ── Feature status gate ───────────────────────────────────────────────────
    status = cfg.get("product_pagination_status", "enabled")
    if status == "disabled":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
        ]])
        await _safe_edit_catalog(
            query,
            "🛍️ <b>Products</b>\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n🔴 The product list is currently unavailable.",
            kb,
        )
        return
    if status == "maintenance":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Try Again", callback_data="products_refresh"),
             InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
        ])
        await _safe_edit_catalog(
            query,
            "🛍️ <b>Products</b>\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n🟡 The product list is currently under maintenance.\n"
            "Please check back shortly.",
            kb,
        )
        return

    # ── Read admin-configurable settings ─────────────────────────────────────
    per_page       = max(1, min(50, cfg.get_int("products_per_page", 20)))
    allow_pag      = cfg.get_bool("product_list_allow_pagination", True)
    show_refresh   = cfg.get_bool("product_list_refresh_button", True)
    show_stock     = cfg.get_bool("product_list_show_stock", True)
    show_counter   = cfg.get_bool("product_list_show_counter", True)

    # ── Load & sort all active products ──────────────────────────────────────
    with get_db_session() as session:
        products = session.query(Product).filter(Product.is_active == True).all()
        rows = [
            {
                "id": p.id,
                "name": p.name,
                "emoji": p.product_emoji,
                "price_display": _product_price_for_user(p, telegram_id),
                "sort_order": p.sort_order,
            }
            for p in products
        ]

    if not rows:
        text = (
            "🛍️ <b>Products</b>\n"
            "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n"
            "📭 No products are currently available.\n\n"
            "Please check back later."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="products_refresh")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
        ])
        await _safe_edit_catalog(query, text, kb)
        return

    from services.inventory import count_available_bulk
    stock_map = count_available_bulk([r["id"] for r in rows])
    for r in rows:
        r["stock"] = stock_map.get(r["id"], 0)

    # In-stock first; within group: manual sort_order then id ASC
    rows.sort(key=lambda r: (
        0 if r["stock"] > 0 else 1,
        r["sort_order"] if r["sort_order"] is not None else 10 ** 9,
        r["id"],
    ))

    total_count     = len(rows)
    available_count = sum(1 for r in rows if r["stock"] > 0)

    # ── Pagination arithmetic ─────────────────────────────────────────────────
    if allow_pag:
        total_pages = max(1, (total_count + per_page - 1) // per_page)
    else:
        total_pages = 1
        per_page    = total_count  # show everything on one screen

    page      = max(0, min(page, total_pages - 1))
    start     = page * per_page
    page_rows = rows[start: start + per_page]

    # ── Build header text (premium marketplace style) ─────────────────────────
    out_of_stock_count = total_count - available_count
    page_line = ""
    if allow_pag and total_pages > 1:
        page_line = f"\n📄 Page: {page + 1} / {total_pages}"

    text = (
        "🛍️ <b>Products</b>\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"📦 Total Products: <b>{total_count}</b>\n"
        f"🟢 In Stock: <b>{available_count}</b>\n"
        f"🔴 Out of Stock: <b>{out_of_stock_count}</b>"
        f"{page_line}\n\n"
        "Select a product below."
    )

    if notice:
        text += f"\n\n{notice}"

    # ── Render ────────────────────────────────────────────────────────────────
    keyboard = InlineKeyboardMarkup(build_all_products_keyboard(
        page_rows,
        page=page,
        total_pages=total_pages,
        show_refresh=show_refresh,
        allow_pagination=allow_pag,
        show_stock=show_stock,
    ))
    await _safe_edit_catalog(query, text, keyboard)


@perf_track("products_handler")
async def products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all flat-catalog callbacks:
      • 'products'           — initial open (main menu button)
      • 'products_refresh'   — refresh button (page 0)
      • 'products_page_{N}'  — pagination next/prev/refresh at page N

    Category and Subcategory data is untouched and still used by admin tooling.
    """
    query = update.callback_query
    data  = query.data or ""

    is_refresh = data == "products_refresh"
    await query.answer("Updated ✅" if is_refresh else None)

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # Parse page number from products_page_{N}; fall back to 0 for any other
    # variant (bare 'products', 'products_refresh', unknown suffixes).
    page = 0
    if data.startswith("products_page_"):
        try:
            page = int(data[len("products_page_"):])
        except (ValueError, IndexError):
            page = 0

    await render_all_products_catalog(
        query, context, update.effective_user.id, page=page,
    )


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product callback."""
    await product_detail_callback(update, context)


async def subcategory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subcategory selection."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    subcategory_id = int(query.data.split("_")[1])

    # If coming from a photo message (product detail with image), delete and send new message
    if query.message.photo:
        await query.message.delete()
        # Create a new text message for products list
        message = await query.message.reply_text("Loading products...")
        # Now we need to pass this message to show_products_list
        # We'll use a workaround by creating a mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        mock_query = MockQuery(message)
        await show_products_list(mock_query, subcategory_id=subcategory_id, context=context,
                                  telegram_id=update.effective_user.id)
    else:
        await show_products_list(query, subcategory_id=subcategory_id, context=context,
                                  telegram_id=update.effective_user.id)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection - show subcategories or products."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    callback_data = query.data
    category_id = int(callback_data.split("_")[1])

    # If coming from a photo message, delete it and create new text message
    if query.message.photo:
        await query.message.delete()
        message = await query.message.reply_text("Loading...")

        # Create mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        query = MockQuery(message)

    with get_db_session() as session:
        category = session.query(Category).filter_by(id=category_id).first()

        if not category:
            try:
                await query.edit_message_text("❌ Category not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Check if category has subcategories
        subcategories = session.query(Subcategory).filter_by(category_id=category_id).all()

        if subcategories:
            # Show subcategories
            subcat_buttons = [
                [InlineKeyboardButton(subcat.name, callback_data=f"subcategory_{subcat.id}")]
                for subcat in subcategories[:5]
            ]

            # Create keyboard with back to products
            from telegram import InlineKeyboardMarkup
            keyboard = subcat_buttons + [[
                InlineKeyboardButton("🔙 Back", callback_data="back_to_products"),
                InlineKeyboardButton("☎️ Support", callback_data="support_center")
            ]]

            try:
                await query.edit_message_text(
                    f"📦 Select the product you need from {category.name}:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            # Show products directly
            await show_products_list(query, category_id=category_id, context=context,
                                      telegram_id=update.effective_user.id)


async def show_products_list(query, category_id=None, subcategory_id=None, page=0, context=None, telegram_id=None):
    """Show list of products for a category or subcategory."""
    with get_db_session() as session:
        query_filter = Product.is_active == True

        if category_id:
            products = session.query(Product).filter(
                Product.category_id == category_id,
                Product.subcategory_id == None,
                query_filter
            ).all()
        elif subcategory_id:
            products = session.query(Product).filter(
                Product.subcategory_id == subcategory_id,
                query_filter
            ).all()
        else:
            products = session.query(Product).filter(query_filter).all()

        if not products:
            try:
                await query.edit_message_text(
                    "📦 No products available in this category.",
                    reply_markup=create_back_support_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Paginate products
        page_info = paginate_items(products, page, page_size=5)

        # Build the "🆕 New" badge context once for this listing (avoids one
        # DB query per product row) — window is admin-configurable via
        # BotConfig (utils/bot_config.py -> "new_product_days", default 7).
        try:
            from services.badges import build_context
            _badge_ctx = build_context()
        except Exception:
            _badge_ctx = None

        def _is_new(prod):
            if not _badge_ctx or not getattr(prod, "created_at", None):
                return False
            return prod.created_at >= _badge_ctx.new_cutoff

        # Create product buttons — legacy helper kept for backward compat; active
        # path now uses _row_label (see below) which follows the premium format.
        def _button_label(prod):
            new_badge  = "🆕 " if _is_new(prod) else ""
            prod_emoji = (prod.product_emoji or "").strip() or "📦"
            short_name = _shorten_product_name(prod.name or "", budget=24)
            try:
                from services.pricing import get_flash_sale_for_display
                fs = get_flash_sale_for_display(prod.id)
            except Exception:
                fs = None
            if fs:
                s_ind = "❌ Out of Stock" if prod.stock_count <= 0 else f"📦 {prod.stock_count} In Stock"
                return (f"{prod_emoji} {new_badge}🔥 {short_name} • ${fs['sale_price']:.2f}"
                        f" (was {fs['original_price']:.2f}) • {s_ind}")
            s_ind = "❌ Out of Stock" if prod.stock_count <= 0 else f"📦 {prod.stock_count} In Stock"
            return f"{prod_emoji} {new_badge}{short_name} • {_product_price_for_user(prod, telegram_id)} • {s_ind}"

        # Row label format (premium): "{emoji} {short_name} • {price} • 📦 N In Stock"
        def _row_label(prod):
            new_badge = "🆕 " if _is_new(prod) else ""
            prod_emoji = (prod.product_emoji or "").strip() or "📦"
            short_name = _shorten_product_name(prod.name or "", budget=24)
            try:
                from services.pricing import get_flash_sale_for_display
                fs = get_flash_sale_for_display(prod.id)
            except Exception:
                fs = None
            if fs:
                price_part = f"{fs['sale_price']:.2f}"
                stock_indicator = "❌ Out of Stock" if prod.stock_count <= 0 else f"📦 {prod.stock_count} In Stock"
                return f"{prod_emoji} {new_badge}🔥 {short_name} • ${price_part} (was {fs['original_price']:.2f}) • {stock_indicator}"
            price_display = _product_price_for_user(prod, telegram_id)
            stock_indicator = "❌ Out of Stock" if prod.stock_count <= 0 else f"📦 {prod.stock_count} In Stock"
            return f"{prod_emoji} {new_badge}{short_name} • {price_display} • {stock_indicator}"

        product_buttons = [
            [InlineKeyboardButton(_row_label(prod), callback_data=f"product_{prod.id}")]
            for prod in page_info['items']
        ]

        # Add pagination if needed
        from telegram import InlineKeyboardMarkup
        keyboard = product_buttons.copy()
        if page_info['total_pages'] > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"products_page_{page-1}"))
            if page < page_info['total_pages'] - 1:
                pagination_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"products_page_{page+1}"))
            if pagination_row:
                keyboard.append(pagination_row)

        keyboard.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"products_page_{page}"),
        ])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])

        total_count = len(products)
        available_count = sum(1 for p in products if p.stock_count > 0)

        out_of_stock_c = total_count - available_count
        page_line_s = ""
        if page_info['total_pages'] > 1:
            page_line_s = f"\n📄 Page: {page_info['page'] + 1} / {page_info['total_pages']}"
        text = (
            "🛍️ <b>Products</b>\n"
            "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            f"📦 Total Products: <b>{total_count}</b>\n"
            f"🟢 In Stock: <b>{available_count}</b>\n"
            f"🔴 Out of Stock: <b>{out_of_stock_c}</b>"
            f"{page_line_s}\n\n"
            "Select a product below."
        )

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection - show product details."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    product_id = int(query.data.split("_")[1])

    # V23: Record this product view in recently-viewed history (best-effort, non-blocking)
    try:
        from handlers.feature_handlers import track_recently_viewed
        track_recently_viewed(update.effective_user.id, product_id)
    except Exception:
        pass

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Flat catalog: back navigation always returns to the complete
        # all-products list — there is no category/subcategory state.
        back_callback = "back_to_products"

        # Format product details + Section 14 badges
        details = format_product_display(product, include_description=True)
        # V12 (Multi-Currency): replace the USD-only price line with the
        # viewer's preferred-currency price.
        details = details.replace(
            f"💰 <b>Price:</b> {format_price(product.price)}",
            f"💰 <b>Price:</b> {_product_price_for_user(product, update.effective_user.id)}",
        )
        try:
            from services.badges import badge_line
            _bl = badge_line(product)
            if _bl:
                details = f"{_bl}\n\n{details}"
        except Exception:
            pass

        # Social proof: "⭐ 4.8 (120 reviews) • 500+ sold" — cached aggregate
        # from Review + Order/OrderItem (services/social_proof.py).
        try:
            from services.social_proof import get_social_proof
            _sp = get_social_proof(product.id).format()
            if _sp:
                details = f"{details}\n{_sp}"
        except Exception:
            pass

        # Urgency: "⚠️ Only 3 left!" when available stock is at/below the
        # admin-configured low_stock_threshold (default 5). Nothing shown
        # above the threshold.
        try:
            from services.inventory import low_stock_warning
            _lsw = low_stock_warning(product.id)
            if _lsw:
                details = f"{details}\n{_lsw}"
        except Exception:
            pass

        # V15: Flash sale banner + countdown (prepended above everything else
        # so it's the first thing a buyer sees).
        banner = _flash_sale_banner(product.id)
        if banner:
            details = f"{banner}\n{details}"

        # Send product image if available
        _telegram_id = update.effective_user.id
        _stock_count = product.stock_count
        if product.image_path and os.path.exists(product.image_path):
            with open(product.image_path, 'rb') as image:
                await query.message.reply_photo(
                    photo=image,
                    caption=details,
                    reply_markup=create_product_detail_keyboard(
                        product_id,
                        back_callback,
                        telegram_id=_telegram_id,
                        stock_count=_stock_count,
                    ),
                    parse_mode='HTML',
                )
            await query.message.delete()
        else:
            try:
                await query.edit_message_text(
                    details,
                    reply_markup=create_product_detail_keyboard(
                        product_id,
                        back_callback,
                        telegram_id=_telegram_id,
                        stock_count=_stock_count,
                    ),
                    parse_mode='HTML',
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise


async def availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle availability button - show all available products."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    with get_db_session() as session:
        categories = session.query(Category).all()
        products_by_category = {}

        for category in categories:
            products = session.query(Product).filter_by(
                category_id=category.id,
                is_active=True
            ).limit(15).all()

            if products:
                products_by_category[category.name] = products

        if not products_by_category:
            try:
                await query.edit_message_text(
                    "📦 No products available yet.",
                    reply_markup=create_back_support_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        text = build_availability_text(products_by_category)

        try:
            await query.edit_message_text(
                text,
                reply_markup=create_back_support_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def flash_sales_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a live hub of everything currently on flash sale, with countdowns."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    from database import FlashSale
    from services.pricing import flash_sale_price

    now = datetime.utcnow()
    with get_db_session() as session:
        sales = session.query(FlashSale).filter(
            FlashSale.is_active == True,
            FlashSale.start_time <= now,
            FlashSale.end_time > now,
        ).all()

        # Resolve to distinct active products — product-level sales take
        # precedence over a category-level sale hitting the same product.
        product_map = {}
        for fs in sales:
            if fs.product_id:
                p = session.query(Product).filter_by(id=fs.product_id, is_active=True).first()
                if p:
                    product_map[p.id] = (p, fs)
        for fs in sales:
            if fs.category_id:
                for p in session.query(Product).filter_by(category_id=fs.category_id, is_active=True).all():
                    if p.id not in product_map:
                        product_map[p.id] = (p, fs)

        if not product_map:
            try:
                await query.edit_message_text(
                    "🔥 No flash sales are running right now — check back soon!",
                    reply_markup=create_back_support_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        lines = ["🔥 <b>FLASH SALES — Limited Time!</b>\n"]
        buttons = []
        for p, fs in product_map.values():
            base = float(p.price)
            sp = flash_sale_price(base, fs)
            countdown = _format_countdown(fs.end_time)
            lines.append(
                f"• {p.name} — −{fs.discount_percent:.0f}% "
                f"(${base:.2f} → ${sp:.2f})  ⏰ {countdown}"
            )
            buttons.append([InlineKeyboardButton(
                f"🔥 {p.name} — ${sp:.2f}", callback_data=f"product_{p.id}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])

        try:
            try:
                await query.edit_message_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="HTML",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            pass


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support button - show support page."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    with get_db_session() as session:
        store_settings = session.query(Settings).first()

        support_username = store_settings.support_username if store_settings else ""
        channel_username = store_settings.channel_username if store_settings else ""

        message = "☎️ My Shop is Open 24/7"

        try:
            await query.edit_message_text(
                message,
                reply_markup=create_support_keyboard(support_username, channel_username)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


# ── Order History / Order Detail ── shared module-level helpers ───────────────

_OH_STATUS_EMOJI = {
    OrderStatus.PROCESSING: "⏳",
    OrderStatus.COMPLETED:  "✅",
    OrderStatus.CANCELLED:  "❌",
    OrderStatus.FAILED:     "❌",
    OrderStatus.REFUNDED:   "🔄",
}
_OH_STATUS_LABEL = {
    OrderStatus.PROCESSING: "Processing",
    OrderStatus.COMPLETED:  "Completed",
    OrderStatus.CANCELLED:  "Cancelled",
    OrderStatus.FAILED:     "Failed",
    OrderStatus.REFUNDED:   "Refunded",
}
_OH_FIELD_META = [
    ("link",     "🔗", "Link"),
    ("key",      "🔑", "License Key"),
    ("username", "👤", "Username"),
    ("email",    "📧", "Email"),
    ("password", "🔒", "Password"),
]


def _oh_fmt_dt(dt) -> str:
    """'17 Jul 2026' for a datetime, '—' for None."""
    if not dt:
        return "—"
    return f"{dt.day:02d} {dt.strftime('%b')} {dt.year}"


_TG_TEXT_LIMIT = 4096


def _safe_truncate_html(text: str, limit: int = _TG_TEXT_LIMIT) -> str:
    """Trim ``text`` to fit Telegram's message-length limit without breaking
    HTML markup or cutting a tag in half.

    Telegram counts the raw text including entity tags toward the 4096-char
    limit for ``sendMessage``/``editMessageText``. If we naively slice a
    string that contains ``<b>...</b>``/``<code>...</code>`` we can end up
    with an unclosed tag, which Telegram rejects with a *different* error
    (``Can't parse entities``). This walks the string tag-aware, stops
    comfortably before the limit, and closes any tags left open.
    """
    if len(text) <= limit:
        return text

    ellipsis = "\n\n… (truncated)"
    budget = limit - len(ellipsis) - 20  # safety margin for closing tags
    if budget < 0:
        budget = 0

    open_tags: list = []
    out: list = []
    i = 0
    n = len(text)
    while i < n and len(out) < budget:
        ch = text[i]
        if ch == "<":
            end = text.find(">", i)
            if end == -1:
                break
            tag = text[i:end + 1]
            tag_name = tag.strip("<>/").split()[0].lower() if tag.strip("<>/") else ""
            if tag.startswith("</"):
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
            elif not tag.endswith("/>"):
                open_tags.append(tag_name)
            out.append(tag)
            i = end + 1
        else:
            out.append(ch)
            i += 1

    result = "".join(out) + ellipsis
    for tag_name in reversed(open_tags):
        result += f"</{tag_name}>"
    return result


def _parse_delivery_content(raw: str) -> dict:
    """Parse a delivered_asset string into typed named fields.

    Returns any subset of: link, key, username, email, password, custom (list).
    """
    if not raw:
        return {}
    text = raw.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}

    # Single URL → activation link
    if len(lines) == 1 and text.startswith(("http://", "https://")):
        return {"link": text}

    # Single non-URL → product key / license
    if len(lines) == 1:
        return {"key": text}

    # Multi-line → try K: V pairs
    result: dict = {}
    custom: list = []
    for line in lines:
        if ":" not in line:
            custom.append(line)
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        k_l = k.strip().lower()
        if not v:
            custom.append(line)
            continue
        if k_l in ("username", "user", "login", "account", "id"):
            result["username"] = v
        elif k_l in ("password", "pass", "pwd"):
            result["password"] = v
        elif k_l in ("email", "mail", "e-mail"):
            result["email"] = v
        elif k_l in ("key", "license", "license key", "activation key",
                     "serial", "serial key", "code", "product key"):
            result["key"] = v
        elif k_l in ("link", "url", "activation link", "download",
                     "download link", "download url"):
            result["link"] = v
        else:
            custom.append(line)
    if custom:
        result["custom"] = custom
    return result


async def _do_render_order_detail(query, context, order_id: int, telegram_id: int) -> None:
    """Core renderer for the Order Detail view.

    Called by ``user_order_detail_callback`` and ``oh_toggle_callback`` so both
    always produce the same, always-fresh view without duplicating code.
    """
    revealed: set = context.user_data.get(f"order_revealed_{order_id}", set())

    enable_timeline  = cfg.get_bool("order_history_enable_timeline",         True)
    enable_receipt   = cfg.get_bool("order_history_enable_receipt",           True)
    enable_buy_again = cfg.get_bool("order_history_enable_buy_again",         True)
    enable_copy      = cfg.get_bool("order_history_enable_copy_buttons",      True)
    enable_masking   = cfg.get_bool("order_history_enable_security_masking",  True)
    enable_review    = cfg.get_bool("order_history_enable_review",           False)
    enable_dispute   = cfg.get_bool("order_history_enable_dispute",          False)

    # ── Load ────────────────────────────────────────────────────────────────
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        order = session.query(Order).options(
            selectinload(Order.order_items).joinedload(OrderItem.product)
        ).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            try:
                await query.edit_message_text("❌ Order not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        o_status      = order.status
        o_dispute     = order.dispute_status
        o_total       = order.total_amount
        o_created     = order.created_at
        o_completed   = order.completed_at
        o_del_status  = order.delivery_status

        items_snap: list = []
        for item in order.order_items:
            pname = item.product.name if item.product else f"Product #{item.product_id}"
            items_snap.append({
                "product_id":      item.product_id,
                "product_name":    pname,
                "quantity":        item.quantity,
                "price":           item.price,
                "delivered_asset": item.delivered_asset,
            })

        dr_snap: dict | None = None
        try:
            from database.models import DeliveryRecord as _DR
            dr = (session.query(_DR)
                  .filter_by(order_id=order_id)
                  .order_by(_DR.created_at.desc())
                  .first())
            if dr:
                dr_snap = {
                    "delivery_type":   dr.delivery_type,
                    "delivery_method": dr.delivery_method,
                    "status":          dr.status,
                    "delivered_at":    dr.delivered_at,
                }
        except Exception:
            pass

        # Download Center records for this order — surfaced here so Downloads
        # stay reachable from Order Details instead of a separate hub page.
        download_ids: list = []
        try:
            from database.models import UserDownload as _UD
            download_ids = [
                d.id for d in session.query(_UD).filter_by(order_id=order_id).all()
            ]
        except Exception:
            pass

        review_eligible: list = []
        try:
            from database import Review as _Rev
            seen_pids: set = set()
            for item in order.order_items:
                if item.product_id in seen_pids:
                    continue
                seen_pids.add(item.product_id)
                already = session.query(_Rev).filter_by(
                    user_id=user.id,
                    product_id=item.product_id,
                    order_id=order.id,
                ).first()
                if not already:
                    pn = item.product.name if item.product else f"#{item.product_id}"
                    review_eligible.append((item.product_id, pn, order.id))
        except Exception:
            pass

        # Receipt number — captured inside the session block so it's a plain str
        receipt_number = ""
        try:
            from database.models import OrderReceipt as _ORec
            _or = session.query(_ORec).filter_by(order_id=order_id).first()
            if _or and _or.receipt_number:
                receipt_number = _or.receipt_number
        except Exception:
            pass

    # ── Build message text ───────────────────────────────────────────────────
    import html as _html

    s_emoji = _OH_STATUS_EMOJI.get(o_status, "❓")
    s_label = _OH_STATUS_LABEL.get(o_status,
                                    o_status.value if o_status else "—")

    dispute_note = ""
    if o_dispute == DisputeStatus.OPENED:
        dispute_note = "  🚨 Dispute: OPEN — Under admin review"
    elif o_dispute == DisputeStatus.RESOLVED:
        dispute_note = "  ✔️ Dispute: Resolved"

    from utils.helpers import format_order_id as _fmt_oid
    _display_order_id = _fmt_oid(order_id, o_created)
    lines: list = [f"🧾 <b>Order ID</b>\n{_display_order_id}"]
    lines.append(f"{s_emoji} <b>{s_label}</b>{dispute_note}")

    # Product(s)
    if len(items_snap) == 1:
        lines.append(f"🎁 {_html.escape(items_snap[0]['product_name'])}")
        lines.append(f"📦 Qty: {items_snap[0]['quantity']}")
    else:
        lines.append("🎁 <b>Products</b>")
        for it in items_snap:
            lines.append(
                f"• {_html.escape(it['product_name'])} ×{it['quantity']}"
                f" — {format_price(it['price'] * it['quantity'])}"
            )

    lines.append(f"💰 <code>{format_price(o_total)}</code>")
    lines.append(f"📅 {_oh_fmt_dt(o_created)}")
    lines.append("💳 Wallet")

    # ── Delivered Product / License Key section (hidden if no data) ─────────
    all_content: dict = {}
    for it in items_snap:
        if it["delivered_asset"]:
            parsed = _parse_delivery_content(it["delivered_asset"])
            for k, v in parsed.items():
                if k == "custom":
                    all_content.setdefault("custom", []).extend(
                        v if isinstance(v, list) else [v]
                    )
                else:
                    all_content[k] = v

    if all_content:
        lines.append("🔑 <b>Delivered Product</b>")
        for field, icon, label in _OH_FIELD_META:
            if field not in all_content:
                continue
            val = all_content[field]
            if field == "password" and enable_masking and field not in revealed:
                display_val = "•" * min(len(val), 12)
            else:
                display_val = _html.escape(val)
            lines.append(f"{icon} {label}: <code>{display_val}</code>")
        if "custom" in all_content:
            customs = all_content["custom"]
            for cline in (customs if isinstance(customs, list) else [customs]):
                lines.append(_html.escape(cline))

    # ── Order Timeline (only for orders still in-flight, never completed) ───
    # Keep a copy of the lines *before* the timeline is appended so we can
    # drop the timeline first if the full message ends up too long for
    # Telegram (it has the least essential info and is often the culprit —
    # e.g. many status transitions on an old order).
    _lines_before_timeline = list(lines)

    if enable_timeline and o_status == OrderStatus.PROCESSING:
        try:
            from services.order_lifecycle import render_timeline
            import re as _re
            tl = render_timeline(order_id, limit=10)
            if tl and tl != "— no history yet —":
                lines.append("")
                lines.append("📜 <b>Timeline</b>")
                for ln in tl.splitlines():
                    lines.append(_html.escape(_re.sub(r"\s*\[[^\]]+\]\s*", "  ", ln)))
        except Exception:
            pass

    message = "\n".join(lines).strip()

    # ── Length guard ─────────────────────────────────────────────────────────
    # Telegram rejects sendMessage/editMessageText with BadRequest
    # ("Message_too_long") once the text exceeds 4096 characters. Orders with
    # many line items, a long custom delivery payload, or a deep timeline can
    # blow past that. First try dropping the timeline (least essential,
    # often the biggest single contributor); if it's still too long, fall
    # back to a tag-safe hard truncation so we never crash the handler.
    if len(message) > _TG_TEXT_LIMIT:
        message = "\n".join(_lines_before_timeline).strip()
        if len(message) > _TG_TEXT_LIMIT:
            message = _safe_truncate_html(message)

    # ── Build keyboard ────────────────────────────────────────────────────────
    # Simplified, premium-marketplace button set. Only the core five actions
    # (Copy Product, Receipt, Buy Again, Support, Back) show by default; the
    # legacy per-field copy buttons, timeline, review and dispute buttons are
    # collapsed into a single "Copy Product" action and stay hidden unless
    # explicitly turned on from the Admin Panel.
    keyboard: list = []

    # Copy Product + Show/Hide password (same row when both apply)
    row: list = []
    if enable_copy and all_content:
        row.append(InlineKeyboardButton(
            "📋 Copy Product", callback_data=f"oh_copy_{order_id}_product"))
    if "password" in all_content and enable_masking:
        toggle_label = ("🙈 Hide" if "password" in revealed else "👁 Show Password")
        row.append(InlineKeyboardButton(
            toggle_label, callback_data=f"oh_toggle_{order_id}_password"))
    if row:
        keyboard.append(row)

    # Receipt
    if o_status == OrderStatus.COMPLETED and enable_receipt:
        keyboard.append([InlineKeyboardButton(
            "🧾 Receipt", callback_data=f"receipt_{order_id}")])

    # Downloads — only shown when this order actually has download-center
    # records (e.g. file-based products) and the feature is enabled; links
    # straight to the item when there's exactly one, otherwise to the
    # filtered list.
    if download_ids and cfg.get_bool("feature_download_center_enabled", True):
        dl_cb = f"ua:dl:v:{download_ids[0]}" if len(download_ids) == 1 else "ua:dl"
        keyboard.append([InlineKeyboardButton(
            "📁 Downloads", callback_data=dl_cb)])

    # Timeline — only for in-flight orders, and only if the admin has it on
    if enable_timeline and o_status == OrderStatus.PROCESSING:
        try:
            from services.order_timeline import show_to_users as _ots_show
            if _ots_show():
                keyboard.append([InlineKeyboardButton(
                    "📜 Timeline", callback_data=f"user_timeline_{order_id}")])
        except Exception:
            pass

    # Buy Again
    if enable_buy_again and o_status == OrderStatus.COMPLETED and items_snap:
        keyboard.append([InlineKeyboardButton(
            "🔄 Buy Again",
            callback_data=f"product_{items_snap[0]['product_id']}")])

    # Review — admin-gated, off by default
    if enable_review:
        for rev_pid, rev_pname, rev_oid in review_eligible:
            display = rev_pname[:25] if len(rev_pname) > 25 else rev_pname
            keyboard.append([InlineKeyboardButton(
                f"⭐ Review: {display}",
                callback_data=f"review_start_{rev_oid}_{rev_pid}")])

    # Open Dispute — admin-gated, off by default
    if enable_dispute and o_dispute == DisputeStatus.NIL:
        keyboard.append([InlineKeyboardButton(
            "🚨 Open Dispute", callback_data=f"open_dispute_{order_id}")])

    # Support
    keyboard.append([InlineKeyboardButton(
        "🎧 Support", callback_data="support_center")])

    # Back
    keyboard.append([InlineKeyboardButton(
        "⬅ Back", callback_data="order_history")])

    try:
        await query.edit_message_text(
            message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        elif "Message_too_long" in str(e) or "message is too long" in str(e).lower():
            # Belt-and-braces: the pre-send length guard should already have
            # caught this, but fall back to a harder truncation rather than
            # crashing the handler if it somehow didn't.
            await query.edit_message_text(
                _safe_truncate_html(message, limit=3500),
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        else:
            raise


async def order_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paginated order history with rich multi-line order summaries.

    Callback patterns handled (all caught by the existing ``^order_history``
    registration in bot.py):
      • order_history           — initial open / back-to-orders
      • order_history_page_{N}  — paginated navigation
    """
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # ── Feature gate ──────────────────────────────────────────────────────────
    status = cfg.get("order_history_status", "enabled")
    if status == "disabled":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
        ]])
        try:
            await query.edit_message_text(
                pui.build_card(title="My Orders", title_emoji="📋", fields=[],
                                note="🔴 Order history is currently unavailable."),
                reply_markup=kb,
                parse_mode='HTML',
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return
    if status == "maintenance":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Try Again", callback_data="order_history"),
            InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
        ]])
        try:
            await query.edit_message_text(
                pui.build_card(title="My Orders", title_emoji="📋", fields=[],
                                note="🟡 Order history is temporarily under maintenance.\nPlease check back shortly."),
                reply_markup=kb,
                parse_mode='HTML',
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # ── Parse page ────────────────────────────────────────────────────────────
    page = 0
    if data.startswith("order_history_page_"):
        try:
            page = int(data[len("order_history_page_"):])
        except (ValueError, IndexError):
            page = 0

    per_page = max(3, min(20, cfg.get_int("orders_per_page", 10)))
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        total = session.query(Order).filter_by(user_id=user.id).count()

        if total == 0:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
            ]])
            try:
                await query.edit_message_text(
                    pui.build_card(title="My Orders", title_emoji="📋", fields=[],
                                    note="📭 You have no orders yet."),
                    reply_markup=kb,
                    parse_mode='HTML',
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))

        orders = (
            session.query(Order)
            .options(
                selectinload(Order.order_items).joinedload(OrderItem.product)
            )
            .filter_by(user_id=user.id)
            .order_by(Order.created_at.desc())
            .offset(page * per_page)
            .limit(per_page)
            .all()
        )

        # ── Build message text ────────────────────────────────────────────────
        range_start = page * per_page + 1
        range_end   = page * per_page + len(orders)

        message = (
            "📦 <b>My Orders</b>\n\n"
            f"Showing {range_start}–{range_end} of {total} Orders\n\n"
            "Select an order below to view its details."
        )

        keyboard = []

        for order in orders:
            from utils.helpers import format_order_id as _fmt_oid_list
            _disp = _fmt_oid_list(order.id, order.created_at)
            keyboard.append([InlineKeyboardButton(
                f"📄 {_disp}",
                callback_data=f"user_order_detail_{order.id}",
            )])

        # ── Pagination ────────────────────────────────────────────────────────
        if total_pages > 1:
            nav: list = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=f"order_history_page_{page - 1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(
                    "➡️ Next",
                    callback_data=f"order_history_page_{page + 1}"))
            if nav:
                keyboard.append(nav)

        keyboard.append([InlineKeyboardButton(
            "⬅️ Back to Menu", callback_data="main_menu")])

        try:
            await query.edit_message_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def user_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the modern Order Detail view. Delegates all rendering to
    ``_do_render_order_detail`` so the show/hide toggle produces the identical
    layout without duplicating code."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned from using this bot.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # ── Feature gate ──────────────────────────────────────────────────────────
    status = cfg.get("order_history_status", "enabled")
    if status == "disabled":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅ Back to Orders", callback_data="order_history"),
        ]])
        try:
            await query.edit_message_text(
                "📋 Order Details\n\n🔴 Order details are currently unavailable.",
                reply_markup=kb,
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return
    if status == "maintenance":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅ Back to Orders", callback_data="order_history"),
        ]])
        try:
            await query.edit_message_text(
                "📋 Order Details\n\n🟡 Order details are temporarily under maintenance.",
                reply_markup=kb,
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    order_id = int(query.data.split("_")[3])
    await _do_render_order_detail(
        query, context, order_id, update.effective_user.id)


async def oh_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle show / hide for a sensitive field (e.g. password) in Order Detail.

    Callback format: ``oh_toggle_{order_id}_{field}``
    """
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        return

    parts = (query.data or "").split("_", 3)
    if len(parts) < 4:
        return
    try:
        order_id = int(parts[2])
    except ValueError:
        return
    field = parts[3]

    revealed: set = context.user_data.get(f"order_revealed_{order_id}", set())
    if field in revealed:
        revealed.discard(field)
    else:
        revealed.add(field)
    context.user_data[f"order_revealed_{order_id}"] = revealed

    await _do_render_order_detail(
        query, context, order_id, update.effective_user.id)


async def oh_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a delivered field's value in a Telegram popup for easy copying.

    Callback format: ``oh_copy_{order_id}_{field}``
    Short values (≤195 chars) appear in a dismissible alert; longer values
    are sent as a private message so nothing is truncated.
    """
    query = update.callback_query

    if check_user_banned(update.effective_user.id):
        await query.answer()
        return

    parts = (query.data or "").split("_", 3)
    if len(parts) < 4:
        await query.answer("⚠️ Invalid request.", show_alert=True)
        return
    try:
        order_id = int(parts[2])
    except ValueError:
        await query.answer("⚠️ Invalid order.", show_alert=True)
        return
    field = parts[3]

    with get_db_session() as session:
        user = session.query(User).filter_by(
            telegram_id=update.effective_user.id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return
        order = session.query(Order).options(
            selectinload(Order.order_items)
        ).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            await query.answer("❌ Order not found.", show_alert=True)
            return
        order_created_at = order.created_at
        assets = [it.delivered_asset for it in order.order_items
                  if it.delivered_asset]

        # Receipt number copy — fetched inside the session block
        _receipt_num_for_copy: str | None = None
        if field == "receipt":
            try:
                from database.models import OrderReceipt as _ORec2
                _or2 = session.query(_ORec2).filter_by(order_id=order_id).first()
                if _or2:
                    _receipt_num_for_copy = _or2.receipt_number
            except Exception:
                pass

    # Receipt field — handled separately (not in delivered_asset)
    if field == "receipt":
        if _receipt_num_for_copy:
            await query.answer(_receipt_num_for_copy, show_alert=True)
        else:
            await query.answer("⚠️ Receipt not found.", show_alert=True)
        return

    # Consolidated "Copy Product" — combines every delivered field into one
    # value instead of requiring a separate button per field.
    if field == "product":
        enable_masking = cfg.get_bool("order_history_enable_security_masking", True)
        revealed: set = context.user_data.get(f"order_revealed_{order_id}", set())
        all_content: dict = {}
        for raw in assets:
            parsed = _parse_delivery_content(raw)
            for k, v in parsed.items():
                if k == "custom":
                    all_content.setdefault("custom", []).extend(
                        v if isinstance(v, list) else [v])
                else:
                    all_content[k] = v

        if not all_content:
            await query.answer("⚠️ This field is not available.", show_alert=True)
            return

        combo_lines: list = []
        for f, _icon, label in _OH_FIELD_META:
            if f not in all_content:
                continue
            v = all_content[f]
            if f == "password" and enable_masking and f not in revealed:
                v = "•" * min(len(v), 12)
            combo_lines.append(f"{label}: {v}")
        if "custom" in all_content:
            customs = all_content["custom"]
            combo_lines.extend(customs if isinstance(customs, list) else [customs])
        value = "\n".join(combo_lines)

        if len(value) <= 195:
            await query.answer(value, show_alert=True)
        else:
            await query.answer("✅ Content sent to your chat.", show_alert=False)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"📋 *Order {_fmt_oid(order_id, order_created_at)}*\n\n`{value}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return

    value: str | None = None
    for raw in assets:
        parsed = _parse_delivery_content(raw)
        if field in parsed:
            v = parsed[field]
            value = "\n".join(v) if isinstance(v, list) else str(v)
            break

    if not value:
        await query.answer("⚠️ This field is not available.", show_alert=True)
        return

    if len(value) <= 195:
        await query.answer(value, show_alert=True)
    else:
        await query.answer("✅ Content sent to your chat.", show_alert=False)
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"📋 *Order {_fmt_oid(order_id, order_created_at)}*\n\n`{value}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def download_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate & send a PDF receipt for an order."""
    query = update.callback_query
    await query.answer("Generating receipt…")

    if check_user_banned(update.effective_user.id):
        return

    try:
        order_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return

    telegram_id = update.effective_user.id
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        order = session.query(Order).filter_by(id=order_id).first()
        if not user or not order or order.user_id != user.id:
            await query.message.reply_text("❌ Order not found.")
            return
        order_created_at = order.created_at

    try:
        from utils import generate_receipt_pdf
        path = generate_receipt_pdf(order_id)
        with open(path, "rb") as fh:
            await query.message.reply_document(
                document=fh,
                filename=f"receipt_{_fmt_oid(order_id, order_created_at)}.pdf",
                caption=f"📄 Receipt for order {_fmt_oid(order_id, order_created_at)}",
            )
        try:
            os.remove(path)
        except OSError:
            pass
    except Exception as e:
        await query.message.reply_text(f"❌ Failed to generate receipt: {e}")


async def back_to_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle "↩️ Back to Products" — reloads the complete flat catalog."""
    # Just redirect to products_callback
    await products_callback(update, context)
