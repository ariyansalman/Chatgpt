"""Admin Broadcast Center — ``acc:bc:*`` callback namespace.

Adds a "📢 Broadcast" section to the existing Admin Control Center
(``handlers/admin_control_center.py``) with three tools:

  1. 📦 Product Broadcast          — pick an existing *active* product,
     preview a dynamically generated announcement (name / price /
     description / live stock) with a "🛒 Buy now" button, then confirm
     before it goes out.
  2. ✍️ Custom Broadcast            — compose a free-form message (Telegram
     text formatting preserved), preview it, then confirm before it goes out.
  3. ⚙️ Restock Broadcast Settings — ON/OFF switch for the automatic
     "back in stock" broadcast, persisted in the project's existing
     ``bot_config`` settings table.

Security: every entry point re-checks ``is_admin()`` against
``settings.ADMIN_TELEGRAM_ID`` — the project's one and only admin
authorization system. No parallel/second admin system is introduced, and
nothing here is reachable by non-admin users (the root Admin Control Center
keyboard is only ever shown to admins, and every handler below re-verifies
admin status independently of that, so a crafted callback from a normal user
is rejected).

Nothing is ever sent to a single user until the admin explicitly presses
"🚀 Send Broadcast" on a preview — this module never sends on its own except
for the automatic restock broadcast, which is gated behind the
"Automatic Restock Broadcast" setting and only fires on a genuine 0 → >0
stock transition (see ``send_restock_broadcast`` below).
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, User, Product, Broadcast
from services import inventory as inventory_svc
from services import customer_segmentation as seg_svc
from utils.helpers import format_price
from utils.bot_config import cfg
from utils.audit import log_admin_action
from utils.safe_conversation import safe_conversation
from utils.permissions import has_permission
from utils.perf import perf_track
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# ── Conversation states (module-local, wired in bot.py) ───────────────────
BC_CUSTOM_TEXT = 9101   # waiting for the admin to type a custom broadcast message
BC_PROD_EDIT = 9102     # waiting for the admin to type a replacement product-broadcast message

PAGE_SIZE = 8


# ─── Shared low-level helpers ─────────────────────────────────────────────

def _eligible_user_ids_sync() -> List[int]:
    """Non-banned users are the broadcast audience. Runs in a worker thread."""
    with get_db_session() as s:
        rows = s.query(User.telegram_id).filter_by(is_banned=False).all()
        return [r[0] for r in rows]


async def _send_to_ids(bot, telegram_ids: List[int], text: str, reply_markup=None,
                        parse_mode: str = "HTML"):
    """Send ``text`` to an explicit list of telegram IDs with light rate limiting.

    Returns (sent_count, failed_count, total_count).
    """
    sent = 0
    failed = 0
    for telegram_id in telegram_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id, text=text,
                reply_markup=reply_markup, parse_mode=parse_mode,
            )
            sent += 1
            # ~20 msg/sec — comfortably under Telegram's 30/sec cap.
            await asyncio.sleep(0.05)
        except Exception:
            # User may have blocked the bot, deleted their account, etc.
            failed += 1
    return sent, failed, len(telegram_ids)


async def _send_to_eligible(bot, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Send ``text`` to every eligible (non-banned) user. Used by the automatic
    restock broadcast, which always targets everyone regardless of segment.

    Returns (sent_count, failed_count, total_count).
    """
    user_ids = await asyncio.to_thread(_eligible_user_ids_sync)
    return await _send_to_ids(bot, user_ids, text, reply_markup, parse_mode)


async def _send_to_segment(bot, segment_key: str, text: str, reply_markup=None,
                            parse_mode: str = "HTML"):
    """Send ``text`` only to users belonging to ``segment_key``.

    Returns (sent_count, failed_count, total_count).
    """
    telegram_ids = await asyncio.to_thread(seg_svc.get_segment_telegram_ids, segment_key)
    return await _send_to_ids(bot, telegram_ids, text, reply_markup, parse_mode)


def _record_broadcast(message_text: str, sent_count: int) -> None:
    """Best-effort audit row in the existing ``broadcasts`` table."""
    try:
        with get_db_session() as s:
            s.add(Broadcast(message_text=(message_text or "")[:9000], sent_count=sent_count))
            s.commit()
    except Exception:
        logger.exception("Failed to record broadcast row")


def _build_product_broadcast_text(name: str, price: float, description: Optional[str],
                                   available: int) -> str:
    desc = (description or "").strip() or "—"
    return (
        "📢 <b>Product Announcement</b>\n\n"
        f"🛍 <b>{name}</b>\n\n"
        f"{desc}\n\n"
        f"💰 Price: <b>{format_price(price)}</b>\n"
        f"📦 Available: <b>{available}</b>\n\n"
        "Tap below to grab yours now 👇"
    )


def _back_kb(cb: str = "acc:sec:broadcast") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


async def _safe_edit(query, text, reply_markup=None, parse_mode="HTML"):
    try:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass


# ─── Shared audience (segment) picker — used by both Product and Custom ──
# broadcasts. Selection is stashed in ``context.user_data["bc_segment"]``
# (default ``seg_svc.SEG_ALL``) and read back when the broadcast is sent.

def _audience_line(segment_key: str, count: int) -> str:
    return f"🎯 Audience: <b>{seg_svc.segment_label(segment_key)}</b> ({count} user{'s' if count != 1 else ''})"


def _segment_picker_kb(flow: str, counts: dict, current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label, _desc in seg_svc.SEGMENT_DEFS:
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{label} ({counts.get(key, 0)})",
            callback_data=f"acc:bc:{flow}:seg:set:{key}",
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"acc:bc:{flow}:seg:back")])
    return InlineKeyboardMarkup(rows)


async def _show_segment_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str):
    """Render the audience picker for ``flow`` ("prod" or "custom")."""
    query = update.callback_query
    current = context.user_data.get("bc_segment", seg_svc.SEG_ALL)
    counts = await asyncio.to_thread(seg_svc.get_segment_counts)
    lines = ["🎯 <b>Choose Audience</b>\n"]
    for key, label, desc in seg_svc.SEGMENT_DEFS:
        lines.append(f"• <b>{label}</b> — {desc}")
    text = "\n".join(lines) + "\n\nTap a segment to target this broadcast:"
    await _safe_edit(query, text, reply_markup=_segment_picker_kb(flow, counts, current))


# ─── Root section: 📢 Broadcast ───────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        # ── Enterprise Broadcast Center ──────────────────────────────────────
        [InlineKeyboardButton("📢 Broadcast Center",           callback_data="asb:menu")],
        [
            InlineKeyboardButton("📝 Draft Broadcasts",        callback_data="asb:drafts"),
            InlineKeyboardButton("📅 Scheduled",               callback_data="asb:scheduled_list"),
        ],
        [InlineKeyboardButton("📊 Broadcast Reports",          callback_data="asb:reports")],
        # ── Advanced Broadcast Types (V44.2) ─────────────────────────────────
        [InlineKeyboardButton("🎯 Advanced Broadcast Types",   callback_data="abt:menu")],
        # ── Enterprise Analytics (V44.3) ─────────────────────────────────────
        [
            InlineKeyboardButton("📊 Broadcast Analytics",    callback_data="bca:menu"),
            InlineKeyboardButton("📜 History",                callback_data="bca:history"),
        ],
        [
            InlineKeyboardButton("📈 Reports",                callback_data="bca:period_reports"),
            InlineKeyboardButton("📤 Export",                 callback_data="bca:export_hub"),
            InlineKeyboardButton("⚠️ Failed",                 callback_data="bca:history:filter:failed"),
        ],
        # ── Quick-send tools (existing) ──────────────────────────────────────
        [InlineKeyboardButton("📦 Product Broadcast",          callback_data="acc:bc:prod:menu:0")],
        [InlineKeyboardButton("✍️ Custom Broadcast",           callback_data="acc:bc:custom:start")],
        [InlineKeyboardButton("⚙️ Restock Settings",           callback_data="acc:bc:restock:menu")],
        [InlineKeyboardButton("🛒 Marketing Automation",       callback_data="acc:bc:mkt:menu")],
        # ── Enterprise Campaign Manager (V44.4) ──────────────────────────────
        [InlineKeyboardButton("📢 Campaign Manager",           callback_data="bcm:menu")],
        [
            InlineKeyboardButton("📄 Template Library",        callback_data="bcm:templates:0"),
            InlineKeyboardButton("🤖 Automation Rules",        callback_data="bcm:automation"),
        ],
        [
            InlineKeyboardButton("📅 Campaign Scheduler",      callback_data="bcm:scheduler"),
            InlineKeyboardButton("⚙️ Campaign Settings",       callback_data="bcm:settings"),
        ],
        [InlineKeyboardButton("🔙 Back",                       callback_data="acc:root")],
    ])


@perf_track("broadcast_start")
async def broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the 📢 Enterprise Broadcast Center section (``acc:sec:broadcast``)."""
    query = update.callback_query
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    # ── Live dashboard stats ─────────────────────────────────────────────────
    try:
        from services.broadcast_service import get_broadcast_dashboard_stats
        stats = await asyncio.to_thread(get_broadcast_dashboard_stats)
        running   = stats.get("running", 0)
        scheduled = stats.get("scheduled", 0)
        completed = stats.get("completed", 0)
        failed    = stats.get("failed", 0)
        drafts    = stats.get("drafts", 0)
        today_bc  = stats.get("today", 0)
        week_bc   = stats.get("week", 0)
        month_bc  = stats.get("month", 0)
        total_sent = stats.get("total_sent", 0)
        delivery_rate = stats.get("delivery_rate", 0.0)
        avg_ms    = stats.get("avg_delivery_ms")
        retries   = stats.get("retry_pending", 0)
        avg_str   = f"{avg_ms:.0f} ms" if avg_ms else "—"
        stats_block = (
            f"📤 Running: <b>{running}</b>  |  ⏰ Scheduled: <b>{scheduled}</b>  "
            f"|  ⏸ Drafts: <b>{drafts}</b>\n"
            f"✅ Completed: <b>{completed}</b>  |  🔴 Failed: <b>{failed}</b>\n"
            f"📅 Today: <b>{today_bc}</b>  |  Week: <b>{week_bc}</b>  "
            f"|  Month: <b>{month_bc}</b>\n"
            f"📨 Total sent: <b>{total_sent:,}</b>  |  "
            f"📈 Rate: <b>{delivery_rate:.1f}%</b>  |  "
            f"⚡ Avg: <b>{avg_str}</b>\n"
            f"🔄 Retry pending: <b>{retries}</b>"
        )
    except Exception:
        logger.exception("broadcast_menu: failed to load dashboard stats")
        stats_block = "Dashboard stats unavailable."

    restock_on = cfg.get_bool("restock_broadcast_enabled", False)
    text = (
        "📢 <b>Enterprise Broadcast Center</b>\n\n"
        + stats_block + "\n\n"
        "Use <b>Broadcast Center</b> to create, schedule, duplicate, and manage all "
        "broadcasts from one place. Use the quick-send tools below for instant "
        "product or custom announcements.\n\n"
        f"🔔 Automatic Restock Broadcast: <b>{'ON' if restock_on else 'OFF'}</b>"
    )
    await _safe_edit(query, text, reply_markup=_menu_kb())


# ─── Product Broadcast ─────────────────────────────────────────────────────

def _list_active_products():
    with get_db_session() as s:
        products = (
            s.query(Product)
            .filter_by(is_active=True)
            .order_by(Product.name.asc())
            .all()
        )
        return [(p.id, p.name, p.price) for p in products]


async def _show_product_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    products = _list_active_products()
    total = len(products)
    start = page * PAGE_SIZE
    chunk = products[start:start + PAGE_SIZE]

    rows = [
        [InlineKeyboardButton(f"{name} — {format_price(price)}",
                               callback_data=f"acc:bc:prod:sel:{pid}:{page}")]
        for pid, name, price in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"acc:bc:prod:menu:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"acc:bc:prod:menu:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="acc:sec:broadcast")])

    text = "📦 <b>Product Broadcast</b>\n\nSelect an active product to broadcast:"
    if not products:
        text = "📦 <b>Product Broadcast</b>\n\nNo active products found."

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))


def _prod_preview_kb(product_id: int, segment_key: str, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy now", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton(f"🎯 Audience: {seg_svc.segment_label(segment_key)} ({count})",
                               callback_data="acc:bc:prod:seg:menu")],
        [InlineKeyboardButton("🚀 Send Broadcast", callback_data="acc:bc:prod:send")],
        [InlineKeyboardButton("✏️ Edit Message", callback_data="acc:bc:prod:edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="acc:bc:prod:cancel")],
    ])


async def _render_prod_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the current product-broadcast preview from ``context.user_data``."""
    query = update.callback_query
    product_id = context.user_data.get("bc_product_id")
    if not product_id:
        await broadcast_menu(update, context)
        return

    with get_db_session() as s:
        product = s.query(Product).filter_by(id=product_id).first()
        if not product or not product.is_active:
            if query:
                await query.answer("❌ Product not found or no longer active.", show_alert=True)
            await _show_product_list(update, context, context.user_data.get("bc_page", 0))
            return
        name, price, desc = product.name, product.price, product.description

    available = inventory_svc.count_available(product_id)
    custom = context.user_data.get("bc_custom_text")
    text = custom if custom else _build_product_broadcast_text(name, price, desc, available)

    segment = context.user_data.get("bc_segment", seg_svc.SEG_ALL)
    count = await asyncio.to_thread(seg_svc.get_segment_count, segment)
    body = "👁 <b>Preview</b> — not sent yet\n\n" + text + "\n\n" + _audience_line(segment, count)

    kb = _prod_preview_kb(product_id, segment, count)
    if query:
        await _safe_edit(query, body, reply_markup=kb)
    else:
        await update.message.reply_text(body, reply_markup=kb, parse_mode="HTML")


async def _select_product(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, page: int):
    context.user_data["bc_product_id"] = product_id
    context.user_data["bc_page"] = page
    context.user_data.pop("bc_custom_text", None)
    context.user_data.setdefault("bc_segment", seg_svc.SEG_ALL)
    await _render_prod_preview(update, context)


async def _send_product_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    product_id = context.user_data.get("bc_product_id")
    if not product_id:
        await query.answer("Session expired.", show_alert=True)
        await broadcast_menu(update, context)
        return

    with get_db_session() as s:
        product = s.query(Product).filter_by(id=product_id).first()
        if not product or not product.is_active:
            await query.answer("❌ Product not found or no longer active.", show_alert=True)
            await _show_product_list(update, context, context.user_data.get("bc_page", 0))
            return
        name, price, desc = product.name, product.price, product.description

    available = inventory_svc.count_available(product_id)
    custom = context.user_data.get("bc_custom_text")
    text = custom if custom else _build_product_broadcast_text(name, price, desc, available)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy now", callback_data=f"buy_{product_id}")]])

    segment = context.user_data.get("bc_segment", seg_svc.SEG_ALL)
    await query.answer("Sending…")
    sent, failed, total = await _send_to_segment(context.bot, segment, text, reply_markup=kb)
    _record_broadcast(text, sent)
    log_admin_action(
        update.effective_user.id, "broadcast.product.send",
        target_type="product", target_id=product_id,
        details=f"segment={segment} sent={sent} failed={failed} total={total}",
    )

    for k in ("bc_product_id", "bc_page", "bc_custom_text", "bc_segment"):
        context.user_data.pop(k, None)

    result = (
        "✅ <b>Product Broadcast Sent</b>\n\n"
        f"🛍 {name}\n"
        f"{_audience_line(segment, total)}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total in segment: {total}"
    )
    await _safe_edit(query, result, reply_markup=_back_kb())


async def _cancel_product_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    for k in ("bc_product_id", "bc_page", "bc_custom_text", "bc_segment"):
        context.user_data.pop(k, None)
    if query:
        await query.answer("Cancelled.")
    await broadcast_menu(update, context)


# ── Product broadcast: "✏️ Edit Message" conversation ─────────────────────

async def prod_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    if not context.user_data.get("bc_product_id"):
        await broadcast_menu(update, context)
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            "✏️ <b>Edit Broadcast Message</b>\n\n"
            "Send the replacement text for this product broadcast "
            "(Telegram formatting supported). The 🛒 Buy now button will still "
            "be attached automatically.\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return BC_PROD_EDIT


@safe_conversation(cleanup_keys=())
async def prod_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        return ConversationHandler.END
    if not context.user_data.get("bc_product_id"):
        await update.message.reply_text("Session expired. Please start over from the Broadcast menu.")
        return ConversationHandler.END

    text_html = update.message.text_html or update.message.text or ""
    if not text_html.strip():
        await update.message.reply_text("❌ Empty message. Please send some text, or /cancel.")
        return BC_PROD_EDIT

    context.user_data["bc_custom_text"] = text_html
    await _render_prod_preview(update, context)
    return ConversationHandler.END


# ─── Custom Broadcast ───────────────────────────────────────────────────────

async def custom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    context.user_data.pop("bc_custom_broadcast_text", None)
    context.user_data.setdefault("bc_segment", seg_svc.SEG_ALL)
    try:
        await query.edit_message_text(
            "✍️ <b>Custom Broadcast</b>\n\n"
            "Send the message you'd like to broadcast. You'll be able to pick "
            "the audience (all users or a segment) on the next screen. "
            "Telegram text formatting (bold, italic, links, etc.) is preserved.\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return BC_CUSTOM_TEXT


def _custom_preview_kb(segment_key: str, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎯 Audience: {seg_svc.segment_label(segment_key)} ({count})",
                               callback_data="acc:bc:custom:seg:menu")],
        [InlineKeyboardButton("🚀 Send Broadcast", callback_data="acc:bc:custom:send")],
        [InlineKeyboardButton("✏️ Edit Message", callback_data="acc:bc:custom:edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="acc:bc:custom:cancel")],
    ])


async def _render_custom_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the current custom-broadcast preview from ``context.user_data``."""
    query = update.callback_query
    text_html = context.user_data.get("bc_custom_broadcast_text")
    if not text_html:
        if query:
            await query.answer("Session expired.", show_alert=True)
        await broadcast_menu(update, context)
        return

    segment = context.user_data.get("bc_segment", seg_svc.SEG_ALL)
    count = await asyncio.to_thread(seg_svc.get_segment_count, segment)
    body = "👁 <b>Preview</b> — not sent yet\n\n" + text_html + "\n\n" + _audience_line(segment, count)
    kb = _custom_preview_kb(segment, count)

    if query:
        await _safe_edit(query, body, reply_markup=kb)
    else:
        await update.message.reply_text(body, reply_markup=kb, parse_mode="HTML")


@safe_conversation(cleanup_keys=("bc_custom_broadcast_text",))
async def custom_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        return ConversationHandler.END

    text_html = update.message.text_html or update.message.text or ""
    if not text_html.strip():
        await update.message.reply_text("❌ Empty message. Please send some text, or /cancel.")
        return BC_CUSTOM_TEXT

    context.user_data["bc_custom_broadcast_text"] = text_html
    context.user_data.setdefault("bc_segment", seg_svc.SEG_ALL)
    await _render_custom_preview(update, context)
    return ConversationHandler.END


async def _send_custom_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text_html = context.user_data.get("bc_custom_broadcast_text")
    if not text_html:
        await query.answer("Session expired.", show_alert=True)
        await broadcast_menu(update, context)
        return

    segment = context.user_data.get("bc_segment", seg_svc.SEG_ALL)
    await query.answer("Sending…")
    sent, failed, total = await _send_to_segment(context.bot, segment, text_html)
    _record_broadcast(text_html, sent)
    log_admin_action(
        update.effective_user.id, "broadcast.custom.send",
        target_type="broadcast", target_id=None,
        details=f"segment={segment} sent={sent} failed={failed} total={total}",
    )
    for k in ("bc_custom_broadcast_text", "bc_segment"):
        context.user_data.pop(k, None)

    result = (
        "✅ <b>Custom Broadcast Sent</b>\n\n"
        f"{_audience_line(segment, total)}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total in segment: {total}"
    )
    await _safe_edit(query, result, reply_markup=_back_kb())


async def _cancel_custom_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    for k in ("bc_custom_broadcast_text", "bc_segment"):
        context.user_data.pop(k, None)
    if query:
        await query.answer("Cancelled.")
    await broadcast_menu(update, context)


# ─── Restock Broadcast Settings ────────────────────────────────────────────

def _restock_kb() -> InlineKeyboardMarkup:
    on = cfg.get_bool("restock_broadcast_enabled", False)
    label = f"🔔 Automatic Restock Broadcast: {'ON' if on else 'OFF'}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="acc:bc:restock:tgl")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:sec:broadcast")],
    ])


async def _restock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    on = cfg.get_bool("restock_broadcast_enabled", False)
    text = (
        "⚙️ <b>Restock Broadcast Settings</b>\n\n"
        f"Current state: <b>{'ON' if on else 'OFF'}</b>\n\n"
        "When <b>ON</b>, the bot automatically broadcasts to eligible users "
        "the moment a product's available stock goes from 0 to more than 0.\n"
        "When <b>OFF</b>, no automatic restock broadcasts are ever sent.\n\n"
        "📦 Manual Product Broadcast (above) always works regardless of this setting."
    )
    await _safe_edit(query, text, reply_markup=_restock_kb())


async def _restock_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_val = not cfg.get_bool("restock_broadcast_enabled", False)
    cfg.set("restock_broadcast_enabled", new_val)
    log_admin_action(
        update.effective_user.id, "broadcast.restock.toggle",
        target_type="setting", target_id="restock_broadcast_enabled",
        details=f"new_value={new_val}",
    )
    await query.answer(f"Automatic Restock Broadcast: {'ON' if new_val else 'OFF'}")
    await _restock_menu(update, context)


# ─── Automatic restock broadcast (fired from restock/import code paths) ───

async def send_restock_broadcast(bot, product_id: int, variant_id: Optional[int] = None) -> None:
    """Best-effort automatic "back in stock" broadcast for ``product_id``.

    Callers should invoke this exactly when a product's (or one of its
    variants') available stock transitions from 0 to a positive number as a
    direct result of an admin restocking inventory. This function re-checks
    the ``restock_broadcast_enabled`` setting itself, so callers can call it
    unconditionally right after detecting a 0 → >0 transition.
    """
    try:
        if not cfg.get_bool("restock_broadcast_enabled", False):
            return
        with get_db_session() as s:
            product = s.query(Product).filter_by(id=product_id, is_active=True).first()
            if not product:
                return
            name, price, desc = product.name, product.price, product.description

        available = inventory_svc.count_available(product_id, variant_id)
        if available <= 0:
            return  # guard against races — only broadcast if truly back in stock

        text = "🔔 <b>Back in Stock!</b>\n\n" + _build_product_broadcast_text(name, price, desc, available)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy now", callback_data=f"buy_{product_id}")]])
        sent, failed, total = await _send_to_eligible(bot, text, reply_markup=kb)
        _record_broadcast(text, sent)
        logger.info(
            "Automatic restock broadcast for product_id=%s: sent=%s failed=%s total=%s",
            product_id, sent, failed, total,
        )
    except Exception:
        logger.exception("Automatic restock broadcast failed for product_id=%s", product_id)


# ─── Marketing Automation (V14): abandoned cart + win-back ────────────────
# Thin admin-panel wrapper over services/marketing_automation.py — this file
# owns no marketing logic itself, only the ON/OFF toggles, stats view, and
# manual "Run now" triggers surfaced here inside the Broadcast section.

def _mkt_kb(stats: dict) -> InlineKeyboardMarkup:
    cart_label = f"🛒 Abandoned Cart Reminders: {'ON' if stats['cart_reminders_enabled'] else 'OFF'}"
    winback_label = f"💌 Win-Back Offers: {'ON' if stats['winback_enabled'] else 'OFF'}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(cart_label, callback_data="acc:bc:mkt:tgl:cart")],
        [InlineKeyboardButton(winback_label, callback_data="acc:bc:mkt:tgl:winback")],
        [InlineKeyboardButton("▶️ Run Cart Reminders Now", callback_data="acc:bc:mkt:run:cart")],
        [InlineKeyboardButton("▶️ Run Win-Back Now", callback_data="acc:bc:mkt:run:winback")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:sec:broadcast")],
    ])


async def _mkt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import marketing_automation as mkt_svc
    query = update.callback_query
    stats = await asyncio.to_thread(mkt_svc.get_stats)
    by_type = stats["by_type_week"]
    text = (
        "🛒 <b>Marketing Automation</b>\n\n"
        "Automatically reminds users about items left in their cart, and "
        "wins back users who've gone quiet — each with its own auto-generated "
        "single-use discount coupon.\n\n"
        f"📦 Carts currently with items: <b>{stats['pending_carts']}</b>\n"
        f"📨 Touches sent (24h): <b>{stats['sent_today']}</b>  ·  (7d): <b>{stats['sent_week']}</b>\n\n"
        "<b>Last 7 days by campaign:</b>\n"
        f"  • Cart reminder (30m): <b>{by_type.get('cart_30m', 0)}</b>\n"
        f"  • Cart reminder (24h): <b>{by_type.get('cart_24h', 0)}</b>\n"
        f"  • Win-back (7d): <b>{by_type.get('winback_7d', 0)}</b>\n"
        f"  • Win-back (30d): <b>{by_type.get('winback_30d', 0)}</b>\n\n"
        "Timing, discount %, and coupon validity are tunable under "
        "⚙️ Bot Settings → 🛒 Marketing Automation."
    )
    await _safe_edit(query, text, reply_markup=_mkt_kb(stats))


async def _mkt_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, which: str):
    query = update.callback_query
    key = "marketing_cart_reminders_enabled" if which == "cart" else "marketing_winback_enabled"
    new_val = not cfg.get_bool(key, True)
    cfg.set(key, new_val)
    log_admin_action(
        update.effective_user.id, "broadcast.marketing.toggle",
        target_type="setting", target_id=key,
        details=f"new_value={new_val}",
    )
    await query.answer(f"{'Enabled' if new_val else 'Disabled'}.")
    await _mkt_menu(update, context)


async def _mkt_run_now(update: Update, context: ContextTypes.DEFAULT_TYPE, which: str):
    from services import marketing_automation as mkt_svc
    query = update.callback_query
    await query.answer("Running…")
    try:
        if which == "cart":
            outcome = await mkt_svc.send_cart_abandonment_reminders(context.bot)
            msg = (f"✅ Cart reminders sent — 30m: {outcome['stage_30m']}, "
                  f"24h: {outcome['stage_24h']}")
        else:
            outcome = await mkt_svc.send_winback_offers(context.bot)
            msg = (f"✅ Win-back offers sent — 7d: {outcome['tier_7d']}, "
                  f"30d: {outcome['tier_30d']}")
        log_admin_action(
            update.effective_user.id, f"broadcast.marketing.run.{which}",
            target_type="marketing_automation", target_id=which, details=msg,
        )
    except Exception:
        logger.exception("Manual marketing_automation run failed (%s)", which)
        msg = "❌ Run failed — check logs."
    try:
        await query.message.reply_text(msg)
    except Exception:
        pass
    await _mkt_menu(update, context)


# ─── Central `acc:bc:*` sub-action router (called from admin_control_center) ─

async def route(action: str, rest: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route every ``acc:bc:<action>:<rest...>`` callback that is not a
    ConversationHandler entry point (those are wired directly in ``bot.py``:
    ``acc:bc:prod:edit``, ``acc:bc:custom:start``, ``acc:bc:custom:edit``).
    """
    query = update.callback_query
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    sub = rest[0] if rest else None

    if action == "prod":
        if sub == "menu":
            await query.answer()
            page = int(rest[1]) if len(rest) > 1 else 0
            await _show_product_list(update, context, page)
            return
        if sub == "sel":
            await query.answer()
            product_id = int(rest[1])
            page = int(rest[2]) if len(rest) > 2 else 0
            await _select_product(update, context, product_id, page)
            return
        if sub == "send":
            await _send_product_broadcast(update, context)
            return
        if sub == "cancel":
            await _cancel_product_broadcast(update, context)
            return
        if sub == "seg":
            sub2 = rest[1] if len(rest) > 1 else None
            if sub2 == "menu":
                await query.answer()
                await _show_segment_picker(update, context, "prod")
                return
            if sub2 == "set":
                key = rest[2] if len(rest) > 2 else seg_svc.SEG_ALL
                context.user_data["bc_segment"] = key
                await query.answer("Audience updated.")
                await _render_prod_preview(update, context)
                return
            if sub2 == "back":
                await query.answer()
                await _render_prod_preview(update, context)
                return

    if action == "custom":
        if sub == "send":
            await _send_custom_broadcast(update, context)
            return
        if sub == "cancel":
            await _cancel_custom_broadcast(update, context)
            return
        if sub == "seg":
            sub2 = rest[1] if len(rest) > 1 else None
            if sub2 == "menu":
                await query.answer()
                await _show_segment_picker(update, context, "custom")
                return
            if sub2 == "set":
                key = rest[2] if len(rest) > 2 else seg_svc.SEG_ALL
                context.user_data["bc_segment"] = key
                await query.answer("Audience updated.")
                await _render_custom_preview(update, context)
                return
            if sub2 == "back":
                await query.answer()
                await _render_custom_preview(update, context)
                return

    if action == "restock":
        if sub == "menu":
            await query.answer()
            await _restock_menu(update, context)
            return
        if sub == "tgl":
            await _restock_toggle(update, context)
            return

    if action == "mkt":
        if sub == "menu":
            await query.answer()
            await _mkt_menu(update, context)
            return
        if sub == "tgl":
            which = rest[1] if len(rest) > 1 else ""
            if which in ("cart", "winback"):
                await _mkt_toggle(update, context, which)
                return
        if sub == "run":
            which = rest[1] if len(rest) > 1 else ""
            if which in ("cart", "winback"):
                await _mkt_run_now(update, context, which)
                return

    # Unknown — fall back to the Broadcast section root.
    await query.answer()
    await broadcast_menu(update, context)
