"""Admin Anti-Spam & Auto-Moderation Handler — V40.

Callback namespace: aasm:*

Sub-namespaces:
  aasm:menu        — Anti-spam center main menu
  aasm:violations  — Recent violations log
  aasm:search      — Search user by ID/username
  aasm:user:<id>   — User moderation detail
  aasm:mute:<id>   — Mute a user
  aasm:unmute:<id> — Unmute a user
  aasm:ban:<id>    — Ban a user
  aasm:unban:<id>  — Unban a user
  aasm:wl:<id>     — Whitelist a user
  aasm:clrwarn:<id>— Clear user warnings
  aasm:bl          — Blacklist management
  aasm:wllist      — Whitelist list
  aasm:settings    — Anti-spam settings
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, CallbackQueryHandler, ConversationHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from services import anti_spam as asp
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from database import get_db_session
from database.models import User

logger = logging.getLogger(__name__)

# Conversation states
SEARCH_INPUT, MUTE_DURATION, BAN_DURATION, BL_WORD_INPUT = range(200, 204)


def _back_kb(cb: str = "aasm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _status_icon(st: dict) -> str:
    if st.get("is_banned"):  return "🚫"
    if st.get("is_muted"):   return "🔇"
    if st.get("is_in_cooldown"): return "⏳"
    if st.get("needs_captcha"):  return "🤖"
    return "✅"


def _resolve_tg_id(text: str) -> Optional[int]:
    """Return telegram_id from numeric ID or @username lookup."""
    text = text.strip().lstrip("@")
    try:
        return int(text)
    except ValueError:
        with get_db_session() as s:
            u = s.query(User).filter(User.username == text).first()
            return u.telegram_id if u else None


# ─── Main Menu ────────────────────────────────────────────────────────────────

async def aasm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🛡 Anti-Spam Center main screen."""
    q = update.callback_query
    if q:
        await q.answer()

    if not has_permission(update.effective_user.id, "manage_users", check_2fa=False):
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    stats = asp.get_stats()
    status_val  = cfg.get("antispam_status", "enabled")
    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status_val, "⚪")

    lines = [
        f"🛡 <b>Anti-Spam & Auto-Moderation</b>\n",
        f"Status: {status_icon} <b>{status_val.title()}</b>\n",
        f"🚫 Spam attempts (total): <b>{stats.get('spam_attempts', 0)}</b>",
        f"📅 Spam attempts (today): <b>{stats.get('spam_today', 0)}</b>",
        f"🚫 Blocked users:  <b>{stats.get('blocked_users', 0)}</b>",
        f"🔇 Muted users:    <b>{stats.get('muted_users', 0)}</b>",
        f"🚫 Banned users:   <b>{stats.get('banned_users', 0)}</b>",
        f"⚠️ With warnings:  <b>{stats.get('total_warnings', 0)}</b>",
        f"🤖 Captcha pending: <b>{stats.get('captcha_pending', 0)}</b>",
        f"✅ Whitelisted:    <b>{stats.get('whitelisted', 0)}</b>",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Violations Log",  callback_data="aasm:violations"),
         InlineKeyboardButton("🔍 Search User",     callback_data="aasm:search:start")],
        [InlineKeyboardButton("🚫 Blacklist",       callback_data="aasm:bl"),
         InlineKeyboardButton("✅ Whitelist",       callback_data="aasm:wllist")],
        [InlineKeyboardButton("⚙️ Settings",        callback_data="aasm:settings"),
         InlineKeyboardButton("🔙 Back",            callback_data="acc:root")],
    ])
    try:
        if q:
            await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_menu: %s", e)


# ─── Violations Log ───────────────────────────────────────────────────────────

async def aasm_violations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_users", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    violations = asp.get_recent_violations(20)
    lines = ["📋 <b>Recent Violations</b>\n"]
    if not violations:
        lines.append("<i>No violations recorded.</i>")
    else:
        for v in violations:
            when = v["created_at"].strftime("%m/%d %H:%M") if v["created_at"] else "?"
            user = v["username"] or f"ID:{v['telegram_id']}"
            lines.append(
                f"• <b>{user}</b> [{v['violation_type']}] → {v['action_taken']} {when}"
            )
            if v["detail"]:
                lines.append(f"  <i>{v['detail'][:60]}</i>")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",   callback_data="aasm:violations"),
         InlineKeyboardButton("🔙 Back",      callback_data="aasm:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_violations: %s", e)


# ─── User Search (conversation) ───────────────────────────────────────────────

async def aasm_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    try:
        await q.edit_message_text(
            "🔍 <b>Search User</b>\n\nEnter Telegram ID or @username:\n\n/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return SEARCH_INPUT


async def aasm_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    tg_id = _resolve_tg_id(text)
    if not tg_id:
        await update.message.reply_text("❌ User not found. Try again or /cancel:")
        return SEARCH_INPUT
    context.user_data["aasm_tg_id"] = tg_id
    await update.message.reply_text("✅ Found! Loading profile…")
    # Simulate callback data
    update.callback_query = None
    await _show_user_detail(update, context, tg_id)
    return ConversationHandler.END


async def aasm_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Search cancelled.")
    return ConversationHandler.END


def build_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aasm_search_start, pattern=r"^aasm:search:start$")],
        states={
            SEARCH_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, aasm_search_input)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aasm_search_cancel)],
        allow_reentry=True,
    )


# ─── User Detail ─────────────────────────────────────────────────────────────

async def _show_user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             tg_id: int) -> None:
    """Show moderation detail for a specific user."""
    info = asp.search_user_violations(tg_id)
    st   = info["status"]
    icon = _status_icon(st)

    with get_db_session() as s:
        u = s.query(User).filter(User.telegram_id == tg_id).first()
        uname = f"@{u.username}" if u and u.username else f"ID:{tg_id}"

    lines = [
        f"👤 <b>User: {uname}</b> (TG: {tg_id})\n",
        f"Status: {icon} <b>{st.get('status', 'active').title()}</b>",
        f"Warnings: <b>{st.get('warning_count', 0)}</b>   "
        f"Total violations: <b>{st.get('total_violations', 0)}</b>",
        f"Muted: <b>{'Yes' if st.get('is_muted') else 'No'}</b>"
        + (f" until {st['mute_expires_at'].strftime('%m/%d %H:%M')}" if st.get('mute_expires_at') else ""),
        f"Banned: <b>{'Yes' if st.get('is_banned') else 'No'}</b>"
        + (f" until {st['ban_expires_at'].strftime('%m/%d %H:%M')}" if st.get('ban_expires_at') else ""),
        "",
    ]
    if info["violations"]:
        lines.append("<b>Last violations:</b>")
        for v in info["violations"][:4]:
            when = v["when"].strftime("%m/%d %H:%M") if v["when"] else "?"
            lines.append(f"  • [{v['type']}] → {v['action']} {when}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔇 Mute",    callback_data=f"aasm:mute:{tg_id}:300"),
         InlineKeyboardButton("🔊 Unmute",  callback_data=f"aasm:unmute:{tg_id}")],
        [InlineKeyboardButton("🚫 Ban 24h", callback_data=f"aasm:ban:{tg_id}:86400"),
         InlineKeyboardButton("🚫 Perm Ban",callback_data=f"aasm:ban:{tg_id}:0")],
        [InlineKeyboardButton("✅ Unban",   callback_data=f"aasm:unban:{tg_id}"),
         InlineKeyboardButton("⚠️ Clr Warn",callback_data=f"aasm:clrwarn:{tg_id}")],
        [InlineKeyboardButton("✅ Whitelist",callback_data=f"aasm:wl:{tg_id}"),
         InlineKeyboardButton("🚫 Blacklist",callback_data=f"aasm:bluser:{tg_id}")],
        [InlineKeyboardButton("🔙 Back",    callback_data="aasm:menu")],
    ])
    text = "\n".join(lines)
    msg  = update.effective_message
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("_show_user_detail: %s", e)


async def aasm_user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    await _show_user_detail(update, context, tg_id)


# ─── Moderation Actions ───────────────────────────────────────────────────────

async def aasm_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts  = (q.data or "").split(":")
    tg_id  = int(parts[2]) if len(parts) >= 3 else 0
    dur    = int(parts[3]) if len(parts) >= 4 else 300
    try:
        asp.admin_mute(tg_id, dur, reason="Admin mute", actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.mute",
                         target_type="user", target_id=tg_id,
                         details=f"duration={dur}s")
        await q.answer(f"✅ User {tg_id} muted for {dur//60}m.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


async def aasm_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        asp.admin_unmute(tg_id, reason="Admin unmute", actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.unmute",
                         target_type="user", target_id=tg_id)
        await q.answer(f"✅ User {tg_id} unmuted.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


async def aasm_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    dur   = int(parts[3]) if len(parts) >= 4 else 86400
    perm  = (dur == 0)
    try:
        asp.admin_ban(tg_id, permanent=perm, duration_secs=dur,
                      reason="Admin ban", actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.ban",
                         target_type="user", target_id=tg_id,
                         details=f"perm={perm} dur={dur}s")
        label = "permanently" if perm else f"for {dur//3600}h"
        await q.answer(f"✅ User {tg_id} banned {label}.", show_alert=True)
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


async def aasm_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        asp.admin_unban(tg_id, reason="Admin unban", actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.unban",
                         target_type="user", target_id=tg_id)
        await q.answer(f"✅ User {tg_id} unbanned.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


async def aasm_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        asp.admin_add_whitelist(tg_id, entry_type="trusted",
                                reason="Admin whitelist",
                                actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.whitelist",
                         target_type="user", target_id=tg_id)
        await q.answer(f"✅ User {tg_id} whitelisted.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


async def aasm_bl_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        asp.admin_add_blacklist_user(tg_id, reason="Admin blacklist",
                                     actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.blacklist_user",
                         target_type="user", target_id=tg_id)
        await q.answer(f"✅ User {tg_id} blacklisted.", show_alert=True)
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)


async def aasm_clear_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_users"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    tg_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        asp.admin_clear_warnings(tg_id, actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.clear_warnings",
                         target_type="user", target_id=tg_id)
        await q.answer(f"✅ Warnings cleared for {tg_id}.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return
    await _show_user_detail(update, context, tg_id)


# ─── Blacklist Management ─────────────────────────────────────────────────────

async def aasm_bl_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Blacklist management view."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_users", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    users = asp.get_all_blacklist("user")
    words = asp.get_all_blacklist("word")

    lines = [
        "🚫 <b>Blacklist</b>\n",
        f"Blocked users: <b>{len(users)}</b>",
        f"Blocked words: <b>{len(words)}</b>",
        "",
        "<b>Blocked Words:</b>",
    ]
    for w in words[:15]:
        lines.append(f"  • <code>{w['value']}</code>")
    if len(words) > 15:
        lines.append(f"  … and {len(words)-15} more")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Word",      callback_data="aasm:bl:addword"),
         InlineKeyboardButton("👤 Blocked Users", callback_data="aasm:bl:users")],
        [InlineKeyboardButton("🔙 Back",          callback_data="aasm:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_bl_menu: %s", e)


async def aasm_bl_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List blacklisted users."""
    q = update.callback_query
    await q.answer()
    users = asp.get_all_blacklist("user")
    lines = ["🚫 <b>Blacklisted Users</b>\n"]
    for u in users[:20]:
        lines.append(f"• TG:{u['value']}  <i>{u.get('reason','')[:30]}</i>")
    if not users:
        lines.append("<i>No blacklisted users.</i>")
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=_back_kb("aasm:bl"),
                                  parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_bl_users: %s", e)


async def aasm_bl_addword_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    try:
        await q.edit_message_text(
            "➕ <b>Add Blacklisted Word</b>\n\nType the word or phrase to block:\n\n/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return BL_WORD_INPUT


async def aasm_bl_addword_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = (update.message.text or "").strip().lower()
    if not word or len(word) > 200:
        await update.message.reply_text("❌ Word must be 1-200 chars:")
        return BL_WORD_INPUT
    try:
        asp.admin_add_blacklist_word(word, reason="Admin added",
                                     actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "antispam.blacklist_word",
                         details=f"word={word}")
        await update.message.reply_text(f"✅ Word '<code>{word}</code>' added to blacklist.",
                                         parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


async def aasm_addword_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


def build_addword_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aasm_bl_addword_start, pattern=r"^aasm:bl:addword$")],
        states={
            BL_WORD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, aasm_bl_addword_input)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aasm_addword_cancel)],
        allow_reentry=True,
    )


# ─── Whitelist view ───────────────────────────────────────────────────────────

async def aasm_wl_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_users", check_2fa=False):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    entries = asp.get_all_whitelist()
    lines   = ["✅ <b>Whitelist</b>\n"]
    for e in entries[:20]:
        lines.append(f"• TG:{e['telegram_id']} [{e['type']}]  <i>{e.get('reason','')[:30]}</i>")
    if not entries:
        lines.append("<i>No whitelisted users.</i>")
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=_back_kb("aasm:menu"),
                                  parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_wl_list: %s", e)


# ─── Settings ─────────────────────────────────────────────────────────────────

async def aasm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    status      = cfg.get("antispam_status", "enabled")
    auto_mute   = cfg.get_bool("antispam_auto_mute", True)
    auto_ban    = cfg.get_bool("antispam_auto_ban", False)
    max_warn    = cfg.get_int("antispam_max_warnings", 3)
    max_cmds    = cfg.get_int("antispam_max_cmds_per_min", 10)
    max_clicks  = cfg.get_int("antispam_max_clicks_per_min", 20)
    max_msgs    = cfg.get_int("antispam_max_msgs_per_min", 15)
    flood_win   = cfg.get_int("antispam_flood_window_secs", 10)
    flood_thr   = cfg.get_int("antispam_flood_threshold", 8)
    mute_secs   = cfg.get_int("antispam_mute_secs", 300)
    cooldown_s  = cfg.get_int("antispam_cooldown_secs", 60)

    on, off = "✅", "☐"
    lines = [
        "⚙️ <b>Anti-Spam Settings</b>\n",
        f"Status: <b>{status.title()}</b>",
        f"Max commands/min: <b>{max_cmds}</b>",
        f"Max clicks/min:   <b>{max_clicks}</b>",
        f"Max messages/min: <b>{max_msgs}</b>",
        f"Flood window:     <b>{flood_win}s</b>  threshold: <b>{flood_thr}</b>",
        f"Cooldown:         <b>{cooldown_s}s</b>",
        f"Max warnings:     <b>{max_warn}</b>",
        f"Mute duration:    <b>{mute_secs//60}min</b>",
        f"{on if auto_mute else off} Auto-mute on max warnings",
        f"{on if auto_ban else off} Auto-ban on repeated mutes",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Enable",       callback_data="aasm:set:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance",  callback_data="aasm:set:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",      callback_data="aasm:set:status:disabled")],
        [InlineKeyboardButton(f"{'✅' if auto_mute else '☐'} Auto-mute",
                               callback_data="aasm:set:automute"),
         InlineKeyboardButton(f"{'✅' if auto_ban else '☐'} Auto-ban",
                               callback_data="aasm:set:autoban")],
        [InlineKeyboardButton("🔙 Back", callback_data="aasm:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aasm_settings: %s", e)


async def aasm_settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts  = (q.data or "").split(":")
    action = parts[2] if len(parts) >= 3 else ""
    val    = parts[3] if len(parts) >= 4 else ""

    try:
        from database.models import BotConfig
        with get_db_session() as s:
            if action == "status" and val:
                row = s.query(BotConfig).filter_by(key="antispam_status").first()
                if row:
                    row.value = val
                s.commit()
                await q.answer(f"✅ Status set to {val}.")
            elif action == "automute":
                row = s.query(BotConfig).filter_by(key="antispam_auto_mute").first()
                if row:
                    row.value = "false" if str(row.value).lower() in ("true", "1") else "true"
                    s.commit()
                await q.answer("✅ Auto-mute toggled.")
            elif action == "autoban":
                row = s.query(BotConfig).filter_by(key="antispam_auto_ban").first()
                if row:
                    row.value = "false" if str(row.value).lower() in ("true", "1") else "true"
                    s.commit()
                await q.answer("✅ Auto-ban toggled.")
            else:
                await q.answer("❓ Unknown setting.")
        log_admin_action(update.effective_user.id, f"antispam_settings.{action}", details=val)
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
        return

    await aasm_settings(update, context)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def aasm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all aasm:* callbacks not handled by conversations."""
    q = update.callback_query
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await aasm_menu(update, context)
    elif action == "violations":
        await aasm_violations(update, context)
    elif action == "user":
        await aasm_user_detail(update, context)
    elif action == "mute":
        await aasm_mute(update, context)
    elif action == "unmute":
        await aasm_unmute(update, context)
    elif action == "ban":
        await aasm_ban(update, context)
    elif action == "unban":
        await aasm_unban(update, context)
    elif action == "wl":
        await aasm_whitelist(update, context)
    elif action == "bluser":
        await aasm_bl_user(update, context)
    elif action == "clrwarn":
        await aasm_clear_warnings(update, context)
    elif action == "bl":
        sub = parts[2] if len(parts) >= 3 else ""
        if sub == "users":
            await aasm_bl_users(update, context)
        else:
            await aasm_bl_menu(update, context)
    elif action == "wllist":
        await aasm_wl_list(update, context)
    elif action == "settings":
        await aasm_settings(update, context)
    elif action == "set":
        await aasm_settings_action(update, context)
    else:
        await q.answer()
        await aasm_menu(update, context)


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all anti-spam admin handlers."""
    app.add_handler(build_search_conv())
    app.add_handler(build_addword_conv())
    app.add_handler(CallbackQueryHandler(aasm_dispatch, pattern=r"^aasm:.+$"))
