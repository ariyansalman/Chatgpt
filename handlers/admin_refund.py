"""Admin Automatic Refund System — V21.

Auto-refund for failed/cancelled/timed-out/duplicate/overpayment orders.
Refunds go to user wallet or trigger original-method refund flags.
Admin can approve/reject/manually trigger refunds. Full history and logs.

Callback namespace: ``aref:*``
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, text as sqltxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import (
    get_db_session, Order, OrderStatus, User, Transaction,
    TransactionStatus, WalletLedger,
)
from database.models import Refund, RefundStatus, RefundTrigger
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from utils.helpers import format_price, sanitize_message
from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 10

# Conversation states
(AREF_MANUAL_ORDER, AREF_MANUAL_REASON, AREF_REJECT_REASON) = range(3)


def _is_admin(uid: int) -> bool:
    return uid == settings.ADMIN_TELEGRAM_ID or has_permission(uid, "manage_orders")


def _enabled() -> bool:
    return cfg.get_bool("feature_auto_refund_enabled", True)


async def _safe_edit(query, text: str, kb=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(data="aref:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


# ── Main menu ─────────────────────────────────────────────────────────────

async def aref_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await _safe_edit(query, "💰 <b>Refund System</b>\n\n❌ Feature disabled.", _back_kb("acc:root"))
        return

    with get_db_session() as s:
        try:
            pending = s.query(func.count(Refund.id)).filter(Refund.status == RefundStatus.PENDING).scalar() or 0
            total = s.query(func.count(Refund.id)).scalar() or 0
            processed = s.query(func.count(Refund.id)).filter(Refund.status == RefundStatus.PROCESSED).scalar() or 0
            total_refunded = float(s.query(func.coalesce(func.sum(Refund.amount), 0.0)).filter(
                Refund.status == RefundStatus.PROCESSED).scalar() or 0.0)
        except Exception:
            pending = total = processed = 0
            total_refunded = 0.0

    text = (
        f"💰 <b>Refund System</b>\n\n"
        f"<b>Pending:</b> {pending}  |  <b>Total:</b> {total}\n"
        f"<b>Processed:</b> {processed}  |  <b>Total Refunded:</b> {format_price(total_refunded)}\n"
    )
    kb = [
        [InlineKeyboardButton("⏳ Pending Refunds", callback_data="aref:list:pending:0"),
         InlineKeyboardButton("✅ Processed", callback_data="aref:list:processed:0")],
        [InlineKeyboardButton("❌ Rejected", callback_data="aref:list:rejected:0"),
         InlineKeyboardButton("📋 All Refunds", callback_data="aref:list:all:0")],
        [InlineKeyboardButton("➕ Manual Refund", callback_data="aref:manual")],
        [InlineKeyboardButton("⚙ Settings", callback_data="aref:settings")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Refund list ────────────────────────────────────────────────────────────

async def aref_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    status_filter = parts[2] if len(parts) > 2 else "all"
    page = int(parts[3]) if len(parts) > 3 else 0

    with get_db_session() as s:
        try:
            q = s.query(Refund).order_by(Refund.created_at.desc())
            if status_filter != "all":
                status_enum = RefundStatus[status_filter.upper()]
                q = q.filter(Refund.status == status_enum)
            total = q.count()
            rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
            items = [
                (r.id, r.order_id, r.amount, r.status, r.trigger, r.refund_type, r.created_at)
                for r in rows
            ]
        except Exception as e:
            logger.warning("aref_list query failed: %s", e)
            items = []
            total = 0

    status_icons = {
        "pending": "⏳", "approved": "👍", "rejected": "❌",
        "processed": "✅", "failed": "⚠️",
    }
    lines = [f"💰 <b>Refunds [{status_filter}]</b>  ({total} total)\n"]
    kb = []
    for rid, oid, amount, status, trigger, rtype, created_at in items:
        st = status.value if hasattr(status, 'value') else str(status)
        tr = trigger.value if hasattr(trigger, 'value') else str(trigger)
        icon = status_icons.get(st, "📄")
        when = created_at.strftime("%m/%d") if created_at else "?"
        kb.append([InlineKeyboardButton(
            f"{icon} #{rid} Order#{oid} {format_price(float(amount))} [{tr}] {when}",
            callback_data=f"aref:view:{rid}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"aref:list:{status_filter}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"aref:list:{status_filter}:{page+1}"))
    if nav:
        kb.append(nav)
    if not items:
        lines.append("No refunds found.")
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aref:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Refund detail view ────────────────────────────────────────────────────

async def aref_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        rid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return await aref_menu(update, context)

    with get_db_session() as s:
        try:
            r = s.get(Refund, rid)
            if not r:
                await query.answer("❌ Not found.", show_alert=True)
                return
            status = r.status.value if hasattr(r.status, 'value') else str(r.status)
            trigger = r.trigger.value if hasattr(r.trigger, 'value') else str(r.trigger)
            rtype = r.refund_type
            amount = float(r.amount)
            order_id = r.order_id
            user_id = r.user_id
            reason = r.reason or "—"
            admin_note = r.admin_note or "—"
            created_at = r.created_at.strftime("%Y-%m-%d %H:%M UTC") if r.created_at else "?"
            processed_at = r.processed_at.strftime("%Y-%m-%d %H:%M UTC") if r.processed_at else "—"
            admin_tgid = r.admin_telegram_id

            # Get user info
            user = s.get(User, user_id)
            user_str = f"@{user.username}" if user and user.username else f"ID:{user_id}"
        except Exception as e:
            logger.warning("aref_view failed: %s", e)
            await query.answer("❌ Error loading refund.", show_alert=True)
            return

    status_icon = {"pending": "⏳", "approved": "👍", "rejected": "❌",
                   "processed": "✅", "failed": "⚠️"}.get(status, "📄")
    text = (
        f"💰 <b>Refund #{rid}</b>\n\n"
        f"<b>Status:</b> {status_icon} {status}\n"
        f"<b>Order:</b> #{order_id}  |  <b>User:</b> {user_str}\n"
        f"<b>Amount:</b> {format_price(amount)}\n"
        f"<b>Type:</b> {rtype}  |  <b>Trigger:</b> {trigger}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Admin Note:</b> {admin_note}\n"
        f"<b>Created:</b> {created_at}\n"
        f"<b>Processed:</b> {processed_at}\n"
    )
    if admin_tgid:
        text += f"<b>Actioned by:</b> admin <code>{admin_tgid}</code>\n"

    kb = []
    if status == "pending":
        kb.append([
            InlineKeyboardButton("✅ Approve → Wallet", callback_data=f"aref:approve_wallet:{rid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"aref:reject_ask:{rid}"),
        ])
        kb.append([InlineKeyboardButton("💳 Approve → Original", callback_data=f"aref:approve_orig:{rid}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="aref:list:all:0")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Approve / Reject actions ──────────────────────────────────────────────

async def aref_approve_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        rid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    await _process_refund(rid, "wallet", update.effective_user.id, query)


async def aref_approve_orig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        rid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    await _process_refund(rid, "original_method", update.effective_user.id, query)


async def _process_refund(rid: int, refund_type: str, admin_id: int, query):
    with get_db_session() as s:
        try:
            r = s.get(Refund, rid)
            if not r or r.status != RefundStatus.PENDING:
                await query.answer("❌ Cannot process in current state.", show_alert=True)
                return

            # Atomic claim — two admins (or one admin double-tapping the
            # approve button) can otherwise both read status==PENDING before
            # either commits, and both credit the wallet: a double refund.
            # This conditional UPDATE is the single choke point that lets
            # only ONE caller win the PENDING→PROCESSED transition; anyone
            # else gets claimed == 0 and bails out before touching the wallet.
            claimed = s.query(Refund).filter(
                Refund.id == rid,
                Refund.status == RefundStatus.PENDING,
            ).update(
                {
                    Refund.status: RefundStatus.PROCESSED,
                    Refund.refund_type: refund_type,
                    Refund.admin_telegram_id: admin_id,
                    Refund.processed_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
            if claimed == 0:
                s.rollback()
                await query.answer("❌ Cannot process in current state.", show_alert=True)
                return

            user = s.get(User, r.user_id)
            if not user:
                s.rollback()
                await query.answer("❌ User not found.", show_alert=True)
                return
            amount = float(r.amount)

            if refund_type == "wallet":
                # Credit user wallet
                user.wallet_balance = (user.wallet_balance or 0.0) + amount
                ledger = WalletLedger(
                    user_id=user.id,
                    amount=amount,
                    reason=f"refund_order_{r.order_id}",
                    balance_after=user.wallet_balance,
                )
                s.add(ledger)
            # For original_method: just mark as processed — actual gateway refund is external
            s.commit()
            order_id = r.order_id
        except Exception as e:
            logger.exception("_process_refund failed: %s", e)
            await query.answer("❌ Error processing refund.", show_alert=True)
            return

    log_admin_action(admin_id, "refund.approve", "refund", rid,
                     f"type={refund_type} amount={amount} order={order_id}",
                     module="auto_refund")
    # Notify user
    try:
        await query.bot.send_message(
            user.telegram_id,
            f"✅ <b>Refund Processed</b>\n\n"
            f"Your refund of {format_price(amount)} for order #{order_id} has been processed.\n"
            f"{'Credits have been added to your wallet.' if refund_type == 'wallet' else 'Refund via original payment method — please allow 3-5 business days.'}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _safe_edit(query,
        f"✅ Refund #{rid} processed via {'wallet' if refund_type == 'wallet' else 'original method'}.",
        _back_kb("aref:menu"))


async def aref_reject_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        rid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    context.user_data["_aref_rid"] = rid
    await _safe_edit(query,
        f"❌ <b>Reject Refund #{rid}</b>\n\n"
        "Send the rejection reason (will be shown to user):",
        _back_kb(f"aref:view:{rid}"))
    return  # Caller is not a conversation — just store rid


async def aref_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    rid = context.user_data.pop("_aref_rid", None)
    if not rid:
        return ConversationHandler.END
    reason = (update.message.text or "").strip()[:500]
    with get_db_session() as s:
        try:
            r = s.get(Refund, rid)
            if r and r.status == RefundStatus.PENDING:
                r.status = RefundStatus.REJECTED
                r.admin_note = reason
                r.admin_telegram_id = update.effective_user.id
                r.processed_at = datetime.utcnow()
                user = s.get(User, r.user_id)
                order_id = r.order_id
                amount = float(r.amount)
                tgid = user.telegram_id if user else None
                s.commit()
            else:
                await update.message.reply_text("❌ Cannot reject in current state.")
                return ConversationHandler.END
        except Exception as e:
            logger.exception("aref_reject_reason failed: %s", e)
            return ConversationHandler.END

    log_admin_action(update.effective_user.id, "refund.reject", "refund", rid,
                     reason[:200], module="auto_refund")
    if tgid:
        try:
            await context.bot.send_message(
                tgid,
                sanitize_message(
                    f"❌ <b>Refund Declined</b>\n\n"
                    f"Your refund request for order #{order_id} ({format_price(amount)}) was not approved.\n"
                    f"Reason: {reason}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
    await update.message.reply_text(f"❌ Refund #{rid} rejected.")
    return ConversationHandler.END


def build_aref_reject_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aref_reject_ask, pattern=r"^aref:reject_ask:\d+$")],
        states={
            AREF_REJECT_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, aref_reject_reason),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ── Manual refund (conversation) ──────────────────────────────────────────

async def aref_manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["_aref_manual"] = {}
    await _safe_edit(query,
        "💰 <b>Manual Refund — Step 1/2</b>\n\n"
        "Send the <b>order ID</b> to refund:",
        _back_kb())
    return AREF_MANUAL_ORDER


async def aref_manual_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re as _re
    txt = (update.message.text or "").strip()

    # Accept formatted display IDs: ORD-YYYYMMDD-NNNNNN
    _ord_re = _re.compile(r"^ORD-\d{8}-0*(\d+)$", _re.IGNORECASE)
    m = _ord_re.match(txt)
    if m:
        oid = int(m.group(1))
    elif txt.lstrip("#").isdigit():
        oid = int(txt.lstrip("#"))
    else:
        await update.message.reply_text(
            "❌ Invalid Order ID format.\n\n"
            "Expected format:\n<code>ORD-YYYYMMDD-000001</code>\n\n"
            "Or send the numeric order ID (e.g. <code>42</code>).",
            parse_mode="HTML",
        )
        return AREF_MANUAL_ORDER

    with get_db_session() as s:
        order = s.get(Order, oid)
        if not order:
            await update.message.reply_text(f"❌ Order #{oid} not found. Try again:")
            return AREF_MANUAL_ORDER
        context.user_data["_aref_manual"]["order_id"] = oid
        context.user_data["_aref_manual"]["user_id"] = order.user_id
        context.user_data["_aref_manual"]["amount"] = float(order.total_amount)

    await update.message.reply_text(
        f"💰 <b>Step 2/2 — Reason</b>\n\n"
        f"Order #{oid} found. Amount: {format_price(float(context.user_data['_aref_manual']['amount']))}\n\n"
        "Send the refund reason:",
        parse_mode="HTML")
    return AREF_MANUAL_REASON


async def aref_manual_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    reason = (update.message.text or "").strip()[:500]
    data = context.user_data.pop("_aref_manual", {})
    oid = data.get("order_id")
    uid = data.get("user_id")
    amount = data.get("amount", 0.0)
    if not oid or not uid:
        await update.message.reply_text("❌ Missing data. Start over.")
        return ConversationHandler.END

    with get_db_session() as s:
        r = Refund(
            order_id=oid,
            user_id=uid,
            amount=amount,
            reason=reason,
            refund_type="wallet",
            status=RefundStatus.PENDING,
            trigger=RefundTrigger.MANUAL,
            created_at=datetime.utcnow(),
        )
        s.add(r)
        s.commit()
        rid = r.id

    log_admin_action(update.effective_user.id, "refund.create_manual", "refund", rid,
                     f"order={oid} amount={amount}", module="auto_refund")
    await update.message.reply_text(
        f"✅ Manual refund #{rid} created for order #{oid} ({format_price(amount)}).\n"
        "It is now pending — approve it from the refund list.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def aref_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_aref_manual", None)
    context.user_data.pop("_aref_rid", None)
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Cancelled.", _back_kb("aref:menu"))
    return ConversationHandler.END


def build_aref_manual_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aref_manual_start, pattern=r"^aref:manual$")],
        states={
            AREF_MANUAL_ORDER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, aref_manual_order)],
            AREF_MANUAL_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, aref_manual_reason)],
        },
        fallbacks=[
            CallbackQueryHandler(aref_cancel_conv, pattern=r"^aref:menu$"),
            CommandHandler("cancel", aref_cancel_conv),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ── Settings ──────────────────────────────────────────────────────────────

async def aref_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    auto_failed = cfg.get_bool("refund_auto_failed_orders", True)
    auto_cancelled = cfg.get_bool("refund_auto_cancelled_orders", False)
    auto_timeout = cfg.get_bool("refund_auto_timed_out", True)
    auto_duplicate = cfg.get_bool("refund_auto_duplicate", True)
    notify_admin = cfg.get_bool("refund_notify_admin", True)

    text = (
        "⚙ <b>Refund System Settings</b>\n\n"
        f"Auto-refund failed orders:    {'✅' if auto_failed else '❌'}\n"
        f"Auto-refund cancelled orders: {'✅' if auto_cancelled else '❌'}\n"
        f"Auto-refund timed-out:        {'✅' if auto_timeout else '❌'}\n"
        f"Auto-refund duplicates:       {'✅' if auto_duplicate else '❌'}\n"
        f"Notify admin on new refund:   {'✅' if notify_admin else '❌'}\n"
    )
    kb = [
        [InlineKeyboardButton(
            f"{'✅' if auto_failed else '❌'} Failed Orders Auto-Refund",
            callback_data="aref:cfg:refund_auto_failed_orders")],
        [InlineKeyboardButton(
            f"{'✅' if auto_cancelled else '❌'} Cancelled Orders Auto-Refund",
            callback_data="aref:cfg:refund_auto_cancelled_orders")],
        [InlineKeyboardButton(
            f"{'✅' if auto_timeout else '❌'} Timeout Auto-Refund",
            callback_data="aref:cfg:refund_auto_timed_out")],
        [InlineKeyboardButton(
            f"{'✅' if auto_duplicate else '❌'} Duplicate Payment Auto-Refund",
            callback_data="aref:cfg:refund_auto_duplicate")],
        [InlineKeyboardButton(
            f"{'✅' if notify_admin else '❌'} Admin Notifications",
            callback_data="aref:cfg:refund_notify_admin")],
        [InlineKeyboardButton("🔙 Back", callback_data="aref:menu")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def aref_toggle_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    key = query.data.split(":")[2]
    current = cfg.get_bool(key, True)
    cfg.set(key, not current)
    log_admin_action(update.effective_user.id, "refund.settings.toggle", "config", key,
                     new_value=str(not current), module="auto_refund")
    await aref_settings(update, context)


# ── Auto-refund job ───────────────────────────────────────────────────────

async def auto_refund_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job that creates pending refunds for qualifying orders."""
    if not cfg.get_bool("feature_auto_refund_enabled", True):
        return

    cutoff = datetime.utcnow() - timedelta(hours=24)
    new_refunds = []

    try:
        with get_db_session() as s:
            # Auto-refund failed orders
            if cfg.get_bool("refund_auto_failed_orders", True):
                failed_orders = (s.query(Order)
                                 .filter(Order.status == OrderStatus.FAILED,
                                         Order.created_at >= cutoff)
                                 .all())
                for o in failed_orders:
                    if not _refund_exists(s, o.id):
                        r = Refund(
                            order_id=o.id, user_id=o.user_id,
                            amount=o.total_amount, reason="Order failed",
                            refund_type="wallet", status=RefundStatus.PENDING,
                            trigger=RefundTrigger.FAILED_ORDER,
                            created_at=datetime.utcnow(),
                        )
                        s.add(r)
                        new_refunds.append((o.id, float(o.total_amount), o.created_at))

            # Auto-refund cancelled orders (only if enabled)
            if cfg.get_bool("refund_auto_cancelled_orders", False):
                cancelled = (s.query(Order)
                             .filter(Order.status == OrderStatus.CANCELLED,
                                     Order.created_at >= cutoff)
                             .all())
                for o in cancelled:
                    if not _refund_exists(s, o.id):
                        r = Refund(
                            order_id=o.id, user_id=o.user_id,
                            amount=o.total_amount, reason="Order cancelled",
                            refund_type="wallet", status=RefundStatus.PENDING,
                            trigger=RefundTrigger.CANCELLED,
                            created_at=datetime.utcnow(),
                        )
                        s.add(r)
                        new_refunds.append((o.id, float(o.total_amount), o.created_at))

            s.commit()
    except Exception:
        logger.exception("auto_refund_job: query failed")
        return

    if new_refunds and cfg.get_bool("refund_notify_admin", True):
        try:
            from utils.helpers import format_order_id as _fmt_order_id
            from utils import format_price as _fmt_price
            msg = f"💰 <b>Auto-Refund Pending Approval</b>\n"
            msg += f"<i>{len(new_refunds)} order(s) queued for review:</i>\n\n"
            for oid, amt, created in new_refunds[:10]:
                _display_id = _fmt_order_id(oid, created)
                msg += f"  🆔 {_display_id}  ·  {_fmt_price(amt)}\n"
            if len(new_refunds) > 10:
                msg += f"\n  <i>… and {len(new_refunds) - 10} more</i>"
            await context.bot.send_message(
                settings.ADMIN_TELEGRAM_ID, msg, parse_mode="HTML")
        except Exception:
            pass


def _refund_exists(session, order_id: int) -> bool:
    try:
        return session.query(Refund).filter(Refund.order_id == order_id).first() is not None
    except Exception:
        return False


# ── Dispatcher ────────────────────────────────────────────────────────────

async def aref_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    if action == "menu":
        await aref_menu(update, context)
    elif action == "list":
        await aref_list(update, context)
    elif action == "view":
        await aref_view(update, context)
    elif action == "approve_wallet":
        await aref_approve_wallet(update, context)
    elif action == "approve_orig":
        await aref_approve_orig(update, context)
    elif action == "settings":
        await aref_settings(update, context)
    elif action == "cfg":
        await aref_toggle_cfg(update, context)
    else:
        await aref_menu(update, context)
