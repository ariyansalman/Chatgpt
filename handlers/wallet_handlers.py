"""Section 12 — user-facing Wallet menu (the complete financial center).

Shows the real current balance, Total Added, and Total Spent (sums of
COMPLETED Transaction/Order rows only — never counts failed/rejected/
pending/refund transactions). Buttons: Add Funds (routes into the existing
topup flow), Payment History, Back to Menu.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import User, Transaction, TransactionStatus, Order, OrderStatus
from utils.currency import toggle_user_currency, format_price_for_user
from utils.perf import perf_track
from i18n import t, get_user_language
from telegram.error import BadRequest


# Display-only friendly labels for payment methods — never changes the
# stored value or any verification/API logic, only what text the user sees.
# NOTE: "zinipay" itself is never shown — the SPECIFIC provider the user
# paid with (bKash / Nagad / Rocket) is resolved from the transaction's
# crypto_address in _payment_method_display() below via
# services.payment_ui.zinipay_provider_meta(); every other method falls
# back to services.payment_ui.gateway_meta(), the same shared label source
# used by the admin Pending Deposits screen, so a method's display name is
# always consistent everywhere it's shown.


_STATUS_EMOJI = {
    "completed": "✅",
    "pending": "⏳",
    "awaiting_confirmation": "🕓",
    "expired": "⌛",
    "cancelled": "❌",
    "failed": "❌",
    "rejected": "🚫",
}

# Colored circle indicator for the compact history row header.
_STATUS_DOT = {
    "completed": "🟢",
    "pending": "🟡",
    "awaiting_confirmation": "🟡",
    "processing": "🔵",
    "expired": "🔴",
    "cancelled": "🔴",
    "failed": "🔴",
    "rejected": "🔴",
}

# Friendly, properly-capitalized status text — never the raw enum/db value.
_STATUS_LABEL = {
    "completed": "Completed",
    "pending": "Pending",
    "awaiting_confirmation": "Awaiting Confirmation",
    "expired": "Expired",
    "cancelled": "Cancelled",
    "failed": "Failed",
    "rejected": "Rejected",
    "processing": "Processing",
}

_PAGE_SIZE = 10


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get((status or "").lower(), "🔹")


def _status_dot(status: str) -> str:
    return _STATUS_DOT.get((status or "").lower(), "🔹")


def _status_label(status: str) -> str:
    key = (status or "").lower()
    return _STATUS_LABEL.get(key, key.replace("_", " ").title() or "Unknown")


def _payment_method_display(payment_method, crypto_address: str | None = None) -> str:
    raw = payment_method.value if payment_method else None
    if raw == "zinipay":
        from services.payment_ui import zinipay_provider_meta
        label, _emoji = zinipay_provider_meta(crypto_address=crypto_address)
        return label
    from services.payment_ui import gateway_meta
    label, _emoji = gateway_meta(raw)
    return label


def _totals(tg_id: int) -> tuple[float, float, float]:
    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        if not u:
            return 0.0, 0.0, 0.0
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
    return bal, total_dep, total_spent


def _fetch_history_page(tg_id: int, page: int = 0) -> tuple[list, int]:
    """Return (rows, total_count) for the requested page (0-indexed, _PAGE_SIZE per page).

    Newest transactions first. Each row is a tuple of:
      (deposit_id, amount, status_str, payment_method_label, created_at)
    """
    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        if not u:
            return [], 0
        base_q = s.query(Transaction).filter(Transaction.user_id == u.id)
        total = base_q.count()
        txns = (
            base_q
            .order_by(Transaction.created_at.desc())
            .offset(page * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
            .all()
        )
        from services.payment_ui import format_deposit_id as _fmt_dep_id
        rows = [
            (
                _fmt_dep_id(t.id, t.created_at),
                t.amount,
                t.status.value if t.status else "?",
                _payment_method_display(t.payment_method, t.crypto_address),
                t.created_at,
            )
            for t in txns
        ]
    return rows, total


@perf_track("wallet_handler")
async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    bal, dep, spent = _totals(tg_id)

    # Premium marketplace wallet card — no dividers, clean spacing
    bal_str   = format_price_for_user(bal,   tg_id)
    dep_str   = format_price_for_user(dep,   tg_id)
    spent_str = format_price_for_user(spent, tg_id)
    text = (
        "👛 <b>Wallet</b>\n\n"
        f"💰 Current Balance: <b>{bal_str}</b>\n"
        f"📥 Total Added: <b>{dep_str}</b>\n"
        f"🛒 Total Spent: <b>{spent_str}</b>\n\n"
        "Manage your wallet using the options below."
    )

    kb_rows = [
        [InlineKeyboardButton("➕ Add Funds",       callback_data="topup"),
         InlineKeyboardButton("📜 Payment History", callback_data="wallet_history")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    kb = InlineKeyboardMarkup(kb_rows)
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


async def _render_history(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Shared renderer for wallet_history and wallet_history_page."""
    q = update.callback_query
    tg_id = update.effective_user.id
    lang = get_user_language(tg_id)
    rows, total = _fetch_history_page(tg_id, page)
    total_pages = max(1, -(-total // _PAGE_SIZE))  # ceiling division

    if not rows:
        body = t("common.no_transactions", lang)
    else:
        lines = []
        for dep_id, amt, st, pm, ts in rows:
            dot   = _status_dot(st)
            emoji = _status_emoji(st)
            label = _status_label(st)
            amt_fmt = format_price_for_user(amt, tg_id)
            when = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            lines.append(
                f"{dot} <b>{dep_id}</b>\n"
                f"💰 {amt_fmt} • 💳 {pm} • {emoji} {label}\n"
                f"🕒 {when}"
            )
        body = "\n\n".join(lines)

    # Pagination row — only shown when more than one page exists
    kb_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"wallet_history_p_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"wallet_history_p_{page + 1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton("⬅️ Back to Wallet", callback_data="wallet")])
    kb = InlineKeyboardMarkup(kb_rows)

    page_indicator = f" <i>({page + 1}/{total_pages})</i>" if total_pages > 1 else ""
    title = f"📜 <b>Payment History</b>{page_indicator}"
    try:
        await q.edit_message_text(f"{title}\n\n{body}", reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _render_history(update, context, page=0)


async def wallet_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination callbacks: wallet_history_p_<page>."""
    q = update.callback_query
    await q.answer()
    try:
        page = int(q.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        page = 0
    await _render_history(update, context, page=page)


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
    app.add_handler(CallbackQueryHandler(wallet_history_page, pattern=r"^wallet_history_p_\d+$"))
    app.add_handler(CallbackQueryHandler(wallet_currency_toggle, pattern=r"^wallet_currency_toggle$"))
