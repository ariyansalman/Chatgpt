"""V37 — Admin Notification Center handler.

Callback namespace: ``anc:*``

Callbacks handled
─────────────────
anc:menu                         — Dashboard + unread count
anc:list:FILTER:PAGE             — Paginated notification list
anc:view:ID                      — Detail view of one notification
anc:read:ID                      — Mark notification as read
anc:read_all                     — Mark all as read
anc:del:ID                       — Delete notification
anc:del_all:confirm              — Confirm delete all
anc:del_all:go:CATEGORY          — Delete all (optionally by category)
anc:pin:ID                       — Toggle pin
anc:arch:ID                      — Archive notification
anc:settings                     — Notification settings menu
anc:settings:status:VAL          — Set enabled/maintenance/disabled
anc:settings:toggle:KEY          — Flip a bool bot_config key
anc:filter:CATEGORY              — Filter list by category
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import AdminNotification
from services import notification_center_service as ncs
from utils.helpers import is_admin
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8

# Filter → label
_FILTERS = {
    "all":         "All",
    "unread":      "Unread",
    "pinned":      "Pinned",
    "archived":    "Archived",
    "orders":      "Orders",
    "payments":    "Payments",
    "withdrawals": "Withdrawals",
    "products":    "Products",
    "users":       "Users",
    "system":      "System",
    "security":    "Security",
    "api":         "API",
    "fraud":       "Fraud",
    "support":     "Support",
}

_SEVERITY_EMOJI = {
    "push":     "🔔",
    "in_bot":   "📨",
    "silent":   "🔕",
    "critical": "🚨",
}

_CATEGORY_EMOJI = {
    "orders":      "🧾",
    "payments":    "💳",
    "withdrawals": "💸",
    "products":    "📦",
    "users":       "👤",
    "system":      "⚙️",
    "security":    "🔐",
    "api":         "🔌",
    "fraud":       "🚨",
    "support":     "🎧",
}

# Settings bool keys shown in the settings panel
_SETTINGS_BOOL_KEYS = [
    ("notification_center_sound",       "🔊 Enable Sound"),
    ("notification_center_silent_mode", "🔕 Silent Mode (no Telegram messages)"),
    ("notification_center_auto_delete", "🗑 Auto Delete Old Notifications"),
    ("notif_nc_new_user",               "👤 New User Registration"),
    ("notif_nc_new_order",              "🧾 New Order"),
    ("notif_nc_payment_success",        "✅ Successful Payment"),
    ("notif_nc_payment_failed",         "❌ Failed Payment"),
    ("notif_nc_payment_pending",        "⏳ Pending Payment"),
    ("notif_nc_deposit",                "💰 Deposit Received"),
    ("notif_nc_withdrawal_request",     "💸 Withdrawal Request"),
    ("notif_nc_withdrawal_approved",    "✅ Withdrawal Approved"),
    ("notif_nc_withdrawal_rejected",    "❌ Withdrawal Rejected"),
    ("notif_nc_product_delivered",      "📦 Product Delivered"),
    ("notif_nc_refund_request",         "↩️ Refund Request"),
    ("notif_nc_support_ticket",         "🎧 Support Ticket"),
    ("notif_nc_low_stock",              "⚠️ Low Stock Alert"),
    ("notif_nc_out_of_stock",           "🚫 Product Out Of Stock"),
    ("notif_nc_payment_expired",        "⌛ Payment Expired"),
    ("notif_nc_payment_reversed",       "🔄 Payment Reversed"),
    ("notif_nc_order_delivered",        "✅ Order Delivered"),
    ("notif_nc_coupon_used",            "🎟 Coupon Used"),
    ("notif_nc_referral_reward",        "🤝 Referral Reward"),
    ("notif_nc_broadcast_done",         "📢 Broadcast Completed"),
    ("notif_nc_fraud_alert",            "🚨 Fraud Detection Alert"),
    ("notif_nc_api_failure",            "🔌 API Failure"),
    ("notif_nc_webhook_failure",        "📡 Webhook Failure"),
    ("notif_nc_db_error",               "🗄 Database Error"),
    ("notif_nc_tg_api_error",           "📱 Telegram API Error"),
    ("notif_nc_system_warning",         "⚠️ System Warning"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = update.callback_query
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
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


def _list_query(session, filt: str):
    q = session.query(AdminNotification)
    if filt == "unread":
        q = q.filter(AdminNotification.is_read == False, AdminNotification.is_archived == False)  # noqa: E712
    elif filt == "pinned":
        q = q.filter(AdminNotification.is_pinned == True)  # noqa: E712
    elif filt == "archived":
        q = q.filter(AdminNotification.is_archived == True)  # noqa: E712
    elif filt in _FILTERS and filt not in ("all", "unread", "pinned", "archived"):
        # category filter
        q = q.filter(AdminNotification.category == filt, AdminNotification.is_archived == False)  # noqa: E712
    else:
        q = q.filter(AdminNotification.is_archived == False)  # noqa: E712
    return q.order_by(
        AdminNotification.is_pinned.desc(),
        AdminNotification.created_at.desc(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Menu / Dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def anc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    stats = ncs.get_stats()
    status = cfg.get("notification_center_status", "enabled")
    status_emoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")

    text = (
        "🔔 <b>Admin Notification Center</b>\n\n"
        f"{status_emoji} Status: <b>{status.title()}</b>\n\n"
        f"📊 <b>Summary</b>\n"
        f"• Total notifications: <b>{stats['total']}</b>\n"
        f"• Unread: <b>{stats['unread']}</b>\n"
        f"• Pinned: <b>{stats['pinned']}</b>\n"
        f"• Archived: <b>{stats['archived']}</b>\n"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Notifications",  callback_data="anc:list:all:0"),
         InlineKeyboardButton(f"🔔 Unread ({stats['unread']})", callback_data="anc:list:unread:0")],
        [InlineKeyboardButton("📌 Pinned",             callback_data="anc:list:pinned:0"),
         InlineKeyboardButton("🗄 Archived",           callback_data="anc:list:archived:0")],
        [InlineKeyboardButton("🔍 Filter by Category", callback_data="anc:filter_menu")],
        [InlineKeyboardButton("✅ Mark All Read",       callback_data="anc:read_all"),
         InlineKeyboardButton("🗑 Delete All",         callback_data="anc:del_all:confirm")],
        [InlineKeyboardButton("⚙️ Settings",            callback_data="anc:settings")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    await _send(update, text, kb)


async def anc_filter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    rows = []
    cats = ["orders", "payments", "withdrawals", "products", "users",
            "system", "security", "api", "fraud", "support"]
    for i in range(0, len(cats), 2):
        row = []
        for cat in cats[i:i+2]:
            emoji = _CATEGORY_EMOJI.get(cat, "📌")
            row.append(InlineKeyboardButton(
                f"{emoji} {cat.title()}", callback_data=f"anc:list:{cat}:0"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="anc:menu")])
    text = "🔍 <b>Filter Notifications by Category</b>\n\nSelect a category:"
    await _send(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# List view
# ─────────────────────────────────────────────────────────────────────────────

async def anc_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts = q.data.split(":")  # anc:list:FILTER:PAGE
    filt = parts[2] if len(parts) > 2 else "all"
    page = int(parts[3]) if len(parts) > 3 else 0

    with get_db_session() as s:
        base_q = _list_query(s, filt)
        total  = base_q.count()
        rows   = base_q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

    if not rows:
        text = f"🔔 <b>Notifications — {_FILTERS.get(filt, filt).title()}</b>\n\nNo notifications found."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="anc:menu")]])
        await _send(update, text, kb); return

    lines = [f"🔔 <b>Notifications — {_FILTERS.get(filt, filt).title()}</b> (page {page+1})\n"]
    btns  = []
    for n in rows:
        sev   = _SEVERITY_EMOJI.get(n.severity, "🔔")
        read  = "" if n.is_read else " 🔵"
        pin   = " 📌" if n.is_pinned else ""
        ts    = n.created_at.strftime("%m/%d %H:%M")
        label = f"{sev}{read}{pin} {n.title[:30]}  [{ts}]"
        btns.append([InlineKeyboardButton(label, callback_data=f"anc:view:{n.id}")])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"anc:list:{filt}:{page-1}"))
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="anc:menu"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"anc:list:{filt}:{page+1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="anc:menu")])

    text = "\n".join(lines)
    await _send(update, text, InlineKeyboardMarkup(btns))


# ─────────────────────────────────────────────────────────────────────────────
# Detail view
# ─────────────────────────────────────────────────────────────────────────────

async def anc_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts = q.data.split(":")  # anc:view:ID
    nid = int(parts[2])

    with get_db_session() as s:
        n = s.query(AdminNotification).filter_by(id=nid).first()
        if not n:
            await q.answer("Notification not found.", show_alert=True); return

        sev_emoji  = _SEVERITY_EMOJI.get(n.severity, "🔔")
        cat_emoji  = _CATEGORY_EMOJI.get(n.category, "📌")
        read_mark  = "✅ Read" if n.is_read else "🔵 Unread"
        pin_mark   = "📌 Pinned" if n.is_pinned else ""
        arch_mark  = "🗄 Archived" if n.is_archived else ""
        ts         = n.created_at.strftime("%Y-%m-%d %H:%M UTC")

        text = (
            f"{sev_emoji} <b>{n.title}</b>\n\n"
            f"{n.body}\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏷 Type: <code>{n.event_type}</code>\n"
            f"{cat_emoji} Category: {n.category.title()}\n"
            f"🕐 Created: {ts}\n"
            f"📊 Status: {read_mark}  {pin_mark}  {arch_mark}\n"
        )
        if n.source_type and n.source_id:
            text += f"🔗 Source: {n.source_type} #{n.source_id}\n"

    # Auto-mark as read when viewed
    ncs.mark_read(nid)

    btns = [
        [InlineKeyboardButton("✅ Mark Read",  callback_data=f"anc:read:{nid}"),
         InlineKeyboardButton("📌 Pin/Unpin", callback_data=f"anc:pin:{nid}")],
        [InlineKeyboardButton("🗄 Archive",   callback_data=f"anc:arch:{nid}"),
         InlineKeyboardButton("🗑 Delete",    callback_data=f"anc:del:{nid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="anc:list:all:0")],
    ]
    await _send(update, text, InlineKeyboardMarkup(btns))


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────

async def anc_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    nid = int(q.data.split(":")[2])
    ncs.mark_read(nid)
    await q.answer("✅ Marked as read.")
    await anc_list(with_data(update, f"anc:list:all:0"), context)


async def anc_read_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    count = ncs.mark_all_read()
    log_admin_action(update.effective_user.id, "notification_center.mark_all_read",
                     details=f"Marked {count} notifications as read")
    await q.answer(f"✅ {count} notifications marked as read.")
    await anc_menu(with_data(update, "anc:menu"), context)


async def anc_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    nid = int(q.data.split(":")[2])
    ok = ncs.delete_notification(nid)
    if ok:
        log_admin_action(update.effective_user.id, "notification_center.delete", target_id=str(nid))
        await q.answer("🗑 Notification deleted.")
    else:
        await q.answer("Notification not found.", show_alert=True)
    await anc_list(with_data(update, "anc:list:all:0"), context)


async def anc_del_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    text = (
        "⚠️ <b>Delete All Notifications</b>\n\n"
        "This will permanently delete all non-pinned notifications.\n\n"
        "Choose a scope:"
    )
    cats = ["orders", "payments", "withdrawals", "products", "users",
            "system", "security", "api", "fraud", "support"]
    rows = []
    for i in range(0, len(cats), 2):
        row = []
        for cat in cats[i:i+2]:
            row.append(InlineKeyboardButton(
                f"🗑 {cat.title()}", callback_data=f"anc:del_all:go:{cat}"
            ))
        rows.append(row)
    rows.insert(0, [InlineKeyboardButton("🗑 Delete ALL Categories", callback_data="anc:del_all:go:all")])
    rows.append([InlineKeyboardButton("🔙 Cancel", callback_data="anc:menu")])
    await _send(update, text, InlineKeyboardMarkup(rows))


async def anc_del_all_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # anc:del_all:go:CATEGORY
    cat = parts[3] if len(parts) > 3 else "all"
    count = ncs.delete_all(None if cat == "all" else cat)
    log_admin_action(update.effective_user.id, "notification_center.delete_all",
                     details=f"Deleted {count} notifications (category={cat})")
    await q.answer(f"🗑 {count} notifications deleted.")
    await anc_menu(with_data(update, "anc:menu"), context)


async def anc_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    nid = int(q.data.split(":")[2])
    new_val = ncs.toggle_pin(nid)
    if new_val is not None:
        label = "📌 Pinned" if new_val else "📌 Unpinned"
        await q.answer(label)
    await anc_view(with_data(update, f"anc:view:{nid}"), context)


async def anc_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    nid = int(q.data.split(":")[2])
    ok = ncs.archive_notification(nid)
    if ok:
        log_admin_action(update.effective_user.id, "notification_center.archive", target_id=str(nid))
        await q.answer("🗄 Archived.")
    else:
        await q.answer("Not found.", show_alert=True)
    await anc_list(with_data(update, "anc:list:all:0"), context)


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

async def anc_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    status     = cfg.get("notification_center_status", "enabled")
    max_notifs = cfg.get_int("notification_center_max", 1000)
    retention  = cfg.get_int("notification_center_retention_days", 30)

    lines = [
        "⚙️ <b>Notification Center Settings</b>\n",
        f"Status: <b>{status.title()}</b>",
        f"Max stored: <b>{max_notifs}</b>",
        f"Retention: <b>{retention} days</b>\n",
        "<b>Event Toggles:</b>",
    ]
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        lines.append(f"{'✅' if val else '❌'} {label}")

    text = "\n".join(lines)

    btns = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="anc:settings:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="anc:settings:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="anc:settings:status:disabled")],
    ]
    # Bool toggle buttons (first 3)
    for key, label in _SETTINGS_BOOL_KEYS[:3]:
        val = cfg.get_bool(key, True)
        btns.append([InlineKeyboardButton(
            f"{'✅' if val else '❌'} {label}",
            callback_data=f"anc:settings:toggle:{key}",
        )])
    btns.append([InlineKeyboardButton("🔔 Toggle Event Notifications", callback_data="anc:settings:events")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="anc:menu")])
    await _send(update, text, InlineKeyboardMarkup(btns))


async def anc_settings_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    text = "🔔 <b>Event Notification Toggles</b>\n\nTap to toggle each notification type:"
    btns = []
    event_keys = _SETTINGS_BOOL_KEYS[3:]  # skip the first 3 (general settings)
    for key, label in event_keys:
        val = cfg.get_bool(key, True)
        btns.append([InlineKeyboardButton(
            f"{'✅' if val else '❌'} {label}",
            callback_data=f"anc:settings:toggle:{key}",
        )])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="anc:settings")])
    await _send(update, text, InlineKeyboardMarkup(btns))


async def anc_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # anc:settings:status:VAL
    val = parts[3] if len(parts) > 3 else "enabled"
    cfg.set("notification_center_status", val)
    log_admin_action(update.effective_user.id, "notification_center.set_status", new_value=val)
    await q.answer(f"Status set to {val}.")
    await anc_settings(with_data(update, "anc:settings"), context)


async def anc_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # anc:settings:toggle:KEY
    key = ":".join(parts[3:])
    new_val = not cfg.get_bool(key, True)
    cfg.set(key, new_val)
    log_admin_action(update.effective_user.id, "notification_center.toggle", target_id=key, new_value=str(new_val))
    await q.answer(f"{'✅ Enabled' if new_val else '❌ Disabled'}")
    # Navigate back to the right settings page
    if key.startswith("notif_nc_"):
        await anc_settings_events(with_data(update, "anc:settings:events"), context)
    else:
        await anc_settings(with_data(update, "anc:settings"), context)


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all anc:* callback handlers."""
    application.add_handler(CallbackQueryHandler(anc_menu,              pattern=r"^anc:menu$"))
    application.add_handler(CallbackQueryHandler(anc_filter_menu,       pattern=r"^anc:filter_menu$"))
    application.add_handler(CallbackQueryHandler(anc_list,              pattern=r"^anc:list:.+:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_view,              pattern=r"^anc:view:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_read,              pattern=r"^anc:read:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_read_all,          pattern=r"^anc:read_all$"))
    application.add_handler(CallbackQueryHandler(anc_delete,            pattern=r"^anc:del:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_del_all_confirm,   pattern=r"^anc:del_all:confirm$"))
    application.add_handler(CallbackQueryHandler(anc_del_all_go,        pattern=r"^anc:del_all:go:.+$"))
    application.add_handler(CallbackQueryHandler(anc_pin,               pattern=r"^anc:pin:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_archive,           pattern=r"^anc:arch:\d+$"))
    application.add_handler(CallbackQueryHandler(anc_settings,          pattern=r"^anc:settings$"))
    application.add_handler(CallbackQueryHandler(anc_settings_events,   pattern=r"^anc:settings:events$"))
    application.add_handler(CallbackQueryHandler(anc_settings_status,   pattern=r"^anc:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(anc_settings_toggle,   pattern=r"^anc:settings:toggle:.+$"))
