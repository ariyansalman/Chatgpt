"""User-facing Inventory Reservation System handlers — V23.

Callback namespace: ``irs:*``

Callbacks handled:
    irs:view:<pid>         — view stock info + reservation status for a product
    irs:reserve:<pid>      — create a UI reservation for a product
    irs:cancel:<res_id>    — cancel own active reservation
    irs:checkout:<pid>     — release UI reservation → proceed to checkout
    irs:myres              — list all user's active reservations
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

import services.inventory_reservation_ui as svc
from utils import check_user_banned

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_product_kb(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Product", callback_data=f"product_{product_id}")]
    ])


# ─────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────

async def irs_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all irs:* callbacks."""
    query  = update.callback_query
    tg_id  = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()

    data  = query.data   # irs:<action>[:<args...>]
    parts = data.split(":")

    if len(parts) < 2:
        return

    action = parts[1]

    # ── irs:view:<pid> ──────────────────────────────────────────────────────
    if action == "view" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        await _handle_view(update, context, tg_id, product_id)
        return

    # ── irs:reserve:<pid> ───────────────────────────────────────────────────
    if action == "reserve" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        await _handle_reserve(update, context, tg_id, product_id)
        return

    # ── irs:cancel:<res_id> ─────────────────────────────────────────────────
    if action == "cancel" and len(parts) >= 3 and parts[2].isdigit():
        res_id = int(parts[2])
        await _handle_cancel(update, context, tg_id, res_id)
        return

    # ── irs:checkout:<pid> ──────────────────────────────────────────────────
    if action == "checkout" and len(parts) >= 3 and parts[2].isdigit():
        product_id = int(parts[2])
        await _handle_checkout(update, context, tg_id, product_id)
        return

    # ── irs:myres ───────────────────────────────────────────────────────────
    if action == "myres":
        await _handle_my_reservations(update, context, tg_id)
        return


# ─────────────────────────────────────────────────────────────────────────
# View reservation / stock status for a product
# ─────────────────────────────────────────────────────────────────────────

async def _handle_view(update, context, tg_id: int, product_id: int):
    query = update.callback_query

    if not svc.is_enabled():
        status = svc.feature_status()
        if status == "maintenance":
            await _safe_edit(query, "🛠 Inventory reservations are under maintenance.", _back_product_kb(product_id))
        else:
            await _safe_edit(query, "⏳ Inventory reservation is currently unavailable.", _back_product_kb(product_id))
        return

    # Load product info
    from database import get_db_session, Product
    with get_db_session() as s:
        p = s.query(Product).filter_by(id=product_id).first()
        if not p or not p.is_active:
            await _safe_edit(query, "❌ Product not found.", _back_product_kb(product_id))
            return
        p_name = p.name

    stock   = svc.get_stock_summary(product_id)
    user_pk = svc.get_user_pk(tg_id)
    res     = svc.get_user_active_reservation(user_pk, product_id) if user_pk else None

    ttl = svc.ttl_minutes()

    lines = [
        f"⏳ <b>Stock Reservation</b>",
        f"<i>{p_name}</i>",
        "",
        f"📦 <b>Available Stock:</b>   {stock['available']}",
        f"🔒 <b>Reserved Stock:</b>    {stock['reserved']}",
        f"✅ <b>Remaining Stock:</b>   {stock['remaining']}",
        "",
    ]

    kb = []

    if res:
        countdown = svc.format_countdown(res.expires_at)
        time_left = svc.format_time_remaining(res.expires_at)

        if countdown == "Expired":
            lines += [
                "⌛ <b>Your Reservation:</b>  <b>Expired</b>",
                "Your reservation has expired. Reserve again to hold stock.",
                "",
            ]
            kb.append([InlineKeyboardButton(
                f"⏳ Reserve Again  ({ttl} min)",
                callback_data=f"irs:reserve:{product_id}",
            )])
        else:
            lines += [
                "✅ <b>You have an active reservation!</b>",
                "",
                f"⏳ <b>Time Remaining:</b>",
                f"<b>    {countdown}</b>  ({time_left})",
                "",
                "Your stock is held. Complete your purchase or cancel.",
            ]
            kb.append([InlineKeyboardButton(
                f"🛒 Continue to Checkout",
                callback_data=f"irs:checkout:{product_id}",
            )])
            if svc.allow_manual_release():
                kb.append([InlineKeyboardButton(
                    "❌ Cancel Reservation",
                    callback_data=f"irs:cancel:{res.id}",
                )])
    else:
        if stock["remaining"] <= 0:
            lines += ["❌ <b>No stock available to reserve.</b>"]
        else:
            lines += [
                f"No active reservation.",
                f"Reserve now to hold your spot for <b>{ttl} minutes</b>.",
            ]
            kb.append([InlineKeyboardButton(
                f"⏳ Reserve  ({ttl} min)",
                callback_data=f"irs:reserve:{product_id}",
            )])

    kb.append([InlineKeyboardButton("🔙 Back to Product", callback_data=f"product_{product_id}")])

    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Create reservation
# ─────────────────────────────────────────────────────────────────────────

async def _handle_reserve(update, context, tg_id: int, product_id: int):
    query   = update.callback_query

    if not svc.is_enabled():
        await query.answer("⏳ Inventory reservation is unavailable.", show_alert=True)
        return

    user_pk = svc.get_user_pk(tg_id)
    if not user_pk:
        await query.answer("❌ User not found. Please /start the bot.", show_alert=True)
        return

    res, err = svc.create_ui_reservation(user_pk, product_id)
    if err:
        await query.answer(f"❌ {err}", show_alert=True)
        return

    # Show the updated view with countdown
    await _handle_view(update, context, tg_id, product_id)


# ─────────────────────────────────────────────────────────────────────────
# Cancel reservation
# ─────────────────────────────────────────────────────────────────────────

async def _handle_cancel(update, context, tg_id: int, res_id: int):
    query   = update.callback_query
    user_pk = svc.get_user_pk(tg_id)
    if not user_pk:
        await query.answer("❌ User not found.", show_alert=True)
        return

    # Look up the reservation to find product_id for navigation
    product_id = None
    try:
        from database import get_db_session
        from database.models import StockReservation
        with get_db_session() as s:
            r = s.query(StockReservation).filter_by(id=res_id).first()
            if r:
                product_id = r.product_id
    except Exception:
        pass

    ok, msg = svc.cancel_ui_reservation(res_id, user_pk)
    await query.answer(msg, show_alert=not ok)

    if ok and product_id:
        await _handle_view(update, context, tg_id, product_id)
    elif product_id:
        await _handle_view(update, context, tg_id, product_id)


# ─────────────────────────────────────────────────────────────────────────
# Checkout — release UI reservation, route to buy flow
# ─────────────────────────────────────────────────────────────────────────

async def _handle_checkout(update, context, tg_id: int, product_id: int):
    """Release the UI-level reservation then route to the standard checkout."""
    query   = update.callback_query
    user_pk = svc.get_user_pk(tg_id)

    # Release any active UI reservation for this product (best-effort)
    if user_pk:
        try:
            res = svc.get_user_active_reservation(user_pk, product_id)
            if res:
                svc.cancel_ui_reservation(res.id, user_pk)
        except Exception:
            pass

    # Redirect to buy flow (same pattern as favorites buy redirect)
    from utils.update_proxy import with_data
    try:
        from handlers.payment_handlers import buy_product_start
        await buy_product_start(with_data(update, f"buy_{product_id}"), context)
    except Exception:
        logger.exception("irs: checkout redirect failed")
        await query.answer("Redirecting to checkout…", show_alert=False)


# ─────────────────────────────────────────────────────────────────────────
# My reservations list
# ─────────────────────────────────────────────────────────────────────────

async def _handle_my_reservations(update, context, tg_id: int):
    query   = update.callback_query
    user_pk = svc.get_user_pk(tg_id)

    if not user_pk:
        await _safe_edit(query, "❌ User not found.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]
        ]))
        return

    from database import get_db_session
    from database.models import StockReservation, ReservationStatus, Product
    from datetime import datetime

    with get_db_session() as s:
        now = datetime.utcnow()
        rows = (
            s.query(StockReservation)
             .filter(
                 StockReservation.user_id == user_pk,
                 StockReservation.status == ReservationStatus.ACTIVE,
                 StockReservation.expires_at > now,
             )
             .order_by(StockReservation.expires_at.asc())
             .all()
        )
        items = []
        for r in rows:
            p = s.query(Product).filter_by(id=r.product_id).first()
            items.append({
                "id":         r.id,
                "product_id": r.product_id,
                "name":       p.name if p else f"Product #{r.product_id}",
                "qty":        r.quantity,
                "expires_at": r.expires_at,
            })

    if not items:
        text = "⏳ <b>My Reservations</b>\n\nYou have no active stock reservations."
        kb   = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    lines = ["⏳ <b>My Active Reservations</b>", ""]
    kb    = []
    for item in items:
        countdown = svc.format_countdown(item["expires_at"])
        lines.append(
            f"• <b>{item['name']}</b>  qty: {item['qty']}  ⏱ {countdown}"
        )
        kb.append([
            InlineKeyboardButton(
                f"🛒 {item['name'][:24]}",
                callback_data=f"irs:view:{item['product_id']}",
            )
        ])

    kb.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))
