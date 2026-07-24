"""V38 — Admin Flash Sale Manager handler.

Callback namespace: ``fsm:*``

Callbacks handled
─────────────────
fsm:menu                          — Dashboard + stats
fsm:list:STATUS:PAGE              — Paginated sale list
fsm:view:ID                       — Detail view
fsm:dup:ID                        — Duplicate
fsm:pause:ID                      — Pause active sale
fsm:resume:ID                     — Resume paused sale
fsm:end:ID                        — End now
fsm:del_ask:ID                    — Delete confirmation
fsm:del_ok:ID                     — Execute delete
fsm:preview:ID                    — Preview broadcast message
fsm:bc_send:ID:TYPE               — Manually trigger a broadcast
fsm:stats                         — Global statistics
fsm:stats:ID                      — Per-sale statistics
fsm:settings                      — Settings panel
fsm:settings:status:VAL           — Set enabled/maintenance/disabled
fsm:settings:toggle:KEY           — Toggle a bool config key
fsm:edit:ID:FIELD                 — Edit a single field (starts conversation)

ConversationHandler entry: fsm:create  — Full multi-step creation wizard
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from database import get_db_session
from database.models import FlashSaleEvent, FlashSaleBroadcastLog, Category, Product
from services import flash_sale_service as fss
from utils.helpers import is_admin
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8

# ConversationHandler states
_S_NAME      = 200
_S_SCOPE     = 201
_S_IDS       = 202
_S_DISCOUNT  = 203
_S_START     = 204
_S_END       = 205
_S_BADGE     = 206
_S_BANNER    = 207
_S_CONFIRM   = 208
# Edit wizard
_E_VALUE     = 210

_STATUS_EMOJI = {
    "draft":     "📝",
    "scheduled": "🕐",
    "active":    "🟢",
    "paused":    "⏸",
    "ended":     "🔴",
    "cancelled": "❌",
}

_SCOPE_LABELS = {
    "single_product":  "Single Product",
    "multi_product":   "Multiple Products",
    "category":        "Entire Category",
    "multi_category":  "Selected Categories",
}

_SETTINGS_BOOL_KEYS = [
    ("fsm_auto_price_update",    "🏷 Auto Price Update"),
    ("fsm_auto_broadcast",       "📢 Auto Broadcast"),
    ("fsm_countdown_timer",      "⏰ Show Countdown Timer"),
    ("fsm_homepage_banner",      "🏠 Homepage Banner"),
    ("fsm_product_badge",        "⚡ Product Page Badge"),
    ("fsm_stack_discounts",      "📦 Stack Discounts with Coupons"),
    ("fsm_allow_multiple_sales", "🔀 Allow Multiple Sales per Product"),
    ("fsm_broadcast_24h",        "📢 Broadcast: 24h Before End"),
    ("fsm_broadcast_12h",        "📢 Broadcast: 12h Before End"),
    ("fsm_broadcast_6h",         "📢 Broadcast: 6h Before End"),
    ("fsm_broadcast_3h",         "📢 Broadcast: 3h Before End"),
    ("fsm_broadcast_1h",         "📢 Broadcast: 1h Before End"),
    ("fsm_broadcast_30m",        "📢 Broadcast: 30m Before End"),
    ("fsm_broadcast_10m",        "📢 Broadcast: 10m Before End"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup,
                photo_file_id: Optional[str] = None) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(
                text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await q.message.reply_text(
                    text, reply_markup=kb, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    else:
        msg = getattr(update, "message", None)
        if msg:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)


def _sale_summary_line(fse: FlashSaleEvent) -> str:
    emoji = _STATUS_EMOJI.get(fse.status, "•")
    countdown = ""
    if fse.status == "active":
        countdown = f" ⏰ {fse.countdown()}"
    disc = f"{fse.discount_percent:.0f}%" if fse.discount_percent else f"${fse.fixed_sale_price}"
    return f"{emoji} {fse.name[:28]} [{disc}]{countdown}"


def _parse_datetime(text: str) -> Optional[datetime]:
    """Parse YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS — returns UTC datetime."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_int_list(text: str) -> List[int]:
    """Parse comma or newline-separated integers."""
    parts = text.replace("\n", ",").split(",")
    result = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            result.append(int(p))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Menu / Dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    stats = fss.get_stats()
    best  = fss.get_best_selling_sale()
    status = cfg.get("flash_sale_manager_status", "enabled")
    st_emoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")

    text = (
        "⚡ <b>Flash Sale Manager</b>\n\n"
        f"{st_emoji} Status: <b>{status.title()}</b>\n\n"
        f"📊 <b>Overview</b>\n"
        f"• Total sales: <b>{stats['total']}</b>\n"
        f"• 🟢 Active: <b>{stats['active']}</b>\n"
        f"• 🕐 Scheduled: <b>{stats['scheduled']}</b>\n"
        f"• ⏸ Paused: <b>{stats['paused']}</b>\n"
        f"• 🔴 Ended: <b>{stats['ended']}</b>\n"
        f"• 📝 Draft: <b>{stats['draft']}</b>\n\n"
        f"💰 Revenue: <b>${stats['revenue']:.2f}</b>\n"
        f"🧾 Orders: <b>{stats['total_orders']}</b>\n"
    )
    if best:
        text += f"\n🏆 Best Sale: <b>{best['name']}</b> ({best['order_count']} orders)"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Flash Sale",  callback_data="fsm:create"),
         InlineKeyboardButton("📋 All Sales",         callback_data="fsm:list:all:0")],
        [InlineKeyboardButton("🟢 Active",            callback_data="fsm:list:active:0"),
         InlineKeyboardButton("🕐 Scheduled",         callback_data="fsm:list:scheduled:0")],
        [InlineKeyboardButton("⏸ Paused",            callback_data="fsm:list:paused:0"),
         InlineKeyboardButton("🔴 Ended",             callback_data="fsm:list:ended:0")],
        [InlineKeyboardButton("📝 Drafts",            callback_data="fsm:list:draft:0"),
         InlineKeyboardButton("📊 Statistics",        callback_data="fsm:stats")],
        [InlineKeyboardButton("⚙️ Settings",          callback_data="fsm:settings")],
        [InlineKeyboardButton("🔙 Back",              callback_data="acc:root")],
    ])
    await _send(update, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# List view
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    parts  = q.data.split(":")   # fsm:list:STATUS:PAGE
    status = parts[2] if len(parts) > 2 else "all"
    page   = int(parts[3]) if len(parts) > 3 else 0

    with get_db_session() as s:
        base_q = s.query(FlashSaleEvent)
        if status != "all":
            base_q = base_q.filter(FlashSaleEvent.status == status)
        base_q = base_q.order_by(
            FlashSaleEvent.start_time.desc()
        )
        total = base_q.count()
        rows  = base_q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

    label = status.title() if status != "all" else "All"
    heading = f"⚡ <b>Flash Sales — {label}</b>  (page {page+1})\n"

    if not rows:
        text = heading + "\nNo flash sales found."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create", callback_data="fsm:create")],
            [InlineKeyboardButton("🔙 Back",   callback_data="fsm:menu")],
        ])
        await _send(update, text, kb); return

    btns = []
    for fse in rows:
        btns.append([InlineKeyboardButton(
            _sale_summary_line(fse), callback_data=f"fsm:view:{fse.id}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"fsm:list:{status}:{page-1}"))
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="fsm:menu"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"fsm:list:{status}:{page+1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="fsm:menu")])
    await _send(update, heading, InlineKeyboardMarkup(btns))


# ─────────────────────────────────────────────────────────────────────────────
# Detail view
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    fid = int(q.data.split(":")[2])
    with get_db_session() as s:
        fse = s.query(FlashSaleEvent).filter_by(id=fid).first()
        if not fse:
            await q.answer("Flash sale not found.", show_alert=True); return

        st_emoji = _STATUS_EMOJI.get(fse.status, "•")
        scope    = _SCOPE_LABELS.get(fse.scope_type, fse.scope_type)
        start    = fse.start_time.strftime("%Y-%m-%d %H:%M UTC")
        end      = fse.end_time.strftime("%Y-%m-%d %H:%M UTC")
        countdown = fse.countdown() if fse.status in ("active", "scheduled") else "—"
        disc = (f"{fse.discount_percent:.0f}% off"
                if fse.discount_percent else f"Fixed ${fse.fixed_sale_price}")

        # Count products
        import json as _json
        n_products = 0
        try:
            pids = _json.loads(fse.product_ids_json or "[]")
            cids = _json.loads(fse.category_ids_json or "[]")
            n_products = len(pids) or len(cids)
        except Exception:
            pass

        text = (
            f"⚡ <b>{fse.name}</b>\n\n"
            f"{st_emoji} Status: <b>{fse.status.title()}</b>\n"
            f"🏷 Badge: {fse.badge_text or 'None'}\n"
            f"📦 Scope: {scope}  ({n_products} item(s))\n"
            f"Discount: <b>{disc}</b>\n"
            f"🗓 Start: {start}\n"
            f"🗓 End: {end}\n"
            f"⏰ Countdown: <b>{countdown}</b>\n"
            f"🌍 Timezone: {fse.timezone}\n"
            f"⭐ Priority: {fse.priority}\n"
            f"🏠 Homepage: {'Yes' if fse.show_on_homepage else 'No'}\n\n"
            f"📊 Views: {fse.view_count}  |  Clicks: {fse.click_count}  |  Orders: {fse.order_count}\n"
            f"💰 Revenue: ${fse.revenue:.2f}\n"
        )
        if fse.description:
            text += f"\n📝 {fse.description[:200]}\n"

    btns = []
    # Primary actions based on status
    if fse.status == "draft":
        btns.append([
            InlineKeyboardButton("🟢 Schedule", callback_data=f"fsm:schedule:{fid}"),
            InlineKeyboardButton("✏️ Edit",     callback_data=f"fsm:edit_menu:{fid}"),
        ])
    elif fse.status == "scheduled":
        btns.append([
            InlineKeyboardButton("▶️ Start Now",  callback_data=f"fsm:startnow:{fid}"),
            InlineKeyboardButton("✏️ Edit",       callback_data=f"fsm:edit_menu:{fid}"),
        ])
    elif fse.status == "active":
        btns.append([
            InlineKeyboardButton("⏸ Pause",    callback_data=f"fsm:pause:{fid}"),
            InlineKeyboardButton("🔴 End Now", callback_data=f"fsm:end:{fid}"),
        ])
        btns.append([
            InlineKeyboardButton("✏️ Edit",    callback_data=f"fsm:edit_menu:{fid}"),
        ])
    elif fse.status == "paused":
        btns.append([
            InlineKeyboardButton("▶️ Resume",  callback_data=f"fsm:resume:{fid}"),
            InlineKeyboardButton("🔴 End Now", callback_data=f"fsm:end:{fid}"),
        ])

    btns.append([
        InlineKeyboardButton("📋 Preview Msg",  callback_data=f"fsm:preview:{fid}"),
        InlineKeyboardButton("📊 Per-Sale Stats", callback_data=f"fsm:stats:{fid}"),
    ])
    btns.append([
        InlineKeyboardButton("📤 Duplicate",  callback_data=f"fsm:dup:{fid}"),
        InlineKeyboardButton("🗑 Delete",    callback_data=f"fsm:del_ask:{fid}"),
    ])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="fsm:list:all:0")])
    await _send(update, text, InlineKeyboardMarkup(btns))


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    ok = fss.update(fid, status="scheduled")
    await q.answer("🕐 Sale scheduled." if ok else "Failed.", show_alert=not ok)
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


async def fsm_start_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    with get_db_session() as s:
        fse = s.query(FlashSaleEvent).filter_by(id=fid).first()
        if not fse:
            await q.answer("Not found.", show_alert=True); return
        from services.flash_sale_service import _apply_prices
        if cfg.get_bool("fsm_auto_price_update", True):
            _apply_prices(s, fse)
        fse.status = "active"
        fse.updated_at = datetime.utcnow()
        s.commit()
    log_admin_action(update.effective_user.id, "flash_sale.start_now", target_id=str(fid))
    await q.answer("🟢 Sale is now active!")
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


async def fsm_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    ok = fss.pause(fid)
    if ok:
        log_admin_action(update.effective_user.id, "flash_sale.pause", target_id=str(fid))
    await q.answer("⏸ Paused." if ok else "Could not pause.")
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


async def fsm_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    ok = fss.resume(fid)
    if ok:
        log_admin_action(update.effective_user.id, "flash_sale.resume", target_id=str(fid))
    await q.answer("▶️ Resumed." if ok else "Could not resume.")
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


async def fsm_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    ok = fss.end_now(fid, bot=context.bot)
    if ok:
        log_admin_action(update.effective_user.id, "flash_sale.end_now", target_id=str(fid))
    await q.answer("🔴 Sale ended." if ok else "Failed.")
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


async def fsm_dup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    copy = fss.duplicate(fid, created_by=update.effective_user.id)
    if copy:
        log_admin_action(update.effective_user.id, "flash_sale.duplicate",
                         target_id=str(fid), details=f"New id={copy.id}")
        await q.answer(f"📤 Duplicated as #{copy.id}")
        await fsm_view(with_data(update, f"fsm:view:{copy.id}"), context)
    else:
        await q.answer("Failed to duplicate.", show_alert=True)


async def fsm_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return
    fid = int(q.data.split(":")[2])
    text = (
        "⚠️ <b>Delete Flash Sale</b>\n\n"
        "This will permanently delete the flash sale.\n"
        "If active, prices will be restored first.\n\n"
        "Are you sure?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"fsm:del_ok:{fid}")],
        [InlineKeyboardButton("🔙 Cancel",      callback_data=f"fsm:view:{fid}")],
    ])
    await _send(update, text, kb)


async def fsm_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    fid = int(q.data.split(":")[2])
    ok = fss.delete_sale(fid)
    if ok:
        log_admin_action(update.effective_user.id, "flash_sale.delete", target_id=str(fid))
        await q.answer("🗑 Deleted.")
    else:
        await q.answer("Not found.", show_alert=True)
    await fsm_list(with_data(update, "fsm:list:all:0"), context)


async def fsm_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    fid = int(q.data.split(":")[2])
    with get_db_session() as s:
        fse = s.query(FlashSaleEvent).filter_by(id=fid).first()
        if not fse:
            await q.answer("Not found.", show_alert=True); return

        template = fse.message_template or cfg.get(
            "fsm_default_message_template",
            "⚡ <b>FLASH SALE</b>\n\n{product_name}\n\n${old_price} → {sale_price}"
        )

        # Resolve sample product
        import json as _json
        pids = _json.loads(fse.product_ids_json or "[]")
        prod_name = fse.name
        old_price = "99.99"
        sale_price = "49.99"
        disc_str = f"{fse.discount_percent:.0f}" if fse.discount_percent else "50"

        if pids:
            prod = s.query(Product).filter_by(id=pids[0]).first()
            if prod:
                prod_name = prod.name
                old_price = f"{prod.price:.2f}"
                if fse.discount_percent:
                    sp = round(float(prod.price) * (1 - fse.discount_percent/100), 2)
                    sale_price = f"{sp:.2f}"
                    disc_str = f"{fse.discount_percent:.0f}"
                elif fse.fixed_sale_price:
                    sale_price = f"{fse.fixed_sale_price:.2f}"

        text = template.format(
            product_name=prod_name,
            old_price=old_price,
            sale_price=sale_price,
            discount_percent=disc_str,
            countdown=fse.countdown(),
            badge=fse.badge_text or "⚡ FLASH SALE",
            sale_name=fse.name,
        )

    preview_text = f"📋 <b>Preview of broadcast message:</b>\n\n{text}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Send Now (Start)", callback_data=f"fsm:bc_send:{fid}:start")],
        [InlineKeyboardButton("🔙 Back",             callback_data=f"fsm:view:{fid}")],
    ])
    await _send(update, preview_text, kb)


async def fsm_bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger a broadcast for a flash sale."""
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    parts = q.data.split(":")  # fsm:bc_send:ID:TYPE
    fid = int(parts[2])
    bc_type = parts[3] if len(parts) > 3 else "start"
    await q.answer("📢 Sending broadcast…")
    from services.flash_sale_service import _send_broadcast
    await _send_broadcast(fid, bc_type, context.bot)
    log_admin_action(update.effective_user.id, "flash_sale.manual_broadcast",
                     target_id=str(fid), details=f"type={bc_type}")
    await q.answer("📢 Broadcast sent!")
    await fsm_view(with_data(update, f"fsm:view:{fid}"), context)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    # Check if per-sale stats requested
    parts = q.data.split(":")
    if len(parts) > 2:
        await fsm_stats_single(update, context); return

    stats = fss.get_stats()
    best  = fss.get_best_selling_sale()

    text = (
        "📊 <b>Flash Sale Manager Statistics</b>\n\n"
        f"📋 Total Sales: <b>{stats['total']}</b>\n"
        f"🟢 Active: <b>{stats['active']}</b>\n"
        f"🕐 Scheduled: <b>{stats['scheduled']}</b>\n"
        f"⏸ Paused: <b>{stats['paused']}</b>\n"
        f"🔴 Ended: <b>{stats['ended']}</b>\n"
        f"📝 Draft: <b>{stats['draft']}</b>\n"
        f"❌ Cancelled: <b>{stats['cancelled']}</b>\n\n"
        f"💰 Total Revenue: <b>${stats['revenue']:.2f}</b>\n"
        f"🧾 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"👁 Total Views: <b>{stats['total_views']}</b>\n"
        f"🖱 Total Clicks: <b>{stats['total_clicks']}</b>\n"
    )
    if stats['total_clicks'] > 0:
        conv = stats['total_orders'] / stats['total_clicks'] * 100
        text += f"📈 Conversion Rate: <b>{conv:.1f}%</b>\n"
    if best:
        text += f"\n🏆 Best Sale: <b>{best['name']}</b>\n"
        text += f"   Orders: {best['order_count']}  |  Revenue: ${best['revenue']:.2f}\n"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="fsm:menu")]])
    await _send(update, text, kb)


async def fsm_stats_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split(":")   # fsm:stats:ID
    fid = int(parts[2])

    with get_db_session() as s:
        fse = s.query(FlashSaleEvent).filter_by(id=fid).first()
        if not fse:
            await q.answer("Not found.", show_alert=True); return

        logs = s.query(FlashSaleBroadcastLog).filter_by(
            flash_sale_event_id=fid
        ).order_by(FlashSaleBroadcastLog.sent_at.asc()).all()

        log_lines = []
        for l in logs:
            ts = l.sent_at.strftime("%m/%d %H:%M")
            log_lines.append(f"  📢 {l.broadcast_type}: {l.recipients} recipients  [{ts}]")

        disc = (f"{fse.discount_percent:.0f}%"
                if fse.discount_percent else f"${fse.fixed_sale_price}")
        text = (
            f"📊 <b>Stats: {fse.name}</b>\n\n"
            f"Status: {_STATUS_EMOJI.get(fse.status, '')} {fse.status.title()}\n"
            f"Discount: <b>{disc}</b>\n"
            f"Countdown: <b>{fse.countdown()}</b>\n\n"
            f"👁 Views: <b>{fse.view_count}</b>\n"
            f"🖱 Clicks: <b>{fse.click_count}</b>\n"
            f"🧾 Orders: <b>{fse.order_count}</b>\n"
            f"💰 Revenue: <b>${fse.revenue:.2f}</b>\n\n"
            f"<b>Broadcasts Sent:</b>\n"
        )
        if log_lines:
            text += "\n".join(log_lines)
        else:
            text += "  None yet."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Sale", callback_data=f"fsm:view:{fid}")],
    ])
    await _send(update, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# Edit menu
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    fid = int(q.data.split(":")[2])
    text = (
        f"✏️ <b>Edit Flash Sale #{fid}</b>\n\n"
        "Select a field to edit:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Name",           callback_data=f"fsm:edit:{fid}:name"),
         InlineKeyboardButton("🏷 Badge Text",     callback_data=f"fsm:edit:{fid}:badge")],
        [InlineKeyboardButton("Discount %",     callback_data=f"fsm:edit:{fid}:discount"),
         InlineKeyboardButton("💵 Fixed Price",    callback_data=f"fsm:edit:{fid}:fixed_price")],
        [InlineKeyboardButton("🗓 Start Time",     callback_data=f"fsm:edit:{fid}:start"),
         InlineKeyboardButton("🗓 End Time",       callback_data=f"fsm:edit:{fid}:end")],
        [InlineKeyboardButton("📝 Description",    callback_data=f"fsm:edit:{fid}:description"),
         InlineKeyboardButton("📨 Msg Template",   callback_data=f"fsm:edit:{fid}:template")],
        [InlineKeyboardButton("⭐ Priority",       callback_data=f"fsm:edit:{fid}:priority"),
         InlineKeyboardButton("🌍 Timezone",       callback_data=f"fsm:edit:{fid}:timezone")],
        [InlineKeyboardButton("🔙 Back",           callback_data=f"fsm:view:{fid}")],
    ])
    await _send(update, text, kb)


async def fsm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the edit conversation."""
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return ConversationHandler.END

    parts = q.data.split(":")  # fsm:edit:ID:FIELD
    fid   = int(parts[2])
    field = parts[3] if len(parts) > 3 else "name"
    context.user_data["fsm_edit_id"]    = fid
    context.user_data["fsm_edit_field"] = field

    prompts = {
        "name":        "Enter new sale <b>name</b>:",
        "badge":       "Enter new <b>badge text</b> (e.g. ⚡ FLASH SALE):",
        "discount":    "Enter <b>discount %</b> (e.g. 25 for 25% off):",
        "fixed_price": "Enter <b>fixed sale price</b> (e.g. 9.99):",
        "start":       "Enter <b>start time</b> (YYYY-MM-DD HH:MM, UTC):",
        "end":         "Enter <b>end time</b> (YYYY-MM-DD HH:MM, UTC):",
        "description": "Enter new <b>description</b> (or - to clear):",
        "template":    "Enter new <b>message template</b>.\nSupports: {product_name}, {old_price}, {sale_price}, {discount_percent}, {countdown}, {badge}",
        "priority":    "Enter new <b>priority</b> (integer, higher = more important):",
        "timezone":    "Enter <b>timezone</b> (e.g. UTC, US/Eastern, Europe/London):",
    }
    prompt = prompts.get(field, f"Enter new value for <b>{field}</b>:")
    await q.message.reply_text(
        f"✏️ {prompt}\n\nSend /cancel to abort.",
        parse_mode="HTML",
    )
    return _E_VALUE


async def fsm_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new value for the field being edited."""
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END

    fid   = context.user_data.pop("fsm_edit_id", None)
    field = context.user_data.pop("fsm_edit_field", None)
    raw   = update.message.text.strip()

    if not fid or not field:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END

    field_map = {
        "name":        ("name",             lambda v: v[:255]),
        "badge":       ("badge_text",       lambda v: v[:64]),
        "discount":    ("discount_percent", float),
        "fixed_price": ("fixed_sale_price", float),
        "start":       ("start_time",       _parse_datetime),
        "end":         ("end_time",         _parse_datetime),
        "description": ("description",      lambda v: None if v == "-" else v),
        "template":    ("message_template", lambda v: v),
        "priority":    ("priority",         int),
        "timezone":    ("timezone",         lambda v: v[:64]),
    }
    if field not in field_map:
        await update.message.reply_text("❌ Unknown field.")
        return ConversationHandler.END

    db_field, converter = field_map[field]
    try:
        value = converter(raw)
        if value is None and field not in ("description",):
            await update.message.reply_text("❌ Invalid format. Please try again.")
            return ConversationHandler.END
        ok = fss.update(fid, **{db_field: value})
        if ok:
            log_admin_action(update.effective_user.id, "flash_sale.edit",
                             target_id=str(fid), details=f"field={field}")
            await update.message.reply_text(f"✅ <b>{field.title()}</b> updated.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Update failed (sale not found).")
    except (ValueError, TypeError) as e:
        await update.message.reply_text(f"❌ Invalid value: {e}")
    return ConversationHandler.END


async def fsm_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("fsm_edit_id", None)
    context.user_data.pop("fsm_edit_field", None)
    await update.message.reply_text("❌ Edit cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Create wizard (multi-step ConversationHandler)
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — admin clicks 'Create Flash Sale'."""
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return ConversationHandler.END

    context.user_data.clear()
    context.user_data["fsm_creating"] = True
    await q.message.reply_text(
        "⚡ <b>Create Flash Sale — Step 1/7</b>\n\n"
        "Enter the <b>name</b> for this flash sale:\n\n"
        "Example: <code>Weekend Flash Sale 🔥</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _S_NAME


async def fsm_create_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["fsm_name"] = update.message.text.strip()[:255]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Single Product",    callback_data="fsm_scope:single_product")],
        [InlineKeyboardButton("📦 Multiple Products", callback_data="fsm_scope:multi_product")],
        [InlineKeyboardButton("📂 Entire Category",  callback_data="fsm_scope:category")],
        [InlineKeyboardButton("📂 Selected Categories", callback_data="fsm_scope:multi_category")],
    ])
    await update.message.reply_text(
        "⚡ <b>Create Flash Sale — Step 2/7</b>\n\n"
        "Select <b>scope</b> — what this sale applies to:",
        reply_markup=kb, parse_mode="HTML",
    )
    return _S_SCOPE


async def fsm_create_scope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    scope = q.data.split(":")[1]
    context.user_data["fsm_scope"] = scope

    if scope in ("single_product", "multi_product"):
        prompt = (
            "⚡ <b>Step 3/7 — Products</b>\n\n"
            "Enter product ID(s) separated by commas:\n\n"
            "Example: <code>12, 45, 78</code>"
        )
    else:
        prompt = (
            "⚡ <b>Step 3/7 — Categories</b>\n\n"
            "Enter category ID(s) separated by commas:\n\n"
            "Example: <code>3, 7</code>"
        )
    await q.message.reply_text(prompt + "\n\nSend /cancel to abort.", parse_mode="HTML")
    return _S_IDS


async def fsm_create_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    raw  = update.message.text.strip()
    ids  = _parse_int_list(raw)
    if not ids:
        await update.message.reply_text("❌ No valid IDs found. Please enter integer IDs.")
        return _S_IDS
    scope = context.user_data.get("fsm_scope", "single_product")
    if scope in ("single_product", "multi_product"):
        context.user_data["fsm_product_ids"] = ids
        context.user_data["fsm_category_ids"] = []
    else:
        context.user_data["fsm_category_ids"] = ids
        context.user_data["fsm_product_ids"] = []

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💯 Percentage Discount", callback_data="fsm_disc:percent")],
        [InlineKeyboardButton("💵 Fixed Sale Price",    callback_data="fsm_disc:fixed")],
    ])
    await update.message.reply_text(
        "⚡ <b>Step 4/7 — Discount Type</b>\n\n"
        "How should the discount be calculated?",
        reply_markup=kb, parse_mode="HTML",
    )
    return _S_DISCOUNT


async def fsm_create_discount_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    disc_type = q.data.split(":")[1]
    context.user_data["fsm_disc_type"] = disc_type
    if disc_type == "percent":
        prompt = "⚡ <b>Step 4b/7 — Discount %</b>\n\nEnter the discount percentage (e.g. <code>25</code> for 25% off):"
    else:
        prompt = "⚡ <b>Step 4b/7 — Fixed Price</b>\n\nEnter the sale price (e.g. <code>9.99</code>):"
    await q.message.reply_text(prompt + "\n\nSend /cancel to abort.", parse_mode="HTML")
    return _S_DISCOUNT


async def fsm_create_discount_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a positive number.")
        return _S_DISCOUNT

    disc_type = context.user_data.get("fsm_disc_type", "percent")
    if disc_type == "percent":
        if val >= 100:
            await update.message.reply_text("❌ Discount must be less than 100%.")
            return _S_DISCOUNT
        context.user_data["fsm_discount_percent"] = val
        context.user_data["fsm_fixed_sale_price"] = None
    else:
        context.user_data["fsm_fixed_sale_price"] = val
        context.user_data["fsm_discount_percent"] = None

    await update.message.reply_text(
        "⚡ <b>Step 5/7 — Start Time</b>\n\n"
        "Enter start time in UTC:\n<code>YYYY-MM-DD HH:MM</code>\n\n"
        "Example: <code>2026-09-15 08:00</code>\n\nSend /cancel to abort.",
        parse_mode="HTML",
    )
    return _S_START


async def fsm_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    dt = _parse_datetime(update.message.text.strip())
    if not dt:
        await update.message.reply_text(
            "❌ Invalid format. Use: <code>YYYY-MM-DD HH:MM</code>", parse_mode="HTML"
        )
        return _S_START
    context.user_data["fsm_start_time"] = dt
    await update.message.reply_text(
        "⚡ <b>Step 6/7 — End Time</b>\n\n"
        "Enter end time in UTC:\n<code>YYYY-MM-DD HH:MM</code>\n\n"
        "Example: <code>2026-09-15 20:00</code>\n\nSend /cancel to abort.",
        parse_mode="HTML",
    )
    return _S_END


async def fsm_create_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    dt = _parse_datetime(update.message.text.strip())
    if not dt:
        await update.message.reply_text(
            "❌ Invalid format. Use: <code>YYYY-MM-DD HH:MM</code>", parse_mode="HTML"
        )
        return _S_END
    start = context.user_data.get("fsm_start_time")
    if start and dt <= start:
        await update.message.reply_text("❌ End time must be after start time.")
        return _S_END
    context.user_data["fsm_end_time"] = dt
    await update.message.reply_text(
        "⚡ <b>Step 7/7 — Badge Text</b>\n\n"
        "Enter badge text shown on the sale (or send <code>-</code> to use default):\n\n"
        "Example: <code>⚡ FLASH SALE 🔥</code>\n\nSend /cancel to abort.",
        parse_mode="HTML",
    )
    return _S_BADGE


async def fsm_create_badge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    raw = update.message.text.strip()
    context.user_data["fsm_badge"] = None if raw == "-" else raw[:64]
    await update.message.reply_text(
        "📸 <b>Banner Image</b> (Optional)\n\n"
        "Send a photo for the flash sale banner, or send <code>-</code> to skip.",
        parse_mode="HTML",
    )
    return _S_BANNER


async def fsm_create_banner_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    photo = update.message.photo
    if photo:
        context.user_data["fsm_banner_file_id"] = photo[-1].file_id
    return await _fsm_do_create(update, context)


async def fsm_create_banner_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _require_admin(update.effective_user.id):
        return ConversationHandler.END
    raw = update.message.text.strip()
    if raw != "-":
        await update.message.reply_text(
            "Please send a photo or type <code>-</code> to skip.", parse_mode="HTML"
        )
        return _S_BANNER
    context.user_data["fsm_banner_file_id"] = None
    return await _fsm_do_create(update, context)


async def _fsm_do_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute sale creation with all collected data."""
    uid = update.effective_user.id
    ud = context.user_data

    try:
        fse = fss.create(
            name=ud.get("fsm_name", "Flash Sale"),
            scope_type=ud.get("fsm_scope", "single_product"),
            start_time=ud["fsm_start_time"],
            end_time=ud["fsm_end_time"],
            product_ids=ud.get("fsm_product_ids", []),
            category_ids=ud.get("fsm_category_ids", []),
            discount_percent=ud.get("fsm_discount_percent"),
            fixed_sale_price=ud.get("fsm_fixed_sale_price"),
            badge_text=ud.get("fsm_badge", "⚡ FLASH SALE"),
            banner_file_id=ud.get("fsm_banner_file_id"),
            broadcast_on_start=True,
            broadcast_1h=True,
            broadcast_24h=True,
            created_by=uid,
        )
        if fse:
            log_admin_action(uid, "flash_sale.create", target_id=str(fse.id),
                             details=f"name={fse.name}")
            disc = (f"{fse.discount_percent:.0f}%"
                    if fse.discount_percent else f"${fse.fixed_sale_price}")
            await update.message.reply_text(
                f"✅ <b>Flash Sale Created!</b>\n\n"
                f"🆔 ID: #{fse.id}\n"
                f"📝 Name: {fse.name}\n"
                f"Discount: {disc}\n"
                f"🕐 Status: {fse.status.title()}\n\n"
                f"Use /admin → Flash Sale Manager to manage it.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ Failed to create flash sale. Check the values and try again.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        for k in list(context.user_data.keys()):
            if k.startswith("fsm_"):
                context.user_data.pop(k, None)
    return ConversationHandler.END


async def fsm_create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in list(context.user_data.keys()):
        if k.startswith("fsm_"):
            context.user_data.pop(k, None)
    await update.message.reply_text("❌ Flash sale creation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

async def fsm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update); return

    status = cfg.get("flash_sale_manager_status", "enabled")
    lines = ["⚙️ <b>Flash Sale Manager Settings</b>\n",
             f"Status: <b>{status.title()}</b>\n"]
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        lines.append(f"{'✅' if val else '❌'} {label}")

    text = "\n".join(lines)
    btns = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="fsm:settings:status:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="fsm:settings:status:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="fsm:settings:status:disabled")],
    ]
    for key, label in _SETTINGS_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        btns.append([InlineKeyboardButton(
            f"{'✅' if val else '❌'} {label}",
            callback_data=f"fsm:settings:toggle:{key}",
        )])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="fsm:menu")])
    await _send(update, text, InlineKeyboardMarkup(btns))


async def fsm_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    val = q.data.split(":")[3]
    cfg.set("flash_sale_manager_status", val)
    log_admin_action(update.effective_user.id, "flash_sale_manager.set_status", new_value=val)
    await q.answer(f"Status set to {val}.")
    await fsm_settings(with_data(update, "fsm:settings"), context)


async def fsm_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _require_admin(update.effective_user.id):
        await q.answer("⛔ Access denied.", show_alert=True); return
    key = ":".join(q.data.split(":")[3:])
    new_val = not cfg.get_bool(key, True)
    cfg.set(key, new_val)
    log_admin_action(update.effective_user.id, "flash_sale_manager.toggle",
                     target_id=key, new_value=str(new_val))
    await q.answer(f"{'✅ Enabled' if new_val else '❌ Disabled'}")
    await fsm_settings(with_data(update, "fsm:settings"), context)


# ─────────────────────────────────────────────────────────────────────────────
# ConversationHandler builders
# ─────────────────────────────────────────────────────────────────────────────

def build_fsm_create_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(fsm_create_entry, pattern=r"^fsm:create$")],
        states={
            _S_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_name),
            ],
            _S_SCOPE: [
                CallbackQueryHandler(fsm_create_scope, pattern=r"^fsm_scope:.+$"),
            ],
            _S_IDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_ids),
            ],
            _S_DISCOUNT: [
                CallbackQueryHandler(fsm_create_discount_type, pattern=r"^fsm_disc:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_discount_value),
            ],
            _S_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_start),
            ],
            _S_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_end),
            ],
            _S_BADGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_badge),
            ],
            _S_BANNER: [
                MessageHandler(filters.PHOTO, fsm_create_banner_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_create_banner_skip),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r"^/cancel$"), fsm_create_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_fsm_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(fsm_edit_start, pattern=r"^fsm:edit:\d+:.+$")],
        states={
            _E_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_edit_value),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r"^/cancel$"), fsm_edit_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all fsm:* handlers and conversations."""
    # ConversationHandlers first (so they claim entry points before plain handlers)
    application.add_handler(build_fsm_create_conv())
    application.add_handler(build_fsm_edit_conv())

    # Plain callbacks
    application.add_handler(CallbackQueryHandler(fsm_menu,            pattern=r"^fsm:menu$"))
    application.add_handler(CallbackQueryHandler(fsm_list,            pattern=r"^fsm:list:.+:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_view,            pattern=r"^fsm:view:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_schedule,        pattern=r"^fsm:schedule:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_start_now,       pattern=r"^fsm:startnow:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_pause,           pattern=r"^fsm:pause:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_resume,          pattern=r"^fsm:resume:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_end,             pattern=r"^fsm:end:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_dup,             pattern=r"^fsm:dup:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_del_ask,         pattern=r"^fsm:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_del_ok,          pattern=r"^fsm:del_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_preview,         pattern=r"^fsm:preview:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_bc_send,         pattern=r"^fsm:bc_send:\d+:.+$"))
    application.add_handler(CallbackQueryHandler(fsm_stats,           pattern=r"^fsm:stats$"))
    application.add_handler(CallbackQueryHandler(fsm_stats,           pattern=r"^fsm:stats:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_edit_menu,       pattern=r"^fsm:edit_menu:\d+$"))
    application.add_handler(CallbackQueryHandler(fsm_settings,        pattern=r"^fsm:settings$"))
    application.add_handler(CallbackQueryHandler(fsm_settings_status, pattern=r"^fsm:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(fsm_settings_toggle, pattern=r"^fsm:settings:toggle:.+$"))
