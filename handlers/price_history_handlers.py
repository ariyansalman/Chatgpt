"""User-facing Price History handlers — V23.

Callback namespace: ``ph:*``

Callbacks handled:
    ph:view:<pid>:<page>   — view paginated price history for a product
    ph:back:<pid>          — back to product detail from history
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

import services.price_history_service as svc
from utils import check_user_banned

logger = logging.getLogger(__name__)

_USER_PAGE_SIZE = 5


async def _safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _fmt(price: float) -> str:
    return f"${price:.2f}"


def _relative_time(dt) -> str:
    if not dt:
        return "—"
    from datetime import datetime
    now = datetime.utcnow()
    delta = now - dt
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            mins = delta.seconds // 60
            return f"{mins}m ago" if mins > 0 else "just now"
        return f"{hours}h ago"
    if days == 1:
        return "Yesterday"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return f"{months} month{'s' if months > 1 else ''} ago"


def _build_summary_text(product_name: str, summary: dict, product_id: int) -> str:
    lines = [f"📈 <b>Price History</b>", f"<i>{product_name}</i>", ""]

    cp = summary.get("current_price")
    pp = summary.get("previous_price")
    hp = summary.get("highest_price")
    lp = summary.get("lowest_price")
    ap = summary.get("average_price")
    lc = summary.get("last_change")
    tc = summary.get("total_changes", 0)

    lines.append(f"💰 <b>Current Price:</b>   {_fmt(cp) if cp is not None else '—'}")
    lines.append(f"🔙 <b>Previous Price:</b>  {_fmt(pp) if pp is not None else '—'}")
    lines.append(f"📈 <b>Highest Price:</b>   {_fmt(hp) if hp is not None else '—'}")
    lines.append(f"📉 <b>Lowest Price:</b>    {_fmt(lp) if lp is not None else '—'}")
    lines.append(f"📊 <b>Average Price:</b>   {_fmt(ap) if ap is not None else '—'}")
    lines.append(
        f"🕐 <b>Last Updated:</b>    {_relative_time(lc)}"
        + (f"  <i>({lc.strftime('%b %d, %Y')})</i>" if lc else "")
    )
    lines.append(f"🔢 <b>Total Changes:</b>   {tc}")
    return "\n".join(lines)


def _build_timeline_text(records: list[dict], page: int, total: int) -> str:
    if not records:
        return "\n<i>No price change records yet.</i>"

    lines = ["\n<b>── Timeline ──</b>"]
    show_diff = svc.show_difference()
    show_pct  = svc.show_pct_change()

    for r in records:
        when  = _relative_time(r["changed_at"])
        date  = r["changed_at"].strftime("%b %d, %Y %H:%M") if r["changed_at"] else "—"
        diff  = r["difference"]
        pct   = r["pct_change"]
        arrow = "📈" if diff > 0 else ("📉" if diff < 0 else "➡️")
        old_p = _fmt(r["old_price"]) if r["old_price"] else "—"

        parts = [f"{arrow} <b>{_fmt(r['new_price'])}</b>   <i>{when}</i>  ({date})"]
        if r["old_price"]:
            parts.append(f"   From: {old_p}")
        if show_diff and diff:
            sign = "+" if diff > 0 else ""
            parts.append(f"   Δ {sign}{_fmt(diff)}")
        if show_pct and pct is not None:
            sign = "+" if pct > 0 else ""
            parts.append(f"   {sign}{pct:.1f}%")
        if r.get("reason"):
            parts.append(f"   📝 {r['reason']}")
        if r.get("changed_by_name"):
            parts.append(f"   👤 {r['changed_by_name']}")

        lines.append("\n".join(parts))

    return "\n\n".join(lines)


def _history_keyboard(product_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    pages = svc.total_pages(total, _USER_PAGE_SIZE)
    rows = []

    # Pagination
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ph:view:{product_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ph:view:{product_id}:{page + 1}"))
        rows.append(nav)

    # Back to product
    rows.append([InlineKeyboardButton("🔙 Back to Product", callback_data=f"product_{product_id}")])
    return InlineKeyboardMarkup(rows)


async def ph_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all ph:* callbacks."""
    query = update.callback_query
    tg_id = update.effective_user.id

    if check_user_banned(tg_id):
        await query.answer("⛔ You are banned.", show_alert=True)
        return

    await query.answer()

    if not svc.is_enabled():
        await query.answer("📈 Price History is currently unavailable.", show_alert=True)
        return

    if not svc.allow_users():
        await query.answer("📈 Price History is not available.", show_alert=True)
        return

    data   = query.data   # ph:view:<pid>:<page>
    parts  = data.split(":")

    if len(parts) >= 3 and parts[1] == "view":
        try:
            product_id = int(parts[2])
            page = int(parts[3]) if len(parts) >= 4 else 0
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

        from database import get_db_session, Product as _Product
        with get_db_session() as s:
            product = s.query(_Product).filter_by(id=product_id).first()
            product_name = product.name if product else f"Product #{product_id}"

        summary = svc.get_product_summary(product_id)
        records, total = svc.get_product_history(product_id, page, _USER_PAGE_SIZE)

        text = _build_summary_text(product_name, summary, product_id)
        text += _build_timeline_text(records, page, total)
        kb   = _history_keyboard(product_id, page, total)
        await _safe_edit(query, text, kb)
        return

    # Fallback
    await query.answer()
