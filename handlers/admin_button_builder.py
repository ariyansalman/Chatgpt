"""Button Builder — Admin Handlers — Premium Product System, Phase 1, Feature 5.

Global label/emoji/visibility/order control for the shared product-page
buttons (Buy Now, Back, Support, View Plans, Refresh, Favorite, Home).
Editing a button here NEVER changes callback_data — see
services/button_builder_service.py.

Callback-data namespace:
    btnb:list                  — list all buttons
    btnb:item:{key}             — button detail / actions
    btnb:tog:{key}              — toggle visibility
    btnb:up:{key} / btnb:dn:{key} — reorder
    btnb:edit:{field}:{key}     — start editing label/emoji
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
from utils.safe_conversation import end_conversation as _btnb_conv_end
from services.button_builder_service import (
    list_all,
    get_button,
    update_button,
    toggle_visibility,
    move_button,
)

logger = logging.getLogger(__name__)

BTNB_EDIT_VALUE = 790


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


def _list_text_and_rows():
    buttons = list_all()
    text = "🔘 <b>Button Builder</b>\n\nControls every product-page button's label, emoji, visibility, and order.\n"
    rows = []
    for b in buttons:
        vis = "👁" if b["is_visible"] else "🙈"
        rows.append([InlineKeyboardButton(
            f"{vis} {b['emoji']} {b['label']}".strip(),
            callback_data=f"btnb:item:{b['key']}"
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="pib:admin:products:0")])
    return text, rows


async def btnb_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: btnb:list"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    text, rows = _list_text_and_rows()
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def btnb_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: btnb:item:{key}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    try:
        key = query.data.split(":")[2]
    except IndexError:
        return
    b = get_button(key)
    text = (
        f"🔘 <b>Button: {key}</b>\n\n"
        f"Label: {b['label']}\n"
        f"Emoji: {b['emoji'] or '—'}\n"
        f"Visible: {'✅' if b['is_visible'] else '❌'}\n"
        f"Order: {b['display_order']}\n\n"
        "<i>Editing here never changes what tapping the button does.</i>"
    )
    rows = [
        [
            InlineKeyboardButton("😀 Emoji", callback_data=f"btnb:edit:emoji:{key}"),
            InlineKeyboardButton("✏️ Label", callback_data=f"btnb:edit:label:{key}"),
        ],
        [
            InlineKeyboardButton("⬆️ Up", callback_data=f"btnb:up:{key}"),
            InlineKeyboardButton("⬇️ Down", callback_data=f"btnb:dn:{key}"),
            InlineKeyboardButton("👁/🙈 Toggle", callback_data=f"btnb:tog:{key}"),
        ],
        [InlineKeyboardButton("🔙 Back to Buttons", callback_data="btnb:list")],
    ]
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def btnb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    try:
        key = query.data.split(":")[2]
    except IndexError:
        await query.answer()
        return
    toggle_visibility(key)
    await query.answer("Visibility updated.")
    await btnb_item(update, context)


async def btnb_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    parts = query.data.split(":")
    direction = -1 if parts[1] == "up" else 1
    try:
        key = parts[2]
    except IndexError:
        await query.answer()
        return
    moved = move_button(key, direction)
    await query.answer("Moved." if moved else "Already at the edge.")
    await btnb_item(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Edit label / emoji (Conversation)
# ─────────────────────────────────────────────────────────────────────────────

async def btnb_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: btnb:edit:{field}:{key}"""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    parts = query.data.split(":")
    try:
        field = parts[2]
        key = parts[3]
    except IndexError:
        return ConversationHandler.END

    context.user_data["btnb_edit_key"] = key
    context.user_data["btnb_edit_field"] = field

    prompts = {
        "emoji": "😀 Enter new <b>emoji</b> (or send <code>-</code> to remove):",
        "label": "✏️ Enter new <b>label text</b>:",
    }
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"btnb:item:{key}")
    ]])
    await _edit_or_reply(update, prompts.get(field, "Enter new value:"), cancel_kb)
    return BTNB_EDIT_VALUE


async def btnb_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    key = context.user_data.get("btnb_edit_key")
    field = context.user_data.get("btnb_edit_field")
    if not key or not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    if field == "emoji" and value == "-":
        value = ""

    update_button(key, **{field: value})

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Button", callback_data=f"btnb:item:{key}")
    ]])
    await update.message.reply_text(f"✅ <b>{field.title()}</b> updated.",
                                    reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def build_btnb_conversation() -> ConversationHandler:
    return ConversationHandler(
        conversation_timeout=300,
        entry_points=[
            CallbackQueryHandler(btnb_edit_start, pattern=r"^btnb:edit:(emoji|label):[a-z_]+$"),
        ],
        states={
            BTNB_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, btnb_edit_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(_btnb_conv_end, pattern=r"^btnb:list$"),
            MessageHandler(filters.COMMAND, _btnb_conv_end),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    application.add_handler(build_btnb_conversation(), group=0)
    application.add_handler(CallbackQueryHandler(btnb_list, pattern=r"^btnb:list$"))
    application.add_handler(CallbackQueryHandler(btnb_item, pattern=r"^btnb:item:[a-z_]+$"))
    application.add_handler(CallbackQueryHandler(btnb_toggle, pattern=r"^btnb:tog:[a-z_]+$"))
    application.add_handler(CallbackQueryHandler(btnb_move, pattern=r"^btnb:(up|dn):[a-z_]+$"))
