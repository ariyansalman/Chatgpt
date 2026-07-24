"""V25 — Admin Order Timeline panel.

Callback namespace:  ``acc:ots:*`` (routed through admin_control_center)
Section entry:       ``acc:sec:ots``

Sub-actions:
    acc:ots:menu                    → global settings panel
    acc:ots:status:<s>              → 3-state feature status (enabled/maintenance/disabled)
    acc:ots:toggle:<key>            → toggle a boolean setting
    acc:ots:view:<order_id>         → admin view of a specific order's timeline
    acc:ots:chstat:<order_id>       → show status-change picker for an order
    acc:ots:setstat:<order_id>:<s>  → apply a new status to an order
    acc:ots:note:<order_id>         → start add-note conversation
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ContextTypes, ConversationHandler, MessageHandler,
    CommandHandler, CallbackQueryHandler, filters,
)

from database import get_db_session
from database.models import Order, OrderStatus, OrderLifecycleStatus, OrderItem, Product
from services import order_timeline as tl
from services import order_lifecycle as lc
from utils.audit import log_admin_action
from utils.bot_config import cfg
from ._acc_helpers import require_admin, back_root, send

logger = logging.getLogger(__name__)

OTS_NOTE_TEXT = 9400


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_STATUS_OPTS = [
    ("enabled",     "🟢 Enable"),
    ("maintenance", "🟡 Maintenance"),
    ("disabled",    "🔴 Disable"),
]

_BOOL_SETTINGS = [
    ("ots_show_to_users",           "Show Timeline to Users"),
    ("ots_show_processing_time",    "Show Processing Time"),
    ("ots_show_estimated_delivery", "Show Estimated Delivery"),
    ("ots_allow_manual_status",     "Allow Admin Status Updates"),
    ("ots_notify_users",            "Notify Users on Status Change"),
]

_LIFECYCLE_CHOICES = [
    OrderLifecycleStatus.PENDING,
    OrderLifecycleStatus.AWAITING_PAYMENT,
    OrderLifecycleStatus.PAID,
    OrderLifecycleStatus.PROCESSING,
    OrderLifecycleStatus.DELIVERED,
    OrderLifecycleStatus.COMPLETED,
    OrderLifecycleStatus.CANCELLED,
    OrderLifecycleStatus.REFUNDED,
]

_LIFECYCLE_LABELS = {
    "PENDING":          "🆕 Created",
    "AWAITING_PAYMENT": "💳 Awaiting Payment",
    "PAID":             "💰 Paid",
    "PROCESSING":       "⚙️ Processing",
    "DELIVERED":        "📦 Delivered",
    "COMPLETED":        "🎉 Completed",
    "CANCELLED":        "❌ Cancelled",
    "REFUNDED":         "💸 Refunded",
}


def _cur_status() -> str:
    return cfg.get_str("ots_status", "enabled")


def _bool_val(key: str) -> bool:
    return cfg.get_bool(key, True)


def _back_ots() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Timeline Settings", callback_data="acc:ots:menu")


# ─────────────────────────────────────────────────────────────────────────
# Settings Panel
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def ots_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = _cur_status()
    status_label = next((lbl for key, lbl in _STATUS_OPTS if key == status), "?")

    lines = [
        "📋 <b>ORDER TIMELINE SETTINGS</b>  (V25)",
        "",
        f"<b>Feature Status:</b>  {status_label}",
        "",
        "<b>Settings:</b>",
    ]
    for key, label in _BOOL_SETTINGS:
        val = _bool_val(key)
        lines.append(f"  • {label}:  <b>{'✅ ON' if val else '🚫 OFF'}</b>")

    kb = [
        # 3-state status row
        [InlineKeyboardButton(lbl, callback_data=f"acc:ots:status:{key}")
         for key, lbl in _STATUS_OPTS],
    ]
    # Bool toggle buttons (2 per row)
    for i in range(0, len(_BOOL_SETTINGS), 2):
        row = []
        for key, label in _BOOL_SETTINGS[i:i + 2]:
            val = _bool_val(key)
            row.append(InlineKeyboardButton(
                f"{'✅' if val else '🚫'} {label}",
                callback_data=f"acc:ots:toggle:{key}",
            ))
        kb.append(row)

    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Feature Status
# ─────────────────────────────────────────────────────────────────────────

async def _set_status(update, context, value: str):
    if value in ("enabled", "maintenance", "disabled"):
        cfg.set("ots_status", value)
        try:
            log_admin_action(update.effective_user.id, "ots_status_changed",
                             f"ots_status={value}")
        except Exception:
            pass
    await ots_menu(update, context)


async def _toggle_setting(update, context, key: str):
    if key in dict(_BOOL_SETTINGS):
        cfg.set(key, not _bool_val(key))
    await ots_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────
# Admin Order Timeline View
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def ots_view(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    with get_db_session() as s:
        order = s.get(Order, order_id)
        if not order:
            await send(update, "❌ Order not found.", InlineKeyboardMarkup([[_back_ots()]]))
            return
        lifecycle = order.lifecycle_status
        status_name = lifecycle.name if lifecycle else "PENDING"

    timeline_text = tl.render_admin_timeline(order_id)

    kb = [
        [InlineKeyboardButton("📝 Add Note", callback_data=f"acc:ots:note:{order_id}"),
         InlineKeyboardButton("🔄 Change Status", callback_data=f"acc:ots:chstat:{order_id}")],
        [_back_ots(), back_root()],
    ]
    header = f"<b>Order #{order_id}</b>  (current: {_LIFECYCLE_LABELS.get(status_name, status_name)})\n\n"
    await send(update, header + timeline_text, InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Status Change Picker
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def ots_chstat(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    if not _bool_val("ots_allow_manual_status"):
        await send(update, "⛔ Manual status updates are disabled.",
                   InlineKeyboardMarkup([[_back_ots()]]))
        return

    kb = []
    for ls in _LIFECYCLE_CHOICES:
        label = _LIFECYCLE_LABELS.get(ls.name, ls.name)
        kb.append([InlineKeyboardButton(label,
                                        callback_data=f"acc:ots:setstat:{order_id}:{ls.name}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"acc:ots:view:{order_id}")])
    await send(update,
               f"<b>Order #{order_id}</b> — Select new status:",
               InlineKeyboardMarkup(kb))


async def _apply_status(update, context, order_id: int, status_name: str):
    if not _bool_val("ots_allow_manual_status"):
        await ots_menu(update, context)
        return
    try:
        new_status = OrderLifecycleStatus[status_name]
    except KeyError:
        await ots_view(update, context, order_id)
        return

    admin_tg_id = update.effective_user.id
    bot = getattr(context, "bot", None)

    ok = lc.transition(
        order_id, new_status,
        actor_type="admin",
        admin_id=admin_tg_id,
        reason=f"Manual update by admin {admin_tg_id}",
        bot=bot,
    )
    if ok:
        try:
            log_admin_action(admin_tg_id, "ots_status_set",
                             f"order_id={order_id} new_status={status_name}")
        except Exception:
            pass
    await ots_view(update, context, order_id)


# ─────────────────────────────────────────────────────────────────────────
# Add Admin Note — conversation
# ─────────────────────────────────────────────────────────────────────────

@require_admin
async def note_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    order_id = int(parts[-1]) if parts[-1].isdigit() else None
    if not order_id:
        await q.message.reply_text("Invalid order.")
        return ConversationHandler.END
    context.user_data["ots_note_order_id"] = order_id
    await q.message.reply_text(
        f"📝 Enter your note for <b>Order #{order_id}</b> "
        f"(or /cancel to abort):",
        parse_mode="HTML",
    )
    return OTS_NOTE_TEXT


async def note_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    order_id = context.user_data.pop("ots_note_order_id", None)
    if not order_id:
        await update.message.reply_text("Session expired. Please try again.")
        return ConversationHandler.END
    if not note:
        await update.message.reply_text("Note cannot be empty. Try again or /cancel.")
        return OTS_NOTE_TEXT

    try:
        tl.add_admin_note(order_id, note, admin_id=update.effective_user.id)
        try:
            log_admin_action(update.effective_user.id, "ots_note_added",
                             f"order_id={order_id}")
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ Note added to <b>Order #{order_id}</b>.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to save note: {exc}")

    return ConversationHandler.END


async def note_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("ots_note_order_id", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_ots_note_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(note_start, pattern=r"^acc:ots:note:\d+$")],
        states={
            OTS_NOTE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_receive)],
        },
        fallbacks=[CommandHandler("cancel", note_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────
# Router — entry point from admin_control_center
# ─────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await ots_menu(update, context)
        return

    if action == "status" and rest:
        await _set_status(update, context, rest[0])
        return

    if action == "toggle" and rest:
        await _toggle_setting(update, context, rest[0])
        return

    if action == "view" and rest:
        try:
            await ots_view(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await ots_menu(update, context)
        return

    if action == "chstat" and rest:
        try:
            await ots_chstat(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await ots_menu(update, context)
        return

    if action == "setstat" and len(rest) >= 2:
        try:
            await _apply_status(update, context, int(rest[0]), rest[1])
        except (ValueError, IndexError):
            await ots_menu(update, context)
        return

    await ots_menu(update, context)
