"""Admin Promotions panel — a scheduled wrapper over existing Coupons,
plus V15 Flash Sales (time-boxed % discounts on a product or category).

Flash Sales are a separate, self-contained feature from the Coupon-backed
Promotions above them: they write straight to ``FlashSale`` and are read by
``services/pricing.py`` on every price computation, so — unlike Promotions —
they actually change what a buyer pays without requiring a coupon code.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, Promotion, FlashSale, Product, Category
from utils.permissions import has_permission
from utils.audit import log_admin_action
from utils.bot_config import cfg
from telegram.error import BadRequest


def _kb_back(cb="acc:sec:promotions"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


async def promotions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not cfg.get_bool("promotions_enabled", True):
        try:
            await query.edit_message_text(
                "🎁 Promotions are currently disabled in Bot Settings → Promotions.",
                reply_markup=_kb_back(), parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    now = datetime.utcnow()
    active_rows, upcoming_rows, past_rows = [], [], []
    with get_db_session() as s:
        for p in s.query(Promotion).order_by(Promotion.starts_at.desc().nullslast()).limit(30):
            row = {
                "id": p.id, "name": p.name,
                "starts_at": p.starts_at, "ends_at": p.ends_at,
                "is_active": p.is_active,
                "coupon_id": p.coupon_id,
                "discount_pct": p.discount_pct,
            }
            if not p.is_active:
                past_rows.append(row)
            elif p.ends_at and p.ends_at < now:
                past_rows.append(row)
            elif p.starts_at and p.starts_at > now:
                upcoming_rows.append(row)
            else:
                active_rows.append(row)

        live_flash_count = s.query(FlashSale).filter(
            FlashSale.is_active == True,  # noqa: E712
            FlashSale.start_time <= now,
            FlashSale.end_time > now,
        ).count()

    def _fmt(rows, header):
        if not rows:
            return f"<b>{header}</b>\n— none —\n"
        out = [f"<b>{header}</b>"]
        for r in rows:
            when = ""
            if r["starts_at"] or r["ends_at"]:
                s_ = r["starts_at"].strftime("%m-%d %H:%M") if r["starts_at"] else "?"
                e_ = r["ends_at"].strftime("%m-%d %H:%M") if r["ends_at"] else "?"
                when = f"  [{s_} → {e_}]"
            disc = f"  −{r['discount_pct']:.0f}%" if r["discount_pct"] else ""
            coup = f"  🎟 #{r['coupon_id']}" if r["coupon_id"] else ""
            out.append(f"• #{r['id']} {r['name']}{disc}{coup}{when}")
        return "\n".join(out) + "\n"

    text = (
        "🎁 <b>Promotions</b>\n\n"
        + _fmt(active_rows, "Active")
        + "\n" + _fmt(upcoming_rows, "Upcoming")
        + "\n" + _fmt(past_rows, "Past / inactive")
        + f"\n🔥 <b>Flash Sales</b>: {live_flash_count} live right now"
    )
    kb = [
        [InlineKeyboardButton("🔥 Flash Sales", callback_data="acc:promo:fs_list")],
        [InlineKeyboardButton("➕ New promotion (via coupon)",
                              callback_data="admin_coupon_add")],
        [InlineKeyboardButton("🎟 Manage coupons",
                              callback_data="admin_coupons")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    try:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# V15: Flash Sales
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_countdown(end_time, now=None) -> str:
    now = now or datetime.utcnow()
    remaining = (end_time - now).total_seconds()
    if remaining <= 0:
        return "ended"
    days, rem = divmod(int(remaining), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


def _parse_when(text: str, *, relative_to: datetime = None):
    """Accepts 'now', a bare number of hours (relative to ``relative_to`` or
    now), or an absolute 'YYYY-MM-DD HH:MM' (UTC) timestamp. Returns None on
    anything unparseable."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower() == "now":
        return datetime.utcnow()
    try:
        hours = float(text.lstrip("+"))
        base = relative_to or datetime.utcnow()
        return base + timedelta(hours=hours)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _target_label(fs: FlashSale) -> str:
    if fs.product_id:
        return f"📦 {fs.product.name}" if fs.product else "📦 (deleted product)"
    if fs.category_id:
        return f"🗂 {fs.category.name}" if fs.category else "🗂 (deleted category)"
    return "❓ (no target)"


async def flash_sales_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    now = datetime.utcnow()
    active, upcoming, past = [], [], []
    with get_db_session() as s:
        rows = s.query(FlashSale).order_by(FlashSale.start_time.desc()).limit(30).all()
        for fs in rows:
            row = dict(id=fs.id, target=_target_label(fs), pct=fs.discount_percent,
                       start=fs.start_time, end=fs.end_time)
            if not fs.is_active:
                past.append(row)
            elif fs.end_time <= now:
                past.append(row)
            elif fs.start_time > now:
                upcoming.append(row)
            else:
                row["countdown"] = _fmt_countdown(fs.end_time, now)
                active.append(row)

    def _fmt(rows, header):
        if not rows:
            return f"<b>{header}</b>\n— none —\n"
        out = [f"<b>{header}</b>"]
        for r in rows:
            when = f"{r['start'].strftime('%m-%d %H:%M')} → {r['end'].strftime('%m-%d %H:%M')}"
            extra = f"  ⏰ {r['countdown']}" if "countdown" in r else ""
            out.append(f"• #{r['id']} {r['target']}  −{r['pct']:.0f}%  [{when}]{extra}")
        return "\n".join(out) + "\n"

    text = (
        "🔥 <b>Flash Sales</b>\n\n"
        + _fmt(active, "🟢 Live now")
        + "\n" + _fmt(upcoming, "🕒 Upcoming")
        + "\n" + _fmt(past, "⚪ Ended / cancelled")
    )
    kb = [
        [InlineKeyboardButton("➕ New (by Product)", callback_data="acc:promo:fs_new:product"),
         InlineKeyboardButton("➕ New (by Category)", callback_data="acc:promo:fs_new:category")],
    ]
    for r in (active + upcoming)[:10]:
        kb.append([InlineKeyboardButton(
            f"⚙️ #{r['id']} {r['target'][:24]} (−{r['pct']:.0f}%)",
            callback_data=f"acc:promo:fs_view:{r['id']}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:sec:promotions")])
    try:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def flash_sale_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, fs_id: int):
    query = update.callback_query
    now = datetime.utcnow()
    with get_db_session() as s:
        fs = s.get(FlashSale, fs_id)
        if not fs:
            try:
                await query.edit_message_text("❌ Flash sale not found.",
                                              reply_markup=_kb_back("acc:promo:fs_list"))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        target = _target_label(fs)
        if not fs.is_active:
            status = "🚫 Cancelled"
        elif now >= fs.end_time:
            status = "⚪ Ended"
        elif fs.start_time > now:
            status = f"🕒 Upcoming (starts {fs.start_time.strftime('%Y-%m-%d %H:%M')} UTC)"
        else:
            status = f"🟢 Live — {_fmt_countdown(fs.end_time, now)}"
        text = (
            f"🔥 <b>Flash Sale #{fs.id}</b>\n\n"
            f"Target: {target}\n"
            f"Discount: <b>−{fs.discount_percent:.0f}%</b>\n"
            f"Window: {fs.start_time.strftime('%Y-%m-%d %H:%M')} → "
            f"{fs.end_time.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Status: {status}"
        )
        can_edit = fs.is_active and now < fs.end_time

    kb = []
    if can_edit:
        kb.append([
            InlineKeyboardButton("✏️ Edit discount %", callback_data=f"acc:promo:fs_edit_pct:{fs_id}"),
            InlineKeyboardButton("⏱ Edit end time", callback_data=f"acc:promo:fs_edit_end:{fs_id}"),
        ])
        kb.append([InlineKeyboardButton("🚫 Cancel this sale", callback_data=f"acc:promo:fs_cancel:{fs_id}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:promo:fs_list")])
    try:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                          parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


# ── Creation flow (ConversationHandler; entry via acc:promo:fs_new:product|category) ──
FS_TARGET_ID, FS_DISCOUNT, FS_START, FS_END = range(4)


async def fs_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    target_type = (query.data or "").split(":")[-1]  # "product" | "category"
    if target_type not in ("product", "category"):
        return ConversationHandler.END
    context.user_data["_fs_target_type"] = target_type

    with get_db_session() as s:
        if target_type == "product":
            rows = s.query(Product).filter_by(is_active=True).order_by(Product.id).limit(15).all()
            listing = "\n".join(f"#{p.id} — {p.name}" for p in rows) or "(no products yet)"
            prompt = "📦 Enter the Product ID to put on flash sale:"
        else:
            rows = s.query(Category).order_by(Category.id).limit(15).all()
            listing = "\n".join(f"#{c.id} — {c.name}" for c in rows) or "(no categories yet)"
            prompt = "🗂 Enter the Category ID to put on flash sale (discounts every product in it):"

    try:
        await query.edit_message_text(
            f"🔥 <b>New Flash Sale</b>\n\n{prompt}\n\n<i>Reference:</i>\n{listing}\n\nSend /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return FS_TARGET_ID


async def fs_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("Not a number. Enter the ID again:")
        return FS_TARGET_ID

    target_type = context.user_data.get("_fs_target_type")
    with get_db_session() as s:
        if target_type == "product":
            obj = s.get(Product, target_id)
        else:
            obj = s.get(Category, target_id)
    if not obj:
        await update.message.reply_text(f"❌ No {target_type} with ID {target_id}. Enter again:")
        return FS_TARGET_ID

    context.user_data["_fs_target_id"] = target_id
    await update.message.reply_text(
        f"✅ Target: {obj.name}\n\n💸 Enter the discount percent (1-95):"
    )
    return FS_DISCOUNT


async def fs_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().rstrip("%")
    try:
        pct = float(text)
    except ValueError:
        await update.message.reply_text("Not a number. Enter a discount percent (1-95):")
        return FS_DISCOUNT
    if not (0 < pct <= 95):
        await update.message.reply_text("Must be between 1 and 95. Enter again:")
        return FS_DISCOUNT

    context.user_data["_fs_discount"] = pct
    await update.message.reply_text(
        "⏰ When should the sale start?\n\n"
        "Send <b>now</b>, a number of hours from now (e.g. <b>2</b>), "
        "or an absolute UTC time as <b>YYYY-MM-DD HH:MM</b>.",
        parse_mode="HTML",
    )
    return FS_START


async def fs_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    when = _parse_when(update.message.text)
    if when is None:
        await update.message.reply_text(
            "Couldn't read that. Send 'now', a number of hours, or 'YYYY-MM-DD HH:MM':"
        )
        return FS_START

    context.user_data["_fs_start"] = when
    await update.message.reply_text(
        "⏳ How long should it run?\n\n"
        "Send a duration in hours (e.g. <b>3</b> or <b>0.5</b>), "
        "or an absolute end time as <b>YYYY-MM-DD HH:MM</b> (UTC).",
        parse_mode="HTML",
    )
    return FS_END


async def fs_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = context.user_data.get("_fs_start")
    end = _parse_when(update.message.text, relative_to=start)
    if end is None:
        await update.message.reply_text(
            "Couldn't read that. Send a number of hours, or 'YYYY-MM-DD HH:MM':"
        )
        return FS_END
    if end <= start:
        await update.message.reply_text("End time must be after the start time. Enter again:")
        return FS_END

    target_type = context.user_data.pop("_fs_target_type", None)
    target_id = context.user_data.pop("_fs_target_id", None)
    pct = context.user_data.pop("_fs_discount", None)
    context.user_data.pop("_fs_start", None)

    if not target_type or not target_id or pct is None:
        await update.message.reply_text("Session lost. Please start again from 🔥 Flash Sales.")
        return ConversationHandler.END

    with get_db_session() as s:
        fs = FlashSale(
            product_id=(target_id if target_type == "product" else None),
            category_id=(target_id if target_type == "category" else None),
            discount_percent=pct,
            start_time=start,
            end_time=end,
            is_active=True,
            created_by=update.effective_user.id,
        )
        s.add(fs)
        s.flush()
        fs_id = fs.id

    log_admin_action(update.effective_user.id, "flashsale.create",
                     target_type=target_type, target_id=target_id,
                     details=f"-{pct:.0f}% {start.isoformat()}→{end.isoformat()}")

    await update.message.reply_text(
        f"✅ <b>Flash Sale #{fs_id} created!</b>\n\n"
        f"−{pct:.0f}% off, {start.strftime('%Y-%m-%d %H:%M')} → "
        f"{end.strftime('%Y-%m-%d %H:%M')} UTC.\n\n"
        "It'll show automatically on the storefront with a countdown once live.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── Edit flows (single-step conversations) ──────────────────────────────
FS_EDIT_PCT, FS_EDIT_END = range(4, 6)


async def fs_edit_pct_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    fs_id = int((query.data or "").split(":")[-1])
    context.user_data["_fs_edit_id"] = fs_id
    try:
        await query.edit_message_text(
            "✏️ Enter the new discount percent (1-95). Send /cancel to abort:"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return FS_EDIT_PCT


async def fs_edit_pct_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().rstrip("%")
    try:
        pct = float(text)
    except ValueError:
        await update.message.reply_text("Not a number. Enter again:")
        return FS_EDIT_PCT
    if not (0 < pct <= 95):
        await update.message.reply_text("Must be between 1 and 95. Enter again:")
        return FS_EDIT_PCT

    fs_id = context.user_data.pop("_fs_edit_id", None)
    with get_db_session() as s:
        fs = s.get(FlashSale, fs_id) if fs_id else None
        if not fs:
            await update.message.reply_text("❌ Flash sale not found.")
            return ConversationHandler.END
        fs.discount_percent = pct

    log_admin_action(update.effective_user.id, "flashsale.edit_pct",
                     target_type="flash_sale", target_id=fs_id, details=f"pct={pct:.0f}")
    await update.message.reply_text(f"✅ Discount updated to −{pct:.0f}%.")
    return ConversationHandler.END


async def fs_edit_end_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    fs_id = int((query.data or "").split(":")[-1])
    context.user_data["_fs_edit_id"] = fs_id
    try:
        await query.edit_message_text(
            "⏱ Enter the new end time: a number of hours from now (e.g. <b>2</b>), "
            "or an absolute UTC time as <b>YYYY-MM-DD HH:MM</b>. Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return FS_EDIT_END


async def fs_edit_end_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fs_id = context.user_data.get("_fs_edit_id")
    with get_db_session() as s:
        fs = s.get(FlashSale, fs_id) if fs_id else None
        current_start = fs.start_time if fs else None

    end = _parse_when(update.message.text, relative_to=current_start)
    if end is None:
        await update.message.reply_text("Couldn't read that. Enter again:")
        return FS_EDIT_END
    if current_start and end <= current_start:
        await update.message.reply_text("End time must be after the sale's start time. Enter again:")
        return FS_EDIT_END

    context.user_data.pop("_fs_edit_id", None)
    with get_db_session() as s:
        fs = s.get(FlashSale, fs_id) if fs_id else None
        if not fs:
            await update.message.reply_text("❌ Flash sale not found.")
            return ConversationHandler.END
        fs.end_time = end

    log_admin_action(update.effective_user.id, "flashsale.edit_end",
                     target_type="flash_sale", target_id=fs_id, details=f"end={end.isoformat()}")
    await update.message.reply_text(f"✅ End time updated to {end.strftime('%Y-%m-%d %H:%M')} UTC.")
    return ConversationHandler.END


# ── Non-conversation actions (view / list / cancel) ──────────────────────

async def route(action, rest, update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if action == "fs_list":
        await query.answer()
        await flash_sales_menu(update, context)
        return
    if action == "fs_view" and rest:
        await query.answer()
        try:
            await flash_sale_detail(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await flash_sales_menu(update, context)
        return
    if action == "fs_cancel" and rest:
        if not has_permission(update.effective_user.id, "manage_products"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        try:
            fs_id = int(rest[0])
        except (ValueError, IndexError):
            await query.answer()
            await flash_sales_menu(update, context)
            return
        with get_db_session() as s:
            fs = s.get(FlashSale, fs_id)
            if fs:
                fs.is_active = False
        log_admin_action(update.effective_user.id, "flashsale.cancel",
                         target_type="flash_sale", target_id=fs_id)
        await query.answer("🚫 Flash sale cancelled.")
        await flash_sales_menu(update, context)
        return

    # Reserved for future in-panel create/edit of the coupon-backed
    # Promotions above; for now the coupon flow is the single source of
    # truth for those discounts, so we redirect to the coupon UI.
    await query.answer()
    await promotions_menu(update, context)
