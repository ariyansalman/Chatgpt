"""Admin User Profile & User Management — complete production implementation.

Provides a full-featured user management panel accessible from the admin
menu under 👤 Users → 🔍 Advanced User Profile.

Callback / conversation map
────────────────────────────
  up:menu              — Users menu (entry)
  up:list:{page}:{sort} — Paginated user list
  up:search            — ConversationHandler: advanced multi-field search
  up:view:{uid}        — Full profile card for one user
  up:ord:{uid}:{page}  — Purchase history (paginated)
  up:topup:{uid}:{page} — Wallet top-up history (paginated)
  up:ref:{uid}:{page}  — Referral history (paginated)
  up:wal:{uid}:{page}  — Wallet ledger history (paginated)
  up:act:{uid}         — Activity / login history
  up:coup:{uid}:{page} — Coupon redemption history (paginated)
  up:add:{uid}         — Add balance → ConversationHandler
  up:ded:{uid}         — Remove balance → ConversationHandler
  up:bon:{uid}         — Give bonus → ConversationHandler
  up:ban:{uid}         — Ban confirmation screen
  up:ban:cfm:{uid}     — Ban execute
  up:ubn:{uid}         — Unban confirmation screen
  up:ubn:cfm:{uid}     — Unban execute
  up:del:{uid}         — Delete user data confirmation screen
  up:del:cfm:{uid}     — Delete user data execute

All mutations write a WalletLedger row and an AdminAuditLog entry.
All lists are paginated (LIMIT/OFFSET) and optimised with .count()
instead of loading full rows.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from sqlalchemy import func, or_

from database import (
    get_db_session,
    User,
    Order,
    OrderItem,
    Product,
    Transaction,
    ReferralReward,
    WalletLedger,
    CouponRedemption,
    Coupon,
    AdminAuditLog,
)
from database.models import (
    OrderStatus,
    TransactionStatus,
    PaymentMethod,
)
from utils.helpers import format_price, clear_ban_cache
from utils.audit import log_admin_action
from utils.permissions import has_permission
from config.settings import settings as app_settings

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
WAITING_UP_SEARCH = 20   # advanced search input
WAITING_UP_BAL    = 21   # balance / bonus amount input

# ── Pagination sizes ──────────────────────────────────────────────────────────
_PG_USERS  = 10
_PG_HIST   = 8

# ── Payment method display labels ────────────────────────────────────────────
_PM_LABELS = {
    PaymentMethod.CRYPTO_WALLET:  "Crypto Wallet",
    PaymentMethod.CARD:           "Card",
    PaymentMethod.MANUAL:         "Manual",
    PaymentMethod.BKASH:          "bKash",
    PaymentMethod.NAGAD:          "Nagad",
    PaymentMethod.STARS:          "⭐ Stars",
    PaymentMethod.CRYPTOMUS:      "Cryptomus",
    PaymentMethod.NOWPAYMENTS:    "NOWPayments",
    PaymentMethod.ZINIPAY:        "ZiniPay",
    PaymentMethod.BINANCE_PAY:    "Binance Pay",
    PaymentMethod.BYBIT_PAY:      "Bybit Pay",
    PaymentMethod.HELEKET:        "Heleket",
}

_ORDER_STATUS_ICON = {
    OrderStatus.PROCESSING: "⏳",
    OrderStatus.COMPLETED:  "✅",
    OrderStatus.CANCELLED:  "❌",
    OrderStatus.FAILED:     "⚠️",
    OrderStatus.REFUNDED:   "↩️",
}

_TX_STATUS_ICON = {
    TransactionStatus.PENDING:               "⏳",
    TransactionStatus.AWAITING_CONFIRMATION: "🔍",
    TransactionStatus.COMPLETED:             "✅",
    TransactionStatus.FAILED:                "❌",
    TransactionStatus.EXPIRED:               "⌛",
    TransactionStatus.CANCELLED:             "🚫",
    TransactionStatus.REJECTED:              "🚷",
}


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: str | None) -> str:
    return html.escape(str(text)) if text else "—"


def _name(user: User) -> str:
    return f"@{user.username}" if user.username else f"User {user.telegram_id}"


def _name_esc(user: User) -> str:
    return html.escape(_name(user))


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _fmt_pm(pm) -> str:
    if pm is None:
        return "—"
    return _PM_LABELS.get(pm, str(pm.value) if hasattr(pm, "value") else str(pm))


async def _safe_edit(query, text: str, kb: InlineKeyboardMarkup, parse_mode: str = "HTML") -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.debug("safe_edit: %s", e)


def _back_to_profile_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")]
    ])


# ─────────────────────────────────────────────────────────────────────────────
# 1. Users Menu
# ─────────────────────────────────────────────────────────────────────────────

async def up_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:menu — User Management hub."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 User List",              callback_data="up:list:0:desc")],
        [InlineKeyboardButton("🔍 Advanced Search",        callback_data="up:search")],
        [InlineKeyboardButton("🔍 Customer 360° View",     callback_data="c360:search")],
        [InlineKeyboardButton("↩️ Return",                 callback_data="admin_menu")],
    ])
    await _safe_edit(
        query,
        "👥 <b>User Management</b>\n\nSelect an option:",
        kb,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Paginated User List
# ─────────────────────────────────────────────────────────────────────────────

async def up_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:list:{page}:{sort} — paginated user list."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        page = int(parts[2])
        sort = parts[3] if len(parts) > 3 else "desc"
    except (IndexError, ValueError):
        page, sort = 0, "desc"
    sort = "asc" if sort == "asc" else "desc"

    with get_db_session() as session:
        total = session.query(func.count(User.id)).scalar() or 0
        col   = User.created_at.asc() if sort == "asc" else User.created_at.desc()
        rows  = (
            session.query(User.id, User.username, User.telegram_id, User.is_banned)
            .order_by(col)
            .offset(page * _PG_USERS)
            .limit(_PG_USERS)
            .all()
        )

    total_pages = max(1, (total + _PG_USERS - 1) // _PG_USERS)
    next_sort   = "asc" if sort == "desc" else "desc"
    sort_icon   = "🕒" if sort == "desc" else "🕰️"

    kb_rows = []
    for uid, username, tg_id, is_banned in rows:
        status = "🚫 " if is_banned else ""
        label  = f"{status}@{username} | {tg_id}" if username else f"{status}User {tg_id}"
        kb_rows.append([InlineKeyboardButton(label[:60], callback_data=f"up:view:{uid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:list:{page-1}:{sort}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:list:{page+1}:{sort}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([
        InlineKeyboardButton(f"{sort_icon} Sort: {'Latest' if sort == 'desc' else 'Oldest'}",
                             callback_data=f"up:list:{page}:{next_sort}"),
    ])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="up:menu")])

    await _safe_edit(
        query,
        f"👥 <b>User List</b> — {total} total\n"
        f"Page {page+1}/{total_pages}",
        InlineKeyboardMarkup(kb_rows),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Advanced Search — ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

async def up_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: up:search — prompt for search query."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    try:
        msg = await query.edit_message_text(
            "🔍 <b>Advanced User Search</b>\n\n"
            "Search by any of the following:\n"
            "• Telegram User ID (numeric)\n"
            "• @username\n"
            "• First name / display name\n"
            "• Order ID  (e.g. <code>ord:12345</code>)\n"
            "• Wallet/crypto address\n\n"
            "Type your query and send it.\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
        context.user_data["_up_search_msg_id"]  = msg.message_id
        context.user_data["_up_search_chat_id"] = update.effective_chat.id
    except Exception:
        pass
    return WAITING_UP_SEARCH


async def up_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the search query, run multi-field lookup, show results or profile."""
    if not has_permission(update.effective_user.id, "manage_users"):
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    msg_id  = context.user_data.get("_up_search_msg_id")
    chat_id = context.user_data.get("_up_search_chat_id", update.effective_chat.id)

    def _re_prompt(text: str):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="up:menu")
        ]])
        return context.bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id, reply_markup=kb, parse_mode="HTML"
        )

    matches: list[tuple[int, str, int, bool]] = []  # (id, username, tg_id, is_banned)

    with get_db_session() as session:

        # 1. Order ID shortcut: ORD-YYYYMMDD-NNNNNN, "ord:NNN", "#NNN", or pure integer ≥ 5 digits
        import re as _re
        order_id_search: int | None = None
        clean = raw.lower().lstrip("#").strip()
        _ord_re = _re.compile(r"^ord-\d{8}-0*(\d+)$")
        m_ord = _ord_re.match(clean)
        if m_ord:
            order_id_search = int(m_ord.group(1))
        elif clean.startswith("ord:") and clean[4:].isdigit():
            order_id_search = int(clean[4:])
        elif clean.isdigit() and len(clean) >= 5:
            order_id_search = int(clean)

        if order_id_search:
            row = (
                session.query(User.id, User.username, User.telegram_id, User.is_banned)
                .join(Order, Order.user_id == User.id)
                .filter(Order.id == order_id_search)
                .first()
            )
            if row:
                matches = [row]

        # 2. Telegram ID (numeric, short)
        if not matches and raw.lstrip("@+").isdigit():
            tg_id = int(raw.lstrip("@+"))
            row = (
                session.query(User.id, User.username, User.telegram_id, User.is_banned)
                .filter(User.telegram_id == tg_id)
                .first()
            )
            if row:
                matches = [row]

        # 3. Wallet / crypto address (long non-numeric strings that look like addresses)
        if not matches and len(raw) >= 20 and raw.lstrip("@").replace("-", "").replace("_", "").isalnum():
            row = (
                session.query(User.id, User.username, User.telegram_id, User.is_banned)
                .join(Transaction, Transaction.user_id == User.id)
                .filter(
                    or_(
                        Transaction.crypto_address.ilike(f"%{raw}%"),
                        Transaction.txid.ilike(f"%{raw}%"),
                    )
                )
                .first()
            )
            if row:
                matches = [row]

        # 4. @username (exact, case-insensitive)
        if not matches:
            uname = raw.lstrip("@")
            rows = (
                session.query(User.id, User.username, User.telegram_id, User.is_banned)
                .filter(User.username.ilike(uname))
                .limit(20)
                .all()
            )
            if rows:
                matches = list(rows)

        # 5. Partial username / first-name fuzzy match
        if not matches and len(raw.lstrip("@")) >= 2:
            term = raw.lstrip("@").replace("%", "").replace("_", "\\_")
            rows = (
                session.query(User.id, User.username, User.telegram_id, User.is_banned)
                .filter(User.username.ilike(f"%{term}%"))
                .limit(20)
                .all()
            )
            if rows:
                matches = list(rows)

    if not matches:
        try:
            await _re_prompt(
                "❌ <b>No users found.</b>\n\n"
                "Try a different Telegram ID, @username, order ID, or wallet address.\n\n"
                "Send /cancel to abort."
            )
        except Exception:
            pass
        return WAITING_UP_SEARCH

    # Single match → go straight to profile
    if len(matches) == 1:
        uid = matches[0][0]
        context.user_data.pop("_up_search_msg_id", None)
        context.user_data.pop("_up_search_chat_id", None)
        kb = _build_profile_kb(uid, is_banned=matches[0][3])
        profile_text = _build_profile_text_by_id(uid)
        try:
            await context.bot.edit_message_text(
                profile_text,
                chat_id=chat_id, message_id=msg_id,
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.debug("up_search_receive single: %s", e)
        return ConversationHandler.END

    # Multiple matches → show selector
    kb_rows = []
    for uid, username, tg_id, is_banned in matches[:15]:
        status = "🚫 " if is_banned else ""
        label  = f"{status}@{username} | {tg_id}" if username else f"{status}User {tg_id}"
        kb_rows.append([InlineKeyboardButton(label[:60], callback_data=f"up:view:{uid}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="up:menu")])

    context.user_data.pop("_up_search_msg_id", None)
    context.user_data.pop("_up_search_chat_id", None)
    try:
        await context.bot.edit_message_text(
            f"🔍 <b>Search results for:</b> <code>{_esc(raw)}</code>\n\n"
            f"Found {len(matches)} user(s). Select one:",
            chat_id=chat_id, message_id=msg_id,
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.debug("up_search_receive multi: %s", e)
    return ConversationHandler.END


async def up_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_up_search_msg_id", None)
    context.user_data.pop("_up_search_chat_id", None)
    if update.message:
        try:
            await update.message.reply_text("🔍 Search cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def build_up_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(up_search_start, pattern=r"^up:search$")],
        states={
            WAITING_UP_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, up_search_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", up_search_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Full Profile Card
# ─────────────────────────────────────────────────────────────────────────────

def _build_profile_text_by_id(uid: int) -> str:
    """Build the full HTML profile card. Session is opened internally."""
    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            return "❌ User not found."
        return _build_profile_text(session, user)


def _build_profile_text(session, user: User) -> str:
    """Assemble the full profile HTML block for one user (session must be open)."""
    # ── Wallet top-ups ────────────────────────────────────────────────────
    total_deposited = (
        session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == user.id,
            Transaction.status == TransactionStatus.COMPLETED,
        )
        .scalar()
    ) or 0.0

    # ── Orders / spend ────────────────────────────────────────────────────
    order_stats = (
        session.query(
            Order.status,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0.0),
        )
        .filter(Order.user_id == user.id)
        .group_by(Order.status)
        .all()
    )
    total_orders = sum(c for _, c, _ in order_stats)
    total_spent  = sum(
        float(s) for status, _, s in order_stats
        if status == OrderStatus.COMPLETED
    )

    # ── Referrals ─────────────────────────────────────────────────────────
    total_refs = (
        session.query(func.count(User.id))
        .filter(User.referred_by_id == user.id)
        .scalar()
    ) or 0

    active_refs = (
        session.query(func.count(User.id))
        .filter(User.referred_by_id == user.id, User.has_purchased.is_(True))
        .scalar()
    ) or 0

    referral_earnings = float(user.referral_earnings or 0.0)

    # ── Status ────────────────────────────────────────────────────────────
    status_str = "🚫 Banned" if user.is_banned else "✅ Active"
    lang       = (user.language or "en").upper()

    lines = [
        "👤 <b>User Profile</b>",
        "",
        "━━━ <b>Account Information</b> ━━━",
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>",
        f"👤 Username:    {_esc('@' + user.username) if user.username else '—'}",
        f"🌐 Language:    {lang}",
        f"📅 Registered:  {_fmt_dt(user.created_at)}",
        f"🕐 Last Active: {_fmt_dt(user.last_seen_at)}",
        f"🔒 Status:      {status_str}",
        "",
        "━━━ <b>Wallet & Financials</b> ━━━",
        f"💰 Balance:     <b>{format_price(float(user.wallet_balance or 0.0))}</b>",
        f"📥 Deposited:   {format_price(float(total_deposited))}",
        f"💸 Total Spent: {format_price(total_spent)}",
        "",
        "━━━ <b>Orders</b> ━━━",
        f"📦 Total Orders: {total_orders}",
    ]
    for status, cnt, amt in order_stats:
        icon = _ORDER_STATUS_ICON.get(status, "•")
        lines.append(f"  {icon} {status.value}: {cnt}")

    lines += [
        "",
        "━━━ <b>Referrals</b> ━━━",
        f"👥 Total Refs:    {total_refs}",
        f"✅ Active Refs:   {active_refs}",
        f"💵 Ref Earnings:  {format_price(referral_earnings)}",
        f"🏆 Loyalty Pts:  {user.loyalty_points or 0}",
    ]
    return "\n".join(lines)


def _build_profile_kb(uid: int, is_banned: bool) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton("✅ Unban", callback_data=f"up:ubn:{uid}")
        if is_banned
        else InlineKeyboardButton("🚫 Ban",   callback_data=f"up:ban:{uid}")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Balance",    callback_data=f"up:add:{uid}"),
            InlineKeyboardButton("➖ Remove Balance", callback_data=f"up:ded:{uid}"),
        ],
        [
            InlineKeyboardButton("🎁 Give Bonus",    callback_data=f"up:bon:{uid}"),
        ],
        [
            InlineKeyboardButton("📦 Orders",        callback_data=f"up:ord:{uid}:0"),
            InlineKeyboardButton("💳 Top-ups",       callback_data=f"up:topup:{uid}:0"),
        ],
        [
            InlineKeyboardButton("👥 Referrals",     callback_data=f"up:ref:{uid}:0"),
            InlineKeyboardButton("💰 Wallet History",callback_data=f"up:wal:{uid}:0"),
        ],
        [
            InlineKeyboardButton("🎟 Coupons",       callback_data=f"up:coup:{uid}:0"),
            InlineKeyboardButton("📊 Activity",      callback_data=f"up:act:{uid}"),
        ],
        [ban_btn],
        [InlineKeyboardButton("🗑 Delete User Data", callback_data=f"up:del:{uid}")],
        [InlineKeyboardButton("🔙 Back to List",     callback_data="up:list:0:desc")],
    ])


async def up_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:view:{uid} — render/refresh full profile."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.",
                             InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="up:menu")]]))
            return
        text = _build_profile_text(session, user)
        kb   = _build_profile_kb(uid, user.is_banned)

    await _safe_edit(query, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Purchase History
# ─────────────────────────────────────────────────────────────────────────────

async def up_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ord:{uid}:{page} — paginated purchase history."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid  = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    lines = [f"📦 <b>Purchase History</b>", ""]
    nav   = []

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        lines[0] = f"📦 <b>Purchase History</b> — {_name_esc(user)}"

        total = (
            session.query(func.count(Order.id))
            .filter(Order.user_id == uid)
            .scalar()
        ) or 0

        orders = (
            session.query(Order)
            .filter(Order.user_id == uid)
            .order_by(Order.created_at.desc())
            .offset(page * _PG_HIST)
            .limit(_PG_HIST)
            .all()
        )

        total_pages = max(1, (total + _PG_HIST - 1) // _PG_HIST)

        if not orders:
            lines.append("No orders found.")
        else:
            lines.append(f"Total: {total} order(s) | Page {page+1}/{total_pages}")
            lines.append("")
            for o in orders:
                icon = _ORDER_STATUS_ICON.get(o.status, "•")
                lines.append(
                    f"{icon} <b>Order #{o.id}</b> — {_fmt_dt(o.created_at)}\n"
                    f"   Status: {o.status.value}  |  Total: {format_price(float(o.total_amount or 0))}"
                )
                # Line items
                items = (
                    session.query(OrderItem, Product)
                    .join(Product, Product.id == OrderItem.product_id, isouter=True)
                    .filter(OrderItem.order_id == o.id)
                    .all()
                )
                for item, product in items:
                    pname = _esc(product.name) if product else f"Product #{item.product_id}"
                    lines.append(
                        f"   • {pname}  ×{item.quantity}  @{format_price(float(item.price))}"
                    )

        # Pagination nav
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:ord:{uid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:ord:{uid}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Wallet Top-up History
# ─────────────────────────────────────────────────────────────────────────────

async def up_topup_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:topup:{uid}:{page} — paginated wallet top-up history."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid  = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    lines = ["💳 <b>Wallet Top-up History</b>", ""]
    nav   = []

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        lines[0] = f"💳 <b>Wallet Top-up History</b> — {_name_esc(user)}"

        total = (
            session.query(func.count(Transaction.id))
            .filter(Transaction.user_id == uid)
            .scalar()
        ) or 0

        txns = (
            session.query(Transaction)
            .filter(Transaction.user_id == uid)
            .order_by(Transaction.created_at.desc())
            .offset(page * _PG_HIST)
            .limit(_PG_HIST)
            .all()
        )

        total_pages = max(1, (total + _PG_HIST - 1) // _PG_HIST)

        if not txns:
            lines.append("No top-up records found.")
        else:
            lines.append(f"Total: {total} transaction(s) | Page {page+1}/{total_pages}")
            lines.append("")
            for tx in txns:
                icon        = _TX_STATUS_ICON.get(tx.status, "•")
                gateway     = _fmt_pm(tx.payment_method)
                amount_str  = format_price(float(tx.amount or 0))
                verified    = "✅ Verified" if tx.status == TransactionStatus.COMPLETED else "⏳ Pending"
                txid_short  = f"<code>{str(tx.txid)[:20]}</code>" if tx.txid else "—"
                lines.append(
                    f"{icon} <b>TX #{tx.id}</b> — {_fmt_dt(tx.created_at)}\n"
                    f"   Gateway: {gateway}  |  Amount: {amount_str}\n"
                    f"   Status: {tx.status.value}  |  Verification: {verified}\n"
                    f"   TXID: {txid_short}"
                )
                # Bonus annotation if admin_note contains bonus info
                if tx.admin_note:
                    lines.append(f"   📝 Note: {_esc(tx.admin_note[:80])}")

        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:topup:{uid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:topup:{uid}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────────
# 7. Referral History
# ─────────────────────────────────────────────────────────────────────────────

async def up_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ref:{uid}:{page} — paginated referral history."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid  = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    lines = ["👥 <b>Referral History</b>", ""]
    nav   = []

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        lines[0] = f"👥 <b>Referral History</b> — {_name_esc(user)}"

        # Summary stats
        total_refs = (
            session.query(func.count(User.id))
            .filter(User.referred_by_id == uid)
            .scalar()
        ) or 0
        active_refs = (
            session.query(func.count(User.id))
            .filter(User.referred_by_id == uid, User.has_purchased.is_(True))
            .scalar()
        ) or 0
        total_earnings = float(user.referral_earnings or 0.0)

        lines += [
            f"📊 Total Referrals:   {total_refs}",
            f"✅ Active Referrals:  {active_refs}",
            f"💵 Total Earnings:    {format_price(total_earnings)}",
            "",
        ]

        total_pages = max(1, (total_refs + _PG_HIST - 1) // _PG_HIST) if total_refs else 1

        referred_users = (
            session.query(User)
            .filter(User.referred_by_id == uid)
            .order_by(User.created_at.desc())
            .offset(page * _PG_HIST)
            .limit(_PG_HIST)
            .all()
        )

        if not referred_users:
            lines.append("No referred users found.")
        else:
            lines.append(f"Showing page {page+1}/{total_pages}")
            lines.append("")
            for ref_user in referred_users:
                # Get total deposited by this referred user
                ref_deposited = (
                    session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
                    .filter(
                        Transaction.user_id == ref_user.id,
                        Transaction.status == TransactionStatus.COMPLETED,
                    )
                    .scalar()
                ) or 0.0
                # Get commission earned from this specific referred user
                reward_row = (
                    session.query(func.coalesce(func.sum(ReferralReward.amount), 0.0))
                    .filter(ReferralReward.referred_id == ref_user.id)
                    .scalar()
                ) or 0.0
                ref_name  = _name_esc(ref_user)
                purchased = "✅" if ref_user.has_purchased else "❌"
                lines.append(
                    f"• {ref_name} (ID: <code>{ref_user.telegram_id}</code>)\n"
                    f"  Joined: {_fmt_dt(ref_user.created_at)}  |  Active: {purchased}\n"
                    f"  Deposited: {format_price(float(ref_deposited))}  |  Commission: {format_price(float(reward_row))}"
                )

        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:ref:{uid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:ref:{uid}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────────
# 8. Wallet Ledger History
# ─────────────────────────────────────────────────────────────────────────────

async def up_wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:wal:{uid}:{page} — paginated WalletLedger."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid  = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    lines = ["💰 <b>Wallet History</b>", ""]
    nav   = []

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        lines[0] = f"💰 <b>Wallet History</b> — {_name_esc(user)}"

        total = (
            session.query(func.count(WalletLedger.id))
            .filter(WalletLedger.user_id == uid)
            .scalar()
        ) or 0

        entries = (
            session.query(WalletLedger)
            .filter(WalletLedger.user_id == uid)
            .order_by(WalletLedger.created_at.desc())
            .offset(page * _PG_HIST)
            .limit(_PG_HIST)
            .all()
        )

        total_pages = max(1, (total + _PG_HIST - 1) // _PG_HIST)

        if not entries:
            lines.append("No wallet history found.")
        else:
            lines.append(f"Total: {total} entries | Page {page+1}/{total_pages}")
            lines.append("")
            for e in entries:
                direction = "➕" if (e.delta or 0) >= 0 else "➖"
                bal_before = float((e.balance_after or 0) - (e.delta or 0))
                lines.append(
                    f"{direction} <b>{format_price(abs(float(e.delta or 0)))}</b> — {_fmt_dt(e.created_at)}\n"
                    f"   Reason: {_esc(e.reason or '—')}\n"
                    f"   Before: {format_price(bal_before)}  →  After: {format_price(float(e.balance_after or 0))}\n"
                    f"   Actor: {e.actor_type or '—'}"
                    + (f" (ID: <code>{e.actor_id}</code>)" if e.actor_id else "")
                )

        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:wal:{uid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:wal:{uid}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Activity / Login History
# ─────────────────────────────────────────────────────────────────────────────

async def up_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:act:{uid} — activity and login summary."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return

        # Last order date
        last_order = (
            session.query(func.max(Order.created_at))
            .filter(Order.user_id == uid)
            .scalar()
        )
        # Last top-up date
        last_topup = (
            session.query(func.max(Transaction.created_at))
            .filter(Transaction.user_id == uid)
            .scalar()
        )
        # Audit log entries involving this user
        audit_rows = (
            session.query(AdminAuditLog)
            .filter(
                AdminAuditLog.target_type == "user",
                AdminAuditLog.target_id == str(uid),
            )
            .order_by(AdminAuditLog.created_at.desc())
            .limit(10)
            .all()
        )

        text = (
            f"📊 <b>Activity History</b> — {_name_esc(user)}\n\n"
            f"━━━ <b>Timeline</b> ━━━\n"
            f"📅 Registration:  {_fmt_dt(user.created_at)}\n"
            f"🕐 Last Active:   {_fmt_dt(user.last_seen_at)}\n"
            f"📦 Last Order:    {_fmt_dt(last_order)}\n"
            f"💳 Last Top-up:   {_fmt_dt(last_topup)}\n\n"
            f"━━━ <b>Admin Actions on this User</b> ━━━\n"
        )
        if audit_rows:
            for row in audit_rows:
                text += (
                    f"• {_esc(row.action)} — {_fmt_dt(row.created_at)}\n"
                    + (f"  {_esc(row.details)}\n" if row.details else "")
                )
        else:
            text += "No admin actions recorded.\n"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")
        ]])

    await _safe_edit(query, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Coupon Redemption History
# ─────────────────────────────────────────────────────────────────────────────

async def up_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:coup:{uid}:{page} — coupon redemption history."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid  = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    lines = ["🎟 <b>Coupon History</b>", ""]
    nav   = []

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        lines[0] = f"🎟 <b>Coupon History</b> — {_name_esc(user)}"

        total = (
            session.query(func.count(CouponRedemption.id))
            .filter(CouponRedemption.user_id == uid)
            .scalar()
        ) or 0

        redemptions = (
            session.query(CouponRedemption, Coupon)
            .join(Coupon, Coupon.id == CouponRedemption.coupon_id, isouter=True)
            .filter(CouponRedemption.user_id == uid)
            .order_by(CouponRedemption.created_at.desc())
            .offset(page * _PG_HIST)
            .limit(_PG_HIST)
            .all()
        )

        total_pages = max(1, (total + _PG_HIST - 1) // _PG_HIST)

        if not redemptions:
            lines.append("No coupon redemptions found.")
        else:
            lines.append(f"Total: {total} redemption(s) | Page {page+1}/{total_pages}")
            lines.append("")
            for r, c in redemptions:
                code = _esc(c.code) if c else "—"
                discount_str = format_price(float(r.discount_applied or 0))
                order_ref    = f"Order #{r.order_id}" if r.order_id else "—"
                lines.append(
                    f"• <b>{code}</b> — {_fmt_dt(r.created_at)}\n"
                    f"  Discount: {discount_str}  |  Applied to: {order_ref}"
                )

        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"up:coup:{uid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"up:coup:{uid}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────────
# 11. Balance Operations (Add / Remove / Bonus) — shared ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

_BAL_ACTION_LABELS = {
    "add": ("➕ Add Balance",    "positive amount to add"),
    "ded": ("➖ Remove Balance", "amount to deduct"),
    "bon": ("🎁 Give Bonus",     "bonus amount to credit"),
}


async def _up_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["_upbal_action"] = action
    context.user_data["_upbal_uid"]    = uid

    label, prompt = _BAL_ACTION_LABELS.get(action, ("Edit", "amount"))

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return ConversationHandler.END
        user_label = _name_esc(user)
        current_bal = format_price(float(user.wallet_balance or 0))

    try:
        msg = await query.edit_message_text(
            f"{label}\n\n"
            f"User: {user_label}\n"
            f"Current Balance: {current_bal}\n\n"
            f"Enter {prompt} (USD):\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
        context.user_data["_upbal_msg_id"]  = msg.message_id
        context.user_data["_upbal_chat_id"] = update.effective_chat.id
    except Exception:
        pass
    return WAITING_UP_BAL


async def up_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _up_bal_start(update, context, "add")


async def up_ded_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _up_bal_start(update, context, "ded")


async def up_bon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _up_bal_start(update, context, "bon")


async def up_bal_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the amount, show confirmation with idempotency token."""
    if not has_permission(update.effective_user.id, "manage_users"):
        _clr_upbal(context)
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    uid     = context.user_data.get("_upbal_uid")
    action  = context.user_data.get("_upbal_action", "add")
    msg_id  = context.user_data.get("_upbal_msg_id")
    chat_id = context.user_data.get("_upbal_chat_id", update.effective_chat.id)

    label, _ = _BAL_ACTION_LABELS.get(action, ("Edit", "amount"))

    try:
        amount = Decimal(raw.replace(",", "."))
        if amount <= 0:
            raise ValueError("Amount must be positive.")
    except (InvalidOperation, ValueError) as e:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"up:view:{uid}")]])
        try:
            await context.bot.edit_message_text(
                f"❌ Invalid amount: <code>{_esc(raw)}</code>\n\n"
                "Please enter a valid positive number (e.g. <code>10.00</code>).\n\n"
                "Send /cancel to abort.",
                chat_id=chat_id, message_id=msg_id, reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass
        return WAITING_UP_BAL

    context.user_data["_upbal_amount"] = str(amount)
    tok = uuid.uuid4().hex[:16]
    context.user_data["_upbal_idem"] = tok

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        current_bal = format_price(float(user.wallet_balance or 0)) if user else "?"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirm",
                callback_data=f"up:bal:cfm:{uid}:{action}:{tok}",
            ),
            InlineKeyboardButton("❌ Cancel", callback_data=f"up:view:{uid}"),
        ]
    ])
    try:
        await context.bot.edit_message_text(
            f"⚠️ <b>Confirm {label}</b>\n\n"
            f"User: {uid}\n"
            f"Current Balance: {current_bal}\n"
            f"Amount: <b>{format_price(float(amount))}</b>\n\n"
            "Press <b>Confirm</b> to proceed.",
            chat_id=chat_id, message_id=msg_id, reply_markup=kb, parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.debug("up_bal_receive: %s", e)
    return ConversationHandler.END


async def up_bal_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:bal:cfm:{uid}:{action}:{tok} — execute balance change."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid    = int(parts[3])
        action = parts[4]
        tok    = parts[5]
    except (IndexError, ValueError):
        return

    stored_tok = (context.user_data.get("_upbal_idem") or "")[:16]
    if tok != stored_tok:
        await query.answer("⚠️ Already processed or session expired.", show_alert=True)
        return

    context.user_data["_upbal_idem"] = None  # consume token

    try:
        amount = Decimal(context.user_data.get("_upbal_amount", "0"))
    except InvalidOperation:
        await _safe_edit(query, "❌ Session expired. No changes made.", _back_to_profile_kb(uid))
        _clr_upbal(context)
        return

    admin_tg_id = update.effective_user.id
    label, _    = _BAL_ACTION_LABELS.get(action, ("Edit", "amount"))
    prev_bal    = 0.0
    new_bal     = 0.0

    try:
        with get_db_session() as session:
            user = (
                session.query(User)
                .filter(User.id == uid)
                .with_for_update()
                .first()
            )
            if not user:
                await _safe_edit(query, "❌ User not found. No changes made.",
                                 _back_to_profile_kb(uid))
                _clr_upbal(context)
                return

            prev_bal = float(user.wallet_balance or 0)
            delta    = float(amount)          # add / bon = +delta
            if action == "ded":
                delta = -delta

            new_bal = prev_bal + delta
            if new_bal < 0:
                await _safe_edit(
                    query,
                    "❌ Balance would go negative. Operation rejected.",
                    _back_to_profile_kb(uid),
                )
                _clr_upbal(context)
                return

            user.wallet_balance = new_bal
            reason = f"admin:{action.upper()}"
            if action == "bon":
                reason = "admin:BONUS"

            session.add(WalletLedger(
                user_id=uid,
                delta=delta,
                balance_after=new_bal,
                reason=reason,
                actor_type="admin",
                actor_id=admin_tg_id,
                ref_type="admin_adjust",
                ref_id=str(uid),
            ))
            session.commit()

        log_admin_action(
            admin_tg_id, f"wallet.{action}",
            target_type="user", target_id=uid,
            details=f"delta={delta:+.4f} prev={prev_bal:.4f} new={new_bal:.4f}",
        )
    except Exception as exc:
        logger.error("up_bal_confirm error uid=%s: %s", uid, exc, exc_info=True)
        await _safe_edit(
            query,
            f"❌ Balance update failed ({type(exc).__name__}). No changes made.",
            _back_to_profile_kb(uid),
        )
        _clr_upbal(context)
        return

    _clr_upbal(context)
    await _safe_edit(
        query,
        f"✅ <b>{label} successful.</b>\n\n"
        f"Previous: {format_price(prev_bal)}\n"
        f"Change:   {format_price(abs(float(amount)))}\n"
        f"New:      <b>{format_price(new_bal)}</b>",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")
        ]]),
    )


async def up_bal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("_upbal_uid")
    _clr_upbal(context)
    if update.message:
        try:
            await update.message.reply_text("Operation cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def _clr_upbal(ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("_upbal_action", "_upbal_uid", "_upbal_amount", "_upbal_idem",
              "_upbal_msg_id", "_upbal_chat_id"):
        ctx.user_data.pop(k, None)


def build_up_bal_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(up_add_start, pattern=r"^up:add:\d+$"),
            CallbackQueryHandler(up_ded_start, pattern=r"^up:ded:\d+$"),
            CallbackQueryHandler(up_bon_start, pattern=r"^up:bon:\d+$"),
        ],
        states={
            WAITING_UP_BAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, up_bal_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", up_bal_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 12. Ban / Unban
# ─────────────────────────────────────────────────────────────────────────────

async def up_ban_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ban:{uid} — ban confirmation screen."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        user_label = _name_esc(user)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Confirm Ban",  callback_data=f"up:ban:cfm:{uid}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"up:view:{uid}"),
        ]
    ])
    await _safe_edit(
        query,
        f"⚠️ <b>Ban User?</b>\n\nUser: {user_label}\n\n"
        "The user will be blocked from using the bot. This can be reversed.",
        kb,
    )


async def up_ban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ban:cfm:{uid} — execute ban."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[3])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        user.is_banned = True
        tg_id = user.telegram_id
        session.commit()

    try:
        clear_ban_cache(tg_id)
    except Exception:
        pass

    log_admin_action(
        update.effective_user.id, "user.ban",
        target_type="user", target_id=uid,
        details=f"banned tg_id={tg_id}",
    )
    await _safe_edit(
        query,
        f"✅ User <code>{tg_id}</code> has been <b>banned</b>.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")
        ]]),
    )


async def up_unban_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ubn:{uid} — unban confirmation screen."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        user_label = _name_esc(user)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm Unban", callback_data=f"up:ubn:cfm:{uid}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"up:view:{uid}"),
        ]
    ])
    await _safe_edit(
        query,
        f"⚠️ <b>Unban User?</b>\n\nUser: {user_label}\n\n"
        "The user will be able to use the bot again.",
        kb,
    )


async def up_unban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:ubn:cfm:{uid} — execute unban."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[3])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        user.is_banned = False
        tg_id = user.telegram_id
        session.commit()

    try:
        clear_ban_cache(tg_id)
    except Exception:
        pass

    log_admin_action(
        update.effective_user.id, "user.unban",
        target_type="user", target_id=uid,
        details=f"unbanned tg_id={tg_id}",
    )
    await _safe_edit(
        query,
        f"✅ User <code>{tg_id}</code> has been <b>unbanned</b>.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Profile", callback_data=f"up:view:{uid}")
        ]]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 13. Delete User Data
# ─────────────────────────────────────────────────────────────────────────────

async def up_delete_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:del:{uid} — delete-data confirmation screen."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=uid).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
            return
        user_label = _name_esc(user)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 CONFIRM DELETE", callback_data=f"up:del:cfm:{uid}")],
        [InlineKeyboardButton("❌ Cancel",          callback_data=f"up:view:{uid}")],
    ])
    await _safe_edit(
        query,
        f"⚠️ <b>Delete User Data?</b>\n\n"
        f"User: {user_label}\n\n"
        "This will <b>permanently delete</b> all personal data for this user "
        "(username, language, referral links, wallet, orders). "
        "This action <b>cannot be undone</b>.",
        kb,
    )


async def up_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: up:del:cfm:{uid} — execute GDPR-style data wipe."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        uid = int(parts[3])
    except (IndexError, ValueError):
        return

    admin_tg_id = update.effective_user.id

    try:
        with get_db_session() as session:
            user = session.query(User).filter_by(id=uid).first()
            if not user:
                await _safe_edit(query, "❌ User not found.", _back_to_profile_kb(uid))
                return

            tg_id = user.telegram_id

            # Anonymise PII instead of hard-deleting rows (preserves order history
            # integrity for accounting purposes while removing personal data).
            user.username          = None
            user.language          = "en"
            user.referred_by_id    = None
            user.referral_earnings = 0.0
            user.wallet_balance    = 0.0
            user.loyalty_points    = 0
            user.last_seen_at      = None
            user.is_banned         = True   # prevent re-use of the account slot
            session.commit()

        log_admin_action(
            admin_tg_id, "user.delete_data",
            target_type="user", target_id=uid,
            details=f"PII wiped for tg_id={tg_id}",
        )
    except Exception as exc:
        logger.error("up_delete_execute error uid=%s: %s", uid, exc, exc_info=True)
        await _safe_edit(
            query,
            f"❌ Delete failed ({type(exc).__name__}). No changes made.",
            _back_to_profile_kb(uid),
        )
        return

    await _safe_edit(
        query,
        f"✅ User data wiped for <code>{tg_id}</code>.\n\n"
        "Personal data has been anonymised and the account has been banned.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to List", callback_data="up:list:0:desc")
        ]]),
    )
