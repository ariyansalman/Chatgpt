"""User-facing Product Compare handlers — V22.

Callback namespace: ``cmp:*``

Callbacks handled:
    cmp:add:<product_id>   — add a product to the compare list
    cmp:rm:<product_id>    — remove a product from the compare list
    cmp:view               — view the comparison table
    cmp:clear              — clear the entire compare list
    cmp:buy:<product_id>   — buy a product directly from the comparison page

Feature-status behaviour:
    disabled    → button hidden (never sent), but stale callbacks show a
                  brief "feature unavailable" answer so old messages don't
                  hang.
    maintenance → show maintenance notice and return.
    enabled     → normal operation.

Entry point for bot.py:
    application.add_handler(CallbackQueryHandler(cmp_dispatch, pattern=r"^cmp:"))
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest
from telegram.constants import ParseMode

from services import product_compare as svc
from utils import check_user_banned

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup,
                                      parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(product_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if product_id:
        rows.append([InlineKeyboardButton(
            "🔙 Back to Product", callback_data=f"product_{product_id}"
        )])
    rows.append([
        InlineKeyboardButton("⚖️ View Comparison", callback_data="cmp:view"),
        InlineKeyboardButton("⬅️ Back to Menu",       callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _compare_view_kb(product_ids: list[int]) -> InlineKeyboardMarkup:
    rows = []

    # Buy buttons for each product
    if product_ids:
        buy_row = [
            InlineKeyboardButton(f"🛒 Buy #{i+1}", callback_data=f"cmp:buy:{pid}")
            for i, pid in enumerate(product_ids)
        ]
        # Split into max 2 per row
        for i in range(0, len(buy_row), 2):
            rows.append(buy_row[i:i+2])

    # Remove buttons
    rows.append([InlineKeyboardButton("🗑 Clear All",  callback_data="cmp:clear")])
    rows.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _maintenance_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
    ]])


# ─────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────

async def cmp_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You have been banned.", show_alert=True)
        return

    await query.answer()

    # Feature-status guard
    status = svc.feature_status()
    if not svc.cfg.get_bool("feature_product_compare_enabled", True) or status == "disabled":
        await query.answer("⚠️ Product comparison is currently unavailable.", show_alert=True)
        return

    if status == "maintenance":
        await _safe_edit(
            query,
            "⚠️ <b>Product comparison is currently under maintenance.</b>\n"
            "Please try again later.",
            _maintenance_kb(),
        )
        return

    data = query.data  # e.g. "cmp:add:42"
    parts = data.split(":")

    if len(parts) < 2:
        return

    action = parts[1]

    # ── cmp:add:<pid> ──────────────────────────────────────────────────────
    if action == "add" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        ok, msg = svc.add_to_compare(tg_id, product_id)
        count = svc.get_compare_count(tg_id)
        counter_str = f" [{count}/{svc.max_products()}]" if svc.show_counter() else ""
        await _safe_edit(
            query,
            msg,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"⚖️ View Comparison{counter_str}", callback_data="cmp:view"
                )],
                [InlineKeyboardButton(
                    "🔙 Back to Product", callback_data=f"product_{product_id}"
                )],
            ]),
        )
        return

    # ── cmp:rm:<pid> ───────────────────────────────────────────────────────
    if action == "rm" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        ok, msg = svc.remove_from_compare(tg_id, product_id)
        await _safe_edit(
            query,
            msg,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ View Comparison", callback_data="cmp:view")],
                [InlineKeyboardButton(
                    "🔙 Back to Product", callback_data=f"product_{product_id}"
                )],
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
            ]),
        )
        return

    # ── cmp:view ───────────────────────────────────────────────────────────
    if action == "view":
        product_ids = svc.get_compare_list(tg_id)
        if not product_ids:
            await _safe_edit(
                query,
                "📭 <b>Your comparison list is empty.</b>\n\n"
                "Browse products and tap <b>⚖️ Add to Compare</b> to get started.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛍 Products", callback_data="products"),
                    InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
                ]]),
            )
            return

        if len(product_ids) == 1:
            await _safe_edit(
                query,
                "ℹ️ <b>Add at least 2 products to compare.</b>\n\n"
                f"You currently have <b>1</b> product in your list.\n"
                f"Browse products and add more!",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛍 Products", callback_data="products")],
                    [InlineKeyboardButton(
                        "🗑 Clear List", callback_data="cmp:clear"
                    )],
                    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
                ]),
            )
            return

        text, ids = svc.build_comparison_message(tg_id)
        await _safe_edit(query, text, _compare_view_kb(ids))
        return

    # ── cmp:clear ──────────────────────────────────────────────────────────
    if action == "clear":
        n = svc.clear_compare_list(tg_id)
        msg = (f"🗑 <b>Comparison list cleared.</b>\n{n} product(s) removed."
               if n else "ℹ️ Your comparison list was already empty.")
        await _safe_edit(
            query,
            msg,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Browse Products", callback_data="products")],
                [InlineKeyboardButton("⬅️ Back to Menu",       callback_data="main_menu")],
            ]),
        )
        return

    # ── cmp:buy:<pid> ──────────────────────────────────────────────────────
    if action == "buy" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        svc.mark_purchased_from_compare(tg_id, product_id)
        # Delegate to the existing buy flow
        from handlers.payment_handlers import buy_product_start
        from utils.update_proxy import with_data
        try:
            await buy_product_start(with_data(update, f"buy_{product_id}"), context)
        except Exception:
            logger.exception("compare: cmp:buy redirect failed")
            await query.answer(
                "Redirecting to purchase…", show_alert=False
            )
        return

    # Fallback
    await query.answer("Unknown compare action.", show_alert=True)
