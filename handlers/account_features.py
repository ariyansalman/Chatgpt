"""User-facing Account & Order Features — V19.

Callback namespace: ua:*

Provides:
  🧾 ua:rec         — My Receipts (list)
  🧾 ua:rec:v:<id>  — View receipt inline
  📦 ua:orders      — Order Status list
  📦 ua:order:<id>  — Single order timeline
  📁 ua:dl          — Download Center
  📁 ua:dl:v:<id>   — View / re-access a download
  📜 ua:hist        — Activity History
  📜 ua:hist:p:<n>  — Paginated activity history
  🔒 ua:sec         — Security Center
  🔒 ua:sec:end:<n> — Terminate session
  ua:menu           — Account menu hub
  ua:noop           — No-op (page labels)

Public API used by other modules:
  log_activity(user_id_db, action, status, details, ref_type, ref_id)
  record_download(user_id_db, order_id, order_item_id, product_id, product_name, asset_type)
  create_receipt_record(order_id, transaction_id, user_id_db, receipt_type)
  ensure_session(telegram_id)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database import get_db_session, User, Order, OrderItem, Settings
from database.models import (
    OrderReceipt, UserDownload, ActivityLog, UserSession,
    OrderLifecycleStatus, OrderStatus, Transaction, TransactionStatus,
    OrderStatusHistory,
)
from utils.bot_config import cfg
from utils.helpers import check_user_banned
from i18n import get_user_language, t
from services import payment_ui as pui

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Status display helpers
# ─────────────────────────────────────────────────────────────────────────

_LIFECYCLE_DISPLAY = {
    "PENDING":          ("⏳", "Pending"),
    "AWAITING_PAYMENT": ("💳", "Waiting Payment"),
    "PAID":             ("✅", "Payment Received"),
    "PROCESSING":       ("🔍", "Processing / Verifying"),
    "DELIVERED":        ("📦", "Delivered"),
    "COMPLETED":        ("✅", "Completed"),
    "CANCELLED":        ("❌", "Cancelled"),
    "FAILED":           ("💔", "Failed"),
    "REFUNDED":         ("🔄", "Refunded"),
}

_LEGACY_DISPLAY = {
    "PROCESSING": ("🔍", "Processing"),
    "COMPLETED":  ("✅", "Completed"),
    "CANCELLED":  ("❌", "Cancelled"),
    "FAILED":     ("💔", "Failed"),
    "REFUNDED":   ("🔄", "Refunded"),
}


def _order_status_label(order) -> str:
    lc = order.lifecycle_status
    if lc:
        emoji, label = _LIFECYCLE_DISPLAY.get(lc.name, ("📋", lc.name))
    else:
        st = order.status
        st_name = st.name if st else "PROCESSING"
        emoji, label = _LEGACY_DISPLAY.get(st_name, ("📋", st_name))
    return f"{emoji} {label}"


# ─────────────────────────────────────────────────────────────────────────
# Feature gate helpers
# ─────────────────────────────────────────────────────────────────────────

def _feat(key: str, default: bool = True) -> bool:
    return cfg.get_bool(key, default)


def _has_any_account_feature() -> bool:
    return any(_feat(k) for k in [
        "feature_receipt_enabled",
        "feature_order_status_enabled",
        "feature_download_center_enabled",
        "feature_activity_history_enabled",
        "feature_security_center_enabled",
    ])


# ─────────────────────────────────────────────────────────────────────────
# Safe edit helper
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode,
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(label: str = "⬅️ Account", cb: str = "ua:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cb)]])


# ─────────────────────────────────────────────────────────────────────────
# Public API — called by other modules
# ─────────────────────────────────────────────────────────────────────────

def log_activity(
    user_id_db: int,
    action: str,
    status: str = "success",
    details: Optional[str] = None,
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
) -> None:
    """Append one row to activity_logs. Non-blocking best-effort."""
    if not _feat("feature_activity_history_enabled"):
        return
    try:
        max_hist = cfg.get_int("feature_activity_max", 100)
        with get_db_session() as s:
            s.add(ActivityLog(
                user_id=user_id_db,
                action=action,
                status=status,
                details=details,
                ref_type=ref_type,
                ref_id=str(ref_id) if ref_id is not None else None,
                created_at=datetime.utcnow(),
            ))
            # Enforce history cap — delete oldest rows beyond limit
            if max_hist > 0:
                oldest_allowed = (
                    s.query(ActivityLog.created_at)
                    .filter(ActivityLog.user_id == user_id_db)
                    .order_by(ActivityLog.created_at.desc())
                    .offset(max_hist - 1)
                    .limit(1)
                    .scalar()
                )
                if oldest_allowed:
                    s.query(ActivityLog).filter(
                        ActivityLog.user_id == user_id_db,
                        ActivityLog.created_at < oldest_allowed,
                    ).delete(synchronize_session=False)
    except Exception:
        logger.debug("log_activity failed", exc_info=True)


def record_download(
    user_id_db: int,
    order_id: int,
    order_item_id: int,
    product_id: int,
    product_name: str,
    asset_type: str = "key",
) -> None:
    """Upsert a UserDownload record.  Called from order_lifecycle on delivery."""
    if not _feat("feature_download_center_enabled"):
        return
    try:
        expiry_days = cfg.get_int("feature_download_expiry_days", 0)
        expires_at = None
        if expiry_days > 0:
            expires_at = datetime.utcnow() + timedelta(days=expiry_days)

        with get_db_session() as s:
            existing = (
                s.query(UserDownload)
                .filter_by(user_id=user_id_db, order_item_id=order_item_id)
                .first()
            )
            if not existing:
                s.add(UserDownload(
                    user_id=user_id_db,
                    order_id=order_id,
                    order_item_id=order_item_id,
                    product_id=product_id,
                    product_name=product_name[:255],
                    asset_type=asset_type,
                    download_count=0,
                    expires_at=expires_at,
                    created_at=datetime.utcnow(),
                ))
    except Exception:
        logger.debug("record_download failed", exc_info=True)


def create_receipt_record(
    order_id: Optional[int],
    transaction_id: Optional[int],
    user_id_db: int,
    receipt_type: str = "purchase",
) -> Optional[str]:
    """Create an OrderReceipt row and return the receipt number.

    Idempotent — if a receipt for this order already exists, returns the
    existing receipt_number without inserting a duplicate row.
    """
    if not _feat("feature_receipt_enabled"):
        return None
    try:
        with get_db_session() as s:
            # Idempotency: check existing receipt for this order
            if order_id is not None:
                existing = (
                    s.query(OrderReceipt)
                    .filter_by(order_id=order_id)
                    .first()
                )
                if existing:
                    return existing.receipt_number

            # Generate receipt number
            now = datetime.utcnow()
            date_str = now.strftime("%Y%m%d")
            ref_id = order_id or transaction_id or 0
            prefix = "RCP" if receipt_type == "purchase" else "DEP"
            receipt_number = f"{prefix}-{date_str}-{ref_id:05d}"

            # Ensure uniqueness with a suffix if needed
            base = receipt_number
            suffix = 0
            while s.query(OrderReceipt).filter_by(receipt_number=receipt_number).first():
                suffix += 1
                receipt_number = f"{base}-{suffix}"

            s.add(OrderReceipt(
                receipt_number=receipt_number,
                order_id=order_id,
                transaction_id=transaction_id,
                user_id=user_id_db,
                receipt_type=receipt_type,
                created_at=now,
            ))
            return receipt_number
    except Exception:
        logger.debug("create_receipt_record failed", exc_info=True)
        return None


def ensure_session(telegram_id: int, device_info: str = "Telegram") -> None:
    """Create or refresh a UserSession for this user.

    Creates a new session if none is active, otherwise just updates
    last_active_at.  Called from bot.py _track_activity.
    """
    if not _feat("feature_security_center_enabled"):
        return
    try:
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=telegram_id).first()
            if not user:
                return
            session = (
                s.query(UserSession)
                .filter_by(user_id=user.id, is_active=True)
                .first()
            )
            now = datetime.utcnow()
            if session:
                session.last_active_at = now
            else:
                # Token = deterministic hash of (user_id + timestamp_minute)
                raw = f"{user.id}-{now.strftime('%Y%m%d%H%M')}"
                token = hashlib.sha1(raw.encode()).hexdigest()[:32]
                s.add(UserSession(
                    user_id=user.id,
                    session_token=token,
                    device_info=device_info,
                    is_active=True,
                    created_at=now,
                    last_active_at=now,
                ))
    except Exception:
        logger.debug("ensure_session failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────
# Account Menu Hub
# ─────────────────────────────────────────────────────────────────────────

async def account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy entry point for the old "Account Center" hub.

    The hub (Wallet / Order History / Language / Wishlist / Favorites /
    My Deliveries / Receipts / Downloads / Activity / Login History /
    Security / Main Menu — all in one menu) has been removed: those items
    either already live on the Main Menu (Wallet, Order History, Language)
    or are reachable from their own appropriate pages (e.g. Receipts and
    Downloads from Order Details). This callback (``ua:menu``) is kept only
    so old inline keyboards / bookmarks still resolve instead of erroring —
    it now simply shows the compact Profile page.
    """
    await user_profile(update, context)


async def user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """👤 Account — profile summary + navigation hub (V22 redesign).

    Shows profile info at the top followed by a full account navigation
    menu. callback_data values are unchanged for backward compatibility.
    """
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    tg_user = update.effective_user

    if check_user_banned(user_id):
        if query:
            await query.answer("🚫 You are banned.", show_alert=True)
        return

    with get_db_session() as s:
        db_user = s.query(User).filter_by(telegram_id=user_id).first()
        if not db_user:
            if query:
                await query.answer("❌ User not found.", show_alert=True)
            return

        order_count = s.query(Order).filter_by(user_id=db_user.id).count()
        referral_count = s.query(User).filter_by(referred_by_id=db_user.id).count()
        wallet_balance = db_user.wallet_balance
        joined = db_user.created_at.strftime("%d %b %Y") if getattr(db_user, "created_at", None) else "—"
        full_name = tg_user.full_name if tg_user and tg_user.full_name else "—"

        # Membership / VIP tier (read-only lookup; falls back gracefully if
        # the VIP module isn't configured for this store)
        tier_label = "Standard"
        try:
            from services.vip_service import get_user_tier, vip_enabled
            if vip_enabled():
                tier = get_user_tier(s, db_user.id)
                if tier:
                    tier_label = f"{tier.emoji} {tier.name}"
        except Exception:
            pass

    import html as _html
    from utils.helpers import format_price

    lines = [
        f"👤 <b>{_html.escape(full_name)}</b>",
        f"🆔 User ID: <code>{user_id}</code>",
        f"💰 Wallet Balance: <code>{format_price(wallet_balance)}</code>",
        f"📦 Total Orders: {order_count}",
        f"👥 Referrals: {referral_count}",
        f"⭐ Membership: {tier_label}",
        f"📅 Joined: {joined}",
    ]
    text = "\n".join(lines)

    lang = get_user_language(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 My Profile", callback_data="ua:noop"),
         InlineKeyboardButton("💳 Wallet",      callback_data="wallet")],
        [InlineKeyboardButton("📦 My Orders",    callback_data="order_history"),
         InlineKeyboardButton("🔑 Purchased Keys", callback_data="ua:dl")],
        [InlineKeyboardButton("❤️ Wishlist",     callback_data="uf:wl"),
         InlineKeyboardButton("👥 Referral",     callback_data="refer")],
        [InlineKeyboardButton(t("language.menu_button", lang), callback_data="language_menu"),
         InlineKeyboardButton("⚙️ Settings",     callback_data="ua:sec")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")],
    ])

    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────
# 1. AUTOMATIC RECEIPT
# ─────────────────────────────────────────────────────────────────────────

async def receipts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🧾 My Receipts — list of the user's receipt records."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_receipt_enabled"):
        await query.answer("🧾 Receipts are currently disabled.", show_alert=True)
        return

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await _safe_edit(query, "❌ User not found.", _back_kb("⬅️ Back to Menu", "main_menu"))
            return

        receipts = (
            s.query(OrderReceipt)
            .filter_by(user_id=user.id)
            .order_by(OrderReceipt.created_at.desc())
            .limit(20)
            .all()
        )

        if not receipts:
            await _safe_edit(
                query,
                "🧾 <b>My Receipts</b>\n\nNo receipts yet. They appear here after purchases and deposits.",
                _back_kb(),
            )
            return

        rows: List[List[InlineKeyboardButton]] = []
        lines = ["🧾 <b>My Receipts</b>", ""]
        for r in receipts:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            icon = "🛍" if r.receipt_type == "purchase" else "💰"
            lines.append(f"{icon} {pui.copy_code(r.receipt_number)} · <i>{when}</i>")
            rows.append([InlineKeyboardButton(
                f"{icon} {r.receipt_number}",
                callback_data=f"ua:rec:v:{r.id}",
            )])

        rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])
        await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def view_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View a single receipt inline."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_receipt_enabled"):
        await query.answer("🧾 Receipts are currently disabled.", show_alert=True)
        return

    try:
        receipt_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    user_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        r = s.query(OrderReceipt).filter_by(id=receipt_id, user_id=user.id).first()
        if not r:
            await query.answer("❌ Receipt not found.", show_alert=True)
            return

        # Build receipt header/footer from admin config
        header = cfg.get_str("feature_receipt_header", "").strip()
        footer = cfg.get_str("feature_receipt_footer", "Thank you for your purchase!").strip()

        # Fetch store name
        store_settings = s.query(Settings).first()
        store_name = "Digital Store"
        if store_settings and store_settings.welcome_message:
            store_name = store_settings.welcome_message.strip().splitlines()[0][:60]

        when = r.created_at.strftime("%Y-%m-%d %H:%M UTC") if r.created_at else "?"
        customer = pui.customer_display(user.username, user.telegram_id)

        fields = [("🧾", "Receipt ID", r.receipt_number), ("📅", "Date", when), ("👤", "Customer", customer)]
        title = "Purchase Receipt"
        title_emoji = "🛍"

        if r.receipt_type == "purchase" and r.order_id:
            order = s.query(Order).filter_by(id=r.order_id).first()
            if order:
                items = s.query(OrderItem).filter_by(order_id=order.id).all()
                st_label = _order_status_label(order)
                from utils.helpers import format_order_id as _fmt_oid_rec
                _disp_rec = _fmt_oid_rec(order.id, order.created_at)
                fields.append(("📦", "Order", f"{_disp_rec}  {st_label}"))
                if items:
                    item_list = ", ".join(
                        f"{(item.product.name if item.product else f'Product #{item.product_id}')} ×{item.quantity}"
                        for item in items
                    )
                    fields.append(("🎁", "Items", item_list))
                fields.append(("💰", "Amount Paid", f"${order.total_amount:.2f}"))
                fields.append(("💳", "Payment", "Wallet"))

        elif r.receipt_type == "deposit" and r.transaction_id:
            title = "Deposit Receipt"
            title_emoji = "💰"
            txn = s.query(Transaction).filter_by(id=r.transaction_id).first()
            if txn:
                pm_label = pui.gateway_meta(txn.payment_method.value if txn.payment_method else None)[0]
                fields.append(("💳", "Payment Method", pm_label))
                fields.append(("💰", "Amount Credited", f"${txn.amount:.2f}"))
                if txn.txid:
                    fields.append(("🔗", "Transaction ID", pui.copy_code(txn.txid[:40])))

        note_lines = []
        if header:
            note_lines.append(f"<i>{header}</i>")
        note_lines.append(f"🏪 {store_name}")
        if footer:
            note_lines.append(f"<i>{footer}</i>")

        text = pui.build_card(
            title=title,
            title_emoji=title_emoji,
            fields=fields,
            status_key="approved",
            note="\n".join(note_lines),
        )

        # Also offer PDF download if order receipt
        kb_rows = []
        if r.receipt_type == "purchase" and r.order_id:
            kb_rows.append([InlineKeyboardButton(
                "📄 Download PDF", callback_data=f"receipt_{r.order_id}"
            )])
        kb_rows.append([InlineKeyboardButton("⬅️ Receipts", callback_data="ua:rec")])

        await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────
# 2. ORDER STATUS SYSTEM
# ─────────────────────────────────────────────────────────────────────────

_PAGE_SIZE = 5


async def order_status_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📦 Order Status — paginated list of user's orders."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_order_status_enabled"):
        await query.answer("📦 Order Status is currently disabled.", show_alert=True)
        return

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    # Parse page from callback data: ua:orders or ua:orders:p:<n>
    parts = query.data.split(":")
    page = 0
    if len(parts) >= 4 and parts[2] == "p":
        try:
            page = int(parts[3])
        except ValueError:
            page = 0

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        total = s.query(Order).filter_by(user_id=user.id).count()
        orders = (
            s.query(Order)
            .filter_by(user_id=user.id)
            .order_by(Order.created_at.desc())
            .offset(page * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
            .all()
        )

        if not orders and page == 0:
            await _safe_edit(
                query,
                "📦 <b>Order Status</b>\n\nNo orders yet.",
                _back_kb(),
            )
            return

        text = f"📦 <b>My Orders</b>  <i>({total} total)</i>\n\n"
        rows: List[List[InlineKeyboardButton]] = []

        for order in orders:
            when = order.created_at.strftime("%d %b %Y") if order.created_at else "?"
            st = _order_status_label(order)
            items_list = s.query(OrderItem).filter_by(order_id=order.id).limit(1).all()
            pname = ""
            if items_list and items_list[0].product:
                pname = f"\n🎁 {items_list[0].product.name}"
            text += f"🆔 <code>#{order.id}</code>{pname}\n💰 <code>${order.total_amount:.2f}</code>  {st} • 📅 {when}\n\n"
            rows.append([InlineKeyboardButton(
                f"📋 View Order #{order.id}",
                callback_data=f"ua:order:{order.id}",
            )])

        # Pagination
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ua:orders:p:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ua:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ua:orders:p:{page + 1}"))
        if len(nav) > 1:
            rows.append(nav)

        rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])
        await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def order_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📦 Single order — full status timeline view."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_order_status_enabled"):
        await query.answer("📦 Order Status is currently disabled.", show_alert=True)
        return

    try:
        order_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    user_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return
        order = s.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            await query.answer("❌ Order not found.", show_alert=True)
            return

        items = s.query(OrderItem).filter_by(order_id=order.id).all()
        history = (
            s.query(OrderStatusHistory)
            .filter_by(order_id=order.id)
            .order_by(OrderStatusHistory.created_at.asc())
            .limit(20)
            .all()
        )

        st = _order_status_label(order)
        when = order.created_at.strftime("%d %b %Y") if order.created_at else "?"

        from utils.helpers import format_order_id as _fmt_oid_st
        _disp_st = _fmt_oid_st(order.id, order.created_at)
        lines = [
            f"🧾 <b>Order ID</b>\n{_disp_st}",
            f"{st}",
            f"📅 {when}",
        ]

        # Items
        if items:
            for item in items:
                pname = item.product.name if item.product else f"Product #{item.product_id}"
                lines.append(f"🎁 {pname} ×{item.quantity}")
            lines.append(f"💰 <code>${order.total_amount:.2f}</code>")

        lines.append("💳 Wallet")

        # Status timeline — only for in-flight (processing) orders
        from database.models import OrderStatus as _OS
        if order.status == _OS.PROCESSING and history:
            lines.append("📜 <b>Timeline</b>")
            for h in history[-5:]:
                ts = h.created_at.strftime("%d %b %H:%M") if h.created_at else "?"
                to_s = h.to_status or "?"
                emoji = _LIFECYCLE_DISPLAY.get(to_s, ("📋", to_s))[0]
                label = _LIFECYCLE_DISPLAY.get(to_s, ("📋", to_s))[1]
                note = f" — {h.reason}" if h.reason else ""
                lines.append(f"  {emoji} {ts}  {label}{note}")

        text = "\n".join(lines)

        kb = [
            [InlineKeyboardButton("📋 All Orders", callback_data="ua:orders")],
            [InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")],
        ]
        # If there's a receipt for this order, link it
        receipt = s.query(OrderReceipt).filter_by(order_id=order.id).first()
        if receipt and _feat("feature_receipt_enabled"):
            kb.insert(0, [InlineKeyboardButton(
                f"🧾 Order ID: {receipt.receipt_number}",
                callback_data=f"ua:rec:v:{receipt.id}",
            )])

        await _safe_edit(query, text, InlineKeyboardMarkup(kb))

        # Log the view
        log_activity(user.id, "order_viewed", ref_type="order", ref_id=str(order_id))


# ─────────────────────────────────────────────────────────────────────────
# 3. DOWNLOAD CENTER
# ─────────────────────────────────────────────────────────────────────────

async def download_center(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📁 Download Center — list all user's delivered assets."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_download_center_enabled"):
        await query.answer("📁 Download Center is currently disabled.", show_alert=True)
        return

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    # Parse page: ua:dl or ua:dl:p:<n>
    parts = query.data.split(":")
    page = 0
    if len(parts) >= 4 and parts[2] == "p":
        try:
            page = int(parts[3])
        except ValueError:
            page = 0

    max_dl = cfg.get_int("feature_download_max", 0)
    expiry_days = cfg.get_int("feature_download_expiry_days", 0)
    now = datetime.utcnow()

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        q = s.query(UserDownload).filter_by(user_id=user.id)
        # Filter expired
        if expiry_days > 0:
            q = q.filter(
                (UserDownload.expires_at.is_(None)) |
                (UserDownload.expires_at > now)
            )
        total = q.count()
        downloads = (
            q.order_by(UserDownload.created_at.desc())
            .offset(page * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
            .all()
        )

        if not downloads and page == 0:
            await _safe_edit(
                query,
                "📁 <b>Download Center</b>\n\nNo downloads yet. Purchased products will appear here.",
                _back_kb(),
            )
            return

        text = f"📁 <b>Download Center</b>  <i>({total} item{'s' if total != 1 else ''})</i>\n\n"
        rows: List[List[InlineKeyboardButton]] = []

        for dl in downloads:
            when = dl.created_at.strftime("%b %d, %Y") if dl.created_at else "?"
            expired = dl.expires_at and dl.expires_at < now
            cnt_str = f" ↓{dl.download_count}" if dl.download_count else ""
            exp_str = " 🔴 Expired" if expired else ""
            icon = "🔑" if dl.asset_type == "key" else "📁" if dl.asset_type in ("file", "downloadable_file") else "📧" if dl.asset_type == "account_login" else "🎟"
            text += f"{icon} <b>{dl.product_name[:35]}</b>{cnt_str}{exp_str}\n<i>{when}</i>\n\n"
            if not expired:
                # Check max download limit
                if max_dl > 0 and dl.download_count >= max_dl:
                    rows.append([InlineKeyboardButton(
                        f"🔒 {dl.product_name[:30]} (limit reached)",
                        callback_data="ua:noop",
                    )])
                else:
                    rows.append([InlineKeyboardButton(
                        f"{icon} {dl.product_name[:35]}",
                        callback_data=f"ua:dl:v:{dl.id}",
                    )])
            else:
                rows.append([InlineKeyboardButton(
                    f"🔴 {dl.product_name[:30]} (expired)",
                    callback_data="ua:noop",
                )])

        # Pagination
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ua:dl:p:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ua:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ua:dl:p:{page + 1}"))
        if len(nav) > 1:
            rows.append(nav)

        rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])
        await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def view_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View / access a specific download."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_download_center_enabled"):
        await query.answer("📁 Download Center is currently disabled.", show_alert=True)
        return

    try:
        dl_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    user_id = update.effective_user.id
    now = datetime.utcnow()
    max_dl = cfg.get_int("feature_download_max", 0)

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        dl = s.query(UserDownload).filter_by(id=dl_id, user_id=user.id).first()
        if not dl:
            await query.answer("❌ Download not found.", show_alert=True)
            return

        # Check expiry
        if dl.expires_at and dl.expires_at < now:
            await query.answer("🔴 This download has expired.", show_alert=True)
            return

        # Check max downloads
        if max_dl > 0 and dl.download_count >= max_dl:
            await query.answer(
                f"🔒 Download limit reached ({max_dl} downloads allowed).",
                show_alert=True,
            )
            return

        # Fetch the actual OrderItem content
        order_item = s.query(OrderItem).filter_by(id=dl.order_item_id).first()
        if not order_item:
            await query.answer("❌ Order item not found.", show_alert=True)
            return

        asset = order_item.delivered_asset or ""

        # Update counter
        dl.download_count = (dl.download_count or 0) + 1
        dl.last_downloaded_at = now

        icon = "🔑" if dl.asset_type == "key" else "📁" if dl.asset_type in ("file", "downloadable_file") else "📧" if dl.asset_type == "account_login" else "🎟"
        type_label = {
            "key": "License Key(s)",
            "account_login": "Account Credentials",
            "redeem_link": "Redeem Link",
            "file": "File Download",
            "downloadable_file": "Downloadable File",
            "voucher": "Voucher Code",
            "subscription": "Subscription",
        }.get(dl.asset_type, "Content")

        when = dl.created_at.strftime("%Y-%m-%d %H:%M UTC") if dl.created_at else "?"
        cnt = dl.download_count

        dl_count_str = f"{cnt}/{max_dl}" if max_dl > 0 else str(cnt)
        text = (
            f"📁 <b>Download Center</b>\n"
            f"{icon} <b>{dl.product_name}</b>\n"
            f"📋 {type_label}\n"
            f"📅 {when}\n"
            f"↓ {dl_count_str} downloads\n\n"
        )

        if asset:
            # Show content — truncate if too long for Telegram
            if len(asset) > 3000:
                preview = asset[:2900]
                text += f"<code>{preview}</code>\n\n<i>… (content truncated — {len(asset)} chars total)</i>"
            else:
                text += f"<code>{asset}</code>"
        else:
            text += "<i>No content available for this item.</i>"

        kb = [
            [InlineKeyboardButton("📁 All Downloads", callback_data="ua:dl")],
            [InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))

        # Log the download access
        log_activity(user.id, "download", ref_type="order_item", ref_id=str(dl.order_item_id),
                     details=dl.product_name[:100])


# ─────────────────────────────────────────────────────────────────────────
# 4. ACTIVITY HISTORY
# ─────────────────────────────────────────────────────────────────────────

_ACTION_ICONS = {
    "login":          "🔐",
    "deposit":        "💰",
    "purchase":       "🛒",
    "refund":         "🔄",
    "coupon_used":    "🎟",
    "referral_bonus": "👥",
    "wallet_credit":  "➕",
    "wallet_debit":   "➖",
    "profile_changed":"✏️",
    "ticket_opened":  "🎫",
    "ticket_closed":  "✅",
    "ticket_replied": "💬",
    "download":       "📁",
    "order_viewed":   "📦",
}

_STATUS_ICONS = {"success": "✅", "failed": "❌", "pending": "⏳"}


async def activity_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📜 Activity History — paginated user activity log."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_activity_history_enabled"):
        await query.answer("📜 Activity History is currently disabled.", show_alert=True)
        return

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    # Parse page: ua:hist or ua:hist:p:<n>
    parts = query.data.split(":")
    page = 0
    if len(parts) >= 4 and parts[2] == "p":
        try:
            page = int(parts[3])
        except ValueError:
            page = 0

    _PER_PAGE = 10

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        total = s.query(ActivityLog).filter_by(user_id=user.id).count()
        rows_db = (
            s.query(ActivityLog)
            .filter_by(user_id=user.id)
            .order_by(ActivityLog.created_at.desc())
            .offset(page * _PER_PAGE)
            .limit(_PER_PAGE)
            .all()
        )

        if not rows_db and page == 0:
            await _safe_edit(
                query,
                "📜 <b>Activity History</b>\n\nNo activity recorded yet.",
                _back_kb(),
            )
            return

        text = f"📜 <b>Activity History</b>  <i>({total} events)</i>\n\n"
        for row in rows_db:
            ts = row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "?"
            icon = _ACTION_ICONS.get(row.action, "📋")
            st_icon = _STATUS_ICONS.get(row.status, "")
            label = row.action.replace("_", " ").title()
            detail = f" — {row.details}" if row.details else ""
            text += f"{icon} <code>{ts}</code>  {st_icon} <b>{label}</b>{detail}\n"

        total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
        nav_btns: List[InlineKeyboardButton] = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ua:hist:p:{page - 1}"))
        nav_btns.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ua:noop"))
        if page < total_pages - 1:
            nav_btns.append(InlineKeyboardButton("Next ▶️", callback_data=f"ua:hist:p:{page + 1}"))

        kb_rows: List[List[InlineKeyboardButton]] = []
        if len(nav_btns) > 1:
            kb_rows.append(nav_btns)
        kb_rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])

        await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─────────────────────────────────────────────────────────────────────────
# 5. SECURITY CENTER
# ─────────────────────────────────────────────────────────────────────────

async def security_center(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔒 Security Center — last login, sessions, quick stats."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_security_center_enabled"):
        await query.answer("🔒 Security Center is currently disabled.", show_alert=True)
        return

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    now = datetime.utcnow()

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        # Last activity from activity_logs
        last_login_row = (
            s.query(ActivityLog)
            .filter_by(user_id=user.id, action="login")
            .order_by(ActivityLog.created_at.desc())
            .first()
        )
        last_purchase_row = (
            s.query(ActivityLog)
            .filter_by(user_id=user.id, action="purchase")
            .order_by(ActivityLog.created_at.desc())
            .first()
        )
        last_deposit_row = (
            s.query(ActivityLog)
            .filter_by(user_id=user.id, action="deposit")
            .order_by(ActivityLog.created_at.desc())
            .first()
        )

        def _fmt(dt):
            if not dt:
                return "Never"
            return dt.strftime("%Y-%m-%d %H:%M UTC")

        last_login = _fmt(last_login_row.created_at if last_login_row else None)
        last_purchase = _fmt(last_purchase_row.created_at if last_purchase_row else None)
        last_deposit = _fmt(last_deposit_row.created_at if last_deposit_row else None)
        last_seen = _fmt(user.last_seen_at)

        # Sessions
        sessions = (
            s.query(UserSession)
            .filter_by(user_id=user.id)
            .order_by(UserSession.created_at.desc())
            .limit(5)
            .all()
        )
        active_sessions = [ses for ses in sessions if ses.is_active]

        # Check session timeout setting
        timeout_hours = cfg.get_int("feature_session_timeout_hours", 0)
        if timeout_hours > 0:
            cutoff = now - timedelta(hours=timeout_hours)
            for ses in active_sessions:
                if ses.last_active_at and ses.last_active_at < cutoff:
                    ses.is_active = False
                    ses.terminated_at = now
            active_sessions = [ses for ses in active_sessions if ses.is_active]

        text = (
            "🔒 <b>Security Center</b>\n\n"
            f"🔐 Last Login: <code>{last_login}</code>\n"
            f"🛒 Last Purchase: <code>{last_purchase}</code>\n"
            f"💰 Last Deposit: <code>{last_deposit}</code>\n"
            f"⏱ Last Active: <code>{last_seen}</code>\n\n"
            f"🔑 Active Sessions: <b>{len(active_sessions)}</b>\n"
        )

        kb_rows: List[List[InlineKeyboardButton]] = []
        for i, ses in enumerate(sessions[:5]):
            sess_when = ses.created_at.strftime("%b %d %H:%M") if ses.created_at else "?"
            last_act = ses.last_active_at.strftime("%b %d %H:%M") if ses.last_active_at else "?"
            active_label = "🟢 Active" if ses.is_active else "🔴 Ended"
            text += f"\n  {active_label} — Started {sess_when}, last active {last_act}"
            if ses.is_active and len(active_sessions) > 1:
                kb_rows.append([InlineKeyboardButton(
                    f"🔴 Terminate Session (started {sess_when})",
                    callback_data=f"ua:sec:end:{ses.id}",
                )])

        text += "\n"

        # ── V32: Last login from LoginRecord ─────────────────────────────────
        try:
            from services.login_activity import get_last_login as _gll, get_user_devices as _gdev
            if cfg.get_bool("lam_track_history", True):
                last_lr = _gll(user.id)
                if last_lr:
                    lr_when = _fmt(last_lr.get("created_at"))
                    lr_loc  = last_lr.get("language_code") or "N/A"
                    lr_ip   = last_lr.get("ip_address") or "N/A"
                    text += (
                        f"\n🔑 Last Login: <code>{lr_when}</code>\n"
                        f"   🌐 Locale: <code>{lr_loc}</code>\n"
                    )
                    if last_lr.get("is_suspicious"):
                        text += "   ⚠️ <i>Flagged as suspicious</i>\n"
                    elif last_lr.get("is_new_device"):
                        text += "   📱 <i>New device detected</i>\n"
        except Exception:
            pass

        # Security alerts toggle hint
        if cfg.get_bool("feature_new_login_notification", False):
            text += "\n🔔 <i>New login notifications: ON</i>"
        if cfg.get_bool("feature_security_alerts", True):
            text += "\n🚨 <i>Security alerts: ON</i>"
        if cfg.get_bool("lam_notify_new_login", True):
            text += "\n🔔 <i>Login activity alerts: ON</i>"

        if len(active_sessions) > 1:
            kb_rows.append([InlineKeyboardButton(
                "🔴 Log Out All Other Sessions",
                callback_data="ua:sec:endall",
            )])
        kb_rows.append([InlineKeyboardButton("📜 Login History", callback_data="ua:sec:lh:0")])
        kb_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="ua:sec")])
        kb_rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])

        await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


async def login_history_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📜 Login History — paginated list of login events for the user."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_security_center_enabled"):
        await query.answer("🔒 Security Center is currently disabled.", show_alert=True)
        return

    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        return

    _PAGE = 8
    db_user_id: Optional[int] = None

    with get_db_session() as s:
        _u = s.query(User).filter_by(telegram_id=user_id).first()
        if not _u:
            return
        db_user_id = _u.id

    try:
        from services.login_activity import get_login_history, get_login_history_count
        total = get_login_history_count(db_user_id)
        rows  = get_login_history(db_user_id, limit=_PAGE, offset=page * _PAGE)
    except Exception:
        total = 0
        rows  = []

    total_pages = max(1, (total + _PAGE - 1) // _PAGE)

    text = (
        f"📜 <b>Login History</b>  "
        f"<i>({total} records, page {page + 1}/{total_pages})</i>\n\n"
    )

    if not rows:
        text += "<i>No login records found yet.</i>"
    else:
        for r in rows:
            when  = r["created_at"].strftime("%Y-%m-%d %H:%M") if r.get("created_at") else "N/A"
            loc   = r.get("language_code") or "N/A"
            ip    = r.get("ip_address") or "N/A"
            sus  = "⚠️" if r.get("is_suspicious") else ("📱" if r.get("is_new_device") else "🔔")
            text += f"{sus} <code>{when}</code>  lang:{loc}  IP:{ip}\n"

    kb_rows: List[List[InlineKeyboardButton]] = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ua:sec:lh:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ua:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ua:sec:lh:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)

    kb_rows.append([InlineKeyboardButton("🔒 Security Center", callback_data="ua:sec")])
    kb_rows.append([InlineKeyboardButton("⬅️ Account", callback_data="ua:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


async def terminate_all_other_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔴 Log out all sessions except the current one."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_security_center_enabled"):
        return

    user_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        # Keep the most recently active session — that is the user's current one
        sessions = (
            s.query(UserSession)
            .filter_by(user_id=user.id, is_active=True)
            .order_by(UserSession.last_active_at.desc())
            .all()
        )
        if not sessions:
            await query.answer("No active sessions to terminate.", show_alert=True)
            return

        current_id = sessions[0].id
        terminated = 0
        for ses in sessions[1:]:
            ses.is_active    = False
            ses.terminated_at = datetime.utcnow()
            terminated += 1

        log_activity(user.id, "profile_changed",
                     details=f"Logged out {terminated} other session(s)")

    await query.answer(f"✅ {terminated} session(s) terminated.", show_alert=False)
    await security_center(update, context)


async def terminate_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔴 Terminate a specific session."""
    query = update.callback_query
    await query.answer()

    if not _feat("feature_security_center_enabled"):
        return

    try:
        session_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    user_id = update.effective_user.id

    with get_db_session() as s:
        user = s.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return

        ses = s.query(UserSession).filter_by(id=session_id, user_id=user.id).first()
        if not ses:
            await query.answer("❌ Session not found.", show_alert=True)
            return

        ses.is_active = False
        ses.terminated_at = datetime.utcnow()

        log_activity(user.id, "profile_changed", details="Session terminated by user")

    await query.answer("✅ Session terminated.", show_alert=False)
    # Refresh security center
    await security_center(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Dispatcher for all ua:* callbacks
# ─────────────────────────────────────────────────────────────────────────

async def ua_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central dispatcher for ua:* callback patterns."""
    query = update.callback_query
    data = query.data if query else ""

    if data == "ua:menu":
        await account_menu(update, context)
    elif data == "ua:profile":
        await user_profile(update, context)
    elif data == "ua:rec":
        await receipts_menu(update, context)
    elif data.startswith("ua:rec:v:"):
        await view_receipt(update, context)
    elif data == "ua:orders" or data.startswith("ua:orders:p:"):
        await order_status_list(update, context)
    elif data.startswith("ua:order:"):
        await order_timeline(update, context)
    elif data == "ua:dl" or data.startswith("ua:dl:p:"):
        await download_center(update, context)
    elif data.startswith("ua:dl:v:"):
        await view_download(update, context)
    elif data == "ua:hist" or data.startswith("ua:hist:p:"):
        await activity_history(update, context)
    elif data == "ua:sec":
        await security_center(update, context)
    elif data.startswith("ua:sec:end:"):
        await terminate_session(update, context)
    elif data == "ua:sec:endall":
        await terminate_all_other_sessions(update, context)
    elif data.startswith("ua:sec:lh:"):
        await login_history_view(update, context)
    elif data == "ua:noop":
        await query.answer()
    else:
        await query.answer()
