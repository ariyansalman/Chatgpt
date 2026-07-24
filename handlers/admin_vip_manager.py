"""V41 — Admin VIP Tier Manager.

Callback namespace: ``vip:*``

Callbacks handled
─────────────────
vip:menu                          — Main dashboard
vip:tiers                         — Tier list
vip:tier:ID                       — Tier detail
vip:tier_del_ask:ID               — Delete confirm
vip:tier_del_ok:ID                — Execute delete
vip:rewards                       — Reward catalog
vip:reward:ID                     — Reward detail
vip:reward_del_ask:ID             — Delete reward confirm
vip:reward_del_ok:ID              — Execute delete reward
vip:users:PAGE                    — VIP users list
vip:user:USER_ID                  — VIP user detail
vip:promote:USER_ID:TIER_ID       — Promote user
vip:demote:USER_ID:TIER_ID        — Demote to specific tier
vip:pts_rst:USER_ID               — Reset points confirm
vip:pts_rst_ok:USER_ID            — Execute reset
vip:history:USER_ID               — VIP tier history for user
vip:stats                         — Global VIP statistics
vip:settings                      — Settings panel
vip:settings:status:VAL           — Set VIP status
vip:settings:toggle:KEY           — Toggle bool config key

ConversationHandler entries:
  vip:tier_new                    — Create tier wizard
  vip:tier_edit:ID                — Edit tier field wizard
  vip:reward_new                  — Create reward wizard
  vip:pts_add:USER_ID             — Add points to user
  vip:pts_rm:USER_ID              — Remove points from user
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import (
    User, VipTier, UserVipTier, VipTierHistory,
    LoyaltyReward, LoyaltyRewardClaim, LoyaltyLedger,
)
from services import vip_service
from utils.helpers import is_admin, format_price
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# ─── constants ───────────────────────────────────────────────────────────────
_PAGE_SIZE = 8

# ConversationHandler states
_S_TIER_NAME   = 7001
_S_TIER_EMOJI  = 7002
_S_TIER_LEVEL  = 7003
_S_TIER_REQS   = 7004
_S_TIER_BENS   = 7005
_S_TIER_EDIT_V = 7006
_S_RWD_NAME    = 7010
_S_RWD_TYPE    = 7011
_S_RWD_COST    = 7012
_S_RWD_VALUE   = 7013
_S_PTS_AMT     = 7020
_S_PTS_RM_AMT  = 7021

_BOOL_SETTINGS = [
    ("vip_auto_upgrade",          "🔼 Auto Upgrade"),
    ("vip_auto_downgrade",        "🔽 Auto Downgrade"),
    ("vip_cashback_enabled",      "💰 Cashback"),
    ("vip_referral_bonus_enabled","👥 Referral Bonus"),
]


# ─── helpers ─────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                try:
                    await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
    else:
        msg = getattr(update, "message", None)
        if msg:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")


def _back_btn(label: str = "🔙 Back", data: str = "vip:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _status_label(key: str = "vip_status") -> str:
    s = cfg.get_str(key, "enabled")
    return {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance",
            "disabled": "🔴 Disabled"}.get(s, s)


# ─── main menu ───────────────────────────────────────────────────────────────

async def vip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return

    with get_db_session() as session:
        total_tiers = session.query(VipTier).filter_by(is_active=True).count()
        total_users = session.query(UserVipTier).count()
        total_rewards = session.query(LoyaltyReward).filter_by(is_active=True).count()

    text = (
        "🏆 <b>VIP Tier Manager</b>\n\n"
        f"Status: {_status_label()}\n"
        f"Active Tiers: <b>{total_tiers}</b>\n"
        f"VIP Users: <b>{total_users}</b>\n"
        f"Active Rewards: <b>{total_rewards}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Manage Tiers", callback_data="vip:tiers"),
         InlineKeyboardButton("🎁 Rewards", callback_data="vip:rewards")],
        [InlineKeyboardButton("👥 VIP Users", callback_data="vip:users:0"),
         InlineKeyboardButton("📊 Statistics", callback_data="vip:stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="vip:settings")],
        [InlineKeyboardButton("➕ New Tier", callback_data="vip:tier_new"),
         InlineKeyboardButton("➕ New Reward", callback_data="vip:reward_new")],
        [_back_btn("🔙 Admin Panel", "admin_menu")],
    ])
    await _send(update, text, kb)


# ─── tier list ───────────────────────────────────────────────────────────────

async def vip_tiers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return

    with get_db_session() as session:
        tiers = session.query(VipTier).order_by(VipTier.level).all()
        rows = [
            (t.id, t.emoji, t.name, t.level, t.is_active, t.is_default)
            for t in tiers
        ]

    lines = []
    buttons = []
    for tid, emoji, name, level, active, is_def in rows:
        tag = " ⭐ [DEFAULT]" if is_def else ""
        status = "🟢" if active else "🔴"
        lines.append(f"{status} {emoji} <b>{name}</b> (Level {level}){tag}")
        buttons.append([InlineKeyboardButton(
            f"{emoji} {name}", callback_data=f"vip:tier:{tid}"
        )])

    text = "⭐ <b>VIP Tiers</b>\n\n" + ("\n".join(lines) if lines else "No tiers defined.")
    buttons.append([
        InlineKeyboardButton("➕ New Tier", callback_data="vip:tier_new"),
        _back_btn("🔙 Back", "vip:menu"),
    ])
    await _send(update, text, InlineKeyboardMarkup(buttons))


# ─── tier detail ─────────────────────────────────────────────────────────────

async def vip_tier_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return

    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return

    with get_db_session() as session:
        t = session.query(VipTier).filter_by(id=tier_id).first()
        if not t:
            await q.answer("Not found", show_alert=True)
            return
        user_count = session.query(UserVipTier).filter_by(tier_id=t.id).count()
        benefits = []
        if t.discount_pct > 0:
            benefits.append(f"🏷 Product Discount: {t.discount_pct:.1f}%")
        if t.cashback_pct > 0:
            benefits.append(f"💰 Cashback: {t.cashback_pct:.1f}%")
        if t.referral_bonus_pct > 0:
            benefits.append(f"👥 Referral Bonus: +{t.referral_bonus_pct:.1f}%")
        if t.extra_coupon_discount_pct > 0:
            benefits.append(f"🎟 Extra Coupon Discount: {t.extra_coupon_discount_pct:.1f}%")
        if t.priority_support:
            benefits.append("🎫 Priority Support")
        if t.priority_delivery:
            benefits.append("🚀 Priority Delivery")
        if t.exclusive_products:
            benefits.append("🔐 Exclusive Products")
        if t.exclusive_flash_sales:
            benefits.append("⚡ Exclusive Flash Sales")
        if t.withdrawal_limit_multiplier != 1.0:
            benefits.append(f"💸 Withdrawal Limit: ×{t.withdrawal_limit_multiplier:.1f}")
        if t.wallet_limit_multiplier != 1.0:
            benefits.append(f"👛 Wallet Limit: ×{t.wallet_limit_multiplier:.1f}")
        custom = []
        if t.custom_benefits:
            try:
                custom = json.loads(t.custom_benefits)
            except Exception:
                pass
        for cb in custom:
            benefits.append(f"✨ {cb}")

        reqs = []
        if t.min_orders > 0:
            reqs.append(f"🛒 Min Orders: {t.min_orders}")
        if t.min_spending > 0:
            reqs.append(f"💳 Min Spending: ${t.min_spending:,.2f}")
        if t.min_referral_earnings > 0:
            reqs.append(f"👥 Referral Earnings: ${t.min_referral_earnings:,.2f}")
        if t.min_account_age_days > 0:
            reqs.append(f"📅 Account Age: {t.min_account_age_days} days")

        text = (
            f"{t.emoji} <b>{t.name}</b> — Level {t.level}\n"
            f"Status: {'🟢 Active' if t.is_active else '🔴 Inactive'}"
            f"{'  ⭐ DEFAULT' if t.is_default else ''}\n"
            f"Users at this tier: <b>{user_count}</b>\n\n"
        )
        if reqs:
            text += "<b>Upgrade Requirements</b>\n" + "\n".join(reqs) + "\n\n"
        else:
            text += "<b>Upgrade Requirements</b>\n(No requirements — base tier)\n\n"
        if benefits:
            text += "<b>Benefits</b>\n" + "\n".join(benefits)
        else:
            text += "<b>Benefits</b>\n(No special benefits configured)"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✏️ Edit Tier", callback_data=f"vip:tier_edit:{tier_id}")],
        [InlineKeyboardButton(
            "⭐ Set as Default" if not t.is_default else "✅ Already Default",
            callback_data=f"vip:tier_setdefault:{tier_id}" if not t.is_default else "noop",
        )],
        [InlineKeyboardButton(
            "🔴 Deactivate" if t.is_active else "🟢 Activate",
            callback_data=f"vip:tier_toggle:{tier_id}",
        )],
        [InlineKeyboardButton("🗑 Delete Tier", callback_data=f"vip:tier_del_ask:{tier_id}")],
        [_back_btn("🔙 Tiers", "vip:tiers")],
    ])
    await _send(update, text, kb)


async def vip_tier_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        t = session.query(VipTier).filter_by(id=tier_id).first()
        if not t:
            await q.answer("Not found", show_alert=True)
            return
        t.is_active = not t.is_active
        session.commit()
    await vip_tier_detail(with_data(update, f"vip:tier:{tier_id}"), context)


async def vip_tier_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        session.query(VipTier).update({"is_default": False})
        t = session.query(VipTier).filter_by(id=tier_id).first()
        if t:
            t.is_default = True
        session.commit()
    await q.answer("✅ Default tier updated.", show_alert=True)
    await vip_tier_detail(with_data(update, f"vip:tier:{tier_id}"), context)


async def vip_tier_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        t = session.query(VipTier).filter_by(id=tier_id).first()
        name = f"{t.emoji} {t.name}" if t else f"ID {tier_id}"
        count = session.query(UserVipTier).filter_by(tier_id=tier_id).count()
    text = (
        f"🗑 <b>Delete Tier</b>\n\n"
        f"Tier: <b>{name}</b>\n"
        f"Users currently at this tier: <b>{count}</b>\n\n"
        "⚠️ Deleting this tier will remove all user assignments to it. "
        "Those users will fall back to the default tier. Continue?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"vip:tier_del_ok:{tier_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"vip:tier:{tier_id}")],
    ])
    await _send(update, text, kb)


async def vip_tier_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        t = session.query(VipTier).filter_by(id=tier_id).first()
        if not t:
            await q.answer("Not found", show_alert=True)
            return
        # detach users — set to default tier
        default = vip_service._get_default_tier(session)
        if default and default.id != tier_id:
            (session.query(UserVipTier)
             .filter_by(tier_id=tier_id)
             .update({"tier_id": default.id, "reason": "tier_deleted"}))
        session.delete(t)
        session.commit()
        log_admin_action(update.effective_user.id, "vip.tier.delete",
                         details=f"tier_id={tier_id}")
    await q.answer("✅ Tier deleted.", show_alert=True)
    await vip_tiers(with_data(update, "vip:tiers"), context)


# ─── create tier conversation ─────────────────────────────────────────────────

async def vip_tier_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    context.user_data["new_tier"] = {}
    try:
        await q.edit_message_text(
            "➕ <b>Create New VIP Tier</b>\n\nStep 1/3: Enter the tier <b>name</b> (e.g. Silver):",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_TIER_NAME


async def vip_tier_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Try again:")
        return _S_TIER_NAME
    context.user_data["new_tier"]["name"] = name
    await update.message.reply_text(
        f"Step 2/3: Enter an <b>emoji</b> for <i>{name}</i> (e.g. 🥈):",
        parse_mode="HTML",
    )
    return _S_TIER_EMOJI


async def vip_tier_new_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    emoji = (update.message.text or "").strip()[:8] or "⭐"
    context.user_data["new_tier"]["emoji"] = emoji
    await update.message.reply_text(
        "Step 3/3: Enter the <b>level number</b> (0 = lowest, higher = more exclusive):\n"
        "Enter requirements as: <code>orders,spending,referral_earnings,account_age_days</code>\n"
        "Example: <code>10,100,0,0</code> means 10 orders OR $100 spent.",
        parse_mode="HTML",
    )
    return _S_TIER_REQS


async def vip_tier_new_reqs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    try:
        if len(parts) < 4:
            raise ValueError("need 4 values")
        orders = int(parts[0])
        spending = float(parts[1])
        ref_earn = float(parts[2])
        age_days = int(parts[3])
        if orders < 0 or spending < 0 or ref_earn < 0 or age_days < 0:
            raise ValueError("negative")
    except Exception:
        await update.message.reply_text(
            "❌ Invalid format. Enter: <code>orders,spending,referral_earnings,account_age_days</code>",
            parse_mode="HTML",
        )
        return _S_TIER_REQS
    context.user_data["new_tier"].update({
        "min_orders": orders, "min_spending": spending,
        "min_referral_earnings": ref_earn, "min_account_age_days": age_days,
    })
    await update.message.reply_text(
        "Enter <b>benefits</b> as: <code>discount_pct,cashback_pct,referral_bonus_pct,coupon_pct</code>\n"
        "Example: <code>5,2,1,0</code> means 5% product discount, 2% cashback, 1% referral bonus.",
        parse_mode="HTML",
    )
    return _S_TIER_BENS


async def vip_tier_new_bens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    try:
        disc = float(parts[0]) if len(parts) > 0 else 0
        cash = float(parts[1]) if len(parts) > 1 else 0
        ref  = float(parts[2]) if len(parts) > 2 else 0
        coup = float(parts[3]) if len(parts) > 3 else 0
    except Exception:
        disc = cash = ref = coup = 0.0
    context.user_data["new_tier"].update({
        "discount_pct": max(0, disc),
        "cashback_pct": max(0, cash),
        "referral_bonus_pct": max(0, ref),
        "extra_coupon_discount_pct": max(0, coup),
    })

    # auto-assign level: one higher than current max
    with get_db_session() as session:
        from sqlalchemy import func
        max_level = session.query(func.max(VipTier.level)).scalar() or -1
        level = max_level + 1
        nd = context.user_data.get("new_tier", {})
        tier = VipTier(
            name=nd.get("name", "New Tier"),
            emoji=nd.get("emoji", "⭐"),
            level=level,
            min_orders=nd.get("min_orders", 0),
            min_spending=nd.get("min_spending", 0.0),
            min_referral_earnings=nd.get("min_referral_earnings", 0.0),
            min_account_age_days=nd.get("min_account_age_days", 0),
            discount_pct=nd.get("discount_pct", 0.0),
            cashback_pct=nd.get("cashback_pct", 0.0),
            referral_bonus_pct=nd.get("referral_bonus_pct", 0.0),
            extra_coupon_discount_pct=nd.get("extra_coupon_discount_pct", 0.0),
        )
        session.add(tier)
        session.commit()
        log_admin_action(update.effective_user.id, "vip.tier.create",
                         details=f"name={tier.name} level={tier.level}")
        tid = tier.id

    await update.message.reply_text(
        f"✅ Tier <b>{nd.get('name')}</b> created at level {level}!\n"
        f"Use /admin → VIP Manager to edit benefits further.",
        parse_mode="HTML",
    )
    context.user_data.pop("new_tier", None)
    return ConversationHandler.END


async def vip_tier_new_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_tier", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
        q = update.callback_query
        await vip_tiers(with_data(update, "vip:tiers"), context)
    else:
        await update.message.reply_text("❌ Tier creation cancelled.")
    return ConversationHandler.END


# ─── edit tier (single field) wizard ─────────────────────────────────────────

_TIER_EDITABLE_FIELDS = {
    "name":                    ("Name",               "str"),
    "emoji":                   ("Emoji",              "str"),
    "min_orders":              ("Min Orders",         "int"),
    "min_spending":            ("Min Spending ($)",   "float"),
    "min_referral_earnings":   ("Min Referral ($)",   "float"),
    "min_account_age_days":    ("Min Account Age (days)", "int"),
    "discount_pct":            ("Discount %",         "float"),
    "cashback_pct":            ("Cashback %",         "float"),
    "referral_bonus_pct":      ("Referral Bonus %",   "float"),
    "extra_coupon_discount_pct":("Extra Coupon %",    "float"),
    "withdrawal_limit_multiplier":("Withdrawal ×",   "float"),
    "wallet_limit_multiplier": ("Wallet Limit ×",     "float"),
}


async def vip_tier_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        tier_id = int(q.data.split(":")[-1])
    except Exception:
        return
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"vip:tier_edit_field:{tier_id}:{field}")]
        for field, (label, _) in _TIER_EDITABLE_FIELDS.items()
    ]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"vip:tier:{tier_id}")])
    await _send(update, f"✏️ <b>Edit Tier</b> — choose a field:", InlineKeyboardMarkup(buttons))


async def vip_tier_edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        parts = q.data.split(":")  # vip:tier_edit_field:ID:FIELD
        tier_id = int(parts[3])
        field = parts[4]
    except Exception:
        return ConversationHandler.END
    label, _ = _TIER_EDITABLE_FIELDS.get(field, (field, "str"))
    context.user_data["edit_tier"] = {"id": tier_id, "field": field}
    try:
        await q.edit_message_text(
            f"✏️ <b>Edit {label}</b>\n\nEnter the new value:",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_TIER_EDIT_V


async def vip_tier_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    ed = context.user_data.get("edit_tier", {})
    tier_id = ed.get("id")
    field = ed.get("field")
    if not tier_id or not field:
        await update.message.reply_text("❌ Session expired. Start over.")
        return ConversationHandler.END
    _, ftype = _TIER_EDITABLE_FIELDS.get(field, (field, "str"))
    try:
        if ftype == "int":
            value = int(raw)
        elif ftype == "float":
            value = float(raw)
        else:
            value = raw
    except ValueError:
        await update.message.reply_text(f"❌ Invalid value for {ftype}. Try again:")
        return _S_TIER_EDIT_V
    with get_db_session() as session:
        t = session.query(VipTier).filter_by(id=tier_id).first()
        if not t:
            await update.message.reply_text("❌ Tier not found.")
            return ConversationHandler.END
        setattr(t, field, value)
        t.updated_at = datetime.utcnow()
        session.commit()
        log_admin_action(update.effective_user.id, "vip.tier.edit",
                         details=f"tier_id={tier_id} field={field} value={value}")
    await update.message.reply_text(f"✅ <b>{field}</b> updated to <code>{value}</code>.",
                                    parse_mode="HTML")
    context.user_data.pop("edit_tier", None)
    return ConversationHandler.END


# ─── rewards ─────────────────────────────────────────────────────────────────

async def vip_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    with get_db_session() as session:
        rewards = session.query(LoyaltyReward).order_by(LoyaltyReward.points_cost).all()
        rows = [(r.id, r.name, r.reward_type, r.points_cost, r.value, r.is_active)
                for r in rewards]
    _type_emoji = {"wallet": "💰", "coupon": "🎟", "discount": "🏷", "product": "🎁"}
    lines = []
    buttons = []
    for rid, name, rtype, cost, val, active in rows:
        em = _type_emoji.get(rtype, "🎁")
        s = "🟢" if active else "🔴"
        lines.append(f"{s} {em} <b>{name}</b> — {cost} pts")
        buttons.append([InlineKeyboardButton(f"{em} {name}", callback_data=f"vip:reward:{rid}")])
    text = "🎁 <b>Loyalty Rewards</b>\n\n" + ("\n".join(lines) if lines else "No rewards yet.")
    buttons.append([
        InlineKeyboardButton("➕ New Reward", callback_data="vip:reward_new"),
        _back_btn("🔙 Back", "vip:menu"),
    ])
    await _send(update, text, InlineKeyboardMarkup(buttons))


async def vip_reward_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        rid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        r = session.query(LoyaltyReward).filter_by(id=rid).first()
        if not r:
            await q.answer("Not found", show_alert=True)
            return
        claims = session.query(LoyaltyRewardClaim).filter_by(reward_id=rid).count()
        text = (
            f"🎁 <b>{r.name}</b>\n\n"
            f"Type: {r.reward_type.title()}\n"
            f"Points Cost: <b>{r.points_cost}</b>\n"
            f"Value: <b>${r.value:.2f}</b>\n"
            f"Min Tier Level: {r.min_tier_level}\n"
            f"Claims/User Limit: {r.max_claims_per_user or '∞'}\n"
            f"Total Claims Limit: {r.max_total_claims or '∞'}\n"
            f"Total Claims: <b>{r.total_claims}</b> ({claims} in DB)\n"
            f"Status: {'🟢 Active' if r.is_active else '🔴 Inactive'}\n"
            f"Expires: {r.expires_at.strftime('%Y-%m-%d') if r.expires_at else 'Never'}"
        )
        if r.description:
            text += f"\n\n<i>{r.description}</i>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔴 Deactivate" if r.is_active else "🟢 Activate",
            callback_data=f"vip:reward_toggle:{rid}",
        )],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"vip:reward_del_ask:{rid}")],
        [_back_btn("🔙 Rewards", "vip:rewards")],
    ])
    await _send(update, text, kb)


async def vip_reward_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        rid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        r = session.query(LoyaltyReward).filter_by(id=rid).first()
        if r:
            r.is_active = not r.is_active
            session.commit()
    await vip_reward_detail(with_data(update, f"vip:reward:{rid}"), context)


async def vip_reward_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        rid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        r = session.query(LoyaltyReward).filter_by(id=rid).first()
        name = r.name if r else f"ID {rid}"
    text = f"🗑 <b>Delete Reward</b>\n\nReward: <b>{name}</b>\n\nThis will also delete all claim records. Continue?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Delete", callback_data=f"vip:reward_del_ok:{rid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"vip:reward:{rid}")],
    ])
    await _send(update, text, kb)


async def vip_reward_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        rid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        r = session.query(LoyaltyReward).filter_by(id=rid).first()
        if r:
            session.delete(r)
            session.commit()
    await q.answer("✅ Reward deleted.", show_alert=True)
    await vip_rewards(with_data(update, "vip:rewards"), context)


# ─── create reward conversation ───────────────────────────────────────────────

async def vip_reward_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    context.user_data["new_reward"] = {}
    try:
        await q.edit_message_text(
            "➕ <b>Create New Loyalty Reward</b>\n\nStep 1/4: Enter the reward <b>name</b>:",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_RWD_NAME


async def vip_reward_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty.")
        return _S_RWD_NAME
    context.user_data["new_reward"]["name"] = name
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Wallet Credit", callback_data="rwd_type:wallet"),
         InlineKeyboardButton("🎟 Coupon", callback_data="rwd_type:coupon")],
        [InlineKeyboardButton("🏷 Discount", callback_data="rwd_type:discount"),
         InlineKeyboardButton("🎁 Product", callback_data="rwd_type:product")],
    ])
    await update.message.reply_text(
        "Step 2/4: Choose <b>reward type</b>:", reply_markup=kb, parse_mode="HTML"
    )
    return _S_RWD_TYPE


async def vip_reward_new_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    rtype = q.data.split(":")[-1]
    context.user_data["new_reward"]["reward_type"] = rtype
    await q.edit_message_text(
        "Step 3/4: Enter the <b>points cost</b> (integer, e.g. 500):", parse_mode="HTML"
    )
    return _S_RWD_COST


async def vip_reward_new_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cost = int((update.message.text or "").strip())
        if cost <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive integer.")
        return _S_RWD_COST
    context.user_data["new_reward"]["points_cost"] = cost
    await update.message.reply_text(
        "Step 4/4: Enter the <b>value</b> (USD amount or % depending on type):", parse_mode="HTML"
    )
    return _S_RWD_VALUE


async def vip_reward_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float((update.message.text or "").strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return _S_RWD_VALUE
    nr = context.user_data.get("new_reward", {})
    nr["value"] = value
    with get_db_session() as session:
        reward = LoyaltyReward(
            name=nr.get("name", "Reward"),
            reward_type=nr.get("reward_type", "wallet"),
            points_cost=nr.get("points_cost", 100),
            value=nr.get("value", 1.0),
        )
        session.add(reward)
        session.commit()
        log_admin_action(update.effective_user.id, "vip.reward.create",
                         details=f"name={reward.name} type={reward.reward_type} cost={reward.points_cost}")
    await update.message.reply_text(
        f"✅ Reward <b>{nr.get('name')}</b> created!", parse_mode="HTML"
    )
    context.user_data.pop("new_reward", None)
    return ConversationHandler.END


async def vip_reward_new_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_reward", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    else:
        await update.message.reply_text("❌ Reward creation cancelled.")
    return ConversationHandler.END


# ─── VIP users list ───────────────────────────────────────────────────────────

async def vip_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        page = int(q.data.split(":")[-1]) if q else 0
    except Exception:
        page = 0
    with get_db_session() as session:
        total = session.query(UserVipTier).count()
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        rows_q = (
            session.query(UserVipTier, User, VipTier)
            .join(User, User.id == UserVipTier.user_id)
            .join(VipTier, VipTier.id == UserVipTier.tier_id)
            .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE)
            .all()
        )
        rows = [
            (uvt.user_id, u.telegram_id, u.username or u.first_name or f"ID{u.telegram_id}",
             t.emoji, t.name, u.loyalty_points or 0)
            for uvt, u, t in rows_q
        ]
    buttons = []
    for uid, tg_id, uname, temoji, tname, pts in rows:
        buttons.append([InlineKeyboardButton(
            f"{temoji} {uname[:24]} — {pts} pts",
            callback_data=f"vip:user:{uid}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"vip:users:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"vip:users:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([_back_btn("🔙 Back", "vip:menu")])
    text = f"👥 <b>VIP Users</b> — Page {page+1}/{total_pages} ({total} total)"
    await _send(update, text, InlineKeyboardMarkup(buttons))


async def vip_user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_db_id).first()
        if not user:
            await q.answer("Not found", show_alert=True)
            return
        current_tier = vip_service.get_user_tier(session, user_db_id)
        tiers = session.query(VipTier).filter_by(is_active=True).order_by(VipTier.level).all()
        tier_rows = [(t.id, t.emoji, t.name, t.level) for t in tiers]
        pts = user.loyalty_points or 0
        spent = user.total_spent or 0
        uname = user.username or user.first_name or f"ID{user.telegram_id}"
        tier_label = f"{current_tier.emoji} {current_tier.name}" if current_tier else "None"
        text = (
            f"👤 <b>{uname}</b>\n\n"
            f"Current Tier: <b>{tier_label}</b>\n"
            f"Loyalty Points: <b>{pts}</b>\n"
            f"Lifetime Spending: <b>${spent:,.2f}</b>\n"
            f"Telegram ID: <code>{user.telegram_id}</code>"
        )
    tier_promote_buttons = [
        InlineKeyboardButton(
            f"{e} {n}" + (" ✅" if current_tier and current_tier.id == tid else ""),
            callback_data=f"vip:promote:{user_db_id}:{tid}",
        )
        for tid, e, n, lvl in tier_rows
    ]
    # chunk into rows of 2
    tier_kb = [tier_promote_buttons[i:i+2] for i in range(0, len(tier_promote_buttons), 2)]
    kb = InlineKeyboardMarkup(
        tier_kb + [
            [InlineKeyboardButton("➕ Add Points", callback_data=f"vip:pts_add:{user_db_id}"),
             InlineKeyboardButton("➖ Remove Points", callback_data=f"vip:pts_rm:{user_db_id}")],
            [InlineKeyboardButton("🔄 Reset Points", callback_data=f"vip:pts_rst:{user_db_id}"),
             InlineKeyboardButton("📋 VIP History", callback_data=f"vip:history:{user_db_id}")],
            [_back_btn("🔙 VIP Users", "vip:users:0")],
        ]
    )
    await _send(update, text, kb)


async def vip_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        parts = q.data.split(":")
        user_db_id = int(parts[2])
        tier_id = int(parts[3])
    except Exception:
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_db_id).first()
        tier = session.query(VipTier).filter_by(id=tier_id).first()
        if not user or not tier:
            await q.answer("Not found", show_alert=True)
            return
        vip_service.admin_set_tier(session, user, tier, update.effective_user.id,
                                   reason="admin_manual")
        session.commit()
        log_admin_action(update.effective_user.id, "vip.user.promote",
                         details=f"user_id={user_db_id} tier={tier.name}")
    await q.answer(f"✅ User set to {tier.emoji} {tier.name}", show_alert=True)
    await vip_user_detail(with_data(update, f"vip:user:{user_db_id}"), context)


async def vip_pts_rst(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return
    text = "⚠️ <b>Reset Points</b>\n\nThis will set the user's loyalty points to 0. Continue?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Reset", callback_data=f"vip:pts_rst_ok:{user_db_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"vip:user:{user_db_id}")],
    ])
    await _send(update, text, kb)


async def vip_pts_rst_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_db_id).first()
        if user:
            vip_service.reset_points(session, user, update.effective_user.id)
            session.commit()
            log_admin_action(update.effective_user.id, "vip.points.reset",
                             details=f"user_id={user_db_id}")
    await q.answer("✅ Points reset to 0.", show_alert=True)
    await vip_user_detail(with_data(update, f"vip:user:{user_db_id}"), context)


async def vip_pts_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return ConversationHandler.END
    context.user_data["pts_target"] = user_db_id
    try:
        await q.edit_message_text("Enter the number of points to <b>add</b>:", parse_mode="HTML")
    except BadRequest:
        pass
    return _S_PTS_AMT


async def vip_pts_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pts = int((update.message.text or "").strip())
        if pts <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive integer.")
        return _S_PTS_AMT
    uid = context.user_data.get("pts_target")
    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if user:
            vip_service.award_points(session, user, pts, "admin_bonus")
            session.commit()
            log_admin_action(update.effective_user.id, "vip.points.add",
                             details=f"user_id={uid} pts={pts}")
    await update.message.reply_text(f"✅ Added <b>{pts}</b> points.", parse_mode="HTML")
    context.user_data.pop("pts_target", None)
    return ConversationHandler.END


async def vip_pts_rm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return ConversationHandler.END
    context.user_data["pts_rm_target"] = user_db_id
    try:
        await q.edit_message_text("Enter the number of points to <b>remove</b>:", parse_mode="HTML")
    except BadRequest:
        pass
    return _S_PTS_RM_AMT


async def vip_pts_rm_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pts = int((update.message.text or "").strip())
        if pts <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive integer.")
        return _S_PTS_RM_AMT
    uid = context.user_data.get("pts_rm_target")
    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if user:
            ok = vip_service.deduct_points(session, user, pts, "admin_deduct")
            session.commit()
            if ok:
                log_admin_action(update.effective_user.id, "vip.points.remove",
                                 details=f"user_id={uid} pts={pts}")
                await update.message.reply_text(f"✅ Removed <b>{pts}</b> points.", parse_mode="HTML")
            else:
                await update.message.reply_text("❌ User has insufficient points.")
    context.user_data.pop("pts_rm_target", None)
    return ConversationHandler.END


async def vip_pts_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pts_target", None)
    context.user_data.pop("pts_rm_target", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── VIP history ──────────────────────────────────────────────────────────────

async def vip_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        user_db_id = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        entries = (
            session.query(VipTierHistory)
            .filter_by(user_id=user_db_id)
            .order_by(VipTierHistory.created_at.desc())
            .limit(10)
            .all()
        )
        lines = []
        for e in entries:
            old = session.query(VipTier).filter_by(id=e.old_tier_id).first() if e.old_tier_id else None
            new = session.query(VipTier).filter_by(id=e.new_tier_id).first()
            old_label = f"{old.emoji} {old.name}" if old else "None"
            new_label = f"{new.emoji} {new.name}" if new else f"ID {e.new_tier_id}"
            when = e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "?"
            by = f"admin {e.changed_by}" if e.changed_by else "auto"
            lines.append(f"• {when} — {old_label} → {new_label} ({e.reason or 'n/a'}, {by})")
    text = f"📋 <b>VIP History</b> (last 10)\n\n" + ("\n".join(lines) if lines else "No history.")
    kb = InlineKeyboardMarkup([[_back_btn("🔙 Back", f"vip:user:{user_db_id}")]])
    await _send(update, text, kb)


# ─── statistics ───────────────────────────────────────────────────────────────

async def vip_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    with get_db_session() as session:
        stats = vip_service.get_vip_stats(session)
    dist_lines = [
        f"  {emoji} {name}: <b>{count}</b>"
        for name, emoji, count in stats["tier_dist"]
    ]
    spender_lines = []
    for u in stats["top_spenders"]:
        uname = u.username or u.first_name or f"ID{u.telegram_id}"
        spender_lines.append(f"  {uname[:20]}: <b>${u.total_spent:,.2f}</b>")
    text = (
        "📊 <b>VIP Statistics</b>\n\n"
        f"VIP Users: <b>{stats['total_vip']}</b>\n"
        f"Total Points Issued: <b>{stats['total_points']:,}</b>\n"
        f"Redeemed Points: <b>{stats['redeemed_pts']:,}</b>\n"
        f"Total Reward Claims: <b>{stats['total_claims']}</b>\n\n"
        "<b>Tier Distribution:</b>\n" + "\n".join(dist_lines) +
        "\n\n<b>Top 5 Spenders:</b>\n" + ("\n".join(spender_lines) if spender_lines else "N/A")
    )
    kb = InlineKeyboardMarkup([[_back_btn("🔙 Back", "vip:menu")]])
    await _send(update, text, kb)


# ─── settings ────────────────────────────────────────────────────────────────

async def vip_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    status = cfg.get_str("vip_status", "enabled")
    expiry = cfg.get_int("vip_points_expiration_days", 0)
    reward_limit = cfg.get_int("vip_reward_limit_per_day", 0)
    text = (
        "⚙️ <b>VIP Settings</b>\n\n"
        f"Status: {_status_label()}\n"
        f"Points Expiration: {'Never' if expiry == 0 else f'{expiry} days'}\n"
        f"Reward Limit/Day: {'Unlimited' if reward_limit == 0 else reward_limit}\n"
    )
    for key, label in _BOOL_SETTINGS:
        val = "✅ ON" if cfg.get_bool(key, True) else "❌ OFF"
        text += f"\n{label}: {val}"

    status_btns = [
        InlineKeyboardButton("🟢 Enable",      callback_data="vip:settings:status:enabled"),
        InlineKeyboardButton("🟡 Maintenance", callback_data="vip:settings:status:maintenance"),
        InlineKeyboardButton("🔴 Disable",     callback_data="vip:settings:status:disabled"),
    ]
    toggle_rows = [
        [InlineKeyboardButton(f"Toggle: {label}", callback_data=f"vip:settings:toggle:{key}")]
        for key, label in _BOOL_SETTINGS
    ]
    kb = InlineKeyboardMarkup(
        [status_btns] + toggle_rows + [[_back_btn("🔙 Back", "vip:menu")]]
    )
    await _send(update, text, kb)


async def vip_settings_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    data = q.data  # vip:settings:status:VAL  or  vip:settings:toggle:KEY
    parts = data.split(":")
    if len(parts) < 4:
        return
    action = parts[2]
    value = parts[3]
    if action == "status":
        cfg.set("vip_status", value)
        await q.answer(f"VIP status → {value}", show_alert=True)
    elif action == "toggle":
        current = cfg.get_bool(value, True)
        cfg.set(value, not current)
        await q.answer(f"{value} → {'ON' if not current else 'OFF'}", show_alert=True)
    await vip_settings(update, context)


# ─── handler registration ─────────────────────────────────────────────────────

def build_vip_tier_new_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(vip_tier_new_start, pattern=r"^vip:tier_new$")],
        states={
            _S_TIER_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_tier_new_name)],
            _S_TIER_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_tier_new_emoji)],
            _S_TIER_REQS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_tier_new_reqs)],
            _S_TIER_BENS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_tier_new_bens)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, vip_tier_new_cancel),
            CallbackQueryHandler(vip_tier_new_cancel, pattern=r"^vip:menu$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_vip_tier_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(
            vip_tier_edit_field_start, pattern=r"^vip:tier_edit_field:\d+:\w+$"
        )],
        states={
            _S_TIER_EDIT_V: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_tier_edit_value)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, vip_tier_new_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_vip_reward_new_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(vip_reward_new_start, pattern=r"^vip:reward_new$")],
        states={
            _S_RWD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_reward_new_name)],
            _S_RWD_TYPE:  [CallbackQueryHandler(vip_reward_new_type, pattern=r"^rwd_type:")],
            _S_RWD_COST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_reward_new_cost)],
            _S_RWD_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_reward_new_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, vip_reward_new_cancel),
            CallbackQueryHandler(vip_reward_new_cancel, pattern=r"^vip:rewards$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_vip_pts_add_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(vip_pts_add_start, pattern=r"^vip:pts_add:\d+$")],
        states={
            _S_PTS_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_pts_add_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, vip_pts_cancel),
            CallbackQueryHandler(vip_pts_cancel, pattern=r"^vip:menu$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_vip_pts_rm_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(vip_pts_rm_start, pattern=r"^vip:pts_rm:\d+$")],
        states={
            _S_PTS_RM_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_pts_rm_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, vip_pts_cancel),
            CallbackQueryHandler(vip_pts_cancel, pattern=r"^vip:menu$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def vip_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatch for all vip:* callbacks not handled by conversations."""
    data = getattr(update.callback_query, "data", "") or ""
    if data == "vip:menu":
        return vip_menu(update, context)
    if data == "vip:tiers":
        return vip_tiers(update, context)
    if data.startswith("vip:tier:") and not data.startswith("vip:tier_"):
        return vip_tier_detail(update, context)
    if data.startswith("vip:tier_toggle:"):
        return vip_tier_toggle(update, context)
    if data.startswith("vip:tier_setdefault:"):
        return vip_tier_set_default(update, context)
    if data.startswith("vip:tier_del_ask:"):
        return vip_tier_del_ask(update, context)
    if data.startswith("vip:tier_del_ok:"):
        return vip_tier_del_ok(update, context)
    if data.startswith("vip:tier_edit:") and not data.startswith("vip:tier_edit_field:"):
        return vip_tier_edit_menu(update, context)
    if data == "vip:rewards":
        return vip_rewards(update, context)
    if data.startswith("vip:reward:") and not data.startswith("vip:reward_"):
        return vip_reward_detail(update, context)
    if data.startswith("vip:reward_toggle:"):
        return vip_reward_toggle(update, context)
    if data.startswith("vip:reward_del_ask:"):
        return vip_reward_del_ask(update, context)
    if data.startswith("vip:reward_del_ok:"):
        return vip_reward_del_ok(update, context)
    if data.startswith("vip:users:"):
        return vip_users(update, context)
    if data.startswith("vip:user:"):
        return vip_user_detail(update, context)
    if data.startswith("vip:promote:"):
        return vip_promote(update, context)
    if data.startswith("vip:pts_rst_ok:"):
        return vip_pts_rst_ok(update, context)
    if data.startswith("vip:pts_rst:"):
        return vip_pts_rst(update, context)
    if data.startswith("vip:history:"):
        return vip_history(update, context)
    if data == "vip:stats":
        return vip_stats(update, context)
    if data == "vip:settings":
        return vip_settings(update, context)
    if data.startswith("vip:settings:"):
        return vip_settings_dispatch(update, context)
    return None


def register_handlers(app) -> None:
    app.add_handler(build_vip_tier_new_conv())
    app.add_handler(build_vip_tier_edit_conv())
    app.add_handler(build_vip_reward_new_conv())
    app.add_handler(build_vip_pts_add_conv())
    app.add_handler(build_vip_pts_rm_conv())
    # All remaining vip:* callbacks — single central dispatcher
    app.add_handler(CallbackQueryHandler(vip_dispatch, pattern=r"^vip:"))
