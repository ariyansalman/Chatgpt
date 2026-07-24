"""handlers/admin_activity_feed.py — Admin Activity Feed Manager V21.

Callback namespace: af:*

Handlers:
  af:menu              — main dashboard
  af:status:<s>        — set overall status (enabled/maintenance/disabled)
  af:private:menu      — private feed settings page
  af:private:toggle    — toggle private feed on/off
  af:private:channel   — enter private channel ID (ConversationHandler entry)
  af:public:menu       — public feed settings page
  af:public:toggle     — toggle public feed on/off
  af:public:channel    — enter public channel ID
  af:filters:menu      — event filter manager
  af:filters:toggle:<event_key> — toggle individual event
  af:options:menu      — display options
  af:options:toggle:<key>       — toggle display option
  af:cancel            — cancel text input
"""
from __future__ import annotations

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from utils.bot_config import cfg
from utils.helpers import is_admin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ConversationHandler states
# ---------------------------------------------------------------------------

AF_AWAITING_PRIVATE_CHANNEL       = 9100
AF_AWAITING_PUBLIC_CHANNEL         = 9101
AF_AWAITING_PRIVATE_EXTRA_CHANNELS = 9102
AF_AWAITING_PUBLIC_EXTRA_CHANNELS  = 9103

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "enabled":     "🟢 Enabled",
    "maintenance": "🟡 Maintenance",
    "disabled":    "🔴 Disabled",
}

# (config_key_suffix, label)
_ALL_EVENTS: List[tuple] = [
    ("new_order",          "🛒 New Order"),
    ("wallet_topup",       "💰 Wallet Top-Up"),
    ("refund",             "💸 Refund"),
    ("delivery_completed", "📦 Delivery Completed"),
    ("order_cancelled",    "❌ Order Cancelled"),
    ("coupon_used",        "🎁 Coupon Used"),
    ("referral_reward",    "👥 Referral Reward"),
    ("review_submitted",   "⭐ Review Submitted"),
    ("product_restocked",  "🔑 Product Restocked"),
    ("product_out_of_stock","📉 Out of Stock"),
    ("invoice_generated",  "🧾 Invoice Generated"),
    ("user_registered",    "👤 User Registered"),
    ("login_alert",        "🔐 Login Alert"),
    ("failed_payment",     "⚠️ Failed Payment"),
    ("fraud_detected",     "🚫 Fraud Detected"),
    ("support_ticket",     "🎫 Support Ticket"),
    ("admin_action",       "🛠 Admin Action"),
]

# (config_key, label, default)
_OPTIONS: List[tuple] = [
    ("af_anonymous_names",       "👤 Anonymous Customer Names",  False),
    ("af_hide_prices",           "💰 Hide Prices",               False),
    ("af_hide_quantity",         "📦 Hide Quantity",             False),
    ("af_hide_product_name",     "📦 Hide Product Name",         False),
    ("af_hide_payment_method",   "💳 Hide Payment Method",       False),
    ("af_hide_time",             "🕒 Hide Timestamps",           False),
    ("af_enable_emojis",         "😀 Enable Emojis",             True),
    ("af_pin_important",         "📌 Pin Important Messages",    False),
    ("af_auto_delete_seconds",   None, None),  # int — handled specially
]

# Display-only options (toggleable booleans only)
_TOGGLE_OPTIONS = [(k, lbl, d) for k, lbl, d in _OPTIONS if lbl is not None]


def _safe_edit(query, text: str, kb=None):
    """Return coroutine that edits message safely (ignores not-modified)."""
    async def _inner():
        try:
            await query.edit_message_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return _inner()


def _guard(update: Update) -> bool:
    return is_admin(update.effective_user.id)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def af_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    status    = cfg.get_str("af_status", "enabled")
    s_label   = _STATUS_LABELS.get(status, status)
    priv_on   = cfg.get_bool("af_private_enabled", False)
    priv_ch   = cfg.get_str("af_private_channel_id", "").strip() or "not set"
    pub_on    = cfg.get_bool("af_public_enabled", False)
    pub_ch    = cfg.get_str("af_public_channel_id", "").strip() or "not set"

    # Event filter count
    active_events = sum(
        1 for k, _ in _ALL_EVENTS if cfg.get_bool(f"af_event_{k}", True)
    )
    total_events = len(_ALL_EVENTS)

    text = (
        "📡 <b>Activity Feed Manager</b>\n\n"
        f"🔹 Status: <b>{s_label}</b>\n\n"
        f"🔒 <b>Private Feed:</b> {'✅ Active' if priv_on else '🚫 Off'}\n"
        f"   Channel: <code>{priv_ch}</code>\n\n"
        f"🌍 <b>Public Feed:</b> {'✅ Active' if pub_on else '🚫 Off'}\n"
        f"   Channel: <code>{pub_ch}</code>\n\n"
        f"📊 <b>Events:</b> {active_events}/{total_events} enabled"
    )

    status_row = [
        InlineKeyboardButton(
            ("✅ " if s == status else "") + lbl,
            callback_data=f"af:status:{s}",
        )
        for s, lbl in _STATUS_LABELS.items()
    ]

    kb = InlineKeyboardMarkup([
        status_row,
        [InlineKeyboardButton("🔒 Private Feed Settings", callback_data="af:private:menu")],
        [InlineKeyboardButton("🌍 Public Feed Settings",  callback_data="af:public:menu")],
        [InlineKeyboardButton("📊 Event Filters",         callback_data="af:filters:menu")],
        [InlineKeyboardButton("⚙️ Display Options",       callback_data="af:options:menu")],
        [InlineKeyboardButton("⬅️ Admin Menu",            callback_data="admin_menu")],
    ])
    await _safe_edit(query, text, kb)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def af_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    new = (query.data or "").split(":")[-1]
    if new not in _STATUS_LABELS:
        await query.answer("❌ Invalid status.", show_alert=True)
        return
    cfg.set("af_status", new)
    await query.answer(f"✅ Feed status: {_STATUS_LABELS[new]}")
    await af_menu(update, context)


# ---------------------------------------------------------------------------
# Private feed settings
# ---------------------------------------------------------------------------

async def af_private_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    priv_on  = cfg.get_bool("af_private_enabled", False)
    priv_ch  = cfg.get_str("af_private_channel_id", "").strip() or "not set"
    extras   = cfg.get_str("af_private_extra_channels", "").strip() or "none"

    text = (
        "🔒 <b>Private Feed Settings</b>\n\n"
        f"Status: {'✅ Active' if priv_on else '🚫 Off'}\n"
        f"Primary Channel: <code>{priv_ch}</code>\n"
        f"Extra Channels: <code>{extras}</code>\n\n"
        "Add the bot as admin to the channel before enabling.\n"
        "Channel IDs look like: <code>-1001234567890</code>"
    )
    tog_label = "🚫 Disable Private Feed" if priv_on else "✅ Enable Private Feed"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(tog_label, callback_data="af:private:toggle")],
        [InlineKeyboardButton("📝 Set Primary Channel ID", callback_data="af:private:channel")],
        [InlineKeyboardButton("➕ Set Extra Channels",     callback_data="af:private:extras")],
        [InlineKeyboardButton("⬅️ Feed Manager",           callback_data="af:menu")],
    ])
    await _safe_edit(query, text, kb)


async def af_private_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    current = cfg.get_bool("af_private_enabled", False)
    cfg.set("af_private_enabled", not current)
    await query.answer("✅ Private feed " + ("disabled" if current else "enabled"))
    await af_private_menu(update, context)


# --- channel ID input flow ---

async def af_private_channel_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    current = cfg.get_str("af_private_channel_id", "").strip() or "not set"
    await query.edit_message_text(
        f"🔒 <b>Set Private Feed Channel</b>\n\n"
        f"Current: <code>{current}</code>\n\n"
        "Send the channel/group ID (e.g. <code>-1001234567890</code>)\n"
        "or /cancel to abort.",
        parse_mode="HTML",
    )
    return AF_AWAITING_PRIVATE_CHANNEL


async def af_private_channel_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _guard(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    cfg.set("af_private_channel_id", text)
    await update.message.reply_text(
        f"✅ Private feed channel set to <code>{text}</code>.\n\n"
        "Make sure the bot is an admin in that channel.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def af_private_extras_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        return ConversationHandler.END
    current = cfg.get_str("af_private_extra_channels", "").strip() or "none"
    await query.edit_message_text(
        f"🔒 <b>Set Extra Private Channels</b>\n\n"
        f"Current: <code>{current}</code>\n\n"
        "Send comma-separated channel IDs:\n"
        "<code>-100111, -100222</code>\n"
        "or send <b>clear</b> to remove all extras, or /cancel to abort.",
        parse_mode="HTML",
    )
    return AF_AWAITING_PRIVATE_EXTRA_CHANNELS


async def af_private_extras_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _guard(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    value = "" if text.lower() == "clear" else text
    cfg.set("af_private_extra_channels", value)
    await update.message.reply_text(
        "✅ Extra private channels updated.", parse_mode="HTML"
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Public feed settings
# ---------------------------------------------------------------------------

async def af_public_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    pub_on = cfg.get_bool("af_public_enabled", False)
    pub_ch = cfg.get_str("af_public_channel_id", "").strip() or "not set"
    extras = cfg.get_str("af_public_extra_channels", "").strip() or "none"

    text = (
        "🌍 <b>Public Feed Settings</b>\n\n"
        f"Status: {'✅ Active' if pub_on else '🚫 Off'}\n"
        f"Primary Channel: <code>{pub_ch}</code>\n"
        f"Extra Channels: <code>{extras}</code>\n\n"
        "<b>Privacy guarantee:</b> Only masked usernames, product names,\n"
        "prices, and delivery type are posted. No IDs, keys, or balances."
    )
    tog_label = "🚫 Disable Public Feed" if pub_on else "✅ Enable Public Feed"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(tog_label, callback_data="af:public:toggle")],
        [InlineKeyboardButton("📝 Set Primary Channel ID", callback_data="af:public:channel")],
        [InlineKeyboardButton("➕ Set Extra Channels",     callback_data="af:public:extras")],
        [InlineKeyboardButton("⬅️ Feed Manager",           callback_data="af:menu")],
    ])
    await _safe_edit(query, text, kb)


async def af_public_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    current = cfg.get_bool("af_public_enabled", False)
    cfg.set("af_public_enabled", not current)
    await query.answer("✅ Public feed " + ("disabled" if current else "enabled"))
    await af_public_menu(update, context)


async def af_public_channel_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        return ConversationHandler.END
    current = cfg.get_str("af_public_channel_id", "").strip() or "not set"
    await query.edit_message_text(
        f"🌍 <b>Set Public Feed Channel</b>\n\n"
        f"Current: <code>{current}</code>\n\n"
        "Send the channel ID (e.g. <code>-1001234567890</code> or <code>@mychannel</code>)\n"
        "or /cancel to abort.",
        parse_mode="HTML",
    )
    return AF_AWAITING_PUBLIC_CHANNEL


async def af_public_channel_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _guard(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    cfg.set("af_public_channel_id", text)
    await update.message.reply_text(
        f"✅ Public feed channel set to <code>{text}</code>.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def af_public_extras_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        return ConversationHandler.END
    current = cfg.get_str("af_public_extra_channels", "").strip() or "none"
    await query.edit_message_text(
        f"🌍 <b>Set Extra Public Channels</b>\n\n"
        f"Current: <code>{current}</code>\n\n"
        "Send comma-separated channel IDs or <b>clear</b> to remove, or /cancel to abort.",
        parse_mode="HTML",
    )
    return AF_AWAITING_PUBLIC_EXTRA_CHANNELS


async def af_public_extras_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _guard(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    value = "" if text.lower() == "clear" else text
    cfg.set("af_public_extra_channels", value)
    await update.message.reply_text("✅ Extra public channels updated.")
    return ConversationHandler.END


async def af_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generic cancel for any af ConversationHandler state."""
    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Event filters
# ---------------------------------------------------------------------------

async def af_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    rows = []
    for key, label in _ALL_EVENTS:
        enabled = cfg.get_bool(f"af_event_{key}", True)
        rows.append([InlineKeyboardButton(
            f"{'✅' if enabled else '🚫'} {label}",
            callback_data=f"af:filters:toggle:{key}",
        )])

    active = sum(cfg.get_bool(f"af_event_{k}", True) for k, _ in _ALL_EVENTS)
    text = (
        f"📊 <b>Event Filters</b>\n\n"
        f"{active}/{len(_ALL_EVENTS)} events active.\n"
        "Tap an event to toggle it on or off."
    )
    rows.append([InlineKeyboardButton("⬅️ Feed Manager", callback_data="af:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def af_filter_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data  = query.data or ""
    parts = data.split(":", 3)
    if len(parts) < 4:
        await query.answer("❌ Invalid.", show_alert=True)
        return
    event_key = parts[3]
    valid     = {k for k, _ in _ALL_EVENTS}
    if event_key not in valid:
        await query.answer("❌ Unknown event.", show_alert=True)
        return
    cfg_key = f"af_event_{event_key}"
    current = cfg.get_bool(cfg_key, True)
    cfg.set(cfg_key, not current)
    label = next((lbl for k, lbl in _ALL_EVENTS if k == event_key), event_key)
    await query.answer(f"{label}: {'off' if current else 'on'}")
    await af_filters_menu(update, context)


# ---------------------------------------------------------------------------
# Display options
# ---------------------------------------------------------------------------

async def af_options_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    auto_del = cfg.get_int("af_auto_delete_seconds", 0)

    rows = []
    for key, label, default in _TOGGLE_OPTIONS:
        val = cfg.get_bool(key, default)
        rows.append([InlineKeyboardButton(
            f"{'✅' if val else '🚫'} {label}",
            callback_data=f"af:options:toggle:{key}",
        )])
    rows.append([InlineKeyboardButton(
        f"⏳ Auto-Delete: {auto_del}s" if auto_del > 0 else "⏳ Auto-Delete: Off",
        callback_data="af:options:autodel",
    )])
    rows.append([InlineKeyboardButton("⬅️ Feed Manager", callback_data="af:menu")])

    text = (
        "⚙️ <b>Display Options</b>\n\n"
        "Configure what appears in each feed message.\n"
        "Tap an option to toggle it."
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def af_option_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _guard(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data  = query.data or ""
    parts = data.split(":", 3)
    if len(parts) < 4:
        await query.answer("❌ Invalid.", show_alert=True)
        return
    opt_key = parts[3]
    valid   = {k for k, _, _ in _TOGGLE_OPTIONS}
    if opt_key not in valid:
        await query.answer("❌ Unknown option.", show_alert=True)
        return
    default = next((d for k, _, d in _TOGGLE_OPTIONS if k == opt_key), False)
    current = cfg.get_bool(opt_key, default)
    cfg.set(opt_key, not current)
    label = next((lbl for k, lbl, _ in _TOGGLE_OPTIONS if k == opt_key), opt_key)
    await query.answer(f"{label}: {'off' if current else 'on'}")
    await af_options_menu(update, context)


# ---------------------------------------------------------------------------
# Central dispatcher for all af:* callbacks
# ---------------------------------------------------------------------------

async def af_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route af:* callbacks to the appropriate handler.

    ConversationHandler entry points (af:private:channel, af:public:channel,
    af:private:extras, af:public:extras) are handled by the ConversationHandler
    registered in bot.py and never reach this dispatcher.
    """
    query = update.callback_query
    data  = query.data if query else ""

    if data == "af:menu":
        await af_menu(update, context)
    elif data.startswith("af:status:"):
        await af_set_status(update, context)
    elif data == "af:private:menu":
        await af_private_menu(update, context)
    elif data == "af:private:toggle":
        await af_private_toggle(update, context)
    elif data == "af:public:menu":
        await af_public_menu(update, context)
    elif data == "af:public:toggle":
        await af_public_toggle(update, context)
    elif data == "af:filters:menu":
        await af_filters_menu(update, context)
    elif data.startswith("af:filters:toggle:"):
        await af_filter_toggle(update, context)
    elif data == "af:options:menu":
        await af_options_menu(update, context)
    elif data.startswith("af:options:toggle:"):
        await af_option_toggle(update, context)
    else:
        if query:
            await query.answer()


# ---------------------------------------------------------------------------
# ConversationHandler builder — call from bot.py
# ---------------------------------------------------------------------------

def build_af_channel_conv() -> ConversationHandler:
    """Build the ConversationHandler for channel ID input flows."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(af_private_channel_start, pattern=r"^af:private:channel$"),
            CallbackQueryHandler(af_public_channel_start,  pattern=r"^af:public:channel$"),
            CallbackQueryHandler(af_private_extras_start,  pattern=r"^af:private:extras$"),
            CallbackQueryHandler(af_public_extras_start,   pattern=r"^af:public:extras$"),
        ],
        states={
            AF_AWAITING_PRIVATE_CHANNEL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, af_private_channel_receive)],
            AF_AWAITING_PUBLIC_CHANNEL:         [MessageHandler(filters.TEXT & ~filters.COMMAND, af_public_channel_receive)],
            AF_AWAITING_PRIVATE_EXTRA_CHANNELS: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_private_extras_receive)],
            AF_AWAITING_PUBLIC_EXTRA_CHANNELS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, af_public_extras_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(af_conv_cancel, pattern=r"^af:cancel$"),
            MessageHandler(filters.COMMAND, af_conv_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
