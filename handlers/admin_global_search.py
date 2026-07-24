"""V45 — Global Search Engine admin handler.

Callback namespace: gse:*
All admin actions require the 'admin' permission.

V45 additions over V43:
  • Date-range filter conversation (gse:filter_date)
  • Status / sort / rating / price filters panel (gse:filters)
  • Sort control: newest | oldest | amount_desc | amount_asc
  • Complete _fetch_detail for all 27 modules
  • Filter summary shown in results header
  • Filter-reset, filter-set-status helpers
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from services.global_search_service import (
    SEARCH_MODULES, ALL_MODULE_SLUGS,
    search, get_history, get_saved_searches,
    save_search, delete_search_record, clear_history, get_stats,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─── ConversationHandler states ───────────────────────────────────────────────
GSE_QUERY      = 300
GSE_SAVE_LBL   = 301
GSE_DATE_FROM  = 302
GSE_DATE_TO    = 303

# ─── Pagination / UI constants ────────────────────────────────────────────────
RESULTS_PER_PAGE = 5
MODULES_PER_PAGE = 10

_SORT_LABELS = {
    "newest":      "🕐 Newest First",
    "oldest":      "📅 Oldest First",
    "amount_desc": "💰 Amount ↓",
    "amount_asc":  "💰 Amount ↑",
}
_SORT_CYCLE = ["newest", "oldest", "amount_desc", "amount_asc"]

# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _check_perm(update: Update) -> bool:
    uid = update.effective_user.id
    if not has_permission(uid, "admin"):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


def _back_btn(to: str = "gse:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


def _qhash(query: str) -> str:
    return hashlib.md5(query.encode()).hexdigest()[:8]


def _enabled() -> bool:
    return cfg.get("gse_status", "enabled") != "disabled"


def _maintenance() -> bool:
    return cfg.get("gse_status", "enabled") == "maintenance"


# ─── Context storage ──────────────────────────────────────────────────────────

def _store_results(context: ContextTypes.DEFAULT_TYPE,
                   qhash: str, results: list, query: str, modules: list) -> None:
    context.user_data[f"gse_res_{qhash}"] = {
        "results": results, "query": query, "modules": modules
    }


def _load_results(context: ContextTypes.DEFAULT_TYPE, qhash: str) -> Optional[dict]:
    return context.user_data.get(f"gse_res_{qhash}")


def _get_filters(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("gse_filters", {})


def _set_filter(context: ContextTypes.DEFAULT_TYPE, key: str, value) -> None:
    f = context.user_data.setdefault("gse_filters", {})
    if value is None or value == "":
        f.pop(key, None)
    else:
        f[key] = value


def _get_sort(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("gse_sort", "newest")


def _set_sort(context: ContextTypes.DEFAULT_TYPE, sort: str) -> None:
    context.user_data["gse_sort"] = sort


def _fmt_filters(filters: dict) -> str:
    """Return a human-readable summary of active filters."""
    parts = []
    if filters.get("date_from"):
        parts.append(f"From: {filters['date_from']}")
    if filters.get("date_to"):
        parts.append(f"To: {filters['date_to']}")
    if filters.get("status"):
        parts.append(f"Status: {filters['status']}")
    if filters.get("rating"):
        parts.append(f"Rating: ⭐×{filters['rating']}")
    if not parts:
        return "none"
    return " | ".join(parts)


# ─── Main menu ────────────────────────────────────────────────────────────────

async def gse_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    if _maintenance():
        await _edit(update,
                    "🔍 <b>Global Search</b>\n\n🟡 <b>System under maintenance.</b>",
                    InlineKeyboardMarkup([[_back_btn("acc:root")]]))
        return

    stats = get_stats()
    active_filters = _get_filters(context)
    sort = _get_sort(context)
    text = (
        "🔍 <b>GLOBAL SEARCH ENGINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 Total searches:   <b>{stats['total']}</b>\n"
        f"📅 Today:  <b>{stats['today']}</b>   "
        f"📆 Week: <b>{stats['weekly']}</b>\n"
        f"⏱ Avg time:  <b>{stats['avg_ms']} ms</b>\n"
        f"🔀 Sort:     <b>{_SORT_LABELS.get(sort, sort)}</b>\n"
        f"🔧 Filters:  <b>{_fmt_filters(active_filters)}</b>\n"
    )
    if stats["popular"]:
        top = ", ".join(f"<code>{p[0]}</code>" for p in stats["popular"][:3])
        text += f"\n🔥 Popular: {top}\n"

    kb = [
        [InlineKeyboardButton("🔎 New Search",        callback_data="gse:new_search")],
        [InlineKeyboardButton("🔧 Filters",           callback_data="gse:filters"),
         InlineKeyboardButton("🔀 Sort",              callback_data="gse:sort")],
        [InlineKeyboardButton("🗂 Modules",           callback_data="gse:mod_filter"),
         InlineKeyboardButton("📊 Stats",             callback_data="gse:stats")],
        [InlineKeyboardButton("🕐 History",           callback_data="gse:history"),
         InlineKeyboardButton("⭐ Saved",             callback_data="gse:saved")],
        [InlineKeyboardButton("⚙️ Settings",          callback_data="gse:settings")],
        [_back_btn("acc:root")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── New search conversation ──────────────────────────────────────────────────

async def gse_new_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return ConversationHandler.END

    if _maintenance():
        await q.answer("🟡 Search is under maintenance.", show_alert=True)
        return ConversationHandler.END

    context.user_data.setdefault("gse_modules", ALL_MODULE_SLUGS)
    active = _get_filters(context)
    filter_hint = f"\n🔧 Active filters: <i>{_fmt_filters(active)}</i>" if active and any(active.values()) else ""

    await _edit(update,
                "🔍 <b>Global Search</b>\n\n"
                "Type your search query:\n\n"
                "• Keyword, username, order ID, coupon code, TXID…\n"
                "• Partial matches are supported\n"
                "• Case-insensitive"
                f"{filter_hint}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔧 Filters", callback_data="gse:filters"),
                     InlineKeyboardButton("❌ Cancel", callback_data="gse:cancel_search")],
                ]))
    return GSE_QUERY


async def gse_query_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text or len(text) < 2:
        await update.message.reply_text("❌ Query too short. Enter at least 2 characters:")
        return GSE_QUERY

    # Smart query hints: strip # prefix for ID searches, @ for username
    if text.startswith("#") and text[1:].isdigit():
        text = text[1:]
    elif text.startswith("@"):
        text = text[1:]

    admin_id = update.effective_user.id
    modules = context.user_data.get("gse_modules", ALL_MODULE_SLUGS)
    active_filters = _get_filters(context)
    sort = _get_sort(context)

    result = search(text, modules=modules, filters=active_filters,
                    sort=sort, admin_telegram_id=admin_id,
                    page=1, per_page=RESULTS_PER_PAGE)

    qhash = _qhash(text)
    _store_results(context, qhash, result["results"], text, modules)
    context.user_data["gse_last_result"] = result
    context.user_data["gse_last_qhash"] = qhash

    kb = _build_results_kb(result, qhash, page=1)
    msg_text = _build_results_text(result, active_filters, sort)

    await update.message.reply_text(msg_text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def gse_cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("Cancelled")
    await _edit(update, "❌ Search cancelled.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Search Menu",
                                                             callback_data="gse:menu")]]))
    return ConversationHandler.END


# ─── Results helpers ──────────────────────────────────────────────────────────

def _build_results_text(result: dict,
                         active_filters: Optional[dict] = None,
                         sort: str = "newest") -> str:
    total = result["total"]
    query = result["query"]
    elapsed = result["search_time_ms"]
    page = result["page"]
    pages = result["pages"]

    filter_line = ""
    if active_filters and any(active_filters.values()):
        filter_line = f"\n🔧 Filters: <i>{_fmt_filters(active_filters)}</i>"

    if total == 0:
        return (f"🔍 <b>Search: <code>{query}</code></b>\n\n"
                "❌ No results found.\n\n"
                f"Try a different keyword or adjust filters.{filter_line}")

    sort_label = _SORT_LABELS.get(sort, sort)
    text = (f"🔍 <b>Results for: <code>{query}</code></b>\n"
            f"Found <b>{total}</b> results in {elapsed}ms — Page {page}/{pages}\n"
            f"🔀 {sort_label}{filter_line}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n")
    for i, r in enumerate(result["results"], 1):
        mod_meta = SEARCH_MODULES.get(r["module"], {"emoji": "•"})
        date_str = ""
        if r.get("created_at"):
            try:
                date_str = r["created_at"].strftime(" | %Y-%m-%d")
            except Exception:
                pass
        text += (f"{i}. {mod_meta['emoji']} <b>{r['label']}</b>\n"
                 f"   {r['summary']}{date_str}\n")
    return text


def _build_results_kb(result: dict, qhash: str, page: int,
                       active_filters: Optional[dict] = None,
                       sort: str = "newest") -> InlineKeyboardMarkup:
    kb = []
    for r in result["results"]:
        mod_meta = SEARCH_MODULES.get(r["module"], {"emoji": "•"})
        kb.append([InlineKeyboardButton(
            f"{mod_meta['emoji']} {r['label'][:40]}",
            callback_data=r["cb_detail"]
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"gse:page:{qhash}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{result['pages']}", callback_data="gse:noop"))
    if page < result["pages"]:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"gse:page:{qhash}:{page+1}"))
    if nav:
        kb.append(nav)

    action_row = []
    if result["total"] > 0:
        action_row.append(InlineKeyboardButton("⭐ Save", callback_data=f"gse:save_prompt:{qhash}"))
    action_row.append(InlineKeyboardButton("🔀 Sort", callback_data="gse:sort"))
    action_row.append(InlineKeyboardButton("🔧 Filter", callback_data="gse:filters"))
    kb.append(action_row)
    kb.append([InlineKeyboardButton("🔎 New Search", callback_data="gse:new_search"),
               InlineKeyboardButton("🏠 Menu", callback_data="gse:menu")])
    return InlineKeyboardMarkup(kb)


# ─── Pagination ───────────────────────────────────────────────────────────────

async def gse_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")           # gse:page:{qhash}:{page}
    qhash = parts[2]
    page = int(parts[3])
    stored = _load_results(context, qhash)
    if not stored:
        await q.answer("⚠️ Session expired. Please search again.", show_alert=True)
        return

    active_filters = _get_filters(context)
    sort = _get_sort(context)
    result = search(stored["query"], modules=stored["modules"],
                    filters=active_filters, sort=sort,
                    admin_telegram_id=None,
                    page=page, per_page=RESULTS_PER_PAGE)
    _store_results(context, qhash, result["results"], stored["query"], stored["modules"])

    await _edit(update,
                _build_results_text(result, active_filters, sort),
                _build_results_kb(result, qhash, page, active_filters, sort))


# ─── Detail view ──────────────────────────────────────────────────────────────

async def gse_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    # gse:det:{module}:{id}
    parts = q.data.split(":", 4)
    if len(parts) < 4:
        await q.answer("❌ Invalid detail reference.", show_alert=True)
        return

    module = parts[2]
    rec_id = parts[3]
    mod_meta = SEARCH_MODULES.get(module, {"label": module, "emoji": "•"})

    text = f"{mod_meta['emoji']} <b>{mod_meta['label']} — Record #{rec_id}</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━\n"
    detail_lines = _fetch_detail(module, rec_id)
    text += "\n".join(detail_lines) if detail_lines else "No details available."

    kb = [
        [InlineKeyboardButton("🔎 New Search", callback_data="gse:new_search")],
        [InlineKeyboardButton("🏠 Search Menu", callback_data="gse:menu")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


def _fetch_detail(module: str, rec_id: str) -> list[str]:
    """Fetch detail lines for a single record. Covers all 27 modules."""
    try:
        from database import get_db_session
        lines: list[str] = []
        with get_db_session() as session:
            lines = _fetch_detail_inner(session, module, rec_id)
        return lines or [f"No record found with ID {rec_id}."]
    except Exception as e:
        logger.error("_fetch_detail %s/%s: %s", module, rec_id, e, exc_info=True)
        return [f"Error loading record: {e}"]


def _fetch_detail_inner(session, module: str, rec_id: str) -> list[str]:  # noqa: C901
    """Inner detail fetcher — one branch per module."""
    from database.models import (
        User, Order, Product, Transaction, Coupon, SupportTicket,
        GlobalActivityEntry, AdminAuditLog,
    )

    def _safe_str(val, default="—") -> str:
        return str(val) if val is not None else default

    def _dt(val) -> str:
        if not val:
            return "—"
        try:
            return val.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return str(val)

    lines: list[str] = []

    if module == "users":
        r = session.query(User).filter(User.id == int(rec_id)).first()
        if r:
            bal = getattr(r, "wallet_balance", None) or getattr(r, "balance", 0) or 0
            lines = [
                f"🆔 ID: <code>{r.id}</code>",
                f"📱 Telegram ID: <code>{r.telegram_id}</code>",
                f"👤 Username: @{r.username or '—'}",
                f"📛 Name: {getattr(r, 'first_name', '') or ''} {getattr(r, 'last_name', '') or ''}".strip() or "—",
                f"💰 Balance: <b>${bal:.2f}</b>",
                f"🚫 Banned: {'Yes' if r.is_banned else 'No'}",
                f"📅 Joined: {_dt(r.created_at)}",
            ]

    elif module == "orders":
        r = session.query(Order).filter(Order.id == int(rec_id)).first()
        if r:
            st = _safe_str(r.status.value if hasattr(r.status, "value") else r.status)
            lines = [
                f"🧾 Order ID: <code>{r.id}</code>",
                f"👤 User ID: <code>{r.user_id}</code>",
                f"💰 Total: <b>${r.total_amount:.2f}</b>",
                f"💳 Method: {_safe_str(getattr(r, 'payment_method', None))}",
                f"📦 Status: {st}",
                f"📅 Created: {_dt(r.created_at)}",
            ]
            # Show items
            try:
                from database.models import OrderItem, Product as Prd
                items = session.query(OrderItem).filter(OrderItem.order_id == r.id).all()
                if items:
                    lines.append("\n<b>Items:</b>")
                    for item in items[:5]:
                        pname = _safe_str(getattr(item, "product_id"))
                        try:
                            p = session.query(Prd).filter(Prd.id == item.product_id).first()
                            if p:
                                pname = p.name[:30]
                        except Exception:
                            pass
                        lines.append(f"  • {pname} × {item.quantity} @ ${item.price:.2f}")
            except Exception:
                pass

    elif module == "products" or module == "bundles":
        r = session.query(Product).filter(Product.id == int(rec_id)).first()
        if r:
            price = r.sale_price if (r.sale_price and r.sale_price > 0) else r.price
            lines = [
                f"📦 Product: <b>{r.name}</b>",
                f"🆔 ID: <code>{r.id}</code>",
                f"🔖 Type: {_safe_str(r.product_type.value if hasattr(r.product_type, 'value') else r.product_type)}",
                f"💰 Price: <b>${price:.2f}</b>",
                f"🏷 Sale price: ${r.sale_price:.2f}" if r.sale_price else "🏷 Sale: None",
                f"📊 Stock: {r.stock_count}",
                f"✅ Active: {'Yes' if r.is_active else 'No'}",
                f"⭐ Featured: {'Yes' if r.is_featured else 'No'}",
                f"🛒 Sales count: {r.sales_count}",
                f"📅 Created: {_dt(r.created_at)}",
            ]

    elif module in ("transactions", "deposits", "withdrawals", "payments"):
        r = session.query(Transaction).filter(Transaction.id == int(rec_id)).first()
        if r:
            st = _safe_str(r.status.value if hasattr(r.status, "value") else r.status)
            lines = [
                f"💳 Tx ID: <code>{r.id}</code>",
                f"👤 User ID: <code>{r.user_id}</code>",
                f"💰 Amount: <b>${r.amount:.2f}</b>",
                f"💳 Method: {_safe_str(getattr(r, 'payment_method', None))}",
                f"📊 Status: {st}",
                f"🔑 TXID: <code>{getattr(r, 'txid', None) or '—'}</code>",
                f"📝 Proof: {(getattr(r, 'proof', None) or '')[:60] or '—'}",
                f"🌐 Crypto addr: {(getattr(r, 'crypto_address', None) or '')[:40] or '—'}",
                f"📅 Created: {_dt(r.created_at)}",
                f"✅ Completed: {_dt(getattr(r, 'completed_at', None))}",
            ]

    elif module == "coupons":
        r = session.query(Coupon).filter(Coupon.id == int(rec_id)).first()
        if r:
            lines = [
                f"🎟 Code: <code>{r.code}</code>",
                f"💰 Discount: {r.discount_value} ({_safe_str(getattr(r, 'discount_type', None))})",
                f"🔢 Used: {getattr(r, 'times_used', 0)}/{getattr(r, 'max_uses', '∞') or '∞'}",
                f"👤 User limit: {getattr(r, 'per_user_limit', '—')}",
                f"✅ Active: {'Yes' if getattr(r, 'is_active', True) else 'No'}",
                f"📅 Expires: {_dt(getattr(r, 'expires_at', None))}",
                f"📅 Created: {_dt(r.created_at)}",
            ]

    elif module == "support_tickets":
        r = session.query(SupportTicket).filter(SupportTicket.id == int(rec_id)).first()
        if r:
            st = _safe_str(r.status.value if hasattr(r.status, "value") else r.status)
            lines = [
                f"🎫 Ticket: {getattr(r, 'ticket_number', None) or f'#{r.id}'}",
                f"👤 User ID: <code>{r.user_id}</code>",
                f"📋 Subject: {getattr(r, 'subject', '—')}",
                f"📊 Status: {st}",
                f"🎯 Priority: {_safe_str(getattr(r, 'priority', None))}",
                f"📂 Category: {getattr(r, 'category', '—')}",
                f"📅 Created: {_dt(r.created_at)}",
                f"✅ Resolved: {_dt(getattr(r, 'resolved_at', None))}",
            ]

    elif module == "activity_timeline":
        r = session.query(GlobalActivityEntry).filter(GlobalActivityEntry.id == int(rec_id)).first()
        if r:
            lines = [
                f"📜 Action: <b>{r.action}</b>",
                f"📂 Category: {r.category}",
                f"👤 User ID: {r.user_id or '—'}",
                f"👤 Username: @{r.username or '—'}",
                f"📝 Description: {(r.description or '')[:200]}",
                f"✅ Status: {r.status}",
                f"📅 Created: {_dt(r.created_at)}",
            ]

    elif module in ("admin_logs", "audit_logs", "system_logs"):
        r = session.query(AdminAuditLog).filter(AdminAuditLog.id == int(rec_id)).first()
        if r:
            lines = [
                f"🔐 Action: <b>{r.action}</b>",
                f"👤 Admin TG ID: <code>{r.admin_telegram_id}</code>",
                f"🎯 Target: {_safe_str(getattr(r, 'target_user_id', None))}",
                f"📋 Details: {(getattr(r, 'details', '') or '')[:200]}",
                f"📂 Module: {_safe_str(getattr(r, 'module', None))}",
                f"📅 Created: {_dt(r.created_at)}",
            ]

    elif module == "gift_cards":
        try:
            from database.models import GiftCard
            r = session.query(GiftCard).filter(GiftCard.id == int(rec_id)).first()
            if r:
                lines = [
                    f"🎁 Code: <code>{r.code}</code>",
                    f"💰 Value: {r.value}",
                    f"🔢 Used: {r.used_count}/{r.max_uses or '∞'}",
                    f"✅ Active: {'Yes' if r.is_active else 'No'}",
                    f"📅 Created: {_dt(r.created_at)}",
                    f"⏰ Expires: {_dt(getattr(r, 'expires_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "reviews":
        try:
            from database.models import Review
            r = session.query(Review).filter(Review.id == int(rec_id)).first()
            if r:
                lines = [
                    f"⭐ Review ID: <code>{r.id}</code>",
                    f"🌟 Rating: {'★' * r.rating}{'☆' * (5 - r.rating)} ({r.rating}/5)",
                    f"👤 User ID: <code>{r.user_id}</code>",
                    f"📦 Product ID: <code>{getattr(r, 'product_id', '—')}</code>",
                    f"💬 Comment: {(r.comment or '')[:300]}",
                    f"👁 Visibility: {'🙈 Hidden' if r.is_hidden else '👁 Visible'}",
                    f"📅 Created: {_dt(r.created_at)}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "notifications":
        try:
            from database.models import AdminNotification
            r = session.query(AdminNotification).filter(AdminNotification.id == int(rec_id)).first()
            if r:
                lines = [
                    f"🔔 Title: <b>{r.title}</b>",
                    f"🏷 Event: {r.event_type}",
                    f"📂 Category: {r.category}",
                    f"⚠️ Severity: {r.severity}",
                    f"📝 Body: {(r.body or '')[:300]}",
                    f"✅ Read: {'Yes' if r.is_read else 'No'}",
                    f"📌 Pinned: {'Yes' if r.is_pinned else 'No'}",
                    f"📅 Created: {_dt(r.created_at)}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "product_keys":
        try:
            from database.models import ProductKey
            r = session.query(ProductKey).filter(ProductKey.id == int(rec_id)).first()
            if r:
                lines = [
                    f"🔐 Key ID: <code>{r.id}</code>",
                    f"📦 Product ID: <code>{r.product_id}</code>",
                    f"🔑 Value: <code>{(r.key_value or '')[:60]}</code>",
                    f"✅ Sold: {'Yes' if r.is_sold else 'No'}",
                    f"🧾 Order ID: <code>{r.order_id or '—'}</code>",
                    f"📅 Created: {_dt(getattr(r, 'created_at', None))}",
                    f"📅 Sold at: {_dt(getattr(r, 'sold_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "license_keys":
        try:
            from database.models import ManagedKey
            r = session.query(ManagedKey).filter(ManagedKey.id == int(rec_id)).first()
            if r:
                lines = [
                    f"🔑 Key ID: <code>{r.id}</code>",
                    f"📝 Value: <code>{(getattr(r, 'key_value', '') or '')[:60]}</code>",
                    f"📊 Status: {_safe_str(getattr(r, 'status', None))}",
                    f"🏷 Type: {_safe_str(getattr(r, 'key_type', None))}",
                    f"📦 Product ID: {_safe_str(getattr(r, 'product_id', None))}",
                    f"📅 Created: {_dt(getattr(r, 'created_at', None))}",
                    f"📅 Used at: {_dt(getattr(r, 'used_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "files":
        try:
            from database.models import ManagedFile
            r = session.query(ManagedFile).filter(ManagedFile.id == int(rec_id)).first()
            if r:
                lines = [
                    f"📁 File ID: <code>{r.id}</code>",
                    f"📄 Filename: {getattr(r, 'filename', '—')}",
                    f"📏 Size: {getattr(r, 'file_size', 0) or 0} bytes",
                    f"📦 Product ID: {_safe_str(getattr(r, 'product_id', None))}",
                    f"📅 Created: {_dt(getattr(r, 'created_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "broadcasts":
        try:
            from database.models import Broadcast
            r = session.query(Broadcast).filter(Broadcast.id == int(rec_id)).first()
            if r:
                msg = getattr(r, "message_text", None) or getattr(r, "message", "") or ""
                lines = [
                    f"📢 Broadcast ID: <code>{r.id}</code>",
                    f"💬 Message: {msg[:300]}",
                    f"📊 Sent: {getattr(r, 'sent_count', 0)}",
                    f"📅 Created: {_dt(r.created_at)}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "flash_sales":
        try:
            from database.models import FlashSaleEvent
            r = session.query(FlashSaleEvent).filter(FlashSaleEvent.id == int(rec_id)).first()
            if r:
                lines = [
                    f"⚡ Flash Sale: <b>{getattr(r, 'name', r.id)}</b>",
                    f"📊 Status: {_safe_str(getattr(r, 'status', None))}",
                    f"💰 Discount: {_safe_str(getattr(r, 'discount_pct', None))}%",
                    f"📅 Start: {_dt(getattr(r, 'start_at', None))}",
                    f"📅 End: {_dt(getattr(r, 'end_at', None))}",
                    f"📅 Created: {_dt(r.created_at)}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "subscriptions":
        try:
            from database.models import Subscription
            r = session.query(Subscription).filter(Subscription.id == int(rec_id)).first()
            if r:
                st = _safe_str(getattr(r, "status", None))
                lines = [
                    f"🔄 Subscription ID: <code>{r.id}</code>",
                    f"👤 User ID: <code>{r.user_id}</code>",
                    f"📊 Status: {st}",
                    f"📅 Created: {_dt(r.created_at)}",
                    f"📅 Expires: {_dt(getattr(r, 'expires_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "vip_users":
        try:
            from database.models import UserVipTier, User as _U
            r = session.query(UserVipTier).filter(UserVipTier.id == int(rec_id)).first()
            if r:
                u = session.query(_U).filter(_U.id == r.user_id).first()
                uname = (u.username if u else None) or f"User {r.user_id}"
                lines = [
                    f"👑 VIP Record ID: <code>{r.id}</code>",
                    f"👤 User: @{uname} (ID:{r.user_id})",
                    f"🏆 Tier ID: {r.tier_id}",
                    f"📅 Assigned: {_dt(getattr(r, 'assigned_at', None))}",
                    f"📅 Expires: {_dt(getattr(r, 'expires_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "referrals":
        try:
            if str(rec_id).startswith("c"):
                from database.models import ReferralCommission
                rid = int(str(rec_id)[1:])
                r = session.query(ReferralCommission).filter(ReferralCommission.id == rid).first()
                if r:
                    lines = [
                        f"👥 Commission ID: <code>{r.id}</code>",
                        f"👤 Referrer ID: <code>{r.referrer_id}</code>",
                        f"👤 Referred ID: <code>{r.referred_id}</code>",
                        f"🧾 Order ID: <code>{r.order_id or '—'}</code>",
                        f"💰 Commission: ${r.commission_amount:.2f} ({r.commission_rate*100:.1f}%)",
                        f"📊 Status: {r.status}",
                        f"📅 Created: {_dt(r.created_at)}",
                    ]
            else:
                from database.models import ReferralReward
                r = session.query(ReferralReward).filter(ReferralReward.id == int(rec_id)).first()
                if r:
                    lines = [
                        f"👥 Referral Reward ID: <code>{r.id}</code>",
                        f"👤 Referrer ID: <code>{r.referrer_id}</code>",
                        f"👤 Referred ID: <code>{r.referred_id}</code>",
                        f"🧾 Order ID: <code>{r.order_id or '—'}</code>",
                        f"💰 Reward: ${r.amount:.2f}",
                        f"📅 Created: {_dt(r.created_at)}",
                    ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "delivery_logs":
        try:
            from database.models import DeliveryJob
            r = session.query(DeliveryJob).filter(DeliveryJob.id == int(rec_id)).first()
            if r:
                lines = [
                    f"📬 Delivery ID: <code>{r.id}</code>",
                    f"🧾 Order ID: <code>{getattr(r, 'order_id', '—')}</code>",
                    f"📊 Status: {_safe_str(getattr(r, 'status', None))}",
                    f"🔄 Retries: {_safe_str(getattr(r, 'retry_count', None))}",
                    f"📅 Created: {_dt(getattr(r, 'created_at', None))}",
                    f"📅 Delivered: {_dt(getattr(r, 'delivered_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    elif module == "categories":
        try:
            from database.models import Category
            r = session.query(Category).filter(Category.id == int(rec_id)).first()
            if r:
                lines = [
                    f"📂 Category ID: <code>{r.id}</code>",
                    f"📛 Name: <b>{r.name}</b>",
                    f"📅 Created: {_dt(getattr(r, 'created_at', None))}",
                ]
        except Exception as e:
            lines = [f"Error: {e}"]

    else:
        lines = [f"Module <b>{module}</b> — record <code>#{rec_id}</code>",
                 "No detail view configured for this module."]

    return lines


# ─── Save search conversation ─────────────────────────────────────────────────

async def gse_save_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")           # gse:save_prompt:{qhash}
    qhash = parts[2]
    stored = _load_results(context, qhash)
    if not stored:
        await q.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    context.user_data["gse_save_query"]   = stored["query"]
    context.user_data["gse_save_modules"] = stored["modules"]

    await _edit(update,
                f"⭐ <b>Save Search</b>\n\n"
                f"Query: <code>{stored['query']}</code>\n\n"
                "Enter a label for this saved search, or /skip to use the query as label:",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",
                                                             callback_data="gse:cancel_save")]]))
    return GSE_SAVE_LBL


async def gse_save_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    label = None if text == "/skip" else text[:128]
    query   = context.user_data.pop("gse_save_query", "")
    modules = context.user_data.pop("gse_save_modules", ALL_MODULE_SLUGS)
    admin_id = update.effective_user.id

    search(query, modules=modules, admin_telegram_id=admin_id)
    history = get_history(admin_id, limit=1)
    if history:
        save_search(history[0]["id"], label=label)

    await update.message.reply_text(
        f"✅ Search saved: <code>{label or query}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Saved Searches", callback_data="gse:saved")],
            [InlineKeyboardButton("🏠 Search Menu",    callback_data="gse:menu")],
        ]))
    return ConversationHandler.END


async def gse_cancel_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("Cancelled")
    context.user_data.pop("gse_save_query", None)
    context.user_data.pop("gse_save_modules", None)
    await _edit(update, "❌ Save cancelled.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Search Menu",
                                                             callback_data="gse:menu")]]))
    return ConversationHandler.END


# ─── Filter panel & Date filter conversation ──────────────────────────────────

async def gse_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the filter panel with active filter values."""
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    active = _get_filters(context)
    sort = _get_sort(context)

    def _val(k):
        v = active.get(k)
        return f" <b>[{v}]</b>" if v else ""

    text = (
        "🔧 <b>SEARCH FILTERS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date from:{_val('date_from')}\n"
        f"📅 Date to:  {_val('date_to')}\n"
        f"📊 Status:   {_val('status')}\n"
        f"⭐ Min rating:{_val('rating')}\n"
        f"💰 Min price:{_val('price_min')}\n"
        f"💰 Max price:{_val('price_max')}\n"
        f"🔀 Sort:     <b>{_SORT_LABELS.get(sort, sort)}</b>\n"
    )

    kb = [
        [InlineKeyboardButton("📅 Set Date Range",     callback_data="gse:filter_date")],
        [InlineKeyboardButton("📊 Status: Active",     callback_data="gse:fstatus:active"),
         InlineKeyboardButton("Status: All",            callback_data="gse:fstatus:")],
        [InlineKeyboardButton("⭐ Rating 4+",          callback_data="gse:frating:4"),
         InlineKeyboardButton("Rating 5",               callback_data="gse:frating:5"),
         InlineKeyboardButton("Any Rating",             callback_data="gse:frating:")],
        [InlineKeyboardButton("🔀 Cycle Sort",         callback_data="gse:sort")],
        [InlineKeyboardButton("🗑 Reset All Filters",  callback_data="gse:filter_reset")],
        [_back_btn("gse:menu")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def gse_filter_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start date-range filter conversation."""
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return ConversationHandler.END
    active = _get_filters(context)
    cur_from = active.get("date_from", "not set")
    cur_to   = active.get("date_to",   "not set")
    await q.edit_message_text(
        f"📅 <b>Date Range Filter</b>\n\n"
        f"Current: From <b>{cur_from}</b> → To <b>{cur_to}</b>\n\n"
        "Enter <b>From</b> date (YYYY-MM-DD), or /skip to keep current:",
        parse_mode="HTML"
    )
    return GSE_DATE_FROM


async def gse_got_date_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "/skip":
        pass
    else:
        try:
            datetime.strptime(text, "%Y-%m-%d")
            _set_filter(context, "date_from", text)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Use YYYY-MM-DD (e.g. 2026-01-15), or /skip:")
            return GSE_DATE_FROM
    await update.message.reply_text(
        "📅 Enter <b>To</b> date (YYYY-MM-DD), or /skip to keep current:",
        parse_mode="HTML"
    )
    return GSE_DATE_TO


async def gse_got_date_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text != "/skip":
        try:
            datetime.strptime(text, "%Y-%m-%d")
            _set_filter(context, "date_to", text)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Use YYYY-MM-DD (e.g. 2026-12-31), or /skip:")
            return GSE_DATE_TO

    active = _get_filters(context)
    await update.message.reply_text(
        f"✅ <b>Date range set</b>\n"
        f"From: <b>{active.get('date_from', 'none')}</b>\n"
        f"To:   <b>{active.get('date_to',   'none')}</b>\n\n"
        "Use 🔎 New Search to apply.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 New Search", callback_data="gse:new_search")],
            [InlineKeyboardButton("🔧 Filters",    callback_data="gse:filters")],
        ])
    )
    return ConversationHandler.END


async def gse_filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("❌ Filter entry cancelled.")
    return ConversationHandler.END


async def gse_filter_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set status filter: gse:fstatus:{value}"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":", 3)
    status_val = parts[2] if len(parts) > 2 else ""
    _set_filter(context, "status", status_val or None)
    await gse_filters(update, context)


async def gse_filter_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set rating filter: gse:frating:{value}"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":", 3)
    rating_val = parts[2] if len(parts) > 2 else ""
    _set_filter(context, "rating", int(rating_val) if rating_val.isdigit() else None)
    await gse_filters(update, context)


async def gse_filter_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("✅ Filters cleared.")
    context.user_data["gse_filters"] = {}
    context.user_data["gse_sort"] = "newest"
    await gse_filters(update, context)


# ─── Sort control ─────────────────────────────────────────────────────────────

async def gse_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle sort mode to the next option."""
    q = update.callback_query
    await q.answer()
    current = _get_sort(context)
    try:
        idx = _SORT_CYCLE.index(current)
        next_sort = _SORT_CYCLE[(idx + 1) % len(_SORT_CYCLE)]
    except ValueError:
        next_sort = "newest"
    _set_sort(context, next_sort)
    await q.answer(f"Sort: {_SORT_LABELS.get(next_sort, next_sort)}", show_alert=True)
    # Refresh last result if available
    qhash = context.user_data.get("gse_last_qhash")
    if qhash:
        stored = _load_results(context, qhash)
        if stored:
            active_filters = _get_filters(context)
            result = search(stored["query"], modules=stored["modules"],
                            filters=active_filters, sort=next_sort,
                            admin_telegram_id=None,
                            page=1, per_page=RESULTS_PER_PAGE)
            _store_results(context, qhash, result["results"], stored["query"], stored["modules"])
            await _edit(update,
                        _build_results_text(result, active_filters, next_sort),
                        _build_results_kb(result, qhash, 1, active_filters, next_sort))
            return
    # Fall back to menu
    await gse_menu(update, context)


# ─── History ──────────────────────────────────────────────────────────────────

async def gse_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    admin_id = update.effective_user.id
    history = get_history(admin_id, limit=20)

    if not history:
        await _edit(update, "🕐 <b>Recent Searches</b>\n\nNo searches yet.",
                    InlineKeyboardMarkup([[_back_btn("gse:menu")]]))
        return

    text = "🕐 <b>RECENT SEARCHES</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    for h in history[:15]:
        date_str = h["created_at"].strftime("%m-%d %H:%M") if h["created_at"] else ""
        saved_icon = "⭐" if h["is_saved"] else ""
        label = (h["label"] or h["query"])[:30]
        kb.append([InlineKeyboardButton(
            f"{saved_icon}🔍 {label} ({h['result_count']}) — {date_str}",
            callback_data=f"gse:re:{_qhash(h['query'])}:{h['query'][:30]}"
        )])

    kb.append([InlineKeyboardButton("🗑 Clear History", callback_data="gse:clear_hist")])
    kb.append([_back_btn("gse:menu")])
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def gse_clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    admin_id = update.effective_user.id
    deleted = clear_history(admin_id)
    await _edit(update, f"✅ Cleared {deleted} search record(s).",
                InlineKeyboardMarkup([[_back_btn("gse:menu")]]))


# ─── Saved searches ───────────────────────────────────────────────────────────

async def gse_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    admin_id = update.effective_user.id
    saved = get_saved_searches(admin_id)

    if not saved:
        await _edit(update, "⭐ <b>Saved Searches</b>\n\nNo saved searches yet.",
                    InlineKeyboardMarkup([[_back_btn("gse:menu")]]))
        return

    text = "⭐ <b>SAVED SEARCHES</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    for s in saved:
        date_str = s["created_at"].strftime("%m-%d") if s["created_at"] else ""
        label = (s["label"] or s["query"])[:35]
        kb.append([
            InlineKeyboardButton(
                f"⭐ {label} ({s['result_count']}) — {date_str}",
                callback_data=f"gse:re:{_qhash(s['query'])}:{s['query'][:30]}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"gse:del_saved:{s['id']}"),
        ])
    kb.append([_back_btn("gse:menu")])
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def gse_del_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    rec_id = int(parts[2])
    delete_search_record(rec_id)
    await gse_saved(update, context)


# ─── Re-run from history ──────────────────────────────────────────────────────

async def gse_rerun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🔍 Re-running search…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":", 4)
    if len(parts) < 4:
        await q.answer("❌ Invalid.", show_alert=True)
        return

    qhash = parts[2]
    query_prefix = parts[3]

    admin_id = update.effective_user.id
    history = get_history(admin_id, limit=50)
    full_query = next((h["query"] for h in history
                       if _qhash(h["query"]) == qhash), query_prefix)

    active_filters = _get_filters(context)
    sort = _get_sort(context)
    result = search(full_query, admin_telegram_id=admin_id,
                    filters=active_filters, sort=sort,
                    page=1, per_page=RESULTS_PER_PAGE)
    _store_results(context, qhash, result["results"], full_query, ALL_MODULE_SLUGS)
    context.user_data["gse_last_qhash"] = qhash

    await _edit(update,
                _build_results_text(result, active_filters, sort),
                _build_results_kb(result, qhash, page=1, active_filters=active_filters, sort=sort))


# ─── Statistics ───────────────────────────────────────────────────────────────

async def gse_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    stats = get_stats()
    popular_lines = "\n".join(
        f"  {i+1}. <code>{p[0]}</code> — {p[1]}×"
        for i, p in enumerate(stats["popular"])
    ) or "  (none yet)"
    recent_lines = "\n".join(
        f"  • <code>{r}</code>" for r in stats["recent"]
    ) or "  (none yet)"

    text = (
        "📊 <b>SEARCH STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 Total searches:   <b>{stats['total']}</b>\n"
        f"📅 Today:            <b>{stats['today']}</b>\n"
        f"📆 This week:        <b>{stats['weekly']}</b>\n"
        f"⭐ Saved searches:   <b>{stats['saved']}</b>\n"
        f"⏱ Avg search time:  <b>{stats['avg_ms']} ms</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>Popular searches:</b>\n"
        f"{popular_lines}\n\n"
        "🕐 <b>Recent searches:</b>\n"
        f"{recent_lines}"
    )
    await _edit(update, text, InlineKeyboardMarkup([[_back_btn("gse:menu")]]))


# ─── Settings ────────────────────────────────────────────────────────────────

async def gse_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    status      = cfg.get("gse_status", "enabled")
    max_results = cfg.get_int("gse_max_results", 50)
    fuzzy       = cfg.get_bool("gse_fuzzy", True)
    keep_hist   = cfg.get_bool("gse_keep_history", True)

    text = (
        "⚙️ <b>Search Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status:       <b>{status.upper()}</b>\n"
        f"Max results:  <b>{max_results}</b>\n"
        f"Fuzzy search: <b>{'✅ On' if fuzzy else '❌ Off'}</b>\n"
        f"Keep history: <b>{'✅ On' if keep_hist else '❌ Off'}</b>\n"
    )

    def _s(v1, v2) -> str:
        return "✅" if v1 == v2 else "○"

    kb = [
        [InlineKeyboardButton(f"{_s(status,'enabled')} 🟢 Enable",
                              callback_data="gse:set:gse_status:enabled"),
         InlineKeyboardButton(f"{_s(status,'maintenance')} 🟡 Maintenance",
                              callback_data="gse:set:gse_status:maintenance"),
         InlineKeyboardButton(f"{_s(status,'disabled')} 🔴 Disable",
                              callback_data="gse:set:gse_status:disabled")],
        [InlineKeyboardButton(f"{'✅' if fuzzy else '○'} Fuzzy Search",
                              callback_data="gse:toggle:gse_fuzzy"),
         InlineKeyboardButton(f"{'✅' if keep_hist else '○'} Keep History",
                              callback_data="gse:toggle:gse_keep_history")],
        [_back_btn("gse:menu")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def gse_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return
    parts = q.data.split(":")
    key, value = parts[2], parts[3]
    cfg.set(key, value)
    try:
        log_admin_action(update.effective_user.id, "gse_settings", details=f"{key}={value}")
    except Exception:
        pass
    await gse_settings(update, context)


async def gse_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return
    parts = q.data.split(":")
    key = parts[2]
    current = cfg.get_bool(key, True)
    cfg.set(key, "false" if current else "true")
    await gse_settings(update, context)


# ─── Module filter picker ─────────────────────────────────────────────────────

async def gse_mod_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    selected = context.user_data.get("gse_modules", ALL_MODULE_SLUGS)
    kb = []
    row = []
    for slug, meta in SEARCH_MODULES.items():
        is_sel = slug in selected
        row.append(InlineKeyboardButton(
            f"{'✅' if is_sel else '○'} {meta['emoji']} {meta['label']}",
            callback_data=f"gse:mod_tog:{slug}"
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("✅ Select All", callback_data="gse:mod_all"),
               InlineKeyboardButton("○ Clear All",   callback_data="gse:mod_none")])
    kb.append([InlineKeyboardButton("🔎 Search Now", callback_data="gse:new_search"),
               _back_btn("gse:menu")])

    await _edit(update, "🗂 <b>Module Filter</b>\n\nSelect which modules to search:",
                InlineKeyboardMarkup(kb))


async def gse_mod_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    slug = parts[2]
    selected = list(context.user_data.get("gse_modules", ALL_MODULE_SLUGS))
    if slug in selected:
        selected.remove(slug)
    else:
        selected.append(slug)
    context.user_data["gse_modules"] = selected or ALL_MODULE_SLUGS
    await gse_mod_filter(update, context)


async def gse_mod_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["gse_modules"] = ALL_MODULE_SLUGS
    await gse_mod_filter(update, context)


async def gse_mod_none(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["gse_modules"] = []
    await gse_mod_filter(update, context)


# ─── No-op ────────────────────────────────────────────────────────────────────

async def gse_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ─── Handler registration ─────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all Global Search Engine handlers. Called from bot.py main()."""

    # ── Main search conversation ──────────────────────────────────────────────
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(gse_new_search, pattern=r"^gse:new_search$")],
        states={
            GSE_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gse_query_recv),
                CallbackQueryHandler(gse_cancel_search, pattern=r"^gse:cancel_search$"),
                # Allow entering the filter panel during query input without breaking conv
                CallbackQueryHandler(gse_filters, pattern=r"^gse:filters$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(gse_cancel_search, pattern=r"^gse:cancel_search$"),
            CommandHandler("cancel", gse_cancel_search),
        ],
        per_message=False,
        allow_reentry=True,
        name="gse_search",
    )

    # ── Save search label conversation ────────────────────────────────────────
    save_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(gse_save_prompt, pattern=r"^gse:save_prompt:")],
        states={
            GSE_SAVE_LBL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gse_save_label),
                MessageHandler(filters.Regex(r"^/skip$"), gse_save_label),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(gse_cancel_save, pattern=r"^gse:cancel_save$"),
            CommandHandler("cancel", gse_cancel_save),
        ],
        per_message=False,
        allow_reentry=True,
        name="gse_save",
    )

    # ── Date range filter conversation ────────────────────────────────────────
    date_filter_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(gse_filter_date_start, pattern=r"^gse:filter_date$")],
        states={
            GSE_DATE_FROM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gse_got_date_from),
                CommandHandler("skip", gse_got_date_from),
            ],
            GSE_DATE_TO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gse_got_date_to),
                CommandHandler("skip", gse_got_date_to),
            ],
        },
        fallbacks=[CommandHandler("cancel", gse_filter_cancel)],
        per_message=False,
        allow_reentry=True,
        name="gse_date_filter",
    )

    application.add_handler(search_conv)
    application.add_handler(save_conv)
    application.add_handler(date_filter_conv)

    # ── Plain callback handlers ───────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(gse_menu,          pattern=r"^gse:menu$"))
    application.add_handler(CallbackQueryHandler(gse_page,          pattern=r"^gse:page:"))
    application.add_handler(CallbackQueryHandler(gse_detail,        pattern=r"^gse:det:"))
    application.add_handler(CallbackQueryHandler(gse_history,       pattern=r"^gse:history$"))
    application.add_handler(CallbackQueryHandler(gse_clear_history, pattern=r"^gse:clear_hist$"))
    application.add_handler(CallbackQueryHandler(gse_saved,         pattern=r"^gse:saved$"))
    application.add_handler(CallbackQueryHandler(gse_del_saved,     pattern=r"^gse:del_saved:"))
    application.add_handler(CallbackQueryHandler(gse_rerun,         pattern=r"^gse:re:"))
    application.add_handler(CallbackQueryHandler(gse_stats,         pattern=r"^gse:stats$"))
    application.add_handler(CallbackQueryHandler(gse_settings,      pattern=r"^gse:settings$"))
    application.add_handler(CallbackQueryHandler(gse_set,           pattern=r"^gse:set:"))
    application.add_handler(CallbackQueryHandler(gse_toggle,        pattern=r"^gse:toggle:"))
    application.add_handler(CallbackQueryHandler(gse_mod_filter,    pattern=r"^gse:mod_filter$"))
    application.add_handler(CallbackQueryHandler(gse_mod_toggle,    pattern=r"^gse:mod_tog:"))
    application.add_handler(CallbackQueryHandler(gse_mod_all,       pattern=r"^gse:mod_all$"))
    application.add_handler(CallbackQueryHandler(gse_mod_none,      pattern=r"^gse:mod_none$"))
    application.add_handler(CallbackQueryHandler(gse_noop,          pattern=r"^gse:noop$"))
    # V45: Filters & Sort
    application.add_handler(CallbackQueryHandler(gse_filters,       pattern=r"^gse:filters$"))
    application.add_handler(CallbackQueryHandler(gse_sort,          pattern=r"^gse:sort$"))
    application.add_handler(CallbackQueryHandler(gse_filter_status, pattern=r"^gse:fstatus:"))
    application.add_handler(CallbackQueryHandler(gse_filter_rating, pattern=r"^gse:frating:"))
    application.add_handler(CallbackQueryHandler(gse_filter_reset,  pattern=r"^gse:filter_reset$"))

    logger.info("V45: Global Search Engine handlers registered (27 modules, date filter, sort).")
