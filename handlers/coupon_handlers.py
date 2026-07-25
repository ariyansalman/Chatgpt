"""Coupon / promo-code handlers — admin CRUD + user apply flow.

User side: on the purchase confirmation screen a "🎟 Apply Coupon" button
opens a conversation, the user types the code, and the discount is applied
to that purchase only. State lives in `context.user_data`:
    purchase_coupon_id : int
    purchase_coupon_code : str
    purchase_coupon_discount : float   (dollar amount)

Admin side: `admin_coupons` opens a list with an Add button and a per-coupon
toggle / delete.
"""

from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import (
    get_db_session, Coupon, CouponRedemption, DiscountType, User,
)
from utils import check_user_banned, is_admin
from telegram.error import BadRequest

# ─── Conversation states ────────────────────────────────────────────
COUPON_CODE_INPUT = 1                                    # user apply
ADD_CODE, ADD_TYPE, ADD_VALUE, ADD_MAX_USES = range(10, 14)  # admin add


# ═══════════════════════════ HELPERS ═══════════════════════════════
def _validate_coupon(code: str, user_id: int, order_total: float):
    """Return (coupon_row, discount_amount, error_msg). Read inside a session."""
    with get_db_session() as session:
        c = session.query(Coupon).filter(Coupon.code.ilike(code.strip())).first()
        if not c:
            return None, 0.0, "❌ Coupon code not found."
        if not c.is_active:
            return None, 0.0, "❌ This coupon is not active."
        if c.expires_at and c.expires_at < datetime.utcnow():
            return None, 0.0, "❌ This coupon has expired."
        if c.max_uses and c.used_count >= c.max_uses:
            return None, 0.0, "❌ This coupon has reached its usage limit."
        if c.min_order_amount and order_total < c.min_order_amount:
            return None, 0.0, f"❌ Minimum order of ${c.min_order_amount:.2f} required."

        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user and c.per_user_limit:
            used = (
                session.query(CouponRedemption)
                .filter_by(coupon_id=c.id, user_id=user.id)
                .count()
            )
            if used >= c.per_user_limit:
                return None, 0.0, "❌ You have already used this coupon."

        if c.discount_type == DiscountType.PERCENT:
            discount = order_total * (c.discount_value / 100.0)
        else:
            discount = float(c.discount_value)
        discount = min(discount, order_total)  # never > total
        # snapshot values before session closes
        return (c.id, c.code), round(discount, 2), ""


def record_coupon_redemption(coupon_id: int, user_db_id: int, order_id: int, discount: float):
    """Called from confirm_purchase AFTER order is created.

    Uses an atomic conditional UPDATE on ``used_count`` so simultaneous
    redemptions can never push the counter past ``max_uses``.
    """
    with get_db_session() as session:
        c = session.query(Coupon).filter_by(id=coupon_id).first()
        if not c:
            return
        # Atomic increment; if max_uses is set, refuse when we're already at cap.
        q = session.query(Coupon).filter(Coupon.id == coupon_id)
        if c.max_uses and c.max_uses > 0:
            q = q.filter(Coupon.used_count < c.max_uses)
        incremented = q.update(
            {Coupon.used_count: (Coupon.used_count or 0) + 1},
            synchronize_session=False,
        )
        if incremented == 0:
            # Cap reached before we got here — do not log redemption.
            return
        session.add(CouponRedemption(
            coupon_id=coupon_id, user_id=user_db_id,
            order_id=order_id, discount_applied=discount,
        ))
        session.commit()


# ═══════════════════════════ USER FLOW ═════════════════════════════
async def apply_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User clicked '🎟 Apply Coupon' on the purchase confirmation screen."""
    query = update.callback_query
    await query.answer()

    if check_user_banned(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ You have been banned.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    if 'purchase_product_id' not in context.user_data:
        try:
            await query.edit_message_text("❌ No active purchase. Start again.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "🎟 <b>Apply Coupon</b>\n\nPlease type your coupon code:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")
            ]]),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return COUPON_CODE_INPUT


async def apply_coupon_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate coupon and re-show the purchase confirmation with discount."""
    code = (update.message.text or "").strip()
    if not code:
        await update.message.reply_text("Please type a coupon code.")
        return COUPON_CODE_INPUT

    price = context.user_data.get('purchase_product_price', 0)
    qty = context.user_data.get('purchase_quantity', 1)
    order_total = price * qty

    result, discount, err = _validate_coupon(code, update.effective_user.id, order_total)
    if err:
        await update.message.reply_text(
            f"{err}\n\nTry again or /cancel.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")
            ]]),
        )
        return COUPON_CODE_INPUT

    coupon_id, coupon_code = result
    context.user_data['purchase_coupon_id'] = coupon_id
    context.user_data['purchase_coupon_code'] = coupon_code
    context.user_data['purchase_coupon_discount'] = discount

    # Re-show confirmation screen via payment_handlers (import lazily to avoid cycle)
    from handlers.payment_handlers import show_purchase_confirmation
    await show_purchase_confirmation(update, context, is_message=True)
    return ConversationHandler.END


# ═══════════════════════════ ADMIN LIST / VIEW ═════════════════════
async def admin_coupons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin coupons dashboard — list all coupons."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        try:
            await query.edit_message_text("⛔ Admin only.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    with get_db_session() as session:
        coupons = session.query(Coupon).order_by(Coupon.id.desc()).limit(50).all()
        rows = [(c.id, c.code, c.is_active, c.discount_type, c.discount_value,
                 c.used_count, c.max_uses) for c in coupons]

    keyboard = []
    for cid, code, active, dtype, dval, used, mx in rows:
        status = "✅" if active else "🚫"
        label = f"{dval:g}%" if dtype == DiscountType.PERCENT else f"${dval:g}"
        usage = f"{used}/{mx or '∞'}"
        keyboard.append([InlineKeyboardButton(
            f"{status} {code} — {label} ({usage})",
            callback_data=f"admin_coupon_view_{cid}",
        )])
    keyboard.append([InlineKeyboardButton("➕ Add Coupon", callback_data="admin_coupon_add")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_settings")])

    text = "🎟 <b>Coupon Management</b>\n\n"
    text += f"Total: {len(rows)} coupon(s)\n" if rows else "No coupons yet.\n"

    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_coupon_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = int(query.data.split("_")[-1])

    with get_db_session() as session:
        c = session.query(Coupon).filter_by(id=cid).first()
        if not c:
            try:
                await query.edit_message_text("❌ Coupon not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        info = {
            "id": c.id, "code": c.code, "active": c.is_active,
            "type": c.discount_type.value, "value": c.discount_value,
            "min": c.min_order_amount, "used": c.used_count, "max": c.max_uses,
            "per_user": c.per_user_limit,
            "expires": c.expires_at.strftime("%Y-%m-%d") if c.expires_at else "Never",
        }

    text = (
        f"🎟 <b>Coupon: {info['code']}</b>\n\n"
        f"Status: {'✅ Active' if info['active'] else '🚫 Disabled'}\n"
        f"Discount: {info['value']:g}{'%' if info['type']=='percent' else ' USD'}\n"
        f"Min order: ${info['min']:.2f}\n"
        f"Used: {info['used']} / {info['max'] or '∞'}\n"
        f"Per user: {info['per_user'] or '∞'}\n"
        f"Expires: {info['expires']}\n"
    )
    toggle_label = "🚫 Disable" if info['active'] else "✅ Enable"
    keyboard = [
        [InlineKeyboardButton(toggle_label, callback_data=f"admin_coupon_toggle_{cid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"admin_coupon_delete_{cid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_coupons")],
    ]
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_coupon_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = int(query.data.split("_")[-1])
    with get_db_session() as session:
        c = session.query(Coupon).filter_by(id=cid).first()
        if c:
            c.is_active = not c.is_active
            session.commit()
    await admin_coupon_view(update, context)


async def admin_coupon_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Deleted")
    cid = int(query.data.split("_")[-1])
    with get_db_session() as session:
        c = session.query(Coupon).filter_by(id=cid).first()
        if c:
            session.delete(c)
            session.commit()
    await admin_coupons_menu(update, context)


# ═══════════════════════════ ADMIN ADD CONVERSATION ════════════════
async def admin_coupon_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        await query.edit_message_text(
            "🎟 <b>New Coupon</b>\n\nEnter the coupon <b>code</b> (letters/numbers, no spaces):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="admin_coupons")
            ]]),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ADD_CODE


async def admin_coupon_add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()
    if not code or " " in code or len(code) > 64:
        await update.message.reply_text("❌ Invalid code. Try again.")
        return ADD_CODE
    with get_db_session() as session:
        if session.query(Coupon).filter(Coupon.code.ilike(code)).first():
            await update.message.reply_text("❌ Code already exists. Enter a different code.")
            return ADD_CODE
    context.user_data['new_coupon_code'] = code
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("% Percent", callback_data="coupontype_percent"),
         InlineKeyboardButton("$ Fixed amount", callback_data="coupontype_amount")],
    ])
    await update.message.reply_text("Choose discount type:", reply_markup=kb)
    return ADD_TYPE


async def admin_coupon_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    t = query.data.split("_")[-1]
    context.user_data['new_coupon_type'] = t
    unit = "percent (e.g. 10 for 10%)" if t == "percent" else "USD amount (e.g. 5)"
    try:
        await query.edit_message_text(f"Enter discount value in {unit}:")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ADD_VALUE


async def admin_coupon_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        v = float((update.message.text or "").strip())
        if v <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return ADD_VALUE
    if context.user_data['new_coupon_type'] == "percent" and v > 100:
        await update.message.reply_text("❌ Percent must be ≤ 100.")
        return ADD_VALUE
    context.user_data['new_coupon_value'] = v
    await update.message.reply_text(
        "Enter max total uses (0 = unlimited):"
    )
    return ADD_MAX_USES


async def admin_coupon_add_max_uses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mx = int((update.message.text or "0").strip())
        if mx < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a non-negative integer.")
        return ADD_MAX_USES

    code = context.user_data.pop('new_coupon_code')
    dtype = context.user_data.pop('new_coupon_type')
    value = context.user_data.pop('new_coupon_value')

    with get_db_session() as session:
        c = Coupon(
            code=code,
            discount_type=DiscountType.PERCENT if dtype == "percent" else DiscountType.AMOUNT,
            discount_value=value,
            max_uses=mx,
            is_active=True,
        )
        session.add(c)
        session.commit()

    await update.message.reply_text(
        f"✅ Coupon <code>{code}</code> created.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Coupons", callback_data="admin_coupons")
        ]]),
    )
    return ConversationHandler.END


async def admin_coupon_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel add conversation (called by callback query)."""
    for k in ('new_coupon_code', 'new_coupon_type', 'new_coupon_value'):
        context.user_data.pop(k, None)
    if update.callback_query:
        await update.callback_query.answer()
    return ConversationHandler.END


# ═══════════════════════════ ADMIN CURRENCY SETTINGS ═══════════════
from database import Settings
CUR_CODE, CUR_SYMBOL, CUR_RATE = 20, 21, 22


async def admin_currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as session:
        s = session.query(Settings).first()
        code = (s.secondary_currency_code if s else "") or "(not set)"
        sym = (s.secondary_currency_symbol if s else "") or ""
        rate = (s.secondary_currency_rate if s else 0.0) or 0.0

    text = (
        "💱 <b>Display Currency</b>\n\n"
        "DB stores everything in USD. If configured, prices are shown as:\n"
        "  $12.50 (~<code>SYM</code>1,375.00)\n\n"
        f"Current code: <b>{code}</b>\n"
        f"Current symbol: <b>{sym or '—'}</b>\n"
        f"Rate (1 USD = ? {code}): <b>{rate:g}</b>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set / Change", callback_data="admin_currency_set")],
        [InlineKeyboardButton("🗑 Clear (USD only)", callback_data="admin_currency_clear")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_settings")],
    ])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_currency_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cleared")
    with get_db_session() as session:
        s = session.query(Settings).first()
        if s:
            s.secondary_currency_code = None
            s.secondary_currency_symbol = None
            s.secondary_currency_rate = 0.0
            session.commit()
    from utils.currency import clear_currency_cache
    clear_currency_cache()
    await admin_currency_menu(update, context)


async def admin_currency_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text(
            "Enter the currency <b>code</b> (e.g. EUR, BDT, INR, GBP):",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return CUR_CODE


async def admin_currency_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()[:8]
    if not code:
        await update.message.reply_text("❌ Try again.")
        return CUR_CODE
    context.user_data['cur_code'] = code
    await update.message.reply_text(f"Enter symbol for {code} (e.g. €, ৳, ₹, £). Type - to reuse the code:")
    return CUR_SYMBOL


async def admin_currency_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sym = (update.message.text or "").strip()[:8]
    if sym == "-":
        sym = context.user_data.get('cur_code', '')
    context.user_data['cur_sym'] = sym
    await update.message.reply_text(
        f"Enter the rate — how many {context.user_data['cur_code']} equals 1 USD? (e.g. 110 or 0.92):"
    )
    return CUR_RATE


async def admin_currency_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float((update.message.text or "").strip())
        if rate <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return CUR_RATE

    code = context.user_data.pop('cur_code')
    sym = context.user_data.pop('cur_sym')

    with get_db_session() as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings()
            session.add(s)
        s.secondary_currency_code = code
        s.secondary_currency_symbol = sym
        s.secondary_currency_rate = rate
        session.commit()

    from utils.currency import clear_currency_cache
    clear_currency_cache()

    await update.message.reply_text(
        f"✅ Saved. Prices will now show `$X.XX (~{sym}Y.YY)` where 1 USD = {rate:g} {code}.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="admin_currency")
        ]]),
    )
    return ConversationHandler.END
