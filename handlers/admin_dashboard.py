"""Professional Admin Dashboard, low-stock viewer, preview, and audit log.

All rendering is additive — the existing product / order / user / payment
/ coupon / loyalty / referral / broadcast / settings handlers are reused
unchanged; this module only replaces the *main menu* rendering and adds
the new sections (low stock, preview, audit log).
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    get_db_session, User, Product, Order, Transaction,
    OrderStatus, TransactionStatus, Settings, AdminAuditLog,
)
from utils import is_admin, format_price
from utils.bot_config import cfg
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Main dashboard keyboard
# ─────────────────────────────────────────────────────────────────────

def build_admin_dashboard_keyboard(
    maintenance_on: bool,
    stats: dict | None = None,
) -> InlineKeyboardMarkup:
    """Two-column dashboard keyboard used by the main /admin menu.

    ``stats`` is the dict returned by ``_collect_dashboard_stats()``.  When
    provided, dynamic counters are shown on Orders, Payments, Low Stock, and
    Tickets buttons so attention-worthy items are visible at a glance.
    """
    s = stats or {}

    # ── Dynamic counter helpers ──────────────────────────────────────────────
    def _counter(n: int) -> str:
        """Return ' (N)' when N > 0, else empty string."""
        return f" ({n:,})" if n > 0 else ""

    pending_orders   = s.get("pending_orders", 0)
    pending_payments = s.get("pending_payments", 0)
    low_stock        = s.get("low_stock", 0)
    open_tickets     = s.get("open_tickets", 0)

    # ── Maintenance toggle label ─────────────────────────────────────────────
    # Two-line format: emoji + name on line 1, state on line 2.
    # 🔴 = maintenance active (caution); 🟢 = maintenance off (bot healthy).
    maint_label = (
        "🔴 Maintenance: ON"
        if maintenance_on
        else "🟢 Maintenance: OFF"
    )

    # ── Keyboard — consistent 2-column grid, logically paired rows ───────────
    kb = [
        # Core operations
        [
            InlineKeyboardButton("📦 Products",               callback_data="admin_products"),
            InlineKeyboardButton(f"🛒 Orders{_counter(pending_orders)}",
                                                              callback_data="admin_orders"),
        ],
        [
            InlineKeyboardButton(f"💳 Payments{_counter(pending_payments)}",
                                                              callback_data="admin_confirm_order"),
            InlineKeyboardButton("👥 Users",                  callback_data="admin_users"),
        ],
        # Marketing & engagement
        [
            InlineKeyboardButton("📢 Broadcast",              callback_data="admin_broadcast"),
            InlineKeyboardButton("🎟️ Coupons",               callback_data="admin_coupons"),
        ],
        [
            InlineKeyboardButton("🎁 Loyalty",                callback_data="admin_loyalty"),
            InlineKeyboardButton("👑 Referrals",              callback_data="admin_referral_reward"),
        ],
        # Management tools
        [
            InlineKeyboardButton("🏆 VIP Manager",            callback_data="vip:menu"),
            InlineKeyboardButton("🔑 API Keys",               callback_data="aim:menu"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings",               callback_data="admin_settings"),
            InlineKeyboardButton("📊 Analytics",              callback_data="admin_analytics"),
        ],
        # Insights & search — paired in one row
        [
            InlineKeyboardButton("📈 Growth & LTV",           callback_data="admin_analytics_cohort"),
            InlineKeyboardButton("🔍 Order Search",           callback_data="aos:menu"),
        ],
        # Monitoring — both carry live counters
        [
            InlineKeyboardButton(f"⚠️ Low Stock{_counter(low_stock)}",
                                                              callback_data="admin_low_stock"),
            InlineKeyboardButton(f"🎫 Tickets{_counter(open_tickets)}",
                                                              callback_data="admin_tickets"),
        ],
        # Tooling
        [
            InlineKeyboardButton("🧾 Audit Log",              callback_data="admin_audit_log_0"),
            InlineKeyboardButton("👁️ Store Preview",          callback_data="admin_preview"),
        ],
        # ── System Tools ─────────────────────────────────────────────────────
        [
            InlineKeyboardButton("📈 System Health",          callback_data="acc:sys:health"),
            InlineKeyboardButton("📝 Activity Logs",          callback_data="acc:sys:logs"),
        ],
        [
            InlineKeyboardButton("🗄️ Database Status",        callback_data="acc:sys:db"),
            InlineKeyboardButton("🧹 Cache Manager",          callback_data="acc:sys:cache"),
        ],
        [
            InlineKeyboardButton("📤 Backup",                 callback_data="acc:sys:backup"),
            InlineKeyboardButton("📥 Restore",                callback_data="acc:sys:restore"),
        ],
        [InlineKeyboardButton("🔄 Background Jobs",           callback_data="acc:sys:jobs")],
        # Full-width controls
        [InlineKeyboardButton(maint_label,                    callback_data="admin_maintenance_toggle")],
        [InlineKeyboardButton("⬅️ Exit Admin Panel",          callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(kb)


def _collect_dashboard_stats() -> dict:
    """Live counts + revenue for the dashboard header."""
    stats = {
        "users": 0, "products": 0, "orders": 0,
        "pending_orders": 0, "pending_payments": 0,
        "total_sales": 0.0, "low_stock": 0,
        "failed_orders": 0, "system_alerts": 0,
    }
    # V18 — feature stats (non-blocking; failures return 0)
    try:
        from handlers.admin_features import get_feature_stats
        stats.update(get_feature_stats())
    except Exception:
        pass

    try:
        with get_db_session() as s:
            stats["users"] = s.query(func.count(User.id)).scalar() or 0
            stats["products"] = s.query(func.count(Product.id)).filter(
                Product.is_active == True  # noqa: E712
            ).scalar() or 0
            stats["orders"] = s.query(func.count(Order.id)).scalar() or 0
            stats["pending_orders"] = s.query(func.count(Order.id)).filter(
                Order.status == OrderStatus.PROCESSING
            ).scalar() or 0
            # This badge links straight to the Payments menu -> Pending
            # Deposits queue, so it must count exactly what that queue
            # counts: manual deposits (generic ManualPaymentMethod +
            # bKash/Nagad Manual mode) waiting for a human to check a
            # submitted TXID/screenshot. It deliberately excludes gateways
            # (Binance Pay, Bybit Pay, ZiniPay, Cryptomus, NOWPayments,
            # Heleket, Telegram Stars) still waiting on their own webhook/API
            # confirmation, and PendingManualVerification rows (failed
            # auto-verifications) — those are a separate concern with their
            # own admin pages (see handlers/admin_binance.py,
            # handlers/admin_bybit.py, handlers/admin_webhook_monitor.py) and
            # must never be folded into this number, or this badge would
            # show a count the Pending Deposits page itself can't match.
            from services.payment_ui import count_pending_deposits as _cpd
            stats["pending_payments"] = _cpd(s)["deposits"]
            stats["total_sales"] = float(s.query(func.coalesce(
                func.sum(Order.total_amount), 0.0
            )).filter(Order.status == OrderStatus.COMPLETED).scalar() or 0.0)
            low_th = cfg.get_int("low_stock_threshold", 5)
            stats["low_stock"] = s.query(func.count(Product.id)).filter(
                Product.is_active == True,  # noqa: E712
                Product.stock_count <= low_th,
            ).scalar() or 0
            stats["failed_orders"] = s.query(func.count(Order.id)).filter(
                Order.status == OrderStatus.FAILED
            ).scalar() or 0
    except Exception:
        logger.exception("dashboard stats query failed")

    # System alerts — any integration currently offline/warning per the
    # existing health-monitor service (unchanged; just counted here).
    try:
        from services.health_monitor import get_latest_statuses
        stats["system_alerts"] = sum(
            1 for row in get_latest_statuses()
            if row.get("status") in ("offline", "warning")
        )
    except Exception:
        stats.setdefault("system_alerts", 0)

    # V20: Open ticket count
    try:
        from sqlalchemy import text as _sqltxt
        with get_db_session() as s:
            row = s.execute(_sqltxt(
                "SELECT COUNT(*) FROM support_tickets WHERE status = 'open'"
            )).fetchone()
            stats["open_tickets"] = int(row[0]) if row else 0
    except Exception:
        stats.setdefault("open_tickets", 0)

    # V20: Active announcement count
    try:
        from sqlalchemy import text as _sqltxt2
        with get_db_session() as s:
            row = s.execute(_sqltxt2(
                "SELECT COUNT(*) FROM announcements WHERE is_active = TRUE"
            )).fetchone()
            stats["active_announcements"] = int(row[0]) if row else 0
    except Exception:
        stats.setdefault("active_announcements", 0)

    return stats


def _render_dashboard_text(stats: dict) -> str:
    """Build the dashboard header shown above the admin menu.

    Layout, top to bottom:
      1. 🛡️ Admin Control Center
      2. ⚠️ Needs Attention
      3. 📊 Store Overview
      4. 💰 Revenue
      5. 📈 Performance
    """
    failed_orders = stats.get("failed_orders", 0)
    system_alerts = stats.get("system_alerts", 0)

    # Action Required — only genuinely actionable items; each line is
    # hidden automatically whenever its count is zero.
    alerts = []
    if stats["pending_payments"]:
        alerts.append(f"  🔴  <b>{stats['pending_payments']:,}</b> Pending Payments")
    if stats["low_stock"]:
        alerts.append(f"  📦  <b>{stats['low_stock']:,}</b> Low Stock Products")
    if failed_orders:
        alerts.append(f"  ⚠️  <b>{failed_orders:,}</b> Failed Orders")
    if system_alerts:
        alerts.append(f"  🚨  <b>{system_alerts:,}</b> System Alerts")

    if alerts:
        attention_block = "🔴 <b>Action Required</b>\n" + "\n".join(alerts)
    else:
        attention_block = "🟢 <b>All Clear</b>  —  No pending actions"

    return (
        "⚡ <b>Admin Control Center</b>\n"
        "──────────────────────────\n\n"
        f"{attention_block}\n\n"
        "──────────────────────────\n"
        f"👥  Total Users <b>{stats['users']:,}</b>\n"
        f"📦  Total Products <b>{stats['products']:,}</b>\n"
        f"🛒  Total Orders <b>{stats['orders']:,}</b>\n"
        f"💰  Total Sales <b>{format_price(stats['total_sales'])}</b>"
    )


async def render_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """V9: /admin routes into the Premium Admin Control Center."""
    from handlers.admin_control_center import render_control_center
    await render_control_center(update, context)


async def render_legacy_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy compact dashboard (kept for the "📊 Dashboard" tile inside ACC)."""
    stats = _collect_dashboard_stats()
    text = _render_dashboard_text(stats)
    kb = build_admin_dashboard_keyboard(cfg.get_bool("maintenance_mode", False), stats=stats)

    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            try:
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────
# Maintenance toggle
# ─────────────────────────────────────────────────────────────────────

async def admin_maintenance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    new_val = not cfg.get_bool("maintenance_mode", False)
    cfg.set("maintenance_mode", new_val)
    try:
        from utils.audit import log_admin_action
        log_admin_action(update.effective_user.id, "maintenance.toggle",
                         details=f"maintenance_mode={new_val}")
    except Exception:
        pass
    await query.answer(
        f"Maintenance mode {'ENABLED' if new_val else 'DISABLED'}.",
        show_alert=True,
    )
    await render_dashboard(update, context)


# ─────────────────────────────────────────────────────────────────────
# Low-stock viewer
# ─────────────────────────────────────────────────────────────────────

async def admin_low_stock_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    threshold = cfg.get_int("low_stock_threshold", 5)
    with get_db_session() as s:
        rows = (s.query(Product)
                 .filter(Product.is_active == True,  # noqa: E712
                         Product.stock_count <= threshold)
                 .order_by(Product.stock_count.asc())
                 .limit(20).all())
        lines = [f"📉 <b>Low-Stock Products</b>",
                 f"<i>Threshold: {threshold} — configurable in Bot Configuration → Inventory.</i>",
                 ""]
        if not rows:
            lines.append("✅ No products at or below the low-stock threshold.")
        else:
            for p in rows:
                lines.append(
                    f"• <b>{p.name}</b> — stock: <b>{p.stock_count}</b> "
                    f"({format_price(p.price)})"
                )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_low_stock")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    try:
        await query.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────
# Preview system
# ─────────────────────────────────────────────────────────────────────

def _preview_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👋 Welcome Message", callback_data="admin_preview_welcome")],
        [InlineKeyboardButton("📦 Product Card", callback_data="admin_preview_product")],
        [InlineKeyboardButton("🧾 Receipt Footer", callback_data="admin_preview_receipt")],
        [InlineKeyboardButton("💳 Payment Instructions", callback_data="admin_preview_payment")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])


async def admin_preview_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            "👁 <b>Preview</b>\n\nRenders the message users would actually see, using "
            "the current database configuration.",
            reply_markup=_preview_menu_kb(),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_to_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_preview")]])


async def admin_preview_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as s:
        row = s.query(Settings).first()
        msg = (row.welcome_message if row and row.welcome_message
               else "Welcome to our digital store!")
    try:
        await query.edit_message_text(
            f"👋 <b>Welcome Message Preview</b>\n\n{msg}",
            reply_markup=_back_to_preview_kb(), parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_preview_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as s:
        p = (s.query(Product)
              .filter(Product.is_active == True)  # noqa: E712
              .order_by(Product.id.desc()).first())
        if not p:
            text = "📦 No active product to preview yet."
        else:
            text = (
                "📦 <b>Product Card Preview</b>\n\n"
                f"🏷 <b>{p.name}</b>\n"
                f"💰 Price: <b>{format_price(p.price)}</b>\n"
                f"📦 Stock: <b>{p.stock_count}</b>\n"
            )
            if p.description:
                text += f"\n{p.description[:400]}"
    try:
        await query.edit_message_text(text, reply_markup=_back_to_preview_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_preview_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    footer = cfg.get_str("receipt_footer", "Thank you for shopping with us!")
    text = (
        "🧾 <b>Receipt Preview</b>\n\n"
        "Order #12345\n"
        "Item: <i>Sample Product</i> × 1\n"
        f"Total: <b>{format_price(10.0)}</b>\n"
        f"\n<i>{footer}</i>"
    )
    try:
        await query.edit_message_text(text, reply_markup=_back_to_preview_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_preview_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    from database import ManualPaymentMethod
    with get_db_session() as s:
        m = (s.query(ManualPaymentMethod)
              .filter(ManualPaymentMethod.is_active == True)  # noqa: E712
              .order_by(ManualPaymentMethod.sort_order.asc(),
                        ManualPaymentMethod.id.asc()).first())
        if not m:
            text = "💳 No active payment method configured to preview."
        else:
            text = (
                f"💳 <b>{m.emoji or ''} {m.name}</b>\n\n"
                f"{m.instructions or ''}\n\n"
                + (f"🏷 {m.account_label}\n" if m.account_label else "")
                + (f"💳 <code>{m.account_number}</code>\n" if m.account_number else "")
                + f"\n💰 Min: <b>{format_price(m.min_amount or 0)}</b>"
                + (f" — Max: <b>{format_price(m.max_amount)}</b>"
                   if m.max_amount else "")
            )
    try:
        await query.edit_message_text(text, reply_markup=_back_to_preview_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────
# Audit log viewer
# ─────────────────────────────────────────────────────────────────────

_AUDIT_PAGE_SIZE = 10


async def admin_audit_log_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        page = int(query.data.split("_")[-1])
    except Exception:
        page = 0
    page = max(page, 0)

    with get_db_session() as s:
        total = s.query(func.count(AdminAuditLog.id)).scalar() or 0
        rows = (s.query(AdminAuditLog)
                 .order_by(AdminAuditLog.id.desc())
                 .offset(page * _AUDIT_PAGE_SIZE)
                 .limit(_AUDIT_PAGE_SIZE).all())

        lines = [f"🧾 <b>Admin Audit Log</b>  <i>({total} entries)</i>", ""]
        if not rows:
            lines.append("No admin actions recorded yet.")
        for r in rows:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            tgt = ""
            if r.target_type:
                tgt = f" · {r.target_type}"
                if r.target_id:
                    tgt += f"#{r.target_id}"
            detail = f" — {r.details}" if r.details else ""
            lines.append(f"<code>{when}</code> · <b>{r.action}</b>{tgt}{detail}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"admin_audit_log_{page-1}"))
    if (page + 1) * _AUDIT_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"admin_audit_log_{page+1}"))
    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])

    try:
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
