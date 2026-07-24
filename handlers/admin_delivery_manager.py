"""V36 — Delivery Management System admin handler.

Namespace: ``dms:*``

Callbacks handled
─────────────────
dms:menu                  — Dashboard with live stats
dms:list:STATUS:PAGE      — Paginated list filtered by status
dms:view:ID               — Detail view of one DeliveryRecord
dms:retry:ID              — Reset failed record to pending
dms:resend:ID             — Re-send stored content to user
dms:cancel:ID             — Cancel a pending/processing record
dms:replace:ID            — Start ConversationHandler: enter new content
dms:search:PAGE           — Show search results (after search conv)
dms:export:FORMAT         — Export CSV or JSON (answers as file)
dms:settings              — Settings menu
dms:settings:status:VAL   — Set enabled / maintenance / disabled
dms:settings:toggle:KEY   — Flip a bool bot_config key
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import get_db_session
from utils.helpers import is_admin
from utils.audit import log_admin_action
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ConversationHandler states
_SEARCH_INPUT   = 10
_REPLACE_INPUT  = 11

# Status display config
_STATUS_EMOJI = {
    "pending":    "⏳",
    "preparing":  "🔧",
    "processing": "⚙️",
    "delivered":  "✅",
    "completed":  "🏁",
    "failed":     "❌",
    "cancelled":  "🚫",
    "expired":    "⌛",
    "refunded":   "↩️",
}

_ALL_STATUSES = [
    "pending", "preparing", "processing",
    "delivered", "completed", "failed",
    "cancelled", "expired", "refunded",
]

_SETTINGS_BOOL_KEYS = [
    ("delivery_auto_enabled",          "⚡ Automatic Delivery"),
    ("delivery_manual_enabled",        "👤 Manual Delivery"),
    ("delivery_retry_enabled",         "🔁 Retry Failed"),
    ("delivery_notifications_enabled", "🔔 User Notifications"),
    ("delivery_secure_links_enabled",  "🔒 Secure Download Links"),
    ("delivery_one_time_download",     "1️⃣ One-Time Downloads"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await q.message.reply_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )


def _back_menu() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ DMS Menu", callback_data="dms:menu")


def _back_root() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Control Center", callback_data="acc:root")


def _nav_buttons(base_cb: str, page: int, total_pages: int):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"{base_cb}:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="dms:noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"{base_cb}:{page + 1}"))
    return row


def _feature_blocked(update) -> Optional[str]:
    """Return a reason string if DMS is not operational, else None."""
    st = cfg.get("delivery_manager_status", "enabled")
    if st == "disabled":
        return "❌ Delivery Management is currently <b>disabled</b>."
    if st == "maintenance":
        return "🟡 Delivery Management is in <b>maintenance mode</b>. Read-only access only."
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def dms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return

    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    with get_db_session() as s:
        from services.delivery_management_service import get_dashboard_stats
        stats = get_dashboard_stats(s)

    st_badge = cfg.get("delivery_manager_status", "enabled")
    badge = {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance", "disabled": "🔴 Disabled"}.get(st_badge, st_badge)

    text = (
        f"📦 <b>Delivery Management System</b>  [{badge}]\n"
        f"{'─' * 34}\n"
        f"📊 <b>Dashboard</b>\n\n"
        f"  ⏳ Pending / Processing : <b>{stats['pending']}</b>\n"
        f"  ✅ Delivered / Completed: <b>{stats['delivered']}</b>\n"
        f"  ❌ Failed               : <b>{stats['failed']}</b>\n"
        f"  🔁 Retry Queue          : <b>{stats['retry_queue']}</b>\n"
        f"  🚫 Cancelled            : <b>{stats['cancelled']}</b>\n"
        f"  ⌛ Expired              : <b>{stats['expired']}</b>\n"
        f"  ↩️ Refunded             : <b>{stats['refunded']}</b>\n\n"
        f"  📅 Today's Deliveries   : <b>{stats['today']}</b>\n"
        f"  🎯 Success Rate         : <b>{stats['success_rate']}%</b>\n"
        f"  ⏱ Avg Delivery Time    : <b>{stats['avg_time']}</b>\n"
    )

    kb = [
        [
            InlineKeyboardButton("⏳ Pending",   callback_data="dms:list:pending:0"),
            InlineKeyboardButton("❌ Failed",    callback_data="dms:list:failed:0"),
        ],
        [
            InlineKeyboardButton("✅ Delivered", callback_data="dms:list:delivered:0"),
            InlineKeyboardButton("🏁 Completed", callback_data="dms:list:completed:0"),
        ],
        [
            InlineKeyboardButton("🚫 Cancelled", callback_data="dms:list:cancelled:0"),
            InlineKeyboardButton("⌛ Expired",   callback_data="dms:list:expired:0"),
        ],
        [InlineKeyboardButton("🔍 Search Deliveries",    callback_data="dms:search_start")],
        [
            InlineKeyboardButton("📤 Export CSV",  callback_data="dms:export:csv"),
            InlineKeyboardButton("📤 Export JSON", callback_data="dms:export:json"),
        ],
        [InlineKeyboardButton("⚙️ Settings",              callback_data="dms:settings")],
        [_back_root()],
    ]
    await _send(update, text, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────────
# List view
# ─────────────────────────────────────────────────────────────────────────────

async def _dms_list(update: Update, status: str, page: int) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return

    emoji = _STATUS_EMOJI.get(status, "📋")
    with get_db_session() as s:
        from services.delivery_management_service import list_records
        records, page, total_pages = list_records(s, status=status, page=page)

    lines = [f"📦 <b>Deliveries — {emoji} {status.upper()}</b>",
             f"Page {page + 1}/{total_pages}", ""]
    if not records:
        lines.append("No deliveries found.")
    for r in records:
        dt = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "—"
        lines.append(
            f"  <b>#{r.id}</b> · order {r.order_id or '—'} · "
            f"{r.delivery_type} · {dt}"
        )

    kb = []
    for r in records:
        kb.append([InlineKeyboardButton(
            f"#{r.id} · {r.delivery_type} · {r.status}",
            callback_data=f"dms:view:{r.id}",
        )])

    # Status tab bar (2 per row)
    tabs = [InlineKeyboardButton(
        ("• " + s.upper()) if s == status else s.upper(),
        callback_data=f"dms:list:{s}:0",
    ) for s in _ALL_STATUSES]
    for i in range(0, len(tabs), 3):
        kb.append(tabs[i:i + 3])

    if total_pages > 1:
        kb.append(_nav_buttons(f"dms:list:{status}", page, total_pages))
    kb.append([_back_menu(), _back_root()])

    await _send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────────
# Detail view
# ─────────────────────────────────────────────────────────────────────────────

async def _dms_view(update: Update, record_id: int) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return

    with get_db_session() as s:
        from services.delivery_management_service import get_record
        rec = get_record(s, record_id)
        if not rec:
            await _send(update, "❌ Delivery record not found.",
                        InlineKeyboardMarkup([[_back_menu()]]))
            return

        emoji = _STATUS_EMOJI.get(rec.status, "📋")

        def _dt(d):
            return d.strftime("%Y-%m-%d %H:%M UTC") if d else "—"

        content_preview = ""
        if rec.delivered_content:
            raw = rec.delivered_content[:200]
            content_preview = f"\n\n📋 <b>Content preview:</b>\n<code>{raw}</code>{'…' if len(rec.delivered_content) > 200 else ''}"

        text = (
            f"📦 <b>Delivery Record #{rec.id}</b>\n"
            f"{'─' * 30}\n"
            f"🆔 Secure ID : <code>{rec.secure_id}</code>\n"
            f"📦 Status    : {emoji} <b>{rec.status.upper()}</b>\n"
            f"🛒 Order     : #{rec.order_id or '—'}\n"
            f"👤 User ID   : <code>{rec.user_id}</code>\n"
            f"🏷 Product   : #{rec.product_id or '—'}\n"
            f"📬 Type      : {rec.delivery_type}\n"
            f"🚀 Method    : {rec.delivery_method}\n"
            f"🔁 Retries   : {rec.retry_count}/{rec.max_retries}\n"
            f"👮 Admin     : {rec.admin_id or '—'}\n"
            f"⬇️ Downloads : {rec.download_count}"
            + (f"/{rec.download_limit}" if rec.download_limit else "") + "\n"
            f"📅 Created   : {_dt(rec.created_at)}\n"
            f"✅ Delivered : {_dt(rec.delivered_at)}\n"
            f"🏁 Completed : {_dt(rec.completed_at)}\n"
            + (f"❌ Error     : {rec.last_error}\n" if rec.last_error else "")
            + content_preview
        )

        kb = []
        if rec.status in ("failed", "expired", "cancelled"):
            kb.append([InlineKeyboardButton(
                "🔁 Retry Delivery", callback_data=f"dms:retry:{rec.id}"
            )])
        if rec.delivered_content:
            kb.append([InlineKeyboardButton(
                "📤 Resend to User", callback_data=f"dms:resend:{rec.id}"
            )])
        if rec.status not in ("completed", "cancelled", "refunded"):
            kb.append([InlineKeyboardButton(
                "🔄 Replace Content", callback_data=f"dms:replace:{rec.id}"
            )])
        if rec.status not in ("completed", "cancelled", "refunded"):
            kb.append([InlineKeyboardButton(
                "🚫 Cancel Delivery", callback_data=f"dms:cancel:{rec.id}"
            )])
        back_status = rec.status
    kb.append([
        InlineKeyboardButton("⬅️ Back", callback_data=f"dms:list:{back_status}:0"),
        _back_menu(),
    ])
    await _send(update, text, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────

async def _dms_retry(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: int) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    blocked = _feature_blocked(update)
    if blocked and "maintenance" not in (cfg.get("delivery_manager_status", "") or ""):
        pass  # retry allowed even in maintenance

    with get_db_session() as s:
        from services.delivery_management_service import retry_delivery
        ok, msg = retry_delivery(s, record_id, uid)

    prefix = "✅" if ok else "❌"
    if q:
        await q.answer(f"{prefix} {msg}", show_alert=True)
    await _dms_view(update, record_id)


async def _dms_resend(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: int) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⏳ Resending…")

    if not cfg.get_bool("delivery_manual_enabled", True):
        if q:
            await q.answer("❌ Manual delivery is disabled.", show_alert=True)
        return

    with get_db_session() as s:
        from services.delivery_management_service import resend_delivery
        ok, msg = await resend_delivery(s, record_id, uid, context.bot)

    if q:
        await q.answer(("✅ " if ok else "❌ ") + msg, show_alert=True)
    await _dms_view(update, record_id)


async def _dms_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: int) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    with get_db_session() as s:
        from services.delivery_management_service import cancel_delivery
        ok, msg = cancel_delivery(s, record_id, uid)

    if q:
        await q.answer(("✅ " if ok else "❌ ") + msg, show_alert=True)
    await _dms_view(update, record_id)


# ─────────────────────────────────────────────────────────────────────────────
# Replace content ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

async def _replace_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return ConversationHandler.END

    q = update.callback_query
    await q.answer()
    record_id = int(q.data.split(":")[-1])
    context.user_data["dms_replace_id"] = record_id

    await q.message.reply_text(
        f"✏️ <b>Replace Delivery Content — Record #{record_id}</b>\n\n"
        "Send the new content to deliver to the user.\n"
        "This will replace the existing content and immediately resend it.\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _REPLACE_INPUT


async def _replace_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    new_content = (update.message.text or "").strip()
    record_id   = context.user_data.get("dms_replace_id")

    if not new_content or not record_id:
        await update.message.reply_text("❌ No content provided. Cancelled.")
        return ConversationHandler.END

    with get_db_session() as s:
        from services.delivery_management_service import replace_content
        ok, msg = await replace_content(s, record_id, new_content, uid, context.bot)

    await update.message.reply_text(("✅ " if ok else "❌ ") + msg, parse_mode="HTML")
    context.user_data.pop("dms_replace_id", None)
    return ConversationHandler.END


async def _replace_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("dms_replace_id", None)
    await update.message.reply_text("❌ Replace cancelled.")
    return ConversationHandler.END


def build_replace_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(_replace_start, pattern=r"^dms:replace:\d+$")],
        states={
            _REPLACE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _replace_receive),
                MessageHandler(filters.COMMAND & filters.Regex(r"^/cancel"), _replace_cancel),
            ],
        },
        fallbacks=[MessageHandler(filters.COMMAND, _replace_cancel)],
        name="dms_replace_conv",
        persistent=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Search ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

async def _search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return ConversationHandler.END

    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🔍 <b>Search Deliveries</b>\n\n"
        "Enter: order ID, user ID, product ID, delivery type, or status.\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _SEARCH_INPUT


async def _search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("❌ Empty query. Search cancelled.")
        return ConversationHandler.END

    with get_db_session() as s:
        from services.delivery_management_service import search_records
        records, page, total_pages = search_records(s, query, page=0)

    if not records:
        await update.message.reply_text(
            f"🔍 No deliveries found for <code>{query}</code>.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    lines = [f"🔍 <b>Search Results for</b> <code>{query}</code>",
             f"Found {len(records)} record(s) (page 1/{total_pages})", ""]
    kb = []
    for r in records:
        emoji = _STATUS_EMOJI.get(r.status, "📋")
        dt    = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "—"
        lines.append(f"  <b>#{r.id}</b> · {r.delivery_type} · {emoji}{r.status} · {dt}")
        kb.append([InlineKeyboardButton(
            f"#{r.id} · {r.delivery_type} · {r.status}",
            callback_data=f"dms:view:{r.id}",
        )])
    kb.append([InlineKeyboardButton("⬅️ DMS Menu", callback_data="dms:menu")])

    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
    )
    return ConversationHandler.END


async def _search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Search cancelled.")
    return ConversationHandler.END


def build_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(_search_start, pattern=r"^dms:search_start$")],
        states={
            _SEARCH_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _search_receive),
                MessageHandler(filters.COMMAND & filters.Regex(r"^/cancel"), _search_cancel),
            ],
        },
        fallbacks=[MessageHandler(filters.COMMAND, _search_cancel)],
        name="dms_search_conv",
        persistent=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────

async def _dms_export(update: Update, context: ContextTypes.DEFAULT_TYPE, fmt: str) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⏳ Generating export…")

    with get_db_session() as s:
        from services.delivery_management_service import export_logs
        data = export_logs(s, fmt=fmt)

    filename = f"delivery_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.{fmt}"
    chat_id  = update.effective_chat.id if update.effective_chat else uid
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(data),
        filename=filename,
        caption=f"📤 Delivery logs export — {fmt.upper()}\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    )
    try:
        log_admin_action(uid, "delivery_export",
                         f"format={fmt} bytes={len(data)}",
                         module="delivery_manager")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

async def _dms_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    st = cfg.get("delivery_manager_status", "enabled")
    max_retries  = cfg.get_int("delivery_max_retries", 3)
    retry_delay  = cfg.get_int("delivery_retry_delay_seconds", 300)
    link_expiry  = cfg.get_int("delivery_link_expiry_hours", 24)

    text = (
        "⚙️ <b>Delivery Manager — Settings</b>\n"
        f"{'─' * 32}\n\n"
        f"<b>System Status</b>\n"
        f"  Current: <b>{'🟢 Enabled' if st == 'enabled' else '🟡 Maintenance' if st == 'maintenance' else '🔴 Disabled'}</b>\n\n"
        f"<b>Retry Settings</b>\n"
        f"  Max Retries   : <b>{max_retries}</b>\n"
        f"  Retry Delay   : <b>{retry_delay}s</b>\n\n"
        f"<b>Download Links</b>\n"
        f"  Link Expiry   : <b>{link_expiry}h</b>\n"
    )

    kb = [
        # Status row
        [
            InlineKeyboardButton(
                "🟢 Enable" if st != "enabled" else "• 🟢 Enabled",
                callback_data="dms:settings:status:enabled",
            ),
            InlineKeyboardButton(
                "🟡 Maintenance" if st != "maintenance" else "• 🟡 Maint.",
                callback_data="dms:settings:status:maintenance",
            ),
            InlineKeyboardButton(
                "🔴 Disable" if st != "disabled" else "• 🔴 Disabled",
                callback_data="dms:settings:status:disabled",
            ),
        ],
    ]

    # Bool toggles
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        indicator = "✅" if val else "⬜"
        kb.append([InlineKeyboardButton(
            f"{indicator} {label}",
            callback_data=f"dms:settings:toggle:{key}",
        )])

    kb.append([_back_menu(), _back_root()])
    await _send(update, text, InlineKeyboardMarkup(kb))


async def _dms_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE, val: str) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    if val not in ("enabled", "maintenance", "disabled"):
        return
    cfg.set("delivery_manager_status", val)
    try:
        log_admin_action(uid, "delivery_manager_status_change",
                         f"new_status={val}", module="delivery_manager")
    except Exception:
        pass
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"✅ Status set to {val}.")
    await _dms_settings(update, context)


async def _dms_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await _deny(update)
        return
    current = cfg.get_bool(key, True)
    cfg.set(key, str(not current).lower())
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer(f"✅ {'Enabled' if not current else 'Disabled'}.")
    try:
        log_admin_action(uid, "delivery_settings_toggle",
                         f"key={key} new={'true' if not current else 'false'}",
                         module="delivery_manager")
    except Exception:
        pass
    await _dms_settings(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Central dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def dms_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    uid = update.effective_user.id if update.effective_user else 0
    if not _require_admin(uid):
        await q.answer("⛔ Access denied.", show_alert=True)
        return

    data  = q.data or ""
    parts = data.split(":")   # ["dms", action, ...]

    if len(parts) < 2:
        return

    action = parts[1]

    if action == "menu":
        await dms_menu(update, context)

    elif action == "noop":
        await q.answer()

    elif action == "list" and len(parts) >= 4:
        status = parts[2]
        page   = int(parts[3]) if parts[3].isdigit() else 0
        await q.answer()
        await _dms_list(update, status, page)

    elif action == "view" and len(parts) >= 3:
        await q.answer()
        await _dms_view(update, int(parts[2]))

    elif action == "retry" and len(parts) >= 3:
        await _dms_retry(update, context, int(parts[2]))

    elif action == "resend" and len(parts) >= 3:
        await _dms_resend(update, context, int(parts[2]))

    elif action == "cancel" and len(parts) >= 3:
        await _dms_cancel(update, context, int(parts[2]))

    elif action == "export" and len(parts) >= 3:
        await _dms_export(update, context, parts[2])

    elif action == "settings":
        if len(parts) == 2:
            await _dms_settings(update, context)
        elif parts[2] == "status" and len(parts) >= 4:
            await _dms_settings_status(update, context, parts[3])
        elif parts[2] == "toggle" and len(parts) >= 4:
            await _dms_settings_toggle(update, context, parts[3])
        else:
            await _dms_settings(update, context)

    else:
        await q.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all DMS handlers. Call BEFORE the acc_dispatch handler."""
    # ConversationHandlers must come first so they intercept messages
    application.add_handler(build_search_conv())
    application.add_handler(build_replace_conv())
    # Central dispatcher for all other dms:* callbacks
    application.add_handler(
        CallbackQueryHandler(dms_dispatch, pattern=r"^dms:.+$")
    )
