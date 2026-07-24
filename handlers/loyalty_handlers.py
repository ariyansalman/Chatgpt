"""Loyalty points: view balance, redeem for wallet credit, award on purchase."""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import get_db_session, User, Settings, LoyaltyLedger
from utils.helpers import format_price
from i18n import t, get_user_language
from telegram.error import BadRequest

REDEEM_AMOUNT = 5001


def _get_settings(session):
    s = session.query(Settings).first()
    if not s:
        s = Settings()
        session.add(s)
        session.commit()
    return s


def award_loyalty_points(session, user: User, order_id: int, amount_spent: float) -> int:
    """Award points on purchase. Called from payment_handlers within same session/txn.

    Returns number of points awarded (0 if disabled).
    """
    s = _get_settings(session)
    if not s.loyalty_enabled or s.loyalty_earn_rate <= 0 or amount_spent <= 0:
        return 0
    pts = int(amount_spent * float(s.loyalty_earn_rate))
    if pts <= 0:
        return 0
    user.loyalty_points = (user.loyalty_points or 0) + pts
    session.add(LoyaltyLedger(
        user_id=user.id, change=pts, balance_after=user.loyalty_points,
        reason="purchase", order_id=order_id,
    ))
    # V41 — non-breaking: auto-upgrade VIP tier after awarding points
    try:
        from services.vip_service import check_and_upgrade
        check_and_upgrade(session, user)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).debug("vip check_and_upgrade skipped", exc_info=True)
    return pts


# ─── User-facing menu ────────────────────────────────────────────────
async def loyalty_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    with get_db_session() as session:
        s = _get_settings(session)
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            try:
                await query.edit_message_text(t("common.user_not_found", lang))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        pts = user.loyalty_points or 0
        if not s.loyalty_enabled:
            try:
                await query.edit_message_text(
                    t("loyalty.disabled", lang),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t("common.main_menu", lang), callback_data="main_menu")]]),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        redeem_rate = float(s.loyalty_redeem_rate or 100)
        earn_rate = float(s.loyalty_earn_rate or 1)
        min_r = int(s.loyalty_min_redeem or 100)
        equiv = pts / redeem_rate if redeem_rate > 0 else 0
        msg = t(
            "loyalty.body", lang,
            points=pts, equiv=format_price(equiv),
            earn_rate=f"{earn_rate:g}", redeem_rate=f"{redeem_rate:g}", min_redeem=min_r,
        )
        kb = []
        if pts >= min_r:
            kb.append([InlineKeyboardButton(t("loyalty.redeem_button", lang), callback_data="loyalty_redeem")])
        kb.append([InlineKeyboardButton(t("common.main_menu", lang), callback_data="main_menu")])
        try:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def loyalty_redeem_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    with get_db_session() as session:
        s = _get_settings(session)
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user or (user.loyalty_points or 0) < int(s.loyalty_min_redeem or 100):
            try:
                await query.edit_message_text(t("loyalty.not_enough_points", lang))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        try:
            await query.edit_message_text(
                t(
                    "loyalty.redeem_prompt", lang,
                    points=user.loyalty_points, min_redeem=int(s.loyalty_min_redeem),
                    redeem_rate=int(s.loyalty_redeem_rate),
                )
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return REDEEM_AMOUNT


async def loyalty_redeem_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    txt = (update.message.text or "").strip()
    try:
        pts_req = int(txt)
    except ValueError:
        await update.message.reply_text(t("common.enter_whole_number", lang))
        return REDEEM_AMOUNT

    with get_db_session() as session:
        s = _get_settings(session)
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            await update.message.reply_text(t("common.user_not_found", lang))
            return ConversationHandler.END

        rate = float(s.loyalty_redeem_rate or 100)
        min_r = int(s.loyalty_min_redeem or 100)
        if pts_req < min_r:
            await update.message.reply_text(t("loyalty.min_points", lang, min_redeem=min_r))
            return REDEEM_AMOUNT
        if pts_req > (user.loyalty_points or 0):
            await update.message.reply_text(t("loyalty.not_enough_points_short", lang))
            return REDEEM_AMOUNT
        credit = pts_req / rate
        if credit <= 0:
            await update.message.reply_text(t("common.invalid_amount", lang))
            return REDEEM_AMOUNT

        user.loyalty_points -= pts_req
        user.wallet_balance = (user.wallet_balance or 0) + credit
        session.add(LoyaltyLedger(
            user_id=user.id, change=-pts_req, balance_after=user.loyalty_points,
            reason="redeem",
        ))
        session.commit()
        await update.message.reply_text(
            t(
                "loyalty.redeemed", lang,
                points=pts_req, credit=format_price(credit),
                remaining=user.loyalty_points, wallet=format_price(user.wallet_balance),
            )
        )
    return ConversationHandler.END


async def loyalty_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    return ConversationHandler.END


# ─── Admin: toggle & tune loyalty ────────────────────────────────────
async def admin_loyalty_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with get_db_session() as session:
        s = _get_settings(session)
        status = "✅ Enabled" if s.loyalty_enabled else "🚫 Disabled"
        msg = (
            "🎁 Loyalty Program Settings\n\n"
            f"Status: {status}\n"
            f"Earn rate: {s.loyalty_earn_rate:g} pt / $1\n"
            f"Redeem rate: {s.loyalty_redeem_rate:g} pts = $1\n"
            f"Min redeem: {s.loyalty_min_redeem} pts"
        )
        kb = [
            [InlineKeyboardButton("🔁 Toggle Enabled/Disabled", callback_data="admin_loy_toggle")],
            [InlineKeyboardButton("✏️ Set Earn Rate", callback_data="admin_loy_earn")],
            [InlineKeyboardButton("✏️ Set Redeem Rate", callback_data="admin_loy_redeem")],
            [InlineKeyboardButton("✏️ Set Min Redeem", callback_data="admin_loy_min")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_settings")],
        ]
        try:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_loyalty_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with get_db_session() as session:
        s = _get_settings(session)
        s.loyalty_enabled = not bool(s.loyalty_enabled)
        session.commit()
    await admin_loyalty_menu(update, context)


LOY_SET_EARN, LOY_SET_REDEEM, LOY_SET_MIN = 5101, 5102, 5103


async def admin_loyalty_set_earn(update, context):
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text("Enter new EARN rate (points per $1, e.g. 2):")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return LOY_SET_EARN


async def admin_loyalty_set_redeem(update, context):
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text("Enter new REDEEM rate (points required per $1, e.g. 100):")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return LOY_SET_REDEEM


async def admin_loyalty_set_min(update, context):
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text("Enter minimum points required to redeem (e.g. 100):")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return LOY_SET_MIN


async def _admin_loy_apply(update, field, cast, validate_msg):
    try:
        val = cast((update.message.text or "").strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(validate_msg)
        return
    with get_db_session() as session:
        s = _get_settings(session)
        setattr(s, field, val)
        session.commit()
    await update.message.reply_text(f"✅ Updated {field} → {val}")


async def admin_loyalty_earn_input(update, context):
    await _admin_loy_apply(update, "loyalty_earn_rate", float, "❌ Enter a positive number.")
    return ConversationHandler.END


async def admin_loyalty_redeem_input(update, context):
    await _admin_loy_apply(update, "loyalty_redeem_rate", float, "❌ Enter a positive number.")
    return ConversationHandler.END


async def admin_loyalty_min_input(update, context):
    await _admin_loy_apply(update, "loyalty_min_redeem", int, "❌ Enter a positive whole number.")
    return ConversationHandler.END
