"""V45 — Admin Restock Notification Management.

Callback namespace: rsn:*
Lets admins view subscribers, pending/sent notifications, trigger bulk
notifications, and see delivery statistics.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes

from services.restock_service import (
    get_stats, get_all_subscriptions, get_pending_notifications,
    get_notification_logs, get_subscribers, process_restock_notifications,
    bulk_notify_subscribers,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

_PAGE = 10


def _back(to: str = "rsn:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


async def _check(update: Update) -> bool:
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


# ─── Menu ─────────────────────────────────────────────────────────────────────

async def rsn_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    stats = await asyncio.to_thread(get_stats)
    text = (
        "🔔 <b>RESTOCK NOTIFICATIONS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Total subscriptions: <b>{stats['total_subscriptions']}</b>\n"
        f"⏳ Pending notifications: <b>{stats['pending']}</b>\n"
        f"✅ Notified: <b>{stats['notified']}</b>\n"
        f"📬 Sent: <b>{stats['sent_notifications']}</b>  "
        f"❌ Failed: <b>{stats['failed_notifications']}</b>\n"
        f"👁 Products watched: <b>{stats['products_watched']}</b>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Subscriptions", callback_data="rsn:all_subs:1")],
        [InlineKeyboardButton("⏳ Pending Notifications", callback_data="rsn:pending")],
        [InlineKeyboardButton("📜 Notification Log",   callback_data="rsn:log:1")],
        [InlineKeyboardButton("📤 Trigger All Pending", callback_data="rsn:trigger_all")],
        [_back("acc:root")],
    ])
    await _edit(update, text, kb)


# ─── All Subscriptions ────────────────────────────────────────────────────────

async def rsn_all_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    parts = q.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1

    data = await asyncio.to_thread(get_all_subscriptions, page, _PAGE)
    items = data["items"]
    total = data["total"]
    pages = data["pages"]

    if not items:
        text = "📋 <b>All Subscriptions</b>\n\nNo subscriptions found."
    else:
        lines = [f"📋 <b>All Subscriptions</b> (page {page}/{pages}, total {total})\n"]
        for it in items:
            notif = "✅" if it["notified"] else "⏳"
            uname = it["username"] or f"ID:{it['telegram_id']}"
            lines.append(f"{notif} <b>{it['product_name'][:30]}</b> — @{uname}")
        text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"rsn:all_subs:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"rsn:all_subs:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


# ─── Pending Notifications ────────────────────────────────────────────────────

async def rsn_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    pending = await asyncio.to_thread(get_pending_notifications)

    if not pending:
        text = "⏳ <b>Pending Notifications</b>\n\nNo pending notifications."
    else:
        lines = [f"⏳ <b>Pending Notifications</b> ({len(pending)} total)\n"]
        for it in pending[:20]:
            uname = it["username"] or f"TG:{it['telegram_id']}"
            lines.append(f"📦 <b>{it['product_name'][:30]}</b> → @{uname}")
        if len(pending) > 20:
            lines.append(f"\n… and {len(pending)-20} more")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Send All Now", callback_data="rsn:trigger_all")],
        [_back()],
    ])
    await _edit(update, text, kb)


# ─── Notification Log ─────────────────────────────────────────────────────────

async def rsn_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    parts = q.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1

    data = await asyncio.to_thread(get_notification_logs, None, page, _PAGE)
    items = data["items"]
    pages = data["pages"]
    total = data["total"]

    if not items:
        text = "📜 <b>Notification Log</b>\n\nNo log entries."
    else:
        lines = [f"📜 <b>Notification Log</b> (page {page}/{pages}, total {total})\n"]
        for it in items:
            icon = "✅" if it["status"] == "sent" else "❌"
            ts = it["sent_at"].strftime("%m-%d %H:%M") if it["sent_at"] else "—"
            lines.append(
                f"{icon} {it['product_name'][:25]} | TG:{it['telegram_id']} | {ts}"
            )
        text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"rsn:log:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"rsn:log:{page+1}"))
    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


# ─── Trigger All Pending ──────────────────────────────────────────────────────

async def rsn_trigger_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⏳ Sending notifications…")
    if not await _check(update):
        return

    sent = await process_restock_notifications(context)
    admin_id = update.effective_user.id
    log_admin_action(admin_id, "restock_trigger_all",
                     details=f"sent={sent}", module="restock_notifications")

    text = (
        f"✅ <b>Restock Notifications Triggered</b>\n\n"
        f"📬 Sent: <b>{sent}</b> notifications"
    )
    kb = InlineKeyboardMarkup([[_back()]])
    await _edit(update, text, kb)


# ─── Register ─────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    application.add_handler(CallbackQueryHandler(rsn_menu,        pattern=r"^rsn:menu$"))
    application.add_handler(CallbackQueryHandler(rsn_all_subs,    pattern=r"^rsn:all_subs:"))
    application.add_handler(CallbackQueryHandler(rsn_pending,     pattern=r"^rsn:pending$"))
    application.add_handler(CallbackQueryHandler(rsn_log,         pattern=r"^rsn:log:"))
    application.add_handler(CallbackQueryHandler(rsn_trigger_all, pattern=r"^rsn:trigger_all$"))
    logger.info("V45: Restock Notification admin handlers registered.")
