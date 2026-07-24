"""Withdrawal Approval Handlers — V29.

Callback namespace: ``wda:*``

Features
--------
User:
  • Full withdrawal creation flow: payment method → wallet address → amount → confirm
  • Withdrawal history with status tracking
  • Cancel a pending withdrawal
  • Estimated processing time

Admin:
  • Withdrawal Approval Manager: list, filter, detail, approve, reject, cancel, complete
  • Under-review marking and processing state
  • Internal admin notes
  • Withdrawal Approval Settings panel (status, auto-approval, limits, processing time)
  • Admin dashboard stats widget
  • User notifications on every status change
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    filters,
)
from telegram.error import BadRequest

from database import get_db_session, User
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils import is_admin
import services.withdrawal_approval as wda_svc
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# ── Conversation states (unique, non-colliding — 60..70) ──────────────────────
WDA_METHOD           = 60
WDA_ADDRESS          = 61
WDA_AMOUNT           = 62
WDA_ADM_REJECT       = 63
WDA_ADM_NOTE         = 64
WDA_ADM_PROC_TIME    = 65
WDA_ADM_AUTO_MAX     = 66
WDA_ADM_MIN          = 67
WDA_ADM_MAX          = 68
WDA_ADM_MAX_DAILY    = 69


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_method(key: str) -> str:
    return wda_svc.PAYMENT_METHODS.get(key, key)


def _fmt_status(st: str) -> str:
    return wda_svc.STATUS_LABELS.get(st, st)


async def _safe_edit(query, text: str, markup=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def _notify_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_tg_id: int,
    text: str,
) -> None:
    """Send a DM to the user; failures are non-fatal."""
    try:
        await context.bot.send_message(
            chat_id=user_tg_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.debug("_notify_user: failed to send to %s", user_tg_id)


def _processing_time_note() -> str:
    pt = cfg.get("withdrawal_approval_processing_time", "1-3 business days") or "1-3 business days"
    return f"⏱ Estimated: <b>{pt}</b>"


def _back_to_dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 My Dashboard", callback_data="rd:menu")
    ]])


# ─────────────────────────────────────────────────────────────────────────────
# User: resolve internal user ID
# ─────────────────────────────────────────────────────────────────────────────

def _get_user_id(telegram_id: int) -> Optional[int]:
    with get_db_session() as s:
        u = s.query(User).filter_by(telegram_id=telegram_id).first()
        return u.id if u else None


# ─────────────────────────────────────────────────────────────────────────────
# User withdrawal creation flow
# Entry: rd:withdraw → wda_start
# ─────────────────────────────────────────────────────────────────────────────

async def wda_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start withdrawal flow — check feature status and available balance."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    feature_status = wda_svc.get_feature_status()
    if feature_status == "disabled":
        await _safe_edit(
            query,
            "❌ <b>Withdrawals are currently disabled.</b>\n\nPlease try again later.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]),
        )
        return ConversationHandler.END

    if feature_status == "maintenance":
        await _safe_edit(
            query,
            "🔧 <b>Withdrawals are under maintenance.</b>\n\nPlease try again shortly.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]),
        )
        return ConversationHandler.END

    # Resolve internal user ID
    user_id = _get_user_id(tid)
    if not user_id:
        await _safe_edit(query, "❌ User not found.")
        return ConversationHandler.END

    # Block if user already has an active withdrawal
    if wda_svc.has_pending_withdrawal(user_id):
        await _safe_edit(
            query,
            "⚠️ <b>You already have an active withdrawal request.</b>\n\n"
            "Please wait for it to be processed before submitting a new one.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 View History", callback_data="wda:history")],
                [InlineKeyboardButton("🔙 Back", callback_data="rd:menu")],
            ]),
        )
        return ConversationHandler.END

    # Check minimum balance
    min_amt = cfg.get_float("withdrawal_approval_min_amount", 5.0)
    available = wda_svc.get_available_balance(user_id)
    if available < min_amt:
        await _safe_edit(
            query,
            f"💸 <b>Withdrawal</b>\n\n"
            f"You need at least <b>${min_amt:.2f}</b> available commission.\n"
            f"Your current available balance: <b>${available:.2f}</b>",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]),
        )
        return ConversationHandler.END

    context.user_data["_wda_available"] = available
    context.user_data["_wda_user_id"] = user_id

    # Show payment method selection
    max_amt = cfg.get_float("withdrawal_approval_max_amount", 0.0)
    limit_note = f" (max ${max_amt:.2f})" if max_amt > 0 else ""
    kb = []
    for method_key, method_label in wda_svc.PAYMENT_METHODS.items():
        kb.append([InlineKeyboardButton(method_label, callback_data=f"wda:m:{method_key}")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="rd:menu")])

    await _safe_edit(
        query,
        f"💸 <b>Withdrawal Request</b>\n\n"
        f"Available: <b>${available:.2f}</b>\n"
        f"Minimum: <b>${min_amt:.2f}</b>{limit_note}\n"
        f"{_processing_time_note()}\n\n"
        f"<b>Select payment method:</b>",
        InlineKeyboardMarkup(kb),
    )
    return WDA_METHOD


async def wda_method_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected a payment method; ask for wallet address."""
    query = update.callback_query
    await query.answer()

    method_key = query.data.replace("wda:m:", "")
    if method_key not in wda_svc.PAYMENT_METHODS:
        await query.answer("❌ Invalid method. Please choose from the list.", show_alert=True)
        return WDA_METHOD

    context.user_data["_wda_method"] = method_key
    method_label = _fmt_method(method_key)

    # Prompt varies by method
    if method_key == "mobile_banking":
        prompt = (
            f"🏦 <b>Mobile Banking Details</b>\n\n"
            f"Please send your bank name, account number, and account holder name.\n"
            f"Example: <code>bKash | 01XXXXXXXXX | John Doe</code>"
        )
    elif method_key in ("binance_pay", "bybit_pay"):
        prompt = (
            f"{method_label}\n\n"
            f"Please send your Pay ID or registered email/phone."
        )
    else:
        prompt = (
            f"{method_label}\n\n"
            f"Please send your wallet address:"
        )

    await _safe_edit(
        query,
        f"💸 <b>Withdrawal — {method_label}</b>\n\n{prompt}",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="rd:menu")]]),
    )
    return WDA_ADDRESS


async def wda_address_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive wallet address / payment details; ask for amount."""
    address = (update.message.text or "").strip()
    if not address or len(address) < 3:
        await update.message.reply_text(
            "❌ Invalid input. Please send your wallet address or payment details."
        )
        return WDA_ADDRESS

    context.user_data["_wda_address"] = address
    available = context.user_data.get("_wda_available", 0.0)
    min_amt = cfg.get_float("withdrawal_approval_min_amount", 5.0)
    max_amt = cfg.get_float("withdrawal_approval_max_amount", 0.0)
    limit_note = f" (max ${max_amt:.2f})" if max_amt > 0 else ""

    await update.message.reply_text(
        f"💰 <b>Enter Amount</b>\n\n"
        f"Available: <b>${available:.2f}</b>\n"
        f"Minimum: <b>${min_amt:.2f}</b>{limit_note}\n\n"
        f"How much do you want to withdraw?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="rd:menu")
        ]]),
    )
    return WDA_AMOUNT


async def wda_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive amount, validate, create withdrawal, and notify."""
    tid = update.effective_user.id
    text = (update.message.text or "").strip()
    available = context.user_data.get("_wda_available", 0.0)
    min_amt = cfg.get_float("withdrawal_approval_min_amount", 5.0)
    max_amt = cfg.get_float("withdrawal_approval_max_amount", 0.0)
    method  = context.user_data.get("_wda_method", "")
    address = context.user_data.get("_wda_address", "")

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError("non-positive")
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid positive number (e.g. <code>25.00</code>).",
            parse_mode="HTML",
        )
        return WDA_AMOUNT

    if amount < min_amt:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is <b>${min_amt:.2f}</b>.", parse_mode="HTML"
        )
        return WDA_AMOUNT

    if max_amt > 0 and amount > max_amt:
        await update.message.reply_text(
            f"❌ Maximum withdrawal is <b>${max_amt:.2f}</b>.", parse_mode="HTML"
        )
        return WDA_AMOUNT

    if amount > available:
        await update.message.reply_text(
            f"❌ Insufficient balance. Available: <b>${available:.2f}</b>.", parse_mode="HTML"
        )
        return WDA_AMOUNT

    # Create the withdrawal
    result = wda_svc.create_withdrawal(tid, amount, method, address)

    if result is None:
        await update.message.reply_text(
            "❌ An error occurred. Please try again later."
        )
        return ConversationHandler.END

    if isinstance(result, dict) and "error" in result:
        err = result["error"]
        if err == "duplicate":
            msg = "⚠️ You already have an active withdrawal request."
        elif err == "daily_limit":
            msg = "⚠️ You've reached the daily withdrawal limit."
        elif err == "insufficient":
            msg = f"❌ Insufficient balance. Available: ${available:.2f}"
        else:
            msg = "❌ Withdrawal request failed. Please try again."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    wid     = result.get("id")
    status  = result.get("status", "pending")
    method_label = _fmt_method(method)
    status_label = _fmt_status(status)

    await update.message.reply_text(
        f"✅ <b>Withdrawal Request Submitted</b>\n\n"
        f"ID: <code>#{wid}</code>\n"
        f"Amount: <b>${amount:.2f}</b>\n"
        f"Method: {method_label}\n"
        f"Status: {status_label}\n\n"
        f"{_processing_time_note()}\n\n"
        f"You'll be notified on every status update.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View History", callback_data="wda:history")],
            [InlineKeyboardButton("🔙 My Dashboard", callback_data="rd:menu")],
        ]),
    )

    # Notify admins
    try:
        from services.notifications import notify_admins
        from utils.notify_format import render as _render_notif, utc_now_str as _ts
        from utils.helpers import format_withdrawal_id as _fmt_wid
        await notify_admins(
            context.bot,
            event="manual_payment",
            text=_render_notif("💸", "Withdrawal Requested", [
                ("Withdrawal ID", _fmt_wid(wid)),
                ("Customer", f"@{update.effective_user.username}" if update.effective_user.username else f"<code>{tid}</code>"),
                ("Amount", f"${amount:.2f}"),
                ("Method", method_label),
                ("Address", address[:60]),
            ], _ts()),
        )
    except Exception:
        logger.debug("wda: admin notification failed (non-fatal)")

    # Clean up user_data
    for k in ("_wda_available", "_wda_user_id", "_wda_method", "_wda_address"):
        context.user_data.pop(k, None)

    return ConversationHandler.END


async def wda_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the withdrawal conversation."""
    for k in ("_wda_available", "_wda_user_id", "_wda_method", "_wda_address"):
        context.user_data.pop(k, None)
    q = update.callback_query
    if q:
        await q.answer()
        await _safe_edit(
            q,
            "❌ Withdrawal cancelled.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]),
        )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# User: withdrawal history
# ─────────────────────────────────────────────────────────────────────────────

async def wda_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user withdrawal history (wda:history)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    user_id = _get_user_id(tid)
    if not user_id:
        await _safe_edit(query, "❌ User not found.")
        return

    withdrawals = wda_svc.list_withdrawals(user_id=user_id, limit=10)
    if not withdrawals:
        await _safe_edit(
            query,
            "📋 <b>Withdrawal History</b>\n\nNo withdrawal requests yet.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="rd:menu")]]),
        )
        return

    lines = ["📋 <b>Withdrawal History</b>\n"]
    kb = []
    for w in withdrawals:
        status_label = _fmt_status(w.get("status", ""))
        method_label = _fmt_method(w.get("payment_method") or "")
        dt = w["created_at"].strftime("%b %d, %H:%M") if w.get("created_at") else ""
        lines.append(
            f"• <code>#{w['id']}</code>  <b>${float(w['amount']):.2f}</b>  "
            f"{status_label}  <i>{dt}</i>"
        )
        kb.append([InlineKeyboardButton(
            f"📄 #{w['id']} — {_fmt_status(w['status'])}",
            callback_data=f"wda:status:{w['id']}",
        )])

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="rd:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def wda_status_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detail of a single withdrawal (wda:status:<id>)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    w = wda_svc.get_withdrawal(wid)
    if not w or w.get("user_tg_id") != tid:
        await query.answer("❌ Not found.", show_alert=True)
        return

    status_label = _fmt_status(w.get("status", ""))
    method_label = _fmt_method(w.get("payment_method") or "—")
    dt = w["created_at"].strftime("%Y-%m-%d %H:%M UTC") if w.get("created_at") else "—"
    reason = w.get("reason") or ""

    lines = [
        f"📄 <b>Withdrawal #{wid}</b>\n",
        f"Amount: <b>${float(w['amount']):.2f}</b>",
        f"Method: {method_label}",
        f"Status: {status_label}",
        f"Submitted: <i>{dt}</i>",
    ]
    if reason:
        lines.append(f"Reason: {reason}")

    kb = []
    if w.get("status") == "pending":
        kb.append([InlineKeyboardButton("🚫 Cancel Withdrawal", callback_data=f"wda:cancel_user:{wid}")])
    kb.append([InlineKeyboardButton("🔙 History", callback_data="wda:history")])

    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def wda_cancel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User cancels their own pending withdrawal (wda:cancel_user:<id>)."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    w = wda_svc.get_withdrawal(wid)
    if not w or w.get("user_tg_id") != tid:
        await query.answer("❌ Not found.", show_alert=True)
        return

    if w.get("status") != "pending":
        await query.answer("❌ Only pending withdrawals can be cancelled.", show_alert=True)
        return

    result = wda_svc.cancel_withdrawal(wid, reason="Cancelled by user")
    if result:
        await _safe_edit(
            query,
            f"🚫 Withdrawal <code>#{wid}</code> has been cancelled.\n\n"
            f"Your commission balance has been restored.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 History", callback_data="wda:history")]]),
        )
    else:
        await query.answer("❌ Could not cancel. Please try again.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Withdrawal Approval Manager
# ─────────────────────────────────────────────────────────────────────────────

def _admin_guard(update: Update) -> bool:
    return has_permission(update.effective_user.id, "manage_settings")


async def wda_adm_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: list withdrawals with optional status filter (wda:adm:list[:<status>])."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = query.data.split(":")
    # wda:adm:list  OR  wda:adm:list:pending  etc.
    status_filter = parts[3] if len(parts) > 3 and parts[3] != "all" else None

    stats = wda_svc.get_admin_stats()
    withdrawals = wda_svc.list_withdrawals(status=status_filter, limit=15)

    # Header with stats
    feature_status = wda_svc.get_feature_status()
    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(feature_status, "⚪")
    lines = [
        f"💸 <b>Withdrawal Approval Manager</b>  {status_icon}\n",
        f"⏳ Pending: <b>{stats['pending']}</b>   "
        f"👀 Under Review: <b>{stats['under_review']}</b>",
        f"✅ Approved today: <b>{stats['approved_today']}</b>   "
        f"❌ Rejected today: <b>{stats['rejected_today']}</b>",
        f"🎉 Completed today: <b>{stats['completed_today']}</b>   "
        f"💰 Total volume: <b>${stats['total_volume']:.2f}</b>",
    ]
    if stats.get("avg_processing_minutes") is not None:
        mins = int(stats["avg_processing_minutes"])
        lines.append(f"⏱ Avg processing: <b>{mins}m</b>")

    if not withdrawals:
        filter_label = f" ({_fmt_status(status_filter)})" if status_filter else ""
        lines.append(f"\n✅ No withdrawal requests{filter_label}.")

    else:
        filter_label = f" — {_fmt_status(status_filter)}" if status_filter else ""
        lines.append(f"\n<b>Requests{filter_label}:</b>")
        for w in withdrawals:
            st = _fmt_status(w.get("status", ""))
            method = _fmt_method(w.get("payment_method") or "—")
            dt = w["created_at"].strftime("%m/%d %H:%M") if w.get("created_at") else ""
            uname = w.get("user_username") or str(w.get("user_tg_id", "?"))
            lines.append(
                f"• <code>#{w['id']}</code>  @{uname}  "
                f"<b>${float(w['amount']):.2f}</b>  {st}  {method}  <i>{dt}</i>"
            )

    # Filter buttons
    filter_row = [
        InlineKeyboardButton("⏳", callback_data="wda:adm:list:pending"),
        InlineKeyboardButton("👀", callback_data="wda:adm:list:under_review"),
        InlineKeyboardButton("✅", callback_data="wda:adm:list:approved"),
        InlineKeyboardButton("💸", callback_data="wda:adm:list:processing"),
        InlineKeyboardButton("🎉", callback_data="wda:adm:list:completed"),
        InlineKeyboardButton("❌", callback_data="wda:adm:list:rejected"),
        InlineKeyboardButton("🔄 All", callback_data="wda:adm:list:all"),
    ]

    # Detail buttons for listed items
    detail_rows = []
    for w in withdrawals[:8]:
        detail_rows.append([InlineKeyboardButton(
            f"📄 #{w['id']} — ${float(w['amount']):.2f} {_fmt_status(w['status'])}",
            callback_data=f"wda:adm:detail:{w['id']}",
        )])

    kb = [filter_row] + detail_rows + [
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="wda:adm:settings"),
            InlineKeyboardButton("🔙 Back", callback_data="rd:admin"),
        ],
    ]

    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def wda_adm_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: detail view of a single withdrawal (wda:adm:detail:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    w = wda_svc.get_withdrawal(wid)
    if not w:
        await query.answer("❌ Withdrawal not found.", show_alert=True)
        return

    status = w.get("status", "")
    status_label = _fmt_status(status)
    method_label = _fmt_method(w.get("payment_method") or "—")
    wallet = w.get("wallet_address") or "—"
    uname = w.get("user_username") or str(w.get("user_tg_id", "?"))
    dt = w["created_at"].strftime("%Y-%m-%d %H:%M UTC") if w.get("created_at") else "—"
    reason = w.get("reason") or ""
    notes  = w.get("notes") or w.get("admin_note") or ""
    approval_time = w.get("approval_time")
    completion_time = w.get("completion_time")

    lines = [
        f"📄 <b>Withdrawal #{wid}</b>\n",
        f"👤 User: @{uname} (<code>{w.get('user_tg_id')}</code>)",
        f"💰 Amount: <b>${float(w['amount']):.2f}</b>",
        f"📋 Method: {method_label}",
        f"🏦 Address: <code>{wallet[:80]}</code>",
        f"📊 Status: {status_label}",
        f"🕐 Submitted: <i>{dt}</i>",
    ]
    if approval_time:
        lines.append(f"✅ Approved: <i>{approval_time.strftime('%Y-%m-%d %H:%M')}</i>")
    if completion_time:
        lines.append(f"🎉 Completed: <i>{completion_time.strftime('%Y-%m-%d %H:%M')}</i>")
    if reason:
        lines.append(f"📝 Reason: {reason}")
    if notes:
        lines.append(f"🗒 Notes: <i>{notes}</i>")

    # Build action buttons based on current status
    action_kb = []
    if status == "pending":
        action_kb.append([
            InlineKeyboardButton("👀 Under Review", callback_data=f"wda:adm:review:{wid}"),
            InlineKeyboardButton("✅ Approve", callback_data=f"wda:adm:approve:{wid}"),
        ])
        action_kb.append([
            InlineKeyboardButton("❌ Reject", callback_data=f"wda:adm:reject:{wid}"),
            InlineKeyboardButton("🚫 Cancel", callback_data=f"wda:adm:cancel:{wid}"),
        ])
    elif status == "under_review":
        action_kb.append([
            InlineKeyboardButton("✅ Approve", callback_data=f"wda:adm:approve:{wid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"wda:adm:reject:{wid}"),
        ])
        action_kb.append([
            InlineKeyboardButton("🚫 Cancel", callback_data=f"wda:adm:cancel:{wid}"),
        ])
    elif status == "approved":
        action_kb.append([
            InlineKeyboardButton("💸 Mark Processing", callback_data=f"wda:adm:processing:{wid}"),
            InlineKeyboardButton("🎉 Complete", callback_data=f"wda:adm:complete:{wid}"),
        ])
        action_kb.append([
            InlineKeyboardButton("❌ Reject", callback_data=f"wda:adm:reject:{wid}"),
        ])
    elif status == "processing":
        action_kb.append([
            InlineKeyboardButton("🎉 Mark Completed", callback_data=f"wda:adm:complete:{wid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"wda:adm:reject:{wid}"),
        ])

    if status not in wda_svc._TERMINAL_STATUSES:
        action_kb.append([
            InlineKeyboardButton("🗒 Add Note", callback_data=f"wda:adm:note:{wid}"),
        ])

    action_kb.append([InlineKeyboardButton("🔙 Back", callback_data="wda:adm:list")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(action_kb))


async def wda_adm_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: approve a withdrawal (wda:adm:approve:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    result = wda_svc.approve_withdrawal(wid, update.effective_user.id)
    if not result:
        await query.answer("❌ Could not approve — status may have changed.", show_alert=True)
        # Re-show list
        await wda_adm_list(with_data(update, "wda:adm:list"), context)
        return

    await query.answer(f"✅ Withdrawal #{wid} approved!", show_alert=True)

    # Notify user
    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"✅ <b>Withdrawal #{wid} Approved</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Method: {_fmt_method(result.get('payment_method') or '')}\n\n"
            f"{_processing_time_note()}\n\n"
            f"We'll notify you once the transfer is processed.",
        )

    # Refresh detail view
    await wda_adm_detail(with_data(update, f"wda:adm:detail:{wid}"), context)


async def wda_adm_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: mark withdrawal as under review (wda:adm:review:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    result = wda_svc.mark_under_review(wid, update.effective_user.id)
    if not result:
        await query.answer("❌ Could not update — status may have changed.", show_alert=True)
        return

    await query.answer(f"👀 Withdrawal #{wid} marked Under Review.", show_alert=True)

    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"👀 <b>Withdrawal #{wid} Under Review</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Our team is reviewing your withdrawal request.",
        )

    await wda_adm_detail(with_data(update, f"wda:adm:detail:{wid}"), context)


async def wda_adm_processing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: mark withdrawal as processing (wda:adm:processing:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    result = wda_svc.mark_processing(wid, update.effective_user.id)
    if not result:
        await query.answer("❌ Could not update — must be in 'approved' status.", show_alert=True)
        return

    await query.answer(f"💸 Withdrawal #{wid} is now Processing.", show_alert=True)

    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"💸 <b>Withdrawal #{wid} Processing</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Your withdrawal is being processed. You'll receive funds shortly.",
        )

    await wda_adm_detail(with_data(update, f"wda:adm:detail:{wid}"), context)


async def wda_adm_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: mark withdrawal as completed (wda:adm:complete:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    result = wda_svc.complete_withdrawal(wid, update.effective_user.id)
    if not result:
        await query.answer("❌ Could not complete — must be approved or processing.", show_alert=True)
        return

    await query.answer(f"🎉 Withdrawal #{wid} marked Completed!", show_alert=True)

    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"🎉 <b>Withdrawal #{wid} Completed!</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Method: {_fmt_method(result.get('payment_method') or '')}\n\n"
            f"Your funds have been sent. Thank you! 🙏",
        )

    await wda_adm_detail(with_data(update, f"wda:adm:detail:{wid}"), context)


async def wda_adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: cancel a withdrawal (wda:adm:cancel:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    result = wda_svc.cancel_withdrawal(wid, reason="Cancelled by admin", admin_tg_id=update.effective_user.id)
    if not result:
        await query.answer("❌ Could not cancel — already in terminal state.", show_alert=True)
        return

    await query.answer(f"🚫 Withdrawal #{wid} cancelled.", show_alert=True)

    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"🚫 <b>Withdrawal #{wid} Cancelled</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Your withdrawal has been cancelled. Your commission balance has been restored.",
        )

    await wda_adm_list(with_data(update, "wda:adm:list"), context)


# ── Admin: reject conversation ────────────────────────────────────────────────

async def wda_adm_reject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start rejection conversation (wda:adm:reject:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return ConversationHandler.END

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["_wda_adm_reject_id"] = wid
    await _safe_edit(
        query,
        f"❌ <b>Reject Withdrawal #{wid}</b>\n\n"
        f"Please send the rejection reason (shown to the user):",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"wda:adm:detail:{wid}")
        ]]),
    )
    return WDA_ADM_REJECT


async def wda_adm_reject_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive rejection reason and reject the withdrawal."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    reason = (update.message.text or "").strip()
    if not reason:
        await update.message.reply_text("❌ Please enter a rejection reason.")
        return WDA_ADM_REJECT

    wid = context.user_data.pop("_wda_adm_reject_id", None)
    if not wid:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    result = wda_svc.reject_withdrawal(wid, update.effective_user.id, reason)
    if not result:
        await update.message.reply_text("❌ Could not reject — already in terminal state.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"❌ Withdrawal #{wid} rejected.\nReason: {reason}", parse_mode="HTML"
    )

    user_tg_id = result.get("user_tg_id")
    if user_tg_id:
        await _notify_user(
            context,
            user_tg_id,
            f"❌ <b>Withdrawal #{wid} Rejected</b>\n\n"
            f"Amount: <b>${float(result['amount']):.2f}</b>\n"
            f"Reason: {reason}\n\n"
            f"Your commission balance has been restored.",
        )
    return ConversationHandler.END


# ── Admin: add note conversation ──────────────────────────────────────────────

async def wda_adm_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add-note conversation (wda:adm:note:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return ConversationHandler.END

    try:
        wid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["_wda_adm_note_id"] = wid
    await _safe_edit(
        query,
        f"🗒 <b>Add Internal Note for Withdrawal #{wid}</b>\n\n"
        f"This note is only visible to admins:",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"wda:adm:detail:{wid}")
        ]]),
    )
    return WDA_ADM_NOTE


async def wda_adm_note_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and save admin note."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    note = (update.message.text or "").strip()
    if not note:
        await update.message.reply_text("❌ Please enter a note.")
        return WDA_ADM_NOTE

    wid = context.user_data.pop("_wda_adm_note_id", None)
    if not wid:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END

    ok = wda_svc.add_admin_note(wid, update.effective_user.id, note)
    if ok:
        await update.message.reply_text(f"✅ Note saved for withdrawal #{wid}.")
    else:
        await update.message.reply_text("❌ Failed to save note.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Settings panel
# ─────────────────────────────────────────────────────────────────────────────

async def wda_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Withdrawal Approval Settings panel (wda:adm:settings)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    feature_status = wda_svc.get_feature_status()
    status_icon = {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance", "disabled": "🔴 Disabled"}.get(
        feature_status, feature_status
    )
    auto_on  = cfg.get_bool("withdrawal_approval_auto_approval", False)
    auto_max = cfg.get_float("withdrawal_approval_auto_max", 10.0)
    min_amt  = cfg.get_float("withdrawal_approval_min_amount", 5.0)
    max_amt  = cfg.get_float("withdrawal_approval_max_amount", 0.0)
    max_daily = cfg.get_int("withdrawal_approval_max_daily", 0)
    proc_time = cfg.get("withdrawal_approval_processing_time", "1-3 business days") or "1-3 business days"
    retry    = cfg.get_bool("withdrawal_approval_retry_failed", True)

    lines = [
        "⚙️ <b>Withdrawal Approval Settings</b>\n",
        f"Status: <b>{status_icon}</b>",
        f"Auto Approval: <b>{'✅ ON' if auto_on else '❌ OFF'}</b>",
        f"Auto Approval Max: <b>${auto_max:.2f}</b>",
        f"Minimum Amount: <b>${min_amt:.2f}</b>",
        f"Maximum Amount: <b>${max_amt:.2f}</b> (0 = unlimited)",
        f"Max Daily Per User: <b>{max_daily if max_daily > 0 else 'Unlimited'}</b>",
        f"Processing Time: <b>{proc_time}</b>",
        f"Retry Failed: <b>{'✅ ON' if retry else '❌ OFF'}</b>",
    ]

    kb = [
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="wda:adm:setstatus:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="wda:adm:setstatus:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="wda:adm:setstatus:disabled"),
        ],
        [
            InlineKeyboardButton(
                "🤖 Auto Approval: OFF" if auto_on else "🤖 Auto Approval: ON",
                callback_data="wda:adm:toggle_auto",
            )
        ],
        [
            InlineKeyboardButton("💰 Set Min Amount",   callback_data="wda:adm:set_min"),
            InlineKeyboardButton("💰 Set Max Amount",   callback_data="wda:adm:set_max"),
        ],
        [
            InlineKeyboardButton("🤖 Auto Max Amount",  callback_data="wda:adm:set_auto_max"),
            InlineKeyboardButton("📅 Max Daily",        callback_data="wda:adm:set_daily"),
        ],
        [
            InlineKeyboardButton("⏱ Processing Time",  callback_data="wda:adm:set_proc_time"),
            InlineKeyboardButton(
                "🔄 Retry: OFF" if retry else "🔄 Retry: ON",
                callback_data="wda:adm:toggle_retry",
            ),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="wda:adm:list")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def wda_adm_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set feature status (wda:adm:setstatus:<val>)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    val = query.data.split(":")[-1]
    if val in ("enabled", "maintenance", "disabled"):
        cfg.set("withdrawal_approval_status", val)
        await query.answer(f"✅ Status set to '{val}'", show_alert=True)
    await wda_adm_settings(update, context)


async def wda_adm_toggle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-approval (wda:adm:toggle_auto)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    current = cfg.get_bool("withdrawal_approval_auto_approval", False)
    cfg.set("withdrawal_approval_auto_approval", not current)
    await wda_adm_settings(update, context)


async def wda_adm_toggle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle retry failed (wda:adm:toggle_retry)."""
    query = update.callback_query
    await query.answer()
    if not _admin_guard(update):
        return

    current = cfg.get_bool("withdrawal_approval_retry_failed", True)
    cfg.set("withdrawal_approval_retry_failed", not current)
    await wda_adm_settings(update, context)


# ── Admin: setting input conversations ───────────────────────────────────────

async def wda_adm_set_min_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _admin_guard(update):
        return ConversationHandler.END
    cur = cfg.get_float("withdrawal_approval_min_amount", 5.0)
    await _safe_edit(q,
        f"💰 <b>Set Minimum Withdrawal Amount</b>\n\nCurrent: <b>${cur:.2f}</b>\n\nSend new minimum:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wda:adm:settings")]]),
    )
    context.user_data["_wda_adm_set"] = "min"
    return WDA_ADM_MIN


async def wda_adm_set_max_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _admin_guard(update):
        return ConversationHandler.END
    cur = cfg.get_float("withdrawal_approval_max_amount", 0.0)
    await _safe_edit(q,
        f"💰 <b>Set Maximum Withdrawal Amount</b>\n\nCurrent: <b>${cur:.2f}</b> (0=unlimited)\n\nSend new max:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wda:adm:settings")]]),
    )
    context.user_data["_wda_adm_set"] = "max"
    return WDA_ADM_MAX


async def wda_adm_set_auto_max_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _admin_guard(update):
        return ConversationHandler.END
    cur = cfg.get_float("withdrawal_approval_auto_max", 10.0)
    await _safe_edit(q,
        f"🤖 <b>Set Auto-Approval Max Amount</b>\n\nCurrent: <b>${cur:.2f}</b>\n\n"
        f"Withdrawals at or below this amount are auto-approved when auto-approval is ON.\n\nSend new value:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wda:adm:settings")]]),
    )
    return WDA_ADM_AUTO_MAX


async def wda_adm_set_daily_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _admin_guard(update):
        return ConversationHandler.END
    cur = cfg.get_int("withdrawal_approval_max_daily", 0)
    await _safe_edit(q,
        f"📅 <b>Set Max Daily Withdrawals Per User</b>\n\nCurrent: <b>{cur if cur > 0 else 'Unlimited'}</b>\n\n"
        f"Send a number (0 = unlimited):",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wda:adm:settings")]]),
    )
    return WDA_ADM_MAX_DAILY


async def wda_adm_set_proc_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _admin_guard(update):
        return ConversationHandler.END
    cur = cfg.get("withdrawal_approval_processing_time", "1-3 business days")
    await _safe_edit(q,
        f"⏱ <b>Set Processing Time Text</b>\n\nCurrent: <b>{cur}</b>\n\n"
        f"Send the new processing time (shown to users, e.g. '24 hours' or '1-3 business days'):",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wda:adm:settings")]]),
    )
    return WDA_ADM_PROC_TIME


async def wda_adm_set_float_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic handler for min/max float inputs."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    key_map = {
        "min": ("withdrawal_approval_min_amount", "Min amount"),
        "max": ("withdrawal_approval_max_amount", "Max amount"),
    }
    which = context.user_data.pop("_wda_adm_set", "min")
    cfg_key, label = key_map.get(which, ("withdrawal_approval_min_amount", "Amount"))
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid non-negative number.")
        context.user_data["_wda_adm_set"] = which
        return WDA_ADM_MIN if which == "min" else WDA_ADM_MAX
    cfg.set(cfg_key, val)
    await update.message.reply_text(f"✅ {label} set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def wda_adm_auto_max_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive number.")
        return WDA_ADM_AUTO_MAX
    cfg.set("withdrawal_approval_auto_max", val)
    await update.message.reply_text(f"✅ Auto-approval max set to <b>${val:.2f}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def wda_adm_daily_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    try:
        val = int((update.message.text or "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a non-negative integer (0 = unlimited).")
        return WDA_ADM_MAX_DAILY
    cfg.set("withdrawal_approval_max_daily", val)
    label = str(val) if val > 0 else "Unlimited"
    await update.message.reply_text(f"✅ Max daily withdrawals per user set to <b>{label}</b>.", parse_mode="HTML")
    return ConversationHandler.END


async def wda_adm_proc_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END
    val = (update.message.text or "").strip()[:100]
    if not val:
        await update.message.reply_text("❌ Please enter a processing time text.")
        return WDA_ADM_PROC_TIME
    cfg.set("withdrawal_approval_processing_time", val)
    await update.message.reply_text(f"✅ Processing time set to: <b>{val}</b>.", parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Conversation handler builders
# ─────────────────────────────────────────────────────────────────────────────

def build_wda_withdraw_conv() -> ConversationHandler:
    """Build the user-facing withdrawal conversation (replaces rd_withdraw_conv)."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_start, pattern=r"^rd:withdraw$")],
        states={
            WDA_METHOD: [
                CallbackQueryHandler(wda_method_select, pattern=r"^wda:m:.+$"),
                CallbackQueryHandler(wda_cancel_conv, pattern=r"^rd:menu$"),
            ],
            WDA_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wda_address_input),
                CallbackQueryHandler(wda_cancel_conv, pattern=r"^rd:menu$"),
            ],
            WDA_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wda_amount_input),
                CallbackQueryHandler(wda_cancel_conv, pattern=r"^rd:menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(wda_cancel_conv, pattern=r"^rd:menu$"),
            CommandHandler("cancel", wda_cancel_conv),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_wda_admin_reject_conv() -> ConversationHandler:
    """Admin reject conversation."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_reject_start, pattern=r"^wda:adm:reject:\d+$")],
        states={
            WDA_ADM_REJECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_reject_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_wda_admin_note_conv() -> ConversationHandler:
    """Admin add-note conversation."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_note_start, pattern=r"^wda:adm:note:\d+$")],
        states={
            WDA_ADM_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_note_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_wda_admin_settings_convs() -> list:
    """Return list of admin settings conversations."""
    fallback_cmd = CommandHandler("cancel", lambda u, c: ConversationHandler.END)
    convs = []

    # Min amount
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_set_min_start, pattern=r"^wda:adm:set_min$")],
        states={WDA_ADM_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_set_float_input)]},
        fallbacks=[fallback_cmd], per_user=True, per_chat=True, allow_reentry=True,
    ))
    # Max amount
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_set_max_start, pattern=r"^wda:adm:set_max$")],
        states={WDA_ADM_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_set_float_input)]},
        fallbacks=[fallback_cmd], per_user=True, per_chat=True, allow_reentry=True,
    ))
    # Auto-approval max
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_set_auto_max_start, pattern=r"^wda:adm:set_auto_max$")],
        states={WDA_ADM_AUTO_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_auto_max_input)]},
        fallbacks=[fallback_cmd], per_user=True, per_chat=True, allow_reentry=True,
    ))
    # Max daily
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_set_daily_start, pattern=r"^wda:adm:set_daily$")],
        states={WDA_ADM_MAX_DAILY: [MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_daily_input)]},
        fallbacks=[fallback_cmd], per_user=True, per_chat=True, allow_reentry=True,
    ))
    # Processing time
    convs.append(ConversationHandler(
        entry_points=[CallbackQueryHandler(wda_adm_set_proc_time_start, pattern=r"^wda:adm:set_proc_time$")],
        states={WDA_ADM_PROC_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, wda_adm_proc_time_input)]},
        fallbacks=[fallback_cmd], per_user=True, per_chat=True, allow_reentry=True,
    ))
    return convs


def register_handlers(application) -> None:
    """Register all withdrawal approval handlers on the application.

    Call this from bot.py in place of the old build_rd_withdraw_conv registration.
    """
    # ── User flow ────────────────────────────────────────────────────────────
    application.add_handler(build_wda_withdraw_conv())
    application.add_handler(CallbackQueryHandler(wda_history,      pattern=r"^wda:history$"))
    application.add_handler(CallbackQueryHandler(wda_status_detail, pattern=r"^wda:status:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_cancel_user,   pattern=r"^wda:cancel_user:\d+$"))

    # ── Admin list / detail / actions ────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(wda_adm_list,       pattern=r"^wda:adm:list(:(pending|under_review|approved|processing|completed|rejected|cancelled|expired|all))?$"))
    application.add_handler(CallbackQueryHandler(wda_adm_detail,     pattern=r"^wda:adm:detail:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_adm_approve,    pattern=r"^wda:adm:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_adm_review,     pattern=r"^wda:adm:review:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_adm_processing, pattern=r"^wda:adm:processing:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_adm_complete,   pattern=r"^wda:adm:complete:\d+$"))
    application.add_handler(CallbackQueryHandler(wda_adm_cancel,     pattern=r"^wda:adm:cancel:\d+$"))

    # ── Admin conversations ──────────────────────────────────────────────────
    application.add_handler(build_wda_admin_reject_conv())
    application.add_handler(build_wda_admin_note_conv())
    for conv in build_wda_admin_settings_convs():
        application.add_handler(conv)

    # ── Admin settings panel ─────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(wda_adm_settings,   pattern=r"^wda:adm:settings$"))
    application.add_handler(CallbackQueryHandler(wda_adm_set_status,  pattern=r"^wda:adm:setstatus:(enabled|maintenance|disabled)$"))
    application.add_handler(CallbackQueryHandler(wda_adm_toggle_auto, pattern=r"^wda:adm:toggle_auto$"))
    application.add_handler(CallbackQueryHandler(wda_adm_toggle_retry, pattern=r"^wda:adm:toggle_retry$"))
