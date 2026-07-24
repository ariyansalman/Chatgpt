"""Advanced Broadcast Types — Admin Handler.

Callback namespace: ``abt:*``
Conversation key:   ``_abt`` in context.user_data

Provides:
  • 10 predefined broadcast types with editable templates
  • 27-segment smart audience targeting
  • AND-combined extra audience filters
  • 15 message variable placeholders with per-user substitution
  • Live audience count preview + estimated delivery time
  • Test send (to self / to any user by Telegram ID)
  • Send now / schedule (delegates to ScheduledBroadcast pipeline)
  • Settings page with feature toggles
  • Full feature-management (enabled / maintenance / disabled)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import get_db_session
from database.models import ScheduledBroadcast
from utils.bot_config import cfg
from utils.audit import log_admin_action
from utils.permissions import has_permission
from config.settings import settings
from utils.update_proxy import with_data

from services.broadcast_audience_service import (
    SEGMENTS,
    VARIABLE_KEYS,
    count_audience,
    estimate_delivery_seconds,
    preview_for_first_user,
    resolve_audience,
    substitute_variables,
)

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
ABT_TEMPLATE_EDIT = 0   # editing the message template
ABT_SCHEDULE      = 1   # receiving schedule datetime
ABT_TEST_USER     = 2   # receiving telegram_id for test-to-user
ABT_FILTER_VALUE  = 3   # receiving extra filter value (numeric/text)
ABT_SEL_IDS       = 4   # receiving comma-sep telegram IDs
ABT_VAR_VALUE     = 5   # receiving override for a variable

# ── Broadcast type definitions ─────────────────────────────────────────────────

BROADCAST_TYPES: Dict[str, Dict[str, Any]] = {
    "coupon": {
        "label":   "🎯 Coupon Broadcast",
        "desc":    "Send exclusive coupon codes to targeted users.",
        "template": (
            "🎟 <b>Exclusive Coupon Just For You, {first_name}!</b>\n\n"
            "Use code: <code>{coupon_code}</code>\n"
            "Discount: <b>{discount}%</b>\n\n"
            "⏰ Valid for a limited time only. Don't miss out!"
        ),
    },
    "flash_sale": {
        "label":   "🔥 Flash Sale Broadcast",
        "desc":    "Announce a time-limited flash sale to drive urgency.",
        "template": (
            "🔥 <b>FLASH SALE — Limited Time!</b>\n\n"
            "Hey {first_name}, a special deal is LIVE right now!\n\n"
            "🏷 <b>{product_name}</b>\n"
            "Was: <s>{old_price}</s>  ➜  Now: <b>{new_price}</b>\n\n"
            "⚡ Grab it before it's gone!"
        ),
    },
    "giveaway": {
        "label":   "🎁 Giveaway Broadcast",
        "desc":    "Run a giveaway and invite users to participate.",
        "template": (
            "🎁 <b>GIVEAWAY TIME, {first_name}!</b>\n\n"
            "We're giving away <b>{product_name}</b> to lucky users.\n"
            "Earn <b>{bonus}</b> bonus points for every entry!\n\n"
            "Enter now for your chance to win — limited entries only!"
        ),
    },
    "maintenance": {
        "label":   "🚨 Maintenance Notice",
        "desc":    "Notify users of upcoming scheduled maintenance.",
        "template": (
            "🚨 <b>Scheduled Maintenance Notice</b>\n\n"
            "Dear {first_name},\n\n"
            "Our bot will undergo scheduled maintenance shortly.\n"
            "Please complete any pending orders or transactions before then.\n\n"
            "We apologize for the inconvenience and will be back shortly. 🙏"
        ),
    },
    "new_product": {
        "label":   "📢 New Product Announcement",
        "desc":    "Alert users when a new product is added to the store.",
        "template": (
            "📢 <b>NEW PRODUCT ALERT, {first_name}!</b>\n\n"
            "We just added: <b>{product_name}</b>\n"
            "Category: {category_name}\n\n"
            "🛒 Be the first to grab it — tap below to order now!"
        ),
    },
    "price_drop": {
        "label":   "📈 Price Drop Alert",
        "desc":    "Notify users when a product price drops.",
        "template": (
            "📉 <b>Price Drop Alert for {first_name}!</b>\n\n"
            "Good news! <b>{product_name}</b> just got cheaper.\n\n"
            "Was: <s>{old_price}</s>\n"
            "Now: <b>{new_price}</b> 🎉\n\n"
            "⏰ Limited time — order now before the price goes back up!"
        ),
    },
    "restock": {
        "label":   "📦 Restock Alert",
        "desc":    "Let users know when an out-of-stock item is back.",
        "template": (
            "📦 <b>BACK IN STOCK, {first_name}!</b>\n\n"
            "<b>{product_name}</b> is available again!\n"
            "Category: {category_name}\n\n"
            "🚀 Order now before it sells out again!"
        ),
    },
    "wallet_bonus": {
        "label":   "💰 Wallet Bonus Alert",
        "desc":    "Promote a wallet top-up bonus to users.",
        "template": (
            "💰 <b>Wallet Bonus Alert, {first_name}!</b>\n\n"
            "Your current balance: <b>{wallet_balance}</b>\n\n"
            "Top up now and get <b>{bonus}</b> extra bonus credited instantly!\n\n"
            "🎁 Limited-time offer — don't miss it!"
        ),
    },
    "referral_reward": {
        "label":   "🎉 Referral Reward Alert",
        "desc":    "Notify users about referral earnings or bonuses.",
        "template": (
            "🎉 <b>Referral Reward for {first_name}!</b>\n\n"
            "You've earned <b>{bonus}</b> from your referrals! 🙌\n\n"
            "The more friends you invite, the more you earn.\n"
            "Share your referral link and keep the rewards coming!"
        ),
    },
    "custom": {
        "label":   "📢 Custom Broadcast",
        "desc":    "Write a fully custom message to any audience.",
        "template": (
            "📢 <b>Message for {first_name}</b>\n\n"
            "{custom_field}"
        ),
    },
}

# ── Extra filter definitions (for AND-combination) ────────────────────────────

EXTRA_FILTERS: Dict[str, Dict[str, Any]] = {
    "min_wallet":    {"label": "💰 Min Wallet Balance ($)", "type": "float"},
    "max_wallet":    {"label": "💸 Max Wallet Balance ($)", "type": "float"},
    "active_days":   {"label": "🟢 Active in Last N Days",  "type": "int"},
    "min_orders":    {"label": "🛒 Min Completed Orders",   "type": "int"},
    "has_purchased": {"label": "✅ Has Purchased",           "type": "bool_yes"},
    "language":      {"label": "🌐 Language Code (e.g. en)","type": "str"},
}

# Segments that require a text value from the user (handled in compose flow)
SEGMENTS_NEEDING_VALUE = {
    "product_buyers":  ("product_id",           "Enter the <b>Product ID</b> to filter by:"),
    "category_buyers": ("category_id",           "Enter the <b>Category ID</b> to filter by:"),
    "language":        ("language",              "Enter the <b>language code</b> (e.g. <code>en</code>, <code>ar</code>):"),
    "tag":             ("tag_name",              "Enter the <b>tag name</b> to filter by:"),
    "selected_users":  ("selected_usernames",    "Enter <b>@usernames</b> separated by commas:"),
    "selected_ids":    ("selected_ids",          "Enter <b>Telegram IDs</b> separated by commas:"),
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return has_permission(uid, "manage_broadcasts")


async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML") -> None:
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            try:
                await query.message.reply_text(text, parse_mode=parse_mode, reply_markup=kb)
            except Exception:
                pass


def _back_kb(cb: str = "abt:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _get_data(context) -> dict:
    if "_abt" not in context.user_data:
        context.user_data["_abt"] = {}
    return context.user_data["_abt"]


def _status_ok() -> bool:
    return cfg.get("advanced_broadcast_types_status", "enabled") == "enabled"


def _maintenance() -> bool:
    return cfg.get("advanced_broadcast_types_status", "enabled") == "maintenance"


def _fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, mn = divmod(m, 60)
    return f"{h}h {mn}m"


# ── Main menu ──────────────────────────────────────────────────────────────────

async def abt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advanced Broadcast Types hub (abt:menu)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    status = cfg.get("advanced_broadcast_types_status", "enabled")
    if status == "disabled":
        await _safe_edit(query,
            "🎯 <b>Advanced Broadcast Types</b>\n\n"
            "🔴 This feature is currently <b>disabled</b>.\n"
            "Enable it in Broadcast Settings.",
            _back_kb("acc:sec:broadcast"))
        return

    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")
    text = (
        f"🎯 <b>Advanced Broadcast Types</b>  {status_icon}\n\n"
        "Select a broadcast type to compose and send:\n"
    )
    if _maintenance():
        text += "\n⚠️ <i>Maintenance mode — broadcasts are paused.</i>\n"

    kb = []
    for key, bt in BROADCAST_TYPES.items():
        kb.append([InlineKeyboardButton(bt["label"], callback_data=f"abt:compose:{key}")])
    kb.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="abt:settings"),
        InlineKeyboardButton("🔙 Back",     callback_data="acc:sec:broadcast"),
    ])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Compose view ───────────────────────────────────────────────────────────────

async def abt_compose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show compose screen for a broadcast type (abt:compose:<type>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    btype = parts[2] if len(parts) > 2 else "custom"
    bt    = BROADCAST_TYPES.get(btype, BROADCAST_TYPES["custom"])

    data = _get_data(context)
    if data.get("type") != btype:
        # Reset for new type
        data.clear()
        data["type"]             = btype
        data["template"]         = bt["template"]
        data["primary_segment"]  = "all"
        data["extra_filters"]    = {}
        data["variables"]        = {}
        data["schedule_at"]      = None
        data["is_recurring"]     = False
        data["recurrence_type"]  = None

    seg_label    = SEGMENTS.get(data["primary_segment"], "👥 All Users")
    ef_count     = len(data.get("extra_filters", {}))
    ef_label     = f" + {ef_count} extra filter(s)" if ef_count else ""
    tmpl_preview = data["template"][:200]

    text = (
        f"{bt['label']}\n\n"
        f"<b>Description:</b> {bt['desc']}\n\n"
        f"<b>Audience:</b> {seg_label}{ef_label}\n\n"
        f"<b>Template preview:</b>\n"
        f"<i>{tmpl_preview}{'…' if len(data['template']) > 200 else ''}</i>\n"
    )

    vars_enabled = cfg.get_bool("broadcast_variables_enabled", True)
    preview_enabled = cfg.get_bool("broadcast_audience_preview_enabled", True)
    test_enabled = cfg.get_bool("broadcast_test_mode_enabled", True)

    kb = [
        [InlineKeyboardButton("✏️ Edit Template",  callback_data="abt:edit_template"),
         InlineKeyboardButton("🎯 Audience",        callback_data="abt:audience")],
    ]
    if ef_count < 6:
        kb.append([InlineKeyboardButton("➕ Extra Filters", callback_data="abt:filters")])
    if vars_enabled:
        kb.append([InlineKeyboardButton("🔤 Variables",     callback_data="abt:variables")])
    if preview_enabled:
        kb.append([InlineKeyboardButton("👁 Preview Message", callback_data="abt:preview_msg"),
                   InlineKeyboardButton("📊 Audience Count",  callback_data="abt:audience_preview")])
    if test_enabled:
        kb.append([InlineKeyboardButton("🧪 Test → Me",       callback_data="abt:test_self"),
                   InlineKeyboardButton("🧪 Test → User",     callback_data="abt:test_user_ask")])
    kb.append([InlineKeyboardButton("📤 Send Now",   callback_data="abt:send_now"),
               InlineKeyboardButton("📅 Schedule",    callback_data="abt:schedule_ask")])
    kb.append([InlineKeyboardButton("🔙 Back",        callback_data="abt:menu")])

    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Edit template conversation ─────────────────────────────────────────────────

async def abt_edit_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for template editing (abt:edit_template)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    data = _get_data(context)
    btype = data.get("type", "custom")
    bt    = BROADCAST_TYPES.get(btype, BROADCAST_TYPES["custom"])

    var_list = "  ".join(f"<code>{{{k}}}</code>" for k in VARIABLE_KEYS)
    await _safe_edit(query,
        f"✏️ <b>Edit Template — {bt['label']}</b>\n\n"
        f"Send the new message template.\n"
        f"You may use these variables:\n{var_list}\n\n"
        f"<b>Current template:</b>\n"
        f"<i>{data['template'][:500]}</i>\n\n"
        f"Send /cancel to keep the current template.",
        None)
    return ABT_TEMPLATE_EDIT


async def abt_receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the new template text."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt  = (update.message.text or "").strip()
    data = _get_data(context)
    data["template"] = txt
    btype = data.get("type", "custom")
    await update.message.reply_text(
        f"✅ Template updated for <b>{BROADCAST_TYPES.get(btype, {}).get('label', btype)}</b>!\n\n"
        "Use the Compose menu to preview or send.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Compose", callback_data=f"abt:compose:{btype}")
        ]]))
    return ConversationHandler.END


# ── Audience selection ─────────────────────────────────────────────────────────

async def abt_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Primary audience segment picker (abt:audience, paginated)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    page  = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    items = list(SEGMENTS.items())
    page_size = 8
    total_pages = (len(items) + page_size - 1) // page_size
    slice_  = items[page * page_size: (page + 1) * page_size]

    data = _get_data(context)
    current = data.get("primary_segment", "all")

    kb = []
    for seg_key, seg_label in slice_:
        icon = "✅ " if seg_key == current else ""
        kb.append([InlineKeyboardButton(
            f"{icon}{seg_label}", callback_data=f"abt:audience_sel:{seg_key}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"abt:audience:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="abt:audience:0"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"abt:audience:{page+1}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"abt:compose:{data.get('type', 'custom')}")])

    await _safe_edit(query,
        f"🎯 <b>Select Primary Audience</b>\n"
        f"Current: <b>{SEGMENTS.get(current, current)}</b>\n\n"
        "Tap a segment to select it:",
        InlineKeyboardMarkup(kb))


async def abt_audience_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle primary segment selection (abt:audience_sel:<seg>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    parts = query.data.split(":")
    seg   = parts[2] if len(parts) > 2 else "all"
    data  = _get_data(context)
    btype = data.get("type", "custom")

    # Some segments need a value from the user
    if seg in SEGMENTS_NEEDING_VALUE:
        key, prompt = SEGMENTS_NEEDING_VALUE[seg]
        data["primary_segment"]  = seg
        data["_filter_key"]      = key
        await _safe_edit(query,
            f"🎯 <b>Segment: {SEGMENTS.get(seg, seg)}</b>\n\n{prompt}",
            _back_kb(f"abt:compose:{btype}"))
        return ABT_FILTER_VALUE

    data["primary_segment"] = seg
    await query.answer(f"✅ Segment set: {SEGMENTS.get(seg, seg)}", show_alert=False)
    # Show compose view
    return await abt_compose(with_data(update, f"abt:compose:{btype}"), context)


async def abt_filter_value_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a text/numeric value for a segment or extra filter."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt  = (update.message.text or "").strip()
    data = _get_data(context)
    key  = data.pop("_filter_key", None)
    btype = data.get("type", "custom")

    if key:
        # Determine if it should go to extra_filters or the main data
        if key in ("product_id", "category_id"):
            try:
                data["extra_filters"][key] = int(txt)
            except ValueError:
                await update.message.reply_text("❌ Please send a valid numeric ID.")
                data["_filter_key"] = key
                return ABT_FILTER_VALUE
        elif key in ("selected_ids",):
            # Validate comma-sep IDs
            try:
                [int(x.strip()) for x in txt.split(",") if x.strip()]
                data["extra_filters"][key] = txt
            except ValueError:
                await update.message.reply_text("❌ Send valid Telegram IDs, comma-separated.")
                data["_filter_key"] = key
                return ABT_FILTER_VALUE
        else:
            data["extra_filters"][key] = txt

    await update.message.reply_text(
        "✅ Filter value saved!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Compose", callback_data=f"abt:compose:{btype}")
        ]]))
    return ConversationHandler.END


# ── Extra filters (AND-combination) ──────────────────────────────────────────

async def abt_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show extra AND-filter selection (abt:filters)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    data   = _get_data(context)
    btype  = data.get("type", "custom")
    active = data.get("extra_filters", {})
    smart  = cfg.get_bool("broadcast_smart_filters_enabled", True)

    if not smart:
        await _safe_edit(query,
            "➕ <b>Extra Filters</b>\n\n❌ Smart filters are disabled in Broadcast Settings.",
            _back_kb(f"abt:compose:{btype}"))
        return

    kb = []
    for fkey, fdef in EXTRA_FILTERS.items():
        current_val = active.get(fkey)
        badge = f" [{current_val}]" if current_val is not None else ""
        action = "🗑" if current_val is not None else "➕"
        kb.append([InlineKeyboardButton(
            f"{action} {fdef['label']}{badge}",
            callback_data=f"abt:filter_toggle:{fkey}")])

    if active:
        kb.append([InlineKeyboardButton("🗑 Clear All Filters", callback_data="abt:filters_clear")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"abt:compose:{btype}")])

    applied_lines = "\n".join(
        f"  • {EXTRA_FILTERS.get(k, {}).get('label', k)}: <b>{v}</b>"
        for k, v in active.items()
    ) or "  None applied"

    await _safe_edit(query,
        f"➕ <b>Extra AND-Filters</b>\n\n"
        f"Applied:\n{applied_lines}\n\n"
        "Tap a filter to add/remove it. All active filters are combined with AND logic.",
        InlineKeyboardMarkup(kb))


async def abt_filter_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle or prompt for an extra filter value (abt:filter_toggle:<key>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    parts = query.data.split(":")
    fkey  = parts[2] if len(parts) > 2 else ""
    data  = _get_data(context)
    btype = data.get("type", "custom")
    ef    = data.setdefault("extra_filters", {})
    fdef  = EXTRA_FILTERS.get(fkey, {})

    if fkey in ef:
        # Remove existing filter
        del ef[fkey]
        await query.answer(f"🗑 Filter removed: {fdef.get('label', fkey)}", show_alert=False)
        return await abt_filters(with_data(update, "abt:filters"), context)

    # Add filter — bool_yes type doesn't need a prompt
    if fdef.get("type") == "bool_yes":
        ef[fkey] = True
        await query.answer(f"✅ Filter added.", show_alert=False)
        return await abt_filters(with_data(update, "abt:filters"), context)

    # Need text/numeric value
    data["_filter_key"] = fkey
    type_hint = {"float": "number (e.g. 10.5)", "int": "whole number", "str": "text"}.get(
        fdef.get("type", "str"), "value")
    await _safe_edit(query,
        f"➕ <b>{fdef.get('label', fkey)}</b>\n\n"
        f"Send the {type_hint}:",
        _back_kb("abt:filters"))
    return ABT_FILTER_VALUE


async def abt_filters_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all extra filters (abt:filters_clear)."""
    query = update.callback_query
    await query.answer()
    data = _get_data(context)
    data["extra_filters"] = {}
    return await abt_filters(with_data(update, "abt:filters"), context)


# ── Variables ──────────────────────────────────────────────────────────────────

async def abt_variables(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Variable override management (abt:variables)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_variables_enabled", True):
        await query.answer("🔤 Variables are disabled in settings.", show_alert=True)
        return

    data   = _get_data(context)
    btype  = data.get("type", "custom")
    ov     = data.get("variables", {})

    broadcast_vars = [k for k in VARIABLE_KEYS
                      if k not in ("first_name", "last_name", "username",
                                   "telegram_id", "wallet_balance")]

    lines = ["🔤 <b>Broadcast Variables</b>\n\n"
             "Set values for variables in your template. "
             "Per-user values (first_name, wallet_balance, etc.) are filled automatically.\n"]
    kb = []
    for k in broadcast_vars:
        val = ov.get(k)
        badge = f" = <code>{val}</code>" if val else " <i>(placeholder)</i>"
        lines.append(f"  • <code>{{{k}}}</code>{badge}")
        kb.append([InlineKeyboardButton(
            f"✏️ {k}",
            callback_data=f"abt:var_edit:{k}")])

    if ov:
        kb.append([InlineKeyboardButton("🗑 Clear All Variables", callback_data="abt:vars_clear")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"abt:compose:{btype}")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def abt_var_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to enter a variable value (abt:var_edit:<key>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    parts = query.data.split(":")
    vkey  = parts[2] if len(parts) > 2 else ""
    data  = _get_data(context)
    data["_var_key"] = vkey
    ov = data.get("variables", {})
    current = ov.get(vkey, "")

    await _safe_edit(query,
        f"✏️ <b>Set Variable: <code>{{{vkey}}}</code></b>\n\n"
        f"Current value: <code>{current or '(none)'}</code>\n\n"
        f"Send the new value, or /cancel to keep it unchanged.",
        _back_kb("abt:variables"))
    return ABT_VAR_VALUE


async def abt_var_value_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a variable value."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt  = (update.message.text or "").strip()
    data = _get_data(context)
    vkey = data.pop("_var_key", None)
    btype = data.get("type", "custom")
    if vkey:
        data.setdefault("variables", {})[vkey] = txt
    await update.message.reply_text(
        f"✅ Variable <code>{{{vkey}}}</code> set to: <code>{txt}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Variables", callback_data="abt:variables"),
            InlineKeyboardButton("📋 Compose",           callback_data=f"abt:compose:{btype}"),
        ]]))
    return ConversationHandler.END


async def abt_vars_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all variable overrides (abt:vars_clear)."""
    query = update.callback_query
    await query.answer()
    _get_data(context)["variables"] = {}
    return await abt_variables(with_data(update, "abt:variables"), context)


# ── Audience preview ───────────────────────────────────────────────────────────

async def abt_audience_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live audience count + delivery estimate (abt:audience_preview)."""
    query = update.callback_query
    await query.answer("Counting audience…")
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_audience_preview_enabled", True):
        await query.answer("📊 Audience preview is disabled.", show_alert=True)
        return

    data    = _get_data(context)
    btype   = data.get("type", "custom")
    seg     = data.get("primary_segment", "all")
    ef      = data.get("extra_filters", {})

    try:
        count   = await __import__("asyncio").to_thread(count_audience, seg, ef)
        est_s   = estimate_delivery_seconds(count)
        est_str = _fmt_seconds(est_s)
        exp_msg = count  # 1 message per user

        seg_label = SEGMENTS.get(seg, seg)
        ef_count  = len(ef)

        text = (
            f"📊 <b>Audience Preview</b>\n\n"
            f"<b>Type:</b> {BROADCAST_TYPES.get(btype, {}).get('label', btype)}\n"
            f"<b>Segment:</b> {seg_label}\n"
            f"<b>Extra Filters:</b> {ef_count}\n\n"
            f"👥 <b>Total Matching Users:</b> {count:,}\n"
            f"📨 <b>Expected Messages:</b> {exp_msg:,}\n"
            f"⏱ <b>Estimated Delivery:</b> {est_str}\n"
        )
        if count == 0:
            text += "\n⚠️ <b>Empty audience!</b> No users match the current filters."
    except Exception:
        logger.exception("abt_audience_preview: error")
        text = "❌ Could not load audience statistics. Check logs."

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send Now",   callback_data="abt:send_now"),
        InlineKeyboardButton("🔙 Compose",    callback_data=f"abt:compose:{data.get('type', 'custom')}"),
    ]])
    await _safe_edit(query, text, kb)


# ── Preview message ────────────────────────────────────────────────────────────

async def abt_preview_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview the final rendered message for the first matching user (abt:preview_msg)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    data    = _get_data(context)
    btype   = data.get("type", "custom")
    seg     = data.get("primary_segment", "all")
    ef      = data.get("extra_filters", {})
    tmpl    = data.get("template", "")
    ov      = data.get("variables", {})

    try:
        rendered, username = await __import__("asyncio").to_thread(
            preview_for_first_user, seg, tmpl, ef, ov)
        user_note = f" (for @{username})" if username else " (placeholder values)"
    except Exception:
        logger.exception("abt_preview_msg error")
        rendered  = tmpl
        user_note = " (raw template)"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🧪 Test → Me",   callback_data="abt:test_self"),
        InlineKeyboardButton("📤 Send Now",     callback_data="abt:send_now"),
        InlineKeyboardButton("🔙 Compose",      callback_data=f"abt:compose:{btype}"),
    ]])
    header = f"👁 <b>Message Preview{user_note}</b>\n\n"
    try:
        await query.edit_message_text(
            header + rendered, parse_mode="HTML", reply_markup=kb)
    except BadRequest:
        await query.message.reply_text(
            header + rendered, parse_mode="HTML", reply_markup=kb)


# ── Test send ──────────────────────────────────────────────────────────────────

async def abt_test_self(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send test message to the admin (abt:test_self)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_test_mode_enabled", True):
        await query.answer("🧪 Test mode is disabled.", show_alert=True)
        return

    data     = _get_data(context)
    tmpl     = data.get("template", "")
    ov       = data.get("variables", {})
    admin_id = settings.ADMIN_TELEGRAM_ID

    try:
        rendered = substitute_variables(tmpl, None, ov)
        header   = f"🧪 <b>TEST MESSAGE</b>\n\n"
        await context.bot.send_message(admin_id, header + rendered, parse_mode="HTML")
        await query.answer("🧪 Test message sent to you!", show_alert=True)
        log_admin_action(update.effective_user.id, "abt.test_self",
                         "advanced_broadcast", 0, f"type={data.get('type')}",
                         module="admin_broadcast_types")
    except Exception as exc:
        logger.exception("abt_test_self failed")
        await query.answer(f"❌ Test failed: {str(exc)[:100]}", show_alert=True)


async def abt_test_user_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for a Telegram ID to send a test message to (abt:test_user_ask)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    if not cfg.get_bool("broadcast_test_mode_enabled", True):
        await query.answer("🧪 Test mode is disabled.", show_alert=True)
        return ConversationHandler.END

    await _safe_edit(query,
        "🧪 <b>Test Send → Selected User</b>\n\n"
        "Send the <b>Telegram ID</b> of the user to test this broadcast on:",
        _back_kb(f"abt:compose:{_get_data(context).get('type', 'custom')}"))
    return ABT_TEST_USER


async def abt_test_user_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the target Telegram ID and send test message."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt  = (update.message.text or "").strip()
    data = _get_data(context)
    btype = data.get("type", "custom")
    try:
        target_id = int(txt)
    except ValueError:
        await update.message.reply_text("❌ Invalid Telegram ID. Send a number:")
        return ABT_TEST_USER

    tmpl = data.get("template", "")
    ov   = data.get("variables", {})

    try:
        # Fetch user from DB for personalization
        with get_db_session() as s:
            user = s.query(__import__("database.models", fromlist=["User"]).User
                           ).filter_by(telegram_id=target_id).first()
        rendered = substitute_variables(tmpl, user, ov)
        header   = f"🧪 <b>TEST MESSAGE (sent by admin)</b>\n\n"
        await context.bot.send_message(target_id, header + rendered, parse_mode="HTML")
        await update.message.reply_text(
            f"✅ Test message sent to Telegram ID <code>{target_id}</code>!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Compose", callback_data=f"abt:compose:{btype}")
            ]]))
        log_admin_action(update.effective_user.id, "abt.test_user",
                         "advanced_broadcast", 0,
                         f"type={btype} target={target_id}",
                         module="admin_broadcast_types")
    except Exception as exc:
        logger.exception("abt_test_user_recv: send failed to %d", target_id)
        await update.message.reply_text(
            f"❌ Test send failed: <code>{str(exc)[:200]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Compose", callback_data=f"abt:compose:{btype}")
            ]]))
    return ConversationHandler.END


# ── Send now ───────────────────────────────────────────────────────────────────

async def abt_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate audience and fire the broadcast immediately (abt:send_now)."""
    query = update.callback_query
    await query.answer("Preparing broadcast…")
    if not _is_admin(update.effective_user.id):
        return

    if _maintenance():
        await _safe_edit(query,
            "⚠️ <b>Maintenance Mode</b>\n\nBroadcasts are paused. "
            "Disable maintenance mode in Settings first.",
            _back_kb("abt:settings"))
        return

    data  = _get_data(context)
    btype = data.get("type", "custom")
    seg   = data.get("primary_segment", "all")
    ef    = data.get("extra_filters", {})
    tmpl  = data.get("template", "")
    ov    = data.get("variables", {})

    import asyncio as _asyncio

    try:
        audience = await _asyncio.to_thread(resolve_audience, seg, ef)
    except Exception:
        logger.exception("abt_send_now: resolve_audience failed")
        await _safe_edit(query, "❌ Failed to resolve audience.", _back_kb(f"abt:compose:{btype}"))
        return

    if not audience:
        await _safe_edit(query,
            "⚠️ <b>Empty Audience</b>\n\n"
            "No users match the selected filters. Adjust your audience and try again.",
            _back_kb(f"abt:compose:{btype}"))
        return

    # Persist as ScheduledBroadcast (status=sending so the job picks it up immediately)
    try:
        with get_db_session() as s:
            br = ScheduledBroadcast(
                title            = f"[{BROADCAST_TYPES.get(btype, {}).get('label', btype)}] "
                                   f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                message_text     = tmpl,
                media_type       = "text",
                target_segment   = seg,
                target_user_ids  = json.dumps([t for t, _ in audience]),
                status           = "scheduled",
                scheduled_at     = datetime.utcnow(),
                is_recurring     = False,
                created_by       = update.effective_user.id,
                created_at       = datetime.utcnow(),
                updated_at       = datetime.utcnow(),
                broadcast_type   = btype,
                audience_filters_json = json.dumps(ef),
                template_used    = BROADCAST_TYPES.get(btype, {}).get("label"),
                variables_json   = json.dumps(ov) if ov else None,
            )
            s.add(br)
            s.commit()
            bid = br.id
    except Exception:
        logger.exception("abt_send_now: DB persist failed")
        await _safe_edit(query, "❌ Failed to save broadcast.", _back_kb(f"abt:compose:{btype}"))
        return

    est_s   = estimate_delivery_seconds(len(audience))
    est_str = _fmt_seconds(est_s)
    count   = len(audience)

    log_admin_action(
        update.effective_user.id, "abt.send_now",
        "advanced_broadcast", bid,
        f"type={btype} seg={seg} recipients={count}",
        module="admin_broadcast_types",
    )

    # Clear compose state
    context.user_data.pop("_abt", None)

    text = (
        f"📤 <b>Broadcast Queued!</b>\n\n"
        f"<b>Type:</b> {BROADCAST_TYPES.get(btype, {}).get('label', btype)}\n"
        f"<b>Broadcast ID:</b> #{bid}\n"
        f"<b>Recipients:</b> {count:,}\n"
        f"<b>Estimated Delivery:</b> {est_str}\n\n"
        "The broadcast has been added to the scheduled broadcast queue.\n"
        "Track it via 📢 Broadcast Center → asb:view."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 View Broadcast", callback_data=f"asb:view:{bid}"),
        InlineKeyboardButton("🔙 Broadcast Menu", callback_data="abt:menu"),
    ]])
    await _safe_edit(query, text, kb)


# ── Schedule ───────────────────────────────────────────────────────────────────

async def abt_schedule_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for a schedule datetime (abt:schedule_ask)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    await _safe_edit(query,
        "📅 <b>Schedule Broadcast</b>\n\n"
        "Send the datetime in: <code>YYYY-MM-DD HH:MM</code> (UTC)\n\n"
        "Example: <code>2026-09-20 14:30</code>\n\n"
        "Or send /cancel to go back.",
        _back_kb(f"abt:compose:{_get_data(context).get('type', 'custom')}"))
    return ABT_SCHEDULE


async def abt_schedule_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and persist a scheduled broadcast."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt  = (update.message.text or "").strip()
    data = _get_data(context)
    btype = data.get("type", "custom")

    try:
        sched_at = datetime.strptime(txt, "%Y-%m-%d %H:%M")
        if sched_at <= datetime.utcnow():
            raise ValueError("Past date")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid datetime or date is in the past.\n"
            "Format: <code>YYYY-MM-DD HH:MM</code> (UTC)",
            parse_mode="HTML")
        return ABT_SCHEDULE

    seg = data.get("primary_segment", "all")
    ef  = data.get("extra_filters", {})
    ov  = data.get("variables", {})
    tmpl = data.get("template", "")

    try:
        audience = resolve_audience(seg, ef)
    except Exception:
        audience = []

    try:
        with get_db_session() as s:
            br = ScheduledBroadcast(
                title            = f"[{BROADCAST_TYPES.get(btype, {}).get('label', btype)}] "
                                   f"{sched_at.strftime('%Y-%m-%d %H:%M')}",
                message_text     = tmpl,
                media_type       = "text",
                target_segment   = seg,
                target_user_ids  = json.dumps([t for t, _ in audience]) if audience else None,
                status           = "scheduled",
                scheduled_at     = sched_at,
                is_recurring     = False,
                created_by       = update.effective_user.id,
                created_at       = datetime.utcnow(),
                updated_at       = datetime.utcnow(),
                broadcast_type   = btype,
                audience_filters_json = json.dumps(ef),
                template_used    = BROADCAST_TYPES.get(btype, {}).get("label"),
                variables_json   = json.dumps(ov) if ov else None,
            )
            s.add(br)
            s.commit()
            bid = br.id
    except Exception:
        logger.exception("abt_schedule_recv: DB persist failed")
        await update.message.reply_text("❌ Failed to save scheduled broadcast.")
        return ConversationHandler.END

    log_admin_action(
        update.effective_user.id, "abt.schedule",
        "advanced_broadcast", bid,
        f"type={btype} at={sched_at} recipients={len(audience)}",
        module="admin_broadcast_types",
    )
    context.user_data.pop("_abt", None)

    await update.message.reply_text(
        f"📅 <b>Broadcast Scheduled!</b>\n\n"
        f"<b>Type:</b> {BROADCAST_TYPES.get(btype, {}).get('label', btype)}\n"
        f"<b>ID:</b> #{bid}\n"
        f"<b>Scheduled for:</b> {sched_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"<b>Est. Recipients:</b> {len(audience):,}\n",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 View Broadcast", callback_data=f"asb:view:{bid}"),
            InlineKeyboardButton("🔙 Broadcast Menu", callback_data="abt:menu"),
        ]]))
    return ConversationHandler.END


# ── Settings ───────────────────────────────────────────────────────────────────

async def abt_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advanced Broadcast Types settings page (abt:settings)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    status     = cfg.get("advanced_broadcast_types_status", "enabled")
    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")

    def tf(v: bool) -> str:
        return "✅ ON" if v else "❌ OFF"

    smart      = cfg.get_bool("broadcast_smart_filters_enabled", True)
    types_on   = cfg.get_bool("broadcast_types_enabled", True)
    vars_on    = cfg.get_bool("broadcast_variables_enabled", True)
    test_on    = cfg.get_bool("broadcast_test_mode_enabled", True)
    preview_on = cfg.get_bool("broadcast_audience_preview_enabled", True)

    text = (
        f"⚙️ <b>Advanced Broadcast Types — Settings</b>\n\n"
        f"<b>Feature Status:</b> {status_icon} {status.capitalize()}\n\n"
        f"<b>Smart Filters:</b>      {tf(smart)}\n"
        f"<b>Broadcast Types UI:</b> {tf(types_on)}\n"
        f"<b>Variables:</b>          {tf(vars_on)}\n"
        f"<b>Test Mode:</b>          {tf(test_on)}\n"
        f"<b>Audience Preview:</b>   {tf(preview_on)}\n"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="abt:settings:status:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="abt:settings:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="abt:settings:status:disabled"),
        ],
        [
            InlineKeyboardButton(f"Smart Filters: {'ON ✅' if smart else 'OFF ❌'}",
                                  callback_data="abt:settings:toggle:broadcast_smart_filters_enabled"),
        ],
        [
            InlineKeyboardButton(f"Types UI: {'ON ✅' if types_on else 'OFF ❌'}",
                                  callback_data="abt:settings:toggle:broadcast_types_enabled"),
            InlineKeyboardButton(f"Variables: {'ON ✅' if vars_on else 'OFF ❌'}",
                                  callback_data="abt:settings:toggle:broadcast_variables_enabled"),
        ],
        [
            InlineKeyboardButton(f"Test Mode: {'ON ✅' if test_on else 'OFF ❌'}",
                                  callback_data="abt:settings:toggle:broadcast_test_mode_enabled"),
            InlineKeyboardButton(f"Audience Preview: {'ON ✅' if preview_on else 'OFF ❌'}",
                                  callback_data="abt:settings:toggle:broadcast_audience_preview_enabled"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="abt:menu")],
    ])
    await _safe_edit(query, text, kb)


async def abt_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set feature status (abt:settings:status:<value>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    status = query.data.split(":")[-1]
    if status in ("enabled", "maintenance", "disabled"):
        cfg.set("advanced_broadcast_types_status", status)
        log_admin_action(update.effective_user.id, "abt.settings.status",
                         "advanced_broadcast", 0, f"status={status}",
                         module="admin_broadcast_types")
    await abt_settings(update, context)


async def abt_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a boolean setting (abt:settings:toggle:<key>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    key     = query.data.split(":")[-1]
    current = cfg.get_bool(key, True)
    cfg.set(key, "false" if current else "true")
    log_admin_action(update.effective_user.id, "abt.settings.toggle",
                     "advanced_broadcast", 0,
                     f"{key}: {current} → {not current}",
                     module="admin_broadcast_types")
    await abt_settings(update, context)


# ── Cancel conversation ────────────────────────────────────────────────────────

async def abt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current Advanced Broadcast conversation."""
    context.user_data.pop("_abt", None)
    if update.message:
        await update.message.reply_text(
            "❌ Cancelled.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="abt:menu")
            ]]))
    elif update.callback_query:
        await update.callback_query.answer("Cancelled.")
    return ConversationHandler.END


# ── Conversation handler ───────────────────────────────────────────────────────

def build_abt_conv() -> ConversationHandler:
    """Build the Advanced Broadcast Types conversation handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(abt_edit_template_start, pattern=r"^abt:edit_template$"),
            CallbackQueryHandler(abt_audience_sel,        pattern=r"^abt:audience_sel:.+$"),
            CallbackQueryHandler(abt_filter_toggle,       pattern=r"^abt:filter_toggle:.+$"),
            CallbackQueryHandler(abt_var_edit,            pattern=r"^abt:var_edit:.+$"),
            CallbackQueryHandler(abt_test_user_ask,       pattern=r"^abt:test_user_ask$"),
            CallbackQueryHandler(abt_schedule_ask,        pattern=r"^abt:schedule_ask$"),
        ],
        states={
            ABT_TEMPLATE_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, abt_receive_template),
            ],
            ABT_SCHEDULE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, abt_schedule_recv),
            ],
            ABT_TEST_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, abt_test_user_recv),
            ],
            ABT_FILTER_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, abt_filter_value_recv),
            ],
            ABT_VAR_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, abt_var_value_recv),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(abt_cancel,  pattern=r"^abt:cancel$"),
            CommandHandler("cancel",           abt_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
