"""Announcement System — V20.

Callback namespace: ``ann:*``

Admin features:
  • Create / edit / delete announcements (title + content)
  • List active and scheduled announcements
  • Send immediately to all active users (broadcast)
  • Schedule for future delivery (stored as scheduled_at)
  • Pin / unpin announcements (featured at top)
  • Auto-expire by date (expires_at)
  • Target: all users, VIP only, or specific user IDs
  • Types: popup (DM), banner (main menu notice), silent (internal only)

User features:
  • Users see pinned announcements when they open the main menu (if popup type)
  • ``ann:read:<id>`` marks an announcement as read for the user

Background job (registered in bot.py):
  • ``announcement_send_job`` — runs every minute, finds due scheduled
    announcements and sends them
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session, User
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states (non-colliding) ────────────────────────────────────────
ANN_TITLE    = 70
ANN_CONTENT  = 71
ANN_SCHEDULE = 72
ANN_EXPIRES  = 73
ANN_TARGETS  = 74


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_announcement(ann_id: int) -> Optional[dict]:
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            row = s.execute(text(
                "SELECT id, title, content, target, is_active, is_pinned, is_sent, "
                "sent_count, announcement_type, scheduled_at, expires_at, created_at "
                "FROM announcements WHERE id = :aid"
            ), {"aid": ann_id}).fetchone()
            if not row:
                return None
            return {
                "id": row[0], "title": row[1], "content": row[2],
                "target": row[3], "is_active": row[4], "is_pinned": row[5],
                "is_sent": row[6], "sent_count": row[7], "ann_type": row[8],
                "scheduled_at": row[9], "expires_at": row[10],
                "created_at": row[11],
            }
    except Exception:
        logger.exception("_get_announcement failed")
        return None


def _list_announcements(page: int = 0, page_size: int = 10) -> List[dict]:
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT id, title, is_active, is_pinned, is_sent, sent_count, "
                "announcement_type, created_at "
                "FROM announcements "
                "ORDER BY is_pinned DESC, created_at DESC "
                "LIMIT :lim OFFSET :off"
            ), {"lim": page_size, "off": page * page_size}).fetchall()
            return [
                {
                    "id": r[0], "title": r[1], "is_active": r[2],
                    "is_pinned": r[3], "is_sent": r[4], "sent_count": r[5],
                    "ann_type": r[6], "created_at": r[7],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("_list_announcements failed")
        return []


def _count_announcements() -> int:
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            row = s.execute(text("SELECT COUNT(*) FROM announcements")).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _count_active_users() -> int:
    try:
        with get_db_session() as s:
            return s.query(User).filter(User.is_banned.is_(False)).count()
    except Exception:
        try:
            with get_db_session() as s:
                return s.query(User).count()
        except Exception:
            return 0


def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Admin panels
# ─────────────────────────────────────────────────────────────────────────────

async def announcements_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Announcement system main panel (ann:menu)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    enabled = cfg.get_bool("feature_announcements_enabled", True)
    total = _count_announcements()
    user_count = _count_active_users()

    lines = [
        "📢 <b>Announcement System</b>\n",
        f"Feature: {'✅ Enabled' if enabled else '❌ Disabled'}",
        f"Total announcements: <b>{total}</b>",
        f"Active users: <b>{user_count:,}</b>",
    ]

    kb = [
        [InlineKeyboardButton(
            "❌ Disable" if enabled else "✅ Enable",
            callback_data="ann:toggle",
        )],
        [InlineKeyboardButton("➕ New Announcement", callback_data="ann:create"),
         InlineKeyboardButton("📋 List All", callback_data="ann:list:0")],
        [InlineKeyboardButton("📌 Pinned", callback_data="ann:pinned"),
         InlineKeyboardButton("⏰ Scheduled", callback_data="ann:scheduled")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def ann_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle announcement feature (ann:toggle)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return
    current = cfg.get_bool("feature_announcements_enabled", True)
    cfg.set("feature_announcements_enabled", not current)
    await announcements_menu(update, context)


async def ann_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List announcements paginated (ann:list:<page>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    items = _list_announcements(page=page, page_size=10)
    total = _count_announcements()
    pages = max(1, (total + 9) // 10)

    lines = [f"📋 <b>All Announcements</b> (page {page + 1}/{pages})\n"]
    kb = []
    for item in items:
        pin = "📌 " if item["is_pinned"] else ""
        sent = f"✅ {item['sent_count']}" if item["is_sent"] else "📝 Draft"
        title_short = item["title"][:35]
        lines.append(f"{pin}#{item['id']} {title_short} — {sent}")
        kb.append([InlineKeyboardButton(
            f"{pin}#{item['id']} {title_short}",
            callback_data=f"ann:view:{item['id']}",
        )])

    if not items:
        lines.append("No announcements yet.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ann:list:{page - 1}"))
    if (page + 1) < pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"ann:list:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="ann:menu")])

    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def ann_pinned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List pinned announcements (ann:pinned)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT id, title, is_sent, sent_count FROM announcements "
                "WHERE is_pinned = TRUE ORDER BY created_at DESC LIMIT 20"
            )).fetchall()
    except Exception:
        rows = []

    lines = ["📌 <b>Pinned Announcements</b>\n"]
    kb = []
    for r in rows:
        sent_info = f"✅ {r[3]}" if r[2] else "📝 Draft"
        kb.append([InlineKeyboardButton(
            f"📌 #{r[0]} {r[1][:40]}",
            callback_data=f"ann:view:{r[0]}",
        )])
        lines.append(f"#{r[0]} {r[1]} — {sent_info}")
    if not rows:
        lines.append("No pinned announcements.")

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="ann:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def ann_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List scheduled (unsent) announcements (ann:scheduled)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT id, title, scheduled_at FROM announcements "
                "WHERE is_sent = FALSE AND is_scheduled = TRUE AND is_active = TRUE "
                "ORDER BY scheduled_at ASC LIMIT 20"
            )).fetchall()
    except Exception:
        rows = []

    lines = ["⏰ <b>Scheduled Announcements</b>\n"]
    kb = []
    for r in rows:
        dt = r[2].strftime("%b %d %H:%M UTC") if r[2] else "—"
        kb.append([InlineKeyboardButton(
            f"⏰ #{r[0]} {r[1][:35]}",
            callback_data=f"ann:view:{r[0]}",
        )])
        lines.append(f"#{r[0]} {r[1]} — {dt}")
    if not rows:
        lines.append("No scheduled announcements.")

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="ann:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def ann_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View a single announcement (ann:view:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        ann_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    ann = _get_announcement(ann_id)
    if not ann:
        await _safe_edit(query, "❌ Announcement not found.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🔙 Back", callback_data="ann:list:0")
                         ]]))
        return

    pin_str = "📌 PINNED · " if ann["is_pinned"] else ""
    sent_str = f"Sent to {ann['sent_count']} users" if ann["is_sent"] else "Not sent yet"
    scheduled_str = ""
    if ann["scheduled_at"]:
        scheduled_str = f"\n⏰ Scheduled: {ann['scheduled_at'].strftime('%Y-%m-%d %H:%M UTC')}"
    expires_str = ""
    if ann["expires_at"]:
        expires_str = f"\n📅 Expires: {ann['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}"

    lines = [
        f"📢 <b>{pin_str}Announcement #{ann['id']}</b>\n",
        f"<b>{ann['title']}</b>\n",
        ann["content"],
        "",
        f"Type: <b>{ann['ann_type']}</b> · Target: <b>{ann['target']}</b>",
        f"Status: <b>{'Active' if ann['is_active'] else 'Inactive'}</b> — {sent_str}",
        scheduled_str,
        expires_str,
    ]

    kb = []
    if not ann["is_sent"]:
        kb.append([InlineKeyboardButton(
            "📤 Send Now", callback_data=f"ann:send:{ann_id}"
        )])
    if ann["is_pinned"]:
        kb.append([InlineKeyboardButton(
            "📌 Unpin", callback_data=f"ann:unpin:{ann_id}"
        )])
    else:
        kb.append([InlineKeyboardButton(
            "📌 Pin", callback_data=f"ann:pin:{ann_id}"
        )])
    if ann["is_active"]:
        kb.append([InlineKeyboardButton(
            "🚫 Deactivate", callback_data=f"ann:deactivate:{ann_id}"
        )])
    else:
        kb.append([InlineKeyboardButton(
            "✅ Activate", callback_data=f"ann:activate:{ann_id}"
        )])
    kb.append([
        InlineKeyboardButton("🗑 Delete", callback_data=f"ann:delete:{ann_id}"),
        InlineKeyboardButton("🔙 Back", callback_data="ann:list:0"),
    ])
    await _safe_edit(query, "\n".join(l for l in lines if l is not None),
                     InlineKeyboardMarkup(kb))


async def ann_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pin an announcement (ann:pin:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        ann_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE announcements SET is_pinned = TRUE, updated_at = NOW() WHERE id = :aid"
            ), {"aid": ann_id})
            s.commit()
    except Exception:
        logger.exception("ann_pin failed")

    await query.answer("📌 Pinned!", show_alert=False)
    # Refresh view
    context.user_data["_cb_data_override"] = str(ann_id)
    await ann_view(update, context)


async def ann_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unpin an announcement (ann:unpin:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        ann_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE announcements SET is_pinned = FALSE, updated_at = NOW() WHERE id = :aid"
            ), {"aid": ann_id})
            s.commit()
    except Exception:
        logger.exception("ann_unpin failed")

    await query.answer("📌 Unpinned.", show_alert=False)
    context.user_data["_cb_data_override"] = str(ann_id)
    await ann_view(update, context)


async def ann_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate an announcement (ann:activate:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return
    try:
        ann_id = int(query.data.split(":")[-1])
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE announcements SET is_active = TRUE, updated_at = NOW() WHERE id = :aid"
            ), {"aid": ann_id})
            s.commit()
    except Exception:
        logger.exception("ann_activate failed")
    context.user_data["_cb_data_override"] = str(ann_id)
    await ann_view(update, context)


async def ann_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivate an announcement (ann:deactivate:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return
    try:
        ann_id = int(query.data.split(":")[-1])
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE announcements SET is_active = FALSE, updated_at = NOW() WHERE id = :aid"
            ), {"aid": ann_id})
            s.commit()
    except Exception:
        logger.exception("ann_deactivate failed")
    context.user_data["_cb_data_override"] = str(ann_id)
    await ann_view(update, context)


async def ann_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete an announcement (ann:delete:<id>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        ann_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    # Two-step confirm
    if context.user_data.get(f"_ann_del_confirm") == ann_id:
        # Confirmed — delete
        try:
            from sqlalchemy import text
            with get_db_session() as s:
                s.execute(text(
                    "DELETE FROM announcement_reads WHERE announcement_id = :aid"
                ), {"aid": ann_id})
                s.execute(text(
                    "DELETE FROM announcements WHERE id = :aid"
                ), {"aid": ann_id})
                s.commit()
        except Exception:
            logger.exception("ann_delete failed")
        context.user_data.pop("_ann_del_confirm", None)
        await _safe_edit(query, f"🗑 Announcement #{ann_id} deleted.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("📋 List", callback_data="ann:list:0")
                         ]]))
    else:
        context.user_data["_ann_del_confirm"] = ann_id
        await _safe_edit(
            query,
            f"⚠️ <b>Delete Announcement #{ann_id}?</b>\nThis cannot be undone. Tap again to confirm.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"ann:delete:{ann_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"ann:view:{ann_id}")],
            ]),
        )


async def ann_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send an announcement immediately to all active users (ann:send:<id>)."""
    query = update.callback_query
    await query.answer("📤 Sending… this may take a while.", show_alert=False)

    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    try:
        ann_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    ann = _get_announcement(ann_id)
    if not ann:
        await _safe_edit(query, "❌ Announcement not found.")
        return

    if ann["is_sent"]:
        await query.answer("Already sent.", show_alert=True)
        return

    # Get target users
    try:
        with get_db_session() as s:
            if ann["target"] == "vip":
                from sqlalchemy import func as sqlfunc
                from database import Order, OrderStatus
                vip_thresh = cfg.get_float("seg_vip_spend_threshold", 100.0)
                rows = (
                    s.query(User.telegram_id)
                    .join(Order, Order.user_id == User.id)
                    .filter(Order.status == OrderStatus.COMPLETED)
                    .group_by(User.id, User.telegram_id)
                    .having(sqlfunc.sum(Order.total_amount) >= vip_thresh)
                    .limit(10000)
                    .all()
                )
                user_ids = [r[0] for r in rows]
            elif ann["target"] == "specific_users":
                try:
                    target_ids = json.loads(ann.get("target_user_ids") or "[]")
                    user_ids = [int(i) for i in target_ids if str(i).isdigit()]
                except Exception:
                    user_ids = []
            else:
                # all
                rows = s.query(User.telegram_id).limit(50000).all()
                user_ids = [r[0] for r in rows]
    except Exception:
        logger.exception("ann_send_now: failed to get users")
        user_ids = []

    if not user_ids:
        await _safe_edit(query, "❌ No target users found.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🔙 Back", callback_data=f"ann:view:{ann_id}")
                         ]]))
        return

    msg_text = f"📢 <b>{ann['title']}</b>\n\n{ann['content']}"
    sent = 0
    failed = 0
    read_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark as Read", callback_data=f"ann:read:{ann_id}")
    ]])

    for tg_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=msg_text,
                parse_mode="HTML",
                reply_markup=read_kb if ann["ann_type"] == "popup" else None,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            failed += 1

    # Mark as sent
    try:
        from sqlalchemy import text
        with get_db_session() as s:
            s.execute(text(
                "UPDATE announcements SET is_sent = TRUE, sent_at = NOW(), "
                "sent_count = :cnt, updated_at = NOW() WHERE id = :aid"
            ), {"cnt": sent, "aid": ann_id})
            s.commit()
    except Exception:
        logger.exception("ann_send_now: marking sent failed")

    await _safe_edit(
        query,
        f"✅ <b>Announcement #{ann_id} Sent!</b>\n\n"
        f"Delivered to <b>{sent}</b> users.\n"
        f"Failed: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=f"ann:view:{ann_id}")
        ]]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Create announcement conversation
# ─────────────────────────────────────────────────────────────────────────────

async def ann_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start create-announcement conversation (ann:create)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    context.user_data["_ann"] = {}
    await _safe_edit(
        query,
        "➕ <b>New Announcement</b>\n\nStep 1/2 — Send the <b>title</b> of the announcement:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="ann:menu")
        ]]),
    )
    return ANN_TITLE


async def ann_title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive announcement title."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("❌ Title cannot be empty. Try again:")
        return ANN_TITLE
    if len(title) > 255:
        await update.message.reply_text("❌ Title too long (max 255 chars). Try again:")
        return ANN_TITLE

    context.user_data.setdefault("_ann", {})["title"] = title
    await update.message.reply_text(
        f"✅ Title: <b>{title}</b>\n\n"
        f"Step 2/2 — Now send the <b>announcement content</b> (supports HTML):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="ann:menu")
        ]]),
    )
    return ANN_CONTENT


async def ann_content_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive announcement content and confirm creation."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    content = (update.message.text or "").strip()
    if not content:
        await update.message.reply_text("❌ Content cannot be empty. Try again:")
        return ANN_CONTENT

    ann_data = context.user_data.get("_ann", {})
    title = ann_data.get("title", "")
    admin_id = update.effective_user.id

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            row = s.execute(text(
                "INSERT INTO announcements "
                "(title, content, target, is_active, is_pinned, is_scheduled, "
                " is_sent, announcement_type, created_by, created_at, updated_at) "
                "VALUES (:title, :content, 'all', TRUE, FALSE, FALSE, "
                "FALSE, 'popup', :admin_id, NOW(), NOW()) "
                "RETURNING id"
            ), {"title": title, "content": content, "admin_id": admin_id}).fetchone()
            s.commit()
            ann_id = row[0] if row else None
    except Exception:
        logger.exception("ann_content_received: insert failed")
        ann_id = None

    context.user_data.pop("_ann", None)

    if ann_id:
        await update.message.reply_text(
            f"✅ <b>Announcement #{ann_id} created!</b>\n\n"
            f"<b>{title}</b>\n{content[:200]}{'...' if len(content) > 200 else ''}\n\n"
            f"Use the panel below to send, pin, or schedule it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Send Now", callback_data=f"ann:send:{ann_id}"),
                 InlineKeyboardButton("👁 View", callback_data=f"ann:view:{ann_id}")],
                [InlineKeyboardButton("📢 Announcements", callback_data="ann:menu")],
            ]),
        )
    else:
        await update.message.reply_text("❌ Failed to create announcement. Please try again.",
                                        reply_markup=InlineKeyboardMarkup([[
                                            InlineKeyboardButton("🔙 Back", callback_data="ann:menu")
                                        ]]))
    return ConversationHandler.END


async def ann_create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel announcement creation."""
    q = update.callback_query
    if q:
        await q.answer()
    context.user_data.pop("_ann", None)
    if q:
        await _safe_edit(q, "❌ Cancelled.",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🔙 Back", callback_data="ann:menu")
                         ]]))
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# User-facing: mark announcement as read
# ─────────────────────────────────────────────────────────────────────────────

async def ann_mark_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User marks announcement as read (ann:read:<id>)."""
    query = update.callback_query
    await query.answer("✅ Marked as read.", show_alert=False)
    tid = update.effective_user.id

    try:
        ann_id = int(query.data.split(":")[-1])
        from sqlalchemy import text
        with get_db_session() as s:
            user = s.query(User).filter_by(telegram_id=tid).first()
            if user:
                s.execute(text(
                    "INSERT INTO announcement_reads (announcement_id, user_id, read_at) "
                    "VALUES (:aid, :uid, NOW()) "
                    "ON CONFLICT (announcement_id, user_id) DO NOTHING"
                ), {"aid": ann_id, "uid": user.id})
                s.commit()
    except Exception:
        pass  # Non-critical


# ─────────────────────────────────────────────────────────────────────────────
# Background job: send scheduled announcements
# ─────────────────────────────────────────────────────────────────────────────

async def announcement_send_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: send due scheduled announcements.

    Runs every 60 seconds. Finds announcements where:
      - is_active = TRUE
      - is_sent = FALSE
      - is_scheduled = TRUE
      - scheduled_at <= NOW()
    and broadcasts them.
    """
    if not cfg.get_bool("feature_announcements_enabled", True):
        return

    try:
        from sqlalchemy import text
        with get_db_session() as s:
            rows = s.execute(text(
                "SELECT id FROM announcements "
                "WHERE is_active = TRUE AND is_sent = FALSE "
                "AND is_scheduled = TRUE AND scheduled_at <= NOW() "
                "LIMIT 5"
            )).fetchall()
            due_ids = [r[0] for r in rows]
    except Exception:
        return

    for ann_id in due_ids:
        try:
            ann = _get_announcement(ann_id)
            if not ann:
                continue

            with get_db_session() as s:
                rows = s.query(User.telegram_id).limit(50000).all()
                user_ids = [r[0] for r in rows]

            msg_text = f"📢 <b>{ann['title']}</b>\n\n{ann['content']}"
            read_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Mark as Read", callback_data=f"ann:read:{ann_id}")
            ]])
            sent = 0
            for tg_id in user_ids:
                try:
                    await context.bot.send_message(
                        chat_id=tg_id, text=msg_text, parse_mode="HTML",
                        reply_markup=read_kb, disable_web_page_preview=True,
                    )
                    sent += 1
                except Exception:
                    pass

            from sqlalchemy import text as sqltxt
            with get_db_session() as s:
                s.execute(sqltxt(
                    "UPDATE announcements SET is_sent=TRUE, sent_at=NOW(), "
                    "sent_count=:cnt, updated_at=NOW() WHERE id=:aid"
                ), {"cnt": sent, "aid": ann_id})
                s.commit()
        except Exception:
            logger.exception("announcement_send_job: failed for ann %s", ann_id)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation builder
# ─────────────────────────────────────────────────────────────────────────────

def build_ann_create_conv():
    from telegram.ext import (
        ConversationHandler as CH, CallbackQueryHandler as CQH,
        MessageHandler, filters,
    )
    return CH(
        entry_points=[CQH(ann_create_start, pattern=r"^ann:create$")],
        states={
            ANN_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ann_title_received),
                CQH(ann_create_cancel, pattern=r"^ann:menu$"),
            ],
            ANN_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ann_content_received),
                CQH(ann_create_cancel, pattern=r"^ann:menu$"),
            ],
        },
        fallbacks=[CQH(ann_create_cancel, pattern=r"^ann:menu$")],
        per_user=True, per_chat=True, allow_reentry=True,
    )
