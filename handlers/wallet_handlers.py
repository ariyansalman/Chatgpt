"""Section 12 — user-facing Wallet menu.

Shows the real current balance and Total Deposited (sum of COMPLETED
Transaction rows only — never counts failed/rejected/pending/refund/purchase).
Buttons: Add Funds (routes into existing topup flow), Payment History,
Download Statement (CSV), Back.
"""
from __future__ import annotations

import csv
import io

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import User, Transaction, TransactionStatus, Order, OrderStatus
from utils.currency import toggle_user_currency, format_price_for_user
from utils.perf import perf_track
from i18n import t, get_user_language
from telegram.error import BadRequest


# Display-only label overrides for payment methods whose internal/API key
# should not be shown to end users verbatim (e.g. gateway codenames).
# This does NOT change the stored value or any verification/API logic —
# it only affects what text the user sees in their transaction history.
_PAYMENT_METHOD_DISPLAY_OVERRIDES = {
    "zinipay": "BKash • Nagad • Rocket",
}


_STATUS_EMOJI = {
    "completed": "✅",
    "pending": "⏳",
    "awaiting_confirmation": "🕓",
    "expired": "⌛",
    "cancelled": "🚫",
    "failed": "❌",
    "rejected": "🚫",
}


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get((status or "").lower(), "🔹")


def _payment_method_display(payment_method) -> str:
    raw = payment_method.value if payment_method else "?"
    return _PAYMENT_METHOD_DISPLAY_OVERRIDES.get(raw, raw)


def _totals(tg_id: int) -> tuple[float, float, float, list[Transaction]]:
    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        if not u:
            return 0.0, 0.0, 0.0, []
        bal = float(u.wallet_balance or 0)
        # Only completed deposit transactions count toward Total Deposited.
        total_dep = 0.0
        for row in s.query(Transaction).filter(
            Transaction.user_id == u.id,
            Transaction.status == TransactionStatus.COMPLETED,
        ).all():
            if float(row.amount or 0) > 0:
                total_dep += float(row.amount)
        # Lifetime spend — completed orders only. Display-only figure,
        # does not touch order/payment processing logic.
        total_spent = sum(
            row[0] or 0.0
            for row in s.query(Order.total_amount)
            .filter(Order.user_id == u.id, Order.status == OrderStatus.COMPLETED)
            .all()
        )
        history = (
            s.query(Transaction)
            .filter(Transaction.user_id == u.id)
            .order_by(Transaction.created_at.desc())
            .limit(10)
            .all()
        )
        # Detach: read attributes we need now. `deposit_id` is the
        # human-readable DEP-YYYYMMDD-NNNNNN reference — the internal
        # numeric primary key is never shown to the user.
        from services.payment_ui import format_deposit_id as _fmt_dep_id
        hist = [(_fmt_dep_id(t.id, t.created_at), t.amount, t.status.value if t.status else "?",
                 _payment_method_display(t.payment_method),
                 t.created_at) for t in history]
    return bal, total_dep, total_spent, hist


def _full_history(tg_id: int, limit: int = 5000) -> list[tuple]:
    """All of this user's transactions, most recent first, for statement
    export. Capped at `limit` rows as a safety bound — a single user's own
    history is never expected to approach this in practice."""
    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        if not u:
            return []
        rows = (
            s.query(Transaction)
            .filter(Transaction.user_id == u.id)
            .order_by(Transaction.created_at.desc())
            .limit(limit)
            .all()
        )
        from services.payment_ui import format_deposit_id as _fmt_dep_id
        return [
            (_fmt_dep_id(r.id, r.created_at), r.amount, r.status.value if r.status else "?",
             _payment_method_display(r.payment_method), r.created_at)
            for r in rows
        ]


@perf_track("wallet_handler")
async def wallet_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the user their full wallet transaction history as a CSV file."""
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    rows = _full_history(tg_id)
    if not rows:
        await q.answer(t("common.no_transactions", lang), show_alert=True)
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Deposit ID", "Amount", "Status", "Method", "Date (UTC)"])
    for deposit_id, amt, st, pm, ts in rows:
        writer.writerow([
            deposit_id, f"{float(amt or 0):.2f}", st, pm,
            ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "",
        ])
    data = buf.getvalue().encode("utf-8")

    file_obj = InputFile(io.BytesIO(data), filename=f"wallet_statement_{tg_id}.csv")
    await q.message.reply_document(
        document=file_obj,
        caption=t("wallet.export_caption", lang, count=len(rows)),
    )


@perf_track("wallet_handler")
async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    bal, dep, spent, _ = _totals(tg_id)

    # Premium marketplace wallet card — no dividers, clean spacing
    bal_str   = format_price_for_user(bal,   tg_id)
    dep_str   = format_price_for_user(dep,   tg_id)
    spent_str = format_price_for_user(spent, tg_id)
    text = (
        "👛 <b>Wallet</b>\n\n"
        f"💰 Current Balance: <b>{bal_str}</b>\n"
        f"📥 Total Deposited: <b>{dep_str}</b>\n"
        f"🛒 Total Spent: <b>{spent_str}</b>\n\n"
        "Manage your wallet using the options below."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Funds",       callback_data="topup"),
         InlineKeyboardButton("📜 Payment History", callback_data="wallet_history")],
        [InlineKeyboardButton("📄 Download Statement", callback_data="wallet_export"),
         InlineKeyboardButton("🌍 Multi-Currency",    callback_data="mcw:overview")],
        [InlineKeyboardButton("⬅️ Back to Menu",   callback_data="main_menu")],
    ])
    if q:
        try:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    _, _, _, hist = _totals(tg_id)
    if not hist:
        body = t("common.no_transactions", lang)
    else:
        lines = []
        for tid, amt, st, pm, ts in hist:
            when = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            lines.append(t(
                "wallet.history_row", lang,
                emoji=_status_emoji(st),
                id=tid, amount=format_price_for_user(amt, tg_id), method=pm, status=st, when=when,
            ))
        body = "\n\n".join(lines)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="wallet")]])
    history_title = "📜 <b>Payment History</b>"
    try:
        await q.edit_message_text(f"{history_title}\n\n{body}",
                                  reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def wallet_currency_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip the user's preferred display currency (USD <-> BDT) and re-render the wallet."""
    q = update.callback_query
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    new_currency = toggle_user_currency(tg_id)
    await q.answer(t("common.prices_now_in", lang, currency=new_currency))
    await wallet_menu(update, context)


def register_handlers(app):
    app.add_handler(CallbackQueryHandler(wallet_menu, pattern=r"^wallet$"))
    app.add_handler(CallbackQueryHandler(wallet_history, pattern=r"^wallet_history$"))
    app.add_handler(CallbackQueryHandler(wallet_export_csv, pattern=r"^wallet_export$"))
    app.add_handler(CallbackQueryHandler(wallet_currency_toggle, pattern=r"^wallet_currency_toggle$"))
