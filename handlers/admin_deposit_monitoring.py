"""Admin Deposit Monitoring Dashboard — V1.

Displays real-time deposit statistics:
  • Today's Deposits       — count of all deposit transactions started today
  • Today's Volume         — total USD credited today (completed only)
  • Pending Deposits       — awaiting confirmation
  • Completed Deposits     — successfully verified
  • Cancelled Deposits     — cancelled/expired
  • Failed Deposits        — failed or rejected
  • Success Rate           — completed / (completed + failed + cancelled) %
  • Average Verification Time — mean minutes from created_at to completed_at

Callback namespace: ``adm:*``

  adm:menu          — main dashboard (live stats)
  adm:refresh       — same as adm:menu (force refresh)
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Optional

from sqlalchemy import func, and_
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session
from database.models import Transaction, TransactionStatus
from utils.permissions import has_permission

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guard(uid: int) -> bool:
    return has_permission(uid, "view_analytics")


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                      disable_web_page_preview=True)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                try:
                    await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                               disable_web_page_preview=True)
                except Exception:
                    pass
    else:
        msg = getattr(update, "message", None)
        if msg:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)


# ── Stats collection ─────────────────────────────────────────────────────────

def _collect_deposit_stats() -> dict:
    """Query Transaction table for today's deposit monitoring stats."""
    stats = {
        "today_count":       0,
        "today_volume":      0.0,
        "pending":           0,
        "completed":         0,
        "cancelled":         0,
        "failed":            0,
        "success_rate":      None,   # float 0-100 or None
        "avg_verify_mins":   None,   # float or None
        "as_of":             datetime.utcnow(),
    }
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        with get_db_session() as s:
            # ── Today's deposit count ─────────────────────────────────────
            stats["today_count"] = (
                s.query(func.count(Transaction.id))
                 .filter(Transaction.created_at >= today_start)
                 .scalar() or 0
            )

            # ── Today's completed volume ──────────────────────────────────
            stats["today_volume"] = float(
                s.query(func.coalesce(func.sum(Transaction.amount), 0.0))
                 .filter(
                     Transaction.created_at >= today_start,
                     Transaction.status == TransactionStatus.COMPLETED,
                 )
                 .scalar() or 0.0
            )

            # ── All-time status breakdowns ────────────────────────────────
            status_counts: dict[str, int] = dict(
                s.query(Transaction.status, func.count(Transaction.id))
                 .group_by(Transaction.status)
                 .all()
            )

            stats["pending"] = (
                (status_counts.get(TransactionStatus.PENDING) or 0) +
                (status_counts.get(TransactionStatus.AWAITING_CONFIRMATION) or 0)
            )
            stats["completed"] = status_counts.get(TransactionStatus.COMPLETED) or 0
            stats["cancelled"] = (
                (status_counts.get(TransactionStatus.CANCELLED) or 0) +
                (status_counts.get(TransactionStatus.EXPIRED) or 0)
            )
            stats["failed"] = (
                (status_counts.get(TransactionStatus.FAILED) or 0) +
                (status_counts.get(TransactionStatus.REJECTED) or 0)
            )

            # ── Success rate ──────────────────────────────────────────────
            denominator = stats["completed"] + stats["failed"] + stats["cancelled"]
            if denominator > 0:
                stats["success_rate"] = round(
                    stats["completed"] / denominator * 100, 1
                )

            # ── Average verification time (completed txns with both timestamps)
            completed_rows = (
                s.query(Transaction.created_at, Transaction.completed_at)
                 .filter(
                     Transaction.status == TransactionStatus.COMPLETED,
                     Transaction.completed_at.isnot(None),
                 )
                 .limit(500)          # cap scan for performance
                 .all()
            )
            if completed_rows:
                total_secs = sum(
                    (r.completed_at - r.created_at).total_seconds()
                    for r in completed_rows
                    if r.completed_at and r.created_at and r.completed_at > r.created_at
                )
                count = len(completed_rows)
                if count > 0 and total_secs > 0:
                    stats["avg_verify_mins"] = round(total_secs / count / 60, 1)

    except Exception:
        logger.exception("deposit_monitoring: stats query failed")

    return stats


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "N/A"
    if rate >= 90:
        return f"🟢 {rate}%"
    if rate >= 70:
        return f"🟡 {rate}%"
    return f"🔴 {rate}%"


def _fmt_time(mins: Optional[float]) -> str:
    if mins is None:
        return "N/A"
    if mins < 1:
        return "< 1 min"
    if mins < 60:
        return f"{mins:.1f} min"
    hrs = mins / 60
    return f"{hrs:.1f} hr"


def _render_text(stats: dict) -> str:
    as_of = stats["as_of"].strftime("%Y-%m-%d %H:%M UTC")
    today_str = datetime.utcnow().strftime("%b %d")

    lines = [
        f"📊 <b>Deposit Monitoring</b>",
        f"<i>as of {as_of}</i>",
        "",
        f"<b>— Today ({today_str}) ─────────────────</b>",
        f"📥 Today's Deposits:       <b>{stats['today_count']:,}</b>",
        f"💰 Today's Volume:         <b>${stats['today_volume']:,.2f}</b>",
        "",
        f"<b>— All Time ─────────────────────────</b>",
        f"⏳ Pending Deposits:       <b>{stats['pending']:,}</b>",
        f"✅ Completed Deposits:     <b>{stats['completed']:,}</b>",
        f"❌ Cancelled Deposits:     <b>{stats['cancelled']:,}</b>",
        f"💥 Failed Deposits:        <b>{stats['failed']:,}</b>",
        "",
        f"<b>— Performance ──────────────────────</b>",
        f"🎯 Success Rate:           <b>{_fmt_rate(stats['success_rate'])}</b>",
        f"⏱ Avg Verification Time:  <b>{_fmt_time(stats['avg_verify_mins'])}</b>",
    ]
    return "\n".join(lines)


# ── Main view ─────────────────────────────────────────────────────────────────

async def adm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main deposit monitoring dashboard."""
    q = update.callback_query
    if q:
        await q.answer()

    uid = update.effective_user.id
    if not _guard(uid):
        await _deny(update)
        return

    stats = _collect_deposit_stats()
    text = _render_text(stats)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh")],
        [InlineKeyboardButton("💳 Payments Menu",    callback_data="admin_gateways"),
         InlineKeyboardButton("📝 Audit Log",        callback_data="acc:audit:page:0")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    await _edit(update, text, kb)


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def adm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data if q else ""

    if data in ("adm:menu", "adm:refresh"):
        await adm_menu(update, context)
    elif q:
        await q.answer("Unknown action.", show_alert=False)


def register_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(adm_dispatch, pattern=r"^adm:"))
