"""Menu Actions — all CRUD and conversation handlers for the Admin Menu Manager.

Covers:
  - Per-item detail, visibility, enabled, audience, move, rename/emoji edit
  - Custom button management (mm:custom:*)
  - Admin Menu Manager (mm:amgr:*) — full CRUD on every registered menu
  - Conversation handlers for text input (rename, emoji, callback, add item,
    create menu)
  - build_mm_edit_conversation()
  - build_custom_button_conversation()
  - build_amgr_conversation()

No rendering-specific code (button_markup builders), preview logic, color
management, or layout management lives here.
"""

from __future__ import annotations

import copy
import html
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils.button_colors import (
    color_emoji,
    color_label,
    cycle_color,
    default_color_for_button,
    normalize_color,
    random_color,
)
from utils.menu_registry import (
    MENU_AUDIENCE_LABELS,
    MENU_AUDIENCES,
    get_item_label,
    get_menu_layout,
    is_item_enabled,
    is_item_visible,
    normalize_menu_audience,
    save_menu_layout,
)
from handlers.menu_colors import (
    DEFAULT_ITEM_STYLE,
    STYLE_DOTS,
    amgr_color_picker_rows,
    color_picker_rows,
    get_item_style,
)
from handlers.menu_preview import mm_refresh_preview
from handlers.menu_renderer import build_item_markup, save_reordered
from handlers.menu_state import (
    active_audience,
    audience_label,
    custom_bool,
    get_item,
    get_items,
    is_admin_user,
    item_display_name,
    item_detail_text,
    item_key_set,
    parse_custom_buttons,
    safe_edit,
    save_custom_buttons,
    save_items,
    update_item,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-item detail + toggle handlers (main menu items)
# ─────────────────────────────────────────────────────────────────────────────

async def mm_item_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    key = (query.data or "").split(":", 2)[-1]
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Menu item not found.", show_alert=True)
        return
    await query.answer()
    await safe_edit(query, item_detail_text(item, context), build_item_markup(item, context))


async def mm_toggle_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    key = (query.data or "").split(":", 2)[-1]
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Menu item not found.", show_alert=True)
        return
    new_value = not is_item_visible(item)
    update_item(key, context, visible=new_value)
    await query.answer(f"✅ {item_display_name(item)} is now {'shown' if new_value else 'hidden'}.")
    await mm_refresh_preview(context)
    await mm_item_detail(update, context)


async def mm_toggle_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    key = (query.data or "").split(":", 2)[-1]
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Menu item not found.", show_alert=True)
        return
    new_value = not is_item_enabled(item)
    update_item(key, context, enabled=new_value)
    await query.answer(f"✅ {item_display_name(item)} is now {'enabled' if new_value else 'disabled'}.")
    await mm_refresh_preview(context)
    await mm_item_detail(update, context)


async def mm_set_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[3] not in {"all", "admin", "user", "premium"}:
        await query.answer("❌ Invalid audience.", show_alert=True)
        return
    key, audience = parts[2], parts[3]
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Menu item not found.", show_alert=True)
        return
    update_item(key, context, audience=audience)
    await query.answer(f"✅ Audience set to {audience_label(audience)}.")
    await mm_refresh_preview(context)
    await mm_item_detail(update, context)


async def mm_move_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"moveup", "movedown"}:
        await query.answer("❌ Invalid move.", show_alert=True)
        return
    items = copy.deepcopy(get_items(context))
    direction = "up" if parts[1] == "moveup" else "down"
    if not save_reordered(items, direction, parts[2], context):
        await query.answer("↔️ Already at the edge of the menu.", show_alert=False)
    else:
        await query.answer("✅ Menu order updated.")
    await mm_refresh_preview(context)
    item = get_item(parts[2], context)
    if item:
        await mm_item_detail(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Rename / emoji edit conversation (main menu items)
# ─────────────────────────────────────────────────────────────────────────────

async def mm_edit_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the inline rename/emoji editor."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"editname", "editemoji"}:
        await query.answer("❌ Invalid edit.", show_alert=True)
        return ConversationHandler.END
    item = get_item(parts[2], context)
    if not item:
        await query.answer("❌ Menu item not found.", show_alert=True)
        return ConversationHandler.END
    context.user_data["mm_edit_key"] = parts[2]
    context.user_data["mm_edit_field"] = "label" if parts[1] == "editname" else "emoji"
    await query.answer()
    prompt = "new name" if parts[1] == "editname" else "one emoji"
    await query.edit_message_text(
        f"✏️ Send the {prompt} for <b>{item_display_name(item)}</b>.\n\n"
        "Send /cancel to leave it unchanged.",
        parse_mode="HTML",
    )
    return 1


async def mm_edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_user(update):
        return ConversationHandler.END
    value = (update.message.text or "").strip()
    key = context.user_data.pop("mm_edit_key", None)
    field = context.user_data.pop("mm_edit_field", None)
    if not key or field not in {"label", "emoji"}:
        return ConversationHandler.END
    if field == "emoji":
        if len(value) > 16 or any(char.isalnum() for char in value):
            await update.message.reply_text("❌ Send an emoji only, or /cancel.")
            context.user_data["mm_edit_key"] = key
            context.user_data["mm_edit_field"] = field
            return 1
    elif len(value) > 64:
        await update.message.reply_text("❌ Names must be 64 characters or fewer.")
        context.user_data["mm_edit_key"] = key
        context.user_data["mm_edit_field"] = field
        return 1
    if not get_item(key, context):
        await update.message.reply_text("❌ Menu item no longer exists.")
        return ConversationHandler.END
    update_item(key, context, **{field: value})
    await mm_refresh_preview(context)
    await update.message.reply_text(
        "✅ Saved immediately. The live preview above has been updated."
    )
    return ConversationHandler.END


async def mm_edit_value_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("mm_edit_key", None)
    context.user_data.pop("mm_edit_field", None)
    await update.message.reply_text("↩️ Edit cancelled.")
    return ConversationHandler.END


def build_mm_edit_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(mm_edit_value_start, pattern=r"^mm:(editname|editemoji):"),
        ],
        states={
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, mm_edit_value_received)],
        },
        fallbacks=[CommandHandler("cancel", mm_edit_value_cancel)],
        allow_reentry=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Profile selectors
# ─────────────────────────────────────────────────────────────────────────────

async def mm_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Choose which independently managed audience profile to edit."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    audience = active_audience(context)
    rows = []
    for option in MENU_AUDIENCES:
        marker = "✅ " if option == audience else ""
        rows.append([
            InlineKeyboardButton(
                f"{marker}{MENU_AUDIENCE_LABELS[option]}",
                callback_data=f"mm:profile:{option}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Menu Manager", callback_data="mm:menu")])
    await query.answer()
    await safe_edit(
        query,
        "👥 <b>Menu Profile</b>\n\n"
        "Choose the audience layout to manage. Changes to one profile "
        "do not affect the others.",
        InlineKeyboardMarkup(rows),
    )


async def mm_set_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    profile = normalize_menu_audience((query.data or "").split(":")[-1])
    if profile not in MENU_AUDIENCES:
        await query.answer("❌ Invalid profile.", show_alert=True)
        return
    context.user_data["mm_audience"] = profile
    context.user_data.pop("mm_preview_msg_id", None)
    context.user_data.pop("mm_preview_chat_id", None)
    await query.answer(f"✅ Editing {MENU_AUDIENCE_LABELS[profile]}.")
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Custom Buttons
# ─────────────────────────────────────────────────────────────────────────────

def _custom_index(query_data: str) -> Optional[int]:
    try:
        return int(query_data.split(":")[-1])
    except (TypeError, ValueError):
        return None


def _custom_normalize_positions(buttons: list) -> None:
    """Keep explicit positions in sync while preserving unlimited list order."""
    for position, button in enumerate(buttons, start=1):
        button["position"] = position


def _custom_destination(button: dict) -> str:
    return button.get("callback") or button.get("url") or ""


def _custom_prompt(context: ContextTypes.DEFAULT_TYPE) -> str:
    draft = context.user_data["mm_custom_draft"]
    step = context.user_data["mm_custom_step"]
    mode = context.user_data["mm_custom_mode"]
    field = ("name", "emoji", "color", "callback", "url", "position", "visible")[step]
    current = draft.get("label" if field == "name" else field, "")
    if isinstance(current, bool):
        current = "visible" if current else "hidden"
    suffix = (
        f" Current: <code>{html.escape(str(current))}</code>."
        if mode == "edit" and current != ""
        else ""
    )
    prompts = [
        ("Name", "Send the button name." if mode == "add" else "Send the button name, or /keep to leave it unchanged."),
        ("Emoji", "Send an emoji, or /skip for none." if mode == "add" else "Send an emoji, /clear to remove it, or /keep to leave it unchanged."),
        ("Color", "Send one of the 12 palette colors (green, blue, red, yellow, orange, purple, pink, black, white, brown, cyan, lime)." if mode == "add" else "Send a palette color or /keep."),
        ("Callback", "Send the callback data, or /skip if this button uses a URL." if mode == "add" else "Send callback data, /clear to remove it, or /keep."),
        ("URL", "Send the URL, or /skip if this button uses a callback." if mode == "add" else "Send a URL, /clear to remove it, or /keep."),
        ("Position", "Send a 1-based position, or /skip to place it last." if mode == "add" else "Send a 1-based position, or /keep."),
        ("Visibility", "Send visible or hidden." if mode == "add" else "Send visible, hidden, or /keep."),
    ]
    title, instruction = prompts[step]
    return f"✏️ <b>{title}</b>{suffix}\n\n{instruction}\n\nSend /cancel to cancel."


async def mm_custom_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📌 Custom Buttons submenu — list and manage custom main menu buttons."""
    query = update.callback_query
    await query.answer()

    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    buttons = parse_custom_buttons(context)
    rows: List[List[InlineKeyboardButton]] = []

    if buttons:
        for idx, btn in enumerate(buttons):
            label = btn.get("label", f"Button {idx + 1}")
            emoji = btn.get("emoji", "")
            color = normalize_color(btn.get("color", btn.get("style", "white")))
            visible = custom_bool(btn.get("visible", True))
            destination = btn.get("callback") or btn.get("url") or "not set"
            rows.append([
                InlineKeyboardButton(
                    f"{'👁' if visible else '🚫'} {emoji} {str(label)} · "
                    f"{str(color)}".strip(),
                    callback_data=f"mm:custom:edit:{idx}",
                ),
            ])
            rows.append([
                InlineKeyboardButton(
                    "🎨 Change Color",
                    callback_data=f"mm:custom:color:{idx}",
                ),
            ])
            rows.append([
                InlineKeyboardButton("⬆️", callback_data=f"mm:custom:up:{idx}"),
                InlineKeyboardButton("⬇️", callback_data=f"mm:custom:down:{idx}"),
                InlineKeyboardButton(
                    "👁 Hide" if visible else "👁 Show",
                    callback_data=f"mm:custom:visibility:{idx}",
                ),
                InlineKeyboardButton("🗑 Delete", callback_data=f"mm:custom:del:{idx}"),
            ])
            rows.append([
                InlineKeyboardButton(
                    f"↗ {str(destination)[:42]}",
                    callback_data="mm:noop",
                ),
            ])
        rows.append([InlineKeyboardButton("🗑 Clear All", callback_data="mm:custom:clear")])
    else:
        rows.append([InlineKeyboardButton("(no custom buttons yet)", callback_data="mm:noop")])

    rows.append([InlineKeyboardButton("➕ Add Button", callback_data="mm:custom:add")])
    rows.append([InlineKeyboardButton("⬅️ Menu Manager", callback_data="mm:menu")])

    text = (
        "📌 <b>Custom Button Manager</b>\n\n"
        f"You have <b>{len(buttons)}</b> custom button(s).\n\n"
        "Custom buttons appear below the standard menu items in this order.\n"
        "Tap a button to edit it. Use the arrow, visibility, and delete controls "
        "to manage it immediately.\n\n"
        "<i>There is no button limit.</i>"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


async def mm_custom_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the add-button conversation."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["mm_custom_mode"] = "add"
    context.user_data["mm_custom_index"] = None
    context.user_data["mm_custom_step"] = 0
    context.user_data["mm_custom_draft"] = {
        "label": "", "emoji": "", "color": "white",
        "callback": "", "url": "", "position": "", "visible": True,
    }
    await query.edit_message_text(_custom_prompt(context), parse_mode="HTML")
    return 1


async def mm_custom_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start editing one custom button."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    idx = _custom_index(query.data or "")
    buttons = parse_custom_buttons(context)
    if idx is None or idx < 0 or idx >= len(buttons):
        await query.answer("❌ Button not found.", show_alert=True)
        return ConversationHandler.END
    button = buttons[idx]
    await query.answer()
    context.user_data["mm_custom_mode"] = "edit"
    context.user_data["mm_custom_index"] = idx
    context.user_data["mm_custom_step"] = 0
    context.user_data["mm_custom_draft"] = {
        "label":    str(button.get("label", "")),
        "emoji":    str(button.get("emoji", "")),
        "color":    normalize_color(button.get("color", button.get("style", "white"))),
        "callback": str(button.get("callback", "")),
        "url":      str(button.get("url", "")),
        "position": str(button.get("position", idx + 1)),
        "visible":  custom_bool(button.get("visible", True)),
    }
    await query.edit_message_text(_custom_prompt(context), parse_mode="HTML")
    return 1


async def mm_custom_wizard_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect and immediately persist a custom button's fields."""
    if not is_admin_user(update):
        return ConversationHandler.END
    draft = context.user_data.get("mm_custom_draft")
    if not draft:
        return ConversationHandler.END
    step = int(context.user_data.get("mm_custom_step", 0))
    value = (update.message.text or "").strip()
    field = ("name", "emoji", "color", "callback", "url", "position", "visible")[step]
    if value.lower() == "/keep":
        pass
    elif value.lower() in ("/skip", "/clear"):
        if field == "name":
            await update.message.reply_text("❌ Name is required. Send a name or /cancel.")
            return 1
        draft["label" if field == "name" else field] = "" if field != "visible" else True
    elif field == "name":
        if not value or len(value) > 64:
            await update.message.reply_text("❌ Name must be 1–64 characters.")
            return 1
        draft["label"] = value
    elif field == "emoji":
        if len(value) > 16 or any(char.isalnum() for char in value):
            await update.message.reply_text("❌ Send an emoji only, /skip, or /clear.")
            return 1
        draft["emoji"] = value
    elif field == "color":
        selected_color = normalize_color(value, fallback="")
        if not selected_color:
            await update.message.reply_text(
                "❌ Choose one of: green, blue, red, yellow, orange, purple, "
                "pink, black, white, brown, cyan, lime."
            )
            return 1
        draft["color"] = selected_color
    elif field == "callback":
        if len(value.encode("utf-8")) > 64:
            await update.message.reply_text("❌ Callback data must be 64 bytes or fewer.")
            return 1
        draft[field] = value
    elif field == "url":
        if len(value) > 2048:
            await update.message.reply_text("❌ URL must be 2,048 characters or fewer.")
            return 1
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "tg"}:
            await update.message.reply_text(
                "❌ URL must start with http://, https://, or tg://."
            )
            return 1
        draft[field] = value
    elif field == "position":
        try:
            position = int(value)
            if position < 1:
                raise ValueError
            draft["position"] = position
        except ValueError:
            await update.message.reply_text("❌ Position must be a positive whole number.")
            return 1
    elif field == "visible":
        if value.lower() not in {"visible", "hidden"}:
            await update.message.reply_text("❌ Send visible or hidden.")
            return 1
        draft["visible"] = value.lower() == "visible"

    if step < 6:
        context.user_data["mm_custom_step"] = step + 1
        await update.message.reply_text(_custom_prompt(context), parse_mode="HTML")
        return 1

    if not draft.get("label", "").strip():
        context.user_data["mm_custom_step"] = 0
        await update.message.reply_text("❌ A button name is required. Send a name.")
        return 1
    if not draft.get("callback") and not draft.get("url"):
        context.user_data["mm_custom_step"] = 3
        await update.message.reply_text(
            "❌ Add a callback or URL. Send callback data now, or /skip to continue to URL.",
        )
        return 1
    if draft.get("callback") and draft.get("url"):
        context.user_data["mm_custom_step"] = 4
        await update.message.reply_text(
            "❌ Use either a callback or a URL, not both. Send a URL now or /clear it.",
        )
        return 1

    buttons = parse_custom_buttons(context)
    saved = {
        "label":   draft["label"],
        "emoji":   draft.get("emoji", ""),
        "color":   draft.get("color", "none"),
        "visible": bool(draft.get("visible", True)),
    }
    if draft.get("callback"):
        saved["callback"] = draft["callback"]
    if draft.get("url"):
        saved["url"] = draft["url"]
    index = context.user_data.get("mm_custom_index")
    editing = context.user_data.get("mm_custom_mode") == "edit" and isinstance(index, int)
    if editing and 0 <= index < len(buttons):
        legacy_fields = {
            k: v for k, v in buttons[index].items()
            if k not in {"label", "emoji", "color", "style", "callback", "url",
                         "position", "visible"}
        }
        saved = {**legacy_fields, **saved}
        if "emoji_id" in buttons[index]:
            saved["emoji_id"] = buttons[index]["emoji_id"]
        buttons.pop(index)
    buttons.append(saved)
    position = draft.get("position")
    try:
        target = max(1, min(int(position), len(buttons))) if position else len(buttons)
    except (TypeError, ValueError):
        target = len(buttons)
    buttons.insert(target - 1, buttons.pop())
    _custom_normalize_positions(buttons)
    save_custom_buttons(buttons, context)
    context.user_data.pop("mm_custom_draft", None)
    context.user_data.pop("mm_custom_step", None)
    context.user_data.pop("mm_custom_mode", None)
    context.user_data.pop("mm_custom_index", None)
    await mm_refresh_preview(context)
    await update.message.reply_text(
        "✅ Custom button saved. The live preview above has been updated."
    )
    return ConversationHandler.END


async def mm_custom_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("mm_custom_draft", "mm_custom_step", "mm_custom_mode", "mm_custom_index"):
        context.user_data.pop(key, None)
    await update.message.reply_text("↩️ Custom button edit cancelled.")
    return ConversationHandler.END


def build_custom_button_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(mm_custom_add,  pattern=r"^mm:custom:add$"),
            CallbackQueryHandler(mm_custom_edit, pattern=r"^mm:custom:edit:\d+$"),
        ],
        states={
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, mm_custom_wizard_message)],
        },
        fallbacks=[
            CommandHandler("cancel", mm_custom_cancel),
            CommandHandler("skip",   mm_custom_wizard_message),
            CommandHandler("clear",  mm_custom_wizard_message),
        ],
        allow_reentry=True,
    )


async def mm_custom_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a custom button by index."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        idx = int(parts[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid index.", show_alert=True)
        return
    buttons = parse_custom_buttons(context)
    if 0 <= idx < len(buttons):
        removed = buttons.pop(idx)
        _custom_normalize_positions(buttons)
        save_custom_buttons(buttons, context)
        await query.answer(f"🗑 Removed: {removed.get('label', 'button')}", show_alert=False)
        await mm_refresh_preview(context)
    else:
        await query.answer("❌ Button not found.", show_alert=True)
    await mm_custom_menu(update, context)


async def mm_custom_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a custom button's visibility."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    idx = _custom_index(query.data or "")
    buttons = parse_custom_buttons(context)
    if idx is None or idx < 0 or idx >= len(buttons):
        await query.answer("❌ Button not found.", show_alert=True)
        return
    buttons[idx]["visible"] = not custom_bool(buttons[idx].get("visible", True))
    _custom_normalize_positions(buttons)
    save_custom_buttons(buttons, context)
    await query.answer(
        "✅ Button shown." if buttons[idx]["visible"] else "🚫 Button hidden.",
        show_alert=False,
    )
    await mm_refresh_preview(context)
    await mm_custom_menu(update, context)


async def mm_custom_color_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the complete palette for one custom main-menu button."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    try:
        idx = int(parts[-1])
    except (TypeError, ValueError):
        await query.answer("❌ Invalid button.", show_alert=True)
        return
    buttons = parse_custom_buttons(context)
    if idx < 0 or idx >= len(buttons):
        await query.answer("❌ Button not found.", show_alert=True)
        return
    current = normalize_color(buttons[idx].get("color", buttons[idx].get("style", "white")))
    rows = color_picker_rows(f"mm:custom:setcolor:{idx}", current)
    rows.extend([
        [
            InlineKeyboardButton("🎲 Random Color", callback_data=f"mm:custom:random:{idx}"),
            InlineKeyboardButton("🔄 Cycle Colors", callback_data=f"mm:custom:cycle:{idx}"),
        ],
        [InlineKeyboardButton("♻ Reset Button Color", callback_data=f"mm:custom:resetcolor:{idx}")],
        [InlineKeyboardButton("⬅️ Custom Buttons", callback_data="mm:custom")],
    ])
    await query.answer()
    await safe_edit(
        query,
        f"🎨 <b>Change Color</b>\n\n"
        f"<b>{html.escape(str(buttons[idx].get('label', f'Button {idx + 1}')))}</b>\n"
        f"Current: <b>{html.escape(color_label(current))}</b>\n\n"
        "Choose a color. The live preview refreshes immediately.",
        InlineKeyboardMarkup(rows),
    )


async def mm_custom_color_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply a selected, random, cycled, or reset custom-button color."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.answer("❌ Invalid color action.", show_alert=True)
        return
    action = parts[2]
    value = parts[3]
    try:
        idx = int(value)
    except ValueError:
        await query.answer("❌ Invalid button.", show_alert=True)
        return
    buttons = parse_custom_buttons(context)
    if idx < 0 or idx >= len(buttons):
        await query.answer("❌ Button not found.", show_alert=True)
        return
    current = normalize_color(buttons[idx].get("color", buttons[idx].get("style", "white")))
    if action == "setcolor":
        if len(parts) != 5:
            await query.answer("❌ Invalid color.", show_alert=True)
            return
        selected = normalize_color(parts[4], fallback="")
    elif action == "random":
        selected = random_color()
    elif action == "cycle":
        selected = cycle_color(current)
    elif action == "resetcolor":
        selected = "white"
    else:
        selected = ""
    if not selected:
        await query.answer("❌ Unknown color.", show_alert=True)
        return
    buttons[idx]["color"] = selected
    buttons[idx].pop("style", None)
    _custom_normalize_positions(buttons)
    save_custom_buttons(buttons, context)
    await query.answer(f"{color_emoji(selected)} {color_label(selected)} applied.", show_alert=False)
    await mm_refresh_preview(context)
    query.data = "mm:custom"
    await mm_custom_menu(update, context)


async def mm_custom_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move a custom button one position while preserving all its fields."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[2] not in {"up", "down"}:
        await query.answer("❌ Invalid move.", show_alert=True)
        return
    try:
        idx = int(parts[3])
    except ValueError:
        await query.answer("❌ Invalid position.", show_alert=True)
        return
    buttons = parse_custom_buttons(context)
    neighbor = idx - 1 if parts[2] == "up" else idx + 1
    if idx < 0 or idx >= len(buttons) or neighbor < 0 or neighbor >= len(buttons):
        await query.answer("↔️ Already at the edge.", show_alert=False)
        return
    buttons[idx], buttons[neighbor] = buttons[neighbor], buttons[idx]
    _custom_normalize_positions(buttons)
    save_custom_buttons(buttons, context)
    await query.answer("✅ Position updated.", show_alert=False)
    await mm_refresh_preview(context)
    await mm_custom_menu(update, context)


async def mm_custom_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all custom buttons."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    save_custom_buttons([], context)
    await query.answer("✅ All custom buttons cleared.", show_alert=False)
    await mm_refresh_preview(context)
    await mm_custom_menu(update, context)


# =============================================================================
# Admin Menu Manager (mm:amgr:*) — manage ALL registered admin menus
# =============================================================================

_AMGR_BUILTIN_IDS = (
    "admin_menu",
    "admin_products_menu",
    "admin_category_menu",
    "admin_user_menu",
    "admin_order_menu",
    "admin_settings_menu",
    "admin_broadcast_menu",
    "user_account_menu",
)

_AMGR_MENU_LABELS: dict[str, str] = {
    "admin_menu":           "📋 Admin Main Menu",
    "admin_products_menu":  "📦 Product Management",
    "admin_category_menu":  "📁 Category Management",
    "admin_user_menu":      "👥 User Management",
    "admin_order_menu":     "🛍 Order Management",
    "admin_settings_menu":  "⚙️ Store Settings",
    "admin_broadcast_menu": "📢 Broadcast",
    "user_account_menu":    "👤 User Account Menu",
}

_CUSTOM_MENUS_CFG_KEY = "amgr_custom_menus_json"


def _cfg_safe_get(key: str, default: str = "") -> str:
    try:
        from utils.bot_config import cfg
        return cfg.get_str(key, default)
    except Exception:
        return default


def _load_custom_menus() -> list:
    from utils.bot_config import cfg
    raw = _cfg_safe_get(_CUSTOM_MENUS_CFG_KEY, "")
    if not raw.strip():
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _save_custom_menus(menus: list) -> None:
    from utils.bot_config import cfg
    cfg.set(_CUSTOM_MENUS_CFG_KEY, json.dumps(menus, ensure_ascii=False))


def _amgr_all_menu_ids() -> list:
    ids: list = list(_AMGR_BUILTIN_IDS)
    try:
        from utils.menu_builder import list_menus
        for mid in list_menus():
            if mid not in ids:
                ids.append(mid)
    except Exception:
        pass
    for entry in _load_custom_menus():
        mid = entry.get("id", "")
        if mid and mid not in ids:
            ids.append(mid)
    return ids


def _amgr_menu_label(menu_id: str) -> str:
    if menu_id in _AMGR_MENU_LABELS:
        return _AMGR_MENU_LABELS[menu_id]
    for entry in _load_custom_menus():
        if entry.get("id") == menu_id:
            desc = entry.get("description", "")
            return f"🔧 {desc}" if desc else f"🔧 {menu_id}"
    try:
        from utils.menu_builder import get_menu_description
        desc = get_menu_description(menu_id)
        return desc if desc and desc != menu_id else f"📋 {menu_id}"
    except Exception:
        return f"📋 {menu_id}"


def _amgr_parse(data: str) -> tuple:
    """Parse mm:amgr:<menu_id>:<action>:<key> into (menu_id, action, key)."""
    tail = data[len("mm:amgr:"):]
    for action in (
        "item", "visible", "enabled", "moveup", "movedown",
        "style", "color", "random", "cycle", "resetcolor", "preview", "reset",
        "rename", "emoji", "callback",
        "audopt", "audience",
        "delconfirm", "delitem",
        "additem",
        "deletemenu",
    ):
        marker = f":{action}:"
        if marker in tail:
            idx = tail.index(marker)
            menu_id = tail[:idx]
            key = tail[idx + len(marker):]
            return menu_id, action, key
        end_marker = f":{action}"
        if tail.endswith(end_marker):
            menu_id = tail[: -len(end_marker)]
            return menu_id, action, ""
    return tail.rstrip(":"), "", ""


def _amgr_items(menu_id: str) -> list:
    try:
        from utils.menu_builder import get_menu_items
        return [
            item for item in get_menu_items(menu_id)
            if isinstance(item, dict) and item.get("key")
        ]
    except Exception:
        return []


def _amgr_item(menu_id: str, key: str) -> Optional[dict]:
    return next((i for i in _amgr_items(menu_id) if i["key"] == key), None)


def _amgr_save_items(menu_id: str, items: list) -> None:
    try:
        from utils.menu_builder import save_menu
        save_menu(menu_id, items)
    except Exception:
        logger.warning("mm_amgr: failed to save menu %s", menu_id, exc_info=True)


def _amgr_item_label(item: dict) -> str:
    return get_item_label(item, "en")


def _amgr_reorder(items: list, direction: str, key: str) -> bool:
    slots = sorted(
        {(int(i.get("row", 0)), int(i.get("order", 0))) for i in items}
    )
    occ = {(int(i.get("row", 0)), int(i.get("order", 0))): i for i in items}
    current = next((i for i in items if i["key"] == key), None)
    if not current:
        return False
    cur_slot = (int(current.get("row", 0)), int(current.get("order", 0)))
    try:
        idx = slots.index(cur_slot)
    except ValueError:
        return False
    nb_idx = idx - 1 if direction == "up" else idx + 1
    if nb_idx < 0 or nb_idx >= len(slots):
        return False
    nb = occ[slots[nb_idx]]
    nb_slot = slots[nb_idx]
    current["row"], current["order"] = nb_slot
    nb["row"], nb["order"] = cur_slot
    return True


# ── Admin menu list ──────────────────────────────────────────────────────────

async def mm_amgr_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dashboard: list ALL registered menus (built-in + custom) with a button each."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    all_ids = _amgr_all_menu_ids()
    custom_ids = {e["id"] for e in _load_custom_menus()}

    rows: List[List[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("━━━ Built-in Menus ━━━", callback_data="mm:noop")])
    for mid in all_ids:
        label = _amgr_menu_label(mid)
        items = _amgr_items(mid)
        visible = sum(1 for i in items if is_item_visible(i))
        tag = " 🔧" if mid in custom_ids else ""
        rows.append([
            InlineKeyboardButton(
                f"{label}{tag}  ({visible}/{len(items)} items)",
                callback_data=f"mm:amgr:{mid}",
            )
        ])

    rows.append([InlineKeyboardButton("━━━━━━━━━━━━━━━━━━", callback_data="mm:noop")])
    rows.append([InlineKeyboardButton("➕ Create New Menu", callback_data="mm:amgr:createmenu")])
    rows.append([InlineKeyboardButton("⬅️ Menu Manager", callback_data="mm:menu")])
    text = (
        "🗂 <b>Admin Menu Builder</b>\n\n"
        "All registered menus are listed below. Changes take effect immediately.\n"
        "🔧 = custom (admin-created) menu\n\n"
        "<i>Tap a menu to manage items: visibility, enabled, label, emoji, "
        "color, callback, audience, position, add/delete items.\n"
        "Tap ➕ Create New Menu to add a menu for a future module.</i>"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


# ── Per-menu dashboard ───────────────────────────────────────────────────────

async def mm_amgr_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dashboard for a single registered admin menu."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, _ = _amgr_parse(query.data or "")
    if not menu_id:
        await query.answer("❌ Invalid menu.", show_alert=True)
        return
    await query.answer()

    items = _amgr_items(menu_id)
    menu_label = _amgr_menu_label(menu_id)
    custom_ids = {e["id"] for e in _load_custom_menus()}
    is_custom = menu_id in custom_ids

    rows: List[List[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("━━━ Menu Items ━━━", callback_data="mm:noop")])
    for item in items:
        key = item["key"]
        visible = is_item_visible(item)
        enabled = is_item_enabled(item)
        rows.append([
            InlineKeyboardButton(
                f"{'👁' if visible else '🚫'} {_amgr_item_label(item)}",
                callback_data=f"mm:amgr:{menu_id}:item:{key}",
            ),
            InlineKeyboardButton(
                f"{'✅' if enabled else '⏸'} {'Active' if enabled else 'Off'}",
                callback_data=f"mm:amgr:{menu_id}:item:{key}",
            ),
        ])

    rows.append([InlineKeyboardButton("━━━━━━━━━━━━━━━━━━", callback_data="mm:noop")])
    rows.append([
        InlineKeyboardButton("➕ Add Item", callback_data=f"mm:amgr:{menu_id}:additem"),
        InlineKeyboardButton("📺 Preview",  callback_data=f"mm:amgr:{menu_id}:preview"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 Reset to Default", callback_data=f"mm:amgr:{menu_id}:reset"),
        InlineKeyboardButton(
            "🗑 Delete Menu" if is_custom else "⚙️ Reset Items",
            callback_data=f"mm:amgr:{menu_id}:deletemenu" if is_custom
                          else f"mm:amgr:{menu_id}:reset",
        ),
    ])
    rows.append([InlineKeyboardButton("⬅️ Admin Menus", callback_data="mm:amgr")])

    items_summary = "\n".join(
        f"  {'👁' if is_item_visible(i) else '🚫'} "
        f"{'✅' if is_item_enabled(i) else '⏸'} "
        f"{_amgr_item_label(i)}"
        for i in items
    ) or "  (no items yet — use ➕ Add Item)"
    text = (
        f"📋 <b>{html.escape(menu_label)}</b>\n\n"
        f"<b>ID:</b> <code>{html.escape(menu_id)}</code>\n"
        f"<b>Type:</b> {'🔧 Custom' if is_custom else '🏛 Built-in'}\n"
        f"<b>Items:</b> {len(items)}\n\n"
        + items_summary
        + "\n\n"
        "Tap an item to manage: visibility, enabled, rename, emoji, "
        "callback, audience, color, position, delete.\n"
        "Tap ➕ Add Item to add a new button (e.g. for a future module).\n"
        "<i>Changes apply immediately.</i>"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


# ── Per-item detail ──────────────────────────────────────────────────────────

async def mm_amgr_item_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show full controls for a single item in an admin menu."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    await query.answer()

    visible = is_item_visible(item)
    enabled = is_item_enabled(item)
    style = normalize_color(
        item.get("style"),
        fallback=default_color_for_button(
            item.get("callback") or key,
            item.get("label", ""),
        ),
    )
    dot = STYLE_DOTS.get(style, "⚪")
    menu_label = _amgr_menu_label(menu_id)
    audience_val = str(item.get("audience", "admin" if item.get("admin_only") else "all"))
    callback_val = item.get("callback", "")
    emoji_val = item.get("emoji", "")
    label_val = _amgr_item_label(item)

    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'👁 Hide' if visible else '👁 Show'}",
                callback_data=f"mm:amgr:{menu_id}:visible:{key}",
            ),
            InlineKeyboardButton(
                f"{'🚫 Disable' if enabled else '✅ Enable'}",
                callback_data=f"mm:amgr:{menu_id}:enabled:{key}",
            ),
        ],
        [
            InlineKeyboardButton("✏️ Rename",         callback_data=f"mm:amgr:{menu_id}:rename:{key}"),
            InlineKeyboardButton("😀 Emoji",           callback_data=f"mm:amgr:{menu_id}:emoji:{key}"),
        ],
        [
            InlineKeyboardButton("🔗 Change Callback", callback_data=f"mm:amgr:{menu_id}:callback:{key}"),
        ],
        [
            InlineKeyboardButton("⬆️ Move Up",         callback_data=f"mm:amgr:{menu_id}:moveup:{key}"),
            InlineKeyboardButton("⬇️ Move Down",       callback_data=f"mm:amgr:{menu_id}:movedown:{key}"),
        ],
        [
            InlineKeyboardButton(
                f"{dot} Change Color",
                callback_data=f"mm:amgr:{menu_id}:style:{key}",
            ),
        ],
        [
            InlineKeyboardButton("🎲 Random Color", callback_data=f"mm:amgr:{menu_id}:random:{key}"),
            InlineKeyboardButton("🔄 Cycle Colors", callback_data=f"mm:amgr:{menu_id}:cycle:{key}"),
        ],
        [
            InlineKeyboardButton("♻ Reset Button Color", callback_data=f"mm:amgr:{menu_id}:resetcolor:{key}"),
        ],
        [
            InlineKeyboardButton(
                f"👥 Audience: {audience_label(audience_val)}",
                callback_data=f"mm:amgr:{menu_id}:audopt:{key}",
            ),
        ],
        [
            InlineKeyboardButton("🗑 Delete Item", callback_data=f"mm:amgr:{menu_id}:delconfirm:{key}"),
        ],
        [InlineKeyboardButton(f"⬅️ {html.escape(menu_label)}", callback_data=f"mm:amgr:{menu_id}")],
    ]

    text = (
        f"📋 <b>{html.escape(label_val)}</b>\n\n"
        f"Menu: <b>{html.escape(menu_label)}</b>\n"
        f"Key: <code>{html.escape(key)}</code>\n"
        f"Emoji: <b>{html.escape(emoji_val) or '—'}</b>\n"
        f"Callback: <code>{html.escape(callback_val) or '—'}</code>\n"
        f"Visibility: <b>{'Shown' if visible else 'Hidden'}</b>\n"
        f"Action: <b>{'Enabled' if enabled else 'Disabled'}</b>\n"
        f"Audience: <b>{audience_label(audience_val)}</b>\n"
        f"Color: <b>{html.escape(style)}</b>\n"
        f"Position: row {item.get('row', 0)}, order {item.get('order', 0)}\n\n"
        "<i>Changes apply immediately and persist across restarts.</i>"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


# ── Toggle visibility/enabled ────────────────────────────────────────────────

async def mm_amgr_toggle_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    new_val = not is_item_visible(item)
    items = _amgr_items(menu_id)
    for i in items:
        if i["key"] == key:
            i["visible"] = new_val
    _amgr_save_items(menu_id, items)
    await query.answer(f"✅ {_amgr_item_label(item)} is now {'shown' if new_val else 'hidden'}.")
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


async def mm_amgr_toggle_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    new_val = not is_item_enabled(item)
    items = _amgr_items(menu_id)
    for i in items:
        if i["key"] == key:
            i["enabled"] = new_val
    _amgr_save_items(menu_id, items)
    await query.answer(f"✅ {_amgr_item_label(item)} is now {'enabled' if new_val else 'disabled'}.")
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


# ── Move item ────────────────────────────────────────────────────────────────

async def mm_amgr_move_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, action, key = _amgr_parse(query.data or "")
    direction = "up" if action == "moveup" else "down"
    items = copy.deepcopy(_amgr_items(menu_id))
    if _amgr_reorder(items, direction, key):
        _amgr_save_items(menu_id, items)
        await query.answer("✅ Order updated.")
    else:
        await query.answer("↔️ Already at the edge.", show_alert=False)
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


# ── Color handlers ───────────────────────────────────────────────────────────

async def mm_amgr_cycle_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show color picker for an admin menu item."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    current = normalize_color(
        item.get("style"),
        fallback=default_color_for_button(item.get("callback") or key, item.get("label", "")),
    )
    await query.answer()
    rows = amgr_color_picker_rows(menu_id, key, current)
    rows.extend([
        [
            InlineKeyboardButton("🎲 Random Color", callback_data=f"mm:amgr:{menu_id}:random:{key}"),
            InlineKeyboardButton("🔄 Cycle Colors", callback_data=f"mm:amgr:{menu_id}:cycle:{key}"),
        ],
        [InlineKeyboardButton("♻ Reset Button Color", callback_data=f"mm:amgr:{menu_id}:resetcolor:{key}")],
        [InlineKeyboardButton("⬅️ Button Settings", callback_data=f"mm:amgr:{menu_id}:item:{key}")],
    ])
    await safe_edit(
        query,
        f"🎨 <b>Change Color</b>\n\n"
        f"<b>{html.escape(_amgr_item_label(item))}</b>\n"
        f"Current: <b>{html.escape(color_label(current))}</b>\n\n"
        "Choose a color. The menu updates immediately.",
        InlineKeyboardMarkup(rows),
    )


async def mm_amgr_set_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":", 5)
    if len(parts) != 6 or parts[0:2] != ["mm", "amgr"] or parts[3] != "color":
        await query.answer("❌ Invalid color.", show_alert=True)
        return
    menu_id, key = parts[2], parts[4]
    selected = normalize_color(parts[5], fallback="")
    item = _amgr_item(menu_id, key)
    if not item or not selected:
        await query.answer("❌ Unknown color or menu item.", show_alert=True)
        return
    items = _amgr_items(menu_id)
    for candidate in items:
        if candidate.get("key") == key:
            candidate["style"] = selected
    _amgr_save_items(menu_id, items)
    await query.answer(f"{color_emoji(selected)} {color_label(selected)} applied.", show_alert=False)
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


async def mm_amgr_color_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply random, cycle, or per-button reset in an admin menu."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, action, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    current = normalize_color(
        item.get("style"),
        fallback=default_color_for_button(item.get("callback") or key, item.get("label", "")),
    )
    selected = (
        random_color() if action == "random"
        else cycle_color(current) if action == "cycle"
        else default_color_for_button(item.get("callback") or key, item.get("label", ""))
    )
    items = _amgr_items(menu_id)
    for candidate in items:
        if candidate.get("key") == key:
            candidate["style"] = selected
    _amgr_save_items(menu_id, items)
    await query.answer(f"{color_emoji(selected)} {color_label(selected)} applied.", show_alert=False)
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


# ── Live preview ─────────────────────────────────────────────────────────────

async def mm_amgr_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a simulated preview of the selected admin menu as a new message."""
    from datetime import datetime, timezone
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, _ = _amgr_parse(query.data or "")
    items = _amgr_items(menu_id)
    menu_label = _AMGR_MENU_LABELS.get(menu_id, menu_id)
    await query.answer("📺 Preview sent below.", show_alert=False)

    # Render through the exact same function real admin submenus use
    # with simulate=True so preview can never drift from the actual menu.
    from utils.menu_builder import get_menu_keyboard
    kbd = get_menu_keyboard(menu_id, audience="admin", simulate=True)
    if not kbd.inline_keyboard:
        kbd = InlineKeyboardMarkup([[InlineKeyboardButton(
            "(no visible items)", callback_data="mm:noop",
        )]])

    summary = "\n".join(
        f"  {'👁' if is_item_visible(i) else '🚫'} "
        f"{'✅' if is_item_enabled(i) else '⏸'} "
        f"{_amgr_item_label(i)}"
        for i in items
    )
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    text = (
        f"📺 <b>Preview: {html.escape(menu_label)}</b>\n"
        f"<i>Simulates how this menu renders right now.</i>\n\n"
        "<b>Items (👁=shown 🚫=hidden ✅=active ⏸=disabled):</b>\n"
        + summary
        + f"\n\n<i>Updated: {ts}</i>"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=kbd,
        parse_mode="HTML",
    )


# ── Reset confirmation + execute ─────────────────────────────────────────────

async def mm_amgr_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, _ = _amgr_parse(query.data or "")
    menu_label = _AMGR_MENU_LABELS.get(menu_id, menu_id)
    await query.answer()
    text = (
        f"⚠️ <b>Reset «{html.escape(menu_label)}» to Default?</b>\n\n"
        "This will restore the factory item list:\n"
        "• All items shown and enabled\n"
        "• Default row / order restored\n"
        "• Custom labels, emojis, and colors wiped\n\n"
        "This cannot be undone."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Reset", callback_data=f"mm:amgr:{menu_id}:reset:confirm"),
            InlineKeyboardButton("⬅️ Back",     callback_data=f"mm:amgr:{menu_id}"),
        ],
    ])
    await safe_edit(query, text, kb)


async def mm_amgr_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = (query.data or "").removeprefix("mm:amgr:")
    menu_id = data.removesuffix(":reset:confirm")
    try:
        from utils.menu_builder import reset_menu
        reset_menu(menu_id)
    except Exception:
        await query.answer("❌ Reset failed.", show_alert=True)
        return
    await query.answer("🔄 Menu reset to factory defaults.", show_alert=True)
    query.data = f"mm:amgr:{menu_id}"
    await mm_amgr_menu(update, context)


# ── Audience submenu ─────────────────────────────────────────────────────────

async def mm_amgr_audopt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    await query.answer()
    current = str(item.get("audience", "admin" if item.get("admin_only") else "all"))
    audience_choices = [
        ("all",     "🌐 Everyone"),
        ("user",    "👤 User Only"),
        ("premium", "⭐ Premium Only"),
        ("admin",   "🛡 Admin Only"),
    ]
    rows: List[List[InlineKeyboardButton]] = []
    for val, lbl in audience_choices:
        marker = "✅ " if val == current else ""
        rows.append([InlineKeyboardButton(
            f"{marker}{lbl}",
            callback_data=f"mm:amgr:{menu_id}:audience:{key}:{val}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Back to Item", callback_data=f"mm:amgr:{menu_id}:item:{key}")])
    menu_label = _amgr_menu_label(menu_id)
    await safe_edit(
        query,
        f"👥 <b>Audience — {html.escape(_amgr_item_label(item))}</b>\n\n"
        f"Menu: <b>{html.escape(menu_label)}</b>\n"
        f"Current: <b>{audience_label(current)}</b>\n\n"
        "Select who can see this menu item.",
        InlineKeyboardMarkup(rows),
    )


async def mm_amgr_set_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, rest = _amgr_parse(query.data or "")
    if ":" not in rest:
        await query.answer("❌ Invalid audience data.", show_alert=True)
        return
    key, audience_val = rest.rsplit(":", 1)
    if audience_val not in {"all", "user", "premium", "admin"}:
        await query.answer("❌ Invalid audience.", show_alert=True)
        return
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    items = _amgr_items(menu_id)
    for i in items:
        if i["key"] == key:
            i["audience"] = audience_val
            i.pop("admin_only", None)
    _amgr_save_items(menu_id, items)
    await query.answer(f"✅ Audience set to {audience_label(audience_val)}.")
    query.data = f"mm:amgr:{menu_id}:item:{key}"
    await mm_amgr_item_detail(update, context)


# ── Delete item ──────────────────────────────────────────────────────────────

async def mm_amgr_delconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    await query.answer()
    text = (
        f"⚠️ <b>Delete «{html.escape(_amgr_item_label(item))}»?</b>\n\n"
        "This will permanently remove this item from the menu.\n"
        "Built-in items can be recovered by resetting the menu to defaults.\n\n"
        "This cannot be undone for custom-added items."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"mm:amgr:{menu_id}:delitem:{key}"),
            InlineKeyboardButton("⬅️ Back",      callback_data=f"mm:amgr:{menu_id}:item:{key}"),
        ]
    ])
    await safe_edit(query, text, kb)


async def mm_amgr_delitem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, _, key = _amgr_parse(query.data or "")
    items = _amgr_items(menu_id)
    item = next((i for i in items if i["key"] == key), None)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return
    label = _amgr_item_label(item)
    new_items = [i for i in items if i["key"] != key]
    _amgr_save_items(menu_id, new_items)
    await query.answer(f"🗑 Deleted: {label}", show_alert=True)
    query.data = f"mm:amgr:{menu_id}"
    await mm_amgr_menu(update, context)


# ── Delete menu ──────────────────────────────────────────────────────────────

async def mm_amgr_deletemenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    menu_id, action, key = _amgr_parse(query.data or "")
    if key == "confirm":
        await mm_amgr_deletemenu_confirm(update, context)
        return
    menu_label = _amgr_menu_label(menu_id)
    custom_ids = {e["id"] for e in _load_custom_menus()}
    is_custom = menu_id in custom_ids
    await query.answer()
    if is_custom:
        text = (
            f"⚠️ <b>Delete menu «{html.escape(menu_label)}»?</b>\n\n"
            "This will remove the entire menu and all its items from the system.\n"
            "<b>This cannot be undone.</b>"
        )
    else:
        text = (
            f"⚠️ <b>Reset/clear «{html.escape(menu_label)}»?</b>\n\n"
            "Built-in menus cannot be fully deleted but their items can be reset "
            "to factory defaults.\n"
            "Tap Reset to restore all items, or cancel."
        )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Yes, Delete" if is_custom else "🔄 Reset to Default",
                callback_data=f"mm:amgr:{menu_id}:deletemenu:confirm",
            ),
            InlineKeyboardButton("⬅️ Back", callback_data=f"mm:amgr:{menu_id}"),
        ]
    ])
    await safe_edit(query, text, kb)


async def mm_amgr_deletemenu_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = (query.data or "").removeprefix("mm:amgr:")
    menu_id = data.removesuffix(":deletemenu:confirm")
    custom_menus = _load_custom_menus()
    custom_ids = {e["id"] for e in custom_menus}
    if menu_id in custom_ids:
        try:
            from utils.menu_builder import save_menu
            save_menu(menu_id, [])
        except Exception:
            pass
        new_list = [e for e in custom_menus if e["id"] != menu_id]
        _save_custom_menus(new_list)
        await query.answer("🗑 Menu deleted.", show_alert=True)
    else:
        try:
            from utils.menu_builder import reset_menu
            reset_menu(menu_id)
        except Exception:
            pass
        await query.answer("🔄 Menu reset to factory defaults.", show_alert=True)
    query.data = "mm:amgr"
    await mm_amgr_list(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation-based text input (rename / emoji / callback / add item / create menu)
# ─────────────────────────────────────────────────────────────────────────────

_AMGR_STATE_TEXT = 1
_AMGR_MODES_SINGLE = {"rename", "emoji", "callback"}
_AMGR_WIZARD_FIELDS_ADDITEM = ("key", "label", "emoji", "callback", "audience", "row")
_AMGR_WIZARD_FIELDS_CREATEMENU = ("menu_id", "description")


def _amgr_conv_prompt(context: ContextTypes.DEFAULT_TYPE) -> str:
    mode = context.user_data.get("amgr_mode", "")
    step = context.user_data.get("amgr_step", 0)
    draft = context.user_data.get("amgr_draft", {})

    if mode == "rename":
        item_label = context.user_data.get("amgr_item_label", "")
        return (
            f"✏️ <b>Rename</b>\n\n"
            f"Send the new name for <b>{html.escape(item_label)}</b>.\n"
            "Max 64 characters.\n\nSend /cancel to leave it unchanged."
        )
    if mode == "emoji":
        item_label = context.user_data.get("amgr_item_label", "")
        return (
            f"😀 <b>Change Emoji</b>\n\n"
            f"Send one emoji for <b>{html.escape(item_label)}</b>, or /clear to remove it.\n\n"
            "Send /cancel to leave it unchanged."
        )
    if mode == "callback":
        item_label = context.user_data.get("amgr_item_label", "")
        return (
            f"🔗 <b>Change Callback</b>\n\n"
            f"Send the new callback_data for <b>{html.escape(item_label)}</b>.\n"
            "Max 64 bytes. The callback must already be handled by the bot.\n\n"
            "Send /cancel to leave it unchanged."
        )
    if mode == "additem":
        fields = _AMGR_WIZARD_FIELDS_ADDITEM
        field = fields[step] if step < len(fields) else "done"
        prompts = {
            "key": (
                "🔑 <b>Item Key</b>\n\nSend a short unique identifier (letters, digits, underscores).\n"
                "Example: <code>wishlist</code>\n\n/cancel to abort."
            ),
            "label": (
                "🏷 <b>Button Label</b>\n\nSend the text shown on the button.\n"
                "Example: <code>❤️ Wishlist</code>\n\n/cancel to abort."
            ),
            "emoji": (
                "😀 <b>Emoji (optional)</b>\n\nSend one emoji, or /skip to use none.\n\n/cancel to abort."
            ),
            "callback": (
                "🔗 <b>Callback Data</b>\n\nSend the callback_data string this button should trigger.\n"
                "Must be handled by the bot (max 64 bytes).\n"
                "Example: <code>wishlist</code>\n\n/cancel to abort."
            ),
            "audience": (
                "👥 <b>Audience</b>\n\nWho should see this button? Send one of:\n"
                "• <code>all</code> — everyone\n• <code>user</code> — regular users only\n"
                "• <code>premium</code> — premium users only\n• <code>admin</code> — admins only\n\n"
                "/cancel to abort."
            ),
            "row": (
                "📐 <b>Row Number</b>\n\nWhich row should this button appear on? "
                "Send a positive integer.\nOr /skip to place it on its own last row.\n\n/cancel to abort."
            ),
        }
        return prompts.get(field, "Send your value or /cancel.")

    if mode == "createmenu":
        fields = _AMGR_WIZARD_FIELDS_CREATEMENU
        field = fields[step] if step < len(fields) else "done"
        prompts = {
            "menu_id": (
                "🔑 <b>Menu ID</b>\n\nSend a unique menu identifier (letters, digits, underscores).\n"
                "Example: <code>wishlist_menu</code>\n\n/cancel to abort."
            ),
            "description": (
                "📝 <b>Menu Description</b>\n\nSend a short human-readable description.\n"
                "Example: <code>Wishlist module menu</code>\n\n/cancel to abort."
            ),
        }
        return prompts.get(field, "Send your value or /cancel.")
    return "Send your value or /cancel."


async def amgr_conv_start_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.update({
        "amgr_mode": "rename", "amgr_menu_id": menu_id,
        "amgr_item_key": key, "amgr_item_label": _amgr_item_label(item),
    })
    await query.edit_message_text(_amgr_conv_prompt(context), parse_mode="HTML")
    return _AMGR_STATE_TEXT


async def amgr_conv_start_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.update({
        "amgr_mode": "emoji", "amgr_menu_id": menu_id,
        "amgr_item_key": key, "amgr_item_label": _amgr_item_label(item),
    })
    await query.edit_message_text(_amgr_conv_prompt(context), parse_mode="HTML")
    return _AMGR_STATE_TEXT


async def amgr_conv_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    menu_id, _, key = _amgr_parse(query.data or "")
    item = _amgr_item(menu_id, key)
    if not item:
        await query.answer("❌ Item not found.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.update({
        "amgr_mode": "callback", "amgr_menu_id": menu_id,
        "amgr_item_key": key, "amgr_item_label": _amgr_item_label(item),
    })
    await query.edit_message_text(_amgr_conv_prompt(context), parse_mode="HTML")
    return _AMGR_STATE_TEXT


async def amgr_conv_start_additem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    menu_id, _, _ = _amgr_parse(query.data or "")
    if not menu_id:
        await query.answer("❌ Invalid menu.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.update({
        "amgr_mode": "additem", "amgr_menu_id": menu_id,
        "amgr_step": 0, "amgr_draft": {},
    })
    await query.edit_message_text(_amgr_conv_prompt(context), parse_mode="HTML")
    return _AMGR_STATE_TEXT


async def amgr_conv_start_createmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.update({
        "amgr_mode": "createmenu", "amgr_step": 0, "amgr_draft": {},
    })
    await query.edit_message_text(_amgr_conv_prompt(context), parse_mode="HTML")
    return _AMGR_STATE_TEXT


def _amgr_conv_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("amgr_mode", "amgr_menu_id", "amgr_item_key", "amgr_item_label",
               "amgr_step", "amgr_draft"):
        context.user_data.pop(k, None)


async def amgr_conv_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle text replies for all amgr conversation modes."""
    if not is_admin_user(update):
        return ConversationHandler.END

    mode = context.user_data.get("amgr_mode", "")
    text = (update.message.text or "").strip()

    # ─── Single-step modes ───────────────────────────────────────────────────
    if mode in _AMGR_MODES_SINGLE:
        menu_id = context.user_data.get("amgr_menu_id", "")
        key = context.user_data.get("amgr_item_key", "")
        item = _amgr_item(menu_id, key)
        if not item:
            await update.message.reply_text("❌ Item no longer exists.")
            _amgr_conv_clear(context)
            return ConversationHandler.END

        if mode == "rename":
            if len(text) > 64:
                await update.message.reply_text("❌ Name must be 64 characters or fewer. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            items = _amgr_items(menu_id)
            for i in items:
                if i["key"] == key:
                    i["label"] = text
            _amgr_save_items(menu_id, items)
            await update.message.reply_text(f"✅ Renamed to <b>{html.escape(text)}</b>.", parse_mode="HTML")

        elif mode == "emoji":
            if len(text) > 16 or any(c.isalnum() for c in text):
                await update.message.reply_text("❌ Send an emoji only, /clear to remove, or /cancel.")
                return _AMGR_STATE_TEXT
            items = _amgr_items(menu_id)
            for i in items:
                if i["key"] == key:
                    i["emoji"] = text
            _amgr_save_items(menu_id, items)
            await update.message.reply_text(f"✅ Emoji set to {text}.")

        elif mode == "callback":
            if len(text.encode("utf-8")) > 64:
                await update.message.reply_text("❌ Callback data must be 64 bytes or fewer. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            items = _amgr_items(menu_id)
            for i in items:
                if i["key"] == key:
                    i["callback"] = text
            _amgr_save_items(menu_id, items)
            await update.message.reply_text(
                f"✅ Callback set to <code>{html.escape(text)}</code>.", parse_mode="HTML"
            )

        _amgr_conv_clear(context)
        return ConversationHandler.END

    # ─── Add Item wizard ──────────────────────────────────────────────────────
    if mode == "additem":
        step = context.user_data.get("amgr_step", 0)
        draft = context.user_data.get("amgr_draft", {})
        fields = _AMGR_WIZARD_FIELDS_ADDITEM
        field = fields[step] if step < len(fields) else "done"

        if field == "key":
            if not re.fullmatch(r"[A-Za-z0-9_]+", text):
                await update.message.reply_text("❌ Key must contain only letters, digits, and underscores. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            menu_id = context.user_data.get("amgr_menu_id", "")
            existing = {i["key"] for i in _amgr_items(menu_id)}
            if text in existing:
                await update.message.reply_text(
                    f"❌ Key <code>{html.escape(text)}</code> already exists. Send a different key or /cancel.",
                    parse_mode="HTML",
                )
                return _AMGR_STATE_TEXT
            draft["key"] = text
        elif field == "label":
            if not text or len(text) > 64:
                await update.message.reply_text("❌ Label must be 1–64 characters. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            draft["label"] = text
        elif field == "emoji":
            if len(text) > 16 or any(c.isalnum() for c in text):
                await update.message.reply_text("❌ Send an emoji only or /skip. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            draft["emoji"] = text
        elif field == "callback":
            if len(text.encode("utf-8")) > 64:
                await update.message.reply_text("❌ Max 64 bytes. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            draft["callback"] = text
        elif field == "audience":
            if text.lower() not in {"all", "user", "premium", "admin"}:
                await update.message.reply_text("❌ Send: all, user, premium, or admin.")
                return _AMGR_STATE_TEXT
            draft["audience"] = text.lower()
        elif field == "row":
            try:
                draft["row"] = max(1, int(text))
            except ValueError:
                await update.message.reply_text("❌ Send a positive integer or /skip.")
                return _AMGR_STATE_TEXT

        context.user_data["amgr_draft"] = draft
        step += 1
        context.user_data["amgr_step"] = step

        if step < len(fields):
            await update.message.reply_text(_amgr_conv_prompt(context), parse_mode="HTML")
            return _AMGR_STATE_TEXT

        # Wizard complete — persist the new item
        menu_id = context.user_data.get("amgr_menu_id", "")
        items = _amgr_items(menu_id)
        max_row = max((i.get("row", 0) for i in items), default=0) + 1
        new_item: dict = {
            "key":       draft.get("key", "custom_item"),
            "label":     draft.get("label", "New Item"),
            "emoji":     draft.get("emoji", ""),
            "callback":  draft.get("callback", ""),
            "audience":  draft.get("audience", "all"),
            "row":       draft.get("row", max_row),
            "order":     len(items),
            "visible":   True,
            "enabled":   True,
            "full_width": True,
        }
        if not new_item["emoji"]:
            new_item.pop("emoji")
        items.append(new_item)
        _amgr_save_items(menu_id, items)
        menu_label = _amgr_menu_label(menu_id)
        await update.message.reply_text(
            f"✅ <b>{html.escape(new_item['label'])}</b> added to "
            f"<b>{html.escape(menu_label)}</b>!\n\n"
            "The button is now live. Tap it to adjust visibility, color, "
            "audience, or position.",
            parse_mode="HTML",
        )
        _amgr_conv_clear(context)
        return ConversationHandler.END

    # ─── Create Menu wizard ───────────────────────────────────────────────────
    if mode == "createmenu":
        step = context.user_data.get("amgr_step", 0)
        draft = context.user_data.get("amgr_draft", {})
        fields = _AMGR_WIZARD_FIELDS_CREATEMENU
        field = fields[step] if step < len(fields) else "done"

        if field == "menu_id":
            if not re.fullmatch(r"[A-Za-z0-9_]+", text):
                await update.message.reply_text("❌ ID must contain only letters, digits, and underscores. Try again or /cancel.")
                return _AMGR_STATE_TEXT
            if text in _amgr_all_menu_ids():
                await update.message.reply_text(
                    f"❌ Menu ID <code>{html.escape(text)}</code> already exists. "
                    "Send a different ID or /cancel.",
                    parse_mode="HTML",
                )
                return _AMGR_STATE_TEXT
            draft["menu_id"] = text
        elif field == "description":
            if len(text) > 128:
                await update.message.reply_text("❌ Description must be 128 characters or fewer.")
                return _AMGR_STATE_TEXT
            draft["description"] = text

        context.user_data["amgr_draft"] = draft
        step += 1
        context.user_data["amgr_step"] = step

        if step < len(fields):
            await update.message.reply_text(_amgr_conv_prompt(context), parse_mode="HTML")
            return _AMGR_STATE_TEXT

        # Wizard complete — register the new menu
        new_menu_id = draft["menu_id"]
        description = draft.get("description", "")
        try:
            from utils.menu_builder import register_menu, save_menu
            register_menu(new_menu_id, [], description=description)
            save_menu(new_menu_id, [])
        except Exception:
            logger.warning("amgr: failed to register custom menu %s", new_menu_id, exc_info=True)

        custom_menus = _load_custom_menus()
        custom_menus.append({"id": new_menu_id, "description": description})
        _save_custom_menus(custom_menus)
        _AMGR_MENU_LABELS[new_menu_id] = f"🔧 {description}" if description else f"🔧 {new_menu_id}"

        await update.message.reply_text(
            f"✅ Menu <code>{html.escape(new_menu_id)}</code> created!\n\n"
            f"Description: <b>{html.escape(description)}</b>\n\n"
            "Go to Admin Menu Builder → select your new menu → ➕ Add Item "
            "to add buttons for your module.",
            parse_mode="HTML",
        )
        _amgr_conv_clear(context)
        return ConversationHandler.END

    _amgr_conv_clear(context)
    return ConversationHandler.END


async def amgr_conv_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    mode = context.user_data.get("amgr_mode", "")
    if mode == "additem":
        step = context.user_data.get("amgr_step", 0)
        fields = _AMGR_WIZARD_FIELDS_ADDITEM
        field = fields[step] if step < len(fields) else "done"
        draft = context.user_data.get("amgr_draft", {})
        if field in ("emoji", "row"):
            if field == "emoji":
                draft["emoji"] = ""
            context.user_data["amgr_draft"] = draft
            context.user_data["amgr_step"] = step + 1
            await update.message.reply_text(_amgr_conv_prompt(context), parse_mode="HTML")
            return _AMGR_STATE_TEXT
    if mode == "emoji":
        menu_id = context.user_data.get("amgr_menu_id", "")
        key = context.user_data.get("amgr_item_key", "")
        items = _amgr_items(menu_id)
        for i in items:
            if i["key"] == key:
                i["emoji"] = ""
        _amgr_save_items(menu_id, items)
        await update.message.reply_text("✅ Emoji removed.")
        _amgr_conv_clear(context)
        return ConversationHandler.END
    await update.message.reply_text("↩️ /skip not applicable here. Send your value or /cancel.")
    return _AMGR_STATE_TEXT


async def amgr_conv_clear_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    mode = context.user_data.get("amgr_mode", "")
    if mode == "emoji":
        menu_id = context.user_data.get("amgr_menu_id", "")
        key = context.user_data.get("amgr_item_key", "")
        items = _amgr_items(menu_id)
        for i in items:
            if i["key"] == key:
                i["emoji"] = ""
        _amgr_save_items(menu_id, items)
        await update.message.reply_text("✅ Emoji cleared.")
        _amgr_conv_clear(context)
        return ConversationHandler.END
    await update.message.reply_text("↩️ /clear not applicable here. Send your value or /cancel.")
    return _AMGR_STATE_TEXT


async def amgr_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _amgr_conv_clear(context)
    await update.message.reply_text("↩️ Operation cancelled.")
    return ConversationHandler.END


def build_amgr_conversation() -> ConversationHandler:
    """Build the ConversationHandler for all amgr text-input flows."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(amgr_conv_start_rename,     pattern=r"^mm:amgr:.+:rename:.+$"),
            CallbackQueryHandler(amgr_conv_start_emoji,      pattern=r"^mm:amgr:.+:emoji:.+$"),
            CallbackQueryHandler(amgr_conv_start_callback,   pattern=r"^mm:amgr:.+:callback:.+$"),
            CallbackQueryHandler(amgr_conv_start_additem,    pattern=r"^mm:amgr:.+:additem$"),
            CallbackQueryHandler(amgr_conv_start_createmenu, pattern=r"^mm:amgr:createmenu$"),
        ],
        states={
            _AMGR_STATE_TEXT: [
                CommandHandler("skip",  amgr_conv_skip),
                CommandHandler("clear", amgr_conv_clear_field),
                MessageHandler(filters.TEXT & ~filters.COMMAND, amgr_conv_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", amgr_conv_cancel)],
        allow_reentry=True,
    )
