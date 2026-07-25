"""User-facing handlers for all six V18 features:
❤️ Wishlist · 🔔 Price Drop Alerts · 🕒 Recently Viewed
⚡ Quick Buy · ⭐ Preferred Payment · 🔁 Buy Again

Callback namespace:  uf:*

Every handler checks its feature flag first and silently ignores requests
when the feature is disabled — no visible error, no data deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session, User, Product
from database.models import (
    UserWishlist, PriceDropAlert, RecentlyViewed,
    QuickBuyConfig, PreferredPayment,
    Order, OrderItem, OrderStatus,
)
from utils import check_user_banned, format_price
from utils.bot_config import cfg

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────

def _feat(key: str, default: bool = True) -> bool:
    return cfg.get_bool(key, default)


def _fmt_price(p: float) -> str:
    return f"${p:.2f}"


def _safe_product_name(product_id: int) -> str:
    try:
        with get_db_session() as s:
            p = s.query(Product).filter_by(id=product_id).first()
            return p.name if p else f"Product #{product_id}"
    except Exception:
        return f"Product #{product_id}"


async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")
    ]])


# ─────────────────────────────────────────────────────────────────────────
# Recently Viewed — tracking helper (called from payment_handlers)
# ─────────────────────────────────────────────────────────────────────────

def track_recently_viewed(telegram_id: int, product_id: int) -> None:
    """Upsert a recently-viewed record.  Non-blocking best-effort."""
    if not _feat("feature_recently_viewed_enabled"):
        return
    try:
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=telegram_id).first()
            if not user:
                return

            max_items = cfg.get_int("feature_recently_viewed_max", 20)

            existing = (
                s.query(RecentlyViewed)
                .filter_by(user_id=user.id, product_id=product_id)
                .first()
            )
            if existing:
                existing.viewed_at = datetime.utcnow()
            else:
                # Enforce limit — evict oldest if needed
                if max_items > 0:
                    count = (
                        s.query(RecentlyViewed)
                        .filter_by(user_id=user.id)
                        .count()
                    )
                    if count >= max_items:
                        oldest = (
                            s.query(RecentlyViewed)
                            .filter_by(user_id=user.id)
                            .order_by(RecentlyViewed.viewed_at.asc())
                            .first()
                        )
                        if oldest:
                            s.delete(oldest)
                s.add(RecentlyViewed(
                    user_id=user.id,
                    product_id=product_id,
                    viewed_at=datetime.utcnow(),
                ))
    except Exception:
        logger.debug("track_recently_viewed failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────
# Quick Buy — save config after a confirmed purchase
# ─────────────────────────────────────────────────────────────────────────

def save_quick_buy_config(
    telegram_id: int,
    product_id: int,
    payment_method: Optional[str],
    quantity: int,
) -> None:
    """Upsert quick-buy config for this (user, product) pair."""
    if not _feat("feature_quick_buy_enabled"):
        return
    try:
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=telegram_id).first()
            if not user:
                return

            max_products = cfg.get_int("feature_quick_buy_max", 10)

            existing = (
                s.query(QuickBuyConfig)
                .filter_by(user_id=user.id, product_id=product_id)
                .first()
            )
            if existing:
                existing.payment_method = payment_method
                existing.quantity = quantity
                existing.last_used_at = datetime.utcnow()
            else:
                if max_products > 0:
                    count = (
                        s.query(QuickBuyConfig)
                        .filter_by(user_id=user.id)
                        .count()
                    )
                    if count >= max_products:
                        oldest = (
                            s.query(QuickBuyConfig)
                            .filter_by(user_id=user.id)
                            .order_by(QuickBuyConfig.last_used_at.asc())
                            .first()
                        )
                        if oldest:
                            s.delete(oldest)
                s.add(QuickBuyConfig(
                    user_id=user.id,
                    product_id=product_id,
                    payment_method=payment_method,
                    quantity=quantity,
                    last_used_at=datetime.utcnow(),
                ))
    except Exception:
        logger.debug("save_quick_buy_config failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────
# Product-level feature buttons (injected into product detail/buy screen)
# ─────────────────────────────────────────────────────────────────────────

def build_product_feature_buttons(
    telegram_id: int,
    product_id: int,
) -> List[List[InlineKeyboardButton]]:
    """Return a list of keyboard rows to add below a product's action buttons.

    Respects feature flags — returns [] when all features are disabled.
    Called from payment_handlers.buy_product_start and user_handlers.
    """
    rows: List[List[InlineKeyboardButton]] = []

    wishlist_on = _feat("feature_wishlist_enabled")
    alerts_on = _feat("feature_price_alerts_enabled")

    if not wishlist_on and not alerts_on:
        return rows

    try:
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=telegram_id).first()
            if not user:
                return rows

            if wishlist_on:
                in_wl = bool(
                    s.query(UserWishlist)
                    .filter_by(user_id=user.id, product_id=product_id)
                    .first()
                )
                wl_label = "💔 Remove from Wishlist" if in_wl else "❤️ Add to Wishlist"
                wl_cb = f"uf:wl:r:{product_id}" if in_wl else f"uf:wl:a:{product_id}"
                rows.append([InlineKeyboardButton(wl_label, callback_data=wl_cb)])

            if alerts_on:
                subscribed = bool(
                    s.query(PriceDropAlert)
                    .filter_by(user_id=user.id, product_id=product_id)
                    .first()
                )
                al_label = "🔕 Unsubscribe Alert" if subscribed else "🔔 Price Drop Alert"
                al_cb = f"uf:pa:u:{product_id}" if subscribed else f"uf:pa:s:{product_id}"
                rows.append([InlineKeyboardButton(al_label, callback_data=al_cb)])
    except Exception:
        logger.debug("build_product_feature_buttons failed", exc_info=True)

    return rows


def get_preferred_payment(telegram_id: int) -> Optional[str]:
    """Return the user's preferred payment method key, or None."""
    if not _feat("feature_preferred_payment_enabled"):
        return None
    try:
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=telegram_id).first()
            if not user:
                return None
            pp = s.query(PreferredPayment).filter_by(user_id=user.id).first()
            return pp.payment_method if pp else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────
# ❤️  WISHLIST
# ─────────────────────────────────────────────────────────────────────────

async def wishlist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's wishlist."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        await _safe_edit(query, "⛔ You have been banned.", _back_main_kb())
        return

    if not _feat("feature_wishlist_enabled"):
        await _safe_edit(query, "❤️ Wishlist is currently disabled.", _back_main_kb())
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_main_kb())
            return

        items = (
            s.query(UserWishlist, Product)
            .join(Product, UserWishlist.product_id == Product.id)
            .filter(UserWishlist.user_id == user.id)
            .order_by(UserWishlist.created_at.desc())
            .all()
        )

    show_counter = _feat("feature_wishlist_counter")
    count = len(items)
    counter_str = f" ({count})" if show_counter and count else ""
    header = f"❤️ <b>My Wishlist{counter_str}</b>\n"

    if not items:
        text = header + "\nYour wishlist is empty.\n\nBrowse products and tap ❤️ Add to Wishlist."
        kb = [[InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
              [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    text = header + "\n"
    kb: List[List[InlineKeyboardButton]] = []
    for wl, product in items:
        avail = "✅" if product.is_active else "❌"
        price_str = _fmt_price(product.price) if product.is_active else "unavailable"
        text += f"{avail} <b>{product.name}</b> — {price_str}\n"
        row = []
        if product.is_active:
            row.append(InlineKeyboardButton(
                f"🛒 Buy", callback_data=f"buy_{product.id}"
            ))
        row.append(InlineKeyboardButton(
            f"💔 Remove", callback_data=f"uf:wl:r:{product.id}"
        ))
        kb.append(row)

    kb.append([InlineKeyboardButton("🛍 Browse Products", callback_data="products")])
    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def wishlist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a product to the wishlist (callback: uf:wl:a:<product_id>)."""
    query = update.callback_query

    if not _feat("feature_wishlist_enabled"):
        await query.answer("❤️ Wishlist is currently disabled.", show_alert=False)
        return

    if check_user_banned(update.effective_user.id):
        await query.answer("⛔ Banned.", show_alert=True)
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        product = s.query(Product).filter_by(id=product_id).first()
        if not product:
            await query.answer("❌ Product not found.", show_alert=True)
            return

        existing = (
            s.query(UserWishlist)
            .filter_by(user_id=user.id, product_id=product_id)
            .first()
        )
        if existing:
            await query.answer("❤️ Already in your wishlist!", show_alert=False)
            return

        # Enforce max
        max_items = cfg.get_int("feature_wishlist_max", 50)
        if max_items > 0:
            count = s.query(UserWishlist).filter_by(user_id=user.id).count()
            if count >= max_items:
                await query.answer(
                    f"❤️ Wishlist full ({max_items} items max). Remove something first.",
                    show_alert=True,
                )
                return

        s.add(UserWishlist(user_id=user.id, product_id=product_id))

    await query.answer(f"❤️ Added to Wishlist!", show_alert=False)

    # Refresh the button on-screen to show "Remove" state
    try:
        kb = query.message.reply_markup
        if kb:
            new_rows = []
            for row in kb.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data == f"uf:wl:a:{product_id}":
                        new_row.append(InlineKeyboardButton(
                            "💔 Remove from Wishlist",
                            callback_data=f"uf:wl:r:{product_id}"
                        ))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
    except Exception:
        pass


async def wishlist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a product from the wishlist (callback: uf:wl:r:<product_id>)."""
    query = update.callback_query

    if not _feat("feature_wishlist_enabled"):
        await query.answer("❤️ Wishlist is currently disabled.", show_alert=False)
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        row = (
            s.query(UserWishlist)
            .filter_by(user_id=user.id, product_id=product_id)
            .first()
        )
        if row:
            s.delete(row)

    await query.answer("💔 Removed from Wishlist.", show_alert=False)

    # If we're on the wishlist page, refresh it
    if query.data.startswith("uf:wl:r:") and query.message:
        try:
            kb = query.message.reply_markup
            if kb:
                new_rows = []
                for row in kb.inline_keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == f"uf:wl:r:{product_id}":
                            new_row.append(InlineKeyboardButton(
                                "❤️ Add to Wishlist",
                                callback_data=f"uf:wl:a:{product_id}"
                            ))
                        else:
                            new_row.append(btn)
                    new_rows.append(new_row)
                await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# 🔔  PRICE DROP ALERTS
# ─────────────────────────────────────────────────────────────────────────

async def price_alerts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's price-drop alert subscriptions."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        await _safe_edit(query, "⛔ You have been banned.", _back_main_kb())
        return

    if not _feat("feature_price_alerts_enabled"):
        await _safe_edit(query, "🔔 Price Drop Alerts are currently disabled.", _back_main_kb())
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_main_kb())
            return

        items = (
            s.query(PriceDropAlert, Product)
            .join(Product, PriceDropAlert.product_id == Product.id)
            .filter(PriceDropAlert.user_id == user.id)
            .order_by(PriceDropAlert.subscribed_at.desc())
            .all()
        )

    if not items:
        text = (
            "🔔 <b>Price Drop Alerts</b>\n\n"
            "You have no active price alerts.\n\n"
            "Browse products and tap 🔔 Price Drop Alert to subscribe."
        )
        kb = [
            [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    text = f"🔔 <b>Price Drop Alerts</b> ({len(items)} subscriptions)\n\n"
    kb: List[List[InlineKeyboardButton]] = []
    for alert, product in items:
        status = "✅" if product.is_active else "❌"
        text += f"{status} <b>{product.name}</b> — {_fmt_price(product.price)}\n"
        kb.append([
            InlineKeyboardButton(f"🛒 Buy Now", callback_data=f"buy_{product.id}"),
            InlineKeyboardButton("🔕 Unsubscribe", callback_data=f"uf:pa:u:{product.id}"),
        ])

    kb.append([InlineKeyboardButton("🛍 Browse Products", callback_data="products")])
    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def price_alert_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Subscribe to price-drop alert (callback: uf:pa:s:<product_id>)."""
    query = update.callback_query

    if not _feat("feature_price_alerts_enabled"):
        await query.answer("🔔 Price Drop Alerts are currently disabled.", show_alert=False)
        return

    if check_user_banned(update.effective_user.id):
        await query.answer("⛔ Banned.", show_alert=True)
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        product = s.query(Product).filter_by(id=product_id).first()
        if not product:
            await query.answer("❌ Product not found.", show_alert=True)
            return

        existing = (
            s.query(PriceDropAlert)
            .filter_by(user_id=user.id, product_id=product_id)
            .first()
        )
        if existing:
            await query.answer("🔔 Already subscribed!", show_alert=False)
            return

        s.add(PriceDropAlert(
            user_id=user.id,
            product_id=product_id,
            last_notified_price=product.price,
        ))

    await query.answer("🔔 Subscribed! We'll notify you if the price drops.", show_alert=False)

    # Update button on-screen
    try:
        kb = query.message.reply_markup
        if kb:
            new_rows = []
            for row in kb.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data == f"uf:pa:s:{product_id}":
                        new_row.append(InlineKeyboardButton(
                            "🔕 Unsubscribe Alert",
                            callback_data=f"uf:pa:u:{product_id}"
                        ))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
    except Exception:
        pass


async def price_alert_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unsubscribe from price-drop alert (callback: uf:pa:u:<product_id>)."""
    query = update.callback_query

    if not _feat("feature_price_alerts_enabled"):
        await query.answer("🔔 Price Drop Alerts are currently disabled.", show_alert=False)
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        row = (
            s.query(PriceDropAlert)
            .filter_by(user_id=user.id, product_id=product_id)
            .first()
        )
        if row:
            s.delete(row)

    await query.answer("🔕 Unsubscribed from price alerts.", show_alert=False)

    # Update button on-screen
    try:
        kb = query.message.reply_markup
        if kb:
            new_rows = []
            for row in kb.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data == f"uf:pa:u:{product_id}":
                        new_row.append(InlineKeyboardButton(
                            "🔔 Price Drop Alert",
                            callback_data=f"uf:pa:s:{product_id}"
                        ))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# 🕒  RECENTLY VIEWED
# ─────────────────────────────────────────────────────────────────────────

async def recently_viewed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for 🕒 Recently Viewed (uf:rv). Delegates to V23 full handler."""
    from handlers.recently_viewed_handlers import my_recently_viewed_menu
    await my_recently_viewed_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# ⚡  QUICK BUY
# ─────────────────────────────────────────────────────────────────────────

async def quick_buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show remembered quick-buy products."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        await _safe_edit(query, "⛔ You have been banned.", _back_main_kb())
        return

    if not _feat("feature_quick_buy_enabled"):
        await _safe_edit(query, "⚡ Quick Buy is currently disabled.", _back_main_kb())
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_main_kb())
            return

        items = (
            s.query(QuickBuyConfig, Product)
            .join(Product, QuickBuyConfig.product_id == Product.id)
            .filter(QuickBuyConfig.user_id == user.id, Product.is_active == True)  # noqa
            .order_by(QuickBuyConfig.last_used_at.desc())
            .all()
        )

    if not items:
        text = (
            "⚡ <b>Quick Buy</b>\n\n"
            "No remembered purchases yet.\n\n"
            "After your first purchase, Quick Buy will remember your "
            "payment method and quantity for one-click checkout."
        )
        kb = [
            [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    text = "⚡ <b>Quick Buy</b>\n\nRepeat your last order instantly:\n\n"
    kb: List[List[InlineKeyboardButton]] = []
    for qbc, product in items:
        pm_label = qbc.payment_method or "Wallet"
        qty = qbc.quantity or 1
        total = product.price * qty
        text += (
            f"• <b>{product.name}</b>\n"
            f"  {qty}x × {_fmt_price(product.price)} = {_fmt_price(total)}"
            f"  via {pm_label}\n"
        )
        kb.append([InlineKeyboardButton(
            f"⚡ {product.name[:20]} ({qty}x via {pm_label[:12]})",
            callback_data=f"uf:qb:b:{product.id}",
        )])

    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def quick_buy_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pre-fill purchase context from remembered settings (callback: uf:qb:b:<pid>)."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_quick_buy_enabled"):
        await query.answer("⚡ Quick Buy is currently disabled.", show_alert=False)
        return

    if check_user_banned(update.effective_user.id):
        await query.answer("⛔ Banned.", show_alert=True)
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_main_kb())
            return

        qbc = (
            s.query(QuickBuyConfig)
            .filter_by(user_id=user.id, product_id=product_id)
            .first()
        )
        product = s.query(Product).filter_by(id=product_id).first()

        if not product or not product.is_active:
            await _safe_edit(query, "❌ Product is no longer available.", _back_main_kb())
            return

        qty = (qbc.quantity if qbc and qbc.quantity else 1)
        pm = (qbc.payment_method if qbc else None)

    # Pre-populate purchase context so the confirm_purchase handler works
    context.user_data['purchase_product_id'] = product_id
    context.user_data['purchase_product_name'] = product.name
    context.user_data['purchase_product_price'] = product.price
    context.user_data['purchase_quantity'] = qty
    context.user_data['quick_buy_payment_method'] = pm

    total = product.price * qty
    pm_note = f"\n💳 Payment: {pm}" if pm else ""
    text = (
        f"⚡ <b>Quick Buy</b>\n\n"
        f"📦 {product.name}\n"
        f"🔢 Quantity: {qty}\n"
        f"💰 Total: {_fmt_price(total)}{pm_note}\n\n"
        f"Confirm to proceed to payment."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Proceed to Checkout",
            callback_data=f"buy_{product_id}",
        )],
        [InlineKeyboardButton("❌ Cancel", callback_data="main_menu")],
    ])
    await _safe_edit(query, text, kb)


# ─────────────────────────────────────────────────────────────────────────
# ⭐  PREFERRED PAYMENT METHOD
# ─────────────────────────────────────────────────────────────────────────

_PAYMENT_METHOD_LABELS = [
    ("nowpayments", "🌐 NOWPayments"),
    ("binance_pay", "🟡 Binance Pay"),
    ("bybit_pay",   "💙 Bybit Pay"),
    ("bybit_trc20", "💵 USDT TRC20"),
    ("bybit_bep20", "🟢 USDT BEP20"),
    ("bybit_erc20", "🔵 USDT ERC20"),
    ("bkash",       "📱 bKash"),
    ("nagad",       "🟠 Nagad"),
    ("cryptomus",   "💠 Cryptomus"),
    ("stars",       "⭐ Telegram Stars"),
]


async def preferred_payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show and set preferred payment method."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        await _safe_edit(query, "⛔ You have been banned.", _back_main_kb())
        return

    if not _feat("feature_preferred_payment_enabled"):
        await _safe_edit(query, "⭐ Preferred Payment is currently disabled.", _back_main_kb())
        return

    telegram_id = update.effective_user.id
    current = get_preferred_payment(telegram_id) or ""

    text = "⭐ <b>Preferred Payment Method</b>\n\n"
    if current:
        label = dict(_PAYMENT_METHOD_LABELS).get(current, current)
        text += f"Current: <b>{label}</b>\n\n"
    else:
        text += "No preferred method set.\n\n"
    text += "Select your preferred payment method (highlighted during checkout):"

    kb: List[List[InlineKeyboardButton]] = []
    for key, label in _PAYMENT_METHOD_LABELS:
        mark = "✅ " if key == current else ""
        kb.append([InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"uf:pp:s:{key}"
        )])
    if current:
        kb.append([InlineKeyboardButton("🗑 Clear Preference", callback_data="uf:pp:s:clear")])
    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])

    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def preferred_payment_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set preferred payment method (callback: uf:pp:s:<method>)."""
    query = update.callback_query

    if not _feat("feature_preferred_payment_enabled"):
        await query.answer("⭐ Preferred Payment is currently disabled.", show_alert=False)
        return

    if check_user_banned(update.effective_user.id):
        await query.answer("⛔ Banned.", show_alert=True)
        return

    try:
        method = query.data.split(":")[3]
    except IndexError:
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    telegram_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        pp = s.query(PreferredPayment).filter_by(user_id=user.id).first()

        if method == "clear":
            if pp:
                s.delete(pp)
            await query.answer("⭐ Preference cleared.", show_alert=False)
        else:
            if pp:
                pp.payment_method = method
                pp.set_at = datetime.utcnow()
            else:
                s.add(PreferredPayment(
                    user_id=user.id,
                    payment_method=method,
                ))
            label = dict(_PAYMENT_METHOD_LABELS).get(method, method)
            await query.answer(f"⭐ Preferred: {label}", show_alert=False)

    # Refresh the menu
    await preferred_payment_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# 🔁  BUY AGAIN
# ─────────────────────────────────────────────────────────────────────────

async def buy_again_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show previously purchased products."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        await _safe_edit(query, "⛔ You have been banned.", _back_main_kb())
        return

    if not _feat("feature_buy_again_enabled"):
        await _safe_edit(query, "🔁 Buy Again is currently disabled.", _back_main_kb())
        return

    telegram_id = update.effective_user.id
    max_hist = cfg.get_int("feature_buy_again_max", 20)

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_main_kb())
            return

        # Distinct products from completed orders, most recent first
        from sqlalchemy import func, desc
        rows = (
            s.query(
                Product,
                func.max(Order.created_at).label("last_ordered"),
            )
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(
                Order.user_id == user.id,
                Order.status == OrderStatus.COMPLETED,
                Product.is_active == True,  # noqa
            )
            .group_by(Product.id)
            .order_by(desc("last_ordered"))
            .limit(max_hist if max_hist > 0 else 100)
            .all()
        )

    if not rows:
        text = (
            "🔁 <b>Buy Again</b>\n\n"
            "No completed purchases found yet.\n\n"
            "After your first purchase, we'll show it here for quick re-ordering."
        )
        kb = [
            [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    text = f"🔁 <b>Buy Again</b> ({len(rows)} products)\n\n"
    kb: List[List[InlineKeyboardButton]] = []
    for product, last_ordered in rows:
        when = last_ordered.strftime("%b %d") if last_ordered else ""
        text += f"• <b>{product.name}</b> — {_fmt_price(product.price)}  <i>last: {when}</i>\n"
        kb.append([InlineKeyboardButton(
            f"🛒 Buy Again: {product.name[:22]}",
            callback_data=f"buy_{product.id}",
        )])

    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))
