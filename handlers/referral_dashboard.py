"""Referral Commission History + Admin Referral Settings — V20.

Callback namespace: ``rd:*``

The old "Advanced Referral Dashboard" user-facing screen (rank, link clicks,
share/leaderboard/withdraw buttons, legacy earnings display) has been removed.
The essential referral system (My Referral Link, Total Referrals, Total
Earned) lives in ``handlers/referral_handlers.py`` behind the ``refer``
callback; this module now only provides:
  • Commission History (rd:comm) — reached from the essential referral screen
  • Admin referral settings: commission %, min/max withdrawal, bonus,
    first-purchase bonus, lifetime referrals, pending withdrawal review
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, User
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils import is_admin, safe_edit_message_text

logger = logging.getLogger(__name__)

# ── Conversation states (unique, non-colliding) ────────────────────────────────
RD_ADM_COMMISSION   = 51
RD_ADM_MIN_WITHDRAW = 52
RD_ADM_MAX_WITHDRAW = 53
RD_ADM_BONUS        = 54
RD_ADM_FPB          = 55   # first-purchase bonus
RD_ADM_MAX_LEVELS   = 56

def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode,
                                        disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_top_referrers(limit: int = 10) -> list:
    """Return top referrers by referral count."""
    from sqlalchemy import func as sqlfunc
    try:
        with get_db_session() as s:
            rows = (
                s.query(User.id, User.username, User.telegram_id,
                        sqlfunc.count(User.id).label("ref_count"))
                .join(User, User.referred_by_id == User.id, isouter=False)
                .group_by(User.id, User.username, User.telegram_id)
                .order_by(sqlfunc.count(User.id).desc())
                .limit(limit)
                .all()
            )
            result = []
            for i, row in enumerate(rows, 1):
                result.append({
                    "rank": i,
                    "username": row.username or str(row.telegram_id),
                    "count": row.ref_count,
                })
            return result
    except Exception:
        logger.exception("_get_top_referrers failed")
        # Fallback: query referred_by_id directly
        try:
            with get_db_session() as s:
                rows = (
                    s.query(User.referred_by_id,
                            sqlfunc.count(User.id).label("cnt"))
                    .filter(User.referred_by_id.isnot(None))
                    .group_by(User.referred_by_id)
                    .order_by(sqlfunc.count(User.id).desc())
                    .limit(limit)
                    .all()
                )
                result = []
                for i, (ref_by_id, cnt) in enumerate(rows, 1):
                    referrer = None
                    with get_db_session() as s2:
                        referrer = s2.query(User).filter_by(id=ref_by_id).first()
                    uname = (referrer.username or str(referrer.telegram_id)) if referrer else str(ref_by_id)
                    result.append({"rank": i, "username": uname, "count": cnt})
                return result
        except Exception:
            return []


def _get_pending_withdrawals(limit: int = 20) -> list:
    """Return pending withdrawal requests."""
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT rw.id, rw.user_id, rw.amount, rw.created_at, u.username, u.telegram_id "
                "FROM referral_withdrawals rw "
                "JOIN users u ON u.id = rw.user_id "
                "WHERE rw.status = 'pending' "
                "ORDER BY rw.created_at ASC LIMIT :lim"
            ), {"lim": limit}).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "amount": r[2],
                    "created_at": r[3],
                    "username": r[4] or str(r[5]),
                    "telegram_id": r[5],
                }
                for r in rows
            ]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# User-facing: Commission History
# ─────────────────────────────────────────────────────────────────────────────

_RD_PAGE_SIZE = 10


def _fmt_order_id(order_id, created_at=None) -> str:
    """Format a numeric order ID as ORD-YYYYMMDD-NNNNNN."""
    if not order_id:
        return "—"
    date_str = created_at.strftime("%Y%m%d") if created_at else "00000000"
    return f"ORD-{date_str}-{int(order_id):06d}"


async def _render_commissions(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Shared renderer for rd_commissions and rd_commissions_page."""
    query = update.callback_query
    tid = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.")
            return
        user_id = user.id

    rows = []
    total = 0
    try:
        from database.models import ReferralReward
        with get_db_session() as s:
            base_q = s.query(ReferralReward).filter_by(referrer_id=user_id)
            total = base_q.count()
            rewards = (
                base_q
                .order_by(ReferralReward.created_at.desc())
                .offset(page * _RD_PAGE_SIZE)
                .limit(_RD_PAGE_SIZE)
                .all()
            )
            rows = [(float(r.amount), r.order_id, r.created_at) for r in rewards]
    except Exception:
        logger.exception("_render_commissions: failed to query ReferralReward")

    total_pages = max(1, -(-total // _RD_PAGE_SIZE))  # ceiling division

    if not rows:
        body = "No commission history found."
    else:
        lines = []
        for amt, order_id, created_at in rows:
            order_label = _fmt_order_id(order_id, created_at)
            when = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "?"
            lines.append(
                f"🟢 <b>+${amt:.2f}</b>\n"
                f"Order: {order_label}\n"
                f"{when}"
            )
        body = "\n\n".join(lines)

    kb_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"rd:comm:p:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"rd:comm:p:{page + 1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="refer")])

    page_indicator = f" <i>({page + 1}/{total_pages})</i>" if total_pages > 1 else ""
    title = f"💰 <b>Commission History</b>{page_indicator}"
    await _safe_edit(query, f"{title}\n\n{body}", InlineKeyboardMarkup(kb_rows))


async def rd_commissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show commission history page 0 (rd:comm)."""
    query = update.callback_query
    await query.answer()
    await _render_commissions(update, context, page=0)


async def rd_commissions_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle commission history pagination (rd:comm:p:<page>)."""
    query = update.callback_query
    await query.answer()
    try:
        page = int(query.data.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        page = 0
    await _render_commissions(update, context, page=page)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Referral Advanced Settings
# ─────────────────────────────────────────────────────────────────────────────

async def rd_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin referral advanced settings (rd:admin)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    commission_pct = cfg.get_float("referral_commission_pct", 0.0)
    min_w = cfg.get_float("referral_min_withdrawal", 5.0)
    max_w = cfg.get_float("referral_max_withdrawal", 0.0)
    bonus = cfg.get_float("referral_bonus", 0.0)
    fpb = cfg.get_float("referral_first_purchase_bonus", 0.0)
    lifetime = cfg.get_bool("referral_lifetime_enabled", True)
    max_levels = cfg.get_int("referral_max_levels", 1)

    lines = [
        "👥 <b>Referral Settings</b>\n",
        f"Commission on purchase: <b>{commission_pct:.2f}%</b>",
        f"Min withdrawal: <b>${min_w:.2f}</b>",
        f"Max withdrawal: <b>${max_w:.2f}</b> (0 = unlimited)",
        f"Signup bonus: <b>${bonus:.2f}</b>",
        f"First-purchase bonus: <b>${fpb:.2f}</b>",
        f"Lifetime referrals: {'✅ ON' if lifetime else '❌ OFF'}",
        f"Max referral levels: <b>{max_levels}</b>",
    ]

    # Pending withdrawals count
    pending = _get_pending_withdrawals(50)
    lines.append(f"\n⏳ Pending withdrawals: <b>{len(pending)}</b>")

    # Top referrers stats
    top = _get_top_referrers(3)
    if top:
        lines.append("\n🏆 <b>Top 3 Referrers:</b>")
        for entry in top[:3]:
            lines.append(f"  {entry['rank']}. @{entry['username']} — {entry['count']} refs")

    kb = [
        [InlineKeyboardButton("💸 Set Commission %", callback_data="rd:adm:set_commission"),
         InlineKeyboardButton("📤 Old Withdrawals", callback_data="rd:adm:withdrawals")],
        [InlineKeyboardButton("💸 Withdrawal Manager ▶", callback_data="wda:adm:list")],
        [InlineKeyboardButton("💰 Min Withdraw", callback_data="rd:adm:set_min_w"),
         InlineKeyboardButton("💰 Max Withdraw", callback_data="rd:adm:set_max_w")],
        [InlineKeyboardButton("🎁 Signup Bonus", callback_data="rd:adm:set_bonus"),
         InlineKeyboardButton("🛒 FP Bonus", callback_data="rd:adm:set_fpb")],
        [InlineKeyboardButton(
            "🔄 Lifetime: OFF" if lifetime else "🔄 Lifetime: ON",
            callback_data="rd:adm:toggle_lifetime"
        )],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_admin_toggle_lifetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle lifetime referral tracking (rd:adm:toggle_lifetime)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return
    current = cfg.get_bool("referral_lifetime_enabled", True)
    cfg.set("referral_lifetime_enabled", not current)
    await rd_admin_menu(update, context)


async def rd_admin_withdrawals_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List pending withdrawal requests (rd:adm:withdrawals)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    pending = _get_pending_withdrawals(20)
    if not pending:
        await _safe_edit(query, "✅ No pending withdrawal requests.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🔙 Back", callback_data="rd:admin")
                         ]]))
        return

    lines = ["⏳ <b>Pending Withdrawal Requests</b>\n"]
    kb = []
    for w in pending:
        dt = w["created_at"].strftime("%b %d") if w.get("created_at") else ""
        lines.append(f"#{w['id']} @{w['username']} — <b>${w['amount']:.2f}</b>  {dt}")
        kb.append([
            InlineKeyboardButton(f"✅ Approve #{w['id']}", callback_data=f"rd:adm:approve:{w['id']}"),
            InlineKeyboardButton(f"❌ Reject #{w['id']}", callback_data=f"rd:adm:reject:{w['id']}"),
        ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="rd:admin")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_admin_approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a withdrawal request (rd:adm:approve:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        withdrawal_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            row = s.execute(text(
                "UPDATE referral_withdrawals SET status='approved', resolved_at=NOW() "
                "WHERE id=:wid AND status='pending' RETURNING user_id, amount"
            ), {"wid": withdrawal_id}).fetchone()
            if row:
                # Deduct from user's available commissions
                s.execute(text(
                    "UPDATE referral_commissions "
                    "SET status='withdrawn', cleared_at=NOW() "
                    "WHERE referrer_id=:uid AND status='available' "
                    "LIMIT 999"
                ), {"uid": row[0]})
                s.commit()
                await query.answer(f"✅ Withdrawal #{withdrawal_id} approved!", show_alert=True)
            else:
                await query.answer("Already processed.", show_alert=True)
    except Exception:
        logger.exception("approve_withdrawal failed")
        await query.answer("❌ Error processing approval.", show_alert=True)

    await rd_admin_withdrawals_list(update, context)


async def rd_admin_reject_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject a withdrawal request (rd:adm:reject:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        withdrawal_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE referral_withdrawals SET status='rejected', resolved_at=NOW() "
                "WHERE id=:wid AND status='pending'"
            ), {"wid": withdrawal_id})
            s.commit()
        await query.answer(f"❌ Withdrawal #{withdrawal_id} rejected.", show_alert=True)
    except Exception:
        logger.exception("reject_withdrawal failed")

    await rd_admin_withdrawals_list(update, context)


# ── Admin numeric input conversations ─────────────────────────────────────────

async def rd_adm_set_commission_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    cur = cfg.get_float("referral_commission_pct", 0.0)
    await _safe_edit(q,
        f"💸 <b>Set Commission Percentage</b>\n\nCurrent: <b>{cur:.2f}%</b>\n\n"
        f"Send the new commission % (0 to disable, max 50):",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:admin")
        ]]),
    )
    return RD_ADM_COMMISSION


async def rd_adm_commission_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0 or val > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a number between 0 and 50.")
        return RD_ADM_COMMISSION
    cfg.set("referral_commission_pct", val)
    await update.message.reply_text(f"✅ Commission set to <b>{val:.2f}%</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def rd_adm_set_min_w_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    cur = cfg.get_float("referral_min_withdrawal", 5.0)
    await _safe_edit(q,
        f"💰 <b>Set Minimum Withdrawal</b>\n\nCurrent: <b>${cur:.2f}</b>\n\nSend new minimum:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:admin")
        ]]),
    )
    return RD_ADM_MIN_WITHDRAW


async def rd_adm_min_w_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return RD_ADM_MIN_WITHDRAW
    cfg.set("referral_min_withdrawal", val)
    await update.message.reply_text(f"✅ Min withdrawal set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def rd_adm_set_max_w_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    cur = cfg.get_float("referral_max_withdrawal", 0.0)
    await _safe_edit(q,
        f"💰 <b>Set Max Withdrawal</b>\n\nCurrent: <b>${cur:.2f}</b> (0=unlimited)\n\nSend new max:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:admin")
        ]]),
    )
    return RD_ADM_MAX_WITHDRAW


async def rd_adm_max_w_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number (0 = no max).")
        return RD_ADM_MAX_WITHDRAW
    cfg.set("referral_max_withdrawal", val)
    await update.message.reply_text(f"✅ Max withdrawal set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def rd_adm_set_bonus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    cur = cfg.get_float("referral_bonus", 0.0)
    await _safe_edit(q,
        f"🎁 <b>Set Signup Bonus</b>\n\nCurrent: <b>${cur:.2f}</b>\n\n"
        f"Bonus credited to the referred user on signup (0 = disabled):",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:admin")
        ]]),
    )
    return RD_ADM_BONUS


async def rd_adm_bonus_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number (0 = disabled).")
        return RD_ADM_BONUS
    cfg.set("referral_bonus", val)
    await update.message.reply_text(f"✅ Signup bonus set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def rd_adm_set_fpb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    cur = cfg.get_float("referral_first_purchase_bonus", 0.0)
    await _safe_edit(q,
        f"🛒 <b>Set First-Purchase Bonus</b>\n\nCurrent: <b>${cur:.2f}</b>\n\n"
        f"Extra bonus to referrer when their referred user makes their first purchase:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data="rd:admin")
        ]]),
    )
    return RD_ADM_FPB


async def rd_adm_fpb_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number (0 = disabled).")
        return RD_ADM_FPB
    cfg.set("referral_first_purchase_bonus", val)
    await update.message.reply_text(f"✅ First-purchase bonus set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


# ── Conversation builder ────────────────────────────────────────────────────────

def build_rd_admin_convs():
    """Return list of all admin referral setting conversations."""
    from telegram.ext import ConversationHandler, CallbackQueryHandler, MessageHandler, filters, CommandHandler
    convs = []
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_adm_set_commission_start, pattern=r"^rd:adm:set_commission$")],
        states={RD_ADM_COMMISSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, rd_adm_commission_input)]},
        fallbacks=[CallbackQueryHandler(rd_admin_menu, pattern=r"^rd:admin$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_adm_set_min_w_start, pattern=r"^rd:adm:set_min_w$")],
        states={RD_ADM_MIN_WITHDRAW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rd_adm_min_w_input)]},
        fallbacks=[CallbackQueryHandler(rd_admin_menu, pattern=r"^rd:admin$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_adm_set_max_w_start, pattern=r"^rd:adm:set_max_w$")],
        states={RD_ADM_MAX_WITHDRAW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rd_adm_max_w_input)]},
        fallbacks=[CallbackQueryHandler(rd_admin_menu, pattern=r"^rd:admin$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_adm_set_bonus_start, pattern=r"^rd:adm:set_bonus$")],
        states={RD_ADM_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rd_adm_bonus_input)]},
        fallbacks=[CallbackQueryHandler(rd_admin_menu, pattern=r"^rd:admin$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_adm_set_fpb_start, pattern=r"^rd:adm:set_fpb$")],
        states={RD_ADM_FPB: [MessageHandler(filters.TEXT & ~filters.COMMAND, rd_adm_fpb_input)]},
        fallbacks=[CallbackQueryHandler(rd_admin_menu, pattern=r"^rd:admin$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))
    return convs
