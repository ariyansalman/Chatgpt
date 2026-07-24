"""Persistent shopping cart (V8 Premium Core) + real multi-item checkout.

Cart lives in the DB (``cart`` table) — never only in ``context.user_data`` —
so it survives restarts and cross-device sessions. All checkout paths
re-validate stock / price / active status at fulfillment time.

Callback prefixes:
  cart                       -> view cart
  cart_add_<pid>             -> add product (or open variant picker)
  cart_addv_<pid>_<vid>      -> add specific variant
  cart_inc_<cartrow_id>      -> +1 quantity
  cart_dec_<cartrow_id>      -> -1 quantity
  cart_rm_<cartrow_id>       -> remove row
  cart_clear                 -> empty cart
  cart_checkout              -> show checkout confirmation
  cart_confirm               -> execute wallet checkout
  cart_cancel                -> cancel confirmation
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update,
)
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import (
    Cart, Coupon, CouponRedemption, DiscountType,
    Product, ProductVariant, ProductKey, StockReservation, User, ProductType,
    Order, OrderItem, OrderStatus, OrderLifecycleStatus,
)
from services import inventory
from i18n import t, get_user_language
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def _get_or_create_user(session, tg_id: int) -> User:
    u = session.query(User).filter(User.telegram_id == tg_id).first()
    if u is None:
        u = User(telegram_id=tg_id)
        session.add(u); session.flush()
    return u


def _line_price(product: Product, variant: ProductVariant | None) -> float:
    if variant is not None:
        return variant.effective_price
    return float(product.price or 0)


def _line_label(product: Product, variant: ProductVariant | None) -> str:
    if variant is not None:
        return f"{product.name} — {variant.name}"
    return product.name


def _format_price(v: float) -> str:
    return f"${float(v):.2f}"


def _revalidate_coupon_by_id(session, coupon_id: int, user_pk: int,
                              subtotal: float) -> tuple[float, str]:
    """Re-validate a coupon from DB and return (discount_amount, error_str).

    Called at checkout time to reject stale user_data cache values.
    Returns (0.0, error_str) when the coupon is no longer valid.
    """
    c = session.query(Coupon).filter_by(id=coupon_id).first()
    if not c:
        return 0.0, "Coupon no longer exists."
    if not c.is_active:
        return 0.0, "Coupon is no longer active."
    if c.expires_at and c.expires_at < datetime.utcnow():
        return 0.0, "Coupon has expired."
    if c.max_uses and c.used_count >= c.max_uses:
        return 0.0, "Coupon has reached its usage limit."
    if c.min_order_amount and subtotal < c.min_order_amount:
        return 0.0, f"Coupon requires a minimum order of {_format_price(c.min_order_amount)}."
    if c.per_user_limit:
        used = session.query(CouponRedemption).filter_by(
            coupon_id=c.id, user_id=user_pk
        ).count()
        if used >= c.per_user_limit:
            return 0.0, "You have already used this coupon the maximum number of times."
    if c.discount_type == DiscountType.PERCENT:
        discount = subtotal * (c.discount_value / 100.0)
    else:
        discount = float(c.discount_value)
    return round(min(discount, subtotal), 2), ""


async def cart_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    lines = [t("cart.title", lang) + "\n"]
    kb: list[list[InlineKeyboardButton]] = []
    subtotal = 0.0
    with get_db_session() as s:
        user = _get_or_create_user(s, tg_id)
        rows = s.query(Cart).filter(Cart.user_id == user.id).all()
        if not rows:
            lines.append(t("cart.empty", lang))
        else:
            for row in rows:
                p = row.product
                v = row.variant
                if not p or not p.is_active:
                    lines.append(t("cart.unavailable_line", lang, qty=row.quantity))
                    kb.append([InlineKeyboardButton(t("cart.remove_button", lang),
                        callback_data=f"cart_rm_{row.id}")])
                    continue
                unit = _line_price(p, v)
                line_total = unit * row.quantity
                subtotal += line_total
                lines.append(t(
                    "cart.line", lang,
                    label=_line_label(p, v), qty=row.quantity,
                    unit=f"${unit:.2f}", total=f"${line_total:.2f}",
                ))
                kb.append([
                    InlineKeyboardButton("−", callback_data=f"cart_dec_{row.id}"),
                    InlineKeyboardButton(str(row.quantity), callback_data="noop"),
                    InlineKeyboardButton("+", callback_data=f"cart_inc_{row.id}"),
                    InlineKeyboardButton("🗑️", callback_data=f"cart_rm_{row.id}"),
                ])
    lines.append(t("cart.subtotal", lang, subtotal=f"${subtotal:.2f}"))
    if subtotal > 0:
        kb.append([InlineKeyboardButton(t("cart.checkout_button", lang), callback_data="cart_checkout")])
        kb.append([InlineKeyboardButton(t("cart.clear_button", lang), callback_data="cart_clear")])
    kb.append([InlineKeyboardButton(t("cart.continue_shopping", lang), callback_data="products")])
    kb.append([InlineKeyboardButton(t("cart.main_menu", lang), callback_data="main_menu")])
    markup = InlineKeyboardMarkup(kb)
    text = "\n".join(lines)
    if q:
        try:
            try:
                await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            await q.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=markup,
                                                 parse_mode="HTML")


async def cart_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add product. If the product has active variants, show a variant picker."""
    q = update.callback_query
    await q.answer()
    pid = int(q.data.rsplit("_", 1)[1])
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    with get_db_session() as s:
        product = s.query(Product).filter(Product.id == pid,
                                          Product.is_active == True).first()  # noqa: E712
        if not product:
            await q.answer(t("cart.product_unavailable", lang), show_alert=True); return
        active_vars = [v for v in product.variants if v.is_active]
        if active_vars:
            kb = [[InlineKeyboardButton(
                f"{v.name} — ${v.effective_price:.2f}",
                callback_data=f"cart_addv_{pid}_{v.id}")] for v in active_vars]
            kb.append([InlineKeyboardButton("🔙", callback_data=f"product_{pid}")])
            try:
                await q.edit_message_text(t("cart.pick_variant", lang, name=product.name),
                                          reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        await _add_row(s, update.effective_user.id, pid, None)
    await cart_view(update, context)


async def cart_add_variant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, _, pid, vid = q.data.split("_")
    pid, vid = int(pid), int(vid)
    with get_db_session() as s:
        await _add_row(s, update.effective_user.id, pid, vid)
    await cart_view(update, context)


async def _add_row(session, tg_id: int, product_id: int,
                   variant_id: int | None) -> None:
    user = _get_or_create_user(session, tg_id)
    existing = session.query(Cart).filter(
        Cart.user_id == user.id,
        Cart.product_id == product_id,
        Cart.variant_id == variant_id,
    ).first()
    available = inventory.count_available(product_id, variant_id)
    if existing:
        existing.quantity = min(existing.quantity + 1, max(1, available))
    else:
        session.add(Cart(user_id=user.id, product_id=product_id,
                         variant_id=variant_id, quantity=1))
    session.commit()


async def cart_inc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_language(update.effective_user.id)
    row_id = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        row = s.query(Cart).filter(Cart.id == row_id,
            Cart.user_id == _get_or_create_user(s, update.effective_user.id).id).first()
        if not row:
            await q.answer(t("cart.row_gone", lang), show_alert=True); return
        avail = inventory.count_available(row.product_id, row.variant_id)
        if row.quantity + 1 > max(1, avail):
            await q.answer(t("cart.no_stock", lang), show_alert=True)
        else:
            row.quantity += 1
            s.commit()
    await cart_view(update, context)


async def cart_dec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_language(update.effective_user.id)
    row_id = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        row = s.query(Cart).filter(Cart.id == row_id,
            Cart.user_id == _get_or_create_user(s, update.effective_user.id).id).first()
        if not row:
            await q.answer(t("cart.row_gone", lang), show_alert=True); return
        if row.quantity <= 1:
            s.delete(row)
        else:
            row.quantity -= 1
        s.commit()
    await cart_view(update, context)


async def cart_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    row_id = int(q.data.rsplit("_", 1)[1])
    with get_db_session() as s:
        row = s.query(Cart).filter(Cart.id == row_id,
            Cart.user_id == _get_or_create_user(s, update.effective_user.id).id).first()
        if row:
            s.delete(row); s.commit()
    await cart_view(update, context)


async def cart_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    with get_db_session() as s:
        user = _get_or_create_user(s, update.effective_user.id)
        s.query(Cart).filter(Cart.user_id == user.id).delete()
        s.commit()
    await cart_view(update, context)


# ─────────────────────────────────────────────────────────────────────
# REAL CART CHECKOUT (multi-item, wallet-based)
# ─────────────────────────────────────────────────────────────────────
def _revalidate_cart(session, user_id: int, lang: str = "en"):
    """Return (valid_lines, errors, subtotal).

    Each valid line is dict(cart_id, product_id, variant_id, name, type,
    unit_price, quantity). Errors is list[str] of human messages.
    Prices are re-read from the DB — callback / user_data values are ignored.
    """
    rows = session.query(Cart).filter(Cart.user_id == user_id).all()
    valid, errors = [], []
    subtotal = 0.0
    for row in rows:
        p = row.product
        if not p or not p.is_active:
            errors.append(t("cart.error_unavailable", lang, name=(p.name if p else "item")))
            continue
        v = row.variant
        if row.variant_id and (not v or not v.is_active):
            errors.append(t("cart.error_variant_unavailable", lang, name=p.name))
            continue
        qty = int(row.quantity or 0)
        if qty <= 0:
            errors.append(t("cart.error_invalid_qty", lang, name=p.name))
            continue
        # Min / max
        if p.min_quantity and qty < p.min_quantity:
            errors.append(t("cart.error_min_qty", lang, name=p.name, min=p.min_quantity))
            continue
        if p.max_quantity and qty > p.max_quantity:
            errors.append(t("cart.error_max_qty", lang, name=p.name, max=p.max_quantity))
            continue
        # Availability (skip for type flows without physical inventory)
        needs_inventory = p.product_type in (
            ProductType.KEY, ProductType.REDEEM_LINK,
            ProductType.ACCOUNT_LOGIN, ProductType.VOUCHER,
        )
        if needs_inventory:
            avail = inventory.count_available(p.id, row.variant_id)
            if avail < qty:
                errors.append(t("cart.error_stock", lang, name=p.name, avail=avail, qty=qty))
                continue
        unit = _line_price(p, v)
        subtotal += unit * qty
        valid.append({
            "cart_id": row.id,
            "product_id": p.id,
            "variant_id": row.variant_id,
            "name": p.name,
            "type": p.product_type,
            "unit_price": float(unit),
            "quantity": qty,
            "download_link": p.download_link,
        })
    return valid, errors, subtotal


async def cart_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show real checkout confirmation screen for wallet payment."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)

    with get_db_session() as s:
        user = _get_or_create_user(s, tg_id)
        valid, errors, subtotal = _revalidate_cart(s, user.id, lang)
        balance = float(user.wallet_balance or 0)

        # ── Coupon: re-validate from DB (not trusting user_data cache) ──
        coupon_id = context.user_data.get('purchase_coupon_id')
        coupon_discount = 0.0
        coupon_code = context.user_data.get('purchase_coupon_code', '')
        if coupon_id and valid:
            discount_val, coupon_err = _revalidate_coupon_by_id(
                s, coupon_id, user.id, subtotal
            )
            if coupon_err:
                logger.info("Cart coupon %s rejected at checkout preview: %s",
                            coupon_id, coupon_err)
                for k in ('purchase_coupon_id', 'purchase_coupon_code',
                          'purchase_coupon_discount'):
                    context.user_data.pop(k, None)
                coupon_id = None
                coupon_code = ''
            else:
                coupon_discount = discount_val
                context.user_data['purchase_coupon_discount'] = coupon_discount

    if not valid:
        text = t("cart.cannot_checkout_title", lang)
        if errors:
            text += "\n\n" + "\n".join(f"• {e}" for e in errors)
        kb = [[InlineKeyboardButton(t("cart.back_to_cart", lang), callback_data="cart")]]
        try:
            await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                      parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    coupon_discount = min(coupon_discount, subtotal)
    total = max(0.0, subtotal - coupon_discount)

    lines = [t("cart.checkout_title", lang), ""]
    for it in valid:
        lines.append(t(
            "cart.item_line", lang,
            name=it['name'], qty=it['quantity'],
            total=_format_price(it['unit_price'] * it['quantity']),
        ))
    lines.append("")
    lines.append(t("cart.subtotal_label", lang, subtotal=_format_price(subtotal)))
    if coupon_discount > 0 and coupon_code:
        lines.append(t("cart.coupon_line", lang, code=coupon_code, discount=_format_price(coupon_discount)))
    elif coupon_discount > 0:
        lines.append(t("cart.discount_line", lang, discount=_format_price(coupon_discount)))
    lines.append(t("cart.total_label", lang, total=_format_price(total)))
    lines.append(t("cart.wallet_balance_label", lang, balance=_format_price(balance)))
    if errors:
        lines.append("")
        lines.append(t("cart.removed_header", lang))
        for e in errors:
            lines.append(f"• {e}")

    kb: list[list[InlineKeyboardButton]] = []
    if balance >= total:
        kb.append([InlineKeyboardButton(
            t("cart.confirm_button", lang, total=_format_price(total)),
            callback_data="cart_confirm")])
    else:
        need = total - balance
        lines.append("")
        lines.append(t("cart.insufficient_balance", lang, need=_format_price(need)))
        kb.append([InlineKeyboardButton(t("cart.deposit_now", lang), callback_data="topup")])
    kb.append([InlineKeyboardButton(t("cart.cancel_button", lang), callback_data="cart_cancel")])
    kb.append([InlineKeyboardButton(t("cart.back_to_cart", lang), callback_data="cart")])

    try:
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def cart_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = get_user_language(update.effective_user.id)
    await q.answer(t("cart.checkout_cancelled_toast", lang))
    await cart_view(update, context)


async def cart_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute the multi-item wallet checkout with full integration wiring.

    Integration fixes:
      - Idempotency guard propagates errors (no silent except-pass)
      - Coupon re-validated from DB at confirm time
      - inventory.reserve() called before wallet debit (locks stock)
      - inventory.consume() called at fulfillment (marks keys sold via service)
      - inventory.release_for_order() called on any failure path
      - Order lifecycle transitions: PROCESSING → COMPLETED / FAILED
      - OrderStatus.FAILED used directly (enum extended)
    """
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)

    # ── Fix: Idempotency — reject duplicate confirm taps without silently
    # swallowing the guard. Only bypass on ImportError (first boot). ────
    try:
        from services.idempotency import claim as _idem_claim
        upd_id = str(update.update_id or getattr(q, "id", None) or "")
    except ImportError:
        _idem_claim = None
        upd_id = ""
    if _idem_claim and upd_id:
        with _idem_claim("cart_confirm", f"tg{tg_id}:u{upd_id}") as ok:
            if not ok:
                await q.answer(t("cart.already_processing", lang), show_alert=True)
                return

    # ── Phase 0: Revalidate cart + coupon from DB ──────────────────
    with get_db_session() as s:
        user = _get_or_create_user(s, tg_id)
        user_pk = user.id
        valid, errors, subtotal = _revalidate_cart(s, user.id, lang)

        if not valid:
            try:
                await q.edit_message_text(
                    t("cart.empty_or_unavailable", lang),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(t("cart.back_button_short", lang), callback_data="cart")]]),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Fix: Coupon revalidation from DB — never trust user_data cache
        coupon_id = context.user_data.get('purchase_coupon_id')
        coupon_discount = 0.0
        if coupon_id:
            coupon_discount, coupon_err = _revalidate_coupon_by_id(
                s, coupon_id, user_pk, subtotal
            )
            if coupon_err:
                logger.info("Cart coupon %s invalidated at confirm: %s",
                            coupon_id, coupon_err)
                for k in ('purchase_coupon_id', 'purchase_coupon_code',
                          'purchase_coupon_discount'):
                    context.user_data.pop(k, None)
                coupon_id = None
        coupon_discount = min(coupon_discount, subtotal)
        total = max(0.0, subtotal - coupon_discount)

    # ── Phase 1: Reserve inventory for all items (each in its own session)
    # Creates StockReservation rows and locks ProductKey rows, preventing
    # concurrent checkouts from overselling the same stock.
    #
    # reservations maps item_key -> reservation_id (None for BUNDLE items, which
    # have no bundle-container StockReservation; their child reservations are
    # tracked separately in bundle_child_reservation_ids).
    reservations: dict[str, int | None] = {}  # "<product_id>_<variant_id>" -> reservation_id | None
    # Child StockReservation ids created atomically by reserve_bundle().
    # Attached to order_id after the order row is committed, and released by
    # release_for_order() on any failure path.
    bundle_child_reservation_ids: list[int] = []
    reservation_errs: list[str] = []

    for it in valid:
        item_key = f"{it['product_id']}_{it['variant_id'] or 0}"
        try:
            if it["type"] == ProductType.BUNDLE:
                # Atomic: reserve ALL key-backed child inventory in ONE transaction.
                # If any child has insufficient stock, reserve_bundle() rolls back
                # the entire transaction and raises ReservationError — no partial
                # state is left behind and the wallet is never debited.
                child_reservations = inventory.reserve_bundle(
                    user_pk,
                    it["product_id"],
                    it["quantity"],
                )
                bundle_child_reservation_ids.extend(r.id for r in child_reservations)
                # No bundle-container reservation is needed — children are tracked
                # via bundle_child_reservation_ids and their order_id is attached
                # below after the Order row is created.
                reservations[item_key] = None
            else:
                res = inventory.reserve(
                    user_pk,
                    it["product_id"],
                    it["quantity"],
                    variant_id=it["variant_id"],
                )
                reservations[item_key] = res.id
        except inventory.ReservationError as e:
            reservation_errs.append(f"'{it['name']}': {e}")

    if reservation_errs:
        for res_id in (r for r in reservations.values() if r is not None):
            try:
                inventory.release(res_id)
            except Exception:
                logger.exception("Reservation release failed for id %s", res_id)
        for res_id in bundle_child_reservation_ids:
            try:
                inventory.release(res_id)
            except Exception:
                logger.exception("Bundle child reservation release failed for id %s", res_id)
        try:
            await q.edit_message_text(
                t("cart.out_of_stock_header", lang) + "\n\n" +
                "\n".join(f"• {e}" for e in reservation_errs) +
                "\n\n" + t("cart.update_cart_retry", lang),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t("cart.back_button_short", lang), callback_data="cart")]]),
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # ── Phase 2: Main checkout session ────────────────────────────
    order_id: int | None = None
    failure: str | None = None
    delivery_summaries: list[str] = []
    bulk_payloads: list[tuple[str, list[str]]] = []
    oversized_deliveries: list[tuple[str, int, str]] = []

    with get_db_session() as s:
        # Atomic wallet debit — succeeds only if balance is still >= total
        debited = s.query(User).filter(
            User.id == user_pk,
            User.wallet_balance >= total,
        ).update(
            {User.wallet_balance: User.wallet_balance - total},
            synchronize_session=False,
        )
        if debited == 0:
            s.rollback()
            for res_id in (r for r in reservations.values() if r is not None):
                try:
                    inventory.release(res_id)
                except Exception:
                    logger.exception("Reservation release failed after wallet debit miss")
            for res_id in bundle_child_reservation_ids:
                try:
                    inventory.release(res_id)
                except Exception:
                    logger.exception("Bundle child reservation release failed after wallet debit miss")
            try:
                await q.edit_message_text(
                    t("cart.insufficient_balance_retry", lang),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(t("cart.deposit_now", lang), callback_data="topup")],
                         [InlineKeyboardButton(t("cart.back_button_short", lang), callback_data="cart")]]),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        s.commit()

        # Create order at PROCESSING status
        order = Order(
            user_id=user_pk,
            total_amount=total,
            status=OrderStatus.PROCESSING,
        )
        s.add(order)
        s.commit()
        s.refresh(order)
        order_id = order.id

        # Attach reservations to the order so release_for_order() works.
        # Include both non-bundle item reservations AND bundle child reservations
        # so a single release_for_order() call cleans up everything on failure.
        _all_res_ids = (
            [r for r in reservations.values() if r is not None]
            + bundle_child_reservation_ids
        )
        if _all_res_ids:
            s.query(StockReservation).filter(
                StockReservation.id.in_(_all_res_ids)
            ).update({"order_id": order_id}, synchronize_session=False)
            s.commit()

        # Lifecycle: PROCESSING
        try:
            from services import order_lifecycle as _lc
            _lc.transition(order_id, OrderLifecycleStatus.PROCESSING)
        except Exception:
            logger.exception("Lifecycle PROCESSING failed for order %s", order_id)

        try:
            for it in valid:
                oi = OrderItem(
                    order_id=order_id,
                    product_id=it["product_id"],
                    variant_id=it["variant_id"],
                    quantity=it["quantity"],
                    price=it["unit_price"],
                )
                s.add(oi)
                s.flush()

                ptype = it["type"]
                item_key = f"{it['product_id']}_{it['variant_id'] or 0}"
                res_id = reservations.get(item_key)

                # ── V11 dispatcher for the 10 new product types ─────
                if ptype not in (ProductType.KEY, ProductType.FILE):
                    try:
                        from services.delivery_service import dispatch as _v11_dispatch
                        # Pass oi.id so the dispatcher delivers THIS specific
                        # order item rather than always falling back to items[0].
                        # This is required for multi-item carts that contain
                        # more than one V11-type product; without it, every
                        # loop iteration would re-deliver the first item.
                        res_obj = _v11_dispatch(order_id, session=s,
                                                order_item_id=oi.id)
                    except Exception:
                        logger.exception("V11 dispatch failed for order %s", order_id)
                        res_obj = None
                    if res_obj and res_obj.handled:
                        if res_obj.success or res_obj.queued:
                            from services.purchase_success import is_delivery_oversized
                            if res_obj.success and is_delivery_oversized(res_obj.user_message):
                                # Multi-quantity delivery too large to inline
                                # safely — defer to a .txt file, same as the
                                # legacy bulk_payloads (KEY) path below.
                                oversized_deliveries.append((it['name'], it['quantity'], res_obj.user_message))
                                delivery_summaries.append(
                                    t("cart.delivery_generic", lang, name=it['name'], qty=it['quantity'])
                                    + "\n" + t("cart.delivery_file_generic", lang)
                                )
                            else:
                                delivery_summaries.append(
                                    t("cart.delivery_generic", lang, name=it['name'], qty=it['quantity'])
                                    + f"\n{res_obj.user_message}"
                                )
                            s.expire(oi)
                            if res_id:
                                try:
                                    inventory.consume(res_id, order_id)
                                except Exception:
                                    logger.exception(
                                        "consume() failed for res %s (non-KEY/FILE)", res_id
                                    )
                            continue
                        raise RuntimeError(res_obj.error or "delivery failed")
                    delivery_summaries.append(
                        t("cart.delivery_pending", lang, name=it['name'], qty=it['quantity'])
                    )
                    continue

                # ── KEY: consume reservation → get assigned key values ──
                if ptype == ProductType.KEY:
                    if res_id:
                        key_values = inventory.consume(res_id, order_id)
                        if not key_values:
                            raise RuntimeError(
                                f"Reservation consume returned no keys for '{it['name']}'"
                            )
                    else:
                        # Fallback: direct lock (reservation was not created)
                        keys_q = s.query(ProductKey).filter_by(
                            product_id=it["product_id"], is_sold=False,
                        ).limit(it["quantity"]).with_for_update().all()
                        if len(keys_q) < it["quantity"]:
                            raise RuntimeError(
                                f"Only {len(keys_q)}/{it['quantity']} keys available"
                                f" for '{it['name']}'"
                            )
                        key_values = []
                        for k in keys_q:
                            k.is_sold = True
                            k.order_id = order_id
                            k.sold_at = datetime.utcnow()
                            key_values.append(k.key_value)

                    oi.delivered_asset = "\n".join(key_values)
                    try:
                        from utils.bot_config import cfg as _cfg
                        threshold = _cfg.get_int("bulk_delivery_threshold", 10)
                    except Exception:
                        threshold = 10
                    if it["quantity"] > threshold:
                        bulk_payloads.append((it["name"], key_values))
                        delivery_summaries.append(
                            t("cart.delivery_generic", lang, name=it['name'], qty=it['quantity'])
                            + "\n" + t("cart.delivery_keys_file", lang)
                        )
                    else:
                        delivery_summaries.append(
                            t("cart.delivery_generic", lang, name=it['name'], qty=it['quantity'])
                            + "\n" + t("cart.delivery_keys_inline", lang, keys=oi.delivered_asset)
                        )

                # ── FILE: consume reservation → decrement stock_count ───
                elif ptype == ProductType.FILE:
                    if not it["download_link"]:
                        raise RuntimeError(
                            f"'{it['name']}' has no download link configured"
                        )
                    oi.delivered_asset = it["download_link"]
                    if res_id:
                        inventory.consume(res_id, order_id)
                    else:
                        s.query(Product).filter(
                            Product.id == it["product_id"],
                            Product.stock_count >= it["quantity"],
                        ).update(
                            {Product.stock_count: Product.stock_count - it["quantity"]},
                            synchronize_session=False,
                        )
                    delivery_summaries.append(
                        t("cart.delivery_file", lang, name=it['name'], link=it['download_link'])
                    )

            s.commit()

            # Loyalty (best-effort — never blocks purchase)
            try:
                from handlers.loyalty_handlers import award_loyalty_points
                u_row = s.query(User).filter_by(id=user_pk).first()
                if u_row is not None:
                    award_loyalty_points(s, u_row, order_id, total)
                    s.commit()
            except Exception:
                logger.exception("Loyalty award failed for order %s", order_id)

            # Coupon redemption (atomic used_count increment inside helper)
            if coupon_id and coupon_discount > 0:
                try:
                    from handlers.coupon_handlers import record_coupon_redemption
                    record_coupon_redemption(coupon_id, user_pk, order_id, coupon_discount)
                except Exception:
                    logger.exception("Coupon redemption log failed for order %s", order_id)
            for _k in ('purchase_coupon_id', 'purchase_coupon_code',
                       'purchase_coupon_discount'):
                context.user_data.pop(_k, None)

            # Sales count denorm (best-effort)
            try:
                for it in valid:
                    s.query(Product).filter(Product.id == it["product_id"]).update(
                        {Product.sales_count: Product.sales_count + it["quantity"]},
                        synchronize_session=False,
                    )
                s.commit()
            except Exception:
                logger.exception("sales_count denorm failed for order %s", order_id)

            try:
                from services.social_proof import invalidate as _invalidate_social_proof
                for it in valid:
                    _invalidate_social_proof(it["product_id"])
            except Exception:
                logger.exception("social_proof cache invalidation failed for order %s", order_id)

            # Clear cart
            s.query(Cart).filter(Cart.user_id == user_pk).delete()
            s.commit()

            # Lifecycle: DELIVERED → COMPLETED (transition() syncs order.status via _LEGACY_MAP)
            try:
                from services import order_lifecycle as _lc
                # Both map to legacy COMPLETED; whichever call first flips
                # order.completed_at fires the auto-invoice hook exactly once.
                _lc.transition(order_id, OrderLifecycleStatus.DELIVERED, bot=context.bot)
                _lc.transition(order_id, OrderLifecycleStatus.COMPLETED, bot=context.bot)
            except Exception:
                logger.exception("Lifecycle COMPLETED failed for order %s", order_id)

        except Exception as e:
            failure = str(e)
            logger.exception("Multi-item checkout failed for order %s", order_id)
            try:
                s.rollback()
            except Exception:
                pass
            # Refund wallet atomically
            try:
                s.query(User).filter(User.id == user_pk).update(
                    {User.wallet_balance: User.wallet_balance + total},
                    synchronize_session=False,
                )
                s.commit()
            except Exception:
                logger.exception("Wallet refund after checkout failure crashed for order %s",
                                 order_id)
                try:
                    s.rollback()
                except Exception:
                    pass
            # Release all inventory reservations (order_id is attached)
            if order_id:
                try:
                    inventory.release_for_order(order_id, reason="checkout_failed")
                except Exception:
                    logger.exception("release_for_order failed for order %s", order_id)
            # Lifecycle: FAILED
            try:
                from services import order_lifecycle as _lc
                _lc.transition(order_id, OrderLifecycleStatus.FAILED,
                               reason=(failure or "")[:200])
            except Exception:
                logger.exception("Lifecycle FAILED transition failed for order %s", order_id)

    # ── Response ─────────────────────────────────────────────────────
    if failure is not None:
        try:
            await q.edit_message_text(
                t("cart.order_failed", lang, amount=_format_price(total), reason=failure),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t("cart.main_menu", lang), callback_data="main_menu")]]),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    header = t("cart.purchase_success_header", lang, order_id=order_id, total=_format_price(total))
    body = "\n\n".join(delivery_summaries) if delivery_summaries else t("cart.delivery_preparing", lang)
    try:
        await q.edit_message_text(
            header + "\n" + body,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("cart.home_button", lang), callback_data="main_menu"),
                InlineKeyboardButton(t("cart.orders_button", lang), callback_data="order_history"),
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

    # ── Bulk .txt delivery(s) as follow-up documents ──────────────────
    for name, keys in bulk_payloads:
        safe = "".join(c for c in name if c.isalnum() or c in ("-", "_"))[:40] or "product"
        filename = f"order_{order_id}_{safe}_keys.txt"
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(keys))
            with open(tmp_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=tg_id,
                    document=InputFile(f, filename=filename),
                    caption=t("cart.bulk_keys_caption", lang, count=len(keys), order_id=order_id),
                )
        except Exception:
            logger.exception("Bulk delivery attach failed for order %s", order_id)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ── Oversized V11-type deliveries (ACCOUNT_LOGIN/REDEEM_LINK/VOUCHER/
    #    etc.) as follow-up documents — same safety net as bulk_payloads
    #    above, generalized to every dispatcher-backed product type. ──────
    if oversized_deliveries:
        from services.purchase_success import send_delivery_as_file
        for name, qty, content in oversized_deliveries:
            try:
                await send_delivery_as_file(
                    context.bot, tg_id, order_id, name, content,
                    caption=t("cart.bulk_file_caption", lang, name=name, qty=qty, order_id=order_id),
                )
            except Exception:
                logger.exception("Oversized delivery attach failed for order %s", order_id)


def register(application):
    application.add_handler(CallbackQueryHandler(cart_view,    pattern="^cart$"))
    application.add_handler(CallbackQueryHandler(cart_add,     pattern=r"^cart_add_\d+$"))
    application.add_handler(CallbackQueryHandler(cart_add_variant,
                                                 pattern=r"^cart_addv_\d+_\d+$"))
    application.add_handler(CallbackQueryHandler(cart_inc,     pattern=r"^cart_inc_\d+$"))
    application.add_handler(CallbackQueryHandler(cart_dec,     pattern=r"^cart_dec_\d+$"))
    application.add_handler(CallbackQueryHandler(cart_remove,  pattern=r"^cart_rm_\d+$"))
    application.add_handler(CallbackQueryHandler(cart_clear,   pattern="^cart_clear$"))
    application.add_handler(CallbackQueryHandler(cart_checkout, pattern="^cart_checkout$"))
    application.add_handler(CallbackQueryHandler(cart_confirm,  pattern="^cart_confirm$"))
    application.add_handler(CallbackQueryHandler(cart_cancel,   pattern="^cart_cancel$"))
