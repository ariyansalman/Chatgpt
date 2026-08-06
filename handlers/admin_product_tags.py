"""Product Tags — Admin Handlers — Premium Product System, Phase 1, Feature 7.

Admin-managed tag catalog (Featured, Best Seller, New, Popular, Premium,
Discount, Limited, Digital, Instant, ...) plus per-product assignment.
Rendered on the product detail card via
services.product_tags_service.render_tag_line.

Callback-data namespace:
    ptag:catalog                    — list the tag catalog
    ptag:new                        — start create-tag conversation
    ptag:tag:{tag_id}               — tag detail / actions
    ptag:tog:{tag_id}               — toggle tag active/inactive
    ptag:up:{tag_id} / ptag:dn:{tag_id} — reorder catalog
    ptag:del:{tag_id}               — delete tag (confirm)
    ptag:delok:{tag_id}             — delete confirmed
    ptag:edit:{field}:{tag_id}      — start editing label/emoji
    ptag:assign:{product_id}        — per-product tag assignment list
    ptag:assign:tog:{product_id}:{tag_id} — toggle a tag on a product
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
from utils.safe_conversation import end_conversation as _ptag_conv_end
from services.product_tags_service import (
    ensure_default_tags,
    list_tags,
    create_tag,
    update_tag,
    delete_tag,
    move_tag,
    assigned_tag_ids,
    toggle_product_tag,
)

logger = logging.getLogger(__name__)

PTAG_NEW_LABEL = 800
PTAG_NEW_EMOJI = 801
PTAG_EDIT_VALUE = 802


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
# Catalog
# ─────────────────────────────────────────────────────────────────────────────

async def ptag_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:catalog"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    ensure_default_tags()
    tags = list_tags()
    text = f"🏷 <b>Product Tags Catalog</b>\n\nTags: {len(tags)}\n\nAdmin controls everything — rename, disable, reorder, delete, or add new tags."
    rows = []
    for t in tags:
        state = "✅" if t["is_active"] else "🚫"
        rows.append([InlineKeyboardButton(
            f"{state} {t['emoji'] or ''} {t['label']}".strip(),
            callback_data=f"ptag:tag:{t['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ New Tag", callback_data="ptag:new")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="pib:admin:products:0")])
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def ptag_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:tag:{tag_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        tag_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    tags = {t["id"]: t for t in list_tags()}
    t = tags.get(tag_id)
    if not t:
        await query.answer("Tag not found.", show_alert=True)
        return

    text = (
        f"🏷 <b>Tag: {t['label']}</b>\n\n"
        f"Key: <code>{t['key']}</code>\n"
        f"Emoji: {t['emoji'] or '—'}\n"
        f"Active: {'✅' if t['is_active'] else '❌'}"
    )
    rows = [
        [
            InlineKeyboardButton("😀 Emoji", callback_data=f"ptag:edit:emoji:{tag_id}"),
            InlineKeyboardButton("✏️ Label", callback_data=f"ptag:edit:label:{tag_id}"),
        ],
        [
            InlineKeyboardButton("⬆️ Up", callback_data=f"ptag:up:{tag_id}"),
            InlineKeyboardButton("⬇️ Down", callback_data=f"ptag:dn:{tag_id}"),
            InlineKeyboardButton("✅/🚫 Toggle", callback_data=f"ptag:tog:{tag_id}"),
        ],
        [InlineKeyboardButton("🗑 Delete Tag", callback_data=f"ptag:del:{tag_id}")],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="ptag:catalog")],
    ]
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def ptag_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    try:
        tag_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    tags = {t["id"]: t for t in list_tags()}
    t = tags.get(tag_id)
    if t:
        update_tag(tag_id, is_active=not t["is_active"])
    await query.answer("Updated.")
    await ptag_tag(update, context)


async def ptag_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    parts = query.data.split(":")
    direction = -1 if parts[1] == "up" else 1
    try:
        tag_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    moved = move_tag(tag_id, direction)
    await query.answer("Moved." if moved else "Already at the edge.")
    await ptag_tag(update, context)


async def ptag_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        tag_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"ptag:delok:{tag_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"ptag:tag:{tag_id}")],
    ])
    await _edit_or_reply(update, "🗑 Delete this tag? It will be removed from every product it's assigned to.", kb)


async def ptag_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        tag_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    delete_tag(tag_id)
    await ptag_catalog(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Create Tag (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def ptag_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:new"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data["ptag_new"] = {}
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="ptag:catalog")
    ]])
    await _edit_or_reply(update,
        "➕ <b>New Tag</b>\n\nStep 1/2 — Enter the <b>label</b> (e.g. 'Limited Time'):",
        cancel_kb)
    return PTAG_NEW_LABEL


async def ptag_new_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["ptag_new"]["label"] = update.message.text.strip()[:64]
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="ptag:catalog")
    ]])
    await update.message.reply_text(
        "Step 2/2 — Send an <b>emoji</b> (or send <code>-</code> for none):",
        reply_markup=cancel_kb, parse_mode="HTML")
    return PTAG_NEW_EMOJI


async def ptag_new_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    value = update.message.text.strip()
    emoji = "" if value == "-" else value[:32]
    data = context.user_data.get("ptag_new", {})
    label = data.get("label", "New Tag")
    key = label.lower().replace(" ", "_")

    ok, msg = create_tag(key=key, label=label, emoji=emoji)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Catalog", callback_data="ptag:catalog")
    ]])
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
    context.user_data.pop("ptag_new", None)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Edit label / emoji (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def ptag_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:edit:{field}:{tag_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    parts = query.data.split(":")
    try:
        field = parts[2]
        tag_id = int(parts[3])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["ptag_edit_id"] = tag_id
    context.user_data["ptag_edit_field"] = field

    prompts = {
        "emoji": "😀 Enter new <b>emoji</b> (or send <code>-</code> to remove):",
        "label": "✏️ Enter new <b>label</b>:",
    }
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"ptag:tag:{tag_id}")
    ]])
    await _edit_or_reply(update, prompts.get(field, "Enter new value:"), cancel_kb)
    return PTAG_EDIT_VALUE


async def ptag_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    tag_id = context.user_data.get("ptag_edit_id")
    field = context.user_data.get("ptag_edit_field")
    if not tag_id or not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    if field == "emoji" and value == "-":
        value = ""

    update_tag(tag_id, **{field: value})

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Tag", callback_data=f"ptag:tag:{tag_id}")
    ]])
    await update.message.reply_text(f"✅ <b>{field.title()}</b> updated.",
                                    reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Per-Product Assignment
# ─────────────────────────────────────────────────────────────────────────────

async def ptag_assign_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:assign:{product_id}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        product_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    ensure_default_tags()
    from database import get_db_session
    from database.models import Product
    with get_db_session() as session:
        p = session.query(Product).filter_by(id=product_id).first()
        pname = p.name if p else "Unknown Product"

    tags = list_tags(active_only=True)
    assigned = assigned_tag_ids(product_id)

    text = f"🏷 <b>Tags: {pname}</b>\n\nTap to assign/unassign:"
    rows = []
    for t in tags:
        mark = "✅" if t["id"] in assigned else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {t['emoji'] or ''} {t['label']}".strip(),
            callback_data=f"ptag:assign:tog:{product_id}:{t['id']}"
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pib:admin:prod:{product_id}")])
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def ptag_assign_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ptag:assign:tog:{product_id}:{tag_id}"""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    parts = query.data.split(":")
    try:
        product_id = int(parts[3])
        tag_id = int(parts[4])
    except (IndexError, ValueError):
        await query.answer()
        return
    toggle_product_tag(product_id, tag_id)
    await query.answer("Updated.")
    await ptag_assign_list(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def build_ptag_conversation() -> ConversationHandler:
    return ConversationHandler(
        conversation_timeout=300,
        entry_points=[
            CallbackQueryHandler(ptag_new_start, pattern=r"^ptag:new$"),
            CallbackQueryHandler(ptag_edit_start, pattern=r"^ptag:edit:(emoji|label):\d+$"),
        ],
        states={
            PTAG_NEW_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ptag_new_label)],
            PTAG_NEW_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, ptag_new_emoji)],
            PTAG_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ptag_edit_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(_ptag_conv_end, pattern=r"^ptag:catalog$"),
            MessageHandler(filters.COMMAND, _ptag_conv_end),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    application.add_handler(build_ptag_conversation(), group=0)
    application.add_handler(CallbackQueryHandler(ptag_catalog, pattern=r"^ptag:catalog$"))
    application.add_handler(CallbackQueryHandler(ptag_tag, pattern=r"^ptag:tag:\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_toggle, pattern=r"^ptag:tog:\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_move, pattern=r"^ptag:(up|dn):\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_delete_confirm, pattern=r"^ptag:del:\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_delete_ok, pattern=r"^ptag:delok:\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_assign_toggle, pattern=r"^ptag:assign:tog:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(ptag_assign_list, pattern=r"^ptag:assign:\d+$"))
