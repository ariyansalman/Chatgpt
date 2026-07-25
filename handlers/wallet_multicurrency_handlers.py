"""User-facing Multi-Currency Wallet handlers — V39.

Callback namespace: mcw:*

Accessible via the existing wallet menu → 🌍 Multi-Currency button.

Users can:
  • View all currency balances + portfolio total
  • See per-currency transaction history
  • Initiate transfers between wallets (conversation-based)
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler, ConversationHandler, ContextTypes,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import User
from services import multicurrency_wallet as mcw_svc
from services import exchange_rate_service as ers
from utils.bot_config import cfg
from utils.permissions import has_permission
from i18n import get_user_language

logger = logging.getLogger(__name__)

# Conversation states
MCW_TRANSFER_FROM, MCW_TRANSFER_AMOUNT, MCW_TRANSFER_TO, MCW_TRANSFER_CONFIRM = range(4)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(amount: float, symbol: str, is_crypto: bool) -> str:
    if is_crypto:
        return f"{symbol}{amount:.8f}".rstrip("0").rstrip(".")
    return f"{symbol}{amount:,.4f}".rstrip("0").rstrip(".")


def _get_user_id_from_tg(tg_id: int) -> Optional[int]:
    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        return u.id if u else None


def _back_kb(callback: str = "wallet") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=callback)]])


def _check_feature_enabled() -> bool:
    return cfg.get("multicurrency_wallet_status", "enabled") == "enabled"


# ─── Main wallet overview ─────────────────────────────────────────────────────

async def mcw_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all currency balances and portfolio total."""
    q = update.callback_query
    if q:
        await q.answer()
    tg_id = update.effective_user.id

    if not _check_feature_enabled():
        status = cfg.get("multicurrency_wallet_status", "enabled")
        msg = ("🌍 <b>Multi-Currency Wallet</b>\n\n"
               "🟡 <i>This feature is currently under maintenance.</i>"
               if status == "maintenance" else
               "🌍 <b>Multi-Currency Wallet</b>\n\n🔴 <i>This feature is disabled.</i>")
        kb = _back_kb("wallet")
        if q:
            try:
                await q.edit_message_text(msg, reply_markup=kb, parse_mode="HTML")
            except BadRequest:
                pass
        return

    user_id = _get_user_id_from_tg(tg_id)
    if not user_id:
        return

    wallets = mcw_svc.get_user_wallets(user_id)
    portfolio_usd = mcw_svc.get_portfolio_value_usd(user_id)

    lines = ["🌍 <b>Multi-Currency Wallet</b>\n"]
    lines.append(f"💼 Portfolio Value: <b>~${portfolio_usd:.2f} USD</b>\n")

    kb_rows = []
    for w in wallets:
        code    = w["currency_code"]
        symbol  = w["symbol"]
        balance = w["balance"]
        frozen  = w["is_frozen"]
        crypto  = w["is_crypto"]
        icon    = "🔒" if frozen else ("💎" if crypto else "💵")
        amt_str = _fmt(balance, symbol, crypto)
        lines.append(f"{icon} <b>{code}</b>: {amt_str}")
        kb_rows.append([
            InlineKeyboardButton(f"📋 {code} History",
                                 callback_data=f"mcw:hist:{code}"),
        ])

    lines.append("\n<i>Tap a currency to view history.</i>")

    # Top-level action buttons
    action_row = [
        InlineKeyboardButton("🔄 Transfer", callback_data="mcw:transfer:start"),
    ]
    kb_rows.insert(0, action_row)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="wallet")])

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(kb_rows)

    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("mcw_overview edit failed: %s", e)


# ─── Transaction history per currency ─────────────────────────────────────────

async def mcw_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show transaction history for a specific currency."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    user_id = _get_user_id_from_tg(tg_id)
    if not user_id:
        return

    # callback_data: mcw:hist:<currency_code>
    parts = (q.data or "").split(":")
    currency_code = parts[2].upper() if len(parts) >= 3 else "USD"

    txs = mcw_svc.get_wallet_transactions(user_id, currency_code, limit=15)
    currency = mcw_svc.get_currency_config(currency_code)
    symbol = currency["symbol"] if currency else currency_code
    is_crypto = currency["is_crypto"] if currency else False

    lines = [f"📋 <b>{currency_code} Transaction History</b>\n"]
    if not txs:
        lines.append("<i>No transactions yet.</i>")
    else:
        for tx in txs:
            when = tx["created_at"].strftime("%m/%d %H:%M") if tx["created_at"] else "?"
            tx_type = tx["tx_type"].replace("_", " ").title()
            amt = _fmt(tx["amount"], symbol, is_crypto)
            bal = _fmt(tx["balance_after"], symbol, is_crypto)
            sign = "➕" if tx["tx_type"] in (
                "deposit", "referral_reward", "bonus", "manual_credit",
                "transfer_in", "exchange_in"
            ) else "➖"
            lines.append(
                f"{sign} <b>{tx_type}</b> {amt}\n"
                f"   Balance: {bal} | {when}"
            )
            if tx["notes"]:
                lines.append(f"   <i>{tx['notes'][:60]}</i>")
            lines.append("")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Wallets", callback_data="mcw:overview")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("mcw_history edit failed: %s", e)


# ─── Transfer between wallets (conversation) ──────────────────────────────────

async def mcw_transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the wallet-to-wallet transfer conversation."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    user_id = _get_user_id_from_tg(tg_id)
    if not user_id:
        return ConversationHandler.END

    wallets = mcw_svc.get_user_wallets(user_id)
    has_balance = [w for w in wallets if w["balance"] > 0]
    if len(has_balance) < 1:
        try:
            await q.edit_message_text(
                "❌ You need a positive balance in at least one currency to transfer.",
                reply_markup=_back_kb("mcw:overview"),
                parse_mode="HTML",
            )
        except BadRequest:
            pass
        return ConversationHandler.END

    lines = ["🔄 <b>Wallet Transfer</b>\n\nSelect the currency to send <b>FROM</b>:\n"]
    kb_rows = []
    for w in has_balance:
        symbol = w["symbol"]
        code   = w["currency_code"]
        crypto = w["is_crypto"]
        bal    = _fmt(w["balance"], symbol, crypto)
        kb_rows.append([
            InlineKeyboardButton(f"{code} ({bal})", callback_data=f"mcw:xfr_from:{code}"),
        ])
    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="mcw:overview")])

    context.user_data["mcw_user_id"] = user_id
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest:
        pass
    return MCW_TRANSFER_FROM


async def mcw_transfer_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected the FROM currency."""
    q = update.callback_query
    await q.answer()

    parts = (q.data or "").split(":")
    from_code = parts[2].upper() if len(parts) >= 3 else None
    if not from_code:
        return ConversationHandler.END

    context.user_data["mcw_from"] = from_code
    user_id = context.user_data.get("mcw_user_id")
    if not user_id:
        return ConversationHandler.END

    # Get available balance
    balance = mcw_svc.get_user_wallet_balance(user_id, from_code)
    cfg_data = mcw_svc.get_currency_config(from_code)
    symbol = cfg_data["symbol"] if cfg_data else from_code
    is_crypto = cfg_data["is_crypto"] if cfg_data else False
    bal_str = _fmt(balance, symbol, is_crypto)

    context.user_data["mcw_from_balance"] = balance
    try:
        await q.edit_message_text(
            f"🔄 <b>Transfer from {from_code}</b>\n\n"
            f"Available: <b>{bal_str}</b>\n\n"
            f"Enter the amount to send (or /cancel):",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return MCW_TRANSFER_AMOUNT


async def mcw_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the amount."""
    try:
        amount = float((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number:")
        return MCW_TRANSFER_AMOUNT

    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0:")
        return MCW_TRANSFER_AMOUNT

    from_balance = context.user_data.get("mcw_from_balance", 0.0)
    if amount > from_balance:
        await update.message.reply_text(
            f"❌ Insufficient balance. Available: {from_balance:.8f}\nEnter a smaller amount:"
        )
        return MCW_TRANSFER_AMOUNT

    context.user_data["mcw_amount"] = amount
    from_code = context.user_data["mcw_from"]

    # Show available target currencies
    user_id = context.user_data.get("mcw_user_id")
    wallets = mcw_svc.get_user_wallets(user_id or 0)
    kb_rows = []
    for w in wallets:
        if w["currency_code"] == from_code:
            continue
        if not w["is_frozen"]:
            to_code = w["currency_code"]
            rate = ers.get_rate(from_code, to_code)
            if rate:
                to_amt = amount * rate
                cfg_data = mcw_svc.get_currency_config(to_code)
                symbol = cfg_data["symbol"] if cfg_data else to_code
                is_crypto = cfg_data["is_crypto"] if cfg_data else False
                est = _fmt(to_amt, symbol, is_crypto)
                kb_rows.append([
                    InlineKeyboardButton(
                        f"{to_code} (~{est})",
                        callback_data=f"mcw:xfr_to:{to_code}",
                    )
                ])
            else:
                kb_rows.append([
                    InlineKeyboardButton(f"{to_code} (rate unavailable)",
                                         callback_data=f"mcw:xfr_to:{to_code}"),
                ])

    if not kb_rows:
        await update.message.reply_text(
            "❌ No target currencies available. Transfer cancelled."
        )
        return ConversationHandler.END

    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="mcw:overview")])
    await update.message.reply_text(
        f"Select the currency to send <b>TO</b>:",
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode="HTML",
    )
    return MCW_TRANSFER_TO


async def mcw_transfer_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected the TO currency."""
    q = update.callback_query
    await q.answer()

    parts = (q.data or "").split(":")
    to_code = parts[2].upper() if len(parts) >= 3 else None
    if not to_code:
        return ConversationHandler.END

    from_code = context.user_data.get("mcw_from", "")
    amount    = context.user_data.get("mcw_amount", 0.0)
    context.user_data["mcw_to"] = to_code

    rate = ers.get_rate(from_code, to_code)
    to_amount = amount * rate if rate else None

    if to_amount is None:
        try:
            await q.edit_message_text(
                f"❌ Exchange rate for {from_code} → {to_code} is unavailable.\n"
                f"Transfer cancelled.",
                parse_mode="HTML",
            )
        except BadRequest:
            pass
        return ConversationHandler.END

    context.user_data["mcw_to_amount"] = to_amount
    context.user_data["mcw_rate"] = rate

    from_cfg  = mcw_svc.get_currency_config(from_code)
    to_cfg    = mcw_svc.get_currency_config(to_code)
    from_sym  = from_cfg["symbol"] if from_cfg else from_code
    to_sym    = to_cfg["symbol"] if to_cfg else to_code
    from_c    = from_cfg["is_crypto"] if from_cfg else False
    to_c      = to_cfg["is_crypto"] if to_cfg else False

    confirm_text = (
        f"✅ <b>Confirm Transfer</b>\n\n"
        f"Send: <b>{_fmt(amount, from_sym, from_c)}</b> {from_code}\n"
        f"Receive: <b>{_fmt(to_amount, to_sym, to_c)}</b> {to_code}\n"
        f"Rate: 1 {from_code} = {rate:.6f} {to_code}\n\n"
        f"Confirm?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="mcw:xfr_confirm"),
         InlineKeyboardButton("❌ Cancel",  callback_data="mcw:overview")],
    ])
    try:
        await q.edit_message_text(confirm_text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass
    return MCW_TRANSFER_CONFIRM


async def mcw_transfer_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute the transfer."""
    q = update.callback_query
    await q.answer()

    user_id   = context.user_data.get("mcw_user_id")
    from_code = context.user_data.get("mcw_from", "")
    to_code   = context.user_data.get("mcw_to", "")
    amount    = context.user_data.get("mcw_amount", 0.0)
    to_amount = context.user_data.get("mcw_to_amount", 0.0)

    if not user_id or not from_code or not to_code or amount <= 0:
        try:
            await q.edit_message_text(
                "❌ Session expired. Please try again.",
                reply_markup=_back_kb("mcw:overview"),
            )
        except BadRequest:
            pass
        return ConversationHandler.END

    try:
        mcw_svc.transfer(
            user_id, from_code, to_code, amount, to_amount,
            reason=f"Wallet transfer {from_code}→{to_code}",
            actor_type="user", actor_id=update.effective_user.id,
        )
        from_cfg = mcw_svc.get_currency_config(from_code)
        to_cfg   = mcw_svc.get_currency_config(to_code)
        from_sym = from_cfg["symbol"] if from_cfg else from_code
        to_sym   = to_cfg["symbol"] if to_cfg else to_code
        from_c   = from_cfg["is_crypto"] if from_cfg else False
        to_c     = to_cfg["is_crypto"] if to_cfg else False

        msg = (
            f"✅ <b>Transfer Successful!</b>\n\n"
            f"Sent: {_fmt(amount, from_sym, from_c)} {from_code}\n"
            f"Received: {_fmt(to_amount, to_sym, to_c)} {to_code}"
        )
    except mcw_svc.MCWalletError as e:
        msg = f"❌ Transfer failed: {e}"
    except Exception as e:
        logger.exception("mcw_transfer_confirm failed")
        msg = "❌ Transfer failed due to an internal error."

    try:
        await q.edit_message_text(
            msg,
            reply_markup=_back_kb("mcw:overview"),
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return ConversationHandler.END


async def mcw_transfer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the transfer conversation via /cancel command."""
    await update.message.reply_text("Transfer cancelled.")
    return ConversationHandler.END


# ─── Registration ─────────────────────────────────────────────────────────────

def build_transfer_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(mcw_transfer_start, pattern=r"^mcw:transfer:start$"),
        ],
        states={
            MCW_TRANSFER_FROM: [
                CallbackQueryHandler(mcw_transfer_from,  pattern=r"^mcw:xfr_from:.+$"),
                CallbackQueryHandler(lambda u, c: ConversationHandler.END,
                                     pattern=r"^mcw:overview$"),
            ],
            MCW_TRANSFER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, mcw_transfer_amount),
            ],
            MCW_TRANSFER_TO: [
                CallbackQueryHandler(mcw_transfer_to, pattern=r"^mcw:xfr_to:.+$"),
                CallbackQueryHandler(lambda u, c: ConversationHandler.END,
                                     pattern=r"^mcw:overview$"),
            ],
            MCW_TRANSFER_CONFIRM: [
                CallbackQueryHandler(mcw_transfer_confirm, pattern=r"^mcw:xfr_confirm$"),
                CallbackQueryHandler(lambda u, c: ConversationHandler.END,
                                     pattern=r"^mcw:overview$"),
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, mcw_transfer_cancel),
            CallbackQueryHandler(lambda u, c: ConversationHandler.END,
                                 pattern=r"^mcw:overview$"),
        ],
        allow_reentry=True,
    )


def register_handlers(app) -> None:
    """Register all multi-currency wallet handlers."""
    app.add_handler(CallbackQueryHandler(mcw_overview, pattern=r"^mcw:overview$"))
    app.add_handler(CallbackQueryHandler(mcw_history,  pattern=r"^mcw:hist:.+$"))
    app.add_handler(build_transfer_conv())
