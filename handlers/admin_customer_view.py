"""Admin "🔍 Customer 360° View" panel.

Single-screen customer profile card for admins: search a user by Telegram ID
or @username and see everything about them in one place — wallet balance,
lifetime spend, order history breakdown, referral performance, support
ticket history, dispute history, and ban status — plus quick action buttons.

Quick actions deliberately reuse the existing, already-audited flows from
``handlers/admin_users.py`` (``usr:ban``/``usr:ubn``, ``usr:bal:add``,
``usr:ord``) instead of re-implementing wallet/ban mutations here, so every
balance change still goes through the same confirmation step, WalletLedger
row, and admin-audit log.

Callback / conversation map:
  c360:search        — entry point, prompts for Telegram ID or @username
  c360:view:{user_id} — render/refresh the profile card for a known user id
"""

from __future__ import annotations

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    filters,
)
from sqlalchemy import func

from database import (
    get_db_session,
    User,
    Order,
    Transaction,
    Dispute,
    SupportTicket,
    OrderStatus,
    DisputeStatus,
    TicketStatus,
    TransactionStatus,
)
from utils.helpers import format_price
from utils.permissions import has_permission
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# ── Conversation state ──────────────────────────────────────────────────────
WAITING_C360_SEARCH = 90

_RECENT_ORDERS_LIMIT = 5

_STATUS_ICON = {
    OrderStatus.PROCESSING: "⏳",
    OrderStatus.COMPLETED: "✅",
    OrderStatus.CANCELLED: "❌",
    OrderStatus.FAILED: "⚠️",
    OrderStatus.REFUNDED: "↩️",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _name(user: User) -> str:
    return f"@{user.username}" if user.username else f"User {user.telegram_id}"


def _name_esc(user: User) -> str:
    return html.escape(_name(user))


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _lookup_user(session, raw: str):
    """Find a user by Telegram ID or @username (case-insensitive)."""
    raw = (raw or "").strip()
    if raw.lstrip("@").lstrip("+").isdigit():
        tg_id = int(raw.lstrip("@").lstrip("+"))
        found = session.query(User).filter_by(telegram_id=tg_id).first()
        if found:
            return found
    uname = raw.lstrip("@")
    return session.query(User).filter(User.username.ilike(uname)).first()


def _build_profile(session, user: User) -> str:
    """Build the full HTML profile card text for one user."""

    # ── Orders / lifetime spend ─────────────────────────────────────────
    order_rows = (
        session.query(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0))
        .filter(Order.user_id == user.id)
        .group_by(Order.status)
        .all()
    )
    total_orders = sum(cnt for _, cnt, _ in order_rows)
    total_spent = 0.0
    for status, cnt, amount_sum in order_rows:
        if status == OrderStatus.COMPLETED:
            total_spent = float(amount_sum or 0.0)
    status_breakdown = {s: c for s, c, _ in order_rows}

    recent_orders = (
        session.query(Order)
        .filter_by(user_id=user.id)
        .order_by(Order.created_at.desc())
        .limit(_RECENT_ORDERS_LIMIT)
        .all()
    )

    # ── Wallet top-ups (completed transactions) ─────────────────────────
    total_topups = (
        session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == user.id,
            Transaction.status == TransactionStatus.COMPLETED,
        )
        .scalar()
        or 0.0
    )

    # ── Referrals ────────────────────────────────────────────────────────
    referral_count = session.query(User).filter_by(referred_by_id=user.id).count()
    referrer = None
    if user.referred_by_id:
        referrer = session.query(User).filter_by(id=user.referred_by_id).first()

    # ── Support tickets ──────────────────────────────────────────────────
    total_tickets = session.query(SupportTicket).filter_by(user_id=user.id).count()
    open_tickets = (
        session.query(SupportTicket)
        .filter_by(user_id=user.id, status=TicketStatus.OPEN)
        .count()
    )
    last_ticket = (
        session.query(SupportTicket)
        .filter_by(user_id=user.id)
        .order_by(SupportTicket.created_at.desc())
        .first()
    )

    # ── Disputes ─────────────────────────────────────────────────────────
    total_disputes = session.query(Dispute).filter_by(user_id=user.id).count()
    open_disputes = (
        session.query(Dispute)
        .filter_by(user_id=user.id, status=DisputeStatus.OPENED)
        .count()
    )

    # ── Assemble ─────────────────────────────────────────────────────────
    ban_line = "🚫 <b>BANNED</b>" if user.is_banned else "✅ Active"
    name = _name_esc(user)
    reg = _fmt_dt(user.created_at)
    referrer_line = _name_esc(referrer) if referrer else "— (organic signup)"

    lines = [
        "🔍 <b>Customer 360° View</b>",
        "",
        f"👤 {name}   |   🆔 <code>{user.telegram_id}</code>",
        f"📅 Joined: {reg}   |   🌐 Lang: {html.escape(user.language or 'en')}",
        f"Status: {ban_line}",
        "",
        "💰 <b>Wallet</b>",
        f"  Balance: <b>{format_price(float(user.wallet_balance or 0.0))}</b>",
        f"  Total Top-ups: {format_price(float(total_topups))}",
        f"  Referral Earnings: {format_price(float(user.referral_earnings or 0.0))}",
        "",
        "🛒 <b>Orders</b>",
        f"  Total Spent (completed): <b>{format_price(total_spent)}</b>",
        f"  Orders: {total_orders} total | "
        f"✅ {status_breakdown.get(OrderStatus.COMPLETED, 0)} completed | "
        f"⏳ {status_breakdown.get(OrderStatus.PROCESSING, 0)} processing | "
        f"❌ {status_breakdown.get(OrderStatus.CANCELLED, 0)} cancelled",
    ]

    if recent_orders:
        lines.append("  Recent:")
        for o in recent_orders:
            ico = _STATUS_ICON.get(o.status, "❓")
            lines.append(
                f"    {ico} #{o.id} — {format_price(float(o.total_amount or 0.0))} — {_fmt_dt(o.created_at)}"
            )
    else:
        lines.append("  No orders yet.")

    lines += [
        "",
        "🔁 <b>Referrals</b>",
        f"  Referred by: {referrer_line}",
        f"  Users Referred: {referral_count}",
        "",
        "🎫 <b>Support Tickets</b>",
        f"  Total: {total_tickets} | 🟢 Open: {open_tickets}",
    ]
    if last_ticket:
        lines.append(
            f"  Last: \"{html.escape((last_ticket.subject or '')[:40])}\" "
            f"[{last_ticket.status.value}] — {_fmt_dt(last_ticket.created_at)}"
        )

    lines += [
        "",
        "⚠️ <b>Disputes</b>",
        f"  Total: {total_disputes} | 🔴 Open: {open_disputes}",
    ]

    return "\n".join(lines)


def _profile_kb(user: User) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton("🟢 Unban", callback_data=f"usr:ubn:{user.id}")
        if user.is_banned
        else InlineKeyboardButton("🔴 Ban", callback_data=f"usr:ban:{user.id}")
    )
    return InlineKeyboardMarkup([
        [ban_btn, InlineKeyboardButton("➕ Add Balance", callback_data=f"usr:bal:add:{user.id}")],
        [InlineKeyboardButton("🧾 View Full Orders", callback_data=f"usr:ord:{user.id}:0")],
        [InlineKeyboardButton("📝 CRM Notes",   callback_data=f"crm:user:{user.id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"c360:view:{user.id}")],
        [InlineKeyboardButton("🔍 New Search", callback_data="c360:search")],
        [InlineKeyboardButton("↩️ Return", callback_data="admin_users")],
    ])


def _not_found_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Try Again", callback_data="c360:search")],
        [InlineKeyboardButton("↩️ Return", callback_data="admin_users")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — prompt for search text
# ─────────────────────────────────────────────────────────────────────────────

async def c360_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: c360:search — prompt admin for Telegram ID or @username."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    context.user_data.pop("_c360_msg_id", None)
    context.user_data.pop("_c360_chat_id", None)
    try:
        try:
            msg = await query.edit_message_text(
                "🔍 <b>Customer 360° View</b>\n\n"
                "Send a Telegram ID or @username to pull up the full profile.\n\n"
                "Send /cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        context.user_data["_c360_msg_id"] = msg.message_id
        context.user_data["_c360_chat_id"] = update.effective_chat.id
    except Exception:
        pass
    return WAITING_C360_SEARCH


async def c360_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive search text, look up the user, and render the profile card."""
    if not has_permission(update.effective_user.id, "manage_users"):
        context.user_data.pop("_c360_msg_id", None)
        context.user_data.pop("_c360_chat_id", None)
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    with get_db_session() as session:
        user = _lookup_user(session, raw)
        if not user:
            msg_id = context.user_data.get("_c360_msg_id")
            chat_id = context.user_data.get("_c360_chat_id", update.effective_chat.id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Return", callback_data="admin_users")]
            ])
            text = (
                "❌ No customer found for that ID/username.\n\n"
                "Send another Telegram ID or @username:\n\n"
                "Send /cancel to abort."
            )
            try:
                if msg_id:
                    try:
                        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                    except BadRequest as e:
                        if "Message is not modified" not in str(e):
                            raise
                else:
                    await update.effective_chat.send_message(text, reply_markup=kb)
            except Exception:
                await update.effective_chat.send_message(text, reply_markup=kb)
            return WAITING_C360_SEARCH

        profile_text = _build_profile(session, user)
        kb = _profile_kb(user)

    msg_id = context.user_data.pop("_c360_msg_id", None)
    chat_id = context.user_data.pop("_c360_chat_id", update.effective_chat.id)
    try:
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    profile_text, chat_id=chat_id, message_id=msg_id, reply_markup=kb, parse_mode="HTML",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.effective_chat.send_message(profile_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await update.effective_chat.send_message(profile_text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def c360_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_c360_msg_id", None)
    context.user_data.pop("_c360_chat_id", None)
    if update.message:
        try:
            await update.message.reply_text("🔍 Customer search cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def build_c360_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(c360_search_start, pattern="^c360:search$"),
        ],
        states={
            WAITING_C360_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, c360_search_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", c360_search_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Refresh / direct view by user id (e.g. after Ban/Unban/Add Balance actions,
# or linked to from elsewhere in the admin panel)
# ─────────────────────────────────────────────────────────────────────────────

async def c360_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: c360:view:{user_id} — render/refresh the profile card."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                try:
                    await query.edit_message_text("❌ Customer not found.", reply_markup=_not_found_kb())
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            except Exception:
                pass
            return
        profile_text = _build_profile(session, user)
        kb = _profile_kb(user)

    try:
        try:
            await query.edit_message_text(profile_text, reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass
