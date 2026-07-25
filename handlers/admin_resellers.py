"""Reseller tier management + user assignment."""
from __future__ import annotations

from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

from database import get_db_session
from database.models import ResellerTier, UserReseller, User
from utils.audit import log_admin_action
from ._acc_helpers import require_admin, back_root, send

T_NAME, T_PCT, T_MINQ = range(9400, 9403)
A_USER, A_TIER = range(9410, 9412)


@require_admin
async def resellers_menu(update, context, page: int = 0):
    with get_db_session() as s:
        tiers = s.query(ResellerTier).order_by(ResellerTier.display_order.asc(),
                                               ResellerTier.name.asc()).all()
        assigned = s.query(UserReseller).count()
    t = ["🤝 <b>RESELLERS</b>",
         f"Tiers: {len(tiers)}  ·  Assigned users: {assigned}", ""]
    for tier in tiers:
        badge = "🟢" if tier.is_active else "⚪"
        t.append(f"{badge} <b>{tier.name}</b> — {tier.discount_pct:.1f}%  "
                 f"(min qty {tier.min_quantity})")
    kb = [
        [InlineKeyboardButton("➕ New tier", callback_data="acc:res:add")],
        [InlineKeyboardButton("👤 Assign user", callback_data="acc:res:assign")],
        [back_root()],
    ]
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


# ── Add tier ───────────────────────────────────────────────────────────
@require_admin
async def add_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Tier name (e.g. Bronze):")
    return T_NAME


async def t_name(update, context):
    context.user_data["tier_new"] = {"name": update.message.text.strip()}
    await update.message.reply_text("Discount percent (0-100):")
    return T_PCT


async def t_pct(update, context):
    try:
        p = float(update.message.text.strip())
        if not 0 <= p <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter number between 0 and 100.")
        return T_PCT
    context.user_data["tier_new"]["pct"] = p
    await update.message.reply_text("Minimum purchase quantity (default 1):")
    return T_MINQ


async def t_minq(update, context):
    try:
        q = int(update.message.text.strip() or "1")
        if q < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a positive integer.")
        return T_MINQ
    d = context.user_data.pop("tier_new")
    with get_db_session() as s:
        tier = ResellerTier(name=d["name"], discount_pct=d["pct"],
                            min_quantity=q, is_active=True,
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow())
        s.add(tier); s.commit(); s.refresh(tier)
    try:
        log_admin_action(update.effective_user.id, "reseller_tier_created",
                         f"tier_id={tier.id} name={d['name']} pct={d['pct']}")
    except Exception:
        pass
    await update.message.reply_text(f"✅ Tier '{d['name']}' created.")
    return ConversationHandler.END


# ── Assign user ────────────────────────────────────────────────────────
@require_admin
async def assign_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Reply with the user's Telegram id (or numeric users.id):")
    return A_USER


async def a_user(update, context):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a numeric id.")
        return A_USER
    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=uid).first() or s.get(User, uid)
    if not user:
        await update.message.reply_text("User not found.")
        return ConversationHandler.END
    context.user_data["assign_user_id"] = user.id
    with get_db_session() as s:
        tiers = s.query(ResellerTier).filter_by(is_active=True).all()
    if not tiers:
        await update.message.reply_text("No active tiers exist. Create one first.")
        return ConversationHandler.END
    lines = ["Tier id (or 0 to remove):"]
    for t in tiers:
        lines.append(f"  <code>{t.id}</code> — {t.name} ({t.discount_pct:.1f}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return A_TIER


async def a_tier(update, context):
    try:
        tid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a numeric id.")
        return A_TIER
    uid = context.user_data.pop("assign_user_id")
    with get_db_session() as s:
        existing = s.query(UserReseller).filter_by(user_id=uid).first()
        if tid == 0:
            if existing:
                s.delete(existing); s.commit()
            msg = "Reseller status removed."
        else:
            if existing:
                existing.tier_id = tid
                existing.assigned_by = update.effective_user.id
                existing.assigned_at = datetime.utcnow()
            else:
                s.add(UserReseller(user_id=uid, tier_id=tid,
                                   assigned_by=update.effective_user.id,
                                   assigned_at=datetime.utcnow()))
            s.commit()
            msg = "Reseller tier assigned."
    try:
        log_admin_action(update.effective_user.id, "reseller_assigned",
                         f"user_id={uid} tier_id={tid}")
    except Exception:
        pass
    await update.message.reply_text("✅ " + msg)
    return ConversationHandler.END


async def _cancel(update, context):
    context.user_data.pop("tier_new", None)
    context.user_data.pop("assign_user_id", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def route(action, rest, update, context):
    if action == "list":
        await resellers_menu(update, context)


def build_reseller_convs():
    add = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^acc:res:add$")],
        states={
            T_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PCT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, t_pct)],
            T_MINQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_minq)],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    assign = ConversationHandler(
        entry_points=[CallbackQueryHandler(assign_start, pattern=r"^acc:res:assign$")],
        states={
            A_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_user)],
            A_TIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_tier)],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    return [add, assign]
