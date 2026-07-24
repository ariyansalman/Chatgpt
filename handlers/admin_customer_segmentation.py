"""V45 — Admin Customer Segmentation & Tags.

Callback namespace: cseg:*
ConversationHandler states: 9820–9825

Provides a unified UI for managing customer segments and tags:
  • View all segments with live user counts
  • Create/delete/rename custom segments
  • Auto-segment rules (applied by background job)
  • Browse users in any segment
  • Bulk actions: broadcast, coupon targeting, export
  • Tag management (extends existing CRM CustomerTag system)

Reuses existing CustomerTag / CustomerTagAssignment / CustomerProfile models
from V33 CRM — no new tag tables needed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from services.customer_segmentation import (
    get_segment_counts, get_segment_telegram_ids,
    SEGMENT_DEFS, SEG_ALL,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from database import get_db_session
from database.models import (
    User, Order, OrderStatus, CustomerTag, CustomerTagAssignment,
    CustomerProfile,
)
from sqlalchemy import func
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# ── ConvHandler states ─────────────────────────────────────────────────────────
CSEG_NEW_TAG_NAME  = 9820
CSEG_NEW_TAG_COLOR = 9821
CSEG_SEARCH_QUERY  = 9822
CSEG_EDIT_TAG_NAME = 9823

_PAGE = 10


def _back(to: str = "cseg:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


async def _check(update: Update) -> bool:
    uid = update.effective_user.id
    if not has_permission(uid, "admin"):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


# ─── Segment helpers ──────────────────────────────────────────────────────────

def _get_segment_users_page(segment_key: str, page: int, per_page: int) -> dict:
    ids = get_segment_telegram_ids(segment_key)
    total = len(ids)
    start = (page - 1) * per_page
    page_ids = ids[start:start + per_page]
    users = []
    if page_ids:
        with get_db_session() as s:
            rows = s.query(User).filter(User.telegram_id.in_(page_ids)).all()
            id_map = {u.telegram_id: u for u in rows}
            for tid in page_ids:
                u = id_map.get(tid)
                if u:
                    users.append({
                        "id": u.id,
                        "telegram_id": u.telegram_id,
                        "username": u.username,
                        "wallet_balance": u.wallet_balance,
                        "created_at": u.created_at,
                    })
    return {
        "users": users,
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


def _get_auto_segment_breakdown() -> dict:
    """Compute automatic customer classifications based on behaviour."""
    now = datetime.utcnow()
    with get_db_session() as s:
        # High spenders (top 10% by total spend)
        spend_rows = (
            s.query(Order.user_id, func.sum(Order.total_amount).label("spend"))
            .filter(Order.status == OrderStatus.COMPLETED)
            .group_by(Order.user_id)
            .order_by(func.sum(Order.total_amount).desc())
            .all()
        )
        high_spenders = len(spend_rows) // 10 or 1
        high_spender_ids = {row.user_id for row in spend_rows[:high_spenders]}

        # Frequent buyers (>= 5 orders)
        freq_rows = (
            s.query(Order.user_id, func.count(Order.id).label("cnt"))
            .filter(Order.status == OrderStatus.COMPLETED)
            .group_by(Order.user_id)
            .having(func.count(Order.id) >= 5)
            .all()
        )
        frequent_buyers = len(freq_rows)

        # Inactive (no order in 60 days but has purchased)
        cutoff = now - timedelta(days=60)
        inactive_rows = (
            s.query(Order.user_id)
            .filter(Order.status == OrderStatus.COMPLETED)
            .group_by(Order.user_id)
            .having(func.max(Order.created_at) < cutoff)
            .all()
        )
        inactive_count = len(inactive_rows)

        # New this month
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        new_this_month = (
            s.query(User).filter(User.created_at >= month_start).count()
        )

        # Suspicious (banned)
        suspicious = s.query(User).filter_by(is_banned=True).count()

        return {
            "high_spenders": len(high_spender_ids),
            "frequent_buyers": frequent_buyers,
            "inactive": inactive_count,
            "new_this_month": new_this_month,
            "suspicious": suspicious,
        }


# ─── Main Menu ────────────────────────────────────────────────────────────────

async def cseg_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return

    counts = await asyncio.to_thread(get_segment_counts)
    lines = ["🎯 <b>CUSTOMER SEGMENTATION</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"]
    for key, label, desc in SEGMENT_DEFS:
        cnt = counts.get(key, 0)
        lines.append(f"  {label}: <b>{cnt}</b>")
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Segment Browser", callback_data="cseg:segments")],
        [InlineKeyboardButton("🏷 Tag Management",   callback_data="cseg:tags:1")],
        [InlineKeyboardButton("🤖 Auto-Segments",    callback_data="cseg:auto")],
        [InlineKeyboardButton("🔍 Search Users",     callback_data="cseg:search")],
        [_back("acc:root")],
    ])
    await _edit(update, text, kb)


# ─── Segment Browser ──────────────────────────────────────────────────────────

async def cseg_segments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    counts = await asyncio.to_thread(get_segment_counts)
    text = "📊 <b>Segment Browser</b>\n\nSelect a segment to browse users:"
    kb_rows = []
    for key, label, desc in SEGMENT_DEFS:
        cnt = counts.get(key, 0)
        kb_rows.append([InlineKeyboardButton(
            f"{label} ({cnt})", callback_data=f"cseg:seg_users:{key}:1")])
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def cseg_seg_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    parts = q.data.split(":")
    seg_key = parts[3]
    page = int(parts[4]) if len(parts) > 4 else 1

    data = await asyncio.to_thread(_get_segment_users_page, seg_key, page, _PAGE)
    users = data["users"]
    total = data["total"]
    pages = data["pages"]

    seg_label = next((l for k, l, _ in SEGMENT_DEFS if k == seg_key), seg_key)
    if not users:
        text = f"📊 <b>{seg_label}</b>\n\nNo users in this segment."
    else:
        lines = [f"📊 <b>{seg_label}</b> (page {page}/{pages}, {total} users)\n"]
        for u in users:
            uname = u["username"] or f"TG:{u['telegram_id']}"
            lines.append(f"• @{uname} — 💰${u['wallet_balance']:.2f}")
        text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"cseg:seg_users:{seg_key}:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"cseg:seg_users:{seg_key}:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("📢 Broadcast to Segment",
                                          callback_data=f"cseg:broadcast:{seg_key}")])
    kb_rows.append([_back("cseg:segments")])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def cseg_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redirect to broadcast center with segment pre-selected."""
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    parts = q.data.split(":")
    seg_key = parts[2] if len(parts) > 2 else SEG_ALL
    context.user_data["bc_segment"] = seg_key
    text = (
        f"📢 <b>Broadcast to Segment</b>\n\n"
        f"Segment <b>{seg_key}</b> pre-selected for targeting.\n\n"
        f"Go to Broadcast Center to compose and send."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Open Broadcast Center", callback_data="acc:root")],
        [_back("cseg:segments")],
    ])
    await _edit(update, text, kb)


# ─── Auto-Segments ────────────────────────────────────────────────────────────

async def cseg_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    data = await asyncio.to_thread(_get_auto_segment_breakdown)
    text = (
        "🤖 <b>Auto-Segment Analysis</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 High Spenders (top 10%): <b>{data['high_spenders']}</b>\n"
        f"🔁 Frequent Buyers (5+ orders): <b>{data['frequent_buyers']}</b>\n"
        f"😴 Inactive (60+ days): <b>{data['inactive']}</b>\n"
        f"🆕 New This Month: <b>{data['new_this_month']}</b>\n"
        f"🚫 Suspicious/Banned: <b>{data['suspicious']}</b>\n\n"
        "Auto-segments are computed live from order history."
    )
    kb = InlineKeyboardMarkup([[_back()]])
    await _edit(update, text, kb)


# ─── Tag Management ───────────────────────────────────────────────────────────

def _get_all_tags(page: int, per_page: int) -> dict:
    with get_db_session() as s:
        q = s.query(CustomerTag).order_by(CustomerTag.created_at.desc())
        total = q.count()
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        tags = []
        for tag in rows:
            cnt = s.query(CustomerTagAssignment).filter_by(tag_id=tag.id).count()
            tags.append({"id": tag.id, "name": tag.name, "color": tag.color,
                         "user_count": cnt, "created_at": tag.created_at})
        return {"tags": tags, "total": total, "page": page,
                "pages": max(1, (total + per_page - 1) // per_page)}


def _create_tag(name: str, color: str, admin_id: int) -> bool:
    try:
        with get_db_session() as s:
            existing = s.query(CustomerTag).filter_by(name=name).first()
            if existing:
                return False
            tag = CustomerTag(name=name, color=color, created_by=admin_id)
            s.add(tag)
            s.commit()
            return True
    except Exception:
        return False


def _delete_tag(tag_id: int) -> bool:
    try:
        with get_db_session() as s:
            tag = s.query(CustomerTag).get(tag_id)
            if not tag:
                return False
            s.delete(tag)
            s.commit()
            return True
    except Exception:
        return False


async def cseg_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    parts = q.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1
    data = await asyncio.to_thread(_get_all_tags, page, _PAGE)
    tags = data["tags"]
    pages = data["pages"]
    total = data["total"]

    if not tags:
        text = "🏷 <b>Tag Management</b>\n\nNo tags created yet."
    else:
        lines = [f"🏷 <b>Tag Management</b> (page {page}/{pages}, {total} tags)\n"]
        for t in tags:
            color = t["color"] or "⚪"
            lines.append(f"  {color} <b>{t['name']}</b> — {t['user_count']} users")
        text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"cseg:tags:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"cseg:tags:{page+1}"))

    kb_rows = [
        [InlineKeyboardButton(f"🗑 Delete: {t['name'][:15]}",
                               callback_data=f"cseg:del_tag:{t['id']}")]
        for t in tags[:5]
    ]
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("➕ New Tag", callback_data="cseg:new_tag")])
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def cseg_del_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    tag_id = int(q.data.split(":")[-1])
    ok = await asyncio.to_thread(_delete_tag, tag_id)
    if ok:
        await q.answer("✅ Tag deleted.", show_alert=True)
        admin_id = update.effective_user.id
        log_admin_action(admin_id, "delete_customer_tag",
                         target_type="customer_tag", target_id=tag_id,
                         module="customer_segmentation")
    else:
        await q.answer("❌ Tag not found or already deleted.", show_alert=True)
    # Refresh tag list
    await cseg_tags(with_data(update, "cseg:tags:1"), context)


# ─── ConvHandler: create new tag ─────────────────────────────────────────────

async def cseg_new_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return ConversationHandler.END
    await q.edit_message_text("🏷 <b>New Tag</b>\n\nEnter a name for the new tag:",
                              parse_mode="HTML")
    return CSEG_NEW_TAG_NAME


async def cseg_got_tag_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()[:64]
    context.user_data["cseg_new_tag_name"] = name
    await update.message.reply_text(
        f"🏷 Tag name: <b>{name}</b>\n\n"
        "Enter a color (emoji or hex, e.g. 🔵 or #3498db), or /skip:",
        parse_mode="HTML"
    )
    return CSEG_NEW_TAG_COLOR


async def cseg_got_tag_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    color = update.message.text.strip()
    if color.startswith("/skip"):
        color = "⚪"
    name = context.user_data.get("cseg_new_tag_name", "Unnamed")
    admin_id = update.effective_user.id
    ok = await asyncio.to_thread(_create_tag, name, color, admin_id)
    if ok:
        log_admin_action(admin_id, "create_customer_tag",
                         details=f"name={name}", module="customer_segmentation")
        await update.message.reply_text(f"✅ Tag <b>{name}</b> created!", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Tag <b>{name}</b> already exists.", parse_mode="HTML")
    return ConversationHandler.END


async def cseg_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── Search Users ─────────────────────────────────────────────────────────────

def _search_users_by_query(query: str) -> list[dict]:
    query_lower = query.lower().strip()
    with get_db_session() as s:
        from sqlalchemy import or_, func as sqlfunc, cast, String
        filters = [sqlfunc.lower(User.username).contains(query_lower)]
        if query.isdigit():
            filters.append(cast(User.telegram_id, String).contains(query))
        rows = (
            s.query(User)
            .filter(or_(*filters))
            .limit(20).all()
        )
        return [{"id": u.id, "telegram_id": u.telegram_id,
                 "username": u.username, "wallet_balance": u.wallet_balance}
                for u in rows]


async def cseg_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return ConversationHandler.END
    await q.edit_message_text("🔍 <b>User Search</b>\n\nEnter username or Telegram ID:",
                              parse_mode="HTML")
    return CSEG_SEARCH_QUERY


async def cseg_got_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.message.text.strip()
    results = await asyncio.to_thread(_search_users_by_query, query)
    if not results:
        await update.message.reply_text("🔍 No users found for that query.")
    else:
        lines = [f"🔍 <b>Search Results for '{query}'</b>\n"]
        for u in results:
            uname = u["username"] or f"TG:{u['telegram_id']}"
            lines.append(f"• @{uname} — ID:{u['telegram_id']} — 💰${u['wallet_balance']:.2f}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return ConversationHandler.END


# ─── Register ─────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    # New tag ConvHandler
    tag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cseg_new_tag, pattern=r"^cseg:new_tag$")],
        states={
            CSEG_NEW_TAG_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, cseg_got_tag_name)],
            CSEG_NEW_TAG_COLOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cseg_got_tag_color),
                CommandHandler("skip", cseg_got_tag_color),
            ],
        },
        fallbacks=[CommandHandler("cancel", cseg_conv_cancel)],
        per_message=False,
        allow_reentry=True,
        name="cseg_new_tag",
    )
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cseg_search_start, pattern=r"^cseg:search$")],
        states={
            CSEG_SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cseg_got_search)],
        },
        fallbacks=[CommandHandler("cancel", cseg_conv_cancel)],
        per_message=False,
        allow_reentry=True,
        name="cseg_search",
    )
    application.add_handler(tag_conv)
    application.add_handler(search_conv)
    application.add_handler(CallbackQueryHandler(cseg_menu,       pattern=r"^cseg:menu$"))
    application.add_handler(CallbackQueryHandler(cseg_segments,   pattern=r"^cseg:segments$"))
    application.add_handler(CallbackQueryHandler(cseg_seg_users,  pattern=r"^cseg:seg_users:"))
    application.add_handler(CallbackQueryHandler(cseg_broadcast,  pattern=r"^cseg:broadcast:"))
    application.add_handler(CallbackQueryHandler(cseg_auto,       pattern=r"^cseg:auto$"))
    application.add_handler(CallbackQueryHandler(cseg_tags,       pattern=r"^cseg:tags:"))
    application.add_handler(CallbackQueryHandler(cseg_del_tag,    pattern=r"^cseg:del_tag:"))
    logger.info("V45: Customer Segmentation admin handlers registered.")
