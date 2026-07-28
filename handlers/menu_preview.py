"""Menu Preview — live preview logic for the Admin Menu Manager.

Centralises preview text/keyboard generation and the auto-refresh helper.
Both the preview and the real menu use the same renderer (keyboards.py)
with simulate=True, ensuring they can never drift apart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from utils.menu_registry import (
    MENU_AUDIENCE_LABELS,
    get_menu_layout,
    is_item_enabled,
    is_item_visible,
)
from handlers.menu_state import (
    STATUS_LABELS,
    active_audience,
    custom_bool,
    get_items,
    is_admin_user,
    item_display_name,
    parse_custom_buttons,
)
from handlers.menu_colors import STYLE_DOTS, get_item_style

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Preview builders
# ─────────────────────────────────────────────────────────────────────────────

def build_preview_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Build the Live Preview keyboard for the current live state.

    Calls the exact same renderer the real Main Menu uses
    (keyboards.create_main_menu_keyboard) with simulate=True — every
    label, color, row, and visibility decision comes from the one real
    renderer so preview and the real menu can never drift apart.
    """
    from utils.keyboards import create_main_menu_keyboard

    markup = create_main_menu_keyboard(
        lang="en",
        audience=active_audience(context),
        simulate=True,
    )
    if markup.inline_keyboard:
        return markup
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "(no visible items)", callback_data="mm:noop",
    )]])


def build_preview_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Build the header text for the live preview message."""
    from utils.bot_config import cfg
    items = get_items(context)
    audience = active_audience(context)
    status = cfg.get_str("main_menu_status", "enabled")
    status_label = STATUS_LABELS.get(status, status)
    colors_on = bool(get_menu_layout(audience).get("colors_enabled", True))
    custom_buttons = parse_custom_buttons(context)

    lines = []
    for item in items:
        key = item["key"]
        visible = is_item_visible(item)
        enabled = is_item_enabled(item)
        style = get_item_style(key, context)
        dot = STYLE_DOTS.get(style, "⚪") if colors_on else "⚪"
        lines.append(
            f"  {'👁' if visible else '🚫'} {'✅' if enabled else '⏸'} "
            f"{dot} {item_display_name(item)}"
        )

    visible_custom = sum(
        1 for b in custom_buttons if custom_bool(b.get("visible", True))
    )
    custom_note = (
        f"\n📌 <b>Custom buttons:</b> {visible_custom} visible"
        if custom_buttons
        else ""
    )

    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    return (
        f"📺 <b>Live Preview</b>\n"
        f"<i>Reflects the menu as it appears to users right now.</i>\n\n"
        f"<b>Profile:</b> {MENU_AUDIENCE_LABELS[audience]}  •  "
        f"<b>Status:</b> {status_label}  •  "
        f"<b>Colors:</b> {'ON 🎨' if colors_on else 'OFF'}\n\n"
        "<b>Items (👁=shown 🚫=hidden ✅=active ⏸=disabled):</b>\n"
        + "\n".join(lines)
        + custom_note
        + f"\n\n<i>Updated: {ts}</i>\n"
        "<i>Keyboard below simulates what users tap.</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Auto-refresh helper
# ─────────────────────────────────────────────────────────────────────────────

async def mm_refresh_preview(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently refresh the live-preview message if one is active this session.

    Called automatically after every configuration change so the preview
    stays in sync without the admin needing to do anything extra.
    Errors are swallowed — a stale preview is acceptable, a crash is not.
    """
    preview_msg_id = context.user_data.get("mm_preview_msg_id")
    preview_chat_id = context.user_data.get("mm_preview_chat_id")
    if not preview_msg_id or not preview_chat_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=preview_chat_id,
            message_id=preview_msg_id,
            text=build_preview_text(context),
            reply_markup=build_preview_keyboard(context),
            parse_mode="HTML",
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.debug("Live preview refresh failed: %s", exc)
            context.user_data.pop("mm_preview_msg_id", None)
            context.user_data.pop("mm_preview_chat_id", None)
    except Exception:
        logger.debug("Live preview refresh failed", exc_info=True)
        context.user_data.pop("mm_preview_msg_id", None)
        context.user_data.pop("mm_preview_chat_id", None)


# ─────────────────────────────────────────────────────────────────────────────
# Preview send handler
# ─────────────────────────────────────────────────────────────────────────────

async def mm_show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send (or re-send) the live preview message below the manager UI.

    The message will be refreshed automatically after every subsequent
    change so the admin never needs to open /start to check the menu.
    """
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer("📺 Preview sent below — it will update live.", show_alert=False)

    # Delete the old preview message if one already exists.
    old_id = context.user_data.get("mm_preview_msg_id")
    old_chat = context.user_data.get("mm_preview_chat_id")
    if old_id and old_chat:
        try:
            await context.bot.delete_message(chat_id=old_chat, message_id=old_id)
        except Exception:
            pass
        context.user_data.pop("mm_preview_msg_id", None)
        context.user_data.pop("mm_preview_chat_id", None)

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_preview_text(context),
        reply_markup=build_preview_keyboard(context),
        parse_mode="HTML",
    )
    context.user_data["mm_preview_msg_id"] = msg.message_id
    context.user_data["mm_preview_chat_id"] = msg.chat_id
