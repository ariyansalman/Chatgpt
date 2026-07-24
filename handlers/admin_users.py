"""Admin User Management panel.

Sections:
  1. Users Menu  — 📋 Users List | 🔍 User Search | 📝 Manual Payments | ↩️ Return
  2. Users List  — DB-paginated (LIMIT/OFFSET), sort toggle (Latest/Oldest)
  3. User Search — ConversationHandler: Telegram ID or @username (case-insensitive)
  4. User Info   — full detail: Balance / Ban / Purchase History / Position
  5. Change Bal  — Set / Add / Deduct + confirmation + WalletLedger + idempotency
  6. Ban / Unban — explicit confirmation before mutation + clear_ban_cache
  7. Purchase History — paginated real Order records → existing order detail
  8. Position    — current role display (settings-controlled)
"""

from __future__ import annotations

import html
import logging
import uuid
from decimal import Decimal, InvalidOperation

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    filters,
)

from database import get_db_session, User, Order, OrderItem, Product
from database.models import OrderStatus, WalletLedger
from utils.helpers import format_price, clear_ban_cache
from utils.audit import log_admin_action
from utils.permissions import has_permission
from config.settings import settings as app_settings
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
WAITING_USR_SEARCH = 10   # user search text
WAITING_BAL_AMOUNT = 11   # balance amount text

# Pagination
_PG_USERS  = 8
_PG_ORDERS = 8


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _name(user) -> str:
    """@username or 'User {tg_id}' — never the literal string None."""
    return f"@{user.username}" if user.username else f"User {user.telegram_id}"


def _name_esc(user) -> str:
    """HTML-escaped display name."""
    return html.escape(_name(user))


def _user_info_msg(user) -> str:
    """👤 User Information message block (HTML)."""
    reg = user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "—"
    return (
        "👤 <b>User Information</b>\n\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"👤 Name: {_name_esc(user)}\n"
        f"💰 Balance: <b>{format_price(float(user.wallet_balance or 0.0))}</b>\n"
        f"📅 Reg Date: {reg}"
    )


def _user_info_kb(user) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton("🟢 Unban User", callback_data=f"usr:ubn:{user.id}")
        if user.is_banned
        else InlineKeyboardButton("🔴 Ban User", callback_data=f"usr:ban:{user.id}")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Change Balance", callback_data=f"usr:bal:{user.id}")],
        [ban_btn],
        [InlineKeyboardButton("🧾 User Purchase History", callback_data=f"usr:ord:{user.id}:0")],
        [InlineKeyboardButton("👔 Position", callback_data=f"usr:pos:{user.id}")],
        [InlineKeyboardButton("↩️ Return", callback_data="usr:list:0:desc")],
    ])


def _clr_bal(ctx):
    for k in ("_bal_action", "_bal_user_id", "_bal_amount", "_bal_curr", "_bal_idem"):
        ctx.user_data.pop(k, None)


def _clr_search(ctx):
    ctx.user_data.pop("_search_msg_id", None)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Users Menu
# ─────────────────────────────────────────────────────────────────────────────

async def users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: admin_users — show the Users menu."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Users List",       callback_data="usr:list:0:desc")],
        [InlineKeyboardButton("🔍 User Search",      callback_data="usr:search")],
        [InlineKeyboardButton("🔍 Customer 360° View", callback_data="c360:search")],
        [InlineKeyboardButton("📝 Manual Payments",  callback_data="mp:list:0:desc")],
        [InlineKeyboardButton("↩️ Return",            callback_data="admin_menu")],
    ])
    try:
        try:
            await query.edit_message_text(
                "👥 <b>User Management</b>\n\nChoose from the options below:",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 2. Users List
# ─────────────────────────────────────────────────────────────────────────────

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:list:{page}:{sort} — paginated user list with sort toggle."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        page = int(parts[2])
        sort = parts[3] if len(parts) > 3 else "desc"
    except (IndexError, ValueError):
        page, sort = 0, "desc"
    sort = "asc" if sort == "asc" else "desc"

    from sqlalchemy import func as _f
    with get_db_session() as session:
        total = session.query(_f.count(User.id)).scalar() or 0
        col   = User.created_at.asc() if sort == "asc" else User.created_at.desc()
        rows  = (
            session.query(User.id, User.username, User.telegram_id)
            .order_by(col)
            .offset(page * _PG_USERS)
            .limit(_PG_USERS)
            .all()
        )

    total_pages = max(1, (total + _PG_USERS - 1) // _PG_USERS)
    next_sort   = "asc" if sort == "desc" else "desc"
    sort_lbl    = "🕒 Latest" if sort == "desc" else "🕰 Oldest"

    kb = []
    for uid, username, tg_id in rows:
        lbl = f"@{username} | {tg_id}" if username else f"User {tg_id} | {tg_id}"
        kb.append([InlineKeyboardButton(lbl[:64], callback_data=f"usr:det:{uid}")])

    pag = []
    if page > 0:
        pag.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"usr:list:{page-1}:{sort}"))
    if total_pages > 1:
        pag.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        pag.append(InlineKeyboardButton("➡️ Next", callback_data=f"usr:list:{page+1}:{sort}"))
    if pag:
        kb.append(pag)

    kb += [
        [InlineKeyboardButton(f"Sort: {sort_lbl}", callback_data=f"usr:list:{page}:{next_sort}")],
        [InlineKeyboardButton("↩️ Return",          callback_data="admin_users")],
    ]
    try:
        try:
            await query.edit_message_text(
                f"📋 <b>Users List</b> (Total: {total})",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. User Search — ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

async def user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: usr:search — prompt for ID or @username."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    _clr_search(context)
    try:
        try:
            msg = await query.edit_message_text(
                "🔍 <b>User Search</b>\n\n"
                "Enter Telegram ID or @username:\n\n"
                "Send /cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        context.user_data["_search_msg_id"] = msg.message_id
        context.user_data["_search_chat_id"] = update.effective_chat.id
    except Exception:
        pass
    return WAITING_USR_SEARCH


async def user_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive search text, look up user, render detail or re-prompt."""
    if not has_permission(update.effective_user.id, "manage_users"):
        _clr_search(context)
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    # Delete the typed message for a cleaner UI
    try:
        await update.message.delete()
    except Exception:
        pass

    user_row = None
    with get_db_session() as session:
        if raw.lstrip("@").lstrip("+").isdigit():
            tg_id = int(raw.lstrip("@").lstrip("+"))
            user_row = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user_row:
            uname = raw.lstrip("@")
            user_row = session.query(User).filter(
                User.username.ilike(uname)
            ).first()

        if not user_row:
            # Re-prompt in the same message
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Return", callback_data="admin_users")]
            ])
            msg_id  = context.user_data.get("_search_msg_id")
            chat_id = context.user_data.get("_search_chat_id", update.effective_chat.id)
            try:
                if msg_id:
                    try:
                        await context.bot.edit_message_text(
                            "❌ User not found.\n\n"
                            "Enter another Telegram ID or @username:\n\n"
                            "Send /cancel to abort.",
                            chat_id=chat_id,
                            message_id=msg_id,
                            reply_markup=kb,
                        )
                    except BadRequest as e:
                        if "Message is not modified" not in str(e):
                            raise
                else:
                    await update.effective_chat.send_message(
                        "❌ User not found. Enter Telegram ID or @username:",
                        reply_markup=kb,
                    )
            except Exception:
                await update.effective_chat.send_message(
                    "❌ User not found. Enter Telegram ID or @username:",
                    reply_markup=kb,
                )
            return WAITING_USR_SEARCH

        # Found — snapshot before session closes
        msg_text = _user_info_msg(user_row)
        kb       = _user_info_kb(user_row)

    msg_id  = context.user_data.pop("_search_msg_id", None)
    chat_id = context.user_data.pop("_search_chat_id", update.effective_chat.id)
    _clr_search(context)
    try:
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    msg_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.effective_chat.send_message(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await update.effective_chat.send_message(msg_text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def user_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel user search via /cancel."""
    _clr_search(context)
    if update.message:
        try:
            await update.message.reply_text("🔍 Search cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def build_user_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(user_search_start, pattern="^usr:search$"),
        ],
        states={
            WAITING_USR_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_search_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", user_search_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. User Detail
# ─────────────────────────────────────────────────────────────────────────────

async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:det:{user_id} — show user info."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text(
                    "❌ User not found.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("↩️ Return", callback_data="usr:list:0:desc")]]
                    ),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        msg_text = _user_info_msg(user)
        kb       = _user_info_kb(user)

    try:
        try:
            await query.edit_message_text(msg_text, reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. Change Balance
# ─────────────────────────────────────────────────────────────────────────────

async def balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:bal:{user_id} — choose Set / Add / Deduct."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Set Balance",    callback_data=f"usr:bal:set:{user_id}")],
        [InlineKeyboardButton("➕ Add Balance",    callback_data=f"usr:bal:add:{user_id}")],
        [InlineKeyboardButton("➖ Deduct Balance", callback_data=f"usr:bal:ded:{user_id}")],
        [InlineKeyboardButton("↩️ Return",         callback_data=f"usr:det:{user_id}")],
    ])
    try:
        try:
            await query.edit_message_text(
                "💰 <b>Change Balance</b>\n\nChoose an action:",
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def balance_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:bal:{set|add|ded}:{user_id} — prompt for amount."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    parts = (query.data or "").split(":")
    try:
        action  = parts[2]   # set | add | ded
        user_id = int(parts[3])
    except (IndexError, ValueError):
        return ConversationHandler.END

    if action not in ("set", "add", "ded"):
        return ConversationHandler.END

    label_map = {
        "set": "💵 Enter the new balance amount:",
        "add": "➕ Enter the amount to add:",
        "ded": "➖ Enter the amount to deduct:",
    }
    _clr_bal(context)
    context.user_data["_bal_action"]  = action
    context.user_data["_bal_user_id"] = user_id

    try:
        try:
            await query.edit_message_text(
                f"<b>{label_map[action]}</b>\n\nSend /cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass
    return WAITING_BAL_AMOUNT


async def balance_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive balance amount and show confirmation screen."""
    if not has_permission(update.effective_user.id, "manage_users"):
        _clr_bal(context)
        return ConversationHandler.END

    action  = context.user_data.get("_bal_action")
    user_id = context.user_data.get("_bal_user_id")
    if not action or not user_id:
        await update.message.reply_text("❌ Session lost. Please start again.")
        _clr_bal(context)
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        await update.message.reply_text(
            "❌ Invalid number. Please enter a valid amount (e.g. 10.00):"
        )
        return WAITING_BAL_AMOUNT

    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0. Try again:")
        return WAITING_BAL_AMOUNT

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            _clr_bal(context)
            return ConversationHandler.END
        curr_bal = Decimal(str(user.wallet_balance or 0.0))
        username = _name(user)

    action_label_map = {"set": "SET", "add": "ADD", "ded": "DEDUCT"}
    action_label = action_label_map[action]

    if action == "set":
        new_bal = amount
    elif action == "add":
        new_bal = curr_bal + amount
    else:  # ded
        new_bal = curr_bal - amount
        if new_bal < 0:
            await update.message.reply_text(
                f"❌ Deduction of {amount:.2f} USD would result in a negative balance "
                f"(current: {curr_bal:.2f} USD).\n\nEnter a smaller amount:"
            )
            return WAITING_BAL_AMOUNT

    # Generate one-time idempotency token
    idem_tok = str(uuid.uuid4())[:16]
    context.user_data["_bal_amount"] = str(amount)
    context.user_data["_bal_curr"]   = str(curr_bal)
    context.user_data["_bal_idem"]   = idem_tok

    confirm = (
        "❓ <b>Confirm Balance Change</b>\n\n"
        f"User: {html.escape(username)}\n"
        f"Current Balance: <b>{curr_bal:.2f} USD</b>\n"
        f"Action: <b>{action_label}</b>\n"
        f"Amount: <b>{amount:.2f} USD</b>\n"
        f"New Balance: <b>{new_bal:.2f} USD</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Confirm",
                                 callback_data=f"usr:bal:cfm:{user_id}:{idem_tok}"),
            InlineKeyboardButton("❌ No, Cancel",
                                 callback_data=f"usr:det:{user_id}"),
        ]
    ])
    try:
        await update.message.reply_text(confirm, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    return ConversationHandler.END


async def balance_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:bal:cfm:{user_id}:{token} — execute balance change."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        user_id  = int(parts[3])
        tok      = parts[4]
    except (IndexError, ValueError):
        return

    stored_tok = (context.user_data.get("_bal_idem") or "")[:16]
    if tok != stored_tok:
        await query.answer("⚠️ Already processed or session expired.", show_alert=True)
        return

    action = context.user_data.get("_bal_action")
    try:
        amount   = Decimal(context.user_data.get("_bal_amount", "0"))
        Decimal(context.user_data.get("_bal_curr",   "0"))
    except InvalidOperation:
        try:
            await query.edit_message_text("❌ Session data corrupted. No changes made.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        _clr_bal(context)
        return

    # Consume token immediately — double-click guard
    context.user_data["_bal_idem"] = None
    admin_tg_id = update.effective_user.id

    prev_bal_f: float = 0.0
    new_bal_f:  float = 0.0

    try:
        with get_db_session() as session:
            user = (
                session.query(User)
                .filter(User.id == user_id)
                .with_for_update()
                .first()
            )
            if not user:
                try:
                    await query.edit_message_text("❌ User not found. No changes made.")
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                _clr_bal(context)
                return

            prev_bal_f = float(user.wallet_balance or 0.0)

            if action == "set":
                delta = float(amount) - prev_bal_f
            elif action == "add":
                delta = float(amount)
            else:  # ded
                delta = -float(amount)

            new_bal_f = prev_bal_f + delta

            if new_bal_f < 0:
                try:
                    await query.edit_message_text(
                        "❌ Balance would go negative. Operation rejected.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("↩️ Return", callback_data=f"usr:det:{user_id}")]]
                        ),
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                _clr_bal(context)
                return

            if delta != 0:
                user.wallet_balance = new_bal_f
                session.add(WalletLedger(
                    user_id=user.id,
                    delta=float(delta),
                    balance_after=float(new_bal_f),
                    reason=f"admin {action.upper()}",
                    actor_type="admin",
                    actor_id=admin_tg_id,
                    ref_type="admin_adjust",
                    ref_id=str(user_id),
                ))
                session.commit()

        log_admin_action(
            admin_tg_id, f"wallet.{action}",
            target_type="user", target_id=user_id,
            details=f"delta={delta:+.4f} prev={prev_bal_f:.4f} new={new_bal_f:.4f}",
        )
    except Exception as exc:
        logger.error("balance_confirm error user=%s: %s", user_id, exc, exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ Balance update failed ({type(exc).__name__}). No changes made."
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        _clr_bal(context)
        return

    _clr_bal(context)

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if user:
            msg = _user_info_msg(user)
            kb  = _user_info_kb(user)
        else:
            msg = ""
            kb  = InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Return", callback_data="usr:list:0:desc")]]
            )

    try:
        try:
            await query.edit_message_text(
                f"✅ <b>Balance updated successfully.</b>\n\n"
                f"Previous: {prev_bal_f:.2f} USD\n"
                f"New: {new_bal_f:.2f} USD\n\n"
                + msg,
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def balance_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel balance edit via /cancel command."""
    _clr_bal(context)
    if update.message:
        try:
            await update.message.reply_text("💰 Balance edit cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def build_balance_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                balance_action_start,
                pattern=r"^usr:bal:(set|add|ded):\d+$",
            ),
        ],
        states={
            WAITING_BAL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, balance_amount_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", balance_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Ban / Unban
# ─────────────────────────────────────────────────────────────────────────────

async def ban_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:ban:{user_id} — show ban confirmation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        name  = _name_esc(user)
        tg_id = user.telegram_id
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Ban",  callback_data=f"usr:ban:cfm:{user_id}"),
            InlineKeyboardButton("❌ No, Cancel", callback_data=f"usr:det:{user_id}"),
        ]
    ])
    try:
        try:
            await query.edit_message_text(
                f"❓ <b>Are you sure you want to BAN this user?</b>\n\n"
                f"User: {name}\nID: {tg_id}",
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def ban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:ban:cfm:{user_id} — execute ban."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[3])
    except (IndexError, ValueError):
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        tg_id = user.telegram_id
        user.is_banned = True
        session.commit()
        clear_ban_cache(tg_id)
        log_admin_action(
            update.effective_user.id, "user.ban",
            target_type="user", target_id=user_id,
            details=f"telegram_id={tg_id}",
        )
        msg = _user_info_msg(user)
        kb  = _user_info_kb(user)
    try:
        try:
            await query.edit_message_text(
                "✅ <b>User banned successfully.</b>\n\n" + msg,
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def unban_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:ubn:{user_id} — show unban confirmation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        name  = _name_esc(user)
        tg_id = user.telegram_id
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Unban",  callback_data=f"usr:ubn:cfm:{user_id}"),
            InlineKeyboardButton("❌ No, Cancel",   callback_data=f"usr:det:{user_id}"),
        ]
    ])
    try:
        try:
            await query.edit_message_text(
                f"❓ <b>Are you sure you want to UNBAN this user?</b>\n\n"
                f"User: {name}\nID: {tg_id}",
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def unban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:ubn:cfm:{user_id} — execute unban."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[3])
    except (IndexError, ValueError):
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        tg_id = user.telegram_id
        user.is_banned = False
        session.commit()
        clear_ban_cache(tg_id)
        log_admin_action(
            update.effective_user.id, "user.unban",
            target_type="user", target_id=user_id,
            details=f"telegram_id={tg_id}",
        )
        msg = _user_info_msg(user)
        kb  = _user_info_kb(user)
    try:
        try:
            await query.edit_message_text(
                "✅ <b>User unbanned successfully.</b>\n\n" + msg,
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 7. Purchase History
# ─────────────────────────────────────────────────────────────────────────────

async def purchase_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:ord:{user_id}:{page} — paginated orders linking to existing detail."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
        page    = int(parts[3])
    except (IndexError, ValueError):
        return

    from sqlalchemy import func as _f
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        username = _name_esc(user)
        total    = session.query(_f.count(Order.id)).filter_by(user_id=user.id).scalar() or 0
        orders   = (
            session.query(Order)
            .filter_by(user_id=user.id)
            .order_by(Order.created_at.desc())
            .offset(page * _PG_ORDERS)
            .limit(_PG_ORDERS)
            .all()
        )

        _STATUS_ICO = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED:  "✅",
            OrderStatus.CANCELLED:  "❌",
        }

        rows = []
        for o in orders:
            item = session.query(OrderItem).filter_by(order_id=o.id).first()
            pname = ""
            if item:
                prod = session.query(Product).filter_by(id=item.product_id).first()
                pname = (prod.name[:18] if prod else "")
            ico   = _STATUS_ICO.get(o.status, "❓")
            sval  = o.status.value if o.status else "?"
            rows.append((o.id, ico, pname, sval))

    total_pages = max(1, (total + _PG_ORDERS - 1) // _PG_ORDERS)
    kb = []
    for o_id, ico, pname, sval in rows:
        if pname:
            lbl = f"{ico} #{o_id} | {pname} | {sval}"
        else:
            lbl = f"{ico} #{o_id} | {sval}"
        # Reuse the existing admin order detail callback (view_order_<id>)
        kb.append([InlineKeyboardButton(lbl[:64], callback_data=f"view_order_{o_id}")])

    pag = []
    if page > 0:
        pag.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"usr:ord:{user_id}:{page-1}"))
    if total_pages > 1:
        pag.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        pag.append(InlineKeyboardButton("➡️ Next", callback_data=f"usr:ord:{user_id}:{page+1}"))
    if pag:
        kb.append(pag)
    kb.append([InlineKeyboardButton("↩️ Return", callback_data=f"usr:det:{user_id}")])

    try:
        try:
            await query.edit_message_text(
                f"🧾 <b>Purchase History</b>\nUser: {username}\nTotal Orders: {total}",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 8. Position
# ─────────────────────────────────────────────────────────────────────────────

async def position_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: usr:pos:{user_id} — show current role."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        user_id = int(parts[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        username = _name_esc(user)
        tg_id    = user.telegram_id
        pos      = "ADMIN" if tg_id == app_settings.ADMIN_TELEGRAM_ID else "USER"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Return", callback_data=f"usr:det:{user_id}")],
    ])
    try:
        try:
            await query.edit_message_text(
                f"👔 <b>User Position</b>\n\n"
                f"User: {username}\n"
                f"Current Position: <b>{pos}</b>\n\n"
                "ℹ️ Admin role is determined by the <code>ADMIN_TELEGRAM_ID</code> "
                "environment setting. To promote a user, update that value and restart the bot.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass
