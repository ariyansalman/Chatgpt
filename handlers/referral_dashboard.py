"""Advanced Referral Dashboard — V20.

Callback namespace: ``rd:*``

Features:
  • Rich user-facing dashboard: clicks, registrations, commissions, rank
  • Commission breakdown: pending / available / withdrawn
  • Withdrawal request flow
  • Admin settings: commission %, min/max withdrawal, bonus, first-purchase bonus,
    lifetime referrals, multi-level, top referrers leaderboard
"""
from __future__ import annotations

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, User, Settings
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils import is_admin, safe_edit_message_text

logger = logging.getLogger(__name__)

# ── Conversation states (unique, non-colliding) ────────────────────────────────
RD_WITHDRAW_AMOUNT  = 50
RD_ADM_COMMISSION   = 51
RD_ADM_MIN_WITHDRAW = 52
RD_ADM_MAX_WITHDRAW = 53
RD_ADM_BONUS        = 54
RD_ADM_FPB          = 55   # first-purchase bonus
RD_ADM_MAX_LEVELS   = 56

# ── Rank thresholds ────────────────────────────────────────────────────────────
_RANKS = [
    (500, "👑 Diamond"),
    (200, "💎 Platinum"),
    (100, "🥇 Gold"),
    (50,  "🥈 Silver"),
    (25,  "🥉 Bronze"),
    (10,  "⭐ Rising Star"),
    (0,   "🌱 Newcomer"),
]


def _get_rank(referral_count: int) -> str:
    for threshold, name in _RANKS:
        if referral_count >= threshold:
            return name
    return "🌱 Newcomer"


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

def _get_referral_stats(user_id: int) -> dict:
    """Return referral stats for a single user (internal DB id)."""
    from sqlalchemy import func as sqlfunc
    stats = {
        "total_referrals": 0,
        "pending_commission": 0.0,
        "available_commission": 0.0,
        "withdrawn": 0.0,
        "clicks": 0,
        "rank": "🌱 Newcomer",
        "earnings_total": 0.0,
    }
    try:
        with get_db_session() as s:
            # Legacy referrals count (referred_by_id = this user's internal id)
            stats["total_referrals"] = s.query(sqlfunc.count(User.id)).filter(
                User.referred_by_id == user_id
            ).scalar() or 0

            stats["rank"] = _get_rank(stats["total_referrals"])

            # Referral earnings from legacy ReferralReward table
            try:
                from database.models import ReferralReward
                total_earned = s.query(sqlfunc.coalesce(sqlfunc.sum(
                    ReferralReward.amount
                ), 0.0)).filter(ReferralReward.referrer_id == user_id).scalar() or 0.0
                stats["earnings_total"] = float(total_earned)
            except Exception:
                pass

            # Advanced: referral_commissions table (may not exist yet)
            try:
                from sqlalchemy import text
                row = s.execute(text(
                    "SELECT "
                    "  COALESCE(SUM(CASE WHEN status='pending' THEN commission_amount ELSE 0 END), 0) AS pending,"
                    "  COALESCE(SUM(CASE WHEN status='available' THEN commission_amount ELSE 0 END), 0) AS available,"
                    "  COALESCE(SUM(CASE WHEN status='withdrawn' THEN commission_amount ELSE 0 END), 0) AS withdrawn "
                    "FROM referral_commissions WHERE referrer_id = :uid"
                ), {"uid": user_id}).fetchone()
                if row:
                    stats["pending_commission"] = float(row[0])
                    stats["available_commission"] = float(row[1])
                    stats["withdrawn"] = float(row[2])
            except Exception:
                pass

            # Click tracking (referral_clicks table may not exist yet)
            try:
                from sqlalchemy import text as sqltxt
                row2 = s.execute(sqltxt(
                    "SELECT COUNT(*) FROM referral_clicks WHERE referrer_id = :uid"
                ), {"uid": user_id}).fetchone()
                stats["clicks"] = int(row2[0]) if row2 else 0
            except Exception:
                pass
    except Exception:
        logger.exception("_get_referral_stats failed for user %s", user_id)
    return stats


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
# User-facing dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def rd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advanced referral dashboard main view (rd:menu)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    if not cfg.get_bool("feature_referral_dashboard_enabled", True):
        await _safe_edit(query, "👥 Referral dashboard is currently disabled.")
        return

    # Get user's internal ID
    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.")
            return
        user_id = user.id
        has_purchased = user.has_purchased

    stats = _get_referral_stats(user_id)

    # Settings
    s_obj = None
    referral_enabled = True
    reward_amount = 0.10
    commission_pct = cfg.get_float("referral_commission_pct", 0.0)
    min_withdraw = cfg.get_float("referral_min_withdrawal", 5.0)

    try:
        with get_db_session() as s:
            s_obj = s.query(Settings).first()
            if s_obj:
                referral_enabled = bool(s_obj.referral_enabled)
                reward_amount = float(s_obj.referral_reward_amount or 0.10)
    except Exception:
        pass

    # Bot username for referral link
    try:
        bot_username = (await context.bot.get_me()).username
    except Exception:
        bot_username = "yourbot"
    link = f"https://t.me/{bot_username}?start=ref_{tid}"

    lines = [
        "👥 <b>Advanced Referral Dashboard</b>\n",
        f"Your rank: <b>{stats['rank']}</b>",
        f"Total referrals: <b>{stats['total_referrals']}</b>",
        f"Link clicks: <b>{stats['clicks']}</b>",
        "",
        "💰 <b>Commissions</b>",
        f"  ⏳ Pending:   <b>${stats['pending_commission']:.2f}</b>",
        f"  ✅ Available: <b>${stats['available_commission']:.2f}</b>",
        f"  📤 Withdrawn: <b>${stats['withdrawn']:.2f}</b>",
        f"  📊 Legacy earnings: <b>${stats['earnings_total']:.2f}</b>",
        "",
        f"💎 Referral reward per signup: <b>${reward_amount:.2f}</b>",
    ]
    if commission_pct > 0:
        lines.append(f"💸 Commission on referrals' purchases: <b>{commission_pct:.1f}%</b>")
    lines.append(f"\n🔗 <b>Your Link:</b>\n<code>{link}</code>")

    from urllib.parse import quote
    share_url = f"https://t.me/share/url?url={quote(link)}"

    kb: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📊 Commission History", callback_data="rd:comm"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="rd:top")],
        [InlineKeyboardButton("🔗 Share Link", url=share_url)],
    ]
    if stats["available_commission"] >= min_withdraw:
        kb.append([InlineKeyboardButton(
            f"💸 Withdraw ${stats['available_commission']:.2f}",
            callback_data="rd:withdraw"
        )])
    elif min_withdraw > 0:
        kb.append([InlineKeyboardButton(
            f"💸 Withdraw (min ${min_withdraw:.2f})",
            callback_data="noop"
        )])

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_commissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show commission history (rd:comm)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.")
            return
        user_id = user.id

    lines = ["💰 <b>Commission History</b>\n"]
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT commission_amount, status, created_at "
                "FROM referral_commissions WHERE referrer_id = :uid "
                "ORDER BY created_at DESC LIMIT 20"
            ), {"uid": user_id}).fetchall()
            if rows:
                for r in rows:
                    icon = {"pending": "⏳", "available": "✅", "withdrawn": "📤"}.get(r[1], "•")
                    dt = r[2].strftime("%b %d") if r[2] else ""
                    lines.append(f"{icon} ${r[0]:.4f} — {r[1]}  <i>{dt}</i>")
            else:
                lines.append("No commissions yet.")
    except Exception:
        lines.append("Commission tracking not yet active.")

    # Legacy referral rewards
    try:
        from database.models import ReferralReward
        with get_db_session() as s:
            rewards = s.query(ReferralReward).filter_by(referrer_id=user_id).limit(10).all()
            if rewards:
                lines.append("\n<b>Legacy Referral Bonuses</b>")
                for r in rewards:
                    dt = r.created_at.strftime("%b %d") if r.created_at else ""
                    lines.append(f"✅ ${r.amount:.2f}  <i>{dt}</i>")
    except Exception:
        pass

    kb = [[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_top_referrers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leaderboard view (rd:top)."""
    query = update.callback_query
    await query.answer()

    top = _get_top_referrers(15)
    lines = ["🏆 <b>Top Referrers</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for entry in top:
        medal = medals[entry["rank"] - 1] if entry["rank"] <= 3 else f"{entry['rank']}."
        lines.append(
            f"{medal} @{entry['username']} — <b>{entry['count']}</b> referrals"
        )
    if not top:
        lines.append("No referrals yet — be the first!")

    kb = [[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start withdrawal conversation (rd:withdraw)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    min_w = cfg.get_float("referral_min_withdrawal", 5.0)
    max_w = cfg.get_float("referral_max_withdrawal", 0.0)

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=tid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.")
            return ConversationHandler.END
        user_id = user.id

    stats = _get_referral_stats(user_id)
    available = stats["available_commission"]

    if available < min_w:
        await _safe_edit(
            query,
            f"💸 <b>Withdrawal</b>\n\nYou need at least <b>${min_w:.2f}</b> available.\n"
            f"Your available balance: <b>${available:.2f}</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="rd:menu")
            ]]),
        )
        return ConversationHandler.END

    limit_note = f" (max ${max_w:.2f})" if max_w > 0 else ""
    await _safe_edit(
        query,
        f"💸 <b>Withdrawal Request</b>\n\n"
        f"Available: <b>${available:.2f}</b>\n"
        f"Minimum: <b>${min_w:.2f}</b>{limit_note}\n\n"
        f"Send the amount you wish to withdraw:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:menu")
        ]]),
    )
    context.user_data["_rd_available"] = available
    return RD_WITHDRAW_AMOUNT


async def rd_withdraw_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive withdrawal amount."""
    tid = update.effective_user.id
    text = (update.message.text or "").strip()
    min_w = cfg.get_float("referral_min_withdrawal", 5.0)
    max_w = cfg.get_float("referral_max_withdrawal", 0.0)
    available = context.user_data.get("_rd_available", 0.0)

    try:
        amount = float(text)
        if amount < min_w:
            raise ValueError("below minimum")
        if max_w > 0 and amount > max_w:
            raise ValueError("above maximum")
        if amount > available:
            raise ValueError("above available")
    except ValueError as e:
        hint = (f"Amount must be between ${min_w:.2f} and ${max_w:.2f}."
                if max_w > 0 else f"Amount must be at least ${min_w:.2f}.")
        await update.message.reply_text(f"❌ Invalid amount. {hint}")
        return RD_WITHDRAW_AMOUNT

    # Create withdrawal request
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=tid).first()
            if user:
                s.execute(text(
                    "INSERT INTO referral_withdrawals (user_id, amount, status, created_at) "
                    "VALUES (:uid, :amt, 'pending', NOW())"
                ), {"uid": user.id, "amt": amount})
                s.commit()
    except Exception:
        logger.exception("rd_withdraw insert failed")

    await update.message.reply_text(
        f"✅ Withdrawal request of <b>${amount:.2f}</b> submitted.\n"
        f"An admin will process it shortly.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 My Dashboard", callback_data="rd:menu")
        ]]),
    )
    context.user_data.pop("_rd_available", None)
    return ConversationHandler.END


async def rd_withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel withdrawal."""
    q = update.callback_query
    if q:
        await q.answer()
        await _safe_edit(q, "❌ Withdrawal cancelled.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🔙 Back", callback_data="rd:menu")
                         ]]))
    context.user_data.pop("_rd_available", None)
    return ConversationHandler.END


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
    dashboard_on = cfg.get_bool("feature_referral_dashboard_enabled", True)

    lines = [
        "👥 <b>Advanced Referral Settings</b>\n",
        f"Dashboard: {'✅ ON' if dashboard_on else '❌ OFF'}",
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
        [InlineKeyboardButton(
            "❌ Disable Dashboard" if dashboard_on else "✅ Enable Dashboard",
            callback_data="rd:adm:toggle_dashboard"
        )],
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
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="rd:top"),
         InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def rd_admin_toggle_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle referral dashboard feature (rd:adm:toggle_dashboard)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return
    current = cfg.get_bool("feature_referral_dashboard_enabled", True)
    cfg.set("feature_referral_dashboard_enabled", not current)
    await rd_admin_menu(update, context)


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
            InlineKeyboardButton("❌ Cancel", callback_data="rd:admin")
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

def build_rd_withdraw_conv():
    from telegram.ext import ConversationHandler, CallbackQueryHandler, MessageHandler, filters, CommandHandler
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(rd_withdraw_start, pattern=r"^rd:withdraw$")],
        states={
            RD_WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rd_withdraw_input),
                CallbackQueryHandler(rd_withdraw_cancel, pattern=r"^rd:menu$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(rd_withdraw_cancel, pattern=r"^rd:menu$")],
        per_user=True, per_chat=True, allow_reentry=True,
    )


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
