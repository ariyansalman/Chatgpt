"""Enhanced Maintenance Mode Management — V20.

Callback namespace: ``maint:*``

Extends the basic maintenance_mode bot_config toggle with:
  • Custom maintenance message (inherited from existing maintenance_message key)
  • Estimated return time (new key: maintenance_estimated_return)
  • User whitelist — bypass maintenance (new key: maintenance_whitelist,
    comma-separated Telegram user IDs)
  • Emergency contact (just stored in maintenance message as text)
  • Full admin panel for all settings
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states (non-colliding) ───────────────────────────────────────
MAINT_MSG        = 60
MAINT_RETURN     = 61
MAINT_WL_ADD     = 62
MAINT_ANNOUNCE   = 63


def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _build_back_kb(target: str = "maint:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=target)]])


def _parse_whitelist() -> List[int]:
    raw = cfg.get_str("maintenance_whitelist", "")
    result = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def _format_whitelist(ids: List[int]) -> str:
    return ",".join(str(i) for i in ids)


# ─────────────────────────────────────────────────────────────────────────────
# Main maintenance panel
# ─────────────────────────────────────────────────────────────────────────────

async def maintenance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced maintenance management panel (maint:menu)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    mode_on = cfg.get_bool("maintenance_mode", False)
    msg = cfg.get_str("maintenance_message",
                      "🔧 The bot is under maintenance. Please try again shortly.")
    return_time = cfg.get_str("maintenance_estimated_return", "")
    whitelist = _parse_whitelist()

    lines = [
        "🔧 <b>Maintenance Mode Manager</b>\n",
        f"Status: {'🟢 <b>ACTIVE</b>' if mode_on else '⚪ OFF'}",
        "",
        f"📝 <b>Message:</b>\n<i>{msg[:200]}</i>",
    ]
    if return_time:
        lines.append(f"\n⏰ <b>Est. Return:</b> {return_time}")
    if whitelist:
        lines.append(f"\n✅ <b>Whitelist ({len(whitelist)} users):</b> "
                     + ", ".join(f"<code>{uid}</code>" for uid in whitelist[:10])
                     + (" …" if len(whitelist) > 10 else ""))
    else:
        lines.append("\n✅ <b>Whitelist:</b> empty (only admins bypass)")

    kb: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "🔴 Disable Maintenance" if mode_on else "🟢 Enable Maintenance",
            callback_data="maint:toggle",
        )],
        [InlineKeyboardButton("📝 Set Message", callback_data="maint:set_msg"),
         InlineKeyboardButton("⏰ Set Return Time", callback_data="maint:set_return")],
        [InlineKeyboardButton("✅ Manage Whitelist", callback_data="maint:wl")],
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="acc:root")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def maintenance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle maintenance mode on/off (maint:toggle)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    current = cfg.get_bool("maintenance_mode", False)
    cfg.set("maintenance_mode", not current)
    state = "ENABLED" if not current else "DISABLED"
    await query.answer(f"🔧 Maintenance mode {state}.", show_alert=False)
    await maintenance_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Set message conversation
# ─────────────────────────────────────────────────────────────────────────────

async def maintenance_set_msg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start set-message conversation (maint:set_msg)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    cur = cfg.get_str("maintenance_message",
                      "🔧 The bot is under maintenance. Please try again shortly.")
    await _safe_edit(
        query,
        f"📝 <b>Set Maintenance Message</b>\n\nCurrent:\n<i>{cur}</i>\n\n"
        f"Send the new message (supports HTML):",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="maint:menu")
        ]]),
    )
    return MAINT_MSG


async def maintenance_set_msg_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new maintenance message."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Message cannot be empty.")
        return MAINT_MSG

    cfg.set("maintenance_message", text)
    await update.message.reply_text(
        f"✅ Maintenance message updated.\n\n<i>{text[:300]}</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="maint:menu")
        ]]),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Set return time conversation
# ─────────────────────────────────────────────────────────────────────────────

async def maintenance_set_return_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start set-return-time conversation (maint:set_return)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    cur = cfg.get_str("maintenance_estimated_return", "")
    await _safe_edit(
        query,
        f"⏰ <b>Set Estimated Return Time</b>\n\n"
        f"Current: <b>{cur or '(not set)'}</b>\n\n"
        f"Send a human-readable time (e.g. <code>~2 hours</code>, <code>14:00 UTC</code>).\n"
        f"Send <code>clear</code> to remove it.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="maint:menu")
        ]]),
    )
    return MAINT_RETURN


async def maintenance_set_return_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive estimated return time."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text.lower() == "clear":
        cfg.set("maintenance_estimated_return", "")
        await update.message.reply_text(
            "✅ Return time cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="maint:menu")
            ]]),
        )
    else:
        cfg.set("maintenance_estimated_return", text[:100])
        await update.message.reply_text(
            f"✅ Estimated return time set to: <b>{text[:100]}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="maint:menu")
            ]]),
        )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Whitelist management
# ─────────────────────────────────────────────────────────────────────────────

async def maintenance_whitelist_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show whitelist management panel (maint:wl)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    whitelist = _parse_whitelist()
    lines = [
        "✅ <b>Maintenance Whitelist</b>\n",
        "Users on this list can access the bot even when maintenance mode is active.",
        "",
        f"Current whitelist ({len(whitelist)} users):",
    ]
    if whitelist:
        for uid in whitelist:
            lines.append(f"  • <code>{uid}</code>")
    else:
        lines.append("  (empty — only admins bypass maintenance)")

    kb = [[InlineKeyboardButton("➕ Add User", callback_data="maint:wl:add")]]
    for uid in whitelist:
        kb.append([InlineKeyboardButton(
            f"🗑 Remove {uid}", callback_data=f"maint:wl:rm:{uid}"
        )])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="maint:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def maintenance_whitelist_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add-to-whitelist conversation (maint:wl:add)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    await _safe_edit(
        query,
        "➕ <b>Add User to Maintenance Whitelist</b>\n\nSend the Telegram user ID to add:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="maint:wl")
        ]]),
    )
    return MAINT_WL_ADD


async def maintenance_whitelist_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive user ID to add to whitelist."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    try:
        uid = int(text)
        if uid <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a valid Telegram user ID (positive integer).")
        return MAINT_WL_ADD

    whitelist = _parse_whitelist()
    if uid not in whitelist:
        whitelist.append(uid)
        cfg.set("maintenance_whitelist", _format_whitelist(whitelist))

    await update.message.reply_text(
        f"✅ User <code>{uid}</code> added to maintenance whitelist.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Whitelist", callback_data="maint:wl")
        ]]),
    )
    return ConversationHandler.END


async def maintenance_whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a user from the whitelist (maint:wl:rm:<id>)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        uid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    whitelist = _parse_whitelist()
    if uid in whitelist:
        whitelist.remove(uid)
        cfg.set("maintenance_whitelist", _format_whitelist(whitelist))
        await query.answer(f"🗑 User {uid} removed from whitelist.", show_alert=False)

    await maintenance_whitelist_view(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation builders
# ─────────────────────────────────────────────────────────────────────────────

def build_maintenance_convs():
    """Return list of all maintenance-related conversation handlers."""
    from telegram.ext import (
        ConversationHandler as CH, CallbackQueryHandler as CQH,
        MessageHandler, filters, CommandHandler,
    )
    convs = []

    # Set message conversation
    convs.append(CH(
        entry_points=[CQH(maintenance_set_msg_start, pattern=r"^maint:set_msg$")],
        states={
            MAINT_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, maintenance_set_msg_input),
                CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:menu$"),
            ],
        },
        fallbacks=[CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:menu$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))

    # Set return time conversation
    convs.append(CH(
        entry_points=[CQH(maintenance_set_return_start, pattern=r"^maint:set_return$")],
        states={
            MAINT_RETURN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, maintenance_set_return_input),
                CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:menu$"),
            ],
        },
        fallbacks=[CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:menu$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))

    # Add to whitelist conversation
    convs.append(CH(
        entry_points=[CQH(maintenance_whitelist_add_start, pattern=r"^maint:wl:add$")],
        states={
            MAINT_WL_ADD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, maintenance_whitelist_add_input),
                CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:wl$"),
            ],
        },
        fallbacks=[CQH(lambda u, c: ConversationHandler.END, pattern=r"^maint:wl$")],
        per_user=True, per_chat=True, allow_reentry=True,
    ))

    return convs
