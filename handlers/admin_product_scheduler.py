"""V45 — Admin Product Scheduler.

Callback namespace: aps:*
ConversationHandler states: 9800–9808

Lets admins schedule future product changes:
  publish, unpublish, price_change, discount, stock_change

Route map:
  aps:menu                — Dashboard
  aps:new                 — Pick schedule type
  aps:new:<type>          — ConvHandler start
  aps:list[:<pg>]         — Pending schedules
  aps:history[:<pg>]      — Executed/failed/cancelled history
  aps:upcoming            — Next 7 days
  aps:detail:<id>         — Detail view
  aps:cancel:<id>         — Cancel a pending schedule
  aps:stats               — Statistics
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from services.product_scheduler_service import (
    create_schedule, cancel_schedule, get_schedule,
    list_schedules, get_upcoming, get_history, get_stats,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── ConvHandler states ─────────────────────────────────────────────────────────
APS_PICK_PRODUCT  = 9800
APS_PICK_DATETIME = 9801
APS_PICK_VALUE    = 9802
APS_CONFIRM       = 9803

_PAGE = 10

SCHEDULE_LABELS = {
    "publish":      "🟢 Publish",
    "unpublish":    "🔴 Unpublish",
    "price_change": "💰 Price Change",
    "discount":     "🏷 Discount",
    "stock_change": "📦 Stock Change",
}

STATUS_ICON = {
    "pending":   "⏳",
    "executed":  "✅",
    "failed":    "❌",
    "cancelled": "🚫",
}


def _back(to: str = "aps:menu") -> InlineKeyboardButton:
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


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ─── Dashboard ────────────────────────────────────────────────────────────────

async def aps_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    stats = await asyncio.to_thread(get_stats)
    text = (
        "🗓 <b>PRODUCT SCHEDULER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Pending:   <b>{stats['pending']}</b>\n"
        f"✅ Executed:  <b>{stats['executed']}</b>\n"
        f"❌ Failed:    <b>{stats['failed']}</b>\n"
        f"🚫 Cancelled: <b>{stats['cancelled']}</b>\n"
        f"🔜 Due soon (24h): <b>{stats['due_soon']}</b>\n"
        f"⚠️ Overdue:   <b>{stats['overdue']}</b>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Schedule",     callback_data="aps:new")],
        [InlineKeyboardButton("📋 Pending",          callback_data="aps:list:1")],
        [InlineKeyboardButton("📅 Upcoming (7 days)", callback_data="aps:upcoming")],
        [InlineKeyboardButton("📜 History",          callback_data="aps:history:1")],
        [_back("acc:root")],
    ])
    await _edit(update, text, kb)


# ─── Pick schedule type ───────────────────────────────────────────────────────

async def aps_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    text = "🗓 <b>New Schedule</b>\n\nSelect the type of change to schedule:"
    kb_rows = [[InlineKeyboardButton(label, callback_data=f"aps:new:{t}")]
               for t, label in SCHEDULE_LABELS.items()]
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


# ─── ConvHandler: create a schedule ──────────────────────────────────────────

async def aps_conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return ConversationHandler.END
    stype = q.data.split(":")[-1]
    context.user_data["aps_type"] = stype
    label = SCHEDULE_LABELS.get(stype, stype)
    text = (
        f"🗓 <b>New {label} Schedule</b>\n\n"
        f"Step 1/3: Enter the <b>product ID</b> to schedule:"
    )
    await q.edit_message_text(text, parse_mode="HTML")
    return APS_PICK_PRODUCT


async def aps_got_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric product ID.")
        return APS_PICK_PRODUCT
    from database import get_db_session
    from database.models import Product as Prd
    product = None
    try:
        with get_db_session() as s:
            product = s.query(Prd).get(int(text))
    except Exception:
        pass
    if not product:
        await update.message.reply_text("❌ Product not found. Try again:")
        return APS_PICK_PRODUCT
    context.user_data["aps_product_id"] = int(text)
    context.user_data["aps_product_name"] = product.name
    stype = context.user_data.get("aps_type", "")
    await update.message.reply_text(
        f"✅ Product: <b>{product.name}</b>\n\n"
        f"Step 2/3: Enter <b>date &amp; time</b> in UTC format:\n"
        f"<code>YYYY-MM-DD HH:MM</code>\n\nExample: <code>2026-09-20 14:00</code>",
        parse_mode="HTML"
    )
    return APS_PICK_DATETIME


async def aps_got_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    dt = None
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            pass
    if not dt:
        await update.message.reply_text(
            "❌ Invalid format. Use <code>YYYY-MM-DD HH:MM</code> (UTC).",
            parse_mode="HTML")
        return APS_PICK_DATETIME
    if dt <= datetime.utcnow():
        await update.message.reply_text("❌ Date must be in the future.")
        return APS_PICK_DATETIME
    context.user_data["aps_execute_at"] = dt
    stype = context.user_data.get("aps_type", "")
    prompts = {
        "publish":      "Step 3/3: Add optional notes (or /skip):",
        "unpublish":    "Step 3/3: Add optional notes (or /skip):",
        "price_change": "Step 3/3: Enter new <b>price</b> (e.g. <code>29.99</code>):",
        "discount":     "Step 3/3: Enter the <b>sale price</b> (e.g. <code>19.99</code>):",
        "stock_change": "Step 3/3: Enter new <b>stock count</b> (e.g. <code>50</code>):",
    }
    await update.message.reply_text(
        prompts.get(stype, "Step 3/3: Enter value or /skip:"),
        parse_mode="HTML"
    )
    return APS_PICK_VALUE


async def aps_got_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    stype = context.user_data.get("aps_type", "")
    payload = {}
    notes = None

    if stype in ("publish", "unpublish"):
        notes = text if text != "/skip" else None
    elif stype == "price_change":
        try:
            payload["price"] = float(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Enter a number like <code>29.99</code>:",
                                            parse_mode="HTML")
            return APS_PICK_VALUE
    elif stype == "discount":
        try:
            payload["sale_price"] = float(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid sale price. Enter a number:", parse_mode="HTML")
            return APS_PICK_VALUE
    elif stype == "stock_change":
        if not text.isdigit():
            await update.message.reply_text("❌ Invalid stock count. Enter a positive integer:")
            return APS_PICK_VALUE
        payload["stock_count"] = int(text)

    admin_id = update.effective_user.id
    product_id = context.user_data.get("aps_product_id")
    execute_at = context.user_data.get("aps_execute_at")
    label = SCHEDULE_LABELS.get(stype, stype)
    pname = context.user_data.get("aps_product_name", f"ID:{product_id}")

    sched = await asyncio.to_thread(
        create_schedule, admin_id, product_id, stype, execute_at, payload, "UTC", notes
    )
    if sched:
        await update.message.reply_text(
            f"✅ <b>Schedule Created</b>\n\n"
            f"📦 Product: <b>{pname}</b>\n"
            f"🔧 Type: <b>{label}</b>\n"
            f"📅 Execute at: <b>{_fmt_dt(execute_at)}</b>\n"
            f"🆔 Schedule ID: <b>{sched.id}</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Failed to create schedule. Please try again.")
    return ConversationHandler.END


async def aps_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── List / History ───────────────────────────────────────────────────────────

async def aps_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    parts = q.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1
    data = await asyncio.to_thread(list_schedules, None, "pending", page, _PAGE)
    items = data["items"]
    pages = data["pages"]
    total = data["total"]
    if not items:
        text = "📋 <b>Pending Schedules</b>\n\nNo pending schedules."
    else:
        lines = [f"📋 <b>Pending Schedules</b> (page {page}/{pages}, total {total})\n"]
        for it in items:
            label = SCHEDULE_LABELS.get(it["schedule_type"], it["schedule_type"])
            lines.append(
                f"⏳ <b>{it['product_name'][:25]}</b> | {label}\n"
                f"   📅 {_fmt_dt(it['execute_at'])} | ID:{it['id']}"
            )
        text = "\n".join(lines)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"aps:list:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"aps:list:{page+1}"))
    kb_rows = [
        [InlineKeyboardButton(f"🗓 Details: {it['id']}", callback_data=f"aps:detail:{it['id']}")]
        for it in items[:5]
    ]
    if nav:
        kb_rows.append(nav)
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def aps_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    parts = q.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1
    data = await asyncio.to_thread(get_history, None, page, _PAGE)
    items = data["items"]
    pages = data["pages"]
    total = data["total"]
    if not items:
        text = "📜 <b>Schedule History</b>\n\nNo history yet."
    else:
        lines = [f"📜 <b>Schedule History</b> (page {page}/{pages}, total {total})\n"]
        for it in items:
            icon = STATUS_ICON.get(it["status"], "❓")
            label = SCHEDULE_LABELS.get(it["schedule_type"], it["schedule_type"])
            lines.append(
                f"{icon} <b>{it['product_name'][:25]}</b> | {label}\n"
                f"   {_fmt_dt(it.get('executed_at') or it.get('created_at'))}"
            )
        text = "\n".join(lines)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"aps:history:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"aps:history:{page+1}"))
    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def aps_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    items = await asyncio.to_thread(get_upcoming, 7, 20)
    if not items:
        text = "📅 <b>Upcoming (7 days)</b>\n\nNo upcoming schedules."
    else:
        lines = [f"📅 <b>Upcoming Schedules (next 7 days)</b>\n"]
        for it in items:
            label = SCHEDULE_LABELS.get(it["schedule_type"], it["schedule_type"])
            lines.append(
                f"⏳ {_fmt_dt(it['execute_at'])} — "
                f"<b>{it['product_name'][:20]}</b> | {label}"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[_back()]])
    await _edit(update, text, kb)


# ─── Detail / Cancel ──────────────────────────────────────────────────────────

async def aps_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    sched_id = int(q.data.split(":")[-1])
    sched = await asyncio.to_thread(get_schedule, sched_id)
    if not sched:
        await q.answer("❌ Schedule not found.", show_alert=True)
        return
    icon = STATUS_ICON.get(sched["status"], "❓")
    label = SCHEDULE_LABELS.get(sched["schedule_type"], sched["schedule_type"])
    text = (
        f"🗓 <b>Schedule #{sched['id']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Product: <b>{sched['product_name']}</b> (ID:{sched['product_id']})\n"
        f"🔧 Type: <b>{label}</b>\n"
        f"📅 Execute at: <b>{_fmt_dt(sched['execute_at'])}</b>\n"
        f"🌐 Timezone: <b>{sched['timezone_name']}</b>\n"
        f"{icon} Status: <b>{sched['status']}</b>\n"
    )
    if sched.get("result_message"):
        text += f"📝 Result: {sched['result_message']}\n"
    if sched.get("notes"):
        text += f"💬 Notes: {sched['notes']}\n"
    if sched.get("payload"):
        text += f"⚙️ Payload: <code>{sched['payload']}</code>\n"
    text += f"\n🕐 Created: {_fmt_dt(sched['created_at'])}"

    kb_rows = []
    if sched["status"] == "pending":
        kb_rows.append([InlineKeyboardButton("🚫 Cancel Schedule",
                                              callback_data=f"aps:cancel:{sched_id}")])
    kb_rows.append([_back("aps:list:1")])
    await _edit(update, text, InlineKeyboardMarkup(kb_rows))


async def aps_cancel_sched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check(update):
        return
    sched_id = int(q.data.split(":")[-1])
    admin_id = update.effective_user.id
    success = await asyncio.to_thread(cancel_schedule, sched_id, admin_id)
    if success:
        await q.answer("✅ Schedule cancelled.", show_alert=True)
    else:
        await q.answer("❌ Could not cancel — already executed or not found.", show_alert=True)
    await aps_list(update, context)


# ─── Register ─────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    # ConvHandler for new schedule creation
    create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(aps_conv_start, pattern=r"^aps:new:(publish|unpublish|price_change|discount|stock_change)$")],
        states={
            APS_PICK_PRODUCT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, aps_got_product)],
            APS_PICK_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, aps_got_datetime)],
            APS_PICK_VALUE:    [
                MessageHandler(filters.TEXT & ~filters.COMMAND, aps_got_value),
                CommandHandler("skip", aps_got_value),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(aps_cancel_conv, pattern=r"^aps:cancel_conv$"),
            CommandHandler("cancel", aps_cancel_conv),
        ],
        per_message=False,
        allow_reentry=True,
        name="aps_create",
    )
    application.add_handler(create_conv)
    application.add_handler(CallbackQueryHandler(aps_menu,        pattern=r"^aps:menu$"))
    application.add_handler(CallbackQueryHandler(aps_new,         pattern=r"^aps:new$"))
    application.add_handler(CallbackQueryHandler(aps_list,        pattern=r"^aps:list:"))
    application.add_handler(CallbackQueryHandler(aps_history,     pattern=r"^aps:history:"))
    application.add_handler(CallbackQueryHandler(aps_upcoming,    pattern=r"^aps:upcoming$"))
    application.add_handler(CallbackQueryHandler(aps_detail,      pattern=r"^aps:detail:"))
    application.add_handler(CallbackQueryHandler(aps_cancel_sched,pattern=r"^aps:cancel:\d+$"))
    logger.info("V45: Product Scheduler admin handlers registered.")
