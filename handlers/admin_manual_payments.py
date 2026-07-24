"""Admin Manual Payments panel.

Sections:
  1. Payments List — DB-paginated (LIMIT/OFFSET), sort toggle (Freshest/Oldest)
  2. Payment Detail — full info, status-aware action buttons
  3. Get Proof      — send stored proof photo or text
  4. Confirm        — confirmation screen → idempotent wallet credit
  5. Reject         — confirmation screen → status update + user notification
  6. Edit Debitable — ConversationHandler for editing Transaction.amount
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime
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

from database import get_db_session, User
from database.models import (
    Transaction,
    TransactionStatus,
    PaymentMethod,
    WalletLedger,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.helpers import sanitize_message
from telegram.error import BadRequest
from services import payment_ui as pui

logger = logging.getLogger(__name__)

# ── Conversation state ────────────────────────────────────────────────────────
WAITING_EDIT_AMOUNT = 20

_PAGE_SZ = 8

_STATUS_ICON = {
    TransactionStatus.PENDING:               "⏳",
    TransactionStatus.AWAITING_CONFIRMATION: "⏳",
    TransactionStatus.COMPLETED:             "✅",
    TransactionStatus.REJECTED:              "❌",
    TransactionStatus.CANCELLED:             "🚫",
    TransactionStatus.EXPIRED:               "⏰",
    TransactionStatus.FAILED:                "❌",
}

_STATUS_BADGE_MAP = {
    TransactionStatus.PENDING:               pui.status_badge("pending_review"),
    TransactionStatus.AWAITING_CONFIRMATION: pui.status_badge("waiting_payment"),
    TransactionStatus.COMPLETED:             pui.status_badge("approved"),
    TransactionStatus.REJECTED:              pui.status_badge("rejected"),
    TransactionStatus.CANCELLED:             pui.status_badge("cancelled"),
    TransactionStatus.EXPIRED:               pui.status_badge("expired"),
    TransactionStatus.FAILED:                pui.status_badge("rejected"),
}

_PENDING_STATUSES = (
    TransactionStatus.PENDING,
    TransactionStatus.AWAITING_CONFIRMATION,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tx_user_name(user) -> str:
    if not user:
        return "?"
    return f"@{user.username}" if user.username else f"User {user.telegram_id}"


def _payment_msg(tx, user) -> str:
    name    = html.escape(_tx_user_name(user))
    tg_id   = user.telegram_id if user else "?"
    badge   = _STATUS_BADGE_MAP.get(tx.status, f"❓ {tx.status.value if tx.status else 'unknown'}")
    date    = tx.created_at.strftime("%Y-%m-%d %H:%M") if tx.created_at else "—"
    amt     = f"${tx.amount:.2f}" if tx.amount is not None else "—"
    method  = tx.manual_method.name if tx.manual_method else "Manual"
    return pui.build_card(
        title=f"Payment #{tx.id}", title_emoji="📝",
        fields=[
            ("💳", "Gateway", method),
            ("💰", "Amount", amt),
            ("🆔", "Order ID", f"#{tx.id}"),
            ("👤", "Customer", name),
            ("🆔", "User ID", tg_id),
            ("🕒", "Time", date),
        ],
        note=badge,
    )


def _payment_kb(tx) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton("🖼 Get Proof", callback_data=f"mp:proof:{tx.id}")]]
    is_pending = tx.status in _PENDING_STATUSES
    if is_pending:
        kb.append([
            InlineKeyboardButton("✅ Confirm",        callback_data=f"mp:cfm_ask:{tx.id}"),
            InlineKeyboardButton("❌ Reject",         callback_data=f"mp:rej_ask:{tx.id}"),
        ])
        kb.append([InlineKeyboardButton("✏️ Edit Debitable", callback_data=f"mp:edit:{tx.id}")])
    elif tx.status == TransactionStatus.COMPLETED:
        kb.append([InlineKeyboardButton("✅ Already Confirmed", callback_data="noop")])
    elif tx.status == TransactionStatus.REJECTED:
        kb.append([InlineKeyboardButton("❌ Already Rejected",  callback_data="noop")])
    kb.append([InlineKeyboardButton("↩️ Return", callback_data="mp:list:0:desc")])
    return InlineKeyboardMarkup(kb)


def _clr_edit(ctx):
    for k in ("_edit_tx_id", "_edit_prev", "_edit_new", "_edit_idem"):
        ctx.user_data.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Payments List
# ─────────────────────────────────────────────────────────────────────────────

async def payments_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:list:{page}:{sort}"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
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
        total = (
            session.query(_f.count(Transaction.id))
            .filter(Transaction.payment_method == PaymentMethod.MANUAL)
            .scalar() or 0
        )
        col = Transaction.created_at.asc() if sort == "asc" else Transaction.created_at.desc()
        txs = (
            session.query(Transaction)
            .filter(Transaction.payment_method == PaymentMethod.MANUAL)
            .order_by(col)
            .offset(page * _PAGE_SZ)
            .limit(_PAGE_SZ)
            .all()
        )
        rows = []
        for tx in txs:
            u = session.query(User).filter_by(id=tx.user_id).first()
            icon     = _STATUS_ICON.get(tx.status, "❓")
            username = f"@{u.username}" if (u and u.username) else f"ID:{u.telegram_id if u else '?'}"
            amt_str  = f"{tx.amount:.2f}" if tx.amount is not None else "Pending"
            rows.append((tx.id, icon, username, amt_str))

    total_pages = max(1, (total + _PAGE_SZ - 1) // _PAGE_SZ)
    next_sort   = "asc" if sort == "desc" else "desc"
    sort_lbl    = "🕒 Freshest" if sort == "desc" else "🕰 Oldest"
    mode_lbl    = "FRESHEST"    if sort == "desc" else "OLDEST"

    kb = []
    for tx_id, icon, username, amt_str in rows:
        lbl = f"{icon} {username} | {amt_str}"
        kb.append([InlineKeyboardButton(lbl[:64], callback_data=f"mp:det:{tx_id}")])

    pag = []
    if page > 0:
        pag.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"mp:list:{page-1}:{sort}"))
    if total_pages > 1:
        pag.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        pag.append(InlineKeyboardButton("➡️ Next", callback_data=f"mp:list:{page+1}:{sort}"))
    if pag:
        kb.append(pag)

    kb += [
        [InlineKeyboardButton(f"Sort: {sort_lbl}", callback_data=f"mp:list:{page}:{next_sort}")],
        [InlineKeyboardButton("↩️ Return",          callback_data="admin_users")],
    ]
    try:
        try:
            await query.edit_message_text(
                f"📝 <b>Manual Payments</b> ({total})\nMode: {mode_lbl}",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 2. Payment Detail
# ─────────────────────────────────────────────────────────────────────────────

async def payment_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:det:{tx_id}"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            try:
                await query.edit_message_text("❌ Payment not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        # Eagerly load the relationship before session closes
        _ = tx.manual_method  # noqa: F841
        u   = session.query(User).filter_by(id=tx.user_id).first()
        msg = _payment_msg(tx, u)
        kb  = _payment_kb(tx)

    try:
        try:
            await query.edit_message_text(msg, reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. Get Proof
# ─────────────────────────────────────────────────────────────────────────────

async def payment_get_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:proof:{tx_id} — send proof photo/text."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            await query.answer("❌ Payment not found.", show_alert=True)
            return
        proof_file_id = tx.proof_file_id
        proof_text    = tx.proof

    chat_id = update.effective_chat.id
    if proof_file_id:
        try:
            caption = f"🖼 Proof for Payment #{tx_id}"
            if proof_text:
                caption += f"\n📝 {proof_text}"
            await context.bot.send_photo(chat_id=chat_id, photo=proof_file_id, caption=caption)
            return
        except Exception:
            logger.warning("Failed to send proof photo for tx %s", tx_id, exc_info=True)

    if proof_text:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📝 Proof for Payment #{tx_id}:\n{proof_text}",
            )
        except Exception:
            pass
    else:
        await query.answer("❌ Payment proof is unavailable.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Confirm
# ─────────────────────────────────────────────────────────────────────────────

async def payment_confirm_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:cfm_ask:{tx_id} — confirmation screen."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Confirm", callback_data=f"mp:cfm_ok:{tx_id}"),
            InlineKeyboardButton("❌ No, Cancel",   callback_data=f"mp:det:{tx_id}"),
        ]
    ])
    try:
        try:
            await query.edit_message_text(
                f"❓ <b>Are you sure you want to CONFIRM Payment #{tx_id}?</b>\n\n"
                f"User will receive funds.",
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def payment_confirm_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:cfm_ok:{tx_id} — idempotent payment approval + wallet credit."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return

    admin_tg_id = update.effective_user.id

    # ── Idempotency guard (reuse existing PaymentIdempotency architecture) ────
    try:
        from services.idempotency import claim as _idem_claim
        with _idem_claim("manual_approve", f"tx:{tx_id}") as _won:
            if not _won:
                logger.info("payment_confirm_execute: duplicate for tx %s", tx_id)
                try:
                    await query.edit_message_text(
                        f"⚠️ Payment #{tx_id} has already been processed.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("↩️ Return", callback_data=f"mp:det:{tx_id}")]]
                        ),
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return
    except Exception:
        logger.error(
            "idempotency.claim raised for manual_approve tx %s — fail closed",
            tx_id, exc_info=True,
        )
        try:
            await query.edit_message_text(
                "❌ Idempotency check failed. No changes made.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Return", callback_data=f"mp:det:{tx_id}")]]
                ),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    amount    : float = 0.0
    new_balance: float = 0.0
    user_tg_id: int | None = None

    with get_db_session() as session:
        # Atomic conditional flip: PENDING/AWAITING → COMPLETED
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method == PaymentMethod.MANUAL,
            Transaction.status.in_(list(_PENDING_STATUSES)),
        ).update(
            {
                Transaction.status:       TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow(),
                Transaction.admin_note:   f"approved by admin {admin_tg_id}",
            },
            synchronize_session=False,
        )
        if flipped == 0:
            session.rollback()
            try:
                await query.edit_message_text(
                    f"⚠️ Payment #{tx_id} could not be confirmed — "
                    "it may already be processed or in an invalid state.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("↩️ Return", callback_data=f"mp:det:{tx_id}")]]
                    ),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            session.rollback()
            try:
                await query.edit_message_text("❌ Payment not found after update.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        amount = float(tx.amount or 0.0)

        # Atomic wallet credit with row-lock
        user = (
            session.query(User)
            .filter(User.id == tx.user_id)
            .with_for_update()
            .first()
        )
        if not user:
            session.rollback()
            try:
                await query.edit_message_text(
                    "❌ User not found. Payment status updated but wallet not credited — "
                    "manual intervention required."
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        prev_bal    = float(user.wallet_balance or 0.0)
        new_balance = prev_bal + amount
        user.wallet_balance = new_balance
        session.add(WalletLedger(
            user_id      = user.id,
            delta        = amount,
            balance_after= new_balance,
            reason       = f"manual payment #{tx_id} approved",
            actor_type   = "admin",
            actor_id     = admin_tg_id,
            ref_type     = "manual_payment",
            ref_id       = str(tx_id),
        ))
        session.commit()
        user_tg_id = user.telegram_id

    log_admin_action(
        admin_tg_id, "manual_payment.approve",
        target_type="transaction", target_id=tx_id,
        details=f"amount={amount:.2f} new_bal={new_balance:.2f}",
    )

    # User notification — non-transactional; log failure only, never re-credit
    if user_tg_id:
        try:
            from utils.keyboards import create_main_menu_keyboard
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.user_payment_card(
                        gateway_key="manual",
                        stage="approved",
                        amount=f"${amount:.2f}",
                        order_id=tx_id,
                        extra=[("🔄", "New Balance", f"${new_balance:.2f}")],
                        note="Thank you!",
                    )
                ),
                parse_mode="HTML",
                reply_markup=create_main_menu_keyboard(user_id=user_tg_id),
            )
        except Exception:
            logger.warning(
                "Failed to notify user %s after payment #%s approval",
                user_tg_id, tx_id, exc_info=True,
            )

    # Refresh detail view
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        _ = tx.manual_method if tx else None  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        msg = _payment_msg(tx, u) if tx else f"Payment #{tx_id} processed."
        kb  = _payment_kb(tx) if tx else InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Return", callback_data="mp:list:0:desc")]]
        )

    try:
        try:
            await query.edit_message_text(
                f"✅ <b>Payment #{tx_id} confirmed.</b>\n"
                f"${amount:.2f} credited to user's wallet.\n\n"
                + msg,
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reject
# ─────────────────────────────────────────────────────────────────────────────

async def payment_reject_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:rej_ask:{tx_id} — confirmation screen."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Reject", callback_data=f"mp:rej_ok:{tx_id}"),
            InlineKeyboardButton("❌ No, Cancel",  callback_data=f"mp:det:{tx_id}"),
        ]
    ])
    try:
        try:
            await query.edit_message_text(
                f"❓ <b>Are you sure you want to REJECT Payment #{tx_id}?</b>",
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def payment_reject_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:rej_ok:{tx_id} — reject payment with status re-validation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return

    admin_tg_id = update.effective_user.id
    user_tg_id: int | None = None
    amount: float = 0.0

    with get_db_session() as session:
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method == PaymentMethod.MANUAL,
            Transaction.status.in_(list(_PENDING_STATUSES)),
        ).update(
            {
                Transaction.status:     TransactionStatus.REJECTED,
                Transaction.admin_note: f"rejected by admin {admin_tg_id}",
            },
            synchronize_session=False,
        )
        if flipped == 0:
            try:
                await query.edit_message_text(
                    f"⚠️ Payment #{tx_id} could not be rejected — "
                    "it may already be confirmed or in an invalid state.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("↩️ Return", callback_data=f"mp:det:{tx_id}")]]
                    ),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        session.commit()

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        _ = tx.manual_method if tx else None  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        if u:
            user_tg_id = u.telegram_id
            amount     = float(tx.amount or 0.0)
        msg = _payment_msg(tx, u) if tx else f"Payment #{tx_id} rejected."
        kb  = _payment_kb(tx) if tx else InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Return", callback_data="mp:list:0:desc")]]
        )

    log_admin_action(
        admin_tg_id, "manual_payment.reject",
        target_type="transaction", target_id=tx_id,
        details=f"amount={amount:.2f}",
    )

    if user_tg_id:
        try:
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.user_payment_card(
                        gateway_key="manual",
                        stage="rejected",
                        amount=f"${amount:.2f}",
                        order_id=tx_id,
                        note="If you believe this is a mistake, please contact support with your proof.",
                    )
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.warning(
                "Failed to notify user %s after payment #%s rejection",
                user_tg_id, tx_id, exc_info=True,
            )

    try:
        try:
            await query.edit_message_text(
                f"❌ <b>Payment #{tx_id} rejected.</b>\n\n" + msg,
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 6. Edit Debitable
# ─────────────────────────────────────────────────────────────────────────────

async def edit_debitable_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:edit:{tx_id} — start edit-amount conversation."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
    except (IndexError, ValueError):
        return ConversationHandler.END

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            try:
                await query.edit_message_text("❌ Payment not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        if tx.status not in _PENDING_STATUSES:
            await query.answer(
                "❌ Only pending payments can be edited.", show_alert=True
            )
            return ConversationHandler.END
        current_amount = float(tx.amount or 0.0)

    _clr_edit(context)
    context.user_data["_edit_tx_id"] = tx_id
    context.user_data["_edit_prev"]  = current_amount

    try:
        try:
            await query.edit_message_text(
                f"✏️ <b>Edit Debitable Amount</b>\n\n"
                f"Payment #{tx_id}\n"
                f"Current amount: <b>${current_amount:.2f}</b>\n\n"
                f"Enter new amount for this payment:\n\n"
                f"Send /cancel to abort.",
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass
    return WAITING_EDIT_AMOUNT


async def edit_debitable_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new amount text and show confirmation."""
    if not has_permission(update.effective_user.id, "manage_orders"):
        _clr_edit(context)
        return ConversationHandler.END

    tx_id = context.user_data.get("_edit_tx_id")
    prev  = context.user_data.get("_edit_prev", 0.0)
    if not tx_id:
        await update.message.reply_text("❌ Session lost. Please start again.")
        _clr_edit(context)
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    try:
        new_amount = Decimal(raw)
    except InvalidOperation:
        await update.message.reply_text(
            "❌ Invalid number. Enter a valid decimal amount (e.g. 25.00):"
        )
        return WAITING_EDIT_AMOUNT

    if new_amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0. Enter again:")
        return WAITING_EDIT_AMOUNT

    idem_tok = str(uuid.uuid4())[:16]
    context.user_data["_edit_new"]  = str(new_amount)
    context.user_data["_edit_idem"] = idem_tok

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Yes, Confirm",
                callback_data=f"mp:edit_cfm:{tx_id}:{idem_tok}",
            ),
            InlineKeyboardButton("❌ No, Cancel", callback_data=f"mp:det:{tx_id}"),
        ]
    ])
    try:
        await update.message.reply_text(
            f"❓ <b>Confirm Payment Amount Change</b>\n\n"
            f"Payment #{tx_id}\n"
            f"Previous Amount: <b>${float(prev):.2f}</b>\n"
            f"New Amount: <b>${float(new_amount):.2f}</b>",
            reply_markup=kb, parse_mode="HTML",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def edit_debitable_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: mp:edit_cfm:{tx_id}:{token} — persist new amount."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    try:
        tx_id = int(parts[2])
        tok   = parts[3]
    except (IndexError, ValueError):
        return

    stored = (context.user_data.get("_edit_idem") or "")[:16]
    if tok != stored:
        await query.answer("⚠️ Already processed or session expired.", show_alert=True)
        return

    try:
        new_amount = Decimal(context.user_data.get("_edit_new", "0"))
    except InvalidOperation:
        try:
            await query.edit_message_text("❌ Session data corrupted. No changes made.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        _clr_edit(context)
        return

    # Consume token — double-click guard
    context.user_data["_edit_idem"] = None
    admin_tg_id = update.effective_user.id

    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            try:
                await query.edit_message_text("❌ Payment not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            _clr_edit(context)
            return
        if tx.status not in _PENDING_STATUSES:
            try:
                await query.edit_message_text(
                    "❌ Payment is no longer pending — amount cannot be changed."
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            _clr_edit(context)
            return
        tx.amount = float(new_amount)
        session.commit()
        _ = tx.manual_method  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first()
        msg = _payment_msg(tx, u)
        kb  = _payment_kb(tx)

    log_admin_action(
        admin_tg_id, "manual_payment.edit_amount",
        target_type="transaction", target_id=tx_id,
        details=f"new_amount={float(new_amount):.2f}",
    )
    _clr_edit(context)
    try:
        try:
            await query.edit_message_text(
                f"✅ Amount updated to ${float(new_amount):.2f}\n\n" + msg,
                reply_markup=kb, parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def edit_debitable_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel edit debitable via /cancel."""
    _clr_edit(context)
    if update.message:
        try:
            await update.message.reply_text("✏️ Amount edit cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


def build_edit_debitable_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_debitable_start, pattern=r"^mp:edit:\d+$"),
        ],
        states={
            WAITING_EDIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_debitable_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", edit_debitable_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
