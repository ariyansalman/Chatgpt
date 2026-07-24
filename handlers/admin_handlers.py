"""Admin panel command and callback handlers."""

import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
from database import (
    get_db_session, User, Category, Subcategory, Product, ProductKey,
    Order, OrderItem, Settings, Broadcast, ProductType, OrderStatus, DisputeStatus,
    ProductVariant
)
from utils import (
    is_admin, admin_only, format_price,
    create_admin_product_menu_keyboard, create_admin_category_menu_keyboard,
    create_admin_user_menu_keyboard, create_admin_order_menu_keyboard,
    create_admin_settings_menu_keyboard, create_admin_broadcast_menu_keyboard,
    clear_ban_cache
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg
from config.settings import settings as app_settings
from telegram.ext import ConversationHandler
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# Conversation states for restock / inventory management
WAITING_FOR_KEYS = 1   # legacy — kept for compatibility
WAITING_FOR_INV  = 2   # new generic inventory management conversation


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command - show admin panel.

    Any admin tier can reach this far (``@admin_only``). A verified 2FA
    session is only additionally required while
    ``config.settings.ADMIN_2FA_ENABLED`` is True — see utils/permissions.py
    is_2fa_enforced(). Currently disabled by default, so admins go straight
    through with no /admin_login prompt.
    """
    from utils.permissions import has_valid_session, is_2fa_enforced
    user_id = update.effective_user.id
    if is_2fa_enforced() and not has_valid_session(user_id):
        await update.message.reply_text(
            "🔒 *Admin login required*\n\n"
            "Send /admin_login — the bot will DM you a one-time code to verify it's really you.",
            parse_mode="Markdown",
        )
        return
    from handlers.admin_dashboard import render_dashboard
    await render_dashboard(update, context)


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin menu callback - return to admin main menu."""
    from utils.permissions import has_valid_session, is_2fa_enforced
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    if is_2fa_enforced() and not has_valid_session(update.effective_user.id):
        await query.message.reply_text(
            "🔒 Your admin session expired. Send /admin_login to verify with a fresh code."
        )
        return

    from handlers.admin_dashboard import render_dashboard
    await render_dashboard(update, context)


# ── Product-type labels for inventory management UI ─────────────────────────
_INV_TYPE_LABELS = {
    "KEY":               ("🔑", "Software Key"),
    "REDEEM_LINK":       ("🔗", "Redeem Link"),
    "ACCOUNT_LOGIN":     ("📧", "Account/Login"),
    "VOUCHER":           ("🎟", "Voucher/Gift Code"),
    "FILE":              ("📁", "File (legacy)"),
    "DOWNLOADABLE_FILE": ("📁", "Downloadable File"),
    "AUTO_GENERATED":    ("🤖", "Auto Generated"),
    "MANUAL_DELIVERY":   ("👤", "Manual Delivery"),
    "PREORDER":          ("⏳", "Pre-Order"),
    "SUBSCRIPTION":      ("♻️", "Subscription"),
    "BUNDLE":            ("📦", "Bundle"),
    "SERVICE":           ("🛠️", "Service"),
    "EXTERNAL_DELIVERY": ("🌐", "External Delivery"),
}
# Types backed by the product_keys table (shared with services/inventory.py KEY_BACKED_TYPES)
_KEY_BACKED = {
    ProductType.KEY, ProductType.REDEEM_LINK,
    ProductType.ACCOUNT_LOGIN, ProductType.VOUCHER,
}
_INV_INPUT_LABEL = {
    ProductType.KEY:          "🔑 Send keys, one per line, or upload a .txt file.",
    ProductType.REDEEM_LINK:  "🔗 Send redeem links, one per line, or upload a .txt file.",
    ProductType.ACCOUNT_LOGIN:"📧 Send account/login entries, one per line (full line kept intact), or upload a .txt file.",
    ProductType.VOUCHER:      "🎟 Send voucher codes, one per line, or upload a .txt file.",
}
_INV_PER_PAGE = 8


def _count_key_backed_available(session, product_id: int, variant_id=None) -> int:
    """Count available (unsold, unreserved) ProductKey rows for a product/variant."""
    q = session.query(ProductKey).filter(
        ProductKey.product_id == product_id,
        ProductKey.is_sold == False,   # noqa: E712
        ProductKey.reservation_id == None,  # noqa: E711
    )
    if variant_id is not None:
        q = q.filter(ProductKey.variant_id == variant_id)
    return q.count()


def _count_key_backed_reserved(session, product_id: int, variant_id=None) -> int:
    """Count reserved ProductKey rows (reservation_id set, not sold)."""
    q = session.query(ProductKey).filter(
        ProductKey.product_id == product_id,
        ProductKey.is_sold == False,   # noqa: E712
        ProductKey.reservation_id != None,  # noqa: E711
    )
    if variant_id is not None:
        q = q.filter(ProductKey.variant_id == variant_id)
    return q.count()


def _build_inv_product_list_keyboard(products, page: int, total_pages: int):
    """Build paginated product list keyboard for inventory management."""
    keyboard = []
    for p in products:
        ptype_name = p.product_type.name if p.product_type else "UNKNOWN"
        emoji, _ = _INV_TYPE_LABELS.get(ptype_name, ("📦", ptype_name))
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {p.name}",
            callback_data=f"inv_prod_{p.id}"
        )])
    # Pagination row
    pag_row = []
    if page > 0:
        pag_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"inv_page_{page - 1}"))
    if page < total_pages - 1:
        pag_row.append(InlineKeyboardButton("Next ▶", callback_data=f"inv_page_{page + 1}"))
    if pag_row:
        keyboard.append(pag_row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])
    return InlineKeyboardMarkup(keyboard)


async def _show_inv_product_list(query_or_update, context, page: int = 0):
    """Render (or re-render) the inventory product list at a given page."""
    with get_db_session() as session:
        all_products = session.query(Product).filter_by(is_active=True).order_by(Product.id).all()
        total = len(all_products)
        total_pages = max(1, (total + _INV_PER_PAGE - 1) // _INV_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        slice_ = all_products[page * _INV_PER_PAGE:(page + 1) * _INV_PER_PAGE]

        # Snapshot attributes before session closes
        rows = []
        for p in slice_:
            rows.append({
                "id": p.id, "name": p.name,
                "product_type": p.product_type,
                "stock_count": p.stock_count,
            })
        total_pages_snap = total_pages
        page_snap = page
        total_snap = total

    class _FakeProduct:
        def __init__(self, d):
            self.id = d["id"]
            self.name = d["name"]
            self.product_type = d["product_type"]
            self.stock_count = d["stock_count"]

    products = [_FakeProduct(r) for r in rows]
    kb = _build_inv_product_list_keyboard(products, page_snap, total_pages_snap)
    plural = "s" if total_snap != 1 else ""
    header = (
        f"📦 Manage Inventory — Page {page_snap + 1}/{total_pages_snap} "
        f"({total_snap} product{plural})\n\n"
        "Select a product to view its inventory:"
    )
    if total_snap == 0:
        header = "📦 Manage Inventory\n\nNo products found. Create a product first."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_products")]])

    query = getattr(query_or_update, 'callback_query', query_or_update)
    try:
        try:
            await query.edit_message_text(header, reply_markup=kb)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def admin_manage_inventory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 📦 Manage Inventory button — show all products (paginated)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    await _show_inv_product_list(query, context, page=0)


async def admin_inv_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination for inventory product list."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        return
    page = int(query.data.split("_")[-1])
    await _show_inv_product_list(query, context, page=page)


async def admin_inv_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show per-product inventory detail and type-appropriate actions."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    _override = context.user_data.pop("_cb_data_override", None)
    product_id = int(_override) if _override else int(query.data.split("_")[-1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text(
                    "❌ Product not found.",
                    reply_markup=create_admin_product_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        ptype = product.product_type
        ptype_name = ptype.name if ptype else "UNKNOWN"
        emoji, label = _INV_TYPE_LABELS.get(ptype_name, ("📦", ptype_name))
        p_name = product.name
        p_id   = product.id
        p_stock = product.stock_count
        variants = [(v.id, v.name, v.is_active) for v in product.variants]
        has_variants = bool(variants)

        has_delivery_template = bool(product.delivery_format_template)

        if ptype in _KEY_BACKED:
            avail = _count_key_backed_available(session, p_id)
            resvd = _count_key_backed_reserved(session, p_id)
            inv_info = f"Available: {avail}\nReserved:  {resvd}"
        elif ptype in (ProductType.FILE, ProductType.DOWNLOADABLE_FILE):
            inv_info = "File delivery — no discrete inventory rows."
        else:
            inv_info = f"Stock counter: {p_stock}"

    # Build the message
    msg = (
        f"{emoji} {p_name}\n"
        f"Type: {label} ({ptype_name})\n"
        f"{'━' * 30}\n"
        f"{inv_info}"
    )
    if has_variants:
        msg += f"\nVariants: {len([v for v in variants if v[2]])}"
    if ptype in _KEY_BACKED:
        msg += f"\nDelivery format: {'📄 Custom template set' if has_delivery_template else 'Default (raw text)'}"

    keyboard = []

    if ptype in _KEY_BACKED:
        if has_variants:
            keyboard.append([InlineKeyboardButton(
                "➕ Add Stock (select variant)", callback_data=f"inv_varsel_{p_id}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                "➕ Add Stock", callback_data=f"inv_add_{p_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            "📄 Set Delivery Format", callback_data=f"delivery_fmt_{p_id}"
        )])

    elif ptype in (ProductType.FILE, ProductType.DOWNLOADABLE_FILE):
        msg += (
            "\n\n📁 File products deliver via download link or Telegram file_id.\n"
            "Edit the product to update its file/link configuration."
        )
        keyboard.append([InlineKeyboardButton(
            "✏️ Edit Product", callback_data=f"admin_edit_product"
        )])

    elif ptype == ProductType.AUTO_GENERATED:
        msg += (
            "\n\n🤖 Auto Generated product.\n"
            "Values are generated at fulfilment time from the generator config\n"
            "(edit via Admin Control Center)."
        )

    elif ptype == ProductType.MANUAL_DELIVERY:
        msg += (
            "\n\n👤 Manual Delivery\n"
            "No automatic inventory required.\n"
            "Admin fulfils orders via the Delivery Queue."
        )
        keyboard.append([InlineKeyboardButton(
            "📋 Delivery Queue", callback_data="acc:sec:delivery"
        )])

    elif ptype == ProductType.PREORDER:
        msg += (
            "\n\n⏳ Pre-Order product.\n"
            "Orders queue until the admin manually fulfils them."
        )

    elif ptype == ProductType.SUBSCRIPTION:
        msg += (
            "\n\n♻️ Subscription product.\n"
            "Subscription plans are managed via the Admin Control Center."
        )

    elif ptype == ProductType.BUNDLE:
        msg += (
            "\n\n📦 Bundle product.\n"
            "Availability is derived from component product inventory.\n"
            "Manage each component's inventory separately."
        )

    elif ptype == ProductType.SERVICE:
        msg += (
            "\n\n🛠️ Service Product\n"
            "No automatic stock inventory required.\n"
            "Service fulfilment is configured in the Admin Control Center."
        )

    elif ptype == ProductType.EXTERNAL_DELIVERY:
        msg += (
            "\n\n🌐 External Delivery product.\n"
            "Delivery is handled by an external API/webhook.\n"
            "No local inventory management needed."
        )

    # V28: Clone & Template shortcut on every product detail view
    keyboard.append([
        InlineKeyboardButton("📄 Clone Product",    callback_data=f"pct:clone_opts:{p_id}"),
        InlineKeyboardButton("💾 Save as Template", callback_data=f"pct:tpl:save:{p_id}"),
    ])
    keyboard.append([InlineKeyboardButton("🔙 Back to Inventory List", callback_data="admin_manage_inventory")])

    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_inv_varsel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show variant selection for inventory management."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        return

    product_id = int(query.data.split("_")[-1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        p_name = product.name
        p_id = product.id
        variants_data = [
            (v.id, v.name, v.is_active,
             _count_key_backed_available(session, p_id, v.id),
             _count_key_backed_reserved(session, p_id, v.id))
            for v in product.variants
        ]

    keyboard = []
    for vid, vname, vactive, avail, resvd in variants_data:
        status = "" if vactive else " [inactive]"
        keyboard.append([InlineKeyboardButton(
            f"{vname}{status} — avail: {avail}, reserved: {resvd}",
            callback_data=f"inv_add_{product_id}_v{vid}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"inv_prod_{product_id}")])

    try:
        await query.edit_message_text(
            f"📦 {p_name} — Select a variant to add inventory:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_inv_add_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the manage-inventory conversation. Sets context and prompts for stock input."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Parse callback: inv_add_{product_id} or inv_add_{product_id}_v{variant_id}
    parts = query.data.split("_")
    # parts: ['inv', 'add', '{pid}'] or ['inv', 'add', '{pid}', 'v{vid}']
    product_id = int(parts[2])
    variant_id = None
    if len(parts) >= 4 and parts[3].startswith("v"):
        variant_id = int(parts[3][1:])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        ptype = product.product_type
        p_name = product.name
        p_id   = product.id
        avail = _count_key_backed_available(session, p_id, variant_id)
        resvd = _count_key_backed_reserved(session, p_id, variant_id)
        if variant_id is not None:
            v = session.query(ProductVariant).filter_by(id=variant_id).first()
            variant_name = v.name if v else f"#{variant_id}"
        else:
            variant_name = None

    # Validate variant belongs to this product (data-integrity guard)
    if variant_id is not None:
        with get_db_session() as _chk:
            _v = _chk.query(ProductVariant).filter_by(id=variant_id, product_id=product_id).first()
            if not _v:
                try:
                    await query.edit_message_text(
                        "❌ Variant not found for this product.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"inv_prod_{product_id}")]]),
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return ConversationHandler.END

    context.user_data['inv_product_id']   = product_id
    context.user_data['inv_variant_id']   = variant_id
    context.user_data['inv_product_type'] = ptype

    label = _INV_INPUT_LABEL.get(ptype, "Send inventory items, one per line, or upload a .txt file.")
    vinfo = f"\nVariant: {variant_name}" if variant_name else ""
    prompt = (
        f"📦 {p_name}{vinfo}\n"
        f"Available: {avail} | Reserved: {resvd}\n"
        f"{'━' * 30}\n"
        f"{label}\n\n"
        "Type 'cancel' or press Cancel to abort."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]]
    try:
        await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return WAITING_FOR_INV


async def _parse_inv_document(document, bot):
    """Download and parse a .txt document. Returns list of lines.
    Handles UTF-8 BOM, CRLF/LF, blank lines, and enforces max 5 MB.
    Raises ValueError with a user-friendly message on error.
    """
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB limit
    if document.file_size and document.file_size > MAX_BYTES:
        raise ValueError(f"File too large ({document.file_size // 1024} KB). Maximum is 5 MB.")
    tg_file = await bot.get_file(document.file_id)
    raw = await tg_file.download_as_bytearray()
    # Decode with BOM stripping
    try:
        text = raw.decode('utf-8-sig')  # utf-8-sig strips BOM automatically
    except UnicodeDecodeError:
        try:
            text = raw.decode('latin-1')
        except Exception:
            raise ValueError("Could not decode file. Please save as UTF-8 and try again.")
    # Normalize line endings, split, strip, drop blanks
    lines = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    lines = [l for l in lines if l]
    return lines


async def handle_inv_add_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .txt file upload during manage-inventory conversation."""
    if not has_permission(update.effective_user.id, "manage_products"):
        return WAITING_FOR_INV

    document = update.message.document
    if not document:
        await update.message.reply_text(
            "❌ Please upload a .txt file or paste items directly.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]])
        )
        return WAITING_FOR_INV

    product_id = context.user_data.get('inv_product_id')
    variant_id = context.user_data.get('inv_variant_id')
    ptype      = context.user_data.get('inv_product_type')

    if not product_id or not ptype:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END

    try:
        lines = await _parse_inv_document(document, context.bot)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ {e}\n\nPlease try again or paste items directly.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]])
        )
        return WAITING_FOR_INV
    except Exception:
        await update.message.reply_text(
            "❌ Error reading file. Please try again or paste items directly.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]])
        )
        return WAITING_FOR_INV

    if not lines:
        await update.message.reply_text(
            "❌ No items found in file (empty or all blank lines). Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]])
        )
        return WAITING_FOR_INV

    await _do_inv_import(update, context, lines, product_id, variant_id, ptype)
    return ConversationHandler.END


async def handle_inv_add_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pasted text during manage-inventory conversation."""
    if not has_permission(update.effective_user.id, "manage_products"):
        return WAITING_FOR_INV

    text = update.message.text or ""
    if text.strip().lower() in ("cancel", "/cancel"):
        await cancel_manage_inventory(update, context)
        return ConversationHandler.END

    product_id = context.user_data.get('inv_product_id')
    variant_id = context.user_data.get('inv_variant_id')
    ptype      = context.user_data.get('inv_product_type')

    if not product_id or not ptype:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END

    # Normalize and split
    lines = [l.strip() for l in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    lines = [l for l in lines if l]
    if ptype == ProductType.ACCOUNT_LOGIN:
        from services.inventory_import import parse_account_inventory
        lines = parse_account_inventory(text)

    if not lines:
        await update.message.reply_text(
            "❌ No items found. Please paste items (one per line) or upload a .txt file.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_inv")]])
        )
        return WAITING_FOR_INV

    await _do_inv_import(update, context, lines, product_id, variant_id, ptype)
    return ConversationHandler.END


def _structure_lines_for_template(product, lines):
    """V17 — Formatted Account Delivery: if ``product`` has an admin-defined
    ``delivery_format_template``, convert plain / pipe-delimited bulk-upload
    lines into JSON ``key_value`` strings whose keys line up with the
    template's ``{placeholder}`` names (see services/structured_delivery.py).

    Products with no template configured pass ``lines`` through completely
    unchanged — this is what keeps every existing product's raw-text stock
    working exactly as before.
    """
    template = getattr(product, "delivery_format_template", None)
    if not template:
        return lines
    from services.structured_delivery import extract_placeholders, bulk_parse_structured_lines
    placeholders = extract_placeholders(template)
    if not placeholders:
        return lines
    return bulk_parse_structured_lines("\n".join(lines), placeholders)


async def _do_inv_import(update, context, lines, product_id, variant_id, ptype):
    """Core import logic: deduplicate and insert inventory items."""
    from services.inventory_import import dedupe_import

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            await update.message.reply_text("❌ Product not found.")
            for k in ("inv_product_id", "inv_variant_id", "inv_product_type"):
                context.user_data.pop(k, None)
            return

        # If admin has configured a delivery-format template for this
        # product, bulk-upload lines are field-delimited (e.g.
        # "user@x.com|Passw0rd|backup@x.com|2026-12-31") and get converted to
        # JSON here so they match the template's placeholders at delivery
        # time. No-op when no template is set (legacy raw-text unaffected).
        lines = _structure_lines_for_template(product, lines)

        # Snapshot availability *before* this import so we can detect a
        # genuine 0 → >0 restock transition for the automatic broadcast.
        avail_before = _count_key_backed_available(session, product_id, variant_id)

        existing_fps = {
            row[0] for row in session.query(ProductKey.key_fingerprint)
            .filter(
                ProductKey.product_id == product_id,
                ProductKey.key_fingerprint.isnot(None),
            )
            .all()
        }
        if variant_id is not None:
            existing_fps_var = {
                row[0] for row in session.query(ProductKey.key_fingerprint)
                .filter(
                    ProductKey.product_id == product_id,
                    ProductKey.variant_id == variant_id,
                    ProductKey.key_fingerprint.isnot(None),
                )
                .all()
            }
            # For duplicate detection, use variant-specific fingerprints
            existing_fps = existing_fps_var

        accepted, dupes, invalid = dedupe_import(lines, product_type=ptype, existing_fps=existing_fps)

        if accepted:
            session.bulk_save_objects([
                ProductKey(
                    product_id=product_id,
                    variant_id=variant_id,
                    key_value=kv,
                    key_fingerprint=fp,
                    is_sold=False,
                )
                for kv, fp in accepted
            ])
            session.flush()
            # Update stock count for product (or variant if applicable)
            if variant_id is not None:
                v = session.query(ProductVariant).filter_by(id=variant_id).first()
                if v:
                    v.stock_count = (v.stock_count or 0) + len(accepted)
            else:
                product.stock_count = _count_key_backed_available(session, product_id)
            session.commit()

        p_name = product.name
        avail_after = _count_key_backed_available(session, product_id, variant_id)

    # Automatic Restock Broadcast: only fires on a genuine 0 → >0 transition
    # of the *product-level* available stock (top-level, non-variant path —
    # variant-scoped restocks are handled where variants are edited). The
    # broadcast module re-checks the ON/OFF setting itself, so it's always
    # safe to call unconditionally here.
    if variant_id is None and avail_before == 0 and avail_after > 0:
        try:
            from handlers.admin_broadcast_center import send_restock_broadcast
            await send_restock_broadcast(context.bot, product_id, variant_id=None)
        except Exception:
            logger.exception("restock broadcast trigger failed for product_id=%s", product_id)

        # Channel Auto-Post (V18): best-effort restock post to the
        # configured channel. Independent of the eligible-users broadcast
        # above and gated by its own on/off + channel settings.
        try:
            from services.channel_poster import post_restock
            await post_restock(context.bot, product_id, variant_id=None, available=avail_after)
        except Exception:
            logger.exception("channel auto-post (restock) failed for product_id=%s", product_id)

    # Build result message — never print raw key values
    result_lines = [f"✅ Inventory import complete for *{p_name}*:"]
    result_lines.append(f"  Added:            {len(accepted)}")
    if dupes:
        result_lines.append(f"  Duplicates skipped: {len(dupes)}")
    if invalid:
        result_lines.append(f"  Invalid skipped:  {len(invalid)}")
    result_lines.append(f"  Available now:    {avail_after}")

    keyboard = [
        [InlineKeyboardButton("➕ Add More", callback_data=f"inv_add_{product_id}" + (f"_v{variant_id}" if variant_id else ""))],
        [InlineKeyboardButton("🔙 Product Detail", callback_data=f"inv_prod_{product_id}")],
        [InlineKeyboardButton("📦 Inventory List", callback_data="admin_manage_inventory")],
    ]
    await update.message.reply_text(
        "\n".join(result_lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Clear inventory context
    for k in ("inv_product_id", "inv_variant_id", "inv_product_type"):
        context.user_data.pop(k, None)


async def cancel_manage_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the manage-inventory conversation."""
    query = update.callback_query
    kb = create_admin_product_menu_keyboard()
    if query:
        await query.answer()
        try:
            try:
                await query.edit_message_text("❌ Inventory management cancelled.", reply_markup=kb)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            pass
    else:
        await update.message.reply_text("❌ Inventory management cancelled.", reply_markup=kb)
    for k in ("inv_product_id", "inv_variant_id", "inv_product_type"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ── Backward-compat redirect for old admin_restock_keys callback ─────────────
async def admin_restock_keys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect legacy admin_restock_keys to the new manage inventory flow."""
    return await admin_manage_inventory_callback(update, context)


async def admin_select_product_restock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy select_product_ handler — now routes into the new inventory detail view."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    # Redirect to new product detail view
    product_id = int(query.data.split("_")[2])
    context.user_data["_cb_data_override"] = str(product_id)
    return await admin_inv_product_callback(update, context)


async def admin_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin products menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        try:
            await query.edit_message_text(
                "📦 Product Management\n\nSelect an option:",
                reply_markup=create_admin_product_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_manage_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin category management menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        try:
            await query.edit_message_text(
                "📁 Category Management\n\nSelect an option:",
                reply_markup=create_admin_category_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_view_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all categories and subcategories."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        categories = session.query(Category).all()

        if not categories:
            try:
                await query.edit_message_text("📁 No categories found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        message = "📁 Categories & Subcategories:\n\n"

        for cat in categories:
            message += f"📦 {cat.name} (ID: #{cat.id})\n"
            if cat.description:
                message += f"   {cat.description}\n"

            subcategories = session.query(Subcategory).filter_by(category_id=cat.id).all()
            if subcategories:
                for subcat in subcategories:
                    message += f"   └─ {subcat.name} (ID: #{subcat.id})\n"

            message += "\n"

        try:
            await query.edit_message_text(message, reply_markup=create_admin_category_menu_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin users menu — delegates to the Advanced User Profile panel."""
    from handlers.admin_user_profile import up_menu
    return await up_menu(update, context)


async def admin_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin orders menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        try:
            await query.edit_message_text(
                "🛍 Order Management\n\nSelect an option:",
                reply_markup=create_admin_order_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin settings menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        try:
            await query.edit_message_text(
                "⚙️ Store Settings\n\nSelect an option:",
                reply_markup=create_admin_settings_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_toggle_currency_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip the ``show_currency_toggle_button`` config flag and refresh the
    Store Settings menu in place so the admin sees the new state immediately."""
    query = update.callback_query

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    new_val = not cfg.get_bool("show_currency_toggle_button", False)
    cfg.set("show_currency_toggle_button", new_val)
    await query.answer(f"Currency toggle button: {'Shown' if new_val else 'Hidden'}", show_alert=False)

    try:
        try:
            await query.edit_message_text(
                "⚙️ Store Settings\n\nSelect an option:",
                reply_markup=create_admin_settings_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        pass


async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin broadcast menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        try:
            await query.edit_message_text(
                "📢 Broadcast Messages\n\nSelect an option:",
                reply_markup=create_admin_broadcast_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_view_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of users."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    with get_db_session() as session:
        # Get all users
        all_users = session.query(User).order_by(User.created_at.desc()).all()

        if not all_users:
            try:
                await query.edit_message_text(
                    "👥 No users found.",
                    reply_markup=create_admin_user_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Pagination settings
        items_per_page = 5
        total_pages = (len(all_users) + items_per_page - 1) // items_per_page
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        users = all_users[start_idx:end_idx]

        # Build user selection keyboard
        keyboard = []
        for user in users:
            status_icon = "🚫" if user.is_banned else "✅"
            username_display = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_icon} {username_display} - {format_price(user.wallet_balance)}",
                    callback_data=f"view_user_{user.id}"
                )
            ])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_users_page_{page-1}"))
            pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_users_page_{page+1}"))
            keyboard.append(pagination_row)

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_users")])

        try:
            await query.edit_message_text(
                "👥 User List\n\nSelect a user to view details:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_user_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual user details with Ban/Unban button."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Handle pagination - redirect back to user list
    if "admin_view_users_page_" in query.data:
        return await admin_view_users_callback(update, context)

    # Extract user ID from callback data
    user_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            try:
                await query.edit_message_text(
                    "❌ User not found.",
                    reply_markup=create_admin_user_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status=OrderStatus.COMPLETED).with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        try:
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle banning a user."""
    query = update.callback_query
    await query.answer("✅ User banned successfully!", show_alert=True)

    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract user ID from callback data
    user_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            try:
                await query.edit_message_text(
                    "❌ User not found.",
                    reply_markup=create_admin_user_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Store telegram_id before committing
        telegram_id = user.telegram_id

        user.is_banned = True
        session.commit()

        # Clear ban cache for this user
        clear_ban_cache(telegram_id)
        log_admin_action(update.effective_user.id, "user.ban",
                         target_type="user", target_id=user_id,
                         details=f"telegram_id={telegram_id}")

        # Refresh user details page - get updated data
        user = session.query(User).filter_by(id=user_id).first()

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status=OrderStatus.COMPLETED).with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        try:
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_unban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unbanning a user."""
    query = update.callback_query
    await query.answer("✅ User unbanned successfully!", show_alert=True)

    if not has_permission(update.effective_user.id, "manage_users"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract user ID from callback data
    user_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            try:
                await query.edit_message_text(
                    "❌ User not found.",
                    reply_markup=create_admin_user_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Store telegram_id before committing
        telegram_id = user.telegram_id

        user.is_banned = False
        session.commit()

        # Clear ban cache for this user
        clear_ban_cache(telegram_id)
        log_admin_action(update.effective_user.id, "user.unban",
                         target_type="user", target_id=user_id,
                         details=f"telegram_id={telegram_id}")

        # Refresh user details page - get updated data
        user = session.query(User).filter_by(id=user_id).first()

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status=OrderStatus.COMPLETED).with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        try:
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_view_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of recent orders with management buttons."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    with get_db_session() as session:
        # Get all orders
        all_orders = session.query(Order).order_by(Order.created_at.desc()).all()

        if not all_orders:
            try:
                await query.edit_message_text(
                    "🛍 No orders found.",
                    reply_markup=create_admin_order_menu_keyboard()
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Pagination settings
        orders_per_page = 5
        total_pages = (len(all_orders) + orders_per_page - 1) // orders_per_page
        start_idx = page * orders_per_page
        end_idx = start_idx + orders_per_page
        orders = all_orders[start_idx:end_idx]

        # Build message
        message = f"🛍 Recent Orders (Page {page + 1}/{total_pages}):\n\n"

        # Build keyboard with order buttons
        keyboard = []

        for order in orders:
            user = session.query(User).filter_by(id=order.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            # Format status emoji
            status_emoji = {
                OrderStatus.PROCESSING: "⏳",
                OrderStatus.COMPLETED: "✅",
                OrderStatus.CANCELLED: "❌"
            }.get(order.status, "❓")

            # Button text: ORD-YYYYMMDD-NNNNNN | User | Amount
            from utils.helpers import format_order_id as _fmt_oid_adm
            button_text = f"{status_emoji} {_fmt_oid_adm(order.id, order.created_at)} | @{username} | {format_price(order.total_amount)}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_order_{order.id}")])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_orders_page_{page-1}"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_orders_page_{page+1}"))
            if pagination_row:
                keyboard.append(pagination_row)

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_orders")])

        try:
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def handle_restock_keys_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy file handler — delegates to new handle_inv_add_file."""
    return await handle_inv_add_file(update, context)


async def handle_restock_keys_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy paste handler — delegates to new handle_inv_add_paste."""
    return await handle_inv_add_paste(update, context)


async def handle_welcome_message_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome message update from admin."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        await update.message.reply_text("⛔ Access denied.")
        return

    new_welcome_message = update.message.text

    with get_db_session() as session:
        settings = session.query(Settings).first()

        if not settings:
            settings = Settings()
            session.add(settings)

        settings.welcome_message = new_welcome_message
        settings.updated_at = datetime.utcnow()
        session.commit()

        await update.message.reply_text("✅ Welcome message updated successfully!")


async def handle_logo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle store logo upload from admin."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Get the uploaded photo
    photo = update.message.photo[-1]  # Get highest resolution

    # Download photo
    file = await context.bot.get_file(photo.file_id)
    logo_path = os.path.join(app_settings.LOGOS_DIR, f"store_logo_{int(datetime.utcnow().timestamp())}.jpg")

    # Ensure directory exists
    os.makedirs(app_settings.LOGOS_DIR, exist_ok=True)

    await file.download_to_drive(logo_path)

    # Update settings
    with get_db_session() as session:
        settings = session.query(Settings).first()

        if not settings:
            settings = Settings()
            session.add(settings)

        settings.store_logo_path = logo_path
        settings.updated_at = datetime.utcnow()
        session.commit()

        await update.message.reply_text("✅ Store logo updated successfully!")


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text-only broadcast to all users."""
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await update.message.reply_text("⛔ Access denied.")
        return

    broadcast_text = update.message.text

    with get_db_session() as session:
        # Get all users
        users = session.query(User).filter_by(is_banned=False).all()

        sent_count = 0

        for user in users:
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=broadcast_text
                )
                sent_count += 1
            except Exception as e:
                logger.warning("Broadcast: failed to send to user %s: %s", user.telegram_id, e)

        # Save broadcast record
        broadcast = Broadcast(
            message_text=broadcast_text,
            sent_count=sent_count
        )
        session.add(broadcast)
        session.commit()

        await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users!")


async def handle_broadcast_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image + text broadcast to all users (as separate messages)."""
    if not has_permission(update.effective_user.id, "manage_broadcasts"):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Get image and caption
    photo = update.message.photo[-1]  # Get highest resolution
    caption_text = update.message.caption or ""

    # Download photo
    file = await context.bot.get_file(photo.file_id)
    image_path = os.path.join(app_settings.ASSETS_DIR, f"broadcast_{int(datetime.utcnow().timestamp())}.jpg")

    os.makedirs(app_settings.ASSETS_DIR, exist_ok=True)
    await file.download_to_drive(image_path)

    with get_db_session() as session:
        # Get all users
        users = session.query(User).filter_by(is_banned=False).all()

        sent_count = 0

        for user in users:
            try:
                # Send image first
                with open(image_path, 'rb') as img:
                    await context.bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=img
                    )

                # Send text as separate message
                if caption_text:
                    await context.bot.send_message(
                        chat_id=user.telegram_id,
                        text=caption_text
                    )

                sent_count += 1
            except Exception as e:
                logger.warning("Broadcast: failed to send to user %s: %s", user.telegram_id, e)

        # Save broadcast record
        broadcast = Broadcast(
            message_text=caption_text,
            image_path=image_path,
            sent_count=sent_count
        )
        session.add(broadcast)
        session.commit()

        await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users!")


async def handle_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban/unban user command."""
    if not has_permission(update.effective_user.id, "manage_users"):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Expected format: telegram_id ban/unban
    try:
        parts = update.message.text.split()
        telegram_id = int(parts[0])
        action = parts[1].lower()

        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=telegram_id).first()

            if not user:
                await update.message.reply_text("❌ User not found.")
                return

            if action == "ban":
                user.is_banned = True
                session.commit()
                await update.message.reply_text(f"✅ User {telegram_id} has been banned.")
            elif action == "unban":
                user.is_banned = False
                session.commit()
                await update.message.reply_text(f"✅ User {telegram_id} has been unbanned.")
            else:
                await update.message.reply_text("❌ Invalid action. Use 'ban' or 'unban'.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: telegram_id ban/unban")


async def handle_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order cancellation with wallet refund."""
    if not has_permission(update.effective_user.id, "manage_orders"):
        await update.message.reply_text("⛔ Access denied.")
        return

    try:
        import re as _re
        _raw = (update.message.text or "").strip()
        _m = _re.match(r"^ORD-\d{8}-0*(\d+)$", _raw, _re.IGNORECASE)
        if _m:
            order_id = int(_m.group(1))
        elif _raw.lstrip("#").isdigit():
            order_id = int(_raw.lstrip("#"))
        else:
            await update.message.reply_text(
                "❌ Invalid Order ID format.\n\n"
                "Expected: <code>ORD-YYYYMMDD-000001</code> or a numeric ID.",
                parse_mode="HTML",
            )
            return

        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                await update.message.reply_text("❌ Order not found.")
                return

            if order.status == OrderStatus.CANCELLED:
                await update.message.reply_text("❌ Order is already cancelled.")
                return

            # Refund to wallet
            user = session.query(User).filter_by(id=order.user_id).first()
            user.wallet_balance += order.total_amount
            session.commit()

            # Centralized lifecycle transition — syncs order.status via _LEGACY_MAP
            try:
                from services.order_lifecycle import transition
                from services.inventory import release_for_order
                from database.models import OrderLifecycleStatus
                transition(order.id, OrderLifecycleStatus.CANCELLED,
                           actor_type="admin", admin_id=update.effective_user.id,
                           reason="admin cancel + wallet refund")
                release_for_order(order.id, reason="admin_cancel")
            except Exception:
                logger.exception("lifecycle transition failed for order %s", order.id)

            await update.message.reply_text(
                f"✅ Order #{order_id} cancelled successfully!\n"
                f"💰 Refunded {format_price(order.total_amount)} to user's wallet."
            )

            # Notify user
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=f"❌ Order #{order_id} has been cancelled by admin.\n"
                     f"💰 {format_price(order.total_amount)} has been refunded to your wallet."
            )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: order_id")


async def handle_dispute_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order dispute status update."""
    if not has_permission(update.effective_user.id, "manage_orders"):
        await update.message.reply_text("⛔ Access denied.")
        return

    try:
        # Format: order_id status (opened/resolved)  — order_id may be ORD-YYYYMMDD-NNNNNN or numeric
        import re as _re
        parts = (update.message.text or "").split()
        _id_token = parts[0] if parts else ""
        _m2 = _re.match(r"^ORD-\d{8}-0*(\d+)$", _id_token, _re.IGNORECASE)
        if _m2:
            order_id = int(_m2.group(1))
        elif _id_token.lstrip("#").isdigit():
            order_id = int(_id_token.lstrip("#"))
        else:
            await update.message.reply_text(
                "❌ Invalid Order ID format.\n\n"
                "Expected: <code>ORD-YYYYMMDD-000001 opened/resolved</code>",
                parse_mode="HTML",
            )
            return
        status = parts[1].lower() if len(parts) > 1 else ""

        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                await update.message.reply_text("❌ Order not found.")
                return

            if status == "opened":
                order.dispute_status = DisputeStatus.OPENED
            elif status == "resolved":
                order.dispute_status = DisputeStatus.RESOLVED
            else:
                await update.message.reply_text("❌ Invalid status. Use 'opened' or 'resolved'.")
                return

            session.commit()
            await update.message.reply_text(f"✅ Order #{order_id} dispute status updated to: {status.upper()}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: order_id opened/resolved")


async def admin_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual order details with management buttons."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract order ID from callback data
    order_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            try:
                await query.edit_message_text(
                    "❌ Order not found.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_view_orders")]])
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Get user and order items
        user = session.query(User).filter_by(id=order.user_id).first()
        order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

        # Format status emoji
        status_emoji = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED: "✅",
            OrderStatus.CANCELLED: "❌"
        }.get(order.status, "❓")

        # Build message
        username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"
        message = f"📋 Order Details\n\n"
        message += f"Order ID: #{order.id}\n"
        message += f"Status: {status_emoji} {order.status.value}\n"
        message += f"User: @{username} ({user.telegram_id if user else 'Unknown'})\n"
        message += f"Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        message += f"Total: {format_price(order.total_amount)}\n\n"

        message += "📦 Items:\n"
        for item in order_items:
            product = session.query(Product).filter_by(id=item.product_id).first()
            product_name = product.name if product else "Unknown Product"
            message += f"• {product_name} x{item.quantity} = {format_price(item.price * item.quantity)}\n"

            # Add delivered assets (keys or download links)
            if item.delivered_asset:
                if product and product.product_type == ProductType.KEY:
                    message += f"  🔐 Keys:\n{item.delivered_asset}\n"
                elif product and product.product_type == ProductType.FILE:
                    message += f"  🔗 Download: {item.delivered_asset}\n"
                message += "\n"

        # Admin timeline — full detail incl. actor / admin_id / reason.
        try:
            from services.order_lifecycle import render_timeline
            tl = render_timeline(order.id, limit=20)
            if tl and tl != "— no history yet —":
                message += "\n📜 Timeline:\n" + tl + "\n"
        except Exception:
            pass

        # Build keyboard with management buttons
        keyboard = []

        # Status-specific actions
        if order.status == OrderStatus.PROCESSING:
            keyboard.append([InlineKeyboardButton("✅ Mark as Completed", callback_data=f"complete_order_{order.id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order.id}")])
        elif order.status == OrderStatus.CANCELLED:
            keyboard.append([InlineKeyboardButton("🔄 Reactivate Order", callback_data=f"reactivate_order_{order.id}")])

        # Manual redelivery — always available for orders that have items
        if order_items:
            keyboard.append([InlineKeyboardButton(
                "🔁 Resend Delivery",
                callback_data=f"admin_redeliver_{order.id}"
            )])

        # Navigation buttons
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_view_orders")])

        try:
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_complete_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark an order as completed."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    order_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            try:
                await query.edit_message_text("❌ Order not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Centralized lifecycle transition — syncs order.status via _LEGACY_MAP
        try:
            from services.order_lifecycle import transition
            from database.models import OrderLifecycleStatus
            transition(order.id, OrderLifecycleStatus.COMPLETED,
                       actor_type="admin", admin_id=update.effective_user.id,
                       reason="admin mark completed", bot=context.bot)
        except Exception:
            logger.exception("lifecycle transition failed for order %s", order.id)

        await query.answer("✅ Order marked as completed!", show_alert=True)

        # Refresh order details
        await admin_order_detail_callback(update, context)


async def admin_confirm_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for manual confirmation."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        # Get all pending transactions
        transactions = session.query(Transaction).filter_by(status=TransactionStatus.PENDING).order_by(Transaction.created_at.desc()).all()

        if not transactions:
            keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(
                    "✅ No pending payments to confirm.",
                    reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Build keyboard with transaction buttons
        keyboard = []
        for txn in transactions:
            user = session.query(User).filter_by(id=txn.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            payment_method = txn.payment_method.value.replace('_', ' ').title()

            button_text = f"⏳ Txn #{txn.id} | @{username} | {format_price(txn.amount)} | {payment_method}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"confirm_payment_{txn.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = f"✅ Manual Payment Confirmation ({len(transactions)} pending)\n\nSelect a transaction to confirm:"

        try:
            await query.edit_message_text(message, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_cancel_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for cancellation."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        # Get all pending transactions
        transactions = session.query(Transaction).filter_by(status=TransactionStatus.PENDING).order_by(Transaction.created_at.desc()).all()

        if not transactions:
            keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(
                    "✅ No pending payments to cancel.",
                    reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Build keyboard with transaction buttons — each pending order gets
        # its own row, plus a Cancel and a hard Delete action.
        keyboard = []
        for txn in transactions:
            user = session.query(User).filter_by(id=txn.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            payment_method = txn.payment_method.value.replace('_', ' ').title()
            label = f"#{txn.id} @{username} {format_price(txn.amount)} {payment_method}"
            keyboard.append([
                InlineKeyboardButton(f"❌ Cancel {label}", callback_data=f"cancel_payment_{txn.id}"),
            ])
            keyboard.append([
                InlineKeyboardButton(f"🗑 Delete {label}", callback_data=f"delete_payment_{txn.id}"),
            ])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (
            f"❌ Cancel / Delete Payments ({len(transactions)} pending)\n\n"
            "Cancel marks the order as cancelled (keeps a record). "
            "Delete permanently removes the order.\n\n"
            "Select an action:"
        )

        try:
            await query.edit_message_text(message, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_confirm_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually confirm a pending payment transaction."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    txn_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        from database import Transaction, TransactionStatus
        from datetime import datetime

        txn = session.query(Transaction).filter_by(id=txn_id).first()

        if not txn:
            try:
                await query.edit_message_text("❌ Transaction not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        if txn.status != TransactionStatus.PENDING:
            await query.answer(f"⚠️ Transaction is already {txn.status.value}", show_alert=True)
            return

        # Idempotency guard — stable reference is the transaction's own DB
        # id (never the Telegram update_id, which changes on button-tap
        # redelivery/retry). Defense-in-depth alongside the atomic
        # conditional UPDATE below: if the claim itself raises, fail
        # CLOSED (no credit).
        #
        # Uses claim_locked() (not claim()) because we are already inside
        # this outer get_db_session() block — claim() opens and closes its
        # OWN nested session, which would close the shared scoped_session
        # out from under this handler and detach `txn`.
        try:
            from services.idempotency import claim_locked as _idem_claim_locked
            if not _idem_claim_locked(session, "admin_approve", f"tx:{txn_id}"):
                await query.answer("⚠️ Already processed.", show_alert=True)
                return
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).error(
                "idempotency.claim_locked raised for admin_approve tx %s — refusing "
                "to credit wallet (fail closed)", txn_id, exc_info=True,
            )
            await query.answer("❌ Approval failed — please retry.", show_alert=True)
            return

        # Atomically flip PENDING → COMPLETED so a double-click can't
        # credit the wallet twice.
        flipped = session.query(Transaction).filter(
            Transaction.id == txn_id,
            Transaction.status == TransactionStatus.PENDING,
        ).update(
            {
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
        if flipped == 0:
            await query.answer("⚠️ Already processed.", show_alert=True)
            return

        # Atomic wallet credit
        session.query(User).filter(User.id == txn.user_id).update(
            {User.wallet_balance: User.wallet_balance + txn.amount},
            synchronize_session=False,
        )
        session.commit()

        user = session.query(User).filter_by(id=txn.user_id).first()
        user_telegram_id = user.telegram_id if user else None
        amount = txn.amount
        new_balance = user.wallet_balance if user else 0

        await query.answer(f"✅ Payment confirmed! {format_price(amount)} added to user's wallet.", show_alert=True)
        log_admin_action(update.effective_user.id, "payment.approve",
                         target_type="transaction", target_id=txn_id,
                         details=f"user_id={txn.user_id} amount={amount}")

        # Notify user
        if user_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"✅ Payment Confirmed!\n\n💰 Amount: {format_price(amount)}\n💵 New Balance: {format_price(new_balance)}"
                )
            except Exception:
                import logging as _lg
                _lg.getLogger(__name__).warning("Notify user failed", exc_info=True)

        # Go back to payment confirmation menu
        await admin_confirm_order_menu(update, context)


async def admin_cancel_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending payment transaction."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    txn_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        txn = session.query(Transaction).filter_by(id=txn_id).first()

        if not txn:
            try:
                await query.edit_message_text("❌ Transaction not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        if txn.status != TransactionStatus.PENDING:
            await query.answer(f"⚠️ Transaction is already {txn.status.value}", show_alert=True)
            return

        # Mark transaction as cancelled — this is an admin-initiated
        # cancellation, not a payment failure, and CANCELLED is what frees
        # the user to create a new pending order right away.
        txn.status = TransactionStatus.CANCELLED
        session.commit()

        # Get details before session closes
        user = session.query(User).filter_by(id=txn.user_id).first()
        user_telegram_id = user.telegram_id if user else None
        amount = txn.amount

        await query.answer(f"✅ Payment cancelled!", show_alert=True)
        log_admin_action(update.effective_user.id, "payment.reject",
                         target_type="transaction", target_id=txn_id,
                         details=f"user_id={txn.user_id} amount={amount}")

        # Notify user
        if user_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"❌ Payment Cancelled\n\n💰 Amount: {format_price(amount)}\n\nYour payment was not confirmed. Please contact support if you believe this is an error."
                )
            except Exception:
                import logging as _lg
                _lg.getLogger(__name__).warning("Notify user failed", exc_info=True)

        # Go back to payment cancellation menu
        await admin_cancel_order_menu(update, context)


async def admin_delete_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permanently delete a pending payment transaction (manual admin action).

    Unlike cancelling (which keeps the row with status=CANCELLED for
    record-keeping), this hard-deletes the row from the database. Only
    ever allowed while the order is still PENDING — once it's completed
    it must be handled through refunds/order cancellation instead.
    """
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    txn_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        txn = session.query(Transaction).filter_by(id=txn_id).first()

        if not txn:
            await query.answer("❌ Transaction not found.", show_alert=True)
            await admin_cancel_order_menu(update, context)
            return

        if txn.status != TransactionStatus.PENDING:
            await query.answer(f"⚠️ Transaction is already {txn.status.value}", show_alert=True)
            return

        # Get details before the row (and session) go away
        user = session.query(User).filter_by(id=txn.user_id).first()
        user_telegram_id = user.telegram_id if user else None
        amount = txn.amount

        session.delete(txn)
        session.commit()

        await query.answer("🗑 Payment order deleted!", show_alert=True)
        log_admin_action(update.effective_user.id, "payment.delete",
                         target_type="transaction", target_id=txn_id,
                         details=f"user_id={txn.user_id if user else 'unknown'} amount={amount}")

        # Notify user
        if user_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"❌ Payment Order Removed\n\n💰 Amount: {format_price(amount)}\n\nYour pending payment order was removed by an admin. You're free to start a new one anytime. Please contact support if you believe this is an error."
                )
            except Exception:
                import logging as _lg
                _lg.getLogger(__name__).warning("Notify user failed", exc_info=True)

        # Go back to payment cancellation menu
        await admin_cancel_order_menu(update, context)


async def admin_cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel an order and refund the user."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_orders"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    order_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        # Atomically flip only orders still in PROCESSING/COMPLETED so a
        # double-click can't refund twice.
        flipped = session.query(Order).filter(
            Order.id == order_id,
            Order.status.in_([OrderStatus.PROCESSING, OrderStatus.COMPLETED]),
        ).update({Order.status: OrderStatus.CANCELLED}, synchronize_session=False)
        if flipped == 0:
            await query.answer("⚠️ Order is not refundable.", show_alert=True)
            return

        order = session.query(Order).filter_by(id=order_id).first()
        if not order:
            try:
                await query.edit_message_text("❌ Order not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Atomic wallet refund
        session.query(User).filter(User.id == order.user_id).update(
            {User.wallet_balance: User.wallet_balance + order.total_amount},
            synchronize_session=False,
        )
        session.commit()

        user = session.query(User).filter_by(id=order.user_id).first()

        await query.answer(f"✅ Order cancelled and {format_price(order.total_amount)} refunded!", show_alert=True)
        log_admin_action(update.effective_user.id, "order.cancel_refund",
                         target_type="order", target_id=order_id,
                         details=f"user_id={order.user_id} refund={order.total_amount}")

        if user:
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Order #{order.id} has been cancelled by admin.\n💰 Refund: {format_price(order.total_amount)}"
                )
            except Exception:
                import logging as _lg
                _lg.getLogger(__name__).warning("Notify user failed", exc_info=True)

        # Refresh order details
        await admin_order_detail_callback(update, context)




async def cancel_restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy cancel_restock — delegates to cancel_manage_inventory."""
    return await cancel_manage_inventory(update, context)
