"""Product Information Builder — V48 Handler

Admin panel for managing per-product information blocks, reusable templates,
purchase-flow settings, and global visibility toggles.

User-facing: renders the Product Information page that appears between the
product card and the quantity selector when the admin has it enabled.

Callback-data namespace:
    pib:view:{product_id}            — user views info page
    pib:proceed:{product_id}         — user proceeds to purchase from info page
    pib:admin:products:{page}        — admin: product list (paginated)
    pib:admin:prod:{product_id}      — admin: blocks list for one product
    pib:admin:add:{product_id}       — admin: start add-block conversation
    pib:admin:blk:{block_id}         — admin: block detail / action menu
    pib:admin:blk:up:{block_id}      — admin: move block up
    pib:admin:blk:dn:{block_id}      — admin: move block down
    pib:admin:blk:tog:{block_id}     — admin: toggle block visibility
    pib:admin:blk:dup:{block_id}     — admin: duplicate block
    pib:admin:blk:del:{block_id}     — admin: delete block (confirm)
    pib:admin:blk:delok:{block_id}   — admin: delete confirmed
    pib:admin:blk:type:{block_id}:{type} — admin: set block type
    pib:admin:blk:color:{block_id}:{col} — admin: set accent color
    pib:admin:blk:edit:{field}:{block_id} — admin: start editing title/emoji/content
    pib:admin:prv:{product_id}       — admin: preview info page
    pib:admin:settings:{product_id}  — admin: per-product purchase settings
    pib:admin:set:{product_id}:{key} — admin: toggle a purchase setting
    pib:admin:tpl:{page}             — admin: template list
    pib:admin:tpl:view:{template_id} — admin: template detail
    pib:admin:tpl:new                — admin: start create-template conversation
    pib:admin:tpl:del:{template_id}  — admin: delete template (confirm)
    pib:admin:tpl:delok:{template_id}— admin: delete confirmed
    pib:admin:tpl:apply:{template_id}:{product_id} — admin: apply template
    pib:admin:tpl:save:{product_id}  — admin: save product blocks as template
    pib:admin:global                 — admin: global visibility settings
    pib:admin:gs:{key}               — admin: toggle a global visibility key
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils.helpers import is_admin
from services.product_info_service import (
    BLOCK_TYPES,
    BLOCK_TYPE_KEYS,
    ACCENT_COLORS,
    VISIBILITY_KEYS,
    get_visibility,
    set_visibility,
    get_purchase_settings,
    save_purchase_settings,
    render_block_html,
    render_product_info_page,
    has_info_blocks,
    count_all_blocks,
    apply_template_to_product,
    save_product_blocks_as_template,
)

logger = logging.getLogger(__name__)

# ─── Conversation States ──────────────────────────────────────────────────────
PIB_BLOCK_TITLE    = 750
PIB_BLOCK_CONTENT  = 751
PIB_BLOCK_EMOJI    = 752
PIB_TPL_NAME       = 753
PIB_EDIT_VALUE     = 754
PIB_TPL_SAVE_NAME  = 755

_PAGE_SIZE = 8


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_answer(query):
    try:
        import asyncio
        asyncio.get_event_loop().run_until_complete(query.answer())
    except Exception:
        pass


async def _edit_or_reply(update: Update, text: str, markup=None, parse_mode="HTML"):
    """Edit the existing message, or send a new one if that fails."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
    if update.effective_chat:
        await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=parse_mode)


# ─────────────────────────────────────────────────────────────────────────────
# User: Product Information Page
# ─────────────────────────────────────────────────────────────────────────────

async def user_show_info_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:view:{product_id} — show info page from product detail."""
    query = update.callback_query
    await query.answer()

    try:
        product_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    html_text, block_count = render_product_info_page(product_id)

    if not html_text:
        await query.answer("ℹ️ No information available for this product.", show_alert=True)
        return

    settings = get_purchase_settings(product_id)
    keyboard = _build_user_info_keyboard(product_id, settings, from_detail=True)
    header = "📋 <b>Product Information</b>\n\n"
    full_text = header + html_text

    # Telegram message limit
    if len(full_text) > 4000:
        full_text = full_text[:3980] + "\n\n<i>…(see full details in store)</i>"

    try:
        await query.edit_message_text(full_text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("PIB user_show_info_page: %s", e)


async def user_proceed_to_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:proceed:{product_id} — routed from info page Continue button.

    Re-enters the purchase flow: loads the product and shows the quantity keyboard.
    This handler is registered as an entry point of the purchase ConversationHandler.
    """
    query = update.callback_query
    await query.answer()

    try:
        product_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.answer("❌ Invalid request.", show_alert=True)
        from telegram.ext import ConversationHandler
        return ConversationHandler.END

    # Delegate entirely to buy_product_start by faking the callback_data
    query._unfreeze()  # type: ignore[attr-defined]
    try:
        query.data = f"buy_{product_id}"
    except Exception:
        pass

    from handlers.payment_handlers import buy_product_start
    return await buy_product_start(update, context)


def _build_user_info_keyboard(product_id: int, settings: dict,
                               from_detail: bool = False) -> InlineKeyboardMarkup:
    rows = []

    if settings.get("show_continue_button", True):
        rows.append([InlineKeyboardButton(
            "🛒 Continue to Purchase",
            callback_data=f"buy_{product_id}"
        )])

    back_cb = f"product_{product_id}"
    rows.append([
        InlineKeyboardButton("🔙 Back to Product", callback_data=back_cb),
        InlineKeyboardButton("☎️ Support", callback_data="support_center"),
    ])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Product List (pick a product to manage)
# ─────────────────────────────────────────────────────────────────────────────

async def admin_product_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:products:{page} — paginated product list."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    try:
        page = int(parts[3])
    except (IndexError, ValueError):
        page = 0

    from database import get_db_session
    from database.models import Product

    def _load(_page):
        with get_db_session() as session:
            products = (session.query(Product)
                        .filter_by(is_deleted=False)
                        .order_by(Product.name)
                        .all())
            rows = [{"id": p.id, "name": p.name, "blocks": count_all_blocks(p.id)} for p in products]
        return rows

    import asyncio
    products = await asyncio.to_thread(_load, page)

    start = page * _PAGE_SIZE
    end   = start + _PAGE_SIZE
    page_items = products[start:end]
    total_pages = max(1, (len(products) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    text = "📖 <b>Product Information Builder</b>\n\nSelect a product to manage its information blocks:\n"
    rows = []
    for p in page_items:
        badge = f" [{p['blocks']}🧱]" if p['blocks'] else " [Empty]"
        rows.append([InlineKeyboardButton(
            f"📦 {p['name']}{badge}",
            callback_data=f"pib:admin:prod:{p['id']}"
        )])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pib:admin:products:{page-1}"))
    if end < len(products):
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"pib:admin:products:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("📚 Templates", callback_data="pib:admin:tpl:0"),
        InlineKeyboardButton("🌐 Global Settings", callback_data="pib:admin:global"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])

    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Blocks List for a Product
# ─────────────────────────────────────────────────────────────────────────────

async def admin_product_blocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:prod:{product_id} — show all blocks for product."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import Product, ProductInfoBlock

    def _load():
        with get_db_session() as session:
            p = session.query(Product).filter_by(id=product_id).first()
            if not p:
                return None, []
            blocks = (session.query(ProductInfoBlock)
                      .filter_by(product_id=product_id)
                      .order_by(ProductInfoBlock.display_order, ProductInfoBlock.id)
                      .all())
            return (
                {"name": p.name, "id": p.id},
                [{"id": b.id, "title": b.title, "emoji": b.emoji,
                  "block_type": b.block_type, "is_visible": b.is_visible,
                  "display_order": b.display_order} for b in blocks]
            )

    import asyncio
    product, blocks = await asyncio.to_thread(_load)
    if not product:
        await query.answer("Product not found.", show_alert=True)
        return

    settings = get_purchase_settings(product_id)
    info_enabled = settings.get("show_info_before_purchase", True)

    text = (
        f"📖 <b>Product Information: {product['name']}</b>\n\n"
        f"ℹ️ Info page: {'✅ Enabled' if info_enabled else '❌ Disabled'}\n"
        f"🧱 Blocks: {len(blocks)}\n\n"
        "Tap a block to edit it, or use the buttons below."
    )
    rows = []

    for b in blocks:
        vis  = "👁" if b["is_visible"] else "🙈"
        emoji = b["emoji"] or ""
        title = b["title"] or "(untitled)"
        rows.append([InlineKeyboardButton(
            f"{vis} {emoji} {title}",
            callback_data=f"pib:admin:blk:{b['id']}"
        )])

    rows.append([
        InlineKeyboardButton("➕ Add Block", callback_data=f"pib:admin:add:{product_id}"),
        InlineKeyboardButton("👁 Preview",  callback_data=f"pib:admin:prv:{product_id}"),
    ])
    rows.append([
        InlineKeyboardButton("📚 Apply Template", callback_data=f"pib:admin:tpl:0:{product_id}"),
        InlineKeyboardButton("💾 Save as Template", callback_data=f"pib:admin:tpl:save:{product_id}"),
    ])
    rows.append([
        InlineKeyboardButton("⚙️ Purchase Settings", callback_data=f"pib:admin:settings:{product_id}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="pib:admin:products:0")])

    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Block Detail / Action Menu
# ─────────────────────────────────────────────────────────────────────────────

async def admin_block_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:blk:{block_id} — block action menu."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        block_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import ProductInfoBlock

    def _load():
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if not b:
                return None
            return {"id": b.id, "product_id": b.product_id, "title": b.title,
                    "emoji": b.emoji, "content": b.content, "block_type": b.block_type,
                    "accent_color": b.accent_color, "is_visible": b.is_visible,
                    "display_order": b.display_order}

    import asyncio
    blk = await asyncio.to_thread(_load)
    if not blk:
        await query.answer("Block not found.", show_alert=True)
        return

    vis_label  = "🙈 Hide"  if blk["is_visible"] else "👁 Show"
    vis_icon   = "👁"       if blk["is_visible"] else "🙈"
    type_label = BLOCK_TYPES.get(blk["block_type"] or "text", "📄 Plain Text")
    pid        = blk["product_id"]

    text = (
        f"🧱 <b>Block: {blk['emoji'] or ''} {blk['title'] or '(untitled)'}</b>\n\n"
        f"📌 Type: {type_label}\n"
        f"🎨 Color: {ACCENT_COLORS.get(blk['accent_color'] or 'none', '⬜ None')}\n"
        f"👁 Visible: {'Yes' if blk['is_visible'] else 'No'}\n"
        f"📐 Order: {blk['display_order']}\n\n"
        f"<i>Preview:</i>\n{render_block_html(type('B', (), blk)())}"
    )

    # Crop text to Telegram limit
    if len(text) > 3800:
        text = text[:3780] + "\n…"

    rows = [
        [
            InlineKeyboardButton("✏️ Title",   callback_data=f"pib:admin:blk:edit:title:{block_id}"),
            InlineKeyboardButton("😀 Emoji",   callback_data=f"pib:admin:blk:edit:emoji:{block_id}"),
            InlineKeyboardButton("📝 Content", callback_data=f"pib:admin:blk:edit:content:{block_id}"),
        ],
        [
            InlineKeyboardButton("📄 Type",    callback_data=f"pib:admin:blk:typemenu:{block_id}"),
            InlineKeyboardButton("🎨 Color",   callback_data=f"pib:admin:blk:colmenu:{block_id}"),
        ],
        [
            InlineKeyboardButton("⬆️ Up",  callback_data=f"pib:admin:blk:up:{block_id}"),
            InlineKeyboardButton("⬇️ Down", callback_data=f"pib:admin:blk:dn:{block_id}"),
            InlineKeyboardButton(f"{vis_icon} {vis_label}", callback_data=f"pib:admin:blk:tog:{block_id}"),
        ],
        [
            InlineKeyboardButton("📋 Duplicate", callback_data=f"pib:admin:blk:dup:{block_id}"),
            InlineKeyboardButton("🗑 Delete",    callback_data=f"pib:admin:blk:del:{block_id}"),
        ],
        [InlineKeyboardButton("🔙 Back to Blocks", callback_data=f"pib:admin:prod:{pid}")],
    ]
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Block Actions (move / toggle / duplicate / delete)
# ─────────────────────────────────────────────────────────────────────────────

async def admin_block_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:blk:{action}:{block_id} — block actions."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    parts  = query.data.split(":")
    action = parts[3]

    # ── up / dn  ──────────────────────────────────────────────────────────
    if action in ("up", "dn"):
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        _block_reorder(block_id, up=(action == "up"))
        # Refresh block detail
        query.data = f"pib:admin:blk:{block_id}"
        await admin_block_detail(update, context)
        return

    # ── tog (toggle visibility)  ──────────────────────────────────────────
    if action == "tog":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        _block_toggle_visibility(block_id)
        query.data = f"pib:admin:blk:{block_id}"
        await admin_block_detail(update, context)
        return

    # ── dup (duplicate) ───────────────────────────────────────────────────
    if action == "dup":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        pid = _block_duplicate(block_id)
        if pid:
            query.data = f"pib:admin:prod:{pid}"
            await admin_product_blocks(update, context)
        return

    # ── del (confirm) ─────────────────────────────────────────────────────
    if action == "del":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        rows = [
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"pib:admin:blk:delok:{block_id}")],
            [InlineKeyboardButton("❌ Cancel",       callback_data=f"pib:admin:blk:{block_id}")],
        ]
        await _edit_or_reply(update, "🗑 <b>Delete this block?</b>\nThis cannot be undone.",
                             InlineKeyboardMarkup(rows))
        return

    # ── delok (execute delete) ────────────────────────────────────────────
    if action == "delok":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        pid = _block_delete(block_id)
        if pid:
            query.data = f"pib:admin:prod:{pid}"
            await admin_product_blocks(update, context)
        return

    # ── typemenu ──────────────────────────────────────────────────────────
    if action == "typemenu":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        rows = []
        for key, label in BLOCK_TYPES.items():
            rows.append([InlineKeyboardButton(
                label, callback_data=f"pib:admin:blk:type:{block_id}:{key}"
            )])
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:blk:{block_id}")])
        await _edit_or_reply(update, "📄 <b>Select Block Type:</b>", InlineKeyboardMarkup(rows))
        return

    # ── type:{block_id}:{type_key} ────────────────────────────────────────
    if action == "type":
        try:
            block_id = int(parts[4])
            type_key = parts[5]
        except (IndexError, ValueError):
            return
        _block_set_field(block_id, "block_type", type_key)
        query.data = f"pib:admin:blk:{block_id}"
        await admin_block_detail(update, context)
        return

    # ── colmenu ───────────────────────────────────────────────────────────
    if action == "colmenu":
        try:
            block_id = int(parts[4])
        except (IndexError, ValueError):
            return
        rows = []
        for key, label in ACCENT_COLORS.items():
            rows.append([InlineKeyboardButton(
                label, callback_data=f"pib:admin:blk:color:{block_id}:{key}"
            )])
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:blk:{block_id}")])
        await _edit_or_reply(update, "🎨 <b>Select Accent Color:</b>", InlineKeyboardMarkup(rows))
        return

    # ── color:{block_id}:{color_key} ──────────────────────────────────────
    if action == "color":
        try:
            block_id = int(parts[4])
            color_key = parts[5]
        except (IndexError, ValueError):
            return
        _block_set_field(block_id, "accent_color", color_key)
        query.data = f"pib:admin:blk:{block_id}"
        await admin_block_detail(update, context)
        return


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Edit Block Field (via Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def admin_block_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:blk:edit:{field}:{block_id} — start editing a field."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    parts = query.data.split(":")
    try:
        field    = parts[4]   # title | emoji | content
        block_id = int(parts[5])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["pib_edit_block_id"] = block_id
    context.user_data["pib_edit_field"]    = field

    prompts = {
        "title":   "✏️ Enter new <b>block title</b>:",
        "emoji":   "😀 Enter new <b>emoji</b> for this block (or send <code>-</code> to remove):",
        "content": "📝 Enter new <b>block content</b>:\n\n"
                   "<i>Tip: For bullet lists put each item on its own line.</i>",
    }
    prompt = prompts.get(field, f"Enter new value for <b>{field}</b>:")
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"pib:admin:blk:{block_id}")
    ]])
    await _edit_or_reply(update, prompt, cancel_kb)
    return PIB_EDIT_VALUE


async def admin_block_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message handler: receive edited field value."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    block_id = context.user_data.get("pib_edit_block_id")
    field    = context.user_data.get("pib_edit_field")
    if not block_id or not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    if field == "emoji" and value == "-":
        value = ""

    _block_set_field(block_id, field, value)

    # Confirm and show block detail
    from database import get_db_session
    from database.models import ProductInfoBlock
    with get_db_session() as session:
        b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
        pid = b.product_id if b else 0

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Block", callback_data=f"pib:admin:blk:{block_id}")
    ]])
    await update.message.reply_text(f"✅ <b>{field.title()}</b> updated.", reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Add Block (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def admin_add_block_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:add:{product_id} — start add-block conversation."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["pib_new_product_id"] = product_id
    context.user_data["pib_new_block"]      = {}

    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await _edit_or_reply(update,
        "➕ <b>Add Information Block</b>\n\n"
        "Step 1/2 — Enter a <b>title</b> for this block:\n"
        "<i>(e.g. 'Features', 'How To Use', 'Requirements')</i>",
        cancel_kb)
    return PIB_BLOCK_TITLE


async def admin_add_block_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: receive block title."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    title = update.message.text.strip()[:200]
    context.user_data["pib_new_block"]["title"] = title

    pid = context.user_data.get("pib_new_product_id", 0)
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"pib:admin:prod:{pid}")
    ]])
    await update.message.reply_text(
        f"✅ Title set: <b>{title}</b>\n\n"
        "Step 2/2 — Enter the <b>content</b> for this block:\n"
        "<i>(You can always edit the type/emoji/color after adding.)</i>",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    return PIB_BLOCK_CONTENT


async def admin_add_block_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: receive block content, save block."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    content    = update.message.text.strip()
    new_block  = context.user_data.get("pib_new_block", {})
    product_id = context.user_data.get("pib_new_product_id", 0)
    new_block["content"]  = content
    new_block["product_id"] = product_id

    _create_block(new_block)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Blocks", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await update.message.reply_text(
        f"✅ Block <b>{new_block.get('title', 'Untitled')}</b> added!",
        reply_markup=kb, parse_mode="HTML"
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Preview
# ─────────────────────────────────────────────────────────────────────────────

async def admin_preview_info_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:prv:{product_id} — preview info page for admin."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        return

    html_text, count = render_product_info_page(product_id)

    if not html_text:
        await query.answer("ℹ️ No visible blocks to preview.", show_alert=True)
        return

    header = "👁 <b>Preview — Product Information Page</b>\n\n"
    full   = header + html_text
    if len(full) > 4000:
        full = full[:3980] + "\n\n<i>…(truncated)</i>"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await _edit_or_reply(update, full, kb)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Purchase Settings
# ─────────────────────────────────────────────────────────────────────────────

async def admin_purchase_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:settings:{product_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        product_id = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        return

    settings = get_purchase_settings(product_id)

    _SETTING_LABELS = {
        "show_info_before_purchase": "📋 Show Info Page Before Purchase",
        "skip_if_no_blocks":         "⏭ Skip If No Blocks",
        "require_scroll":            "📜 Require Reading (Scroll)",
        "show_confirm_checkbox":     "☑️ Show Confirmation Checkbox",
        "show_continue_button":      "🛒 Show Continue Button",
    }

    text = f"⚙️ <b>Purchase Settings for Product #{product_id}</b>\n\n"
    rows = []
    for key, label in _SETTING_LABELS.items():
        val  = settings.get(key, False)
        icon = "✅" if val else "❌"
        rows.append([InlineKeyboardButton(
            f"{icon} {label}",
            callback_data=f"pib:admin:set:{product_id}:{key}"
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")])

    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def admin_toggle_purchase_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:set:{product_id}:{key} — toggle a purchase setting."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    try:
        product_id = int(parts[3])
        key        = parts[4]
    except (IndexError, ValueError):
        return

    settings = get_purchase_settings(product_id)
    settings[key] = not settings.get(key, False)
    save_purchase_settings(product_id, settings)

    query.data = f"pib:admin:settings:{product_id}"
    await admin_purchase_settings(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Global Visibility Settings
# ─────────────────────────────────────────────────────────────────────────────

async def admin_global_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:global — global product card visibility toggles."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    text = "🌐 <b>Global Product Display Settings</b>\n\nToggle what users see on the product detail card:\n"
    rows = []
    for key, (label, _default) in VISIBILITY_KEYS.items():
        val  = get_visibility(key)
        icon = "✅" if val else "❌"
        rows.append([InlineKeyboardButton(
            f"{icon} {label}",
            callback_data=f"pib:admin:gs:{key}"
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="pib:admin:products:0")])
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def admin_toggle_global_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:gs:{key} — toggle a global visibility setting."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        key = query.data.split(":")[3]
    except IndexError:
        return

    if key in VISIBILITY_KEYS:
        set_visibility(key, not get_visibility(key))

    await admin_global_settings(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Template Management
# ─────────────────────────────────────────────────────────────────────────────

async def admin_template_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:{page} or pib:admin:tpl:{page}:{product_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    try:
        page = int(parts[3])
    except (IndexError, ValueError):
        page = 0
    # Optional product_id context (for applying template)
    apply_to = None
    try:
        apply_to = int(parts[4]) if len(parts) > 4 else None
    except (ValueError, TypeError):
        apply_to = None

    context.user_data["pib_tpl_apply_to"] = apply_to

    from database import get_db_session
    from database.models import ProductInfoTemplate, ProductInfoTemplateBlock

    def _load():
        with get_db_session() as session:
            tpls = (session.query(ProductInfoTemplate)
                    .order_by(ProductInfoTemplate.name)
                    .all())
            result = []
            for t in tpls:
                cnt = (session.query(ProductInfoTemplateBlock)
                       .filter_by(template_id=t.id).count())
                result.append({"id": t.id, "name": t.name, "emoji": t.emoji,
                                "blocks": cnt})
            return result

    import asyncio
    templates = await asyncio.to_thread(_load)
    start = page * _PAGE_SIZE
    end   = start + _PAGE_SIZE
    page_items = templates[start:end]

    mode_text = f"\n🎯 Select template to apply to product #{apply_to}:" if apply_to else ""
    text = f"📚 <b>Product Info Templates</b>{mode_text}\n\n{len(templates)} template(s) available.\n"

    rows = []
    for t in page_items:
        emoji = t["emoji"] or "📋"
        label = f"{emoji} {t['name']} [{t['blocks']} blocks]"
        if apply_to:
            cb = f"pib:admin:tpl:apply:{t['id']}:{apply_to}"
        else:
            cb = f"pib:admin:tpl:view:{t['id']}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pib:admin:tpl:{page-1}"))
    if end < len(templates):
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"pib:admin:tpl:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("➕ Create Template", callback_data="pib:admin:tpl:new")])

    if apply_to:
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{apply_to}")])
    else:
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="pib:admin:products:0")])

    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def admin_template_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:view:{template_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        template_id = int(query.data.split(":")[4])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import ProductInfoTemplate, ProductInfoTemplateBlock

    def _load():
        with get_db_session() as session:
            t = session.query(ProductInfoTemplate).filter_by(id=template_id).first()
            if not t:
                return None, []
            blocks = (session.query(ProductInfoTemplateBlock)
                      .filter_by(template_id=template_id)
                      .order_by(ProductInfoTemplateBlock.display_order)
                      .all())
            return (
                {"id": t.id, "name": t.name, "emoji": t.emoji},
                [{"title": b.title, "emoji": b.emoji, "block_type": b.block_type} for b in blocks]
            )

    import asyncio
    tpl, blocks = await asyncio.to_thread(_load)
    if not tpl:
        await query.answer("Template not found.", show_alert=True)
        return

    emoji = tpl["emoji"] or "📋"
    text  = f"📚 <b>Template: {emoji} {tpl['name']}</b>\n\n"
    text += f"🧱 {len(blocks)} block(s):\n"
    for b in blocks:
        e = b["emoji"] or ""
        t_ = b["title"] or "(untitled)"
        type_ = BLOCK_TYPES.get(b["block_type"] or "text", "📄")
        text += f"  • {e} {t_} <i>({type_})</i>\n"

    rows = [
        [InlineKeyboardButton("🗑 Delete Template", callback_data=f"pib:admin:tpl:del:{template_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="pib:admin:tpl:0")],
    ]
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def admin_template_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:apply:{template_id}:{product_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    parts = query.data.split(":")
    try:
        template_id = int(parts[4])
        product_id  = int(parts[5])
    except (IndexError, ValueError):
        return

    ok, msg = apply_template_to_product(template_id, product_id, append=False)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Blocks", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await _edit_or_reply(update, msg, kb)


async def admin_template_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:new — start template creation."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data["pib_new_tpl"] = {}
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="pib:admin:tpl:0")
    ]])
    await _edit_or_reply(update,
        "📚 <b>Create Template</b>\n\nEnter a <b>name</b> for this template:",
        cancel_kb)
    return PIB_TPL_NAME


async def admin_template_create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive template name and create empty template."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    name = update.message.text.strip()[:200]
    from database import get_db_session
    from database.models import ProductInfoTemplate

    with get_db_session() as session:
        tpl = ProductInfoTemplate(
            name=name,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(tpl)
        session.commit()
        tpl_id = tpl.id

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Templates", callback_data="pib:admin:tpl:0")
    ]])
    await update.message.reply_text(
        f"✅ Template <b>{name}</b> created (ID {tpl_id}).",
        reply_markup=kb, parse_mode="HTML"
    )
    return ConversationHandler.END


async def admin_template_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:del:{template_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        template_id = int(query.data.split(":")[4])
    except (IndexError, ValueError):
        return

    rows = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"pib:admin:tpl:delok:{template_id}")],
        [InlineKeyboardButton("❌ Cancel",       callback_data=f"pib:admin:tpl:view:{template_id}")],
    ]
    await _edit_or_reply(update, "🗑 <b>Delete this template?</b>", InlineKeyboardMarkup(rows))


async def admin_template_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:delok:{template_id}."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        template_id = int(query.data.split(":")[4])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import ProductInfoTemplate, ProductInfoTemplateBlock

    with get_db_session() as session:
        (session.query(ProductInfoTemplateBlock)
         .filter_by(template_id=template_id).delete())
        (session.query(ProductInfoTemplate)
         .filter_by(id=template_id).delete())
        session.commit()

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Templates", callback_data="pib:admin:tpl:0")
    ]])
    await _edit_or_reply(update, "✅ Template deleted.", kb)


async def admin_template_save_from_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: pib:admin:tpl:save:{product_id} — save product blocks as template."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    try:
        product_id = int(query.data.split(":")[4])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["pib_tpl_save_product_id"] = product_id
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await _edit_or_reply(update,
        "💾 <b>Save as Template</b>\n\nEnter a <b>name</b> for this template:",
        cancel_kb)
    return PIB_TPL_SAVE_NAME


async def admin_template_save_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive template name and save product blocks."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    name       = update.message.text.strip()[:200]
    product_id = context.user_data.get("pib_tpl_save_product_id", 0)
    ok, msg    = save_product_blocks_as_template(product_id, name)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")
    ]])
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# DB Helpers (sync — called via asyncio.to_thread or inline in sync context)
# ─────────────────────────────────────────────────────────────────────────────

def _block_set_field(block_id: int, field: str, value) -> None:
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if b:
                setattr(b, field, value)
                b.updated_at = datetime.utcnow()
                session.commit()
    except Exception as exc:
        logger.warning("PIB _block_set_field: %s", exc)


def _block_toggle_visibility(block_id: int) -> None:
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if b:
                b.is_visible = not b.is_visible
                b.updated_at = datetime.utcnow()
                session.commit()
    except Exception as exc:
        logger.warning("PIB _block_toggle_visibility: %s", exc)


def _block_reorder(block_id: int, up: bool) -> None:
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if not b:
                return
            pid = b.product_id
            blocks = (session.query(ProductInfoBlock)
                      .filter_by(product_id=pid)
                      .order_by(ProductInfoBlock.display_order, ProductInfoBlock.id)
                      .all())
            idx = next((i for i, x in enumerate(blocks) if x.id == block_id), None)
            if idx is None:
                return
            swap_idx = idx - 1 if up else idx + 1
            if swap_idx < 0 or swap_idx >= len(blocks):
                return
            # Swap display_order values
            a, bv = blocks[idx], blocks[swap_idx]
            a.display_order, bv.display_order = (
                bv.display_order if bv.display_order != a.display_order
                else bv.display_order - 1,
                a.display_order if bv.display_order != a.display_order
                else a.display_order + 1,
            )
            # Simpler: just reassign sequential order
            for i, bl in enumerate(blocks):
                bl.display_order = i * 10
            # Now swap
            blocks[idx].display_order, blocks[swap_idx].display_order = (
                blocks[swap_idx].display_order, blocks[idx].display_order
            )
            session.commit()
    except Exception as exc:
        logger.warning("PIB _block_reorder: %s", exc)


def _block_duplicate(block_id: int) -> Optional[int]:
    """Duplicate a block. Returns product_id or None."""
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if not b:
                return None
            # Find max order
            from sqlalchemy import func
            max_ord = (session.query(func.max(ProductInfoBlock.display_order))
                       .filter_by(product_id=b.product_id).scalar()) or 0
            nb = ProductInfoBlock(
                product_id=b.product_id,
                title=b.title + " (copy)" if b.title else "(copy)",
                emoji=b.emoji,
                content=b.content,
                block_type=b.block_type,
                accent_color=b.accent_color,
                is_bold=b.is_bold,
                is_italic=b.is_italic,
                has_spoiler=b.has_spoiler,
                is_visible=b.is_visible,
                display_order=max_ord + 10,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(nb)
            session.commit()
            return b.product_id
    except Exception as exc:
        logger.warning("PIB _block_duplicate: %s", exc)
        return None


def _block_delete(block_id: int) -> Optional[int]:
    """Delete a block. Returns product_id or None."""
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        with get_db_session() as session:
            b = session.query(ProductInfoBlock).filter_by(id=block_id).first()
            if not b:
                return None
            pid = b.product_id
            session.delete(b)
            session.commit()
            return pid
    except Exception as exc:
        logger.warning("PIB _block_delete: %s", exc)
        return None


def _create_block(data: dict) -> Optional[int]:
    """Create a new block. Returns block_id or None."""
    try:
        from database import get_db_session
        from database.models import ProductInfoBlock
        from sqlalchemy import func
        with get_db_session() as session:
            max_ord = (session.query(func.max(ProductInfoBlock.display_order))
                       .filter_by(product_id=data["product_id"]).scalar()) or 0
            b = ProductInfoBlock(
                product_id=data["product_id"],
                title=data.get("title", ""),
                emoji=data.get("emoji", ""),
                content=data.get("content", ""),
                block_type=data.get("block_type", "text"),
                accent_color=data.get("accent_color", "none"),
                is_bold=data.get("is_bold", False),
                is_italic=data.get("is_italic", False),
                has_spoiler=data.get("has_spoiler", False),
                is_visible=data.get("is_visible", True),
                display_order=max_ord + 10,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(b)
            session.commit()
            return b.id
    except Exception as exc:
        logger.exception("PIB _create_block: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher: routes pib:admin:blk:* based on action
# ─────────────────────────────────────────────────────────────────────────────

async def pib_block_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all pib:admin:blk:* callbacks that aren't edit-start."""
    parts  = (update.callback_query.data or "").split(":")
    action = parts[3] if len(parts) > 3 else ""
    if action in ("up", "dn", "tog", "dup", "del", "delok", "typemenu", "type", "colmenu", "color"):
        await admin_block_action(update, context)
    else:
        await admin_block_detail(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# ConversationHandler Builder
# ─────────────────────────────────────────────────────────────────────────────

def build_pib_conversation() -> ConversationHandler:
    """Build the PIB admin ConversationHandler (add block + template flows)."""
    return ConversationHandler(
        conversation_timeout=300,
        entry_points=[
            CallbackQueryHandler(admin_add_block_start,            pattern=r"^pib:admin:add:\d+$"),
            CallbackQueryHandler(admin_block_edit_start,           pattern=r"^pib:admin:blk:edit:(title|emoji|content):\d+$"),
            CallbackQueryHandler(admin_template_create_start,      pattern=r"^pib:admin:tpl:new$"),
            CallbackQueryHandler(admin_template_save_from_product, pattern=r"^pib:admin:tpl:save:\d+$"),
        ],
        states={
            PIB_BLOCK_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_block_title),
            ],
            PIB_BLOCK_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_block_content),
            ],
            PIB_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_edit_receive),
            ],
            PIB_TPL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_template_create_name),
            ],
            PIB_TPL_SAVE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_template_save_name),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern=r"^pib:admin:prod:\d+$"),
            CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern=r"^pib:admin:tpl:0$"),
            MessageHandler(filters.COMMAND, lambda u, c: ConversationHandler.END),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    """Register all PIB handlers with the application."""
    from telegram.ext import Application

    # Add conversation handler first (higher priority)
    application.add_handler(build_pib_conversation(), group=0)

    # User-facing
    application.add_handler(CallbackQueryHandler(
        user_show_info_page, pattern=r"^pib:view:\d+$"))

    # Admin: product list
    application.add_handler(CallbackQueryHandler(
        admin_product_list, pattern=r"^pib:admin:products:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_product_list, pattern=r"^pib:admin:products$"))

    # Admin: product blocks page
    application.add_handler(CallbackQueryHandler(
        admin_product_blocks, pattern=r"^pib:admin:prod:\d+$"))

    # Admin: block detail + actions
    application.add_handler(CallbackQueryHandler(
        pib_block_dispatcher, pattern=r"^pib:admin:blk:"))

    # Admin: preview
    application.add_handler(CallbackQueryHandler(
        admin_preview_info_page, pattern=r"^pib:admin:prv:\d+$"))

    # Admin: purchase settings
    application.add_handler(CallbackQueryHandler(
        admin_purchase_settings, pattern=r"^pib:admin:settings:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_toggle_purchase_setting, pattern=r"^pib:admin:set:\d+:.+$"))

    # Admin: global settings
    application.add_handler(CallbackQueryHandler(
        admin_global_settings, pattern=r"^pib:admin:global$"))
    application.add_handler(CallbackQueryHandler(
        admin_toggle_global_setting, pattern=r"^pib:admin:gs:.+$"))

    # Admin: templates
    application.add_handler(CallbackQueryHandler(
        admin_template_list, pattern=r"^pib:admin:tpl:\d+(:\d+)?$"))
    application.add_handler(CallbackQueryHandler(
        admin_template_view, pattern=r"^pib:admin:tpl:view:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_template_apply, pattern=r"^pib:admin:tpl:apply:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_template_delete_confirm, pattern=r"^pib:admin:tpl:del:\d+$"))
    application.add_handler(CallbackQueryHandler(
        admin_template_delete_ok, pattern=r"^pib:admin:tpl:delok:\d+$"))
