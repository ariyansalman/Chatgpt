"""Enterprise Admin Smart Search — aos: namespace.

Universal search accessible from the admin dashboard.  Auto-detects the
identifier type from the query string and routes to the correct record.

Supported query formats
-----------------------
• ORD-20260722-000001     — Order ID   → Order detail
• DEP-20260722-000001     — Deposit ID → Deposit (Transaction) detail
• 123456789  (5-15 digits)— Telegram User ID → most-recent order for that user
• @username / username    — Telegram username (exact then partial)
• any txid / proof string — payment transaction match → order
• Customer first/last name — partial name match → latest order

Search is read-only; admin actions (resend, refund, cancel, complete,
approve/reject deposit) delegate to the existing handlers via their
established callback_data so all existing business logic, audit logging,
and permission checks are preserved verbatim.

Callback namespace   : aos:
ConversationHandler  : state AWAITING_QUERY (int 0)
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    get_db_session,
    CouponRedemption,
    Coupon,
    DeliveryStatus,
    Order,
    OrderItem,
    OrderReceipt,
    OrderStatus,
    OrderStatusHistory,
    PaymentLifecycleStatus,
    Product,
    ReferralCommission,
    Transaction,
    TransactionStatus,
    User,
    WalletLedger,
)
from database.models import ProductType
from utils import format_price
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ─── Conversation state ───────────────────────────────────────────────────────
AWAITING_QUERY: int = 0

# ─── Regex helpers ────────────────────────────────────────────────────────────
# Order display format:   ORD-YYYYMMDD-NNNNNN
_ORD_RE      = re.compile(r"^ORD-(\d{8})-0*(\d+)$", re.IGNORECASE)
# Deposit display format: DEP-YYYYMMDD-NNNNNN
_DEP_RE      = re.compile(r"^DEP-(\d{8})-0*(\d+)$", re.IGNORECASE)
# Legacy RC- receipt format — backward compat only (no new receipts use this).
_RECEIPT_RE  = re.compile(r"^RC-\d{8}-\d+$", re.IGNORECASE)
_ORDER_ID_RE = re.compile(r"^#?(\d{1,9})$")       # up to 9 digits → order ID
_TG_UID_RE   = re.compile(r"^(\d{5,15})$")        # 5-15 digits    → Telegram UID

# ─────────────────────────────────────────────────────────────────────────────
# Entry / cancel
# ─────────────────────────────────────────────────────────────────────────────

async def aos_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the search-prompt screen and enter AWAITING_QUERY state."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    prompt = (
        "<b>🔍 Smart Search</b>\n\n"
        "Search using:\n\n"
        "• <code>ORD-20260722-000001</code> — Order ID\n"
        "• <code>DEP-20260722-000001</code> — Deposit ID\n"
        "• <code>123456789</code> — Telegram User ID\n"
        "• <code>@username</code> — Customer username\n"
        "• Transaction ID\n"
        "• Customer Name\n\n"
        "Enter your search query below."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Cancel", callback_data="aos:cancel"),
    ]])

    try:
        await query.edit_message_text(prompt, reply_markup=kb, parse_mode="HTML")
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise

    return AWAITING_QUERY


async def aos_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the search and return to the admin dashboard."""
    query = update.callback_query
    await query.answer()

    try:
        from handlers.admin_dashboard import render_legacy_dashboard
        await render_legacy_dashboard(update, context)
    except Exception:
        try:
            await query.edit_message_text(
                "✅ Search cancelled.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu"),
                ]]),
            )
        except BadRequest:
            pass

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Search logic
# ─────────────────────────────────────────────────────────────────────────────

def _find_order(
    raw: str,
    session,
) -> Tuple[Optional[Order], str]:
    """
    Auto-detect query type and resolve to an Order.

    Returns ``(Order, match_description)`` on success,
    or ``(None, human_readable_error)`` when nothing is found.
    """
    q = raw.strip()

    # ── 1. Formatted Order ID: ORD-YYYYMMDD-NNNNNN ───────────────────
    m_ord = _ORD_RE.match(q)
    if m_ord:
        oid = int(m_ord.group(2))
        order = session.query(Order).filter_by(id=oid).first()
        if order:
            return order, f"Order ID {q.upper()}"
        return None, f"No order found for <code>{q.upper()}</code>."

    # ── 2. Exact order ID: #17 or 17 ─────────────────────────────────
    m = _ORDER_ID_RE.match(q)
    if m:
        oid = int(m.group(1))
        order = session.query(Order).filter_by(id=oid).first()
        if order:
            return order, f"Order ID #{oid}"

    # ── 3. Legacy receipt number: RC-YYYYMMDD-NNNNNNN (backward compat)
    if _RECEIPT_RE.match(q):
        receipt = (
            session.query(OrderReceipt)
            .filter(OrderReceipt.receipt_number.ilike(q))
            .first()
        )
        if receipt:
            order = session.query(Order).filter_by(id=receipt.order_id).first()
            if order:
                return order, f"Receipt {q.upper()}"
        return None, f"No order found for receipt <code>{q}</code>."

    # ── 4. Telegram User ID (5-15 digits) ────────────────────────────
    if _TG_UID_RE.match(q):
        tg_id = int(q)
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if user:
            order = (
                session.query(Order)
                .filter_by(user_id=user.id)
                .order_by(Order.created_at.desc())
                .first()
            )
            if order:
                return order, f"Latest order for Telegram ID {tg_id}"

    # ── 5. Transaction txid (partial, no spaces) ─────────────────────
    if " " not in q and len(q) >= 6:
        txn = (
            session.query(Transaction)
            .filter(Transaction.txid.ilike(f"%{q}%"))
            .order_by(Transaction.created_at.desc())
            .first()
        )
        if txn:
            receipt = (
                session.query(OrderReceipt)
                .filter_by(transaction_id=txn.id)
                .first()
            )
            if receipt:
                order = session.query(Order).filter_by(id=receipt.order_id).first()
                if order:
                    return order, f"Transaction ID matching <code>{q}</code>"

        # Also search proof / external payment ref (admin notes, proof field)
        txn2 = (
            session.query(Transaction)
            .filter(Transaction.proof.ilike(f"%{q}%"))
            .order_by(Transaction.created_at.desc())
            .first()
        )
        if txn2:
            receipt = (
                session.query(OrderReceipt)
                .filter_by(transaction_id=txn2.id)
                .first()
            )
            if receipt:
                order = session.query(Order).filter_by(id=receipt.order_id).first()
                if order:
                    return order, f"Payment proof matching <code>{q}</code>"

    # ── 6. Customer first/last name (if the model has those columns) ─────
    if q and len(q) >= 2:
        try:
            from sqlalchemy import or_, func as _func
            name_conditions = []
            if hasattr(User, "first_name"):
                name_conditions.append(_func.lower(User.first_name).contains(q.lower()))
            if hasattr(User, "last_name"):
                name_conditions.append(_func.lower(User.last_name).contains(q.lower()))
            if hasattr(User, "full_name"):
                name_conditions.append(_func.lower(User.full_name).contains(q.lower()))
            if name_conditions:
                user = session.query(User).filter(or_(*name_conditions)).order_by(User.created_at.desc()).first()
                if user:
                    order = (
                        session.query(Order)
                        .filter_by(user_id=user.id)
                        .order_by(Order.created_at.desc())
                        .first()
                    )
                    if order:
                        display = (
                            f"@{user.username}" if user.username
                            else f"TG:{user.telegram_id}"
                        )
                        return order, f"Latest order for customer {display} (name match)"
        except Exception:
            pass  # name columns not present — fall through to username search

    # ── 7. @username or plain username (exact then partial) ───────────
    username_raw = q.lstrip("@")
    if username_raw:
        user = (
            session.query(User)
            .filter(User.username.ilike(username_raw))
            .first()
        )
        if not user:
            user = (
                session.query(User)
                .filter(User.username.ilike(f"%{username_raw}%"))
                .first()
            )
        if user:
            order = (
                session.query(Order)
                .filter_by(user_id=user.id)
                .order_by(Order.created_at.desc())
                .first()
            )
            if order:
                return order, f"Latest order for @{user.username or username_raw}"

    return None, (
        f"No order found for <code>{q}</code>.\n\n"
        "Try: ORD-YYYYMMDD-NNNNNN, Telegram User ID, @username, "
        "Transaction ID, or Customer Name."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detail rendering
# ─────────────────────────────────────────────────────────────────────────────

_ORDER_STATUS_EMOJI = {
    "PROCESSING": "⏳",
    "COMPLETED":  "✅",
    "CANCELLED":  "❌",
    "FAILED":     "❗",
    "REFUNDED":   "💸",
}
_DELIVERY_STATUS_EMOJI = {
    "PENDING":     "⏳",
    "DELIVERED":   "✅",
    "FAILED":      "❌",
    "REDELIVERED": "🔄",
}


def _render_order_detail(order: Order, session) -> str:  # noqa: C901
    """Build the full HTML order-detail string for display in Telegram."""
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━",
        "📦 <b>ORDER DETAILS</b>",
        "━━━━━━━━━━━━━━",
        "",
    ]

    # ── IDs and status ──────────────────────────────────────────────────
    order_status_name = order.status.value if order.status else "—"
    order_status_emoji = _ORDER_STATUS_EMOJI.get(order_status_name.upper(), "❓")
    pay_status_name    = order.payment_status.value if order.payment_status else "—"
    del_status_name    = order.delivery_status.value if order.delivery_status else "—"

    from utils.helpers import format_order_id as _fmt_oid
    display_order_id = _fmt_oid(order.id, order.created_at)

    lines += [
        f"🧾 <b>Order ID:</b> {display_order_id}",
        f"📊 <b>Order Status:</b> {order_status_emoji} {order_status_name}",
        f"💳 <b>Payment Status:</b> {pay_status_name}",
        "",
    ]

    # ── Customer ─────────────────────────────────────────────────────────
    user: Optional[User] = session.query(User).filter_by(id=order.user_id).first()
    if user:
        uname = f"@{user.username}" if user.username else "—"
        lines += [
            "👤 <b>Customer</b>",
            f"  • Username: {uname}",
            f"  • Telegram ID: <code>{user.telegram_id}</code>",
            "",
        ]
    else:
        lines += ["👤 <b>Customer:</b> Unknown (user deleted?)", ""]

    # ── Products ─────────────────────────────────────────────────────────
    items: list[OrderItem] = (
        session.query(OrderItem).filter_by(order_id=order.id).all()
    )
    total_delivered_assets = 0
    delivery_type = "—"

    lines.append("📦 <b>Product(s)</b>")
    for item in items:
        product: Optional[Product] = (
            session.query(Product).filter_by(id=item.product_id).first()
        )
        p_name = product.name if product else f"Product #{item.product_id}"
        p_type = (
            product.product_type.value
            if product and product.product_type
            else "—"
        )
        if product and product.product_type:
            delivery_type = product.product_type.value

        unit_total = (item.price or 0.0) * (item.quantity or 1)
        lines += [
            f"  • <b>{p_name}</b>",
            f"    ID: #{item.product_id}  |  Type: {p_type}",
            f"    Qty: {item.quantity}  |  Unit: {format_price(item.price)}  |  Total: {format_price(unit_total)}",
        ]
        if item.delivered_asset:
            asset_lines = [
                a.strip()
                for a in item.delivered_asset.splitlines()
                if a.strip()
            ]
            total_delivered_assets += len(asset_lines)
    lines.append("")

    # ── Payment ──────────────────────────────────────────────────────────
    receipt: Optional[OrderReceipt] = (
        session.query(OrderReceipt).filter_by(order_id=order.id).first()
    )
    txn: Optional[Transaction] = None
    if receipt and receipt.transaction_id:
        txn = session.query(Transaction).filter_by(id=receipt.transaction_id).first()

    if not txn and user:
        # Fallback: most-recent completed transaction for this user near order time
        txn = (
            session.query(Transaction)
            .filter_by(user_id=user.id)
            .filter(Transaction.status == TransactionStatus.COMPLETED)
            .order_by(Transaction.completed_at.desc())
            .first()
        )

    pay_method = "—"
    txid       = "—"
    paid_at    = "—"
    if txn:
        pay_method = (
            txn.payment_method.value.replace("_", " ").title()
            if txn.payment_method
            else "—"
        )
        txid    = txn.txid or txn.proof or "—"
        paid_at = (
            txn.completed_at.strftime("%Y-%m-%d %H:%M UTC")
            if txn.completed_at
            else "—"
        )

    lines += [
        "💰 <b>Payment</b>",
        f"  • Unit Price: {format_price(items[0].price) if items else '—'}",
        f"  • Total Amount: <b>{format_price(order.total_amount)}</b>",
        f"  • Currency: {order.currency or '—'}",
        f"  • Method: {pay_method}",
        f"  • Transaction ID: <code>{txid}</code>",
        f"  • Payment Time: {paid_at}",
        "",
    ]

    # ── Delivery ─────────────────────────────────────────────────────────
    del_emoji   = _DELIVERY_STATUS_EMOJI.get(del_status_name.upper().replace(" ", "_"), "❓")
    delivered_at = (
        order.completed_at.strftime("%Y-%m-%d %H:%M UTC")
        if order.completed_at
        else "—"
    )
    lines += [
        "🚚 <b>Delivery</b>",
        f"  • Status: {del_emoji} {del_status_name}",
        f"  • Type: {delivery_type}",
        f"  • Delivered At: {delivered_at}",
        f"  • Items Delivered: {total_delivered_assets}",
        "",
    ]

    # ── Delivery Content ──────────────────────────────────────────────────
    delivered_items = [it for it in items if it.delivered_asset]
    if delivered_items:
        lines.append("🔑 <b>Delivery Content</b>")
        for item in delivered_items:
            product = session.query(Product).filter_by(id=item.product_id).first()
            p_type  = product.product_type if product else None
            asset_lines = [
                a.strip()
                for a in item.delivered_asset.splitlines()
                if a.strip()
            ]
            # ProductType.ACCOUNT does not exist; ACCOUNT_LOGIN is the correct member
            icon = (
                "📄" if p_type in (ProductType.FILE, ProductType.DOWNLOADABLE_FILE)
                else "👤" if p_type == ProductType.ACCOUNT_LOGIN
                else "🔑"
            )
            for al in asset_lines[:15]:
                lines.append(f"  {icon} {al}")
            if len(asset_lines) > 15:
                lines.append(f"  … +{len(asset_lines) - 15} more items")
        lines.append("")

    # ── Coupons / Discounts ───────────────────────────────────────────────
    redemption: Optional[CouponRedemption] = (
        session.query(CouponRedemption)
        .filter_by(order_id=order.id)
        .first()
    )
    if redemption:
        coupon: Optional[Coupon] = (
            session.query(Coupon).filter_by(id=redemption.coupon_id).first()
        )
        lines += [
            "🎁 <b>Discounts</b>",
            f"  • Coupon Used: <code>{coupon.code if coupon else '—'}</code>",
            f"  • Discount Amount: {format_price(redemption.discount_applied)}",
            "",
        ]

    # ── Referral ──────────────────────────────────────────────────────────
    ref_commission: Optional[ReferralCommission] = (
        session.query(ReferralCommission)
        .filter_by(order_id=order.id)
        .first()
    )
    if ref_commission:
        referrer: Optional[User] = (
            session.query(User).filter_by(id=ref_commission.referrer_id).first()
        )
        ref_name = (
            f"@{referrer.username}"
            if referrer and referrer.username
            else f"ID#{ref_commission.referrer_id}"
        )
        lines += [
            "👥 <b>Referral</b>",
            f"  • Referrer: {ref_name}",
            f"  • Commission: {format_price(ref_commission.commission_amount)}",
            f"  • Rate: {ref_commission.commission_rate:.1%}",
            "",
        ]

    # ── Wallet snapshot ───────────────────────────────────────────────────
    if user:
        purchase_ledger: Optional[WalletLedger] = (
            session.query(WalletLedger)
            .filter_by(user_id=user.id, ref_type="order", ref_id=str(order.id))
            .order_by(WalletLedger.id.asc())
            .first()
        )
        if purchase_ledger and purchase_ledger.delta < 0:
            bal_before = purchase_ledger.balance_after - purchase_ledger.delta
            bal_after  = purchase_ledger.balance_after
            lines += [
                "📊 <b>Wallet</b>",
                f"  • Balance Before Purchase: {format_price(bal_before)}",
                f"  • Balance After Purchase: {format_price(bal_after)}",
                "",
            ]

    # ── Timeline ──────────────────────────────────────────────────────────
    timeline_written = False
    try:
        from services.order_lifecycle import render_timeline
        tl = render_timeline(order.id, limit=10)
        if tl and tl != "— no history yet —":
            lines += ["📝 <b>Timeline</b>", tl, ""]
            timeline_written = True
    except Exception:
        pass

    if not timeline_written:
        # Fallback: raw OrderStatusHistory
        history: list[OrderStatusHistory] = (
            session.query(OrderStatusHistory)
            .filter_by(order_id=order.id)
            .order_by(OrderStatusHistory.created_at.asc())
            .limit(10)
            .all()
        )
        if history:
            lines.append("📝 <b>Timeline</b>")
            for h in history:
                ts = (
                    h.created_at.strftime("%Y-%m-%d %H:%M")
                    if h.created_at
                    else "—"
                )
                from_s = h.from_status or "—"
                to_s   = h.to_status   or "—"
                lines.append(f"  [{ts}] {from_s} → {to_s}")
            lines.append("")

    lines.append(
        f"🕐 <b>Created At:</b> "
        f"{order.created_at.strftime('%Y-%m-%d %H:%M UTC') if order.created_at else '—'}"
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Keyboard builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_detail_keyboard(
    order: Order,
    items: list[OrderItem],
    user: Optional[User],
) -> InlineKeyboardMarkup:
    """Build the action keyboard for the order detail view."""
    oid = order.id
    rows: list[list[InlineKeyboardButton]] = []

    # ── Delivery actions ─────────────────────────────────────────────────
    if items:
        rows.append([
            InlineKeyboardButton(
                "🔄 Resend Delivery",
                callback_data=f"admin_redeliver_{oid}",
            ),
            InlineKeyboardButton(
                "♻ Replace Item",
                callback_data=f"admin_redeliver_{oid}",
            ),
        ])

    # ── Order lifecycle actions ───────────────────────────────────────────
    if order.status == OrderStatus.PROCESSING:
        rows.append([
            InlineKeyboardButton("✅ Complete", callback_data=f"complete_order_{oid}"),
            InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{oid}"),
        ])
    elif order.status == OrderStatus.CANCELLED:
        rows.append([
            InlineKeyboardButton("🔄 Reactivate Order", callback_data=f"reactivate_order_{oid}"),
        ])

    # ── Refund ───────────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("💵 Refund", callback_data="aref:menu"),
    ])

    # ── Copy order details ───────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("📋 Copy Order Details", callback_data=f"aos:copy:{oid}"),
    ])

    # ── Navigation: customer + product ───────────────────────────────────
    nav_row: list[InlineKeyboardButton] = []
    if user:
        nav_row.append(
            InlineKeyboardButton("👤 Open Customer", callback_data=f"usr:det:{user.id}")
        )
    if items:
        nav_row.append(
            InlineKeyboardButton(
                "📦 Open Product",
                callback_data=f"inv_prod_{items[0].product_id}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    # ── New Search + back ────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("🔍 New Search", callback_data="aos:menu"),
        InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu"),
    ])

    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Deposit (Transaction) search — DEP-YYYYMMDD-NNNNNN
# ─────────────────────────────────────────────────────────────────────────────

_TX_STATUS_EMOJI = {
    "PENDING":   "⏳",
    "COMPLETED": "✅",
    "FAILED":    "❌",
    "EXPIRED":   "⌛",
    "REVIEWING": "🔍",
    "CANCELLED": "❌",
}


def _find_deposit(
    raw: str,
    session,
) -> Tuple[Optional[Transaction], str]:
    """
    Resolve a DEP-YYYYMMDD-NNNNNN string to a Transaction row.

    Returns ``(Transaction, match_description)`` on success,
    or ``(None, human_readable_error)`` when nothing is found.
    Only called when the caller has already confirmed the input
    starts with the DEP- prefix.
    """
    q = raw.strip()
    m_dep = _DEP_RE.match(q)
    if not m_dep:
        return None, (
            "❌ Invalid Deposit ID format.\n\n"
            "Expected format:\n<code>DEP-YYYYMMDD-000001</code>"
        )

    tid = int(m_dep.group(2))
    txn = session.query(Transaction).filter_by(id=tid).first()
    if txn:
        return txn, f"Deposit ID {q.upper()}"
    return None, f"No deposit found for <code>{q.upper()}</code>."


def _render_deposit_detail(txn: Transaction, session) -> str:
    """Build the full HTML deposit-detail string for display in Telegram."""
    from services.payment_ui import format_deposit_id as _fmt_dep

    lines: list[str] = [
        "━━━━━━━━━━━━━━",
        "💰 <b>DEPOSIT DETAILS</b>",
        "━━━━━━━━━━━━━━",
        "",
    ]

    dep_id       = _fmt_dep(txn.id, txn.created_at)
    status_name  = txn.status.value if txn.status else "—"
    status_emoji = _TX_STATUS_EMOJI.get(status_name.upper(), "❓")

    lines += [
        f"🧾 <b>Deposit ID:</b> {dep_id}",
        f"📊 <b>Status:</b> {status_emoji} {status_name}",
        "",
    ]

    # ── Customer ─────────────────────────────────────────────────────────
    user: Optional[User] = session.query(User).filter_by(id=txn.user_id).first()
    if user:
        uname = f"@{user.username}" if user.username else "—"
        lines += [
            "👤 <b>Customer</b>",
            f"  • Username: {uname}",
            f"  • Telegram ID: <code>{user.telegram_id}</code>",
            "",
        ]
    else:
        lines += ["👤 <b>Customer:</b> Unknown (user deleted?)", ""]

    # ── Amount & payment method ───────────────────────────────────────────
    pay_method = (
        txn.payment_method.value.replace("_", " ").title()
        if txn.payment_method else "—"
    )
    txid_val = txn.txid or txn.proof or "—"

    lines += [
        "💳 <b>Payment</b>",
        f"  • Amount: <b>{format_price(txn.amount)}</b>",
        f"  • Method: {pay_method}",
    ]
    if txn.crypto_address:
        lines.append(f"  • Address: <code>{txn.crypto_address}</code>")
    lines += [
        f"  • Transaction ID: <code>{txid_val}</code>",
        "",
    ]

    # ── Admin note ────────────────────────────────────────────────────────
    if txn.admin_note:
        lines += [f"📝 <b>Admin Note:</b> {txn.admin_note}", ""]

    # ── Timestamps ────────────────────────────────────────────────────────
    created_str   = (
        txn.created_at.strftime("%Y-%m-%d %H:%M UTC")
        if txn.created_at else "—"
    )
    completed_str = (
        txn.completed_at.strftime("%Y-%m-%d %H:%M UTC")
        if txn.completed_at else "—"
    )
    lines += [
        f"🕐 <b>Created:</b> {created_str}",
        f"✅ <b>Completed:</b> {completed_str}",
    ]

    return "\n".join(lines)


def _build_deposit_keyboard(
    txn: Transaction,
    user: Optional[User],
) -> InlineKeyboardMarkup:
    """Build the action keyboard shown below a deposit detail card."""
    rows: list[list[InlineKeyboardButton]] = []

    if user:
        rows.append([
            InlineKeyboardButton(
                "👤 Open Customer",
                callback_data=f"usr:det:{user.id}",
            )
        ])

    rows.append([
        InlineKeyboardButton("🔍 New Search", callback_data="aos:menu"),
        InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu"),
    ])

    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Handler: process typed search query (MessageHandler)
# ─────────────────────────────────────────────────────────────────────────────

async def aos_handle_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Receive the admin's search text and display the matching order."""
    if not has_permission(update.effective_user.id, "manage_orders"):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text(
            "❓ Please send a non-empty search query.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚫 Cancel", callback_data="aos:cancel"),
            ]]),
        )
        return AWAITING_QUERY

    # ── Route by prefix ──────────────────────────────────────────────────
    is_deposit_query = bool(_DEP_RE.match(raw))

    try:
        with get_db_session() as session:

            if is_deposit_query:
                # ── DEP-YYYYMMDD-NNNNNN → Deposit detail ─────────────────
                txn, match_desc = _find_deposit(raw, session)

                if txn is None:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔍 Try Again", callback_data="aos:menu"),
                        InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu"),
                    ]])
                    await update.message.reply_text(
                        match_desc,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    return ConversationHandler.END

                detail_text = _render_deposit_detail(txn, session)
                dep_user    = session.query(User).filter_by(id=txn.user_id).first()
                kb          = _build_deposit_keyboard(txn, dep_user)

            else:
                # ── ORD- / Telegram UID / @username / txid / name → Order detail
                order, match_desc = _find_order(raw, session)

                if order is None:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔍 Try Again", callback_data="aos:menu"),
                        InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu"),
                    ]])
                    await update.message.reply_text(
                        f"❌ {match_desc}",
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    return ConversationHandler.END

                detail_text = _render_order_detail(order, session)
                items       = session.query(OrderItem).filter_by(order_id=order.id).all()
                user        = session.query(User).filter_by(id=order.user_id).first()
                kb          = _build_detail_keyboard(order, items, user)

    except Exception as _exc:
        logger.exception("aos: Error searching for query %r — %s: %s", raw, type(_exc).__name__, _exc)
        exc_name = type(_exc).__name__
        await update.message.reply_text(
            f"❌ Search failed ({exc_name}). The error has been logged. Please try again or contact the developer.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Try Again", callback_data="aos:menu"),
            ]]),
        )
        return ConversationHandler.END

    # Telegram message limit: 4 096 chars
    if len(detail_text) > 4000:
        detail_text = detail_text[:3980] + "\n\n…<i>(truncated)</i>"

    try:
        await update.message.reply_text(
            detail_text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("aos: Failed to send detail for query %r", raw)
        await update.message.reply_text("❌ Could not display record details.")

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Callback: aos:view:{order_id}  — show order detail via callback button
# ─────────────────────────────────────────────────────────────────────────────

async def aos_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show order detail page triggered by ``aos:view:{order_id}`` callback."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        order_id = int(query.data.split(":")[2])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return

    try:
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()
            if not order:
                await query.answer("❌ Order not found.", show_alert=True)
                return
            detail_text = _render_order_detail(order, session)
            items       = session.query(OrderItem).filter_by(order_id=order.id).all()
            user        = session.query(User).filter_by(id=order.user_id).first()
            kb          = _build_detail_keyboard(order, items, user)
    except Exception:
        logger.exception("aos: Error loading order #%s", order_id)
        await query.answer("❌ Internal error.", show_alert=True)
        return

    if len(detail_text) > 4000:
        detail_text = detail_text[:3980] + "\n\n…<i>(truncated)</i>"

    try:
        await query.edit_message_text(detail_text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Callback: aos:copy:{order_id}  — show a short plain-text summary popup
# ─────────────────────────────────────────────────────────────────────────────

async def aos_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a plain-text order summary in a Telegram popup for easy copying."""
    query = update.callback_query

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        order_id = int(query.data.split(":")[2])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid callback data.", show_alert=True)
        return

    try:
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()
            if not order:
                await query.answer("❌ Order not found.", show_alert=True)
                return

            receipt = (
                session.query(OrderReceipt)
                .filter_by(order_id=order.id)
                .first()
            )
            items   = session.query(OrderItem).filter_by(order_id=order.id).all()
            user    = session.query(User).filter_by(id=order.user_id).first()

            from utils.helpers import format_order_id as _fmt_oid2
            display_oid = _fmt_oid2(order.id, order.created_at)
            summary_lines = [
                f"Order {display_oid}",
                f"Status: {order.status.value if order.status else '—'}",
                f"Total: {format_price(order.total_amount)} {order.currency or ''}",
                f"Payment: {order.payment_status.value if order.payment_status else '—'}",
            ]
            if user:
                uname = f"@{user.username}" if user.username else f"TG:{user.telegram_id}"
                summary_lines.append(f"Customer: {uname}")
            for item in items[:3]:
                p = session.query(Product).filter_by(id=item.product_id).first()
                summary_lines.append(
                    f"• {p.name if p else '?'} x{item.quantity} = "
                    f"{format_price((item.price or 0) * item.quantity)}"
                )

            # Telegram popup limit: ~200 chars
            summary = "\n".join(summary_lines)[:195]
    except Exception:
        await query.answer("❌ Error loading order summary.", show_alert=True)
        return

    await query.answer(summary, show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# ConversationHandler factory
# ─────────────────────────────────────────────────────────────────────────────

def build_aos_conv() -> ConversationHandler:
    """Build and return the Order Search ConversationHandler.

    Register this BEFORE the broad catch-all callback handlers.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(aos_menu, pattern=r"^aos:menu$"),
        ],
        states={
            AWAITING_QUERY: [
                # Cancel button inside the prompt
                CallbackQueryHandler(aos_cancel, pattern=r"^aos:cancel$"),
                # Any text (not a command) is treated as the search query
                MessageHandler(filters.TEXT & ~filters.COMMAND, aos_handle_query),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(aos_cancel, pattern=r"^aos:cancel$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
        name="aos_search",
    )
