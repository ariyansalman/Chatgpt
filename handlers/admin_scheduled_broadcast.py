"""Admin Scheduled Broadcast — V26.

Complete broadcast manager: create, edit, duplicate, delete, preview,
send immediately, schedule (one-time / daily / weekly / monthly),
pause, resume, retry, statistics dashboard, and admin settings panel.

Supported media: text, photo, video, animation, document, voice, audio,
                 sticker, poll.

Audience targets: all, buyers, non_buyers, wallet_users, premium,
                  no_balance, no_orders, new_users, inactive, referred,
                  specific_ids, specific_language.

Callback namespace: ``asb:*``
Conversation state key in user_data: ``_asb``
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import get_db_session, User
from database.models import (
    ScheduledBroadcast, BroadcastStatus, BroadcastLog, BroadcastRetryQueue,
    Subscription,
)
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from config.settings import settings

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────
(
    ASB_TITLE,       # 0  — internal title
    ASB_MEDIA_TYPE,  # 1  — choose media type
    ASB_TEXT,        # 2  — message text / poll question
    ASB_MEDIA_FILE,  # 3  — upload photo / video / audio / etc.
    ASB_BUTTON_TEXT, # 4  — inline button label (optional)
    ASB_BUTTON_URL,  # 5  — inline button URL (optional)
    ASB_TARGET,      # 6  — audience segment
    ASB_SCHEDULE,    # 7  — when to send
    ASB_CONFIRM,     # 8  — final confirm
) = range(9)

# Extra states for V26
ASB_TARGET_EXTRA   = 9   # specific_ids or specific_language input
ASB_POLL_OPTIONS   = 10  # poll options (one per line)
ASB_SETTINGS_INPUT = 11  # free-text input for a settings value
# Enterprise Broadcast Center (V44)
ASB_CUSTOM_INTERVAL = 12  # custom recurring interval in hours

PAGE_SIZE = 8
_NS = "asb"

# ── Timezone choices (common) ──────────────────────────────────────────────
COMMON_TIMEZONES = [
    ("UTC",            "🌍 UTC"),
    ("US/Eastern",     "🇺🇸 US/Eastern"),
    ("US/Pacific",     "🇺🇸 US/Pacific"),
    ("Europe/London",  "🇬🇧 London"),
    ("Europe/Berlin",  "🇩🇪 Berlin"),
    ("Asia/Kolkata",   "🇮🇳 India"),
    ("Asia/Dubai",     "🇦🇪 Dubai"),
    ("Asia/Dhaka",     "🇧🇩 Dhaka"),
    ("Asia/Jakarta",   "🇮🇩 Jakarta"),
    ("Asia/Singapore", "🇸🇬 Singapore"),
    ("Asia/Tokyo",     "🇯🇵 Tokyo"),
    ("Asia/Seoul",     "🇰🇷 Seoul"),
    ("Asia/Shanghai",  "🇨🇳 Shanghai"),
    ("America/Sao_Paulo", "🇧🇷 São Paulo"),
    ("Africa/Cairo",   "🇪🇬 Cairo"),
]

# ── Target segment definitions ─────────────────────────────────────────────
TARGET_DEFS: List[Tuple[str, str, str]] = [
    ("all",              "👥 Everyone",                "All non-banned users"),
    ("buyers",           "🛍 Buyers",                  "Users who placed at least one order"),
    ("non_buyers",       "👤 Non-Buyers",              "Users who never placed an order"),
    ("wallet_users",     "💰 Wallet Balance",          "Users with wallet balance > 0"),
    ("no_balance",       "🪙 No Balance",              "Users with zero wallet balance"),
    ("premium",          "⭐ Premium/Subscribers",     "Users with active subscriptions"),
    ("no_orders",        "🆕 No Orders",               "Users who never ordered"),
    ("new_users",        "🌱 New Users (7d)",          "Registered within the last 7 days"),
    ("inactive",         "😴 Inactive (30d+)",         "Last seen 30+ days ago or never"),
    ("referred",         "🤝 Referred Users",          "Users who joined via referral"),
    ("specific_ids",     "🎯 Specific User IDs",       "Comma-separated Telegram IDs"),
    ("specific_language","🌐 Specific Language",       "Users with selected language"),
]

TARGET_LABELS = {k: v for k, v, _ in TARGET_DEFS}

# ── Media type definitions ─────────────────────────────────────────────────
MEDIA_TYPES = [
    ("text",      "📝 Text only"),
    ("photo",     "🖼 Photo"),
    ("video",     "🎥 Video"),
    ("animation", "🎞 Animation/GIF"),
    ("document",  "📄 Document"),
    ("voice",     "🎤 Voice"),
    ("audio",     "🎵 Audio"),
    ("sticker",   "🎭 Sticker"),
    ("poll",      "📊 Poll"),
]

STATUS_ICONS = {
    "draft":     "📝",
    "scheduled": "⏰",
    "sending":   "📤",
    "sent":      "✅",
    "cancelled": "❌",
    "paused":    "⏸",
    "failed":    "🔴",
}


# ── Auth / feature helpers ─────────────────────────────────────────────────

def _enabled() -> bool:
    status = cfg.get("scheduled_broadcast_status", "enabled")
    return status == "enabled"


def _maintenance() -> bool:
    status = cfg.get("scheduled_broadcast_status", "enabled")
    return status == "maintenance"


def _is_admin(uid: int) -> bool:
    return (uid == settings.ADMIN_TELEGRAM_ID
            or has_permission(uid, "manage_broadcasts"))


# ── Keyboard helpers ───────────────────────────────────────────────────────

def _back_kb(data: str = "asb:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Target audience query ──────────────────────────────────────────────────

def _get_target_users(target: str,
                      target_user_ids: Optional[str] = None,
                      target_language: Optional[str] = None) -> List[int]:
    """Return a list of eligible Telegram IDs for the given segment."""
    with get_db_session() as s:
        q = s.query(User.telegram_id).filter(User.is_banned == False)  # noqa: E712

        if target == "buyers":
            q = q.filter(User.has_purchased == True)  # noqa: E712
        elif target == "non_buyers":
            q = q.filter(User.has_purchased == False)  # noqa: E712
        elif target == "wallet_users":
            q = q.filter(User.wallet_balance > 0)
        elif target == "no_balance":
            q = q.filter((User.wallet_balance == 0) | (User.wallet_balance == None))  # noqa: E711
        elif target == "premium":
            # Users with at least one active subscription
            active_sub_ids = (
                s.query(Subscription.user_id)
                .filter(Subscription.status == "active")
                .distinct()
                .subquery()
            )
            q = q.filter(User.id.in_(active_sub_ids))
        elif target == "no_orders":
            from database.models import Order
            ordered_user_ids = (
                s.query(Order.user_id).distinct().subquery()
            )
            q = q.filter(~User.id.in_(ordered_user_ids))
        elif target == "new_users":
            cutoff = datetime.utcnow() - timedelta(days=7)
            q = q.filter(User.created_at >= cutoff)
        elif target == "inactive":
            cutoff = datetime.utcnow() - timedelta(days=30)
            q = q.filter(
                (User.last_seen_at == None) | (User.last_seen_at <= cutoff)  # noqa: E711
            )
        elif target == "referred":
            q = q.filter(User.referred_by_id != None)  # noqa: E711
        elif target == "specific_ids" and target_user_ids:
            try:
                ids = [int(x.strip()) for x in target_user_ids.split(",") if x.strip().isdigit()]
            except Exception:
                ids = []
            q = q.filter(User.telegram_id.in_(ids))
        elif target == "specific_language" and target_language:
            q = q.filter(User.language == target_language)

        return [r[0] for r in q.all()]


def _count_target(target: str,
                  target_user_ids: Optional[str] = None,
                  target_language: Optional[str] = None) -> int:
    return len(_get_target_users(target, target_user_ids, target_language))


# ── Send a single message to one recipient ─────────────────────────────────

async def _send_one(bot, tgid: int, br: ScheduledBroadcast, msg_kb) -> str:
    """Send to one user. Returns: 'delivered' | 'blocked' | 'failed'."""
    try:
        mtype = br.media_type
        pm = br.parse_mode or "HTML"
        silent = br.disable_notification or False

        if mtype == "text":
            await bot.send_message(
                tgid, br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "photo" and br.file_id:
            await bot.send_photo(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "video" and br.file_id:
            await bot.send_video(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "animation" and br.file_id:
            await bot.send_animation(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "document" and br.file_id:
            await bot.send_document(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "voice" and br.file_id:
            await bot.send_voice(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                disable_notification=silent)
        elif mtype == "audio" and br.file_id:
            await bot.send_audio(
                tgid, br.file_id, caption=br.message_text, parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "sticker" and br.file_id:
            await bot.send_sticker(tgid, br.file_id, disable_notification=silent)
        elif mtype == "poll":
            # message_text is JSON: {"question": "...", "options": ["a", "b", ...]}
            try:
                poll_data = json.loads(br.message_text or "{}")
                question = poll_data.get("question", "Poll")
                raw_options = poll_data.get("options", ["Option 1", "Option 2"])
                options = [str(o) for o in raw_options[:10]]
            except Exception:
                question = br.message_text or "Poll"
                options = ["Yes", "No"]
            await bot.send_poll(
                tgid, question, options,
                is_anonymous=True, disable_notification=silent)
        else:
            # Fallback: text only
            await bot.send_message(
                tgid, br.message_text or "(empty)", parse_mode=pm,
                reply_markup=msg_kb, disable_notification=silent)
        return "delivered"
    except Exception as e:
        err = str(e).lower()
        if "blocked" in err or "deactivated" in err or "chat not found" in err or "forbidden" in err:
            return "blocked"
        return "failed"


# ── Execute a broadcast send loop ──────────────────────────────────────────

async def _execute_broadcast(bid: int, context, query=None) -> Tuple[int, int, int, int, int]:
    """Core send loop. Returns (sent, delivered, failed, blocked, skipped)."""
    delay_sec = cfg.get_int("broadcast_delay_ms", 50) / 1000.0
    max_retries = cfg.get_int("broadcast_retry_count", 3)
    retry_enabled = cfg.get_bool("broadcast_retry_failed", True)

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            return 0, 0, 0, 0, 0
        # Snapshot all needed fields
        target        = br.target_segment
        tgt_ids       = br.target_user_ids
        tgt_lang      = br.target_language
        btn_text      = br.button_text
        btn_url       = br.button_url

    msg_kb = None
    if btn_text and btn_url:
        msg_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])

    users = _get_target_users(target, tgt_ids, tgt_lang)
    total = len(users)

    # Update total_recipients
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if br:
            br.total_recipients = total
            s.commit()

    sent = delivered = failed = blocked = skipped = 0
    failed_ids: List[Tuple[int, str]] = []

    for tgid in users:
        # Check if paused
        with get_db_session() as s:
            br = s.get(ScheduledBroadcast, bid)
            if not br or br.status not in ("sending",):
                skipped += (total - sent)
                break
            # Re-snapshot fields for each send to allow edits mid-flight
            br_snap = br

        result = await _send_one(context.bot, tgid, br_snap, msg_kb)
        sent += 1
        if result == "delivered":
            delivered += 1
        elif result == "blocked":
            blocked += 1
        elif result == "failed":
            failed += 1
            failed_ids.append((tgid, "send error"))

        # Rate limiting
        import asyncio
        await asyncio.sleep(max(0.03, delay_sec))

    # Queue failed for retry
    if retry_enabled and failed_ids:
        retry_after = datetime.utcnow() + timedelta(minutes=5)
        with get_db_session() as s:
            for tgid, err in failed_ids:
                s.add(BroadcastRetryQueue(
                    broadcast_id=bid,
                    telegram_id=tgid,
                    error_msg=err,
                    retry_at=retry_after,
                    attempts=0,
                    status="pending",
                    created_at=datetime.utcnow(),
                ))
            s.commit()

    return sent, delivered, failed, blocked, skipped


# ── Main menu ──────────────────────────────────────────────────────────────

async def asb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        status = cfg.get("scheduled_broadcast_status", "enabled")
        if status == "maintenance":
            await _safe_edit(query,
                "📨 <b>Scheduled Broadcast</b>\n\n"
                "🟡 <b>Maintenance Mode</b> — broadcasts are paused for maintenance.",
                _back_kb("acc:root"))
        else:
            await _safe_edit(query,
                "📨 <b>Scheduled Broadcast</b>\n\n❌ Feature is disabled.",
                _back_kb("acc:root"))
        return
    await _render_list(update, context, 0)


async def _render_list(update, context, page: int):
    query = update.callback_query
    with get_db_session() as s:
        q = s.query(ScheduledBroadcast).order_by(ScheduledBroadcast.created_at.desc())
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [
            (r.id, r.title, r.status, r.scheduled_at, r.sent_count, r.total_recipients)
            for r in rows
        ]

    # Dashboard mini-stats
    with get_db_session() as s:
        all_bc = s.query(ScheduledBroadcast).count()
        sched  = s.query(ScheduledBroadcast).filter_by(status="scheduled").count()
        sent   = s.query(ScheduledBroadcast).filter_by(status="sent").count()
        failed = s.query(ScheduledBroadcast).filter_by(status="failed").count()
        paused = s.query(ScheduledBroadcast).filter_by(status="paused").count()

    lines = [
        "📨 <b>Broadcast Manager</b>\n",
        f"📊 Total: {all_bc}  |  ⏰ Scheduled: {sched}  |  ✅ Sent: {sent}  "
        f"|  ⏸ Paused: {paused}  |  🔴 Failed: {failed}\n",
    ]
    kb = []
    for bid, title, status, sched_dt, sent_c, total_r in items:
        icon = STATUS_ICONS.get(status, "📄")
        sched_str = sched_dt.strftime("%m/%d %H:%M") if sched_dt else "—"
        lines.append(f"{icon} <b>{title}</b>  [{status}]  sched:{sched_str}  sent:{sent_c}/{total_r or '?'}")
        kb.append([InlineKeyboardButton(f"{icon} {title[:28]} [{status}]",
                                         callback_data=f"asb:view:{bid}")])

    if not items:
        lines.append("No broadcasts yet. Create your first broadcast below.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"asb:list:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"asb:list:{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([
        InlineKeyboardButton("➕ New Broadcast", callback_data="asb:new"),
        InlineKeyboardButton("📊 Stats", callback_data="asb:stats"),
    ])
    kb.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="asb:settings"),
        InlineKeyboardButton("🔙 Back", callback_data="acc:root"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def asb_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0
    await _render_list(update, context, page)


# ── View single broadcast ──────────────────────────────────────────────────

async def asb_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        bid = int(_override) if _override else int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return await _render_list(update, context, 0)

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await query.answer("❌ Not found.", show_alert=True)
            return
        title        = br.title
        status       = br.status
        media_type   = br.media_type
        msg_text     = (br.message_text or "")[:300]
        target       = br.target_segment
        tgt_lang     = br.target_language
        sched        = br.scheduled_at
        sent_at      = br.sent_at
        started_at   = br.started_at
        finished_at  = br.finished_at
        sent_count   = br.sent_count
        delivered    = br.delivered_count
        failed       = br.failed_count
        blocked      = br.blocked_count
        skipped      = br.skipped_count
        total_r      = br.total_recipients
        btn_text     = br.button_text or ""
        btn_url      = br.button_url or ""
        recurring    = br.is_recurring
        recur_type   = br.recurrence_type or ""
        timezone     = br.timezone or "UTC"
        retry_count  = br.retry_count
        max_ret      = br.max_retries
        parse_mode   = br.parse_mode or "HTML"
        disable_notif = br.disable_notification

    icon       = STATUS_ICONS.get(status, "📄")
    sched_str  = sched.strftime("%Y-%m-%d %H:%M") + f" {timezone}" if sched else "Not scheduled"
    sent_str   = sent_at.strftime("%Y-%m-%d %H:%M UTC") if sent_at else "—"
    start_str  = started_at.strftime("%Y-%m-%d %H:%M UTC") if started_at else "—"
    finish_str = finished_at.strftime("%Y-%m-%d %H:%M UTC") if finished_at else "—"

    # Estimate time remaining if sending
    eta_str = ""
    if status == "sending" and total_r and sent_count < total_r:
        delay_ms = cfg.get_int("broadcast_delay_ms", 50)
        remaining = total_r - sent_count
        eta_sec = (remaining * max(30, delay_ms)) / 1000
        eta_str = f"\n⏱ <b>ETA:</b> ~{int(eta_sec)}s  |  Remaining: {remaining}"

    target_label = TARGET_LABELS.get(target, target)
    if target == "specific_language" and tgt_lang:
        target_label += f" ({tgt_lang})"

    delivery_rate = (delivered / sent_count * 100) if sent_count else 0

    text = (
        f"{icon} <b>{title}</b>  [id {bid}]\n\n"
        f"<b>Status:</b> {status}  |  <b>Media:</b> {media_type}  |  <b>Parse:</b> {parse_mode}\n"
        f"<b>Target:</b> {target_label}\n"
        f"<b>Recurring:</b> {'✅ ' + recur_type if recurring else '❌ one-time'}\n"
        f"<b>Schedule:</b> {sched_str}\n"
        f"<b>Started:</b> {start_str}  |  <b>Finished:</b> {finish_str}\n"
        f"<b>Sent at:</b> {sent_str}\n\n"
        f"<b>📊 Delivery Stats</b>\n"
        f"Total: {total_r}  |  Sent: {sent_count}  |  ✅ Delivered: {delivered}\n"
        f"❌ Failed: {failed}  |  🚫 Blocked: {blocked}  |  ⏭ Skipped: {skipped}\n"
        f"📈 Rate: {delivery_rate:.1f}%  |  Retries: {retry_count}/{max_ret}"
        f"{eta_str}\n\n"
        f"<b>🔕 Silent:</b> {'Yes' if disable_notif else 'No'}\n"
    )
    if btn_text:
        text += f"\n<b>Button:</b> [{btn_text}]({btn_url})"
    text += f"\n\n<b>Preview:</b>\n{msg_text}{'…' if len(br.message_text or '') > 300 else ''}"

    kb = []
    if status in ("draft", "scheduled"):
        kb.append([
            InlineKeyboardButton("✏️ Edit", callback_data=f"asb:edit:{bid}"),
            InlineKeyboardButton("📋 Duplicate", callback_data=f"asb:dup:{bid}"),
        ])
        kb.append([
            InlineKeyboardButton("👁 Preview", callback_data=f"asb:preview:{bid}"),
            InlineKeyboardButton("🧪 Test Send", callback_data=f"asb:test_send:{bid}"),
        ])
        if status == "draft":
            kb.append([InlineKeyboardButton("📤 Send Now", callback_data=f"asb:send:{bid}")])
        if status == "scheduled":
            kb.append([InlineKeyboardButton("❌ Cancel Schedule", callback_data=f"asb:cancel:{bid}")])
    elif status == "sending":
        kb.append([
            InlineKeyboardButton("⏸ Pause", callback_data=f"asb:pause:{bid}"),
            InlineKeyboardButton("📋 Duplicate", callback_data=f"asb:dup:{bid}"),
        ])
    elif status == "paused":
        kb.append([
            InlineKeyboardButton("▶️ Resume", callback_data=f"asb:resume:{bid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"asb:cancel:{bid}"),
        ])
        kb.append([InlineKeyboardButton("📋 Duplicate", callback_data=f"asb:dup:{bid}")])
    elif status in ("sent", "failed"):
        kb.append([
            InlineKeyboardButton("📋 Duplicate", callback_data=f"asb:dup:{bid}"),
            InlineKeyboardButton("🔄 Retry Failed", callback_data=f"asb:retry:{bid}"),
        ])
    elif status == "cancelled":
        kb.append([InlineKeyboardButton("📋 Duplicate", callback_data=f"asb:dup:{bid}")])

    kb.append([InlineKeyboardButton("📋 View Logs", callback_data=f"asb:logs:{bid}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"asb:del_ask:{bid}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="asb:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


# ── Create broadcast conversation ──────────────────────────────────────────

async def asb_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not _enabled():
        await query.answer("Feature disabled or in maintenance.", show_alert=True)
        return ConversationHandler.END
    context.user_data["_asb"] = {}
    await _safe_edit(query,
        "📨 <b>New Broadcast — Step 1/8</b>\n\n"
        "Send a short <b>title</b> for this broadcast (internal reference, max 100 chars):",
        _back_kb())
    return ASB_TITLE


async def asb_receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip()[:100]
    if not title:
        await update.message.reply_text("❌ Title cannot be empty. Send again:")
        return ASB_TITLE
    context.user_data["_asb"]["title"] = title
    # Build media type picker (2-column)
    rows = []
    for i in range(0, len(MEDIA_TYPES), 2):
        row = []
        for mtype, mlabel in MEDIA_TYPES[i:i+2]:
            row.append(InlineKeyboardButton(mlabel, callback_data=f"asb:mtype:{mtype}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="asb:menu")])
    await update.message.reply_text(
        "📨 <b>Step 2/8 — Media Type</b>\n\nChoose the content type for this broadcast:",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
    return ASB_MEDIA_TYPE


async def asb_receive_media_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mtype = query.data.split(":")[2]
    context.user_data["_asb"]["media_type"] = mtype

    if mtype == "poll":
        await _safe_edit(query,
            "📨 <b>Step 3/8 — Poll Question</b>\n\n"
            "Send the <b>poll question</b> text:",
            _back_kb())
    else:
        await _safe_edit(query,
            "📨 <b>Step 3/8 — Message Text</b>\n\n"
            "Send the broadcast message text (supports HTML formatting).\n"
            "For photo/video/audio/document: this becomes the caption.",
            _back_kb())
    return ASB_TEXT


async def asb_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Message cannot be empty. Send again:")
        return ASB_TEXT
    mtype = context.user_data["_asb"].get("media_type", "text")

    if mtype == "poll":
        # Store question temporarily
        context.user_data["_asb"]["poll_question"] = text
        await update.message.reply_text(
            "📨 <b>Step 4/8 — Poll Options</b>\n\n"
            "Send the poll options, <b>one per line</b> (2–10 options):\n\n"
            "Example:\n<code>Yes\nNo\nMaybe</code>",
            parse_mode="HTML")
        return ASB_POLL_OPTIONS

    context.user_data["_asb"]["message_text"] = text

    if mtype == "text":
        context.user_data["_asb"]["file_id"] = None
        return await _ask_button(update.message)

    await update.message.reply_text(
        f"📨 <b>Step 4/8 — Upload Media</b>\n\n"
        f"Send the <b>{mtype}</b> file for this broadcast:",
        parse_mode="HTML")
    return ASB_MEDIA_FILE


async def asb_receive_poll_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    options = [o.strip() for o in raw.splitlines() if o.strip()]
    if len(options) < 2:
        await update.message.reply_text("❌ Provide at least 2 options, one per line:")
        return ASB_POLL_OPTIONS
    if len(options) > 10:
        await update.message.reply_text("❌ Maximum 10 options. Trim your list:")
        return ASB_POLL_OPTIONS
    # Encode as JSON in message_text
    question = context.user_data["_asb"].pop("poll_question", "Poll")
    poll_json = json.dumps({"question": question, "options": options[:10]}, ensure_ascii=False)
    context.user_data["_asb"]["message_text"] = poll_json
    context.user_data["_asb"]["file_id"] = None
    return await _ask_button(update.message)


async def asb_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.video:
        file_id = update.message.video.file_id
    elif update.message.animation:
        file_id = update.message.animation.file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    elif update.message.voice:
        file_id = update.message.voice.file_id
    elif update.message.audio:
        file_id = update.message.audio.file_id
    elif update.message.sticker:
        file_id = update.message.sticker.file_id
    if not file_id:
        await update.message.reply_text("❌ No media detected. Send the file again:")
        return ASB_MEDIA_FILE
    context.user_data["_asb"]["file_id"] = file_id
    return await _ask_button(update.message)


async def _ask_button(message_obj):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip button", callback_data="asb:btn:skip")],
        [InlineKeyboardButton("➕ Add inline button", callback_data="asb:btn:add")],
    ])
    await message_obj.reply_text(
        "📨 <b>Step 5/8 — Inline Button (optional)</b>\n\n"
        "Add an optional inline button to the broadcast:",
        reply_markup=kb, parse_mode="HTML")
    return ASB_BUTTON_TEXT


async def asb_button_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[2]  # skip | add
    if choice == "skip":
        context.user_data["_asb"]["button_text"] = None
        context.user_data["_asb"]["button_url"] = None
        return await _ask_target_cb(query)
    await _safe_edit(query,
        "Send the button <b>label text</b> (e.g. <code>🛒 Shop Now</code>):",
        parse_mode="HTML")
    return ASB_BUTTON_TEXT


async def asb_receive_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only processes text messages (not callback queries)
    if update.callback_query:
        return ASB_BUTTON_TEXT
    txt = (update.message.text or "").strip()
    if not txt:
        context.user_data["_asb"]["button_text"] = None
        context.user_data["_asb"]["button_url"] = None
        await update.message.reply_text(
            "Skipping button. Moving to audience selection…", parse_mode="HTML")
        return await _ask_target_msg(update.message)
    context.user_data["_asb"]["button_text"] = txt[:64]
    await update.message.reply_text(
        "Send the <b>button URL</b>:", parse_mode="HTML")
    return ASB_BUTTON_URL


async def asb_receive_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    context.user_data["_asb"]["button_url"] = url[:512]
    return await _ask_target_msg(update.message)


async def _ask_target_cb(query):
    await _safe_edit(query, _target_prompt(), _target_kb())
    return ASB_TARGET


async def _ask_target_msg(message_obj):
    await message_obj.reply_text(
        _target_prompt(), reply_markup=_target_kb(), parse_mode="HTML")
    return ASB_TARGET


def _target_prompt() -> str:
    return "📨 <b>Step 6/8 — Target Audience</b>\n\nWho should receive this broadcast?"


def _target_kb() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(TARGET_DEFS), 2):
        row = []
        for key, label, _ in TARGET_DEFS[i:i+2]:
            row.append(InlineKeyboardButton(label, callback_data=f"asb:tgt:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="asb:menu")])
    return InlineKeyboardMarkup(rows)


async def asb_receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tgt = query.data.split(":")[2]
    context.user_data["_asb"]["target_segment"] = tgt

    if tgt == "specific_ids":
        await _safe_edit(query,
            "📨 Send a comma-separated list of Telegram User IDs:\n\n"
            "Example: <code>123456789, 987654321</code>",
            parse_mode="HTML")
        return ASB_TARGET_EXTRA

    if tgt == "specific_language":
        await _safe_edit(query,
            "📨 Send the 2-letter language code (e.g. <code>en</code>, <code>ar</code>, "
            "<code>ru</code>, <code>zh</code>, <code>bn</code>, <code>vi</code>):",
            parse_mode="HTML")
        return ASB_TARGET_EXTRA

    context.user_data["_asb"]["target_user_ids"] = None
    context.user_data["_asb"]["target_language"] = None
    return await _ask_schedule_cb(query)


async def asb_receive_target_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle specific_ids or specific_language text input."""
    text = (update.message.text or "").strip()
    tgt = context.user_data["_asb"].get("target_segment", "all")
    if tgt == "specific_ids":
        context.user_data["_asb"]["target_user_ids"] = text
        context.user_data["_asb"]["target_language"] = None
    elif tgt == "specific_language":
        context.user_data["_asb"]["target_language"] = text[:8].lower()
        context.user_data["_asb"]["target_user_ids"] = None
    return await _ask_schedule_msg(update.message)


async def _ask_schedule_cb(query):
    await _safe_edit(query, _schedule_prompt(), _schedule_kb())
    return ASB_SCHEDULE


async def _ask_schedule_msg(message_obj):
    await message_obj.reply_text(
        _schedule_prompt(), reply_markup=_schedule_kb(), parse_mode="HTML")
    return ASB_SCHEDULE


def _schedule_prompt() -> str:
    return (
        "📨 <b>Step 7/8 — Schedule</b>\n\n"
        "When should this broadcast be sent?"
    )


def _schedule_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Send Immediately",      callback_data="asb:sched:now")],
        [InlineKeyboardButton("⏰ One-Time (custom date)", callback_data="asb:sched:custom")],
        [
            InlineKeyboardButton("🔁 Daily",              callback_data="asb:sched:daily"),
            InlineKeyboardButton("📅 Weekly",             callback_data="asb:sched:weekly"),
        ],
        [
            InlineKeyboardButton("🗓 Monthly",            callback_data="asb:sched:monthly"),
            InlineKeyboardButton("⏱ Custom Interval",    callback_data="asb:sched:custom_interval"),
        ],
        [InlineKeyboardButton("❌ Cancel",                callback_data="asb:menu")],
    ])


async def asb_receive_schedule_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[2]
    data = context.user_data["_asb"]

    if choice == "now":
        data["scheduled_at"] = None
        data["is_recurring"] = False
        data["recurrence_type"] = None
        return await _show_confirm_cb(query, data)

    if choice in ("daily", "weekly", "monthly"):
        data["scheduled_at"] = datetime.utcnow()
        data["is_recurring"] = True
        data["recurrence_type"] = choice
        data.pop("custom_interval_hours", None)
        return await _show_confirm_cb(query, data)

    if choice == "custom_interval":
        await _safe_edit(query,
            "📨 <b>Custom Interval Schedule</b>\n\n"
            "Send the <b>number of hours</b> between each recurring send.\n\n"
            "Examples: <code>6</code> (every 6 hours)  "
            "<code>48</code> (every 2 days)  "
            "<code>168</code> (every week)\n\n"
            "Minimum: 1 hour  Maximum: 8760 hours (≈1 year)",
            parse_mode="HTML")
        data["_sched_type"] = "custom_interval"
        return ASB_CUSTOM_INTERVAL

    # custom one-time date
    await _safe_edit(query,
        "📨 <b>One-Time Schedule</b>\n\n"
        "Send the date/time as: <code>YYYY-MM-DD HH:MM</code> (UTC)\n\n"
        "Example: <code>2026-09-01 14:30</code>",
        parse_mode="HTML")
    data["_sched_type"] = "custom"
    return ASB_SCHEDULE


async def asb_receive_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    data = context.user_data.get("_asb", {})
    if not data.get("_sched_type"):
        return ASB_SCHEDULE
    try:
        dt = datetime.strptime(txt, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Use: <code>YYYY-MM-DD HH:MM</code>",
            parse_mode="HTML")
        return ASB_SCHEDULE
    data["scheduled_at"] = dt
    data["is_recurring"] = False
    data["recurrence_type"] = None
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data="asb:confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="asb:menu")],
    ])
    await update.message.reply_text(
        _build_confirm_text(data), reply_markup=kb, parse_mode="HTML")
    return ASB_CONFIRM


def _build_confirm_text(data: dict) -> str:
    sched = data.get("scheduled_at")
    tz    = data.get("timezone", "UTC")
    sched_str = sched.strftime("%Y-%m-%d %H:%M") + f" {tz}" if sched else "Immediately"
    mtype = data.get("media_type", "text")
    tgt   = data.get("target_segment", "all")
    tgt_label = TARGET_LABELS.get(tgt, tgt)
    if tgt == "specific_language" and data.get("target_language"):
        tgt_label += f" ({data['target_language']})"
    elif tgt == "specific_ids" and data.get("target_user_ids"):
        ids = data["target_user_ids"]
        cnt = len([x for x in ids.split(",") if x.strip()])
        tgt_label += f" ({cnt} IDs)"
    msg_preview = data.get("message_text") or ""
    if mtype == "poll":
        try:
            poll_data = json.loads(msg_preview)
            msg_preview = f"[Poll] {poll_data.get('question', '')} ({len(poll_data.get('options', []))} options)"
        except Exception:
            pass
    # Recurring / interval display
    recur_type = data.get("recurrence_type", "")
    if data.get("is_recurring"):
        if recur_type == "custom":
            hours = data.get("custom_interval_hours", 24)
            recur_display = f"✅ Every {hours}h (custom interval)"
        else:
            recur_display = f"✅ {recur_type}"
    else:
        recur_display = "❌ one-time"
    return (
        "📨 <b>Confirm Broadcast</b>\n\n"
        f"<b>Title:</b> {data.get('title', '—')}\n"
        f"<b>Media:</b> {mtype}\n"
        f"<b>Target:</b> {tgt_label}\n"
        f"<b>Schedule:</b> {sched_str}\n"
        f"<b>Recurring:</b> {recur_display}\n"
        f"<b>Button:</b> {data.get('button_text') or '—'}\n\n"
        f"<b>Message preview:</b>\n{msg_preview[:200]}"
    )


async def _show_confirm_cb(query, data: dict):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data="asb:confirm"),
         InlineKeyboardButton("💾 Save Draft", callback_data="asb:save_draft"),
         InlineKeyboardButton("❌ Cancel", callback_data="asb:menu")],
    ])
    await _safe_edit(query, _build_confirm_text(data), kb)
    return ASB_CONFIRM


async def asb_confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _persist_broadcast(query, context, draft=False)


async def asb_save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _persist_broadcast(query, context, draft=True)


async def _persist_broadcast(query, context, draft: bool = False):
    data = context.user_data.get("_asb", {})
    sched_at = data.get("scheduled_at")
    edit_bid = data.get("_edit_bid")  # non-None when editing existing

    if draft:
        status = "draft"
    elif sched_at:
        status = "scheduled"
    else:
        status = "draft"  # "send now" → will be sent immediately after save

    with get_db_session() as s:
        if edit_bid:
            br = s.get(ScheduledBroadcast, edit_bid)
            if not br:
                await query.answer("❌ Broadcast not found.", show_alert=True)
                return ConversationHandler.END
            br.title                = data.get("title", br.title)
            br.message_text         = data.get("message_text", br.message_text)
            br.media_type           = data.get("media_type", br.media_type)
            br.file_id              = data.get("file_id", br.file_id)
            br.target_segment       = data.get("target_segment", br.target_segment)
            br.target_user_ids      = data.get("target_user_ids", br.target_user_ids)
            br.target_language      = data.get("target_language", br.target_language)
            br.scheduled_at         = sched_at
            br.is_recurring         = data.get("is_recurring", br.is_recurring)
            br.recurrence_type      = data.get("recurrence_type", br.recurrence_type)
            br.custom_interval_hours = data.get("custom_interval_hours", br.custom_interval_hours)
            br.button_text          = data.get("button_text", br.button_text)
            br.button_url           = data.get("button_url", br.button_url)
            br.status               = status
            br.updated_at           = datetime.utcnow()
            s.commit()
            bid = edit_bid
            action = "edit"
        else:
            br = ScheduledBroadcast(
                title                = data.get("title", "Untitled"),
                message_text         = data.get("message_text", ""),
                media_type           = data.get("media_type", "text"),
                file_id              = data.get("file_id"),
                target_segment       = data.get("target_segment", "all"),
                target_user_ids      = data.get("target_user_ids"),
                target_language      = data.get("target_language"),
                scheduled_at         = sched_at,
                is_recurring         = data.get("is_recurring", False),
                recurrence_type      = data.get("recurrence_type"),
                custom_interval_hours = data.get("custom_interval_hours"),
                button_text          = data.get("button_text"),
                button_url           = data.get("button_url"),
                status               = status,
                created_by           = query.from_user.id,
                created_at           = datetime.utcnow(),
                updated_at           = datetime.utcnow(),
            )
            s.add(br)
            s.commit()
            bid = br.id
            action = "create"

    log_admin_action(query.from_user.id, f"scheduled_broadcast.{action}",
                     "scheduled_broadcast", bid,
                     f"title={data.get('title')} status={status}",
                     module="scheduled_broadcast")
    context.user_data.pop("_asb", None)

    emoji = "💾" if draft else "✅"
    await _safe_edit(query,
        f"{emoji} <b>Broadcast {'saved as draft' if draft else 'saved'}!</b>  (id {bid})\n\n"
        "Find it in the Scheduled Broadcasts list.",
        _back_kb("asb:menu"))

    # If "send now" (no schedule, not draft), trigger immediately
    if not draft and not sched_at and not edit_bid:
        context.user_data["_pending_send_bid"] = bid
    return ConversationHandler.END


# ── Inline actions ─────────────────────────────────────────────────────────

async def asb_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    await _trigger_send(bid, query, context)


async def _trigger_send(bid: int, query, context):
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br or br.status not in ("draft", "scheduled", "paused"):
            await query.answer("❌ Cannot send in current state.", show_alert=True)
            return
        br.status     = "sending"
        br.started_at = datetime.utcnow()
        s.commit()

    await _safe_edit(query,
        "📤 <b>Sending broadcast…</b>\n\n"
        "This may take a while depending on the audience size.\n"
        "Refresh the view page when done.",
        _back_kb("asb:menu"))

    try:
        sent, delivered, failed, blocked, skipped = await _execute_broadcast(bid, context, query)

        with get_db_session() as s:
            br = s.get(ScheduledBroadcast, bid)
            if br:
                br.status          = "sent"
                br.sent_at         = datetime.utcnow()
                br.finished_at     = datetime.utcnow()
                br.sent_count      = sent
                br.delivered_count = delivered
                br.failed_count    = failed
                br.blocked_count   = blocked
                br.skipped_count   = skipped
                s.add(BroadcastLog(
                    broadcast_id     = bid,
                    started_at       = br.started_at,
                    finished_at      = br.finished_at,
                    total_recipients = br.total_recipients,
                    sent             = sent,
                    delivered        = delivered,
                    failed           = failed,
                    blocked          = blocked,
                    skipped          = skipped,
                    created_at       = datetime.utcnow(),
                ))
                s.commit()

        log_admin_action(query.from_user.id, "scheduled_broadcast.send",
                         "scheduled_broadcast", bid,
                         f"sent={sent} delivered={delivered} failed={failed} blocked={blocked} skipped={skipped}",
                         module="scheduled_broadcast")

        await query.message.reply_text(
            f"✅ <b>Broadcast #{bid} sent!</b>\n\n"
            f"📤 Sent: {sent}  ✅ Delivered: {delivered}\n"
            f"❌ Failed: {failed}  🚫 Blocked: {blocked}  ⏭ Skipped: {skipped}",
            parse_mode="HTML",
            reply_markup=_back_kb("asb:menu"))
    except Exception as exc:
        logger.exception("asb_send_now: error sending broadcast #%d", bid)
        with get_db_session() as s:
            br = s.get(ScheduledBroadcast, bid)
            if br:
                br.status     = "failed"
                br.error_log  = str(exc)[:2000]
                br.finished_at = datetime.utcnow()
                s.commit()
        await query.message.reply_text(
            f"🔴 <b>Broadcast failed!</b> Error: {str(exc)[:200]}",
            parse_mode="HTML",
            reply_markup=_back_kb("asb:menu"))


async def asb_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if br and br.status in ("scheduled", "draft", "paused"):
            br.status     = "cancelled"
            br.finished_at = datetime.utcnow()
            s.commit()
    log_admin_action(update.effective_user.id, "scheduled_broadcast.cancel",
                     "scheduled_broadcast", bid, module="scheduled_broadcast")
    await _safe_edit(query, "❌ Broadcast cancelled.", _back_kb("asb:menu"))


async def asb_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if br and br.status in ("sending", "scheduled"):
            br.status    = "paused"
            br.is_paused = True
            s.commit()
    log_admin_action(update.effective_user.id, "scheduled_broadcast.pause",
                     "scheduled_broadcast", bid, module="scheduled_broadcast")
    await _safe_edit(query,
        f"⏸ Broadcast #{bid} paused.\n\nUse Resume to continue sending.",
        _back_kb("asb:menu"))


async def asb_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br or br.status != "paused":
            await query.answer("❌ Broadcast is not paused.", show_alert=True)
            return
        br.status    = "sending"
        br.is_paused = False
        s.commit()
    log_admin_action(update.effective_user.id, "scheduled_broadcast.resume",
                     "scheduled_broadcast", bid, module="scheduled_broadcast")
    await _trigger_send(bid, query, context)


async def asb_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry failed recipients from the retry queue."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await query.answer("Not found.", show_alert=True)
            return
        pending = (s.query(BroadcastRetryQueue)
                   .filter_by(broadcast_id=bid, status="pending")
                   .all())
        retry_ids = [(r.id, r.telegram_id) for r in pending]

    if not retry_ids:
        await _safe_edit(query, f"ℹ️ No pending retry items for broadcast #{bid}.",
                         _back_kb(f"asb:view:{bid}"))
        return

    await _safe_edit(query,
        f"🔄 Retrying {len(retry_ids)} failed recipients for broadcast #{bid}…",
        _back_kb("asb:menu"))

    sent = failed = blocked = 0
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        msg_kb = None
        if br and br.button_text and br.button_url:
            msg_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(br.button_text, url=br.button_url)]])
        br_snap = br

    for rq_id, tgid in retry_ids:
        result = await _send_one(context.bot, tgid, br_snap, msg_kb)
        if result == "delivered":
            sent += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1

        new_status = "sent" if result == "delivered" else (
            "failed" if result == "failed" else "failed")
        with get_db_session() as s:
            rq = s.get(BroadcastRetryQueue, rq_id)
            if rq:
                rq.status   = new_status
                rq.attempts = rq.attempts + 1
                s.commit()

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if br:
            br.delivered_count += sent
            br.failed_count     = max(0, br.failed_count - sent - blocked)
            br.blocked_count   += blocked
            br.retry_count     = (br.retry_count or 0) + 1
            s.commit()

    log_admin_action(update.effective_user.id, "scheduled_broadcast.retry",
                     "scheduled_broadcast", bid,
                     f"retried={len(retry_ids)} sent={sent} failed={failed} blocked={blocked}",
                     module="scheduled_broadcast")
    await query.message.reply_text(
        f"🔄 <b>Retry complete for broadcast #{bid}</b>\n\n"
        f"✅ Delivered: {sent}  ❌ Failed: {failed}  🚫 Blocked: {blocked}",
        parse_mode="HTML",
        reply_markup=_back_kb(f"asb:view:{bid}"))


async def asb_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"asb:del_ok:{bid}"),
         InlineKeyboardButton("🔙 Cancel", callback_data=f"asb:view:{bid}")],
    ])
    await _safe_edit(query, f"⚠️ Delete broadcast #{bid}? This cannot be undone.", kb)


async def asb_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if br:
            s.delete(br)
            s.commit()
    log_admin_action(update.effective_user.id, "scheduled_broadcast.delete",
                     "scheduled_broadcast", bid, module="scheduled_broadcast")
    await _safe_edit(query, f"🗑 Broadcast #{bid} deleted.", _back_kb("asb:menu"))


async def asb_duplicate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await query.answer("Not found.", show_alert=True)
            return
        new_br = ScheduledBroadcast(
            title           = f"Copy of {br.title}"[:100],
            message_text    = br.message_text,
            media_type      = br.media_type,
            file_id         = br.file_id,
            target_segment  = br.target_segment,
            target_user_ids = br.target_user_ids,
            target_language = br.target_language,
            button_text     = br.button_text,
            button_url      = br.button_url,
            parse_mode      = br.parse_mode,
            disable_notification = br.disable_notification,
            timezone        = br.timezone or "UTC",
            status          = "draft",
            created_by      = update.effective_user.id,
            created_at      = datetime.utcnow(),
            updated_at      = datetime.utcnow(),
        )
        s.add(new_br)
        s.commit()
        new_id = new_br.id
    log_admin_action(update.effective_user.id, "scheduled_broadcast.duplicate",
                     "scheduled_broadcast", new_id,
                     f"source={bid}", module="scheduled_broadcast")
    await query.answer(f"📋 Duplicated as #{new_id}.")
    context.user_data["_cb_data_override"] = str(new_id)
    await asb_view(update, context)


async def asb_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            return
        mtype     = br.media_type
        msg_text  = br.message_text or "(empty)"
        btn_text  = br.button_text
        btn_url   = br.button_url
        file_id   = br.file_id
        parse_mode = br.parse_mode or "HTML"

    kb_rows = []
    if btn_text and btn_url:
        kb_rows.append([InlineKeyboardButton(btn_text, url=btn_url)])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"asb:view:{bid}")])
    reply_kb = InlineKeyboardMarkup(kb_rows)

    try:
        header = f"👁 <b>Preview — Broadcast #{bid}</b>\n\n"
        if mtype == "text":
            await query.message.reply_text(
                header + msg_text, reply_markup=reply_kb, parse_mode="HTML")
        elif mtype == "photo" and file_id:
            await query.message.reply_photo(file_id, caption=msg_text[:1024],
                                             reply_markup=reply_kb, parse_mode=parse_mode)
        elif mtype == "video" and file_id:
            await query.message.reply_video(file_id, caption=msg_text[:1024],
                                             reply_markup=reply_kb, parse_mode=parse_mode)
        elif mtype == "animation" and file_id:
            await query.message.reply_animation(file_id, caption=msg_text[:1024],
                                                  reply_markup=reply_kb, parse_mode=parse_mode)
        elif mtype == "document" and file_id:
            await query.message.reply_document(file_id, caption=msg_text[:1024],
                                                reply_markup=reply_kb, parse_mode=parse_mode)
        elif mtype == "voice" and file_id:
            await query.message.reply_voice(file_id, caption=msg_text[:1024])
        elif mtype == "audio" and file_id:
            await query.message.reply_audio(file_id, caption=msg_text[:1024])
        elif mtype == "sticker" and file_id:
            await query.message.reply_sticker(file_id)
        elif mtype == "poll":
            try:
                poll_data = json.loads(msg_text)
                question = poll_data.get("question", "Poll")
                raw_options = poll_data.get("options", ["Option 1", "Option 2"])
                options = [str(o) for o in raw_options[:10]]
            except Exception:
                question = msg_text[:255]
                options = ["Yes", "No"]
            await query.message.reply_poll(question, options)
        else:
            await query.message.reply_text(
                header + msg_text, reply_markup=reply_kb, parse_mode="HTML")
    except Exception as e:
        logger.warning("asb_preview error: %s", e)


# ── Edit existing broadcast ────────────────────────────────────────────────

async def asb_edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Load existing broadcast into edit mode and restart the creation conv."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return ConversationHandler.END

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br or br.status not in ("draft", "scheduled"):
            await query.answer("❌ Cannot edit in current state.", show_alert=True)
            return ConversationHandler.END
        context.user_data["_asb"] = {
            "_edit_bid":       bid,
            "title":           br.title,
            "message_text":    br.message_text,
            "media_type":      br.media_type,
            "file_id":         br.file_id,
            "target_segment":  br.target_segment,
            "target_user_ids": br.target_user_ids,
            "target_language": br.target_language,
            "scheduled_at":    br.scheduled_at,
            "is_recurring":    br.is_recurring,
            "recurrence_type": br.recurrence_type,
            "button_text":     br.button_text,
            "button_url":      br.button_url,
            "timezone":        br.timezone or "UTC",
        }

    await _safe_edit(query,
        f"✏️ <b>Editing Broadcast #{bid}</b>\n\n"
        "Send a new <b>title</b> (or send the existing one to keep it):\n\n"
        f"Current: <code>{br.title}</code>",
        _back_kb(f"asb:view:{bid}"))
    return ASB_TITLE


# ── Logs ───────────────────────────────────────────────────────────────────

async def asb_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await query.answer("Not found.", show_alert=True)
            return
        br_title = br.title
        logs = (s.query(BroadcastLog)
                .filter_by(broadcast_id=bid)
                .order_by(BroadcastLog.created_at.desc())
                .limit(10)
                .all())
        log_rows = [
            (l.created_at, l.total_recipients, l.sent, l.delivered, l.failed, l.blocked, l.skipped)
            for l in logs
        ]
        retry_pending = (s.query(BroadcastRetryQueue)
                         .filter_by(broadcast_id=bid, status="pending")
                         .count())

    lines = [f"📋 <b>Logs for Broadcast #{bid}: {br_title}</b>\n"]
    if not log_rows:
        lines.append("No logs yet.")
    for created, total, sent, delivered, failed, blocked, skipped in log_rows:
        lines.append(
            f"🕐 {created.strftime('%m/%d %H:%M') if created else '—'}\n"
            f"   Total: {total}  Sent: {sent}  ✅{delivered}  ❌{failed}  🚫{blocked}  ⏭{skipped}\n"
        )
    lines.append(f"\n🔄 Pending retries: {retry_pending}")

    await _safe_edit(query, "\n".join(lines), _back_kb(f"asb:view:{bid}"))


# ── Statistics dashboard ───────────────────────────────────────────────────

async def asb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    now = datetime.utcnow()
    today_start  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start   = today_start - timedelta(days=7)
    month_start  = today_start - timedelta(days=30)

    with get_db_session() as s:
        total      = s.query(ScheduledBroadcast).count()
        scheduled  = s.query(ScheduledBroadcast).filter_by(status="scheduled").count()
        sending    = s.query(ScheduledBroadcast).filter_by(status="sending").count()
        paused     = s.query(ScheduledBroadcast).filter_by(status="paused").count()
        completed  = s.query(ScheduledBroadcast).filter_by(status="sent").count()
        failed     = s.query(ScheduledBroadcast).filter_by(status="failed").count()
        cancelled  = s.query(ScheduledBroadcast).filter_by(status="cancelled").count()
        drafts     = s.query(ScheduledBroadcast).filter_by(status="draft").count()

        today_bc   = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= today_start).count()
        week_bc    = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= week_start).count()
        month_bc   = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= month_start).count()

        # Aggregated delivery stats from broadcast_logs
        from sqlalchemy import func
        agg = s.query(
            func.sum(BroadcastLog.delivered),
            func.sum(BroadcastLog.failed),
            func.sum(BroadcastLog.sent),
        ).first()
        total_delivered = int(agg[0] or 0)
        total_failed    = int(agg[1] or 0)
        total_sent      = int(agg[2] or 0)

        retry_pending = s.query(BroadcastRetryQueue).filter_by(status="pending").count()

    delivery_rate = (total_delivered / total_sent * 100) if total_sent else 0
    failure_rate  = (total_failed   / total_sent * 100) if total_sent else 0

    text = (
        "📊 <b>Broadcast Statistics Dashboard</b>\n\n"
        f"<b>Broadcasts:</b>\n"
        f"  📁 Total: {total}  |  📝 Draft: {drafts}\n"
        f"  ⏰ Scheduled: {scheduled}  |  📤 Sending: {sending}\n"
        f"  ⏸ Paused: {paused}  |  ✅ Completed: {completed}\n"
        f"  🔴 Failed: {failed}  |  ❌ Cancelled: {cancelled}\n\n"
        f"<b>Periods:</b>\n"
        f"  📅 Today: {today_bc}  |  📅 This week: {week_bc}  |  📅 This month: {month_bc}\n\n"
        f"<b>Delivery (all-time):</b>\n"
        f"  📨 Messages sent: {total_sent}\n"
        f"  ✅ Delivered: {total_delivered}  ({delivery_rate:.1f}%)\n"
        f"  ❌ Failed: {total_failed}  ({failure_rate:.1f}%)\n\n"
        f"🔄 Pending retries: {retry_pending}"
    )

    await _safe_edit(query, text, _back_kb("asb:menu"))


# ── Broadcast settings panel ───────────────────────────────────────────────

async def asb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    await _render_settings(query)


async def _render_settings(query):
    feat_status    = cfg.get("scheduled_broadcast_status", "enabled")
    max_speed      = cfg.get_int("broadcast_max_speed", 20)
    delay_ms       = cfg.get_int("broadcast_delay_ms", 50)
    retry_on       = cfg.get_bool("broadcast_retry_failed", True)
    retry_cnt      = cfg.get_int("broadcast_retry_count", 3)
    silent         = cfg.get_bool("broadcast_silent", False)
    notif_off      = cfg.get_bool("broadcast_disable_notifications", False)
    # Enterprise Broadcast Center settings
    max_concurrent = cfg.get_int("broadcast_max_concurrent", 3)
    max_queue      = cfg.get_int("broadcast_max_queue", 10)
    sched_on       = cfg.get_bool("broadcast_scheduler_enabled", True)
    drafts_on      = cfg.get_bool("broadcast_drafts_enabled", True)
    preview_on     = cfg.get_bool("broadcast_preview_enabled", True)
    reports_on     = cfg.get_bool("broadcast_reports_enabled", True)
    test_on        = cfg.get_bool("broadcast_test_send_enabled", True)

    status_icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(feat_status, "🟢")

    def _tf(v: bool) -> str:
        return "✅" if v else "❌"

    text = (
        "⚙️ <b>Enterprise Broadcast Settings</b>\n\n"
        f"<b>Feature Status:</b> {status_icon} {feat_status.capitalize()}\n"
        f"<b>Max Speed:</b> {max_speed} msg/s  |  <b>Delay:</b> {delay_ms} ms\n"
        f"<b>Retry Failed:</b> {_tf(retry_on)}  |  <b>Retry Count:</b> {retry_cnt}\n"
        f"<b>Silent:</b> {_tf(silent)}  |  <b>No Notif:</b> {_tf(notif_off)}\n\n"
        f"<b>Max Concurrent Broadcasts:</b> {max_concurrent}\n"
        f"<b>Max Broadcast Queue:</b> {max_queue} (0 = unlimited)\n\n"
        f"<b>Scheduler:</b> {_tf(sched_on)}  "
        f"<b>Drafts:</b> {_tf(drafts_on)}  "
        f"<b>Preview:</b> {_tf(preview_on)}\n"
        f"<b>Reports:</b> {_tf(reports_on)}  "
        f"<b>Test Send:</b> {_tf(test_on)}\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="asb:settings:status:enabled"),
            InlineKeyboardButton("🟡 Maint.",       callback_data="asb:settings:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="asb:settings:status:disabled"),
        ],
        [
            InlineKeyboardButton(f"🔄 Retry: {'ON ✅' if retry_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_retry_failed"),
            InlineKeyboardButton(f"🔕 Silent: {'ON ✅' if silent else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_silent"),
        ],
        [
            InlineKeyboardButton(f"🔔 No Notif: {'ON ✅' if notif_off else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_disable_notifications"),
        ],
        # Max Concurrent Broadcasts ±
        [
            InlineKeyboardButton("Concurrent −1", callback_data="asb:settings:adj:broadcast_max_concurrent:-1"),
            InlineKeyboardButton(f"Max Concurrent: {max_concurrent}", callback_data="asb:settings"),
            InlineKeyboardButton("Concurrent +1", callback_data="asb:settings:adj:broadcast_max_concurrent:1"),
        ],
        # Max Queue ±
        [
            InlineKeyboardButton("Queue −1", callback_data="asb:settings:adj:broadcast_max_queue:-1"),
            InlineKeyboardButton(f"Max Queue: {max_queue}", callback_data="asb:settings"),
            InlineKeyboardButton("Queue +1", callback_data="asb:settings:adj:broadcast_max_queue:1"),
        ],
        # Feature toggles
        [
            InlineKeyboardButton(f"⏰ Scheduler: {'ON ✅' if sched_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_scheduler_enabled"),
            InlineKeyboardButton(f"📝 Drafts: {'ON ✅' if drafts_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_drafts_enabled"),
        ],
        [
            InlineKeyboardButton(f"👁 Preview: {'ON ✅' if preview_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_preview_enabled"),
            InlineKeyboardButton(f"📊 Reports: {'ON ✅' if reports_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_reports_enabled"),
        ],
        [
            InlineKeyboardButton(f"🧪 Test Send: {'ON ✅' if test_on else 'OFF ❌'}",
                                  callback_data="asb:settings:toggle:broadcast_test_send_enabled"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="asb:menu")],
    ])
    await _safe_edit(query, text, kb)


async def asb_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        new_status = query.data.split(":")[3]
    except IndexError:
        return
    cfg.set("scheduled_broadcast_status", new_status)
    log_admin_action(update.effective_user.id, "scheduled_broadcast.settings",
                     "scheduled_broadcast", 0,
                     f"scheduled_broadcast_status={new_status}",
                     module="scheduled_broadcast")
    await query.answer(f"Status set to: {new_status}", show_alert=True)
    await _render_settings(query)


async def asb_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a boolean BotConfig key — asb:settings:toggle:<key>"""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        key = query.data.split(":")[3]
    except IndexError:
        return
    current = cfg.get_bool(key, False)
    cfg.set(key, str(not current))
    log_admin_action(update.effective_user.id, "scheduled_broadcast.settings",
                     "scheduled_broadcast", 0,
                     f"{key}={not current}",
                     module="scheduled_broadcast")
    await _render_settings(query)


# ── Scheduled APScheduler job ──────────────────────────────────────────────

async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """Run every minute — dispatch due scheduled broadcasts."""
    feat_status = cfg.get("scheduled_broadcast_status", "enabled")
    if feat_status != "enabled":
        return

    now = datetime.utcnow()
    try:
        with get_db_session() as s:
            due = (s.query(ScheduledBroadcast)
                   .filter(
                       ScheduledBroadcast.status == "scheduled",
                       ScheduledBroadcast.scheduled_at <= now,
                   )
                   .all())
            due_ids = [b.id for b in due]
    except Exception:
        logger.exception("scheduled_broadcast_job: query failed")
        return

    for bid in due_ids:
        try:
            with get_db_session() as s:
                br = s.get(ScheduledBroadcast, bid)
                if not br or br.status != "scheduled":
                    continue
                br.status     = "sending"
                br.started_at = now
                s.commit()
                is_recurring  = br.is_recurring
                recur_type    = br.recurrence_type

            sent, delivered, failed, blocked, skipped = await _execute_broadcast(bid, context)

            with get_db_session() as s:
                br = s.get(ScheduledBroadcast, bid)
                if br:
                    br.status          = "sent"
                    br.sent_at         = now
                    br.finished_at     = datetime.utcnow()
                    br.sent_count      = sent
                    br.delivered_count = delivered
                    br.failed_count    = failed
                    br.blocked_count   = blocked
                    br.skipped_count   = skipped
                    s.add(BroadcastLog(
                        broadcast_id     = bid,
                        started_at       = br.started_at,
                        finished_at      = br.finished_at,
                        total_recipients = br.total_recipients,
                        sent             = sent,
                        delivered        = delivered,
                        failed           = failed,
                        blocked          = blocked,
                        skipped          = skipped,
                        created_at       = datetime.utcnow(),
                    ))
                    # If recurring, create next run
                    if is_recurring and recur_type:
                        if recur_type == "daily":
                            delta = timedelta(days=1)
                        elif recur_type == "weekly":
                            delta = timedelta(weeks=1)
                        elif recur_type == "monthly":
                            delta = timedelta(days=30)
                        elif recur_type == "custom":
                            interval_h = getattr(br, "custom_interval_hours", None) or 24
                            delta = timedelta(hours=max(1, int(interval_h)))
                        else:
                            delta = timedelta(days=1)
                        next_br = ScheduledBroadcast(
                            title                = br.title,
                            message_text         = br.message_text,
                            media_type           = br.media_type,
                            file_id              = br.file_id,
                            target_segment       = br.target_segment,
                            target_user_ids      = br.target_user_ids,
                            target_language      = br.target_language,
                            button_text          = br.button_text,
                            button_url           = br.button_url,
                            parse_mode           = br.parse_mode,
                            disable_notification = br.disable_notification,
                            timezone             = br.timezone,
                            is_recurring         = True,
                            recurrence_type      = recur_type,
                            custom_interval_hours = getattr(br, "custom_interval_hours", None),
                            scheduled_at         = now + delta,
                            next_run_at          = now + delta,
                            status               = "scheduled",
                            created_by           = br.created_by,
                            created_at           = datetime.utcnow(),
                            updated_at           = datetime.utcnow(),
                        )
                        s.add(next_br)
                    s.commit()

            logger.info(
                "Scheduled broadcast #%d sent: %d delivered, %d failed, %d blocked, %d skipped",
                bid, delivered, failed, blocked, skipped)
        except Exception:
            logger.exception("scheduled_broadcast_job: error for broadcast #%d", bid)
            try:
                with get_db_session() as s:
                    br = s.get(ScheduledBroadcast, bid)
                    if br:
                        br.status     = "failed"
                        br.finished_at = datetime.utcnow()
                        s.commit()
            except Exception:
                pass

    # Also process retry queue
    try:
        with get_db_session() as s:
            retries = (s.query(BroadcastRetryQueue)
                       .filter(
                           BroadcastRetryQueue.status == "pending",
                           BroadcastRetryQueue.retry_at <= now,
                       )
                       .limit(100)
                       .all())
            retry_items = [(r.id, r.broadcast_id, r.telegram_id) for r in retries]

        for rq_id, bid, tgid in retry_items:
            try:
                with get_db_session() as s:
                    br = s.get(ScheduledBroadcast, bid)
                    if not br:
                        continue
                    msg_kb = None
                    if br.button_text and br.button_url:
                        msg_kb = InlineKeyboardMarkup(
                            [[InlineKeyboardButton(br.button_text, url=br.button_url)]])
                    result = await _send_one(context.bot, tgid, br, msg_kb)
                    with get_db_session() as s2:
                        rq = s2.get(BroadcastRetryQueue, rq_id)
                        if rq:
                            rq.status   = "sent" if result == "delivered" else "failed"
                            rq.attempts = (rq.attempts or 0) + 1
                            s2.commit()
            except Exception:
                logger.exception("retry_queue: error for rq_id=%d", rq_id)
    except Exception:
        logger.exception("scheduled_broadcast_job: retry queue processing failed")


# ── Enterprise Broadcast Center — new handlers (V44) ──────────────────────

async def asb_receive_custom_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom interval input (hours) for recurring broadcasts."""
    txt  = (update.message.text or "").strip()
    data = context.user_data.get("_asb", {})
    try:
        hours = int(txt)
        if hours < 1 or hours > 8760:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid. Send a whole number of hours between 1 and 8760:")
        return ASB_CUSTOM_INTERVAL

    data["custom_interval_hours"] = hours
    data["scheduled_at"]   = datetime.utcnow()
    data["is_recurring"]   = True
    data["recurrence_type"] = "custom"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data="asb:confirm"),
         InlineKeyboardButton("💾 Save Draft",     callback_data="asb:save_draft"),
         InlineKeyboardButton("❌ Cancel",         callback_data="asb:menu")],
    ])
    await update.message.reply_text(
        _build_confirm_text(data), reply_markup=kb, parse_mode="HTML")
    return ASB_CONFIRM


async def asb_test_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send broadcast to the admin only as a test (asb:test_send:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_test_send_enabled", True):
        await query.answer("🧪 Test Send is disabled in settings.", show_alert=True)
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    try:
        from services.broadcast_service import test_broadcast_to_admin
        from config.settings import settings as _settings
        admin_id = _settings.ADMIN_TELEGRAM_ID
        success  = await test_broadcast_to_admin(context.bot, admin_id, bid)
        if success:
            await query.answer(
                f"🧪 Test sent to you (admin) for broadcast #{bid}. "
                "Check your messages.", show_alert=True)
        else:
            await query.answer("❌ Test send failed. Check logs.", show_alert=True)
    except Exception:
        logger.exception("asb_test_send: error for broadcast #%d", bid)
        await query.answer("❌ Test send error. Check logs.", show_alert=True)


async def asb_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show filtered list of draft broadcasts (asb:drafts)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        rows = (s.query(ScheduledBroadcast)
                .filter_by(status="draft")
                .order_by(ScheduledBroadcast.created_at.desc())
                .limit(20)
                .all())
        items = [(r.id, r.title, r.created_at) for r in rows]
        total = s.query(ScheduledBroadcast).filter_by(status="draft").count()

    lines = [f"📝 <b>Draft Broadcasts</b> ({total} total)\n"]
    kb    = []
    for bid, title, created in items:
        date_str = created.strftime("%m/%d %H:%M") if created else "—"
        lines.append(f"📝 <b>{title[:35]}</b>  ({date_str})")
        kb.append([InlineKeyboardButton(
            f"📝 {title[:32]} ({date_str})",
            callback_data=f"asb:view:{bid}")])

    if not items:
        lines.append("No draft broadcasts found.")

    kb.append([InlineKeyboardButton("➕ New Broadcast", callback_data="asb:new")])
    kb.append([
        InlineKeyboardButton("🔙 All Broadcasts", callback_data="asb:menu"),
        InlineKeyboardButton("🏠 Back",           callback_data="acc:sec:broadcast"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def asb_scheduled_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show filtered list of scheduled (upcoming) broadcasts (asb:scheduled_list)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        rows = (s.query(ScheduledBroadcast)
                .filter_by(status="scheduled")
                .order_by(ScheduledBroadcast.scheduled_at.asc())
                .limit(20)
                .all())
        items = [(r.id, r.title, r.scheduled_at, r.is_recurring, r.recurrence_type,
                  getattr(r, "custom_interval_hours", None)) for r in rows]
        total = s.query(ScheduledBroadcast).filter_by(status="scheduled").count()

    lines = [f"📅 <b>Scheduled Broadcasts</b> ({total} upcoming)\n"]
    kb    = []
    for bid, title, sched_at, recurring, recur_type, interval_h in items:
        date_str = sched_at.strftime("%Y-%m-%d %H:%M UTC") if sched_at else "—"
        if recurring and recur_type == "custom" and interval_h:
            recur_str = f"every {interval_h}h"
        elif recurring and recur_type:
            recur_str = recur_type
        else:
            recur_str = "one-time"
        lines.append(f"⏰ <b>{title[:30]}</b>  {date_str}  [{recur_str}]")
        kb.append([InlineKeyboardButton(
            f"⏰ {title[:28]} — {date_str}",
            callback_data=f"asb:view:{bid}")])

    if not items:
        lines.append("No scheduled broadcasts found.")

    kb.append([InlineKeyboardButton("➕ New Broadcast", callback_data="asb:new")])
    kb.append([
        InlineKeyboardButton("🔙 All Broadcasts", callback_data="asb:menu"),
        InlineKeyboardButton("🏠 Back",           callback_data="acc:sec:broadcast"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def asb_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast Reports hub (asb:reports)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_reports_enabled", True):
        await _safe_edit(query,
            "📊 <b>Broadcast Reports</b>\n\n❌ Reports are disabled in Broadcast Settings.",
            _back_kb("asb:menu"))
        return

    try:
        from services.broadcast_service import get_broadcast_dashboard_stats
        stats = get_broadcast_dashboard_stats()
        total     = stats.get("total", 0)
        completed = stats.get("completed", 0)
        failed    = stats.get("failed", 0)
        total_sent = stats.get("total_sent", 0)
        total_delivered = stats.get("total_delivered", 0)
        delivery_rate   = stats.get("delivery_rate", 0.0)
        avg_ms    = stats.get("avg_delivery_ms")
        retries   = stats.get("retry_pending", 0)
        avg_str   = f"{avg_ms:.0f} ms" if avg_ms else "—"
    except Exception:
        logger.exception("asb_reports: stats error")
        total = completed = failed = total_sent = total_delivered = 0
        delivery_rate = 0.0
        avg_str = "—"
        retries = 0

    text = (
        "📊 <b>Broadcast Reports</b>\n\n"
        f"<b>All-Time Delivery Summary</b>\n"
        f"📨 Messages Sent:     <b>{total_sent:,}</b>\n"
        f"✅ Delivered:          <b>{total_delivered:,}</b>  ({delivery_rate:.1f}%)\n"
        f"⚡ Avg Delivery Time: <b>{avg_str}</b>\n"
        f"🔄 Pending Retries:   <b>{retries}</b>\n\n"
        f"<b>Broadcasts Summary</b>\n"
        f"Total: {total}  |  ✅ Completed: {completed}  |  🔴 Failed: {failed}\n\n"
        "Select a broadcast to view its full report and export:"
    )

    # List last 10 completed/failed broadcasts for per-broadcast reports
    with get_db_session() as s:
        recent = (s.query(ScheduledBroadcast)
                  .filter(ScheduledBroadcast.status.in_(["sent", "failed"]))
                  .order_by(ScheduledBroadcast.finished_at.desc())
                  .limit(10)
                  .all())
        recent_items = [(r.id, r.title, r.status, r.delivered_count or 0,
                         r.total_recipients or 0) for r in recent]

    kb = []
    for bid, title, st, delivered, total_r in recent_items:
        icon = "✅" if st == "sent" else "🔴"
        rate = f"{delivered/total_r*100:.0f}%" if total_r else "—"
        kb.append([InlineKeyboardButton(
            f"{icon} {title[:30]} — {rate}",
            callback_data=f"asb:report:{bid}")])

    if not recent_items:
        text += "\n\nNo completed broadcasts to report on yet."

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="asb:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def asb_report_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View detailed report for a single broadcast and offer export (asb:report:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return await asb_reports(update, context)

    try:
        from services.broadcast_service import generate_broadcast_report
        rep = generate_broadcast_report(bid)
    except Exception:
        logger.exception("asb_report_view: error for broadcast #%d", bid)
        await query.answer("❌ Could not load report.", show_alert=True)
        return

    if not rep:
        await _safe_edit(query, f"❌ Broadcast #{bid} not found.",
                          _back_kb("asb:reports"))
        return

    dr   = rep.get("delivery_rate_pct", 0)
    fr   = rep.get("failure_rate_pct",  0)
    br   = rep.get("block_rate_pct",    0)
    avg  = rep.get("avg_delivery_ms")
    avg_str = f"{avg:.0f} ms" if avg else "—"

    text = (
        f"📊 <b>Report: {rep.get('title')} (#{bid})</b>\n\n"
        f"<b>Status:</b> {rep.get('status')}  |  <b>Media:</b> {rep.get('media_type')}\n"
        f"<b>Target:</b> {rep.get('target_segment')}\n"
        f"<b>Scheduled:</b> {rep.get('scheduled_at') or '—'}\n"
        f"<b>Started:</b>   {rep.get('started_at')   or '—'}\n"
        f"<b>Finished:</b>  {rep.get('finished_at')  or '—'}\n\n"
        f"<b>📨 Delivery Report</b>\n"
        f"Total: {rep.get('total_recipients'):,}  |  Sent: {rep.get('sent'):,}\n"
        f"✅ Delivered: {rep.get('delivered'):,}  ({dr:.1f}%)\n"
        f"❌ Failed:    {rep.get('failed'):,}     ({fr:.1f}%)\n"
        f"🚫 Blocked:  {rep.get('blocked'):,}    ({br:.1f}%)\n"
        f"⏭ Skipped:  {rep.get('skipped'):,}\n"
        f"⚡ Avg:      {avg_str}\n\n"
        f"<b>🔄 Retry Queue</b>\n"
        f"Pending: {rep.get('retry_pending')}  |  "
        f"Sent: {rep.get('retry_sent')}  |  "
        f"Failed: {rep.get('retry_failed')}\n\n"
        f"<b>Runs:</b> {len(rep.get('run_logs', []))}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Export CSV",  callback_data=f"asb:export:{bid}:csv"),
            InlineKeyboardButton("📋 Export JSON", callback_data=f"asb:export:{bid}:json"),
        ],
        [InlineKeyboardButton("📋 View Logs", callback_data=f"asb:logs:{bid}")],
        [
            InlineKeyboardButton("🔙 Reports",     callback_data="asb:reports"),
            InlineKeyboardButton("📄 Broadcast",   callback_data=f"asb:view:{bid}"),
        ],
    ])
    await _safe_edit(query, text, kb)


async def asb_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export a broadcast report as CSV or JSON document (asb:export:<id>:<format>)."""
    query = update.callback_query
    await query.answer("Generating export…")
    if not _is_admin(update.effective_user.id):
        return

    try:
        parts  = query.data.split(":")
        bid    = int(parts[2])
        fmt    = parts[3].lower()  # csv | json
    except (IndexError, ValueError):
        return

    try:
        from services.broadcast_service import export_report_csv, export_report_json
        import io as _io
        if fmt == "csv":
            content  = export_report_csv(bid)
            filename = f"broadcast_{bid}_report.csv"
        else:
            content  = export_report_json(bid)
            filename = f"broadcast_{bid}_report.json"

        file_bytes = content.encode("utf-8")
        file_obj   = _io.BytesIO(file_bytes)
        file_obj.name = filename

        await query.message.reply_document(
            document=file_obj,
            filename=filename,
            caption=(
                f"📊 Broadcast #{bid} Report — {fmt.upper()}\n"
                f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            ),
        )
        log_admin_action(
            update.effective_user.id, "scheduled_broadcast.export",
            "scheduled_broadcast", bid,
            f"format={fmt}",
            module="scheduled_broadcast",
        )
    except Exception:
        logger.exception("asb_export: error for broadcast #%d format=%s", bid, fmt)
        await query.message.reply_text("❌ Export failed. Check logs.")


async def asb_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue an interrupted (stuck-in-sending) broadcast (asb:continue:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    try:
        bid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, bid)
        if not br:
            await query.answer("❌ Not found.", show_alert=True)
            return
        if br.status != "sending":
            await query.answer(
                f"❌ Broadcast is not in 'sending' state (current: {br.status}).",
                show_alert=True)
            return
        br_title = br.title

    await _safe_edit(query,
        f"▶️ <b>Continuing interrupted broadcast #{bid}: {br_title}</b>\n\n"
        "Resuming delivery from where it stopped…",
        _back_kb("asb:menu"))

    try:
        sent, delivered, failed, blocked, skipped = await _execute_broadcast(bid, context, query)

        with get_db_session() as s:
            br = s.get(ScheduledBroadcast, bid)
            if br:
                br.status          = "sent"
                br.sent_at         = datetime.utcnow()
                br.finished_at     = datetime.utcnow()
                br.sent_count      = (br.sent_count or 0) + sent
                br.delivered_count = (br.delivered_count or 0) + delivered
                br.failed_count    = (br.failed_count or 0) + failed
                br.blocked_count   = (br.blocked_count or 0) + blocked
                br.skipped_count   = (br.skipped_count or 0) + skipped
                s.add(BroadcastLog(
                    broadcast_id     = bid,
                    started_at       = br.started_at,
                    finished_at      = br.finished_at,
                    total_recipients = br.total_recipients,
                    sent             = sent,
                    delivered        = delivered,
                    failed           = failed,
                    blocked          = blocked,
                    skipped          = skipped,
                    created_at       = datetime.utcnow(),
                ))
                s.commit()

        log_admin_action(
            update.effective_user.id, "scheduled_broadcast.continue",
            "scheduled_broadcast", bid,
            f"sent={sent} delivered={delivered} failed={failed} blocked={blocked} skipped={skipped}",
            module="scheduled_broadcast",
        )
        await query.message.reply_text(
            f"✅ <b>Broadcast #{bid} continued & completed!</b>\n\n"
            f"📤 Sent: {sent}  ✅ Delivered: {delivered}\n"
            f"❌ Failed: {failed}  🚫 Blocked: {blocked}  ⏭ Skipped: {skipped}",
            parse_mode="HTML",
            reply_markup=_back_kb("asb:menu"))
    except Exception as exc:
        logger.exception("asb_continue: error continuing broadcast #%d", bid)
        with get_db_session() as s:
            br = s.get(ScheduledBroadcast, bid)
            if br:
                br.status     = "failed"
                br.error_log  = str(exc)[:2000]
                br.finished_at = datetime.utcnow()
                s.commit()
        await query.message.reply_text(
            f"🔴 <b>Continue failed!</b> Error: {str(exc)[:200]}",
            parse_mode="HTML",
            reply_markup=_back_kb("asb:menu"))


async def asb_interrupted_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show broadcasts currently stuck in 'sending' state (asb:interrupted)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    stale_min = cfg.get_int("broadcast_interrupted_stale_minutes", 30)

    try:
        from services.broadcast_service import find_interrupted_broadcasts
        stuck = find_interrupted_broadcasts(stale_min)
    except Exception:
        logger.exception("asb_interrupted_list: error")
        stuck = []

    if not stuck:
        await _safe_edit(query,
            f"✅ <b>No interrupted broadcasts</b>\n\n"
            f"No broadcasts have been stuck in 'sending' state for more than "
            f"{stale_min} minutes.",
            _back_kb("asb:menu"))
        return

    lines = [
        f"⚠️ <b>Interrupted Broadcasts</b>\n\n"
        f"The following broadcasts have been stuck in 'sending' for >{stale_min} min:\n"
    ]
    kb = []
    for bid, title, started_at, sent_c, total_r in stuck:
        started_str = started_at.strftime("%m/%d %H:%M UTC") if started_at else "—"
        lines.append(
            f"📤 <b>{title[:35]}</b> (#{bid})\n"
            f"   Started: {started_str}  |  Sent: {sent_c}/{total_r or '?'}"
        )
        kb.append([InlineKeyboardButton(
            f"▶️ Continue #{bid}: {title[:28]}",
            callback_data=f"asb:continue:{bid}")])

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="asb:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def asb_settings_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adjust a numeric settings value by ±delta (asb:settings:adj:<key>:<delta>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    try:
        parts = query.data.split(":")
        # asb:settings:adj:<key>:<delta>
        key   = parts[3]
        delta = int(parts[4])
    except (IndexError, ValueError):
        return

    allowed_keys = {"broadcast_max_concurrent", "broadcast_max_queue"}
    if key not in allowed_keys:
        return

    current = cfg.get_int(key, 3)
    new_val = max(0, current + delta)
    cfg.set(key, str(new_val))

    log_admin_action(
        update.effective_user.id, "scheduled_broadcast.settings",
        "scheduled_broadcast", 0,
        f"{key}: {current} → {new_val}",
        module="scheduled_broadcast",
    )
    await _render_settings(query)


# ── Conversation cancel ────────────────────────────────────────────────────

async def asb_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_asb", None)
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Cancelled.", _back_kb("asb:menu"))
    return ConversationHandler.END


# ── Conversation handler builder ───────────────────────────────────────────

def build_asb_conv() -> ConversationHandler:
    """Build the broadcast creation / edit conversation handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(asb_new_start,    pattern=r"^asb:new$"),
            CallbackQueryHandler(asb_edit_handler, pattern=r"^asb:edit:\d+$"),
        ],
        states={
            ASB_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_title),
            ],
            ASB_MEDIA_TYPE: [
                CallbackQueryHandler(asb_receive_media_type, pattern=r"^asb:mtype:"),
            ],
            ASB_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_text),
            ],
            ASB_POLL_OPTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_poll_options),
            ],
            ASB_MEDIA_FILE: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.ANIMATION |
                    filters.Document.ALL | filters.VOICE | filters.AUDIO |
                    filters.Sticker.ALL,
                    asb_receive_media,
                ),
            ],
            ASB_BUTTON_TEXT: [
                CallbackQueryHandler(asb_button_choice, pattern=r"^asb:btn:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_button_text),
            ],
            ASB_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_button_url),
            ],
            ASB_TARGET: [
                CallbackQueryHandler(asb_receive_target, pattern=r"^asb:tgt:"),
            ],
            ASB_TARGET_EXTRA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_target_extra),
            ],
            ASB_SCHEDULE: [
                CallbackQueryHandler(asb_receive_schedule_choice, pattern=r"^asb:sched:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_schedule_text),
            ],
            # V44: Custom interval input
            ASB_CUSTOM_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, asb_receive_custom_interval),
            ],
            ASB_CONFIRM: [
                CallbackQueryHandler(asb_confirm_save,  pattern=r"^asb:confirm$"),
                CallbackQueryHandler(asb_save_draft,    pattern=r"^asb:save_draft$"),
                CallbackQueryHandler(asb_cancel_conv,   pattern=r"^asb:menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(asb_cancel_conv, pattern=r"^asb:menu$"),
            CommandHandler("cancel", asb_cancel_conv),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
