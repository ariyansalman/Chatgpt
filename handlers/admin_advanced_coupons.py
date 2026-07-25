"""Admin Advanced Coupon System — V21.

Extends the existing coupon system with:
  - Percentage / Fixed / Free-product discount types
  - One-time or unlimited use; per-user limits
  - User-specific, product-specific, category-specific targeting
  - Min purchase; max discount cap
  - Expiry and activation dates
  - Automatic / referral / birthday coupon types
  - Full statistics per coupon

Callback namespace: ``acpn:*``
All existing coupon callbacks (``admin_coupons``, ``admin_coupon_*``) are
preserved.  This module adds NEW coupons only accessible through the
``acpn:*`` namespace.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import get_db_session, Coupon, CouponRedemption, Product, Category, User
from database.models import DiscountType
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 10

# Conversation states
(
    ACPN_CODE, ACPN_TYPE, ACPN_VALUE, ACPN_MIN_PURCHASE,
    ACPN_MAX_DISCOUNT, ACPN_MAX_USES, ACPN_PER_USER,
    ACPN_TARGET, ACPN_TARGET_IDS, ACPN_ACTIVATION,
    ACPN_EXPIRY, ACPN_COUPON_TYPE,
) = range(12)


def _is_admin(uid: int) -> bool:
    return uid == settings.ADMIN_TELEGRAM_ID or has_permission(uid, "manage_settings")


def _enabled() -> bool:
    return cfg.get_bool("feature_advanced_coupons_enabled", True)


async def _safe_edit(query, text: str, kb=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(data="acpn:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


# ── Menu ──────────────────────────────────────────────────────────────────

async def acpn_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await _safe_edit(query, "🏷 <b>Advanced Coupons</b>\n\n❌ Feature disabled.", _back_kb("acc:root"))
        return
    await _render_coupon_list(update, context, 0)


async def _render_coupon_list(update, context, page: int):
    query = update.callback_query
    with get_db_session() as s:
        q = s.query(Coupon).order_by(Coupon.created_at.desc())
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = []
        for c in rows:
            redemptions = s.query(func.count(CouponRedemption.id)).filter(
                CouponRedemption.coupon_id == c.id).scalar() or 0
            items.append((c.id, c.code, c.discount_type, c.discount_value,
                          c.is_active, redemptions, getattr(c, 'coupon_type', 'manual')))

    lines = [f"🏷 <b>Advanced Coupons</b>  ({total} total)\n"]
    kb = []
    for cid, code, dtype, dval, active, redemptions, ctype in items:
        icon = "✅" if active else "❌"
        type_icon = {"manual": "🏷", "automatic": "⚡", "referral": "👥", "birthday": "🎂"}.get(ctype or "manual", "🏷")
        disc_str = f"{dval}%" if dtype == DiscountType.PERCENT else f"${dval:.2f}"
        kb.append([InlineKeyboardButton(
            f"{icon} {type_icon} {code}  {disc_str}  ({redemptions} uses)",
            callback_data=f"acpn:view:{cid}",
        )])
    if not items:
        lines.append("No coupons yet.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"acpn:list:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"acpn:list:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("➕ Create Advanced Coupon", callback_data="acpn:new")])
    kb.append([InlineKeyboardButton("📊 Coupon Stats Overview", callback_data="acpn:stats")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Coupon detail view ────────────────────────────────────────────────────

async def acpn_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        cid = int(_override) if _override else int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return await acpn_menu(update, context)

    with get_db_session() as s:
        c = s.get(Coupon, cid)
        if not c:
            await query.answer("❌ Not found.", show_alert=True)
            return
        redemptions = s.query(func.count(CouponRedemption.id)).filter(
            CouponRedemption.coupon_id == cid).scalar() or 0
        total_discount = s.query(func.coalesce(func.sum(CouponRedemption.discount_applied), 0.0)).filter(
            CouponRedemption.coupon_id == cid).scalar() or 0.0
        unique_users = s.query(func.count(func.distinct(CouponRedemption.user_id))).filter(
            CouponRedemption.coupon_id == cid).scalar() or 0

        disc_str = (f"{c.discount_value}%" if c.discount_type == DiscountType.PERCENT
                    else f"${c.discount_value:.2f} off")
        expires_str = c.expires_at.strftime("%Y-%m-%d") if c.expires_at else "Never"
        activation_str = getattr(c, 'activation_date', None)
        activation_str = activation_str.strftime("%Y-%m-%d") if activation_str else "Now"
        target_uid = getattr(c, 'target_user_id', None)
        product_ids = getattr(c, 'product_ids', None)
        category_ids = getattr(c, 'category_ids', None)
        max_discount = getattr(c, 'max_discount_amount', None)
        coupon_type = getattr(c, 'coupon_type', 'manual')
        free_pid = getattr(c, 'free_product_id', None)

    type_icon = {"manual": "🏷", "automatic": "⚡", "referral": "👥", "birthday": "🎂"}.get(coupon_type or "manual", "🏷")
    text = (
        f"🏷 <b>Coupon: <code>{c.code}</code></b>\n\n"
        f"<b>Type:</b> {type_icon} {coupon_type}  |  "
        f"<b>Status:</b> {'✅ Active' if c.is_active else '❌ Inactive'}\n"
        f"<b>Discount:</b> {disc_str}"
        + (f"  (cap: ${max_discount:.2f})" if max_discount else "") + "\n"
        f"<b>Min Purchase:</b> ${c.min_order_amount:.2f}\n"
        f"<b>Max Uses:</b> {c.max_uses or '∞'}  |  <b>Used:</b> {c.used_count}\n"
        f"<b>Per-User Limit:</b> {c.per_user_limit or '∞'}\n"
        f"<b>Activation:</b> {activation_str}  |  <b>Expires:</b> {expires_str}\n"
    )
    if target_uid:
        text += f"<b>Target User:</b> #{target_uid}\n"
    if product_ids:
        text += f"<b>Products:</b> {product_ids}\n"
    if category_ids:
        text += f"<b>Categories:</b> {category_ids}\n"
    if free_pid:
        text += f"<b>Free Product ID:</b> #{free_pid}\n"

    text += (
        f"\n<b>Stats:</b>\n"
        f"  Redemptions: {redemptions}  |  Unique users: {unique_users}\n"
        f"  Total discount given: ${total_discount:.2f}\n"
    )

    kb = [
        [InlineKeyboardButton("🔄 Toggle Active", callback_data=f"acpn:toggle:{cid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"acpn:del_ask:{cid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="acpn:menu")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def acpn_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        c = s.get(Coupon, cid)
        if not c:
            return
        c.is_active = not c.is_active
        new_state = c.is_active
        s.commit()
    log_admin_action(update.effective_user.id, "coupon.toggle", "coupon", cid,
                     new_value=str(new_state), module="advanced_coupons")
    await query.answer("✅ Toggled." if new_state else "❌ Disabled.")
    context.user_data["_cb_data_override"] = str(cid)
    await acpn_view(update, context)


async def acpn_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Confirm Delete", callback_data=f"acpn:del_ok:{cid}"),
         InlineKeyboardButton("🔙 Cancel", callback_data=f"acpn:view:{cid}")],
    ])
    await _safe_edit(query, f"⚠️ Delete coupon #{cid}? Redemption history will be lost.", kb)


async def acpn_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        c = s.get(Coupon, cid)
        if c:
            # Delete redemptions first
            s.query(CouponRedemption).filter(CouponRedemption.coupon_id == cid).delete()
            s.delete(c)
            s.commit()
    log_admin_action(update.effective_user.id, "coupon.delete", "coupon", cid,
                     module="advanced_coupons")
    await _safe_edit(query, f"🗑 Coupon #{cid} deleted.", _back_kb("acpn:menu"))


# ── Stats overview ────────────────────────────────────────────────────────

async def acpn_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    with get_db_session() as s:
        total = s.query(func.count(Coupon.id)).scalar() or 0
        active = s.query(func.count(Coupon.id)).filter(Coupon.is_active == True).scalar() or 0  # noqa: E712
        redemptions = s.query(func.count(CouponRedemption.id)).scalar() or 0
        discount_total = s.query(func.coalesce(func.sum(CouponRedemption.discount_applied), 0.0)).scalar() or 0.0
        top = (s.query(
            Coupon.code,
            func.count(CouponRedemption.id).label("uses"),
        ).join(CouponRedemption, CouponRedemption.coupon_id == Coupon.id)
         .group_by(Coupon.id, Coupon.code)
         .order_by(func.count(CouponRedemption.id).desc())
         .limit(5).all())

    lines = [
        f"📊 <b>Coupon Stats Overview</b>\n{'─' * 30}",
        f"Total Coupons:   <b>{total}</b>",
        f"Active:          <b>{active}</b>",
        f"Total Redeemed:  <b>{redemptions:,}</b>",
        f"Discount Given:  <b>${discount_total:.2f}</b>\n",
        "<b>Top 5 Coupons:</b>",
    ]
    for code, uses in top:
        lines.append(f"  • <code>{code}</code>: {uses} uses")

    await _safe_edit(query, "\n".join(lines), _back_kb("acpn:menu"))


# ── Create advanced coupon (conversation) ────────────────────────────────

async def acpn_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not _enabled():
        await query.answer("Feature disabled.", show_alert=True)
        return ConversationHandler.END
    context.user_data["_acpn"] = {}
    await _safe_edit(query,
        "🏷 <b>New Advanced Coupon — Step 1/9</b>\n\n"
        "Send the coupon <b>code</b> (letters, numbers, dashes, underscores):",
        _back_kb())
    return ACPN_CODE


async def acpn_recv_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()
    if not code or len(code) > 32:
        await update.message.reply_text("❌ Code must be 1–32 characters. Try again:")
        return ACPN_CODE
    # Check uniqueness
    with get_db_session() as s:
        exists = s.query(Coupon).filter(Coupon.code == code).first()
    if exists:
        await update.message.reply_text("❌ Code already exists. Try another:")
        return ACPN_CODE
    context.user_data["_acpn"]["code"] = code
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("% Percentage", callback_data="acpn:t:percent"),
         InlineKeyboardButton("$ Fixed Amount", callback_data="acpn:t:amount")],
        [InlineKeyboardButton("🎁 Free Product", callback_data="acpn:t:free_product")],
    ])
    await update.message.reply_text(
        "🏷 <b>Step 2/9 — Discount Type</b>\n\nChoose the discount type:",
        reply_markup=kb, parse_mode="HTML")
    return ACPN_TYPE


async def acpn_recv_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dtype = query.data.split(":")[2]  # percent|amount|free_product
    context.user_data["_acpn"]["discount_type"] = dtype
    if dtype == "free_product":
        await _safe_edit(query,
            "🏷 <b>Step 3/9 — Free Product ID</b>\n\n"
            "Send the <b>product ID</b> to give for free:",
            _back_kb())
    else:
        label = "percentage (e.g. 10 for 10%)" if dtype == "percent" else "fixed amount (e.g. 5.00)"
        await _safe_edit(query,
            f"🏷 <b>Step 3/9 — Discount Value</b>\n\nSend the {label}:",
            _back_kb())
    return ACPN_VALUE


async def acpn_recv_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        val = float(txt)
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid value. Send a positive number:")
        return ACPN_VALUE
    dtype = context.user_data["_acpn"]["discount_type"]
    if dtype == "free_product":
        context.user_data["_acpn"]["free_product_id"] = int(val)
        context.user_data["_acpn"]["discount_value"] = 0.0
    else:
        context.user_data["_acpn"]["discount_value"] = val
    await update.message.reply_text(
        "🏷 <b>Step 4/9 — Min Purchase Amount</b>\n\n"
        "Send the minimum order amount for this coupon (or <code>0</code> for no minimum):",
        parse_mode="HTML")
    return ACPN_MIN_PURCHASE


async def acpn_recv_min_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        val = max(0.0, float(txt))
    except ValueError:
        await update.message.reply_text("❌ Invalid. Send a number (e.g. 0 or 10):")
        return ACPN_MIN_PURCHASE
    context.user_data["_acpn"]["min_order_amount"] = val
    await update.message.reply_text(
        "🏷 <b>Step 5/9 — Max Discount Cap</b>\n\n"
        "For percentage coupons: max discount in $ (or <code>0</code> for unlimited):",
        parse_mode="HTML")
    return ACPN_MAX_DISCOUNT


async def acpn_recv_max_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        val = max(0.0, float(txt))
    except ValueError:
        val = 0.0
    context.user_data["_acpn"]["max_discount_amount"] = val if val > 0 else None
    await update.message.reply_text(
        "🏷 <b>Step 6/9 — Max Uses (global)</b>\n\n"
        "Max total uses across all users (or <code>0</code> for unlimited):",
        parse_mode="HTML")
    return ACPN_MAX_USES


async def acpn_recv_max_uses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        val = max(0, int(txt))
    except ValueError:
        val = 0
    context.user_data["_acpn"]["max_uses"] = val
    await update.message.reply_text(
        "🏷 <b>Step 7/9 — Per-User Limit</b>\n\n"
        "Max uses per single user (or <code>0</code> for unlimited):",
        parse_mode="HTML")
    return ACPN_PER_USER


async def acpn_recv_per_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        val = max(0, int(txt))
    except ValueError:
        val = 1
    context.user_data["_acpn"]["per_user_limit"] = val
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users", callback_data="acpn:tgt:all"),
         InlineKeyboardButton("👤 Specific User", callback_data="acpn:tgt:user")],
        [InlineKeyboardButton("📦 Specific Products", callback_data="acpn:tgt:products"),
         InlineKeyboardButton("📂 Specific Categories", callback_data="acpn:tgt:categories")],
    ])
    await update.message.reply_text(
        "🏷 <b>Step 8/9 — Targeting</b>\n\nWho/what can use this coupon?",
        reply_markup=kb, parse_mode="HTML")
    return ACPN_TARGET


async def acpn_recv_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tgt = query.data.split(":")[2]
    context.user_data["_acpn"]["_target_type"] = tgt
    if tgt == "all":
        context.user_data["_acpn"]["target_user_id"] = None
        context.user_data["_acpn"]["product_ids"] = None
        context.user_data["_acpn"]["category_ids"] = None
        return await _ask_coupon_type(query)
    labels = {
        "user": "user Telegram ID (e.g. 123456789)",
        "products": "product IDs separated by commas (e.g. 1,2,3)",
        "categories": "category IDs separated by commas (e.g. 1,2)",
    }
    await _safe_edit(query,
        f"🏷 <b>Step 8b — Target IDs</b>\n\nSend the {labels.get(tgt, 'IDs')}:",
        _back_kb())
    return ACPN_TARGET_IDS


async def acpn_recv_target_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    tgt = context.user_data["_acpn"].get("_target_type", "all")
    if tgt == "user":
        try:
            context.user_data["_acpn"]["target_user_id"] = int(txt)
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Send a number:")
            return ACPN_TARGET_IDS
        context.user_data["_acpn"]["product_ids"] = None
        context.user_data["_acpn"]["category_ids"] = None
    elif tgt == "products":
        context.user_data["_acpn"]["product_ids"] = txt
        context.user_data["_acpn"]["target_user_id"] = None
        context.user_data["_acpn"]["category_ids"] = None
    elif tgt == "categories":
        context.user_data["_acpn"]["category_ids"] = txt
        context.user_data["_acpn"]["target_user_id"] = None
        context.user_data["_acpn"]["product_ids"] = None
    # Ask coupon type via reply
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Manual", callback_data="acpn:ctype:manual"),
         InlineKeyboardButton("⚡ Automatic", callback_data="acpn:ctype:automatic")],
        [InlineKeyboardButton("👥 Referral", callback_data="acpn:ctype:referral"),
         InlineKeyboardButton("🎂 Birthday", callback_data="acpn:ctype:birthday")],
    ])
    await update.message.reply_text(
        "🏷 <b>Step 9/9 — Coupon Type</b>\n\n"
        "Select the coupon type:\n"
        "• <b>Manual</b>: user enters code manually\n"
        "• <b>Automatic</b>: auto-applied at checkout\n"
        "• <b>Referral</b>: given to referred users\n"
        "• <b>Birthday</b>: auto-issued on user's birthday",
        reply_markup=kb, parse_mode="HTML")
    return ACPN_COUPON_TYPE


async def _ask_coupon_type(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Manual", callback_data="acpn:ctype:manual"),
         InlineKeyboardButton("⚡ Automatic", callback_data="acpn:ctype:automatic")],
        [InlineKeyboardButton("👥 Referral", callback_data="acpn:ctype:referral"),
         InlineKeyboardButton("🎂 Birthday", callback_data="acpn:ctype:birthday")],
    ])
    await _safe_edit(query,
        "🏷 <b>Step 9/9 — Coupon Type</b>\n\n"
        "Select the coupon type:\n"
        "• <b>Manual</b>: user enters code manually\n"
        "• <b>Automatic</b>: auto-applied at checkout\n"
        "• <b>Referral</b>: given to referred users\n"
        "• <b>Birthday</b>: auto-issued on birthday",
        kb)
    return ACPN_COUPON_TYPE


async def acpn_recv_coupon_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctype = query.data.split(":")[2]
    context.user_data["_acpn"]["coupon_type"] = ctype
    await _save_coupon(query, context, update.effective_user.id)
    return ConversationHandler.END


async def _save_coupon(query, context, admin_id: int):
    data = context.user_data.get("_acpn", {})
    dtype_str = data.get("discount_type", "percent")
    dtype = DiscountType.PERCENT if dtype_str == "percent" else DiscountType.AMOUNT

    with get_db_session() as s:
        c = Coupon(
            code=data.get("code", "CODE"),
            discount_type=dtype,
            discount_value=data.get("discount_value", 0.0),
            min_order_amount=data.get("min_order_amount", 0.0),
            max_uses=data.get("max_uses", 0),
            per_user_limit=data.get("per_user_limit", 1),
            is_active=True,
        )
        # Set advanced fields if columns exist
        for attr, key in [
            ("max_discount_amount", "max_discount_amount"),
            ("target_user_id", "target_user_id"),
            ("product_ids", "product_ids"),
            ("category_ids", "category_ids"),
            ("coupon_type", "coupon_type"),
            ("free_product_id", "free_product_id"),
        ]:
            val = data.get(key)
            if val is not None:
                try:
                    setattr(c, attr, val)
                except Exception:
                    pass
        s.add(c)
        s.commit()
        cid = c.id
        code = c.code

    log_admin_action(admin_id, "coupon.create_advanced", "coupon", cid,
                     f"code={code} type={dtype_str}", module="advanced_coupons")
    context.user_data.pop("_acpn", None)
    await _safe_edit(query,
        f"✅ <b>Coupon <code>{code}</code> created!</b> (id {cid})",
        _back_kb("acpn:menu"))


async def acpn_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_acpn", None)
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Cancelled.", _back_kb("acpn:menu"))
    return ConversationHandler.END


def build_acpn_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(acpn_new_start, pattern=r"^acpn:new$")],
        states={
            ACPN_CODE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_code)],
            ACPN_TYPE:        [CallbackQueryHandler(acpn_recv_type, pattern=r"^acpn:t:")],
            ACPN_VALUE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_value)],
            ACPN_MIN_PURCHASE:[MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_min_purchase)],
            ACPN_MAX_DISCOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_max_discount)],
            ACPN_MAX_USES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_max_uses)],
            ACPN_PER_USER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_per_user)],
            ACPN_TARGET:      [CallbackQueryHandler(acpn_recv_target, pattern=r"^acpn:tgt:")],
            ACPN_TARGET_IDS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, acpn_recv_target_ids)],
            ACPN_COUPON_TYPE: [CallbackQueryHandler(acpn_recv_coupon_type, pattern=r"^acpn:ctype:")],
        },
        fallbacks=[
            CallbackQueryHandler(acpn_cancel_conv, pattern=r"^acpn:menu$"),
            CommandHandler("cancel", acpn_cancel_conv),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ── Dispatcher ────────────────────────────────────────────────────────────

async def acpn_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    dispatch_map = {
        "menu": acpn_menu,
        "view": acpn_view,
        "toggle": acpn_toggle,
        "del_ask": acpn_delete_ask,
        "del_ok": acpn_delete_ok,
        "stats": acpn_stats,
        "list": lambda u, c: _render_coupon_list(u, c, int(parts[2]) if len(parts) > 2 else 0),
    }
    fn = dispatch_map.get(action, acpn_menu)
    await fn(update, context)
