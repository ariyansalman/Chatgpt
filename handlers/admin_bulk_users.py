"""Bulk User Management admin handler — V35.

Callback namespace: bum:*

Features:
  • User search: by Telegram ID, username, name, wallet balance, registration date, status
  • User filters: active, inactive, banned, verified, with/without orders, VIP
  • Bulk actions: ban, unban, verify, unverify, add/remove/reset balance,
    reset referral, reset coupons, reset wallet, delete inactive, broadcast
  • User export: CSV, Excel, JSON (filtered or all)
  • Import/Export history and statistics
  • Feature status management: 🟢 enabled / 🟡 maintenance / 🔴 disabled
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────
BUM_WAIT_SEARCH_VALUE  = 0
BUM_WAIT_AMOUNT        = 1
BUM_WAIT_BROADCAST_MSG = 2

# ── Status helpers ────────────────────────────────────────────────────────
_STATUS_EMOJI = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}

def _mgr_status() -> str:
    return cfg.get("bulk_user_manager_status", "enabled")

def _is_enabled() -> bool:
    return _mgr_status() == "enabled"

def _is_active() -> bool:
    return _mgr_status() in ("enabled", "maintenance")

def _guard(uid: int) -> bool:
    return has_permission(uid, "manage_users")


# ── Safe edit helper ──────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_main() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data="bum:menu")


def _back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_back_main()]])


# ── User filter labels ─────────────────────────────────────────────────────

FILTER_LABELS = {
    "all":           "👥 All Users",
    "active":        "🟢 Active Users",
    "inactive":      "🔴 Inactive Users",
    "banned":        "⛔ Banned Users",
    "verified":      "✅ Verified Users",
    "with_orders":   "🛍 With Orders",
    "without_orders":"🚫 Without Orders",
    "vip":           "👑 VIP Users",
}


# ═════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ═════════════════════════════════════════════════════════════════════════

async def bum_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    if query:
        await query.answer()

    if not _guard(uid):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    mgr_status = _mgr_status()
    status_emoji = _STATUS_EMOJI.get(mgr_status, "⚪")

    from services.bulk_user_service import get_user_bulk_stats
    stats = get_user_bulk_stats()

    from services import payment_ui as pui
    text = (
        f"👥 <b>Bulk User Manager</b>\n"
        f"{pui.DIVIDER}\n"
        f"Status: {status_emoji} {mgr_status.title()}\n\n"
        f"📊 <b>Statistics:</b>\n"
        f"  👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"  📤 Total Exports: <b>{stats['total_exports']}</b>\n"
        f"  ⚡ Bulk Actions: <b>{stats['bulk_user_actions']}</b>\n"
        f"  👤 Total Managed: <b>{stats['total_managed_users']}</b>\n\n"
        f"Choose an action:"
    )

    kb = [
        [InlineKeyboardButton("🔍 Search Users",    callback_data="bum:search:menu"),
         InlineKeyboardButton("🔽 Filter Users",    callback_data="bum:filter:menu")],
        [InlineKeyboardButton("⚡ Bulk Actions",    callback_data="bum:bulk:menu"),
         InlineKeyboardButton("📤 Export Users",    callback_data="bum:export:menu")],
        [InlineKeyboardButton("📋 Export History",  callback_data="bum:history:export:0"),
         InlineKeyboardButton("📋 Action History",  callback_data="bum:history:actions:0")],
        [InlineKeyboardButton(f"{status_emoji} Manager Settings", callback_data="bum:settings")],
        [InlineKeyboardButton("🔙 Admin Panel",    callback_data="acc:root")],
    ]

    if query:
        await _safe_edit(query, text, InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════════════
# SEARCH
# ═════════════════════════════════════════════════════════════════════════

async def bum_search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = (
        "🔍 <b>SEARCH USERS</b>\n\n"
        "Select search field:"
    )
    kb = [
        [InlineKeyboardButton("🆔 Telegram ID",      callback_data="bum:search:by:telegram_id"),
         InlineKeyboardButton("👤 Username",          callback_data="bum:search:by:username")],
        [InlineKeyboardButton("💰 Min Balance",       callback_data="bum:search:by:min_balance")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_search_by(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for search value."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    search_by = parts[3] if len(parts) > 3 else "telegram_id"
    context.user_data["bum_search_by"] = search_by

    field_labels = {
        "telegram_id": "Telegram ID (numeric)",
        "username": "username (partial match)",
        "min_balance": "minimum wallet balance ($)",
    }
    await _safe_edit(
        query,
        f"🔍 <b>SEARCH BY {search_by.upper()}</b>\n\n"
        f"Enter {field_labels.get(search_by, search_by)}:\n\n"
        f"Send /cancel to abort.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bum:search:cancel")]]),
    )
    return BUM_WAIT_SEARCH_VALUE


async def bum_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _guard(uid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    search_by = context.user_data.get("bum_search_by", "telegram_id")
    search_value = update.message.text.strip()
    context.user_data["bum_last_search_by"] = search_by
    context.user_data["bum_last_search_value"] = search_value

    from services.bulk_user_service import search_users
    results, total = search_users(search_by, search_value, limit=20)

    if not results:
        await update.message.reply_text(
            f"🔍 No users found for {search_by}=<code>{search_value}</code>",
            reply_markup=_back_main_kb(), parse_mode="HTML",
        )
        return ConversationHandler.END

    text = f"🔍 <b>SEARCH RESULTS</b> — {total} found\n\n"
    for u in results[:10]:
        ban_icon = "⛔" if u["is_banned"] else "✅"
        text += (
            f"{ban_icon} <b>ID {u['telegram_id']}</b> @{u['username']}\n"
            f"   💰 ${u['wallet_balance']:.2f} | 🛍 {u['has_purchased']} | "
            f"📅 {u['created_at']}\n\n"
        )
    if total > 10:
        text += f"… and {total - 10} more"

    await update.message.reply_text(text, reply_markup=_back_main_kb(), parse_mode="HTML")
    return ConversationHandler.END


async def bum_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await _safe_edit(query, "🔍 Search cancelled.", _back_main_kb())
    return ConversationHandler.END


# ═════════════════════════════════════════════════════════════════════════
# FILTER USERS
# ═════════════════════════════════════════════════════════════════════════

async def bum_filter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return
    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = "🔽 <b>FILTER USERS</b>\n\nSelect a filter to view users:"
    kb = []
    for ftype, label in FILTER_LABELS.items():
        kb.append([InlineKeyboardButton(label, callback_data=f"bum:filter:show:{ftype}")])
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_filter_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    filter_type = parts[3] if len(parts) > 3 else "all"
    context.user_data["bum_filter_type"] = filter_type

    from services.bulk_user_service import search_users
    results, total = search_users("", "", filter_type=filter_type, limit=10)

    label = FILTER_LABELS.get(filter_type, filter_type)
    text = f"🔽 <b>{label}</b> — {total} users\n\n"
    for u in results:
        ban_icon = "⛔" if u["is_banned"] else ("✅" if u["has_purchased"] else "👤")
        text += (
            f"{ban_icon} <b>ID {u['telegram_id']}</b> @{u['username']}\n"
            f"   💰 ${u['wallet_balance']:.2f} | 📅 {u['created_at']}\n\n"
        )
    if total > 10:
        text += f"… and {total - 10} more"

    kb = [
        [InlineKeyboardButton("⚡ Bulk Action on this filter", callback_data=f"bum:bulk:filter:{filter_type}"),
         InlineKeyboardButton("📤 Export this filter",         callback_data=f"bum:export:filter:{filter_type}")],
        [InlineKeyboardButton("🔽 Back to Filters", callback_data="bum:filter:menu")],
        [_back_main()],
    ]
    await _safe_edit(query, text if text.strip() else "No users found.", InlineKeyboardMarkup(kb))


# ═════════════════════════════════════════════════════════════════════════
# BULK ACTIONS
# ═════════════════════════════════════════════════════════════════════════

async def bum_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    if not _is_enabled():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = "⚡ <b>BULK USER ACTIONS</b>\n\nSelect an action:"
    kb = [
        [InlineKeyboardButton("⛔ Bulk Ban",            callback_data="bum:bulk:action:ban"),
         InlineKeyboardButton("✅ Bulk Unban",           callback_data="bum:bulk:action:unban")],
        [InlineKeyboardButton("✔️ Bulk Verify",          callback_data="bum:bulk:action:verify"),
         InlineKeyboardButton("✖️ Bulk Unverify",        callback_data="bum:bulk:action:unverify")],
        [InlineKeyboardButton("💰 Bulk Add Balance",     callback_data="bum:bulk:action:add_balance"),
         InlineKeyboardButton("💸 Bulk Remove Balance",  callback_data="bum:bulk:action:remove_balance")],
        [InlineKeyboardButton("🔄 Bulk Reset Wallet",    callback_data="bum:bulk:action:reset_wallet"),
         InlineKeyboardButton("🔄 Bulk Reset Referral",  callback_data="bum:bulk:action:reset_referral")],
        [InlineKeyboardButton("🎟 Bulk Reset Coupons",   callback_data="bum:bulk:action:reset_coupons")],
        [InlineKeyboardButton("📢 Bulk Broadcast",       callback_data="bum:bulk:action:broadcast"),
         InlineKeyboardButton("🗑 Delete Inactive",      callback_data="bum:bulk:action:delete_inactive")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_bulk_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for a specific bulk action — choose filter scope."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    action = parts[3] if len(parts) > 3 else "ban"
    context.user_data["bum_action"] = action

    # Special actions that need a different flow
    if action == "delete_inactive":
        need_confirm = cfg.get_bool("bulk_user_delete_confirm", True)
        if need_confirm:
            kb = [
                [InlineKeyboardButton("✅ Yes, delete inactive users", callback_data="bum:bulk:exec:delete_inactive:all"),
                 InlineKeyboardButton("❌ Cancel", callback_data="bum:menu")],
            ]
            await _safe_edit(
                query,
                "⚠️ <b>DELETE INACTIVE USERS</b>\n\n"
                "This will delete users who:\n"
                "  • Have not been seen in 90+ days\n"
                "  • Have never ordered\n"
                "  • Have zero balance\n\n"
                "Are you sure?",
                InlineKeyboardMarkup(kb),
            )
            return

    if action == "broadcast":
        await _safe_edit(
            query,
            "📢 <b>BULK BROADCAST</b>\n\n"
            "Select which users to broadcast to:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"bum:bulk:broadcast:filter:{ftype}")]
                for ftype, label in list(FILTER_LABELS.items())[:6]
            ] + [[_back_main()]]),
        )
        return

    if action in ("add_balance", "remove_balance"):
        await _safe_edit(
            query,
            f"💰 <b>BULK {'ADD' if action == 'add_balance' else 'REMOVE'} BALANCE</b>\n\n"
            "First, select which users to apply to:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"bum:bulk:amount:filter:{ftype}:{action}")]
                for ftype, label in list(FILTER_LABELS.items())[:6]
            ] + [[_back_main()]]),
        )
        return

    # For other actions: pick a filter scope
    action_labels = {
        "ban": "⛔ Ban",
        "unban": "✅ Unban",
        "verify": "✔️ Verify",
        "unverify": "✖️ Unverify",
        "reset_wallet": "🔄 Reset Wallet",
        "reset_referral": "🔄 Reset Referral",
        "reset_coupons": "🎟 Reset Coupons",
    }
    text = (
        f"⚡ <b>BULK {action_labels.get(action, action).upper()}</b>\n\n"
        "Select which users to apply this action to:"
    )
    kb = [
        [InlineKeyboardButton(label, callback_data=f"bum:bulk:exec:{action}:{ftype}")]
        for ftype, label in list(FILTER_LABELS.items())[:6]
    ]
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_bulk_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a bulk user action."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid): return
    if not _is_enabled():
        await query.answer("⛔ Manager is disabled.", show_alert=True); return

    parts = (query.data or "").split(":")
    # bum:bulk:exec:<action>:<filter>
    action = parts[3] if len(parts) > 3 else "ban"
    filter_type = parts[4] if len(parts) > 4 else "all"

    await _safe_edit(query, f"⏳ Running bulk {action} on {filter_type} users…")

    from services.bulk_user_service import (
        get_filtered_user_ids,
        bulk_ban_users, bulk_unban_users,
        bulk_verify_users, bulk_unverify_users,
        bulk_reset_wallet, bulk_reset_referral, bulk_reset_coupons,
        bulk_delete_inactive_users,
    )

    try:
        if action == "delete_inactive":
            result = bulk_delete_inactive_users(uid)
        else:
            user_ids = get_filtered_user_ids(filter_type)
            if not user_ids:
                await query.message.reply_text(
                    "⚠️ No users matched the filter.", reply_markup=_back_main_kb()
                )
                return

            dispatch = {
                "ban": bulk_ban_users,
                "unban": bulk_unban_users,
                "verify": bulk_verify_users,
                "unverify": bulk_unverify_users,
                "reset_wallet": bulk_reset_wallet,
                "reset_referral": bulk_reset_referral,
                "reset_coupons": bulk_reset_coupons,
            }
            fn = dispatch.get(action)
            if fn is None:
                await query.message.reply_text(f"❌ Unknown action: {action}")
                return
            result = fn(uid, user_ids)
    except Exception as e:
        await query.message.reply_text(f"❌ Bulk action failed: {e}")
        return

    log_admin_action(uid, f"bulk_user_{action}", target_type="bulk_user_action",
                     details=f"filter={filter_type} success={result.get('success', 0)} failed={result.get('failed', 0)}",
                     module="bulk_users")

    text = (
        f"✅ <b>BULK {action.upper()} COMPLETE</b>\n\n"
        f"✅ Success: <b>{result.get('success', 0)}</b>\n"
        f"❌ Failed:  <b>{result.get('failed', 0)}</b>"
    )
    if "skipped" in result:
        text += f"\n⏭ Skipped: <b>{result['skipped']}</b>"
    await query.message.reply_text(text, reply_markup=_back_main_kb(), parse_mode="HTML")


async def bum_bulk_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bulk action triggered from a filter view."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    # bum:bulk:filter:<filter_type>
    filter_type = parts[3] if len(parts) > 3 else "all"
    context.user_data["bum_filter_type"] = filter_type

    # Show action selection
    text = f"⚡ <b>BULK ACTION on {FILTER_LABELS.get(filter_type, filter_type)}</b>\n\nSelect action:"
    kb = [
        [InlineKeyboardButton("⛔ Ban",         callback_data=f"bum:bulk:exec:ban:{filter_type}"),
         InlineKeyboardButton("✅ Unban",        callback_data=f"bum:bulk:exec:unban:{filter_type}")],
        [InlineKeyboardButton("🔄 Reset Wallet", callback_data=f"bum:bulk:exec:reset_wallet:{filter_type}")],
        [InlineKeyboardButton("📤 Export",       callback_data=f"bum:export:filter:{filter_type}")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Broadcast (separate flow) ────────────────────────────────────────────

async def bum_broadcast_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose broadcast target filter, then await message text."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    # bum:bulk:broadcast:filter:<filter_type>
    filter_type = parts[4] if len(parts) > 4 else "all"
    context.user_data["bum_broadcast_filter"] = filter_type

    limit = cfg.get_int("bulk_user_broadcast_limit", 500)
    await _safe_edit(
        query,
        f"📢 <b>BROADCAST to {FILTER_LABELS.get(filter_type, filter_type)}</b>\n\n"
        f"Max recipients: {limit}\n\n"
        f"Type your broadcast message and send it:\n\nSend /cancel to abort.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bum:broadcast:cancel")]]),
    )
    return BUM_WAIT_BROADCAST_MSG


async def bum_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send broadcast message to selected user group."""
    uid = update.effective_user.id
    if not _guard(uid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    filter_type = context.user_data.get("bum_broadcast_filter", "all")
    message_text = update.message.text.strip()
    limit = cfg.get_int("bulk_user_broadcast_limit", 500)

    from services.bulk_user_service import get_filtered_user_ids
    user_ids = get_filtered_user_ids(filter_type)[:limit]

    if not user_ids:
        await update.message.reply_text("⚠️ No users matched the filter.", reply_markup=_back_main_kb())
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Sending broadcast to {len(user_ids)} users…")

    from database.models import User
    sent = 0
    failed = 0
    with get_db_session() as s:
        for uid_row in user_ids:
            user = s.query(User).filter_by(id=uid_row).first()
            if not user:
                continue
            try:
                await update.get_bot().send_message(
                    chat_id=user.telegram_id,
                    text=f"📢 <b>Message from Admin:</b>\n\n{message_text}",
                    parse_mode="HTML",
                )
                sent += 1
            except Exception:
                failed += 1

    log_admin_action(uid, "bulk_user_broadcast", target_type="broadcast",
                     details=f"filter={filter_type} sent={sent} failed={failed}",
                     module="bulk_users")

    await update.message.reply_text(
        f"📢 <b>BROADCAST COMPLETE</b>\n\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        reply_markup=_back_main_kb(), parse_mode="HTML",
    )
    return ConversationHandler.END


async def bum_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await _safe_edit(query, "📢 Broadcast cancelled.", _back_main_kb())
    return ConversationHandler.END


# ── Balance flow ──────────────────────────────────────────────────────────

async def bum_amount_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for amount to add/remove after filter was chosen."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    # bum:bulk:amount:filter:<filter_type>:<action>
    filter_type = parts[4] if len(parts) > 4 else "all"
    action = parts[5] if len(parts) > 5 else "add_balance"
    context.user_data["bum_amount_filter"] = filter_type
    context.user_data["bum_amount_action"] = action

    verb = "add to" if action == "add_balance" else "remove from"
    await _safe_edit(
        query,
        f"💰 <b>{'ADD' if action == 'add_balance' else 'REMOVE'} BALANCE</b>\n\n"
        f"Filter: {FILTER_LABELS.get(filter_type, filter_type)}\n\n"
        f"Enter the amount to {verb} each user's wallet (e.g. <code>5.00</code>):\n\nSend /cancel to abort.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bum:broadcast:cancel")]]),
    )
    return BUM_WAIT_AMOUNT


async def bum_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _guard(uid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    filter_type = context.user_data.get("bum_amount_filter", "all")
    action = context.user_data.get("bum_amount_action", "add_balance")
    text = update.message.text.strip()

    try:
        amount = float(text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Enter a positive number.")
        return BUM_WAIT_AMOUNT

    from services.bulk_user_service import (
        get_filtered_user_ids, bulk_add_balance, bulk_remove_balance
    )
    user_ids = get_filtered_user_ids(filter_type)
    if not user_ids:
        await update.message.reply_text("⚠️ No users matched.", reply_markup=_back_main_kb())
        return ConversationHandler.END

    fn = bulk_add_balance if action == "add_balance" else bulk_remove_balance
    result = fn(uid, user_ids, amount)

    log_admin_action(uid, f"bulk_user_{action}", target_type="bulk_user_action",
                     details=f"filter={filter_type} amount={amount} success={result['success']}",
                     module="bulk_users")

    verb = "added to" if action == "add_balance" else "removed from"
    await update.message.reply_text(
        f"✅ ${amount:.2f} {verb} {result['success']} users\n"
        f"❌ Failed: {result['failed']}",
        reply_markup=_back_main_kb(), parse_mode="HTML",
    )
    return ConversationHandler.END


# ═════════════════════════════════════════════════════════════════════════
# EXPORT
# ═════════════════════════════════════════════════════════════════════════

async def bum_export_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return
    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    text = "📤 <b>EXPORT USERS</b>\n\nSelect scope:"
    kb = [
        [InlineKeyboardButton(label, callback_data=f"bum:export:filter:{ftype}")]
        for ftype, label in FILTER_LABELS.items()
    ]
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_export_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Select format after choosing filter."""
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    filter_type = parts[3] if len(parts) > 3 else "all"
    context.user_data["bum_export_filter"] = filter_type

    text = (
        f"📤 <b>EXPORT — {FILTER_LABELS.get(filter_type, filter_type)}</b>\n\n"
        f"Select export format:"
    )
    kb = [
        [InlineKeyboardButton("📄 CSV",   callback_data="bum:export:do:csv"),
         InlineKeyboardButton("📊 Excel", callback_data="bum:export:do:xlsx"),
         InlineKeyboardButton("📋 JSON",  callback_data="bum:export:do:json")],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_export_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the export and send file."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid): return
    if not _is_active():
        await _safe_edit(query, f"⛔ Manager is {_mgr_status()}.", _back_main_kb()); return

    parts = (query.data or "").split(":")
    fmt = parts[3] if len(parts) > 3 else "csv"
    filter_type = context.user_data.get("bum_export_filter", "all")

    await _safe_edit(query, "⏳ Generating user export…")

    try:
        from services.bulk_user_service import export_users
        data, row_count = export_users(fmt, filter_type, admin_id=uid)
    except Exception as e:
        await query.message.reply_text(f"❌ Export failed: {e}")
        return

    ext_map = {"csv": "csv", "xlsx": "xlsx", "json": "json"}
    ext = ext_map.get(fmt, fmt)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"users_{filter_type}_{ts}.{ext}"

    file_obj = InputFile(io.BytesIO(data), filename=filename)
    await query.message.reply_document(
        document=file_obj,
        caption=f"📤 User export — {row_count} users\nFilter: {filter_type} | Format: {fmt.upper()}",
    )

    log_admin_action(uid, "bulk_user_export", target_type="export",
                     details=f"fmt={fmt} filter={filter_type} rows={row_count}",
                     module="bulk_users")

    await query.message.reply_text("✅ Export complete.", reply_markup=_back_main_kb())


# ═════════════════════════════════════════════════════════════════════════
# HISTORY
# ═════════════════════════════════════════════════════════════════════════

async def bum_history_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    page = int(parts[4]) if len(parts) > 4 else 0
    per_page = 5

    from database.models import BulkExportRecord
    with get_db_session() as s:
        total = s.query(BulkExportRecord).filter_by(export_type="users").count()
        records = (
            s.query(BulkExportRecord)
            .filter_by(export_type="users")
            .order_by(BulkExportRecord.started_at.desc())
            .limit(per_page).offset(page * per_page)
            .all()
        )
        rows = [
            (r.id, r.file_format, r.scope, r.row_count,
             r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "—")
            for r in records
        ]

    text = "📋 <b>USER EXPORT HISTORY</b>\n\n"
    text += "No records yet." if not rows else ""
    for rid, fmt, scope, count, ts in rows:
        text += f"📤 <b>#{rid}</b> [{fmt.upper()}] filter={scope} — {ts}\n   {count} rows\n\n"

    kb, nav = [], []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bum:history:export:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bum:history:export:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_history_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _guard(update.effective_user.id): return

    parts = (query.data or "").split(":")
    page = int(parts[4]) if len(parts) > 4 else 0
    per_page = 5

    from database.models import BulkActionRecord
    with get_db_session() as s:
        total = s.query(BulkActionRecord).filter_by(entity_type="user").count()
        records = (
            s.query(BulkActionRecord)
            .filter_by(entity_type="user")
            .order_by(BulkActionRecord.created_at.desc())
            .limit(per_page).offset(page * per_page)
            .all()
        )
        rows = [
            (r.id, r.action_type, r.scope, r.success_count, r.failed_count,
             r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—")
            for r in records
        ]

    text = "📋 <b>BULK USER ACTION HISTORY</b>\n\n"
    text += "No records yet." if not rows else ""
    for rid, atype, scope, ok, fail, ts in rows:
        text += f"⚡ <b>#{rid}</b> {atype} scope={scope or '—'} — {ts}\n   ✅{ok} ❌{fail}\n\n"

    kb, nav = [], []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bum:history:actions:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bum:history:actions:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_main()])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ═════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═════════════════════════════════════════════════════════════════════════

async def bum_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    status = _mgr_status()
    export_limit = cfg.get_int("bulk_user_export_max_rows", 10000)
    del_confirm = cfg.get_bool("bulk_user_delete_confirm", True)
    bcast_limit = cfg.get_int("bulk_user_broadcast_limit", 500)
    action_log = cfg.get_bool("bulk_user_action_log_enabled", True)

    text = (
        f"⚙️ <b>BULK USER MANAGER — SETTINGS</b>\n\n"
        f"Status: {_STATUS_EMOJI.get(status, '⚪')} {status.title()}\n"
        f"Max Export Rows: <b>{export_limit}</b>\n"
        f"Delete Confirmation: {'✅' if del_confirm else '⚪'}\n"
        f"Broadcast Limit: <b>{bcast_limit}</b>\n"
        f"Action Logging: {'✅' if action_log else '⚪'}\n"
    )
    kb = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="bum:set:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="bum:set:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="bum:set:status:disabled")],
        [InlineKeyboardButton(
            "✅ Del. Confirm: ON" if del_confirm else "⚪ Del. Confirm: OFF",
            callback_data="bum:set:toggle:bulk_user_delete_confirm",
        )],
        [InlineKeyboardButton(
            "✅ Action Log: ON" if action_log else "⚪ Action Log: OFF",
            callback_data="bum:set:toggle:bulk_user_action_log_enabled",
        )],
        [_back_main()],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def bum_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    parts = (query.data or "").split(":")
    new_status = parts[3] if len(parts) > 3 else "enabled"
    cfg.set("bulk_user_manager_status", new_status)
    log_admin_action(uid, "bulk_user_manager_status_change", target_type="config",
                     new_value=new_status, module="bulk_users")
    await bum_settings(update, context)


async def bum_set_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not has_permission(uid, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True); return

    parts = (query.data or "").split(":")
    key = parts[3] if len(parts) > 3 else ""
    if key:
        current = cfg.get_bool(key, True)
        cfg.set(key, "false" if current else "true")
        log_admin_action(uid, "bulk_user_config_toggle", target_type="config",
                         target_id=key, new_value=str(not current), module="bulk_users")
    await bum_settings(update, context)


# ═════════════════════════════════════════════════════════════════════════
# CONVERSATIONHANDLER BUILDERS
# ═════════════════════════════════════════════════════════════════════════

def build_bum_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bum_search_by, pattern=r"^bum:search:by:"),
        ],
        states={
            BUM_WAIT_SEARCH_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bum_search_receive),
                CallbackQueryHandler(bum_search_cancel, pattern=r"^bum:search:cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bum_search_cancel, pattern=r"^bum:search:cancel$"),
        ],
        per_chat=True, per_user=True,
        name="bum_search_conv", persistent=False,
    )


def build_bum_broadcast_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bum_broadcast_filter, pattern=r"^bum:bulk:broadcast:filter:"),
        ],
        states={
            BUM_WAIT_BROADCAST_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bum_broadcast_send),
                CallbackQueryHandler(bum_broadcast_cancel, pattern=r"^bum:broadcast:cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bum_broadcast_cancel, pattern=r"^bum:broadcast:cancel$"),
        ],
        per_chat=True, per_user=True,
        name="bum_broadcast_conv", persistent=False,
    )


def build_bum_amount_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bum_amount_filter, pattern=r"^bum:bulk:amount:filter:"),
        ],
        states={
            BUM_WAIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bum_amount_receive),
                CallbackQueryHandler(bum_broadcast_cancel, pattern=r"^bum:broadcast:cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bum_broadcast_cancel, pattern=r"^bum:broadcast:cancel$"),
        ],
        per_chat=True, per_user=True,
        name="bum_amount_conv", persistent=False,
    )


def register_handlers(application) -> None:
    """Register all bum:* handlers on the Application."""
    from telegram.ext import CallbackQueryHandler as CQH

    # ConversationHandlers (must come first)
    application.add_handler(build_bum_search_conv())
    application.add_handler(build_bum_broadcast_conv())
    application.add_handler(build_bum_amount_conv())

    # Plain callback handlers
    application.add_handler(CQH(bum_menu,              pattern=r"^bum:menu$"))
    application.add_handler(CQH(bum_search_menu,       pattern=r"^bum:search:menu$"))
    application.add_handler(CQH(bum_filter_menu,       pattern=r"^bum:filter:menu$"))
    application.add_handler(CQH(bum_filter_show,       pattern=r"^bum:filter:show:"))
    application.add_handler(CQH(bum_bulk_menu,         pattern=r"^bum:bulk:menu$"))
    application.add_handler(CQH(bum_bulk_action,       pattern=r"^bum:bulk:action:"))
    application.add_handler(CQH(bum_bulk_exec,         pattern=r"^bum:bulk:exec:"))
    application.add_handler(CQH(bum_bulk_filter,       pattern=r"^bum:bulk:filter:"))
    application.add_handler(CQH(bum_export_menu,       pattern=r"^bum:export:menu$"))
    application.add_handler(CQH(bum_export_filter,     pattern=r"^bum:export:filter:"))
    application.add_handler(CQH(bum_export_do,         pattern=r"^bum:export:do:"))
    application.add_handler(CQH(bum_history_export,    pattern=r"^bum:history:export:"))
    application.add_handler(CQH(bum_history_actions,   pattern=r"^bum:history:actions:"))
    application.add_handler(CQH(bum_settings,          pattern=r"^bum:settings$"))
    application.add_handler(CQH(bum_set_status,        pattern=r"^bum:set:status:"))
    application.add_handler(CQH(bum_set_toggle,        pattern=r"^bum:set:toggle:"))
