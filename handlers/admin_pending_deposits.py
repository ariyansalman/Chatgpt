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

# _PENDING_STATUSES is a tuple of pure enum values that never change at
# runtime — safe to freeze once at import.
#
# IMPORTANT: _REVIEWABLE_METHODS is intentionally NOT defined as a
# module-level constant.  reviewable_methods() reads from the Payment
# Gateway Registry, which is populated by bootstrap_gateways() called from
# bot.py *after* this module is imported.  A frozen constant captured at
# import time would be empty (or incomplete), causing every approve/reject
# UPDATE to generate "IN ()" — an always-false predicate — so flipped
# would always be 0, the handler would bail out early with "could not be
# approved", and the idempotency claim (committed in its own prior
# transaction) would permanently block any retry.  Every caller that needs
# the live reviewable set must call pui.reviewable_methods() inline.
_PENDING_STATUSES = pui.pending_tx_statuses()

# ─────────────────────────────────────────────────────────────────────────────
# BACK-NAVIGATION FIX: single source of truth for the "⬅ Back" target used by
# every Pending Deposit screen (detail, approve ask/confirm, reject
# ask/confirm, already-processed guard, error paths). This used to be a
# literal "pd:list:0:desc" string copy-pasted at each call site — harmless
# while every copy agreed, but exactly one typo/edit away from a Back button
# that no longer points at the live list handler. Defining it once here means
# every "Back" button is guaranteed to always resolve to pending_deposits_list
# (below), which re-queries the database on every press and can never show a
# stale/cached screen.
#
# NAVIGATION FIX (page/sort preservation): a fixed "pd:list:0:desc" always
# sent the admin back to page 0 / newest-first, discarding whatever page and
# sort order they were actually browsing before opening a deposit's detail
# screen — e.g. reviewing page 3 sorted oldest-first, opening a deposit, then
# Back silently dropping them back to page 1 sorted newest-first. Admin Panel
# → Payments → Pending Deposits List → Deposit Details → Back must return to
# that same list at that same page/sort, not reset it.
#
# Fixed via _remember_list_state()/_back_to_list_cb() below: every time the
# list itself is rendered, the (possibly page-clamped) page/sort actually
# shown is stored in context.user_data, keyed per-admin via PTB's per-chat
# user_data. Every "⬅ Back" button inside Deposit Detail (and everything
# reachable from it — approve/reject/already-processed/error screens) then
# builds its callback_data from that remembered state instead of a constant.
# _BACK_TO_LIST_CB itself is kept only as the default fallback for _deposit_kb
# (i.e. page 0/desc, used only if no list has been viewed yet this session —
# should not normally happen, since detail is only reachable from the list).
# ─────────────────────────────────────────────────────────────────────────────
_BACK_TO_LIST_CB = "pd:list:0:desc"


def _remember_list_state(context: ContextTypes.DEFAULT_TYPE, page: int, sort: str) -> None:
    """Record the page/sort the admin is currently viewing in the Pending
    Deposits list. Call this with the FINAL (possibly page-clamped) values
    actually rendered, so Back always matches what the admin really saw —
    never the raw, possibly out-of-range values from the callback_data.
    """
    context.user_data["pd_list_page"] = page
    context.user_data["pd_list_sort"] = sort


def _back_to_list_cb(context: ContextTypes.DEFAULT_TYPE) -> str:
    """The callback_data for "⬅ Back" from Deposit Detail (and every screen
    reachable from it): the Pending Deposits list at the page/sort the admin
    was last viewing, falling back to page 0 / newest-first only if they
    somehow reached a detail screen without ever viewing the list this
    session.
    """
    page = context.user_data.get("pd_list_page", 0)
    sort = context.user_data.get("pd_list_sort", "desc")
    return f"pd:list:{page}:{sort}"


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
    if tx.payment_method and tx.payment_method.value == "zinipay":
        # Always show the SPECIFIC bKash / Nagad / Rocket provider the
        # deposit was actually made with — never the generic combined label.
        label, _ = pui.zinipay_provider_meta(crypto_address=tx.crypto_address)
        return label
    label, _ = pui.gateway_meta(tx.payment_method.value if tx.payment_method else None)
    return label


def _status_key(tx) -> str:
    return {
        TransactionStatus.PENDING:               "pending_review",
        TransactionStatus.AWAITING_CONFIRMATION:  "pending_review",
        TransactionStatus.COMPLETED:              "approved",
        TransactionStatus.REJECTED:               "rejected",
    }.get(tx.status, "pending_review")


# Generic ManualPaymentMethod / bKash / Nagad manual-mode deposits have no
# gateway API to auto-verify — the whole point of this queue is that a
# human checks the submitted TXID/screenshot. Rendered via the standard
# "⚠️ Auto Verification — Status: Not Applicable" block (see
# services/payment_ui.admin_review_card) instead of a raw free-text label.
_VERIFICATION_STATUS = "not_applicable"


def _network_for(tx) -> str | None:
    """Best-effort blockchain-network hint for the admin card's 🌐 Network
    field — shown ONLY for genuine on-chain methods (e.g. USDT TRC20/BEP20,
    BTC, ETH). Returns None (row is simply omitted) for anything that
    settles off-chain, such as bKash, Nagad, Bybit Pay, or Binance Pay —
    those are never a "network" and must never render a placeholder like
    "bKash (BDT)" in a field meant for blockchain networks.

    Sourced from the Payment Gateway Registry rather than hardcoded
    per-gateway comparisons — a future crypto gateway's ``network``
    metadata is picked up automatically.
    """
    from services.payment_workflow import network_hint
    from services.payment_gateway_registry import registry

    fallback = getattr(tx, "crypto_network", None)
    gid = tx.payment_method.value if tx.payment_method else None
    gateway = registry.get(gid)
    if gateway and gateway.payment_type != "crypto" and not fallback:
        return None
    return network_hint(tx.payment_method, fallback_network=fallback)


def _deposit_detail_msg(tx, user) -> str:
    """THE single Admin Review Screen layout for every deposit shown by this
    panel — built entirely through services.payment_ui.admin_review_card so
    it is byte-for-byte the same template every other manual-review surface
    in the bot (bKash/Nagad, Binance Pay, Bybit Pay, ZiniPay) already uses.
    No handler builds its own message; this function only supplies values.
    """
    amount  = f"${tx.amount:.2f}" if tx.amount is not None else "—"
    txn_id  = html.escape(str(tx.txid or tx.proof or "—")) if (tx.txid or tx.proof) else None
    return pui.admin_review_card(
        gateway_key=None,
        gateway_label_override=_method_label(tx),
        amount=amount,
        order_id=tx.id,
        created_at=tx.created_at,
        txn_id=txn_id,
        username=user.username if user else None,
        user_id=user.telegram_id if user else None,
        network=_network_for(tx),
        verification_status=_VERIFICATION_STATUS,
        status_key=_status_key(tx),
    )


def _ordered_pending_tx_ids(session) -> list:
    """Return an ordered list of pending Transaction IDs (newest-first, matching
    the default list-view sort) for use in ⏮/⏭ navigation.

    Only covers Transaction rows — PMV rows have their own per-gateway
    approval callbacks and are not part of this keyboard's approve/reject
    flow.  This function is intentionally simple and read-only; it reuses
    the same ``pui.pending_deposit_rows`` call that already drives
    ``_render_pending_deposits_list`` so the set of "pending" rows is always
    consistent with what the list view shows.
    """
    return [tx.id for tx in pui.pending_deposit_rows(session, sort_desc=True)]


def _deposit_kb(tx, user=None, prev_tx_id=None, next_tx_id=None, back_cb: str = _BACK_TO_LIST_CB) -> InlineKeyboardMarkup:
    """THE single Admin Review Screen keyboard — built through
    pui.admin_review_keyboard so button emoji/order/labels are identical to
    every other manual-review surface: 🔄 Verify Again (n/a here — manual
    submissions have no API to re-query, so omitted), ✅ Approve,
    ❌ Reject, 👤 View User, 📜 Deposit History, ⬅ Back.

    "📜 Deposit History" reuses the existing admin wallet-ledger screen
    (``up:wal:{uid}:{page}`` — handlers/admin_user_profile.py) keyed off
    the deposit's own internal user id, rather than introducing a new
    callback/handler.

    Optional ``prev_tx_id`` / ``next_tx_id`` add ⏮ Previous Pending and
    ⏭ Next Pending quick-navigation buttons directly inside the detail
    screen, eliminating the need to return to the list between reviews.
    These are derived from the live pending queue at render time (see
    ``deposit_detail``), so they always reflect the current queue state.

    ``back_cb`` is the callback_data for "⬅ Back" — callers should pass
    ``_back_to_list_cb(context)`` so Back returns to the exact page/sort the
    admin was browsing, rather than this function's page-0/desc default.
    """
    tg_id = user.telegram_id if user else None
    history_cb = f"up:wal:{tx.user_id}:0" if tx.user_id is not None else None
    if tx.status in _PENDING_STATUSES:
        kb = pui.admin_review_keyboard(
            approve_cb=f"pd:appr_ask:{tx.id}",
            reject_cb=f"pd:rej_ask:{tx.id}",
            view_user_cb=(f"admin_view_user_pmv_{tg_id}" if tg_id else None),
            history_cb=history_cb,
            back_cb=back_cb,
        )
    else:
        already = "🟢 Already Approved" if tx.status == TransactionStatus.COMPLETED else "🔴 Already Rejected"
        rows = [[InlineKeyboardButton(already, callback_data="noop")]]
        user_row = []
        if tg_id:
            user_row.append(InlineKeyboardButton("👤 View User", callback_data=f"admin_view_user_pmv_{tg_id}"))
        if history_cb:
            user_row.append(InlineKeyboardButton("📜 Deposit History", callback_data=history_cb))
        if user_row:
            rows.append(user_row)
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=back_cb)])
        kb = InlineKeyboardMarkup(rows)
    # 📜 View Details stays a separate row — it opens the extended raw-info
    # screen (proof/screenshot/admin notes), which is deliberately NOT part
    # of the standardized review card (see task spec: admin card shows only
    # the fixed field set; anything extra lives one tap away).
    rows = list(kb.inline_keyboard)
    rows.insert(-1, [InlineKeyboardButton("📜 View Details", callback_data=f"pd:info:{tx.id}")])
    # ⏮ Previous Pending / ⏭ Next Pending — quick navigation within the
    # live pending queue.  Only rendered when the caller supplies adjacent
    # IDs (i.e. when viewing a deposit that is itself still pending and
    # neighbours exist).  Placed just above ⬅ Back so the admin can chain
    # through the queue without ever returning to the list.
    nav_row = []
    if prev_tx_id is not None:
        nav_row.append(InlineKeyboardButton("⏮ Previous Pending", callback_data=f"pd:det:{prev_tx_id}"))
    if next_tx_id is not None:
        nav_row.append(InlineKeyboardButton("⏭ Next Pending", callback_data=f"pd:det:{next_tx_id}"))
    if nav_row:
        rows.insert(-1, nav_row)
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

async def _render_pending_deposits_list(query, page: int, sort: str, context: ContextTypes.DEFAULT_TYPE):
    """Load pending deposits fresh from the database and render whichever
    screen the live data calls for.

    This is the ONE implementation of the rendering rule:
        pending_count > 0  -> render the Pending Deposits list
        pending_count == 0 -> render the empty-state screen

    It is called both by the ``pd:list:{page}:{sort}`` callback handler
    (``pending_deposits_list`` below) and — indirectly, via the
    ``_back_to_list_cb(context)`` callback target — by every "⬅ Back" button
    in this module (detail, approve, reject, already-processed, error
    screens).
    Because every one of those Back buttons is dispatched back through this
    same function, the empty-state screen can only ever appear when the
    database genuinely has zero rows matching the live query at the moment
    the button is pressed — never a cached/stale copy of an earlier screen.

    GATEWAY-AGNOSTIC UNIFICATION: this queue merges BOTH sources of "a
    human needs to review this deposit" in the project, so admins have one
    single actionable list instead of having to also remember to check
    handlers/admin_binance.py / handlers/admin_bybit.py's separate PMV
    screens, or rely on a one-time DM notification they may have dismissed:

      • ``Transaction`` rows for gateways with no API at all (generic
        Manual Payment, bKash/Nagad in Manual mode) — pui.pending_deposit_rows.
      • ``PendingManualVerification`` rows created for ANY gateway whose
        own auto-verification failed (Binance Pay, Bybit Pay, ZiniPay
        bKash/Nagad/Rocket today; any future gateway automatically, since
        services/payment_workflow.py's enqueue_pending_review() is
        gateway-agnostic) — pui.pending_pmv_rows.

    Nothing here is gateway-specific: a brand-new gateway that starts
    calling enqueue_pending_review() on a failed auto-verification appears
    in this same list with zero changes to this file.
    """
    sort = "asc" if sort == "asc" else "desc"
    sort_desc = sort == "desc"

    with get_db_session() as session:
        # Use one live result per source for the count, empty-state
        # decision, and page rows.  A separate COUNT query can observe a
        # different state from the rows selected immediately afterwards.
        tx_rows = pui.pending_deposit_rows(session, sort_desc=sort_desc)
        pmv_rows = pui.pending_pmv_rows(session, sort_desc=sort_desc)

        merged = (
            [("tx", t.id, t.created_at) for t in tx_rows]
            + [("pmv", p.id, p.created_at) for p in pmv_rows]
        )
        merged.sort(key=lambda r: r[2] or datetime.min, reverse=sort_desc)
        total = len(merged)

        # Keep a page requested after the last item from producing a
        # misleading empty page.  This uses the same live result, rather
        # than issuing another query that could disagree with the count.
        total_pages = max(1, (total + _PAGE_SZ - 1) // _PAGE_SZ)
        page = min(page, total_pages - 1)
        # Remember the page/sort actually being rendered (post-clamp) so
        # "⬅ Back" from a Deposit Detail opened off this list returns here —
        # same page, same sort — instead of resetting to page 0.
        _remember_list_state(context, page, sort)
        start = page * _PAGE_SZ
        page_slice = merged[start:start + _PAGE_SZ]

        tx_by_id = {t.id: t for t in tx_rows}
        pmv_by_id = {p.id: p for p in pmv_rows}

        rows = []
        for kind, row_id, _created in page_slice:
            if kind == "tx":
                tx = tx_by_id[row_id]
                u = session.query(User).filter_by(id=tx.user_id).first()
                username = f"@{u.username}" if (u and u.username) else f"ID:{u.telegram_id if u else '?'}"
                amt_str = f"{tx.amount:.2f}" if tx.amount is not None else "—"
                rows.append(("tx", row_id, username, amt_str))
            else:
                pmv = pmv_by_id[row_id]
                username = f"ID:{pmv.telegram_user_id}"
                u = session.query(User).filter_by(telegram_id=pmv.telegram_user_id).first()
                if u and u.username:
                    username = f"@{u.username}"
                amt_str = f"{float(pmv.amount):.2f}" if pmv.amount is not None else "—"
                rows.append(("pmv", row_id, username, amt_str))

    # ── Rendering rule: pending_count == 0 -> empty state, else -> list ─────
    if total == 0:
        await _safe_edit(
            query,
            "No deposits are currently waiting for review.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_confirm_order")]
            ]),
        )
        return

    total_pages = max(1, (total + _PAGE_SZ - 1) // _PAGE_SZ)
    next_sort   = "asc" if sort == "desc" else "desc"
    sort_lbl    = "🕒 Freshest" if sort == "desc" else "🕰 Oldest"

    kb = []
    for kind, row_id, username, amt_str in rows:
        lbl = f"⏳ {username} | ${amt_str}"
        cb = f"pd:det:{row_id}" if kind == "tx" else f"pd:pmvdet:{row_id}"
        kb.append([InlineKeyboardButton(lbl[:64], callback_data=cb)])

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
        [InlineKeyboardButton("🔙 Back", callback_data="admin_confirm_order")],
    ]

    header = f"🧾 <b>Pending Deposits</b> ({total})\nDeposits waiting for manual review."

    await _safe_edit(query, header, InlineKeyboardMarkup(kb))


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

    await _render_pending_deposits_list(query, page, sort, context)


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

        # ── Quick navigation: ⏮ Previous Pending / ⏭ Next Pending ──────────
        # Computed from the live pending queue (same source as the list view)
        # so the buttons are only shown — and always accurate — when the
        # deposit being viewed is itself still pending AND adjacent pending
        # deposits actually exist.  Non-pending deposits show no nav buttons
        # (the admin has already acted; navigating a resolved queue would be
        # confusing).
        prev_tx_id = next_tx_id = None
        if tx.status in _PENDING_STATUSES:
            pending_ids = _ordered_pending_tx_ids(session)
            try:
                idx = pending_ids.index(tx_id)
                prev_tx_id = pending_ids[idx - 1] if idx > 0 else None
                next_tx_id = pending_ids[idx + 1] if idx < len(pending_ids) - 1 else None
            except ValueError:
                pass  # tx_id not in pending list — no nav buttons

        kb = _deposit_kb(tx, u, prev_tx_id=prev_tx_id, next_tx_id=next_tx_id, back_cb=_back_to_list_cb(context))

    await _safe_edit(query, msg, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 2b. PendingManualVerification Detail (Binance Pay / Bybit Pay / ZiniPay /
#     any future gateway whose auto-verification failed)
# ─────────────────────────────────────────────────────────────────────────────
#
# This is the read-only "card" half of the unification: it renders a PMV
# row through the exact same services.payment_ui.admin_review_card template
# every other review surface uses, then wires its buttons to the SAME
# already-registered callback_data patterns the original per-gateway admin
# notification uses (admin_<gateway>_approve_/reject_start_/verify_{tx_id}_
# {pmv_id} — see handlers/payment_handlers.py's _pmv_resolve /
# _admin_verify_again / build_admin_pmv_reject_conv, registered in bot.py).
# No wallet-crediting, approval, rejection, or re-verification logic is
# duplicated here — tapping a button on this screen runs the identical
# handler a tap on the original DM notification would have run.

_LEGACY_PMV_GATEWAYS = {"binance_pay", "bybit_pay", "zinipay"}


async def deposit_pmv_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pd:pmvdet:{pmv_id}"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        pmv_id = int(parts[2])
    except (IndexError, ValueError):
        return

    from database.models import PendingManualVerification
    from handlers.payment_handlers import _build_verify_again_admin_keyboard, _pmv_gateway_label

    with get_db_session() as session:
        pmv = session.query(PendingManualVerification).filter_by(id=pmv_id).first()
        if not pmv:
            await _safe_edit(
                query, "❌ Verification request not found.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=_back_to_list_cb(context))]]),
            )
            return

        tx = session.query(Transaction).filter_by(id=pmv.internal_order_id).first()
        u = session.query(User).filter_by(telegram_id=pmv.telegram_user_id).first()

        if pmv.status != "pending":
            already = "🟢 Already Approved" if pmv.status == "approved" else "🔴 Already Rejected"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(already, callback_data="noop")],
                [InlineKeyboardButton("⬅ Back", callback_data=_back_to_list_cb(context))],
            ])
            await _safe_edit(query, _pmv_gateway_label(pmv.gateway, tx=tx) + f" verification #{pmv.id}", kb)
            return

        amount = f"{pmv.amount} {pmv.currency}" if pmv.currency else f"{pmv.amount}"
        msg = pui.admin_review_card(
            gateway_key=pmv.gateway,
            gateway_label_override=_pmv_gateway_label(pmv.gateway, tx=tx),
            amount=amount,
            order_id=pmv.internal_order_id,
            created_at=pmv.created_at,
            txn_id=pmv.submitted_txid,
            username=u.username if u else None,
            user_id=pmv.telegram_user_id,
            network=pmv.network,
            verification_status="failed",
            verification_reason=pmv.auto_detail or pmv.auto_outcome,
            status_key="pending_review",
        )

        kb = _build_verify_again_admin_keyboard(
            pmv.gateway, pmv.internal_order_id, pmv.id, pmv.telegram_user_id,
        ) if pmv.gateway in _LEGACY_PMV_GATEWAYS else pui.admin_review_keyboard(
            verify_cb=f"admin_pmv_verify_{pmv.gateway}_{pmv.internal_order_id}_{pmv.id}",
            approve_cb=f"admin_pmv_approve_{pmv.gateway}_{pmv.internal_order_id}_{pmv.id}",
            reject_cb=f"admin_pmv_reject_start_{pmv.gateway}_{pmv.internal_order_id}_{pmv.id}",
            view_user_cb=f"admin_view_user_pmv_{pmv.telegram_user_id}",
        )
        # Append the same "⬅ Back" target every other card in this queue uses.
        rows = list(kb.inline_keyboard)
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=_back_to_list_cb(context))])
        kb = InlineKeyboardMarkup(rows)

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
            f"📶 <b>Status:</b> {pui.status_badge(_status_key(tx))}",
        ]
        if tx.completed_at:
            lines.append(f"✅ <b>Completed:</b> {tx.completed_at.strftime('%Y-%m-%d %H:%M UTC')}")
        if tx.proof:
            lines.append(f"📝 <b>Proof note:</b> {html.escape(tx.proof)}")
        if tx.admin_note:
            lines.append(f"🗒 <b>Admin note:</b> {html.escape(tx.admin_note)}")
        proof_file_id = tx.proof_file_id

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]])

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

    # Outcome variables populated inside the DB session and used afterwards.
    user_tg_id: int | None = None
    amount: float = 0.0
    credited_usd: float = 0.0
    new_balance: float = 0.0
    is_gateway_manual = False
    method_label = "Manual"
    txn_ref = None

    # ── Single atomic transaction: idempotency + status flip + wallet credit ─
    #
    # ROOT CAUSE 1 FIX — frozen registry:
    #   _REVIEWABLE_METHODS used to be a module-level constant evaluated at
    #   import time, before bootstrap_gateways() has run.  The tuple was
    #   therefore empty, generating SQL "IN ()" (always false), so flipped==0
    #   and the handler bailed out.  Fix: call pui.reviewable_methods() fresh
    #   inside the session so it always reflects the bootstrapped registry.
    #
    # ROOT CAUSE 2 FIX — non-atomic idempotency claim:
    #   The old code called claim() in its own context manager that opened,
    #   committed, and closed a SEPARATE session before the approval ran.  If
    #   approval then failed (e.g. due to root cause 1), the idempotency row
    #   was permanently committed, preventing any retry ("already processed"
    #   forever while the deposit remained PENDING).  Fix: use claim_locked()
    #   inside the SAME get_db_session() block so the claim rolls back with the
    #   approval if anything goes wrong.
    #
    # ROOT CAUSE 3 FIX — stale identity-map after bulk UPDATE:
    #   Session = scoped_session(..., expire_on_commit=False).  After a bulk
    #   UPDATE with synchronize_session=False, in-memory ORM objects keep their
    #   old field values.  The next query().filter_by(id=...).first() may return
    #   the stale cached object (status still PENDING) instead of going to DB.
    #   Fix: session.expire_all() immediately after the UPDATE forces a fresh
    #   DB read on every subsequent attribute access within this session.
    #
    # ROOT CAUSE 4 FIX — dangling verification lock:
    #   If a background verify job set verification_in_progress=True and then
    #   crashed, the scheduler treats the deposit as still-verifying and may
    #   re-queue or re-notify it after manual approval.  Fix: include
    #   verification_in_progress=False in the same UPDATE statement.
    #
    # ROOT CAUSE 5 FIX — re-notification after approval:
    #   Leaving review_notified=False allows a scheduler re-run to send the
    #   "please review this deposit" admin ping for an already-approved deposit.
    #   Fix: include review_notified=True in the UPDATE statement.
    from services.idempotency import claim_locked as _claim_locked

    with get_db_session() as session:
        # ── Idempotency (ROOT CAUSE 2 FIX: atomic with approval) ────────────
        # Same namespace used by mp:cfm_ok and legacy mp_approve_<id> button,
        # so this deposit can never be double-credited regardless of which
        # admin surface is used.
        if not _claim_locked(session, "manual_approve", f"tx:{tx_id}"):
            logger.info("deposit_approve_execute: duplicate for tx %s", tx_id)
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} has already been processed.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # ── Live registry (ROOT CAUSE 1 FIX) ─────────────────────────────────
        _live_reviewable = pui.reviewable_methods()
        _live_pending    = pui.pending_tx_statuses()
        if not _live_reviewable:
            logger.error(
                "deposit_approve_execute: reviewable_methods() returned empty set "
                "for tx %s — gateway bootstrap may not have completed yet", tx_id,
            )
            session.rollback()
            await _safe_edit(
                query,
                "❌ Approval failed — payment gateway registry is not initialised. "
                "Please retry in a moment.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # ── Atomic status flip (ROOT CAUSE 4+5 FIX included in SET clause) ──
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_(_live_reviewable),
            Transaction.status.in_(_live_pending),
        ).update(
            {
                Transaction.status:                   TransactionStatus.COMPLETED,
                Transaction.completed_at:             datetime.utcnow(),
                Transaction.verification_in_progress: False,   # ROOT CAUSE 4 FIX
                Transaction.review_notified:          True,    # ROOT CAUSE 5 FIX
            },
            synchronize_session=False,
        )
        if flipped == 0:
            session.rollback()
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} could not be approved — it may already be "
                "processed or in an invalid state.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # ── Reload from DB (ROOT CAUSE 3 FIX: clear stale identity-map) ──────
        session.expire_all()

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        if not tx:
            session.rollback()
            await _safe_edit(query, "❌ Deposit not found after update.")
            return

        # Gateways settling in a non-USD currency (e.g. bKash/Nagad, which
        # settle in BDT) need conversion before crediting the wallet
        # (wallet_balance is always USD). Sourced from the Payment Gateway
        # Registry's per-gateway `currency`/`to_usd` metadata instead of a
        # hardcoded BKASH/NAGAD check, so a future non-USD gateway inherits
        # this automatically.
        from services.payment_workflow import is_foreign_currency_gateway, credited_usd_amount, native_currency_label
        is_gateway_manual = is_foreign_currency_gateway(tx.payment_method)
        if is_gateway_manual:
            credited_usd = credited_usd_amount(tx.payment_method, tx.amount)
            native_ccy = native_currency_label(tx.payment_method) or "BDT"
            tx.admin_note = (
                f"Manual {tx.payment_method.value} deposit: ৳{tx.amount:.2f} {native_ccy} → "
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
                        extra=[("💵", "Credited", f"${credited_usd:.2f}")],
                        note="🎉 Your wallet has been credited successfully.",
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
                ("Payment Method", method_label),
                ("Customer", f"<code>{user_tg_id}</code>" if user_tg_id else "—"),
                ("Approved By", f"<code>{admin_tg_id}</code>"),
            ], _ts()),
        ))
    except Exception:
        logger.warning(
            "Failed to send admin deposit-approved notification for tx %s",
            tx_id, exc_info=True,
        )

    # ── Auto-advance: open next pending deposit, or show empty-state ─────────
    # After a successful approval the admin should never need to navigate back
    # to the Pending Deposits list manually.  We re-query the live queue
    # (exclusive of the deposit we just approved, which is now COMPLETED) and:
    #   • If another pending deposit exists → open its detail automatically,
    #     with ⏮/⏭ navigation pre-computed from the fresh queue.
    #   • If the queue is now empty → show the "✅ No pending deposits
    #     remaining." empty-state with a single "Back to Payments" button.
    approval_note = (
        f"✅ <b>Deposit #{tx_id} approved.</b> "
        f"${credited_usd:.2f} credited to user's wallet.\n\n"
    )

    with get_db_session() as session:
        remaining_ids = _ordered_pending_tx_ids(session)

    if remaining_ids:
        # Open the first item in the refreshed queue (newest-first order,
        # matching the list view default).
        next_id = remaining_ids[0]
        with get_db_session() as session:
            next_tx = session.query(Transaction).filter_by(id=next_id).first()
            _ = next_tx.manual_method if next_tx else None  # noqa: F841
            next_u  = session.query(User).filter_by(id=next_tx.user_id).first() if next_tx else None
            # Recompute nav for the deposit we're about to display.
            fresh_ids = _ordered_pending_tx_ids(session)
            try:
                idx = fresh_ids.index(next_id)
                prev_tx_id = fresh_ids[idx - 1] if idx > 0 else None
                next_tx_id = fresh_ids[idx + 1] if idx < len(fresh_ids) - 1 else None
            except ValueError:
                prev_tx_id = next_tx_id = None
            next_msg = _deposit_detail_msg(next_tx, next_u) if next_tx else f"Deposit #{next_id}."
            next_kb  = (
                _deposit_kb(next_tx, next_u, prev_tx_id=prev_tx_id, next_tx_id=next_tx_id, back_cb=_back_to_list_cb(context))
                if next_tx else
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=_back_to_list_cb(context))]])
            )
        await _safe_edit(
            query,
            approval_note + next_msg,
            next_kb,
        )
    else:
        # Queue is now empty — no more deposits to review.
        await _safe_edit(
            query,
            approval_note + "✅ No pending deposits remaining.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Payments", callback_data="admin_confirm_order")
            ]]),
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

    # Same five root-cause fixes as deposit_approve_execute — see its inline
    # comments for the full rationale.
    from services.idempotency import claim_locked as _claim_locked

    with get_db_session() as session:
        # ROOT CAUSE 2 FIX: idempotency claim is atomic with the rejection.
        if not _claim_locked(session, "manual_reject", f"tx:{tx_id}"):
            logger.info("deposit_reject_execute: duplicate for tx %s", tx_id)
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} has already been processed.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # ROOT CAUSE 1 FIX: live registry, not a frozen module constant.
        _live_reviewable = pui.reviewable_methods()
        _live_pending    = pui.pending_tx_statuses()
        if not _live_reviewable:
            logger.error(
                "deposit_reject_execute: reviewable_methods() empty for tx %s", tx_id,
            )
            session.rollback()
            await _safe_edit(
                query,
                "❌ Rejection failed — gateway registry not initialised. Please retry.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # Atomic conditional flip — a COMPLETED deposit can never be rejected
        # out from under an already-credited wallet.
        # ROOT CAUSE 4+5 FIX: clear verification lock, mark notified.
        flipped = session.query(Transaction).filter(
            Transaction.id == tx_id,
            Transaction.payment_method.in_(_live_reviewable),
            Transaction.status.in_(_live_pending),
        ).update(
            {
                Transaction.status:                   TransactionStatus.REJECTED,
                Transaction.admin_note:               f"rejected by admin {admin_tg_id}",
                Transaction.verification_in_progress: False,   # ROOT CAUSE 4 FIX
                Transaction.review_notified:          True,    # ROOT CAUSE 5 FIX
            },
            synchronize_session=False,
        )
        if flipped == 0:
            session.rollback()
            await _safe_edit(
                query,
                f"⚠️ Deposit #{tx_id} could not be rejected — it may already "
                "be approved or in an invalid state.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"pd:det:{tx_id}")]]),
            )
            return

        # ROOT CAUSE 3 FIX: expire stale identity-map before re-reading.
        session.expire_all()
        session.commit()

        tx = session.query(Transaction).filter_by(id=tx_id).first()
        _ = tx.manual_method if tx else None  # noqa: F841
        u  = session.query(User).filter_by(id=tx.user_id).first() if tx else None
        if tx:
            from services.payment_workflow import is_foreign_currency_gateway
            is_gateway_manual = is_foreign_currency_gateway(tx.payment_method)
            method_label = _method_label(tx)
            amount = float(tx.amount or 0.0)
        if u:
            user_tg_id = u.telegram_id
        msg = _deposit_detail_msg(tx, u) if tx else f"Deposit #{tx_id} rejected."
        kb  = _deposit_kb(tx, back_cb=_back_to_list_cb(context)) if tx else InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data=_back_to_list_cb(context))]]
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
                ("Payment Method", method_label),
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
