"""Admin Wallets panel — view balance, ledger, and adjust (credit/debit)."""
from __future__ import annotations

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_db_session, User
from services import wallet as wallet_svc
from utils.helpers import format_price
from utils.bot_config import cfg
from utils.audit import log_admin_action
from utils.permissions import has_permission
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def _kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="acc:root")]])


async def wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text = (
        "💰 <b>Wallets</b>\n\n"
        "Open a user's detail (👥 Users → View Users) then tap the wallet "
        "actions to adjust balance. Every credit/debit is recorded in the "
        "wallet ledger with a required reason.\n\n"
        f"Manual-adjust ceiling: <b>{format_price(cfg.get_float('wallet_max_manual_adjust', 1000.0))}</b>\n"
        f"Require reason: <b>{'ON' if cfg.get_bool('wallet_require_reason', True) else 'OFF'}</b>\n"
        f"2-step confirm on destructive: <b>{'ON' if cfg.get_bool('admin_2step_confirm_destructive', True) else 'OFF'}</b>"
    )
    kb = [
        [InlineKeyboardButton("👥 Browse users", callback_data="admin_view_users")],
        [InlineKeyboardButton("⚙️ Wallet settings",
                              callback_data="cfg_cat_wallets")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    try:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def render_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Render the last N ledger rows for a user."""
    query = update.callback_query
    entries = wallet_svc.ledger(user_id, limit=20)
    with get_db_session() as s:
        u = s.query(User).filter(User.id == user_id).first()
        if not u:
            try:
                await query.edit_message_text("User not found.", reply_markup=_kb_back())
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        tg_id = u.telegram_id
        balance = float(u.wallet_balance or 0.0)
        username = u.username or "—"
    lines = [f"💰 <b>Wallet ledger</b> — @{username} (tg <code>{tg_id}</code>)",
             f"Balance: <b>{format_price(balance)}</b>",
             ""]
    if not entries:
        lines.append("— no entries yet —")
    else:
        for e in entries:
            when = e["created_at"].strftime("%Y-%m-%d %H:%M") if e["created_at"] else "?"
            sign = "＋" if e["delta"] >= 0 else "－"
            lines.append(
                f"{when}  {sign}{format_price(abs(e['delta']))}  "
                f"→ {format_price(e['balance_after'])}  "
                f"[{e['actor_type']}]  {e['reason']}"
            )
    kb = [
        [InlineKeyboardButton("➕ Credit", callback_data=f"acc:wal:credit:{user_id}"),
         InlineKeyboardButton("➖ Debit",  callback_data=f"acc:wal:debit:{user_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:sec:wallets")],
    ]
    try:
        try:
            await query.edit_message_text("\n".join(lines),
                                          reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─── Conversation: adjust (credit/debit) ─────────────────────────────────
ADJ_AMOUNT, ADJ_REASON = range(2)


async def adjust_start_credit(update, context):
    return await _adjust_start(update, context, sign=+1)


async def adjust_start_debit(update, context):
    return await _adjust_start(update, context, sign=-1)


async def _adjust_start(update, context, *, sign: int):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return -1  # ConversationHandler.END
    # Data: acc:wal:credit:<user_id>  or  acc:wal:debit:<user_id>
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[-1])
    except Exception:
        return -1
    context.user_data["_wal_user_id"] = user_id
    context.user_data["_wal_sign"] = sign
    verb = "credit" if sign > 0 else "debit"
    try:
        await query.edit_message_text(
            f"💰 <b>Wallet {verb}</b>\n\n"
            f"Enter the amount in USD. Max: "
            f"<b>{format_price(cfg.get_float('wallet_max_manual_adjust', 1000.0))}</b>\n\n"
            f"Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return ADJ_AMOUNT


async def adjust_amount(update, context):
    try:
        amount = float((update.message.text or "").strip())
    except Exception:
        await update.message.reply_text("Not a number. Enter USD amount:")
        return ADJ_AMOUNT
    if amount <= 0:
        await update.message.reply_text("Amount must be > 0.")
        return ADJ_AMOUNT
    ceiling = cfg.get_float("wallet_max_manual_adjust", 1000.0)
    if ceiling > 0 and amount > ceiling:
        await update.message.reply_text(
            f"Above manual-adjust ceiling ({format_price(ceiling)}). Enter a smaller amount:")
        return ADJ_AMOUNT
    context.user_data["_wal_amount"] = amount
    if cfg.get_bool("wallet_require_reason", True):
        await update.message.reply_text("Reason for this adjustment (short text):")
        return ADJ_REASON
    return await _finalize(update, context, reason="admin adjust")


async def adjust_reason(update, context):
    reason = ((update.message.text or "").strip())[:200] or "admin adjust"
    return await _finalize(update, context, reason=reason)


async def _finalize(update, context, *, reason: str):
    user_id = context.user_data.pop("_wal_user_id", None)
    sign = context.user_data.pop("_wal_sign", 0)
    amount = context.user_data.pop("_wal_amount", 0.0)
    if not user_id or sign == 0 or amount <= 0:
        await update.message.reply_text("Session lost. Try again.")
        return -1
    try:
        new_bal = wallet_svc.adjust(user_id, sign * amount,
                                    reason=reason,
                                    actor_type="admin",
                                    actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "wallet.adjust",
                         target_type="user", target_id=user_id,
                         details=f"delta={sign*amount:+.2f} reason={reason}")
        await update.message.reply_text(
            f"✅ New balance: <b>{format_price(new_bal)}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return -1


async def route(action, rest, update, context):
    """Non-conversation actions land here (view ledger)."""
    query = update.callback_query
    if action == "view" and rest:
        try:
            user_id = int(rest[0])
        except Exception:
            return
        await query.answer()
        await render_ledger(update, context, user_id)
        return
    # credit/debit handled via ConversationHandler entry_points
