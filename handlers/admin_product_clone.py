"""Admin Product Clone & Template System — V28.

Full clone system: single-product clone, bulk clone by category, clone
with price/stock/visibility overrides, save-as-template, create from
template, edit/duplicate/delete templates, search, statistics dashboard,
and settings panel.

Callback namespace: ``pct:*``
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import (
    Product, ProductVariant, ProductFAQ, Category,
    ProductTemplate, ProductCloneLog, Coupon,
)
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from config.settings import settings
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

PAGE_SIZE = 8

# Conversation states
(
    PCT_TPL_NAME,        # 0 — enter template name when saving
    PCT_TPL_DESC,        # 1 — enter template description (optional)
    PCT_CLONE_NAME,      # 2 — enter clone name override
    PCT_BULK_PRICE_ADJ,  # 3 — enter bulk price adjustment
) = range(4)


# ── Auth ──────────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return (uid == settings.ADMIN_TELEGRAM_ID
            or has_permission(uid, "manage_products"))


def _enabled() -> bool:
    return cfg.get("product_clone_status", "enabled") == "enabled"


# ── Helpers ────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back(data: str = "pct:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


def _max_templates() -> int:
    v = cfg.get_int("product_template_max", 50)
    return v if v > 0 else 999999


# ── Snapshot helpers ───────────────────────────────────────────────────────

def _snapshot_product(product: Product, session) -> Dict[str, Any]:
    """Capture all cloneable fields into a serialisable dict."""
    clone_images    = cfg.get_bool("product_clone_images",       True)
    clone_faq       = cfg.get_bool("product_clone_faq",          True)
    clone_coupons   = cfg.get_bool("product_clone_coupons",      False)
    clone_stock     = cfg.get_bool("product_clone_stock",        False)
    clone_settings  = cfg.get_bool("product_clone_settings",     True)
    clone_fields    = cfg.get_bool("product_clone_custom_fields", True)

    snap: Dict[str, Any] = {
        "name":                     product.name,
        "description":              product.description,
        "price":                    product.price,
        "sale_price":               product.sale_price,
        "currency":                 product.currency,
        "product_type":             product.product_type.name,
        "category_id":              product.category_id,
        "subcategory_id":           product.subcategory_id,
        "is_active":                product.is_active,
        "is_featured":              product.is_featured,
        "product_emoji":            product.product_emoji,
        "delivery_note":            product.delivery_note,
        "warranty_info":            product.warranty_info,
        "min_quantity":             product.min_quantity,
        "max_quantity":             product.max_quantity,
        "bulk_purchase_enabled":    product.bulk_purchase_enabled,
        "reusable":                 product.reusable,
        "bundle_price":             product.bundle_price,
        "bundle_discount_percent":  product.bundle_discount_percent,
        # type_config = product settings + custom fields
        "type_config":              product.type_config if clone_settings or clone_fields else None,
        "delivery_format_template": product.delivery_format_template if clone_settings else None,
        # Images
        "image_path":               product.image_path if clone_images else None,
        "download_link":            product.download_link if clone_images else None,
        "telegram_file_id":         product.telegram_file_id if clone_images else None,
        "telegram_file_type":       product.telegram_file_type if clone_images else None,
        # Stock (count only, never keys)
        "stock_count":              product.stock_count if clone_stock else 0,
    }

    # Variants (structure only — never keys/inventory rows)
    variants = []
    for v in product.variants:
        variants.append({
            "name":          v.name,
            "price":         v.price,
            "sale_price":    v.sale_price,
            "is_active":     v.is_active,
            "display_order": v.display_order,
        })
    snap["variants"] = variants

    # FAQ
    if clone_faq:
        faqs = (session.query(ProductFAQ)
                .filter_by(product_id=product.id, is_active=True)
                .order_by(ProductFAQ.sort_order)
                .all())
        snap["faqs"] = [
            {
                "question":   f.question,
                "answer":     f.answer,
                "category":   f.category,
                "sort_order": f.sort_order,
                "is_active":  f.is_active,
            }
            for f in faqs
        ]
    else:
        snap["faqs"] = []

    # Product-specific coupons
    if clone_coupons:
        try:
            coupons = (session.query(Coupon)
                       .filter_by(product_id=product.id, is_active=True)
                       .all())
            snap["coupons"] = [
                {
                    "code":            c.code + "_copy",
                    "discount_type":   c.discount_type.value if hasattr(c.discount_type, "value") else str(c.discount_type),
                    "discount_value":  c.discount_value,
                    "min_purchase":    c.min_purchase,
                    "max_uses":        c.max_uses,
                    "expires_at":      c.expires_at.isoformat() if c.expires_at else None,
                }
                for c in coupons
            ]
        except Exception:
            snap["coupons"] = []
    else:
        snap["coupons"] = []

    return snap


def _create_from_snapshot(snap: Dict[str, Any], session,
                           name_override: Optional[str] = None,
                           price_override: Optional[float] = None,
                           stock_override: Optional[int] = None,
                           hidden: bool = False,
                           created_by: Optional[int] = None) -> Product:
    """Materialise a product snapshot into a new Product row (and relations)."""
    from database.models import ProductType as PT
    ptype_name = snap["product_type"]
    ptype = PT[ptype_name]

    new_name = name_override or f"Copy of {snap['name']}"
    new_price = price_override if price_override is not None else snap["price"]
    new_stock = stock_override if stock_override is not None else snap.get("stock_count", 0)
    is_active = False if hidden else snap.get("is_active", True)

    new_product = Product(
        name                   = new_name[:255],
        description            = snap.get("description"),
        price                  = new_price,
        sale_price             = snap.get("sale_price"),
        currency               = snap.get("currency", "USD"),
        product_type           = ptype,
        category_id            = snap.get("category_id"),
        subcategory_id         = snap.get("subcategory_id"),
        image_path             = snap.get("image_path"),
        download_link          = snap.get("download_link"),
        telegram_file_id       = snap.get("telegram_file_id"),
        telegram_file_type     = snap.get("telegram_file_type"),
        is_active              = is_active,
        is_featured            = snap.get("is_featured", False),
        product_emoji          = snap.get("product_emoji"),
        delivery_note          = snap.get("delivery_note"),
        warranty_info          = snap.get("warranty_info"),
        min_quantity           = snap.get("min_quantity"),
        max_quantity           = snap.get("max_quantity"),
        bulk_purchase_enabled  = snap.get("bulk_purchase_enabled", True),
        reusable               = snap.get("reusable", False),
        bundle_price           = snap.get("bundle_price"),
        bundle_discount_percent= snap.get("bundle_discount_percent"),
        type_config            = snap.get("type_config"),
        delivery_format_template = snap.get("delivery_format_template"),
        stock_count            = new_stock,
        sales_count            = 0,  # never copy sales stats
        created_at             = datetime.utcnow(),
    )
    session.add(new_product)
    session.flush()  # get new_product.id

    # Variants (structure only)
    for vsnap in snap.get("variants", []):
        session.add(ProductVariant(
            product_id    = new_product.id,
            name          = vsnap["name"],
            price         = vsnap["price"],
            sale_price    = vsnap.get("sale_price"),
            is_active     = vsnap.get("is_active", True),
            display_order = vsnap.get("display_order", 0),
            stock_count   = 0,  # never copy key inventory
            created_at    = datetime.utcnow(),
        ))

    # FAQs
    for fsnap in snap.get("faqs", []):
        session.add(ProductFAQ(
            product_id = new_product.id,
            question   = fsnap["question"],
            answer     = fsnap["answer"],
            category   = fsnap.get("category", "general"),
            sort_order  = fsnap.get("sort_order", 0),
            is_active  = fsnap.get("is_active", True),
            created_at = datetime.utcnow(),
        ))

    # Product-specific coupons
    for csnap in snap.get("coupons", []):
        try:
            from database.models import Coupon as CouponModel, DiscountType
            session.add(CouponModel(
                code           = csnap["code"][:32],
                discount_type  = DiscountType(csnap["discount_type"]),
                discount_value = csnap["discount_value"],
                product_id     = new_product.id,
                min_purchase   = csnap.get("min_purchase"),
                max_uses       = csnap.get("max_uses"),
                current_uses   = 0,
                is_active      = True,
                created_at     = datetime.utcnow(),
            ))
        except Exception:
            pass  # coupon clone is best-effort

    return new_product


# ── Main menu / dashboard ──────────────────────────────────────────────────

async def pct_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    status = cfg.get("product_clone_status", "enabled")
    if status != "enabled":
        icon = "🟡" if status == "maintenance" else "🔴"
        await _safe_edit(query,
            f"📄 <b>Product Clone & Templates</b>\n\n"
            f"{icon} Status: <b>{status.capitalize()}</b>",
            _back("acc:root"))
        return

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week  = today - timedelta(days=7)
    month = today - timedelta(days=30)

    with get_db_session() as s:
        total_clones = s.query(ProductCloneLog).count()
        today_clones = s.query(ProductCloneLog).filter(
            ProductCloneLog.created_at >= today).count()
        week_clones  = s.query(ProductCloneLog).filter(
            ProductCloneLog.created_at >= week).count()
        month_clones = s.query(ProductCloneLog).filter(
            ProductCloneLog.created_at >= month).count()
        total_tpl    = s.query(ProductTemplate).count()
        # Most used template
        from sqlalchemy import func
        top = (s.query(ProductTemplate)
               .order_by(ProductTemplate.use_count.desc())
               .first())
        top_name = top.name if top else "—"
        top_uses = top.use_count if top else 0

    text = (
        "📄 <b>Product Clone & Template System</b>\n\n"
        f"<b>📋 Templates:</b> {total_tpl} / {_max_templates()}\n"
        f"<b>Most Used:</b> {top_name} ({top_uses} uses)\n\n"
        f"<b>Clone Activity:</b>\n"
        f"  All-time: {total_clones}  |  Today: {today_clones}\n"
        f"  This week: {week_clones}  |  This month: {month_clones}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Templates",     callback_data="pct:templates:0"),
            InlineKeyboardButton("📊 Clone History", callback_data="pct:history:0"),
        ],
        [
            InlineKeyboardButton("📄 Clone a Product",  callback_data="pct:pick"),
            InlineKeyboardButton("📦 Bulk Clone",        callback_data="pct:bulk"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings",          callback_data="pct:settings"),
            InlineKeyboardButton("🔙 Back",              callback_data="acc:root"),
        ],
    ])
    await _safe_edit(query, text, kb)


# ── Product picker for clone ───────────────────────────────────────────────

async def pct_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated product list to select what to clone."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        page = int(query.data.split(":")[-1]) if "pick:" in query.data else 0
    except ValueError:
        page = 0
    await _render_product_picker(query, page, mode="clone")


async def _render_product_picker(query, page: int, mode: str = "clone"):
    with get_db_session() as s:
        q = s.query(Product).order_by(Product.name)
        total = q.count()
        products = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [(p.id, p.name, p.product_type.name if p.product_type else "?", p.is_active)
                 for p in products]

    action = "pct:clone_opts" if mode == "clone" else "pct:tpl:save"
    kb = []
    for pid, name, ptype, active in items:
        icon = "✅" if active else "❌"
        kb.append([InlineKeyboardButton(
            f"{icon} {name[:30]} [{ptype[:8]}]",
            callback_data=f"{action}:{pid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pct:pick:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pct:pick:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="pct:menu")])

    verb = "clone" if mode == "clone" else "save as template"
    await _safe_edit(query,
        f"📄 <b>Select a product to {verb}</b>\n\n"
        f"Showing {len(items)} of {total} products:",
        InlineKeyboardMarkup(kb))


# ── Clone options ──────────────────────────────────────────────────────────

async def pct_clone_opts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show clone options for a specific product."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await query.answer("Feature disabled or in maintenance.", show_alert=True)
        return
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        product = s.get(Product, pid)
        if not product:
            await query.answer("Product not found.", show_alert=True)
            return
        name  = product.name
        price = product.price
        ptype = product.product_type.name if product.product_type else "?"
        variant_count = len(product.variants)
        faq_count     = s.query(ProductFAQ).filter_by(product_id=pid).count()

    context.user_data["_pct"] = {"source_id": pid, "source_name": name}

    text = (
        f"📄 <b>Clone: {name}</b>\n\n"
        f"Type: {ptype}  |  Price: {price}  |  "
        f"Variants: {variant_count}  |  FAQs: {faq_count}\n\n"
        f"<b>Current clone settings:</b>\n"
        f"  Images: {'✅' if cfg.get_bool('product_clone_images', True) else '❌'}\n"
        f"  FAQ: {'✅' if cfg.get_bool('product_clone_faq', True) else '❌'}\n"
        f"  Coupons: {'✅' if cfg.get_bool('product_clone_coupons', False) else '❌'}\n"
        f"  Stock: {'✅' if cfg.get_bool('product_clone_stock', False) else '❌'}\n\n"
        f"Choose a clone option:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Quick Clone (default)",   callback_data=f"pct:clone:quick:{pid}")],
        [InlineKeyboardButton("✏️ Clone with Custom Name",  callback_data=f"pct:clone:named:{pid}")],
        [InlineKeyboardButton("💰 Clone with New Price",    callback_data=f"pct:clone:price:{pid}")],
        [InlineKeyboardButton("📦 Clone with New Stock",    callback_data=f"pct:clone:stock:{pid}")],
        [InlineKeyboardButton("👁 Clone Hidden",            callback_data=f"pct:clone:hidden:{pid}")],
        [InlineKeyboardButton("💾 Save as Template",        callback_data=f"pct:tpl:save:{pid}")],
        [InlineKeyboardButton("🔙 Back",                    callback_data="pct:pick")],
    ])
    await _safe_edit(query, text, kb)


# ── Perform clone ──────────────────────────────────────────────────────────

async def pct_clone_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Immediate one-click clone with default settings."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await query.answer("Feature disabled.", show_alert=True)
        return
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    new_pid, new_name = await _do_clone(
        pid, update.effective_user.id, clone_type="single")
    if new_pid:
        await _safe_edit(query,
            f"✅ <b>Product cloned successfully!</b>\n\n"
            f"<b>Clone ID:</b> #{new_pid}\n"
            f"<b>Name:</b> {new_name}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 View Clone", callback_data=f"inv_prod_{new_pid}")],
                [InlineKeyboardButton("📄 Clone Again", callback_data=f"pct:clone_opts:{pid}")],
                [InlineKeyboardButton("🔙 Dashboard", callback_data="pct:menu")],
            ]))
    else:
        await query.answer("❌ Clone failed. Check logs.", show_alert=True)


async def pct_clone_hidden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clone as hidden (is_active=False)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    new_pid, new_name = await _do_clone(
        pid, update.effective_user.id, clone_type="single", hidden=True)
    if new_pid:
        await _safe_edit(query,
            f"✅ <b>Hidden clone created!</b>\n\n"
            f"Clone #{new_pid} — <b>{new_name}</b>\n"
            f"Status: ❌ Hidden (not visible to customers)",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 View Clone", callback_data=f"inv_prod_{new_pid}")],
                [InlineKeyboardButton("🔙 Dashboard", callback_data="pct:menu")],
            ]))
    else:
        await query.answer("❌ Clone failed.", show_alert=True)


async def pct_clone_named_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for custom name — starts conversation."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    context.user_data["_pct"] = {"source_id": pid, "clone_mode": "named"}
    await _safe_edit(query,
        "✏️ <b>Clone with Custom Name</b>\n\n"
        "Send the new product name:",
        _back(f"pct:clone_opts:{pid}"))
    return PCT_CLONE_NAME


async def pct_receive_clone_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()[:255]
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Send again:")
        return PCT_CLONE_NAME
    data = context.user_data.get("_pct", {})
    pid  = data.get("source_id")
    if not pid:
        return ConversationHandler.END
    new_pid, new_name = await _do_clone(
        pid, update.effective_user.id, clone_type="single",
        name_override=name)
    context.user_data.pop("_pct", None)
    if new_pid:
        await update.message.reply_text(
            f"✅ Cloned as <b>{new_name}</b>  (#{new_pid})\n\n"
            "The clone is ready in your product list.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 View Clone",   callback_data=f"inv_prod_{new_pid}")],
                [InlineKeyboardButton("🔙 Dashboard",    callback_data="pct:menu")],
            ]))
    else:
        await update.message.reply_text("❌ Clone failed. Please try again.")
    return ConversationHandler.END


async def pct_clone_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for new price — starts conversation."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    with get_db_session() as s:
        p = s.get(Product, pid)
        cur_price = p.price if p else 0.0
    context.user_data["_pct"] = {"source_id": pid, "clone_mode": "price"}
    await _safe_edit(query,
        f"💰 <b>Clone with New Price</b>\n\n"
        f"Current price: {cur_price}\n\n"
        "Send the new price as a number (e.g. <code>9.99</code>):",
        _back(f"pct:clone_opts:{pid}"))
    return PCT_BULK_PRICE_ADJ


async def pct_receive_clone_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        new_price = float(txt)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a positive number:")
        return PCT_BULK_PRICE_ADJ
    data = context.user_data.get("_pct", {})
    pid  = data.get("source_id")
    mode = data.get("clone_mode", "price")
    if not pid:
        return ConversationHandler.END
    if mode == "stock":
        try:
            stock = int(txt)
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Send a whole number:")
            return PCT_BULK_PRICE_ADJ
        new_pid, new_name = await _do_clone(
            pid, update.effective_user.id, clone_type="single", stock_override=stock)
        context.user_data.pop("_pct", None)
        if new_pid:
            await update.message.reply_text(
                f"✅ Cloned as <b>{new_name}</b>  (#{new_pid}) with stock: {stock}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 View Clone", callback_data=f"inv_prod_{new_pid}")],
                    [InlineKeyboardButton("🔙 Dashboard",  callback_data="pct:menu")],
                ]))
    else:
        new_pid, new_name = await _do_clone(
            pid, update.effective_user.id, clone_type="single", price_override=new_price)
        context.user_data.pop("_pct", None)
        if new_pid:
            await update.message.reply_text(
                f"✅ Cloned as <b>{new_name}</b>  (#{new_pid}) at price: {new_price}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 View Clone", callback_data=f"inv_prod_{new_pid}")],
                    [InlineKeyboardButton("🔙 Dashboard",  callback_data="pct:menu")],
                ]))
    return ConversationHandler.END


async def pct_clone_stock_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for stock override."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    context.user_data["_pct"] = {"source_id": pid, "clone_mode": "stock"}
    await _safe_edit(query,
        "📦 <b>Clone with New Stock</b>\n\n"
        "Send the initial stock count for the clone (whole number):",
        _back(f"pct:clone_opts:{pid}"))
    return PCT_BULK_PRICE_ADJ  # reuse state for any numeric input


# ── Core clone engine ──────────────────────────────────────────────────────

async def _do_clone(source_id: int, admin_uid: int,
                    clone_type: str = "single",
                    name_override: Optional[str] = None,
                    price_override: Optional[float] = None,
                    stock_override: Optional[int] = None,
                    hidden: bool = False,
                    template_id: Optional[int] = None) -> Tuple[Optional[int], str]:
    """Execute one clone operation. Returns (new_product_id, new_name) or (None, '')."""
    try:
        with get_db_session() as s:
            product = s.get(Product, source_id)
            if not product:
                return None, ""
            snap = _snapshot_product(product, s)
            new_product = _create_from_snapshot(
                snap, s,
                name_override=name_override,
                price_override=price_override,
                stock_override=stock_override,
                hidden=hidden,
                created_by=admin_uid,
            )
            s.flush()
            new_pid  = new_product.id
            new_name = new_product.name

            # Clone log
            s.add(ProductCloneLog(
                source_product_id = source_id,
                cloned_product_id = new_pid,
                template_id       = template_id,
                created_by        = admin_uid,
                clone_type        = clone_type,
                options_json      = json.dumps({
                    "name_override":  name_override,
                    "price_override": price_override,
                    "stock_override": stock_override,
                    "hidden":         hidden,
                }),
                created_at = datetime.utcnow(),
            ))
            s.commit()

        log_admin_action(admin_uid, "product.clone",
                         "product", new_pid,
                         f"source={source_id} type={clone_type} name={new_name}",
                         module="product_clone")
        return new_pid, new_name
    except Exception:
        logger.exception("_do_clone: failed for source=%d", source_id)
        return None, ""


# ── Bulk clone ─────────────────────────────────────────────────────────────

async def pct_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await query.answer("Feature disabled.", show_alert=True)
        return

    with get_db_session() as s:
        categories = s.query(Category).order_by(Category.name).all()
        cat_items  = [(c.id, c.name) for c in categories]
        total_products = s.query(Product).count()

    kb = [[InlineKeyboardButton(f"📁 {name[:28]}", callback_data=f"pct:bulk:cat:{cid}")]
          for cid, name in cat_items]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="pct:menu")])

    await _safe_edit(query,
        f"📦 <b>Bulk Clone by Category</b>\n\n"
        f"Total products in store: {total_products}\n\n"
        "Select a category to clone all its products.\n"
        "Each product will be cloned with default settings.\n"
        "⚠️ Large categories may take a moment.",
        InlineKeyboardMarkup(kb))


async def pct_bulk_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clone every product in a category."""
    query = update.callback_query
    await query.answer("Starting bulk clone…")
    if not _is_admin(update.effective_user.id):
        return
    try:
        cat_id = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        cat = s.get(Category, cat_id)
        if not cat:
            await query.answer("Category not found.", show_alert=True)
            return
        cat_name = cat.name
        products = s.query(Product).filter_by(category_id=cat_id).all()
        source_ids = [p.id for p in products]

    if not source_ids:
        await _safe_edit(query,
            f"ℹ️ No products found in category <b>{cat_name}</b>.",
            _back("pct:bulk"))
        return

    await _safe_edit(query,
        f"⏳ Cloning {len(source_ids)} products from <b>{cat_name}</b>…\n\n"
        "Please wait.",
        _back("pct:menu"))

    succeeded = []
    failed    = 0
    for src_id in source_ids:
        new_pid, new_name = await _do_clone(
            src_id, update.effective_user.id, clone_type="bulk_category")
        if new_pid:
            succeeded.append(new_pid)
        else:
            failed += 1

    log_admin_action(update.effective_user.id, "product.bulk_clone",
                     "category", cat_id,
                     f"cat={cat_name} cloned={len(succeeded)} failed={failed}",
                     module="product_clone")

    await query.message.reply_text(
        f"✅ <b>Bulk Clone Complete</b>\n\n"
        f"Category: <b>{cat_name}</b>\n"
        f"Cloned: {len(succeeded)}  |  Failed: {failed}\n\n"
        f"New product IDs: {', '.join(str(x) for x in succeeded[:20])}"
        f"{'…' if len(succeeded) > 20 else ''}",
        parse_mode="HTML",
        reply_markup=_back("pct:menu"))


# ── Template system ────────────────────────────────────────────────────────

async def pct_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    with get_db_session() as s:
        q = s.query(ProductTemplate).order_by(ProductTemplate.created_at.desc())
        total = q.count()
        rows  = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [(r.id, r.name, r.use_count, r.created_at) for r in rows]

    lines = [f"📋 <b>Product Templates</b>  ({total} / {_max_templates()} used)\n"]
    kb = []
    for tid, name, uses, created in items:
        at_str = created.strftime("%m/%d") if created else "—"
        lines.append(f"📋 <b>{name}</b>  uses:{uses}  {at_str}")
        kb.append([InlineKeyboardButton(f"📋 {name[:30]} ({uses} uses)",
                                         callback_data=f"pct:tpl:view:{tid}")])

    if not items:
        lines.append("No templates yet. Save a product as a template to get started.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pct:templates:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pct:templates:{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([
        InlineKeyboardButton("➕ Save Product as Template", callback_data="pct:pick:tpl"),
        InlineKeyboardButton("🔙 Back",                    callback_data="pct:menu"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def pct_tpl_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tid)
        if not tpl:
            await query.answer("Not found.", show_alert=True)
            return
        name      = tpl.name
        desc      = tpl.description or "—"
        uses      = tpl.use_count
        created   = tpl.created_at
        updated   = tpl.updated_at
        try:
            snap = json.loads(tpl.template_data)
        except Exception:
            snap = {}
        snap_name = snap.get("name", "—")
        snap_type = snap.get("product_type", "—")
        snap_price = snap.get("price", "—")
        variant_count = len(snap.get("variants", []))
        faq_count     = len(snap.get("faqs", []))

    at_str = created.strftime("%Y-%m-%d") if created else "—"
    up_str = updated.strftime("%Y-%m-%d %H:%M") if updated else "—"

    text = (
        f"📋 <b>{name}</b>  [id {tid}]\n\n"
        f"<b>Description:</b> {desc}\n"
        f"<b>Uses:</b> {uses}  |  <b>Created:</b> {at_str}  |  <b>Updated:</b> {up_str}\n\n"
        f"<b>Template Contents:</b>\n"
        f"  Product: {snap_name}\n"
        f"  Type: {snap_type}  |  Price: {snap_price}\n"
        f"  Variants: {variant_count}  |  FAQs: {faq_count}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Create Product", callback_data=f"pct:tpl:from:{tid}"),
            InlineKeyboardButton("📋 Duplicate",      callback_data=f"pct:tpl:dup:{tid}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Name",      callback_data=f"pct:tpl:edit:{tid}"),
            InlineKeyboardButton("🗑 Delete",          callback_data=f"pct:tpl:del_ask:{tid}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="pct:templates:0")],
    ])
    await _safe_edit(query, text, kb)


async def pct_tpl_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new product from a template."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await query.answer("Feature disabled.", show_alert=True)
        return
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tid)
        if not tpl:
            await query.answer("Template not found.", show_alert=True)
            return
        try:
            snap = json.loads(tpl.template_data)
        except Exception:
            await query.answer("Template data is corrupted.", show_alert=True)
            return

        new_product = _create_from_snapshot(snap, s,
                                             name_override=f"{snap.get('name', 'Product')} (from {tpl.name})",
                                             created_by=update.effective_user.id)
        s.flush()
        new_pid  = new_product.id
        new_name = new_product.name
        tpl.use_count = tpl.use_count + 1

        s.add(ProductCloneLog(
            cloned_product_id = new_pid,
            template_id       = tid,
            created_by        = update.effective_user.id,
            clone_type        = "from_template",
            options_json      = json.dumps({"template_id": tid}),
            created_at        = datetime.utcnow(),
        ))
        s.commit()

    log_admin_action(update.effective_user.id, "product.from_template",
                     "product", new_pid,
                     f"template={tid} name={new_name}",
                     module="product_clone")
    await _safe_edit(query,
        f"✅ <b>Product created from template!</b>\n\n"
        f"<b>Name:</b> {new_name}\n<b>ID:</b> #{new_pid}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 View Product", callback_data=f"inv_prod_{new_pid}")],
            [InlineKeyboardButton("🔙 Templates",   callback_data="pct:templates:0")],
        ]))


async def pct_tpl_dup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Duplicate an existing template."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    max_tpl = _max_templates()
    with get_db_session() as s:
        current_count = s.query(ProductTemplate).count()
        if current_count >= max_tpl:
            await query.answer(f"Template limit ({max_tpl}) reached.", show_alert=True)
            return
        tpl = s.get(ProductTemplate, tid)
        if not tpl:
            await query.answer("Not found.", show_alert=True)
            return
        new_tpl = ProductTemplate(
            name          = f"Copy of {tpl.name}"[:120],
            description   = tpl.description,
            template_data = tpl.template_data,
            use_count     = 0,
            created_by    = update.effective_user.id,
            created_at    = datetime.utcnow(),
            updated_at    = datetime.utcnow(),
        )
        s.add(new_tpl)
        s.commit()
        new_tid = new_tpl.id

    log_admin_action(update.effective_user.id, "product_template.duplicate",
                     "product_template", new_tid, f"source={tid}", module="product_clone")
    await query.answer(f"📋 Duplicated as #{new_tid}.", show_alert=True)
    context.user_data["_pct_tpl_id"] = new_tid
    await pct_tpl_view(with_data(update, f"pct:tpl:view:{new_tid}"), context)


async def pct_tpl_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _safe_edit(query,
        f"⚠️ Delete template #{tid}? This cannot be undone.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"pct:tpl:del_ok:{tid}"),
             InlineKeyboardButton("🔙 Cancel",      callback_data=f"pct:tpl:view:{tid}")],
        ]))


async def pct_tpl_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tid)
        if tpl:
            s.delete(tpl)
            s.commit()
    log_admin_action(update.effective_user.id, "product_template.delete",
                     "product_template", tid, module="product_clone")
    await _safe_edit(query, f"🗑 Template #{tid} deleted.", _back("pct:templates:0"))


# ── Save product as template ───────────────────────────────────────────────

async def pct_tpl_save_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Begin save-as-template conversation."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        pid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    max_tpl = _max_templates()
    with get_db_session() as s:
        count = s.query(ProductTemplate).count()
        if count >= max_tpl:
            await query.answer(f"Template limit ({max_tpl}) reached.", show_alert=True)
            return ConversationHandler.END
        product = s.get(Product, pid)
        if not product:
            await query.answer("Product not found.", show_alert=True)
            return ConversationHandler.END
        pname = product.name

    context.user_data["_pct"] = {"source_id": pid, "pname": pname}
    await _safe_edit(query,
        f"💾 <b>Save as Template</b>\n\n"
        f"Source: <b>{pname}</b>\n\n"
        "Send a <b>template name</b> (used to identify it later):",
        _back(f"pct:clone_opts:{pid}"))
    return PCT_TPL_NAME


async def pct_tpl_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()[:120]
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Send again:")
        return PCT_TPL_NAME
    context.user_data["_pct"]["tpl_name"] = name
    await update.message.reply_text(
        "📝 Send a short <b>description</b> for the template (optional — send /skip to skip):",
        parse_mode="HTML")
    return PCT_TPL_DESC


async def pct_tpl_receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = ""
    if update.message.text and not update.message.text.strip().startswith("/skip"):
        desc = update.message.text.strip()[:512]
    return await _finish_save_template(update, context, desc)


async def _finish_save_template(update, context, desc: str):
    data    = context.user_data.pop("_pct", {})
    pid     = data.get("source_id")
    tpl_name = data.get("tpl_name", "Unnamed Template")

    if not pid:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END

    try:
        with get_db_session() as s:
            product = s.get(Product, pid)
            if not product:
                await update.message.reply_text("❌ Product not found.")
                return ConversationHandler.END
            snap = _snapshot_product(product, s)
            tpl = ProductTemplate(
                name          = tpl_name,
                description   = desc or None,
                template_data = json.dumps(snap, default=str),
                use_count     = 0,
                created_by    = update.effective_user.id,
                created_at    = datetime.utcnow(),
                updated_at    = datetime.utcnow(),
            )
            s.add(tpl)
            s.commit()
            tid = tpl.id
    except Exception:
        logger.exception("pct_tpl_receive_desc: failed to save template")
        await update.message.reply_text("❌ Failed to save template. Please try again.")
        return ConversationHandler.END

    log_admin_action(update.effective_user.id, "product_template.save",
                     "product_template", tid,
                     f"source={pid} name={tpl_name}", module="product_clone")
    await update.message.reply_text(
        f"✅ <b>Template saved!</b>\n\n"
        f"<b>Name:</b> {tpl_name}\n<b>ID:</b> #{tid}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Template", callback_data=f"pct:tpl:view:{tid}")],
            [InlineKeyboardButton("🔙 Templates",     callback_data="pct:templates:0")],
        ]))
    return ConversationHandler.END


# ── Edit template name ─────────────────────────────────────────────────────

async def pct_tpl_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        tid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tid)
        if not tpl:
            await query.answer("Not found.", show_alert=True)
            return ConversationHandler.END
        current_name = tpl.name
    context.user_data["_pct"] = {"edit_tpl_id": tid}
    await _safe_edit(query,
        f"✏️ <b>Edit Template Name</b>\n\n"
        f"Current: <code>{current_name}</code>\n\n"
        "Send the new name:",
        _back(f"pct:tpl:view:{tid}"))
    return PCT_TPL_NAME


async def pct_tpl_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()[:120]
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Send again:")
        return PCT_TPL_NAME
    data = context.user_data.pop("_pct", {})
    tid  = data.get("edit_tpl_id")
    if not tid:
        return ConversationHandler.END
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tid)
        if tpl:
            tpl.name       = name
            tpl.updated_at = datetime.utcnow()
            s.commit()
    log_admin_action(update.effective_user.id, "product_template.edit",
                     "product_template", tid,
                     f"name={name}", module="product_clone")
    await update.message.reply_text(
        f"✅ Template renamed to <b>{name}</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Template", callback_data=f"pct:tpl:view:{tid}")],
            [InlineKeyboardButton("🔙 Templates",     callback_data="pct:templates:0")],
        ]))
    return ConversationHandler.END


# ── Clone history ──────────────────────────────────────────────────────────

async def pct_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    with get_db_session() as s:
        q = s.query(ProductCloneLog).order_by(ProductCloneLog.created_at.desc())
        total = q.count()
        rows  = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [
            (r.id, r.source_product_id, r.cloned_product_id,
             r.clone_type, r.created_at)
            for r in rows
        ]

    lines = [f"📊 <b>Clone History</b>  ({total} total)\n"]
    kb    = []
    for rid, src, dst, ctype, at in items:
        at_str = at.strftime("%m/%d %H:%M") if at else "—"
        lines.append(f"#{rid}  src:{src or '—'} → #{dst or '?'}  [{ctype}]  {at_str}")
        if dst:
            kb.append([InlineKeyboardButton(
                f"#{rid} → #{dst} [{ctype}]",
                callback_data=f"inv_prod_{dst}")])

    if not items:
        lines.append("No clone operations yet.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pct:history:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pct:history:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="pct:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Settings panel ─────────────────────────────────────────────────────────

async def pct_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    await _render_settings(query)


async def _render_settings(query):
    status      = cfg.get("product_clone_status", "enabled")
    cl_images   = cfg.get_bool("product_clone_images",       True)
    cl_faq      = cfg.get_bool("product_clone_faq",          True)
    cl_coupons  = cfg.get_bool("product_clone_coupons",      False)
    cl_stock    = cfg.get_bool("product_clone_stock",        False)
    cl_settings = cfg.get_bool("product_clone_settings",     True)
    cl_fields   = cfg.get_bool("product_clone_custom_fields", True)
    tpl_max     = cfg.get_int("product_template_max",        50)

    icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "🟢")
    text = (
        "⚙️ <b>Product Clone Settings</b>\n\n"
        f"<b>Status:</b> {icon} {status.capitalize()}\n"
        f"<b>Clone Images:</b>       {'✅' if cl_images else '❌'}\n"
        f"<b>Clone FAQ:</b>          {'✅' if cl_faq else '❌'}\n"
        f"<b>Clone Coupons:</b>      {'✅' if cl_coupons else '❌'}\n"
        f"<b>Clone Stock Count:</b>  {'✅' if cl_stock else '❌'}\n"
        f"<b>Clone Settings:</b>     {'✅' if cl_settings else '❌'}\n"
        f"<b>Clone Custom Fields:</b>{'✅' if cl_fields else '❌'}\n"
        f"<b>Max Templates:</b>      {tpl_max}"
    )

    def _t(key: str, val: bool) -> InlineKeyboardButton:
        label = f"{'✅' if val else '❌'} {key.replace('product_clone_', '').replace('_', ' ').title()}"
        return InlineKeyboardButton(label, callback_data=f"pct:set:toggle:{key}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Enable",       callback_data="pct:set:status:enabled"),
            InlineKeyboardButton("🟡 Maintenance",  callback_data="pct:set:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",      callback_data="pct:set:status:disabled"),
        ],
        [_t("product_clone_images",        cl_images),
         _t("product_clone_faq",           cl_faq)],
        [_t("product_clone_coupons",       cl_coupons),
         _t("product_clone_stock",         cl_stock)],
        [_t("product_clone_settings",      cl_settings),
         _t("product_clone_custom_fields", cl_fields)],
        [
            InlineKeyboardButton("📋 10 Templates",   callback_data="pct:set:tplmax:10"),
            InlineKeyboardButton("📋 20",              callback_data="pct:set:tplmax:20"),
            InlineKeyboardButton("📋 50",              callback_data="pct:set:tplmax:50"),
            InlineKeyboardButton("📋 100",             callback_data="pct:set:tplmax:100"),
            InlineKeyboardButton("📋 ∞",               callback_data="pct:set:tplmax:0"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="pct:menu")],
    ])
    await _safe_edit(query, text, kb)


async def pct_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        new_status = query.data.split(":")[-1]
    except IndexError:
        return
    cfg.set("product_clone_status", new_status)
    log_admin_action(update.effective_user.id, "product_clone.settings",
                     "product_clone", 0,
                     f"product_clone_status={new_status}", module="product_clone")
    await query.answer(f"Status → {new_status}", show_alert=True)
    await _render_settings(query)


async def pct_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        key = query.data.split(":")[3]
    except IndexError:
        return
    current = cfg.get_bool(key, True)
    cfg.set(key, str(not current))
    log_admin_action(update.effective_user.id, "product_clone.settings",
                     "product_clone", 0,
                     f"{key}={not current}", module="product_clone")
    await _render_settings(query)


async def pct_settings_tplmax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        val = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return
    cfg.set("product_template_max", str(val))
    label = "Unlimited" if val == 0 else str(val)
    await query.answer(f"Max templates set to {label}", show_alert=True)
    await _render_settings(query)


# ── Cancel conversation ────────────────────────────────────────────────────

async def pct_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_pct", None)
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Cancelled.", _back("pct:menu"))
    return ConversationHandler.END


# ── Master dispatcher ──────────────────────────────────────────────────────

async def pct_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all pct:* callbacks not already claimed by the conversation handler."""
    query = update.callback_query
    data  = query.data or ""

    if data == "pct:menu":
        return await pct_menu(update, context)
    if data in ("pct:pick", ) or data.startswith("pct:pick:"):
        return await pct_pick(update, context)
    if data.startswith("pct:clone_opts:"):
        return await pct_clone_opts(update, context)
    if data.startswith("pct:clone:quick:"):
        return await pct_clone_quick(update, context)
    if data.startswith("pct:clone:hidden:"):
        return await pct_clone_hidden(update, context)
    if data == "pct:bulk":
        return await pct_bulk(update, context)
    if data.startswith("pct:bulk:cat:"):
        return await pct_bulk_category(update, context)
    if data.startswith("pct:templates"):
        return await pct_templates(update, context)
    if data.startswith("pct:tpl:view:"):
        return await pct_tpl_view(update, context)
    if data.startswith("pct:tpl:from:"):
        return await pct_tpl_from(update, context)
    if data.startswith("pct:tpl:dup:"):
        return await pct_tpl_dup(update, context)
    if data.startswith("pct:tpl:del_ask:"):
        return await pct_tpl_del_ask(update, context)
    if data.startswith("pct:tpl:del_ok:"):
        return await pct_tpl_del_ok(update, context)
    if data.startswith("pct:history"):
        return await pct_history(update, context)
    if data == "pct:settings":
        return await pct_settings(update, context)
    if data.startswith("pct:set:status:"):
        return await pct_settings_status(update, context)
    if data.startswith("pct:set:toggle:"):
        return await pct_settings_toggle(update, context)
    if data.startswith("pct:set:tplmax:"):
        return await pct_settings_tplmax(update, context)

    await query.answer("❓ Unknown command.", show_alert=True)


# ── Conversation builder ───────────────────────────────────────────────────

def build_pct_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            # Save as template
            CallbackQueryHandler(pct_tpl_save_start,    pattern=r"^pct:tpl:save:\d+$"),
            # Edit template name
            CallbackQueryHandler(pct_tpl_edit_start,    pattern=r"^pct:tpl:edit:\d+$"),
            # Clone with name
            CallbackQueryHandler(pct_clone_named_start, pattern=r"^pct:clone:named:\d+$"),
            # Clone with price
            CallbackQueryHandler(pct_clone_price_start, pattern=r"^pct:clone:price:\d+$"),
            # Clone with stock
            CallbackQueryHandler(pct_clone_stock_start, pattern=r"^pct:clone:stock:\d+$"),
        ],
        states={
            PCT_TPL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               # context-sensitive handler: edit vs save
                               lambda u, c: (pct_tpl_edit_receive(u, c)
                                             if c.user_data.get("_pct", {}).get("edit_tpl_id")
                                             else pct_tpl_receive_name(u, c))),
            ],
            PCT_TPL_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pct_tpl_receive_desc),
                CommandHandler("skip", pct_tpl_receive_desc),
            ],
            PCT_CLONE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pct_receive_clone_name),
            ],
            PCT_BULK_PRICE_ADJ: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pct_receive_clone_price),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(pct_cancel, pattern=r"^pct:menu$"),
            CommandHandler("cancel", pct_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
