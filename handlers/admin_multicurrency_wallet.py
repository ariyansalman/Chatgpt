"""Admin Multi-Currency Wallet Manager — V39.

Callback namespace: amcw:*

Admin capabilities:
  • View all currencies and their stats
  • Add / enable / disable / freeze a currency
  • Edit currency limits and fees
  • Adjust any user's currency balance (credit / debit)
  • View wallet logs per currency
  • Freeze individual user wallets
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, CallbackQueryHandler, ConversationHandler, MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import User, WalletCurrencyConfig, WalletCurrencyStatus
from services import multicurrency_wallet as mcw_svc
from utils.bot_config import cfg
from utils.helpers import format_price
from utils.audit import log_admin_action
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# Conversation states
ADJ_CURRENCY, ADJ_USER, ADJ_AMOUNT, ADJ_REASON = range(4)
EDIT_CURRENCY, EDIT_FIELD, EDIT_VALUE = range(10, 13)
ADD_CODE, ADD_NAME, ADD_SYMBOL, ADD_CRYPTO = range(20, 24)


def _back_kb(back_cb: str = "amcw:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=back_cb)]])


def _status_icon(status: str) -> str:
    return {"enabled": "🟢", "disabled": "🔴", "maintenance": "🟡", "frozen": "🔵"}.get(status, "⚪")


# ─── Main menu ────────────────────────────────────────────────────────────────

async def amcw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🌍 Multi-Currency Wallet Manager main screen."""
    q = update.callback_query
    if q:
        await q.answer()

    if not has_permission(update.effective_user.id, "manage_payments"):
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    stats = mcw_svc.get_admin_wallet_stats()
    currencies = mcw_svc.get_all_currencies()

    lines = [
        "🌍 <b>Multi-Currency Wallet Manager</b>\n",
        f"Enabled currencies: <b>{stats.get('enabled_currencies', 0)}/{stats.get('total_currencies', 0)}</b>",
        f"Total wallets: <b>{stats.get('total_wallets', 0)}</b>",
        f"Frozen wallets: <b>{stats.get('frozen_wallets', 0)}</b>",
        "\n<b>Currencies:</b>",
    ]
    kb_rows = []
    for c in currencies:
        icon   = _status_icon(c["status"])
        frozen = " 🔒" if c["is_frozen"] else ""
        per_c  = stats.get("per_currency", {}).get(c["code"], {})
        count  = per_c.get("wallet_count", 0)
        lines.append(f"{icon} <b>{c['code']}</b> {c['name']}{frozen} — {count} wallets")
        kb_rows.append([
            InlineKeyboardButton(f"{icon} {c['code']}", callback_data=f"amcw:cur:{c['code']}"),
        ])

    kb_rows.append([
        InlineKeyboardButton("➕ Add Currency", callback_data="amcw:add:start"),
    ])
    kb_rows.append([
        InlineKeyboardButton("💰 Adjust User Balance", callback_data="amcw:adj:start"),
    ])
    kb_rows.append([
        InlineKeyboardButton("📊 Wallet Logs", callback_data="amcw:logs"),
        InlineKeyboardButton("🔙 Back", callback_data="acc:root"),
    ])

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup(kb_rows)
    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("amcw_menu: %s", e)


# ─── Currency detail ──────────────────────────────────────────────────────────

async def amcw_currency_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detail for a single currency with action buttons."""
    q = update.callback_query
    await q.answer()

    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = (q.data or "").split(":")
    code  = parts[2].upper() if len(parts) >= 3 else ""
    c     = mcw_svc.get_currency_config(code)
    if not c:
        await q.answer("Currency not found.", show_alert=True)
        return

    stats = mcw_svc.get_admin_wallet_stats()
    per_c = stats.get("per_currency", {}).get(code, {})

    icon = _status_icon(c["status"])
    lines = [
        f"{icon} <b>{c['code']} — {c['name']}</b>",
        f"Symbol: <b>{c['symbol']}</b>  |  Type: {'Crypto' if c['is_crypto'] else 'Fiat'}",
        f"Status: <b>{c['status']}</b>  |  Frozen: <b>{'Yes' if c['is_frozen'] else 'No'}</b>",
        "",
        "<b>Limits:</b>",
        f"Balance: min {c['min_balance']} / max {c['max_balance']} (0=unlimited)",
        f"Deposit: min {c['min_deposit']} / max {c['max_deposit']}",
        f"Deposit fee: {c['deposit_fee_pct']}%",
        f"Withdrawal: min {c['min_withdrawal']} / max {c['max_withdrawal']}",
        f"Withdrawal fee: {c['withdrawal_fee_pct']}% + {c['withdrawal_fee_flat']} flat",
        "",
        f"<b>Stats:</b> {per_c.get('wallet_count', 0)} wallets  |  "
        f"Total: {per_c.get('total_balance', 0):.4f} {code}",
    ]

    toggle_status = "disabled" if c["status"] == "enabled" else "enabled"
    toggle_label  = "🔴 Disable" if c["status"] == "enabled" else "🟢 Enable"
    freeze_label  = "🧊 Unfreeze" if c["is_frozen"] else "🧊 Freeze"
    freeze_action = "unfreeze" if c["is_frozen"] else "freeze"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label,  callback_data=f"amcw:toggle:{code}:{toggle_status}"),
         InlineKeyboardButton(freeze_label,  callback_data=f"amcw:freeze:{code}:{freeze_action}")],
        [InlineKeyboardButton("✏️ Edit Limits", callback_data=f"amcw:edit:{code}")],
        [InlineKeyboardButton("👥 View Wallets", callback_data=f"amcw:wallets:{code}:0")],
        [InlineKeyboardButton("🔙 Back", callback_data="amcw:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("amcw_currency_detail: %s", e)


# ─── Toggle / freeze ─────────────────────────────────────────────────────────

async def amcw_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable / disable a currency."""
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = (q.data or "").split(":")
    code   = parts[2].upper() if len(parts) >= 3 else ""
    status = parts[3] if len(parts) >= 4 else "enabled"

    try:
        mcw_svc.update_currency(code,
                                status=status,
                                is_enabled=(status == "enabled"))
        log_admin_action(update.effective_user.id, f"currency.{status}",
                         target_type="currency", target_id=code)
        await q.answer(f"✅ {code} is now {status}.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return

    context.user_data["_amcw_cur"] = code
    await amcw_currency_detail(update, context)


async def amcw_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Freeze / unfreeze a currency."""
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts  = (q.data or "").split(":")
    code   = parts[2].upper() if len(parts) >= 3 else ""
    action = parts[3] if len(parts) >= 4 else "freeze"
    frozen = (action == "freeze")

    new_status = WalletCurrencyStatus.FROZEN.value if frozen else WalletCurrencyStatus.ENABLED.value
    try:
        mcw_svc.update_currency(code, is_frozen=frozen, status=new_status)
        log_admin_action(update.effective_user.id, f"currency.{action}",
                         target_type="currency", target_id=code)
        await q.answer(f"✅ {code} {'frozen' if frozen else 'unfrozen'}.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return

    await amcw_currency_detail(update, context)


# ─── Browse user wallets per currency ─────────────────────────────────────────

async def amcw_wallets_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List users who have a wallet in a given currency (paginated)."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = (q.data or "").split(":")
    code  = parts[2].upper() if len(parts) >= 3 else ""
    page  = int(parts[3]) if len(parts) >= 4 else 0
    per_page = 10

    from database.models import UserCurrencyWallet
    with get_db_session() as s:
        total = (s.query(UserCurrencyWallet)
                 .filter_by(currency_code=code)
                 .count())
        rows = (s.query(UserCurrencyWallet)
                .filter_by(currency_code=code)
                .order_by(UserCurrencyWallet.balance.desc())
                .offset(page * per_page).limit(per_page).all())
        items = []
        for r in rows:
            u = s.query(User).filter_by(id=r.user_id).first()
            uname  = f"@{u.username}" if u and u.username else f"ID:{r.user_id}"
            frozen = " 🔒" if r.is_frozen else ""
            items.append((r.user_id, uname, float(r.balance), frozen, r.id))

    lines = [f"👥 <b>{code} Wallets</b> (page {page+1})\n"]
    kb_rows = []
    for uid, uname, bal, frozen, wid in items:
        lines.append(f"• {uname}: <b>{bal:.6f}</b>{frozen}")
        kb_rows.append([
            InlineKeyboardButton(f"⚙️ {uname}", callback_data=f"amcw:uwal:{uid}:{code}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"amcw:wallets:{code}:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"amcw:wallets:{code}:{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"amcw:cur:{code}")])

    try:
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows),
                                  parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("amcw_wallets_list: %s", e)


# ─── User wallet detail ───────────────────────────────────────────────────────

async def amcw_user_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a single user's wallet for a currency with adjustment options."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts   = (q.data or "").split(":")
    user_id = int(parts[2]) if len(parts) >= 3 else 0
    code    = parts[3].upper() if len(parts) >= 4 else ""

    with get_db_session() as s:
        u = s.query(User).filter_by(id=user_id).first()
        if not u:
            await q.answer("User not found.", show_alert=True)
            return
        uname = f"@{u.username}" if u.username else f"TG:{u.telegram_id}"
        bal   = mcw_svc.get_user_wallet_balance(user_id, code)

    txs  = mcw_svc.get_wallet_transactions(user_id, code, limit=5)
    lines = [
        f"💰 <b>{code} Wallet — {uname}</b>\n",
        f"Balance: <b>{bal:.8f} {code}</b>\n",
        "<b>Recent transactions:</b>",
    ]
    for tx in txs:
        when = tx["created_at"].strftime("%m/%d %H:%M") if tx["created_at"] else "?"
        sign = "+" if tx["tx_type"] in ("deposit","manual_credit","transfer_in","exchange_in","referral_reward","bonus") else "-"
        lines.append(f"  {sign}{tx['amount']:.6f} [{tx['tx_type']}] {when}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Credit", callback_data=f"amcw:credit:{user_id}:{code}"),
         InlineKeyboardButton("➖ Debit",  callback_data=f"amcw:debit:{user_id}:{code}")],
        [InlineKeyboardButton("🔒 Freeze Wallet", callback_data=f"amcw:frzwal:{user_id}:{code}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"amcw:wallets:{code}:0")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("amcw_user_wallet: %s", e)


async def amcw_freeze_user_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle freeze on an individual user's currency wallet."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts   = (q.data or "").split(":")
    user_id = int(parts[2]) if len(parts) >= 3 else 0
    code    = parts[3].upper() if len(parts) >= 4 else ""

    from database.models import UserCurrencyWallet
    with get_db_session() as s:
        w = s.query(UserCurrencyWallet).filter_by(user_id=user_id, currency_code=code).first()
        if not w:
            await q.answer("Wallet not found.", show_alert=True)
            return
        w.is_frozen = not w.is_frozen
        new_state = w.is_frozen
        s.commit()

    log_admin_action(update.effective_user.id, "user_wallet.freeze_toggle",
                     target_type="user_wallet", target_id=f"{user_id}:{code}",
                     details=f"frozen={new_state}")
    await q.answer(f"Wallet {'frozen' if new_state else 'unfrozen'}.")
    await amcw_user_wallet(update, context)


# ─── Wallet Logs ─────────────────────────────────────────────────────────────

async def amcw_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent currency transactions across all users (admin log view)."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    from database.models import CurrencyTransaction
    from sqlalchemy import desc
    with get_db_session() as s:
        rows = (s.query(CurrencyTransaction)
                .order_by(desc(CurrencyTransaction.created_at))
                .limit(20).all())
        lines = ["📊 <b>Recent Wallet Transactions (All Users)</b>\n"]
        for r in rows:
            when = r.created_at.strftime("%m/%d %H:%M") if r.created_at else "?"
            sign = "+" if r.tx_type in ("deposit","manual_credit","transfer_in",
                                         "exchange_in","referral_reward","bonus") else "-"
            lines.append(
                f"{sign}{r.amount:.4f} <b>{r.currency_code}</b> "
                f"[{r.tx_type}] user={r.user_id} {when}"
            )

    kb = _back_kb("amcw:menu")
    try:
        await q.edit_message_text("\n".join(lines) or "No transactions yet.",
                                  reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("amcw_logs: %s", e)


# ─── Add Currency (conversation) ─────────────────────────────────────────────

async def amcw_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    try:
        await q.edit_message_text(
            "➕ <b>Add Currency</b>\n\nEnter the currency code (e.g. EUR, XRP):\n\n/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return ADD_CODE


async def amcw_add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()
    if not code or len(code) > 16 or not code.isalpha():
        await update.message.reply_text("❌ Invalid code. Use 2-10 uppercase letters (e.g. EUR):")
        return ADD_CODE
    existing = mcw_svc.get_currency_config(code)
    if existing:
        await update.message.reply_text(f"❌ Currency {code} already exists. Enter a different code:")
        return ADD_CODE
    context.user_data["new_cur_code"] = code
    await update.message.reply_text(f"Enter the full name for {code} (e.g. Euro):")
    return ADD_NAME


async def amcw_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name or len(name) > 64:
        await update.message.reply_text("❌ Name must be 1-64 chars:")
        return ADD_NAME
    context.user_data["new_cur_name"] = name
    await update.message.reply_text(f"Enter the symbol for {name} (e.g. €):")
    return ADD_SYMBOL


async def amcw_add_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (update.message.text or "").strip()
    if not symbol or len(symbol) > 8:
        await update.message.reply_text("❌ Symbol must be 1-8 chars:")
        return ADD_SYMBOL
    context.user_data["new_cur_symbol"] = symbol
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Fiat", callback_data="amcw_add_crypto:false"),
         InlineKeyboardButton("💎 Crypto", callback_data="amcw_add_crypto:true")],
    ])
    await update.message.reply_text("Is this a cryptocurrency?", reply_markup=kb)
    return ADD_CRYPTO


async def amcw_add_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    is_crypto = (q.data or "").split(":")[-1] == "true"
    code   = context.user_data.pop("new_cur_code", "")
    name   = context.user_data.pop("new_cur_name", "")
    symbol = context.user_data.pop("new_cur_symbol", "")
    if not code:
        await q.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    try:
        mcw_svc.add_currency(code, name, symbol, is_crypto=is_crypto)
        log_admin_action(update.effective_user.id, "currency.add",
                         target_type="currency", target_id=code)
        try:
            await q.edit_message_text(
                f"✅ Currency <b>{code} ({name})</b> added successfully!",
                reply_markup=_back_kb("amcw:menu"), parse_mode="HTML",
            )
        except BadRequest:
            pass
    except Exception as e:
        try:
            await q.edit_message_text(f"❌ Failed to add currency: {e}",
                                      reply_markup=_back_kb("amcw:menu"))
        except BadRequest:
            pass
    return ConversationHandler.END


async def amcw_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Add currency cancelled.")
    return ConversationHandler.END


def build_add_currency_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(amcw_add_start, pattern=r"^amcw:add:start$")],
        states={
            ADD_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_add_code)],
            ADD_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_add_name)],
            ADD_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_add_symbol)],
            ADD_CRYPTO: [CallbackQueryHandler(amcw_add_crypto, pattern=r"^amcw_add_crypto:.+$")],
        },
        fallbacks=[MessageHandler(filters.COMMAND, amcw_add_cancel)],
        allow_reentry=True,
    )


# ─── Adjust User Balance (conversation) ──────────────────────────────────────

async def amcw_adj_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    currencies = mcw_svc.get_all_currencies(enabled_only=True)
    kb_rows = [[InlineKeyboardButton(f"{c['code']} — {c['name']}",
                                      callback_data=f"amcw:adj_cur:{c['code']}")]
               for c in currencies]
    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="amcw:menu")])
    try:
        await q.edit_message_text(
            "💰 <b>Adjust User Balance</b>\n\nSelect currency:",
            reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="HTML",
        )
    except BadRequest:
        pass
    return ADJ_CURRENCY


async def amcw_adj_select_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    code  = parts[2].upper() if len(parts) >= 3 else ""
    context.user_data["adj_currency"] = code
    try:
        await q.edit_message_text(
            f"💰 <b>Adjust Balance — {code}</b>\n\n"
            f"Enter the user's Telegram ID or username:",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return ADJ_USER


async def amcw_adj_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lstrip("@")
    with get_db_session() as s:
        try:
            user = s.query(User).filter(User.telegram_id == int(text)).first()
        except ValueError:
            user = s.query(User).filter(User.username == text).first()
        if not user:
            await update.message.reply_text("❌ User not found. Try again:")
            return ADJ_USER
        context.user_data["adj_user_id"] = user.id
        uname = f"@{user.username}" if user.username else f"TG:{user.telegram_id}"
        code  = context.user_data.get("adj_currency", "?")
        bal   = mcw_svc.get_user_wallet_balance(user.id, code)
    await update.message.reply_text(
        f"User: <b>{uname}</b>\n"
        f"Current {code} balance: <b>{bal:.8f}</b>\n\n"
        f"Enter adjustment amount (positive = credit, negative = debit):\n"
        f"Or /cancel to abort.",
        parse_mode="HTML",
    )
    return ADJ_AMOUNT


async def amcw_adj_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delta = float((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Not a number. Enter the adjustment delta:")
        return ADJ_AMOUNT
    if delta == 0:
        await update.message.reply_text("❌ Delta must be non-zero:")
        return ADJ_AMOUNT
    context.user_data["adj_delta"] = delta
    await update.message.reply_text("Enter the reason for this adjustment:")
    return ADJ_REASON


async def amcw_adj_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason  = ((update.message.text or "").strip())[:200] or "admin adjust"
    user_id = context.user_data.pop("adj_user_id", None)
    code    = context.user_data.pop("adj_currency", "")
    delta   = context.user_data.pop("adj_delta", 0.0)
    if not user_id or not code or delta == 0:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END
    try:
        result = mcw_svc.admin_adjust(
            user_id, code, delta, reason=reason,
            actor_id=update.effective_user.id,
        )
        log_admin_action(update.effective_user.id, "mcwallet.adjust",
                         target_type="user", target_id=user_id,
                         details=f"currency={code} delta={delta:+.8f} reason={reason}")
        await update.message.reply_text(
            f"✅ Adjusted!\nNew {code} balance: <b>{result['new_balance']:.8f}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


async def amcw_adj_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Adjustment cancelled.")
    return ConversationHandler.END


def build_adj_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(amcw_adj_start, pattern=r"^amcw:adj:start$")],
        states={
            ADJ_CURRENCY: [CallbackQueryHandler(amcw_adj_select_currency,
                                                pattern=r"^amcw:adj_cur:.+$")],
            ADJ_USER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_adj_user)],
            ADJ_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_adj_amount)],
            ADJ_REASON:   [MessageHandler(filters.TEXT & ~filters.COMMAND, amcw_adj_reason)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, amcw_adj_cancel)],
        allow_reentry=True,
    )


# ─── Route dispatcher (non-conv actions) ─────────────────────────────────────

async def amcw_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route amcw:* callbacks that are not handled by conversations."""
    q = update.callback_query
    parts = (q.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await amcw_menu(update, context)
    elif action == "cur":
        await amcw_currency_detail(update, context)
    elif action == "toggle":
        await amcw_toggle(update, context)
    elif action == "freeze":
        await amcw_freeze(update, context)
    elif action == "wallets":
        await amcw_wallets_list(update, context)
    elif action == "uwal":
        await amcw_user_wallet(update, context)
    elif action == "frzwal":
        await amcw_freeze_user_wallet(update, context)
    elif action == "logs":
        await amcw_logs(update, context)
    elif action == "credit":
        # Route to specific-user credit: store in context and start conv
        parts2 = (q.data or "").split(":")
        uid  = int(parts2[2]) if len(parts2) >= 3 else 0
        code = parts2[3].upper() if len(parts2) >= 4 else ""
        await q.answer()
        context.user_data["adj_user_id"] = uid
        context.user_data["adj_currency"] = code
        context.user_data["adj_sign"] = +1
        try:
            await q.edit_message_text(
                f"➕ <b>Credit {code}</b>\n\nEnter amount to credit:\n/cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest:
            pass
    elif action == "debit":
        parts2 = (q.data or "").split(":")
        uid  = int(parts2[2]) if len(parts2) >= 3 else 0
        code = parts2[3].upper() if len(parts2) >= 4 else ""
        await q.answer()
        context.user_data["adj_user_id"] = uid
        context.user_data["adj_currency"] = code
        context.user_data["adj_sign"] = -1
        try:
            await q.edit_message_text(
                f"➖ <b>Debit {code}</b>\n\nEnter amount to debit:\n/cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest:
            pass
    else:
        await q.answer()
        await amcw_menu(update, context)


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all admin multi-currency wallet handlers."""
    app.add_handler(build_add_currency_conv())
    app.add_handler(build_adj_conv())
    # General dispatcher for all other amcw:* callbacks
    app.add_handler(CallbackQueryHandler(amcw_dispatch, pattern=r"^amcw:.+$"))
