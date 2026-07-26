"""Admin Pending Deposit Review system.

Admin Panel → Payments → Pending Deposits

Shows every deposit (wallet top-up) currently waiting for a human to
review it — the generic ``ManualPaymentMethod`` flow plus bKash/Nagad's
Manual-mode flow (see services/gateway_manual_mode.py). Those are the
only payment methods in this project whose PENDING /
AWAITING_CONFIRMATION state means "an admin needs to check a submitted
TrxID/screenshot", as opposed to gateways (Binance Pay, Bybit Pay,
ZiniPay, Cryptomus, NOWPayments, Heleket, Telegram Stars) that are
auto-confirmed by an API/webhook and are intentionally left untouched
here.

This module is a presentation layer on top of the SAME ``Transaction``
table, ``TransactionStatus`` / ``PaymentMethod`` enums, ``WalletLedger``
and ``services.idempotency`` claim namespace ("manual_approve") already
used by handlers/admin_manual_payments.py (the "mp:*" panel) and
handlers/payment_handlers.py's admin_manual_approve/admin_manual_reject
(the inline "mp_approve_<id>"/"mp_reject_<id>" admin-notification
buttons). Approving or rejecting a deposit here claims the exact same
idempotency key those paths use, so a deposit can never be double
processed no matter which of the three surfaces an admin uses.

No database schema, business logic, callback, or API belonging to any
existing payment flow is modified — this only adds new "pd:*" callbacks
and a new Payments-menu entry pointing at them.

Callback namespace: pd:*
  pd:list:{page}:{sort}   — Pending Deposits list (DB-paginated)
  pd:det:{tx_id}          — Deposit detail (required fields + actions)
  pd:info:{tx_id}         — 📜 View Details (proof / screenshot / raw meta)
  pd:appr_ask:{tx_id}     — 🟢 Approve confirmation screen
  pd:appr_ok:{tx_id}      — Approve — credit wallet, notify, log
  pd:rej_ask:{tx_id}      — 🔴 Reject confirmation screen
  pd:rej_ok:{tx_id}       — Reject — no credit, notify
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session, User
from database.models import (
    Transaction,
    TransactionStatus,
    PaymentMethod,
    WalletLedger,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.helpers import sanitize_message, format_deposit_id
from services import payment_ui as pui

logger = logging.getLogger(__name__)

_PAGE_SZ = 8

# The only payment methods whose PENDING/AWAITING_CONFIRMATION state
# means "waiting on a human review" in this project — see module
# docstring. Every other gateway is confirmed automatically and is
# deliberately excluded so this panel never races an API/webhook.
#
# SYNCHRONIZATION FIX: these are no longer defined locally. They are read
# from services.payment_ui — the single shared definition also used by
# handlers/admin_handlers.py (Payments menu badge), handlers/
# admin_dashboard.py (dashboard badge) and handlers/admin_manual_payments.py.
# Previously each of those files had its own hand-copied tuple; whenever one
# was edited and the others weren't, the "Pending Deposits" number shown on
# one screen stopped matching another — the exact bug this audit fixes.
_REVIEWABLE_METHODS = pui.reviewable_methods()
_PENDING_STATUSES = pui.pending_tx_statuses()

_STATUS_ICON = {
    TransactionStatus.PENDING:               "⏳",
    TransactionStatus.AWAITING_CONFIRMATION: "⏳",
    TransactionStatus.COMPLETED:             "🟢",
    TransactionStatus.REJECTED:              "🔴",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _customer_name(user) -> str:
    if not user:
        return "?"
    return f"@{user.username}" if user.username else f"User {user.telegram_id}"


def _method_label(tx) -> str:
    if tx.manual_method and tx.manual_method.name:
        return tx.manual_method.name
    label, _ = pui.gateway_meta(tx.payment_method.value if tx.payment_method else None)
    return label


def _status_key(tx) -> str:
    return {
        TransactionStatus.PENDING:               "pending_review",
        TransactionStatus.AWAITING_CONFIRMATION:  "pending_review",
        TransactionStatus.COMPLETED:              "approved",
        TransactionStatus.REJECTED:               "rejected",
    }.get(tx.status, "pending_review")


_VERIFICATION_RESULT_LABEL = {
    # Generic ManualPaymentMethod / bKash / Nagad manual-mode deposits have
    # no gateway API to auto-verify — the whole point of this queue is that
    # a human checks the submitted TXID/screenshot. Shown so the admin
    # review card never has to silently omit the field one gateway's
    # PendingManualVerification-based card shows (⚠ Verification Result).
    True:  "⚠️ Not auto-verifiable — human review required",
    False: "⚠️ Not auto-verifiable — human review required",
}


def _network_for(tx) -> str | None:
    """Best-effort network/currency hint for the admin card's 🌐 Network
    field. Manual/bKash/Nagad deposits don't always have one; returns None
    (row is simply omitted by build_card) when there's nothing to show."""
    if tx.payment_method == PaymentMethod.BKASH:
        return "bKash (BDT)"
    if tx.payment_method == PaymentMethod.NAGAD:
        return "Nagad (BDT)"
    if getattr(tx, "crypto_network", None):
        return tx.crypto_network
    return None


def _deposit_detail_msg(tx, user) -> str:
    """THE single Admin Review Screen layout for every deposit shown by this
    panel — built entirely through services.payment_ui.admin_review_card so
    it is byte-for-byte the same template every other manual-review surface
    in the bot (bKash/Nagad, Binance Pay, Bybit Pay, ZiniPay) already uses.
    No handler builds its own message; this function only supplies values.
    """
    name    = pui.customer_display(user.username if user else None, user.telegram_id if user else None)
    amount  = f"${tx.amount:.2f}" if tx.amount is not None else "—"
    txn_id  = html.escape(str(tx.txid or tx.proof or "—")) if (tx.txid or tx.proof) else None
    return pui.admin_review_card(
        gateway_key=None,
        gateway_label_override=_method_label(tx),
        amount=amount,
        order_id=tx.id,
        created_at=tx.created_at,
        txn_id=txn_id,
        customer_name=name,
        user_id=user.telegram_id if user else None,
        network=_network_for(tx),
        verification_result=_VERIFICATION_RESULT_LABEL[True],
        status_key=_status_key(tx),
    )


def _deposit_kb(tx, user=None) -> InlineKeyboardMarkup:
    """THE single Admin Review Screen keyboard — built through
    pui.admin_review_keyboard so button emoji/order/labels are identical to
    every other manual-review surface: 🔄 Verify Again (n/a here — manual
    submissions have no API to re-query, so omitted), ✅ Approve,
    ❌ Reject, 👤 View User, ⬅ Back.
    """
    tg_id = user.telegram_id if user else None
    if tx.status in _PENDING_STATUSES:
        kb = pui.admin_review_keyboard(
            approve_cb=f"pd:appr_ask:{tx.id}",
            reject_cb=f"pd:rej_ask:{tx.id}",
            view_user_cb=(f"admin_view_user_pmv_{tg_id}" if tg_id else None),
            back_cb="pd:list:0:desc",
        )
    else:
        already = "🟢 Already Approved" if tx.status == TransactionStatus.COMPLETED else "🔴 Already Rejected"
        rows = [[InlineKeyboardButton(already, callback_data="noop")]]
        if tg_id:
            rows.append([InlineKeyboardButton("👤 View User", callback_data=f"admin_view_user_pmv_{tg_id}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="pd:list:0:desc")])
        kb = InlineKeyboardMarkup(rows)
    # 📜 View Details stays a separate row — it opens the extended raw-info
    # screen (proof/screenshot/admin notes), which is deliberately NOT part
    # of the standardized review card (see task spec: admin card shows only
    # the fixed field set; anything extra lives one tap away).
    rows = list(kb.inline_keyboard)
    rows.insert(-1, [InlineKeyboardButton("📜 View Details", callback_data=f"pd:info:{tx.id}")])
    return InlineKeyboardMarkup(rows)


async def _safe_edit(query, text, reply_markup=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    except Exception:
        logger.warning("Ignored Telegram/API error", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pending Deposits List
# ─────────────────────────────────────────────────────────────────────────────

async def pending_deposits_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:list:{page}:{sort}"""
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

    with get_db_session() as session:
        # Use one live result for the count, empty-state decision, and page
        # rows.  A separate COUNT query can observe a different state from
        # the rows selected immediately afterwards.
        pending_rows = pui.pending_deposit_rows(session, sort_desc=sort == "desc")
        total = len(pending_rows)

        # Keep a page requested after the last item from producing a
        # misleading empty page.  This uses the same live result, rather
        # than issuing another query that could disagree with the count.
        total_pages = max(1, (total + _PAGE_SZ - 1) // _PAGE_SZ)
        page = min(page, total_pages - 1)
        start = page * _PAGE_SZ
        txs = pending_rows[start:start + _PAGE_SZ]
        rows = []
        for tx in txs:
            u = session.query(User).filter_by(id=tx.user_id).first()
            username = f"@{u.username}" if (u and u.username) else f"ID:{u.telegram_id if u else '?'}"
            amt_str = f"{tx.amount:.2f}" if tx.amount is not None else "—"
            rows.append((tx.id, username, amt_str))

    total_pages = max(1, (total + _PAGE_SZ - 1) // _PAGE_SZ)
    next_sort   = "asc" if sort == "desc" else "desc"
    sort_lbl    = "🕒 Freshest" if sort == "desc" else "🕰 Oldest"

    if total == 0:
        # Replace the review screen with only the empty state.  In
        # particular, do not leave list controls or a section heading beside
        # the empty message.
        await _safe_edit(
            query,
            "No deposits are currently waiting for review.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Back", callback_data="admin_confirm_order")]
            ]),
        )
        return

    kb = []
    for tx_id, username, amt_str in rows:
        lbl = f"⏳ {username} | ${amt_str}"
        kb.append([InlineKeyboardButton(lbl[:64], callback_data=f"pd:det:{tx_id}")])

    pag = []
    if page > 0:
        pag.append(InlineKeyboardButton("« Prev", callback_data=f"pd:list:{page-1}:{sort}"))
    if total_pages > 1:
        pag.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        pag.append(InlineKeyboardButton("Next »", callback_data=f"pd:list:{page+1}:{sort}"))
    if pag:
        kb.append(pag)

    kb += [
        [InlineKeyboardButton(f"Sort: {sort_lbl}", callback_data=f"pd:list:{page}:{next_sort}")],
        [InlineKeyboardButton("⬅ Back", callback_data="admin_confirm_order")],
    ]

    header = f"🧾 <b>Pending Deposits</b> ({total})\nDeposits waiting for manual review."

    await _safe_edit(query, header, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Deposit Detail
# ─────────────────────────────────────────────────────────────────────────────

async def deposit_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:det:{tx_id}"""
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
            await _safe_edit(query, "❌ Deposit not found.")
            return
        _ = tx.manual_method  # noqa: F841 — eager-load before session closes
        u   = session.query(User).filter_by(id=tx.user_id).first()
        msg = _deposit_detail_msg(tx, u)
        kb  = _deposit_kb(tx, u)

    await _safe_edit(query, msg, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 3. View Details
# ─────────────────────────────────────────────────────────────────────────────

async def deposit_view_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:info:{tx_id} — extended raw info + proof/screenshot."""
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
            await _safe_edit(query, "❌ Deposit not found.")
            return
        _ = tx.manual_method  # noqa: F841
        u = session.query(User).filter_by(id=tx.user_id).first()

        lines = [
            f"📜 <b>Deposit #{tx.id} — Full Details</b>",
            "",
            f"💳 <b>Payment Method:</b> {html.escape(_method_label(tx))}",
            f"💰 <b>Amount:</b> {tx.amount:.2f}" if tx.amount is not None else "💰 <b>Amount:</b> —",
            f"🧾 <b>Deposit ID:</b> {format_deposit_id(tx.id, tx.created_at)}",
            f"🔗 <b>Transaction ID:</b> {html.escape(str(tx.txid or '—'))}",
            f"👤 <b>Customer:</b> {html.escape(_customer_name(u))}",
            f"🕒 <b>Created Time:</b> {tx.created_at.strftime('%Y-%m-%d %H:%M UTC') if tx.created_at else '—'}",
            f"📶 <b>Status:</b> {tx.status.value if tx.status else '—'}",
        ]
        if tx.completed_at:
            lines.append(f"✅ <b>Completed:</b> {tx.completed_at.strftime('%Y-%m-%d %H:%M UTC')}")
        if tx.proof:
            lines.append(f"📝 <b>Proof note:</b> {html.escape(tx.proof)}")
        if tx.admin_note:
            lines.append(f"🗒 <b>Admin note:</b> {html.escape(tx.admin_note)}")
        proof_file_id = tx.proof_file_id

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"pd:det:{tx_id}")]])

    if proof_file_id:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=proof_file_id,
                caption=f"🖼 Proof for Deposit #{tx_id}",
            )
        except Exception:
            logger.warning("Failed to send proof photo for tx %s", tx_id, exc_info=True)

    await _safe_edit(query, "\n".join(lines), kb)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Approve
# ─────────────────────────────────────────────────────────────────────────────

async def deposit_approve_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:appr_ask:{tx_id} — confirmation screen."""
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Yes, Approve", callback_data=f"pd:appr_ok:{tx_id}"),
        InlineKeyboardButton("❌ No, Cancel",   callback_data=f"pd:det:{tx_id}"),
    ]])
    await _safe_edit(
        query,
        f"❓ <b>Approve Deposit #{tx_id}?</b>\n\nThe user's wallet will be credited.",
        kb,
    )


async def deposit_approve_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:appr_ok:{tx_id} — idempotent approval + wallet credit."""
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

    # ── Duplicate-approval guard ──────────────────────────────────────────
    # Same idempotency namespace ("manual_approve") used by the existing
    # Manual Payments panel (mp:cfm_ok) and the legacy inline
    # mp_approve_<id> button, so this deposit can never be credited twice
    # no matter which of the three admin surfaces is used.
    try:
        from services.idempotency import claim as _idem_claim
        with _idem_claim("manual_approve", f"tx:{tx_id}") as _won:
            if not _won:
                logger.info("deposit_approve_execute: duplicate for tx %s", tx_id)
                await _safe_edit(
                    query,
                    f"⚠️ Deposit #{tx_id} has already been processed.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"pd:det:{tx_id}")]]),
                )
                return
    except Exception:
        logger.error(
            "idempotency.claim raised for manual_approve tx %s — fail closed",
            tx_id, exc_info=True,
        )
        await _safe_edit(
            query, "❌ Approval failed — please retry. No changes made.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"pd:det:{tx_id}")]]),
        )
        return

    user_tg_id: int | None = None
    amount: float = 0.0
    credited_usd: float = 0.0
    new_balance: float = 0.0
    is_gateway_manual = False
    method_label = "Manual"
    txn_ref = None

    with get_db_session() as session:
        # Atomic conditional flip: PENDING/AWAITING_CONFIRMATION → COMPLETED.
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_(_REVIEWABLE_METHODS),
            Transaction.status.in_(_PENDING_STATUSES),
        ).update(
            {
                Transaction.status:       TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
        if flipped == 0:
            session.rollback()
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} could not be approved — it may already be "
                "processed or in an invalid state.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            session.rollback()
            await _safe_edit(query, "❌ Deposit not found after update.")
            return

        # bKash/Nagad Manual mode stores `amount` in BDT — convert to USD
        # with the store's deposit rate before crediting the wallet
        # (wallet_balance is always USD), same as the existing
        # admin_manual_approve flow in handlers/payment_handlers.py.
        is_gateway_manual = tx.payment_method in (PaymentMethod.BKASH, PaymentMethod.NAGAD)
        if is_gateway_manual:
            from services.pricing import convert_currency
            credited_usd = convert_currency(tx.amount, "BDT", "USD")
            tx.admin_note = (
                f"Manual {tx.payment_method.value} deposit: ৳{tx.amount:.2f} BDT → "
                f"${credited_usd:.2f} USD credited (deposit rate applied) — "
                f"approved by admin {admin_tg_id}"
            )
        else:
            credited_usd = float(tx.amount or 0.0)
            tx.admin_note = f"approved by admin {admin_tg_id}"

        method_label = _method_label(tx)
        txn_ref = tx.txid or tx.proof
        amount = float(tx.amount or 0.0)

        # Atomic wallet credit with row-lock.
        user = (
            session.query(User)
            .filter(User.id == tx.user_id)
            .with_for_update()
            .first()
        )
        if not user:
            session.rollback()
            await _safe_edit(
                query,
                "❌ User not found. Deposit status updated but wallet not "
                "credited — manual intervention required.",
            )
            return

        prev_bal    = float(user.wallet_balance or 0.0)
        new_balance = prev_bal + credited_usd
        user.wallet_balance = new_balance
        session.add(WalletLedger(
            user_id       = user.id,
            delta         = credited_usd,
            balance_after = new_balance,
            reason        = f"deposit #{tx_id} approved",
            actor_type    = "admin",
            actor_id      = admin_tg_id,
            ref_type      = "manual_payment",
            ref_id        = str(tx_id),
        ))
        session.commit()
        user_tg_id = user.telegram_id

    log_admin_action(
        admin_tg_id, "deposit.approve",
        target_type="transaction", target_id=tx_id,
        details=f"amount={amount:.2f} credited_usd={credited_usd:.2f} new_bal={new_balance:.2f}",
    )

    amount_str = f"৳{amount:.2f} BDT → ${credited_usd:.2f}" if is_gateway_manual else f"${credited_usd:.2f}"

    # ── User notification: "Deposit Approved" ─────────────────────────────
    if user_tg_id:
        try:
            from utils.keyboards import create_main_menu_keyboard
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.user_payment_card(
                        gateway_key=None,
                        gateway_label_override=method_label,
                        stage="approved",
                        amount=amount_str,
                        order_id=tx_id,
                        txn_id=txn_ref,
                        extra=[("🔄", "New Balance", f"${new_balance:.2f}")],
                        note="Thank you!",
                    )
                ),
                parse_mode="HTML",
                reply_markup=create_main_menu_keyboard(user_id=user_tg_id),
            )
        except Exception:
            logger.warning(
                "Failed to notify user %s after deposit #%s approval",
                user_tg_id, tx_id, exc_info=True,
            )

    # ── Admin / log notification: canonical "Deposit Approved" card ───────
    try:
        from services.notifications import notify_admins as _notify_admins
        from utils.notify_format import render as _render_notif, utc_now_str as _ts
        asyncio.create_task(_notify_admins(
            context.bot,
            "deposit",
            _render_notif("💰", "Deposit Approved", [
                ("Deposit ID", format_deposit_id(tx_id)),
                ("Amount", amount_str),
                ("Method", method_label),
                ("Customer", f"<code>{user_tg_id}</code>" if user_tg_id else "—"),
                ("Approved By", f"<code>{admin_tg_id}</code>"),
            ], _ts()),
        ))
    except Exception:
        logger.warning(
            "Failed to send admin deposit-approved notification for tx %s",
            tx_id, exc_info=True,
        )

    # Refresh detail view
    with get_db_session() as session:
        tx = session.query(Transaction).filter_by(id=tx_id).first()
        _ = tx.manual_method if tx else None  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        msg = _deposit_detail_msg(tx, u) if tx else f"Deposit #{tx_id} processed."
        kb  = _deposit_kb(tx) if tx else InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="pd:list:0:desc")]]
        )

    await _safe_edit(
        query,
        f"✅ <b>Deposit #{tx_id} approved.</b>\n${credited_usd:.2f} credited to user's wallet.\n\n" + msg,
        kb,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reject
# ─────────────────────────────────────────────────────────────────────────────

async def deposit_reject_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:rej_ask:{tx_id} — confirmation screen."""
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 Yes, Reject", callback_data=f"pd:rej_ok:{tx_id}"),
        InlineKeyboardButton("❌ No, Cancel",  callback_data=f"pd:det:{tx_id}"),
    ]])
    await _safe_edit(
        query,
        f"❓ <b>Reject Deposit #{tx_id}?</b>\n\nThe user's wallet will NOT be credited.",
        kb,
    )


async def deposit_reject_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:rej_ok:{tx_id} — reject deposit, no wallet credit."""
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
    is_gateway_manual = False
    method_label = "Manual"

    with get_db_session() as session:
        # Atomic conditional flip — a deposit already COMPLETED (approved,
        # possibly through mp: or the legacy inline button) can never be
        # rejected out from under an already-credited wallet.
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_(_REVIEWABLE_METHODS),
            Transaction.status.in_(_PENDING_STATUSES),
        ).update(
            {
                Transaction.status:     TransactionStatus.REJECTED,
                Transaction.admin_note: f"rejected by admin {admin_tg_id}",
            },
            synchronize_session=False,
        )
        if flipped == 0:
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} could not be rejected — it may already "
                "be approved or in an invalid state.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return
        session.commit()

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        _ = tx.manual_method if tx else None  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        if tx:
            is_gateway_manual = tx.payment_method in (PaymentMethod.BKASH, PaymentMethod.NAGAD)
            method_label = _method_label(tx)
            amount = float(tx.amount or 0.0)
        if u:
            user_tg_id = u.telegram_id
        msg = _deposit_detail_msg(tx, u) if tx else f"Deposit #{tx_id} rejected."
        kb  = _deposit_kb(tx) if tx else InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="pd:list:0:desc")]]
        )

    log_admin_action(
        admin_tg_id, "deposit.reject",
        target_type="transaction", target_id=tx_id,
        details=f"amount={amount:.2f}",
    )

    amount_str = f"৳{amount:.2f} BDT" if is_gateway_manual else f"${amount:.2f}"

    # ── User notification: rejected, no credit ─────────────────────────────
    if user_tg_id:
        try:
            await context.bot.send_message(
                chat_id=user_tg_id,
                text=sanitize_message(
                    pui.user_payment_card(
                        gateway_key=None,
                        gateway_label_override=method_label,
                        stage="rejected",
                        amount=amount_str,
                        order_id=tx_id,
                        note="If you believe this is a mistake, please contact support with your proof.",
                    )
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.warning(
                "Failed to notify user %s after deposit #%s rejection",
                user_tg_id, tx_id, exc_info=True,
            )

    # ── Admin / log notification: canonical "Deposit Rejected" card ───────
    try:
        from services.notifications import notify_admins as _notify_admins
        from utils.notify_format import render as _render_notif, utc_now_str as _ts
        asyncio.create_task(_notify_admins(
            context.bot,
            "payment_reversed",
            _render_notif("❌", "Deposit Rejected", [
                ("Deposit ID", format_deposit_id(tx_id)),
                ("Amount", amount_str),
                ("Method", method_label),
                ("Customer", f"<code>{user_tg_id}</code>" if user_tg_id else "—"),
                ("Rejected By", f"<code>{admin_tg_id}</code>"),
            ], _ts()),
        ))
    except Exception:
        logger.warning(
            "Failed to send admin deposit-rejected notification for tx %s",
            tx_id, exc_info=True,
        )

    await _safe_edit(
        query,
        f"❌ <b>Deposit #{tx_id} rejected.</b>\n\n" + msg,
        kb,
    )
