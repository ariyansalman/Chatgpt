"""Feature Box — Admin Handlers — Premium Product System, Phase 1, Feature 2.

Per-product Feature Box: unlimited emoji + title + description rows, each
independently visible/hidden and reorderable. Rendered on the product
detail card via services.feature_box_service.render_feature_box_html.

Callback-data namespace:
    fbx:list:{product_id}          — list feature rows for a product
    fbx:add:{product_id}           — start add-row conversation
    fbx:item:{item_id}             — row detail / action menu
    fbx:tog:{item_id}              — toggle row visibility
    fbx:up:{item_id}               — move row up
    fbx:dn:{item_id}               — move row down
    fbx:del:{item_id}              — delete row (confirm)
    fbx:delok:{item_id}            — delete confirmed
    fbx:edit:{field}:{item_id}     — start editing emoji/title/description
"""

from __future__ import annotations

import logging

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
from utils.safe_conversation import end_conversation as _fbx_conv_end
from services.feature_box_service import (
    list_items,
    add_item,
    update_item,
    toggle_visibility,
    move_item,
    delete_item,
)

logger = logging.getLogger(__name__)

FBX_NEW_EMOJI = 780
FBX_NEW_TITLE = 781
FBX_NEW_DESC  = 782
FBX_EDIT_VALUE = 783


async def _edit_or_reply(update: Update, text: str, markup=None, parse_mode="HTML"):
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
# List
# ─────────────────────────────────────────────────────────────────────────────

async def fbx_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: fbx:list:{product_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        product_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import Product
    with get_db_session() as session:
        p = session.query(Product).filter_by(id=product_id).first()
        pname = p.name if p else "Unknown Product"

    items = list_items(product_id)
    text = (
        f"⭐ <b>Feature Box: {pname}</b>\n\n"
        f"Rows: {len(items)}\n\n"
        "Tap a row to edit it, or add a new one."
    )
    rows = []
    for it in items:
        vis = "👁" if it["is_visible"] else "🙈"
        rows.append([InlineKeyboardButton(
            f"{vis} {it['emoji'] or ''} {it['title']}".strip(),
            callback_data=f"fbx:item:{it['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Feature", callback_data=f"fbx:add:{product_id}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")])
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Item detail / actions
# ─────────────────────────────────────────────────────────────────────────────

async def fbx_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: fbx:item:{item_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    try:
        item_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import ProductFeatureItem
    with get_db_session() as session:
        item = session.query(ProductFeatureItem).filter_by(id=item_id).first()
        if not item:
            await query.answer("Feature not found.", show_alert=True)
            return
        product_id = item.product_id
        text = (
            f"⭐ <b>Feature Row</b>\n\n"
            f"Emoji: {item.emoji or '—'}\n"
            f"Title: {item.title or '—'}\n"
            f"Description: {item.description or '—'}\n"
            f"Visible: {'✅' if item.is_visible else '❌'}"
        )

    rows = [
        [
            InlineKeyboardButton("😀 Emoji", callback_data=f"fbx:edit:emoji:{item_id}"),
            InlineKeyboardButton("✏️ Title", callback_data=f"fbx:edit:title:{item_id}"),
        ],
        [InlineKeyboardButton("📝 Description", callback_data=f"fbx:edit:description:{item_id}")],
        [
            InlineKeyboardButton("⬆️ Up", callback_data=f"fbx:up:{item_id}"),
            InlineKeyboardButton("⬇️ Down", callback_data=f"fbx:dn:{item_id}"),
            InlineKeyboardButton("👁/🙈 Toggle", callback_data=f"fbx:tog:{item_id}"),
        ],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"fbx:del:{item_id}")],
        [InlineKeyboardButton("🔙 Back to Feature Box", callback_data=f"fbx:list:{product_id}")],
    ]
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def fbx_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    try:
        item_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    toggle_visibility(item_id)
    await query.answer("Visibility updated.")
    await fbx_item(update, context)


async def fbx_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    parts = query.data.split(":")
    direction = -1 if parts[1] == "up" else 1
    try:
        item_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    moved = move_item(item_id, direction)
    await query.answer("Moved." if moved else "Already at the edge.")
    await fbx_item(update, context)


async def fbx_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        item_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"fbx:delok:{item_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"fbx:item:{item_id}")],
    ])
    await _edit_or_reply(update, "🗑 Delete this feature row?", kb)


async def fbx_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        item_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import ProductFeatureItem, Product
    with get_db_session() as session:
        item = session.query(ProductFeatureItem).filter_by(id=item_id).first()
        product_id = item.product_id if item else 0

    delete_item(item_id)

    with get_db_session() as session:
        p = session.query(Product).filter_by(id=product_id).first()
        pname = p.name if p else "Unknown Product"
    items = list_items(product_id)
    text = (
        f"⭐ <b>Feature Box: {pname}</b>\n\n"
        f"Rows: {len(items)}\n\n"
        "🗑 Row deleted.\n\nTap a row to edit it, or add a new one."
    )
    rows = []
    for it in items:
        vis = "👁" if it["is_visible"] else "🙈"
        rows.append([InlineKeyboardButton(
            f"{vis} {it['emoji'] or ''} {it['title']}".strip(),
            callback_data=f"fbx:item:{it['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Feature", callback_data=f"fbx:add:{product_id}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")])
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Add Feature (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def fbx_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: fbx:add:{product_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        product_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["fbx_new_product_id"] = product_id
    context.user_data["fbx_new"] = {}

    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"fbx:list:{product_id}")
    ]])
    await _edit_or_reply(update,
        "➕ <b>Add Feature Row</b>\n\n"
        "Step 1/3 — Send an <b>emoji</b> (or send <code>-</code> for none):",
        cancel_kb)
    return FBX_NEW_EMOJI


async def fbx_add_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    value = update.message.text.strip()
    context.user_data["fbx_new"]["emoji"] = "" if value == "-" else value[:32]
    pid = context.user_data.get("fbx_new_product_id", 0)
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"fbx:list:{pid}")
    ]])
    await update.message.reply_text(
        "Step 2/3 — Enter the <b>title</b> (e.g. 'Instant Delivery'):",
        reply_markup=cancel_kb, parse_mode="HTML")
    return FBX_NEW_TITLE


async def fbx_add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["fbx_new"]["title"] = update.message.text.strip()[:200]
    pid = context.user_data.get("fbx_new_product_id", 0)
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"fbx:list:{pid}")
    ]])
    await update.message.reply_text(
        "Step 3/3 — Enter a short <b>description</b> (or send <code>-</code> to skip):",
        reply_markup=cancel_kb, parse_mode="HTML")
    return FBX_NEW_DESC


async def fbx_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    value = update.message.text.strip()
    desc = None if value == "-" else value[:500]
    pid = context.user_data.get("fbx_new_product_id", 0)
    data = context.user_data.get("fbx_new", {})

    ok, msg = add_item(pid, emoji=data.get("emoji", ""), title=data.get("title", ""),
                       description=desc)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Feature Box", callback_data=f"fbx:list:{pid}")
    ]])
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
    context.user_data.pop("fbx_new", None)
    context.user_data.pop("fbx_new_product_id", None)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Edit Field (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def fbx_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: fbx:edit:{field}:{item_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    parts = query.data.split(":")
    try:
        field = parts[2]
        item_id = int(parts[3])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["fbx_edit_item_id"] = item_id
    context.user_data["fbx_edit_field"] = field

    prompts = {
        "emoji": "😀 Enter new <b>emoji</b> (or send <code>-</code> to remove):",
        "title": "✏️ Enter new <b>title</b>:",
        "description": "📝 Enter new <b>description</b> (or send <code>-</code> to clear):",
    }
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"fbx:item:{item_id}")
    ]])
    await _edit_or_reply(update, prompts.get(field, "Enter new value:"), cancel_kb)
    return FBX_EDIT_VALUE


async def fbx_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    item_id = context.user_data.get("fbx_edit_item_id")
    field = context.user_data.get("fbx_edit_field")
    if not item_id or not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    if field in ("emoji", "description") and value == "-":
        value = "" if field == "emoji" else None

    update_item(item_id, **{field: value})

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Feature", callback_data=f"fbx:item:{item_id}")
    ]])
    await update.message.reply_text(f"✅ <b>{field.title()}</b> updated.",
                                    reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def build_fbx_conversation() -> ConversationHandler:
    return ConversationHandler(
        conversation_timeout=300,
        entry_points=[
            CallbackQueryHandler(fbx_add_start, pattern=r"^fbx:add:\d+$"),
            CallbackQueryHandler(fbx_edit_start, pattern=r"^fbx:edit:(emoji|title|description):\d+$"),
        ],
        states={
            FBX_NEW_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, fbx_add_emoji)],
            FBX_NEW_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fbx_add_title)],
            FBX_NEW_DESC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, fbx_add_desc)],
            FBX_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fbx_edit_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(_fbx_conv_end, pattern=r"^fbx:list:\d+$"),
            MessageHandler(filters.COMMAND, _fbx_conv_end),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    application.add_handler(build_fbx_conversation(), group=0)
    application.add_handler(CallbackQueryHandler(fbx_list, pattern=r"^fbx:list:\d+$"))
    application.add_handler(CallbackQueryHandler(fbx_item, pattern=r"^fbx:item:\d+$"))
    application.add_handler(CallbackQueryHandler(fbx_toggle, pattern=r"^fbx:tog:\d+$"))
    application.add_handler(CallbackQueryHandler(fbx_move, pattern=r"^fbx:(up|dn):\d+$"))
    application.add_handler(CallbackQueryHandler(fbx_delete_confirm, pattern=r"^fbx:del:\d+$"))
    application.add_handler(CallbackQueryHandler(fbx_delete_ok, pattern=r"^fbx:delok:\d+$"))
