"""handlers/admin_notification_settings.py — Notification Settings module.

Owns Notification Event Management for the Admin Panel:
  * delivery mode (Admin Only / Log Channel Only / Admin + Log Channel)
    and log-channel configuration;
  * a categorized event catalog (Orders, Payments, Users, Coupons,
    Inventory, Support, System) where each individual event can be
    enabled or disabled independently — see
    ``services.notifications.NOTIFICATION_CATALOG``.

Standalone, additive module for the Admin Panel. It does NOT modify any
existing business logic, database schema, APIs, routes, callback_data, or
security/permission internals. It only:

  * reads/writes its own ``notif_settings_*`` keys through the existing
    generic BotConfig key/value store (``utils.bot_config.cfg``) — no new
    tables, no schema changes;
  * reuses the existing admin guard (``utils.helpers.is_admin``), the
    existing audit logger (``utils.audit.log_admin_action``), and the
    existing admin-role listing (``utils.permissions.list_admins``) without
    modifying any of them;
  * reuses ``services.notifications.get_prefs`` / ``toggle_pref`` for
    event-level toggles — the same store that actually gates delivery in
    ``notify_admins()``, so this UI is never out of sync with reality;
  * sends messages using the standard ``context.bot`` API the rest of the
    project already uses.

Callback namespace: ``nsm:*`` (new, does not collide with any existing
namespace in this project).

  nsm:menu                            — main dashboard (mode + channel status)
  nsm:mode:<admin|log_channel|both>   — set delivery mode
  nsm:channel:menu                    — configure log channel screen
  nsm:channel:set                     — ConversationHandler entry
                                         (type an ID / forward a message)
  nsm:channel:clear                   — clear the saved channel
  nsm:cat:menu                        — category list (Orders, Payments, …)
  nsm:cat:view:<category>             — events within one category
  nsm:cat:tgl:<category>:<event>      — toggle a single event on/off
  nsm:test                            — send a sample notification
  nsm:cancel                          — cancel text/forward input
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from config.settings import settings
from utils.bot_config import cfg
from utils.helpers import is_admin
from utils.audit import log_admin_action
from services import notifications as notif_svc

try:
    from utils.permissions import list_admins
except Exception:  # pragma: no cover — defensive only, module stays additive
    def list_admins(include_inactive: bool = False):
        return []

logger = logging.getLogger(__name__)

NSM_AWAITING_CHANNEL = 9900

_MODES = [
    ("admin",       "👤 Admin Only"),
    ("log_channel", "📢 Log Channel Only"),
    ("both",        "👥 Admin + Log Channel"),
]
_MODE_LABELS = dict(_MODES)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _guard(update: Update) -> bool:
    return is_admin(update.effective_user.id)


async def _safe_edit(query, text: str, kb: IKM) -> None:
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _admin_recipient_ids() -> list:
    """Every Telegram ID that should receive an 'Admin Only' notification."""
    ids = set()
    owner = getattr(settings, "ADMIN_TELEGRAM_ID", 0) or 0
    if owner:
        ids.add(int(owner))
    try:
        for row in list_admins():
            tid = row.get("telegram_id")
            if tid:
                ids.add(int(tid))
    except Exception:
        logger.exception("nsm: list_admins failed, falling back to owner only")
    return list(ids)


def _channel_status() -> Tuple[str, str, bool]:
    chan_id = cfg.get_str("notif_settings_log_channel_id", "").strip()
    title = cfg.get_str("notif_settings_log_channel_title", "").strip()
    verified = cfg.get_bool("notif_settings_log_channel_verified", False)
    return chan_id, title, verified


def _mode() -> str:
    m = cfg.get_str("notif_settings_mode", "admin").strip().lower()
    return m if m in _MODE_LABELS else "admin"


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

async def nsm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    if not _guard(update):
        if query:
            await query.answer("⛔ Admins only.", show_alert=True)
        return

    mode = _mode()
    chan_id, title, verified = _channel_status()
    if not chan_id:
        chan_line = "<i>not configured</i>"
    else:
        chan_line = f"<code>{chan_id}</code>"
        if title:
            chan_line += f" — {title}"
        chan_line += " ✅" if verified else " ⚠️ unverified"

    text = (
        "🔔 <b>Notification Settings</b>\n\n"
        f"Current mode: <b>{_MODE_LABELS[mode]}</b>\n"
        f"Log channel: {chan_line}\n\n"
        "Choose where admin notifications should be delivered:"
    )

    rows = []
    for key, label in _MODES:
        mark = "✅ " if key == mode else ""
        rows.append([IKB(f"{mark}{label}", callback_data=f"nsm:mode:{key}")])
    rows.append([IKB("📝 Configure Log Channel", callback_data="nsm:channel:menu")])
    rows.append([IKB("📋 Notification Categories", callback_data="nsm:cat:menu")])
    rows.append([IKB("🧪 Send Test Notification", callback_data="nsm:test")])
    rows.append([IKB("⬅️ Admin Panel", callback_data="acc:root")])
    kb = IKM(rows)

    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def nsm_set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    new_mode = (query.data or "").split(":")[-1]
    if new_mode not in _MODE_LABELS:
        await query.answer("❌ Invalid mode.", show_alert=True)
        return

    if new_mode in ("log_channel", "both"):
        chan_id, _title, verified = _channel_status()
        if not chan_id or not verified:
            await query.answer("⚠️ Configure & validate a log channel first.", show_alert=True)
            await nsm_channel_menu(update, context)
            return

    old_mode = _mode()
    cfg.set("notif_settings_mode", new_mode)
    log_admin_action(
        update.effective_user.id, "notification_settings.set_mode",
        old_value=old_mode, new_value=new_mode, module="notification_settings",
    )
    await query.answer(f"✅ Mode: {_MODE_LABELS[new_mode]}")
    await nsm_menu(update, context)


# ---------------------------------------------------------------------------
# Configure log channel
# ---------------------------------------------------------------------------

async def nsm_channel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    if not _guard(update):
        if query:
            await query.answer("⛔ Admins only.", show_alert=True)
        return

    chan_id, title, verified = _channel_status()
    if not chan_id:
        chan_line = "<i>not configured</i>"
    else:
        chan_line = f"<code>{chan_id}</code>"
        if title:
            chan_line += f"\nTitle: {title}"
        chan_line += "\nStatus: ✅ Verified" if verified else "\nStatus: ⚠️ Unverified"

    text = (
        "📝 <b>Configure Log Channel</b>\n\n"
        f"{chan_line}\n\n"
        "To set the channel, either:\n"
        "• Send the channel's numeric ID (e.g. <code>-1001234567890</code>) "
        "or its <code>@username</code>\n"
        "• Or forward any message from that channel here\n\n"
        "The bot must already be added to the channel as an admin with "
        "permission to post messages — this is validated automatically "
        "before saving."
    )

    rows = [[IKB("✍️ Enter Channel ID / Forward Message", callback_data="nsm:channel:set")]]
    if chan_id:
        rows.append([IKB("🗑 Clear Channel", callback_data="nsm:channel:clear")])
    rows.append([IKB("⬅️ Notification Settings", callback_data="nsm:menu")])
    kb = IKM(rows)

    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def nsm_channel_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    cfg.set("notif_settings_log_channel_id", "")
    cfg.set("notif_settings_log_channel_title", "")
    cfg.set("notif_settings_log_channel_verified", False)
    if _mode() in ("log_channel", "both"):
        cfg.set("notif_settings_mode", "admin")
    log_admin_action(update.effective_user.id, "notification_settings.clear_channel",
                      module="notification_settings")
    await query.answer("🗑 Log channel cleared.")
    await nsm_channel_menu(update, context)


async def nsm_channel_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        return ConversationHandler.END
    await query.edit_message_text(
        "📝 <b>Set Log Channel</b>\n\n"
        "Send the channel's numeric ID (e.g. <code>-1001234567890</code>) "
        "or its <code>@username</code>, or forward any message from that "
        "channel here.\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return NSM_AWAITING_CHANNEL


async def _resolve_channel_input(update: Update):
    """Return the candidate chat_id/username from a forwarded message or
    typed text, or None if nothing usable was sent."""
    msg = update.message
    fwd_chat = getattr(msg, "forward_from_chat", None)
    if fwd_chat is not None:
        return fwd_chat.id
    text = (msg.text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text  # e.g. "@mychannel" — get_chat() resolves usernames directly


async def nsm_channel_set_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _guard(update):
        return ConversationHandler.END

    raw_text = (update.message.text or "").strip()
    if raw_text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END

    candidate = await _resolve_channel_input(update)
    if candidate is None:
        await update.message.reply_text(
            "❌ Send a numeric channel ID, an @username, or forward a "
            "message from the channel. Send /cancel to abort."
        )
        return NSM_AWAITING_CHANNEL

    # ── Validate: bot must be able to see the chat ─────────────────────────
    try:
        chat = await context.bot.get_chat(candidate)
    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Couldn't access that channel: {getattr(e, 'message', e)}\n"
            "Make sure the bot has been added to the channel, then try "
            "again or send /cancel."
        )
        return NSM_AWAITING_CHANNEL

    if chat.type not in ("channel", "supergroup", "group"):
        await update.message.reply_text(
            "❌ That's not a channel or group. Send a channel ID/username, "
            "forward a message from it, or send /cancel."
        )
        return NSM_AWAITING_CHANNEL

    # ── Validate: bot must be able to post there ────────────────────────────
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        status_ok = getattr(member, "status", "") in ("administrator", "creator")
        can_post = getattr(member, "can_post_messages", True)
        if not status_ok or can_post is False:
            await update.message.reply_text(
                "⚠️ The bot can see that chat, but it isn't an admin with "
                "permission to post messages there yet. Add the bot as an "
                "admin with 'Post Messages' rights, then try again or "
                "send /cancel."
            )
            return NSM_AWAITING_CHANNEL
    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Couldn't verify the bot's permissions in that chat: "
            f"{getattr(e, 'message', e)}\nTry again or send /cancel."
        )
        return NSM_AWAITING_CHANNEL

    cfg.set("notif_settings_log_channel_id", str(chat.id))
    cfg.set("notif_settings_log_channel_title", chat.title or "")
    cfg.set("notif_settings_log_channel_verified", True)
    log_admin_action(
        update.effective_user.id, "notification_settings.set_channel",
        new_value=str(chat.id), module="notification_settings",
    )

    await update.message.reply_text(
        f"✅ Log channel verified and saved:\n<b>{chat.title or chat.id}</b> "
        f"(<code>{chat.id}</code>)",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def nsm_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Notification Categories — per-event enable/disable
# ---------------------------------------------------------------------------

def _category_lookup(key: str):
    for cat_key, cat_label, events in notif_svc.NOTIFICATION_CATALOG:
        if cat_key == key:
            return cat_label, events
    return None, None


async def nsm_categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    if not _guard(update):
        if query:
            await query.answer("⛔ Admins only.", show_alert=True)
        return

    admin_id = update.effective_user.id
    prefs = notif_svc.get_prefs(admin_id)

    text = (
        "📋 <b>Notification Categories</b>\n\n"
        "Choose a category to enable or disable its individual events.\n"
        "Events marked ⚠️ are configured but not yet triggered by any "
        "action in the bot — toggling them has no effect yet."
    )
    rows = []
    for cat_key, cat_label, events in notif_svc.NOTIFICATION_CATALOG:
        on_count = sum(1 for ev, _lbl, _live in events if prefs.get(ev, True))
        rows.append([IKB(
            f"{cat_label} ({on_count}/{len(events)} on)",
            callback_data=f"nsm:cat:view:{cat_key}",
        )])
    rows.append([IKB("⬅️ Notification Settings", callback_data="nsm:menu")])
    kb = IKM(rows)

    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def nsm_category_view(update: Update, context: ContextTypes.DEFAULT_TYPE, cat_key: str) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    cat_label, events = _category_lookup(cat_key)
    if events is None:
        await nsm_categories_menu(update, context)
        return

    admin_id = update.effective_user.id
    prefs = notif_svc.get_prefs(admin_id)

    text = f"{cat_label}\n\nTap an event to toggle it on or off."
    rows = []
    for ev_key, ev_label, live in events:
        on = bool(prefs.get(ev_key, True))
        mark = "☑️" if on else "⬜"
        suffix = "" if live else " ⚠️"
        rows.append([IKB(
            f"{mark} {ev_label}{suffix}",
            callback_data=f"nsm:cat:tgl:{cat_key}:{ev_key}",
        )])
    rows.append([IKB("⬅️ Categories", callback_data="nsm:cat:menu")])
    kb = IKM(rows)
    await _safe_edit(query, text, kb)


async def nsm_category_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               cat_key: str, ev_key: str) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    admin_id = update.effective_user.id
    new_val = notif_svc.toggle_pref(admin_id, ev_key)
    log_admin_action(
        admin_id, "notification_settings.toggle_event",
        target_type="event", target_id=ev_key,
        new_value=str(new_val), module="notification_settings",
    )
    await query.answer(f"{ev_key}: {'ON' if new_val else 'OFF'}")
    await nsm_category_view(update, context, cat_key)


# ---------------------------------------------------------------------------
# Test notification
# ---------------------------------------------------------------------------

async def nsm_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer("Sending test notification…")

    mode = _mode()
    chan_id, _title, verified = _channel_status()
    from utils.notify_format import render as _render, utc_now_str as _ts
    sample = _render("🧪", "Test Notification", [
        ("Source", "Notification Settings"),
        ("Delivery mode", _MODE_LABELS[mode]),
    ], _ts())

    sent_admin = sent_channel = 0
    errors = []

    if mode in ("admin", "both"):
        for admin_id in _admin_recipient_ids():
            try:
                await context.bot.send_message(chat_id=admin_id, text=sample, parse_mode="HTML")
                sent_admin += 1
            except Exception as e:
                errors.append(f"admin {admin_id}: {e}")

    if mode in ("log_channel", "both"):
        if not chan_id or not verified:
            errors.append("log channel not configured/verified")
        else:
            dest = int(chan_id) if chan_id.lstrip("-").isdigit() else chan_id
            try:
                await context.bot.send_message(chat_id=dest, text=sample, parse_mode="HTML")
                sent_channel += 1
            except Exception as e:
                errors.append(f"log channel: {e}")

    log_admin_action(
        update.effective_user.id, "notification_settings.test_send",
        details=f"admin={sent_admin} channel={sent_channel} errors={len(errors)}",
        module="notification_settings",
    )

    summary = f"✅ Sent to {sent_admin} admin(s), {sent_channel} channel(s)."
    if errors:
        summary += "\n⚠️ " + "; ".join(errors[:3])
    await query.message.reply_text(summary)


# ---------------------------------------------------------------------------
# Dispatcher + registration
# ---------------------------------------------------------------------------

async def nsm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route nsm:* callbacks that aren't ConversationHandler entry points."""
    query = update.callback_query
    data = query.data if query else ""

    if data == "nsm:menu":
        await nsm_menu(update, context)
    elif data.startswith("nsm:mode:"):
        await nsm_set_mode(update, context)
    elif data == "nsm:channel:menu":
        await nsm_channel_menu(update, context)
    elif data == "nsm:channel:clear":
        await nsm_channel_clear(update, context)
    elif data == "nsm:cat:menu":
        await nsm_categories_menu(update, context)
    elif data.startswith("nsm:cat:view:"):
        cat_key = data.split(":", 3)[3]
        await nsm_category_view(update, context, cat_key)
    elif data.startswith("nsm:cat:tgl:"):
        _, _, _, cat_key, ev_key = data.split(":", 4)
        await nsm_category_toggle(update, context, cat_key, ev_key)
    elif data == "nsm:test":
        await nsm_test(update, context)
    else:
        if query:
            await query.answer()


def build_nsm_channel_conv() -> ConversationHandler:
    """ConversationHandler for the channel ID / forward-message input flow."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(nsm_channel_set_start, pattern=r"^nsm:channel:set$"),
        ],
        states={
            NSM_AWAITING_CHANNEL: [
                MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
                                nsm_channel_set_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(nsm_conv_cancel, pattern=r"^nsm:cancel$"),
            MessageHandler(filters.COMMAND, nsm_conv_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    """Register all nsm:* callback + conversation handlers.

    Call this from bot.py alongside the other module registrations —
    it does not touch any existing handler registration.
    """
    application.add_handler(build_nsm_channel_conv())
    application.add_handler(CallbackQueryHandler(nsm_dispatch, pattern=r"^nsm:"))
