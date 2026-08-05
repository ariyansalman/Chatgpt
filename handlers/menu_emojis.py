"""Menu Emojis — emoji management for the Admin Menu Manager.

Centralises the premium emoji icon help screen and any future
emoji-specific logic. No rendering, preview, or color code here.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.menu_state import is_admin_user, safe_edit, get_items, item_display_name

logger = logging.getLogger(__name__)


async def mm_emoji_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how to attach a Telegram Premium custom emoji icon to a menu button."""
    query = update.callback_query
    await query.answer()

    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    item_list = ", ".join(
        f"<code>{item['key']}</code>" for item in get_items(context)
    )
    text = (
        "✨ <b>Premium Emoji Icons</b>\n\n"
        "Buttons can show a small custom/Premium emoji icon next to the "
        "text (requires the bot owner to have Telegram Premium, or "
        "purchased Fragment usernames).\n\n"
        "<b>Step 1 — Get the emoji's ID:</b>\n"
        "Send the custom emoji to any chat, forward that message to "
        "<b>@RawDataBot</b>, and copy the number under "
        "<code>custom_emoji_id</code>.\n\n"
        "<b>Step 2 — Attach it to a menu button:</b>\n"
        "Go to <b>Admin → Bot Configuration → 📋 Main Menu</b>, open the "
        "setting named <b>✨ &lt;Item&gt;: Premium Emoji ID</b> for the "
        "button you want (e.g. <i>Products: Premium Emoji ID</i>), and "
        "paste the number in as its value. You can also just search "
        "<code>emoji</code> from the Bot Configuration search box to jump "
        "straight to all of them.\n\n"
        f"Covers: {item_list}\n\n"
        "Leave the value empty to remove the icon. If the viewer's "
        "Telegram client is older or the bot owner has no Premium, the "
        "icon is simply hidden — the button still works normally."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Menu Manager", callback_data="mm:menu")],
    ])
    await safe_edit(query, text, kb)
