"""Admin Notifications panel — per-admin event toggles + test-send."""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services import notifications as notif_svc
from utils.helpers import is_admin
from telegram.error import BadRequest

_EVENT_LABELS = [
    ("new_user",         "👤 New user registration"),
    ("new_order",        "🧾 New order created"),
    ("order_delivered",  "✅ Order delivered"),
    ("deposit",          "💰 Deposit completed"),
    ("manual_payment",   "💳 Manual payment submitted"),
    ("payment_failed",   "❌ Payment failed"),
    ("payment_expired",  "⌛ Payment expired"),
    ("payment_reversed", "🔄 Payment reversed"),
    ("dispute",          "⚠️ New dispute"),
    ("refund",           "💸 Refund"),
    ("low_stock",        "📉 Low stock"),
    ("ticket_reply",     "🎧 Ticket reply"),
    ("sla_warning",      "⏰ SLA warning"),
    ("sla_breach",       "🚨 SLA breached"),
]


def _kb(prefs: dict) -> InlineKeyboardMarkup:
    rows = []
    for key, label in _EVENT_LABELS:
        on = bool(prefs.get(key, True))
        mark = "🟢" if on else "⚪"
        rows.append([InlineKeyboardButton(
            f"{mark} {label}",
            callback_data=f"acc:notif:tgl:{key}")])
    rows.append([InlineKeyboardButton("📨 Send test", callback_data="acc:notif:test")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    return InlineKeyboardMarkup(rows)


async def notifs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = update.effective_user.id
    prefs = notif_svc.get_prefs(admin_id)
    text = (
        "🔔 <b>Notifications</b>\n\n"
        "Toggle which admin events you want to receive here.\n"
        "Global defaults live in Bot Settings → Notifications."
    )
    try:
        try:
            await query.edit_message_text(text, reply_markup=_kb(prefs), parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def route(action, rest, update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    if action == "tgl" and rest:
        event = rest[0]
        new_val = notif_svc.toggle_pref(admin_id, event)
        await query.answer(f"{event}: {'ON' if new_val else 'OFF'}")
        await notifs_menu(update, context)
        return
    if action == "test":
        await query.answer("Sending test…")
        await notif_svc.notify_admins(
            context.bot, "new_order",
            "🧪 <b>Test notification</b>\nAdmin Control Center is wired.",
        )
        return
