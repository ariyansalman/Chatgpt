"""Admin Main Menu Manager — V20.

Callback namespace: mm:*

Provides:
  mm:menu            — Main Menu Manager dashboard
  mm:status:<s>      — Set main menu status (enabled/maintenance/disabled)
  mm:toggle:<item>   — Toggle a menu item on or off
  mm:custom          — Custom buttons submenu (list)
  mm:custom:add      — Prompt to add a custom button label
  mm:custom:del:<n>  — Delete custom button at index n
  mm:custom:clear    — Clear all custom buttons

Integrates with utils.bot_config (mm settings in the 'main_menu' category).
All status changes take immediate effect via the cfg cache invalidation.
"""

from __future__ import annotations

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from utils.bot_config import cfg
from utils.helpers import is_admin

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "enabled":     "🟢 Enabled",
    "maintenance": "🟡 Maintenance",
    "disabled":    "🔴 Disabled",
}

_MENU_ITEMS = [
    ("products", "🛒 Products"),
    ("topup",    "💰 Top Up"),
    ("orders",   "📜 Orders"),
    ("support",  "💬 Support"),
    ("refer",    "🎁 Refer & Earn"),
    ("account",  "👤 Account"),
    ("language", "🌐 Language"),
]

# Order the "Tap to cycle color" button walks through, per item.
_STYLE_CYCLE = ["none", "success", "primary", "danger"]
_STYLE_DOTS = {
    "none": "⚪",
    "success": "🟢",
    "primary": "🔵",
    "danger": "🔴",
}
# Matches the default colors baked into utils/keyboards.py::_DEFAULT_MENU_STYLES
_DEFAULT_ITEM_STYLE = {
    "products": "success",
    "topup": "success",
    "orders": "primary",
    "support": "primary",
    "refer": "success",
    "account": "primary",
    "language": "primary",
    "admin": "danger",
}


def _get_item_style(key: str) -> str:
    return cfg.get_str(f"menu_item_{key}_style", _DEFAULT_ITEM_STYLE.get(key, "none")) or "none"


# ─────────────────────────────────────────────────────────────────────────────
# Safe edit helper
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML",
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Admin guard
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(update: Update) -> bool:
    return is_admin(update.effective_user.id)


# ─────────────────────────────────────────────────────────────────────────────
# Menu Manager Dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def mm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📋 Main Menu Manager — dashboard for controlling the user main menu."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    current_status = cfg.get_str("main_menu_status", "enabled")
    status_label = _STATUS_LABELS.get(current_status, current_status)

    # Status selector row
    rows: List[List[InlineKeyboardButton]] = []

    status_row = []
    for s, lbl in _STATUS_LABELS.items():
        marker = "✅ " if s == current_status else ""
        status_row.append(InlineKeyboardButton(
            f"{marker}{lbl}", callback_data=f"mm:status:{s}"
        ))
    rows.append(status_row)

    # Global color kill switch + reset
    colors_on = cfg.get_bool("main_menu_colors_enabled", True)
    rows.append([
        InlineKeyboardButton(
            f"🎨 Colors: {'✅ ON' if colors_on else '🚫 OFF'}",
            callback_data="mm:colors_toggle"
        ),
        InlineKeyboardButton("🔁 Reset to Default", callback_data="mm:colors_reset"),
    ])

    # Bot-wide color switch -- covers every OTHER keyboard in the bot
    # (products, cart, orders, admin panels, etc.), separate from the
    # main-menu-only switch above.
    all_colors_on = cfg.get_bool("global_button_colors_enabled", True)
    rows.append([
        InlineKeyboardButton(
            f"🌈 All Bot Buttons: {'✅ ON' if all_colors_on else '🚫 OFF'}",
            callback_data="mm:all_colors_toggle"
        ),
    ])

    rows.append([InlineKeyboardButton("━━━ Menu Items ━━━", callback_data="mm:noop")])

    # Toggle rows for each menu item — plus a color-cycle button
    for key, label in _MENU_ITEMS:
        enabled = cfg.get_bool(f"menu_item_{key}_enabled", True)
        toggle_icon = "✅" if enabled else "🚫"
        style = _get_item_style(key)
        dot = _STYLE_DOTS.get(style, "⚪")
        rows.append([
            InlineKeyboardButton(
                f"{toggle_icon} {label}",
                callback_data=f"mm:toggle:{key}"
            ),
            InlineKeyboardButton(
                f"{dot} Color",
                callback_data=f"mm:style:{key}"
            ),
        ])

    rows.append([InlineKeyboardButton("━━━ Custom Buttons ━━━", callback_data="mm:noop")])
    rows.append([InlineKeyboardButton("🔧 Manage Custom Buttons", callback_data="mm:custom")])
    admin_dot = _STYLE_DOTS.get(_get_item_style("admin"), "⚪")
    rows.append([InlineKeyboardButton(f"{admin_dot} 🛠 Admin Panel Color", callback_data="mm:style:admin")])
    rows.append([InlineKeyboardButton("✨ Premium Emoji Icons (help)", callback_data="mm:emoji_help")])
    rows.append([InlineKeyboardButton("⬅️ Admin Menu", callback_data="admin_menu")])

    # Build summary text
    items_status = []
    for key, label in _MENU_ITEMS:
        enabled = cfg.get_bool(f"menu_item_{key}_enabled", True)
        dot = _STYLE_DOTS.get(_get_item_style(key), "⚪")
        items_status.append(f"  {'✅' if enabled else '🚫'} {dot} {label}")

    text = (
        "📋 <b>Main Menu Manager</b>\n\n"
        f"🔹 Status: <b>{status_label}</b>\n"
        f"🎨 Colors: <b>{'ON' if colors_on else 'OFF (showing default color everywhere)'}</b>\n\n"
        "<b>Visible Items:</b>\n"
        + "\n".join(items_status)
        + "\n\n"
        "Tap a status button to switch modes.\n"
        "Tap an item to toggle its visibility.\n"
        "Tap 🎨 <b>Color</b> to cycle its button color "
        "(⚪ default → 🟢 green → 🔵 blue → 🔴 red).\n"
        "Tap <b>🎨 Colors: ON/OFF</b> to instantly hide/restore all colors "
        "without losing your per-item settings.\n"
        "Tap <b>🔁 Reset to Default</b> to wipe your custom color choices "
        "and go back to the recommended scheme.\n"
        "<i>Requires the Telegram app to be updated to support colored "
        "buttons — older clients just show the default color.</i>"
    )

    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Status Toggle
# ─────────────────────────────────────────────────────────────────────────────

async def mm_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set main menu status: enabled | maintenance | disabled."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    new_status = data.split(":", 2)[-1] if ":" in data else "enabled"
    if new_status not in ("enabled", "maintenance", "disabled"):
        await query.answer("❌ Invalid status.", show_alert=True)
        return

    cfg.set("main_menu_status", new_status)
    label = _STATUS_LABELS.get(new_status, new_status)
    await query.answer(f"✅ Main menu set to {label}", show_alert=False)
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Item Toggle
# ─────────────────────────────────────────────────────────────────────────────

async def mm_toggle_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a single menu item on or off."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 3:
        await query.answer("❌ Invalid action.", show_alert=True)
        return

    item_key = parts[2]
    valid_keys = {k for k, _ in _MENU_ITEMS}
    if item_key not in valid_keys:
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return

    cfg_key = f"menu_item_{item_key}_enabled"
    current = cfg.get_bool(cfg_key, True)
    cfg.set(cfg_key, not current)

    label = next((lbl for k, lbl in _MENU_ITEMS if k == item_key), item_key)
    action = "shown" if not current else "hidden"
    await query.answer(f"{label} is now {action}.", show_alert=False)
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Global Colors Switch + Reset
# ─────────────────────────────────────────────────────────────────────────────

async def mm_toggle_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instantly hide/restore ALL main-menu button colors (per-item settings kept)."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    current = cfg.get_bool("main_menu_colors_enabled", True)
    cfg.set("main_menu_colors_enabled", not current)
    await query.answer(
        "🚫 Colors turned OFF (buttons will show default color)." if current
        else "✅ Colors turned back ON.",
        show_alert=False,
    )
    await mm_menu(update, context)


async def mm_toggle_all_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instantly enable/disable colored buttons across the ENTIRE bot
    (every keyboard outside the main menu -- products, cart, orders,
    admin panels, etc). Independent from the main-menu-only switch."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    current = cfg.get_bool("global_button_colors_enabled", True)
    cfg.set("global_button_colors_enabled", not current)
    await query.answer(
        "🚫 All bot buttons turned OFF (default color everywhere)." if current
        else "✅ All bot buttons colored back ON.",
        show_alert=False,
    )
    await mm_menu(update, context)


async def mm_reset_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe every per-item color override, reverting to the recommended defaults."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    for key in list(_DEFAULT_ITEM_STYLE.keys()):
        cfg.set(f"menu_item_{key}_style", _DEFAULT_ITEM_STYLE[key])
    cfg.set("main_menu_colors_enabled", True)
    await query.answer("🔁 Colors reset to the recommended defaults.", show_alert=False)
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Color Cycle
# ─────────────────────────────────────────────────────────────────────────────

async def mm_cycle_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle a menu item's button color: none -> success -> primary -> danger -> none."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 3:
        await query.answer("❌ Invalid action.", show_alert=True)
        return

    item_key = parts[2]
    valid_keys = {k for k, _ in _MENU_ITEMS} | {"admin"}
    if item_key not in valid_keys:
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return

    current = _get_item_style(item_key)
    idx = _STYLE_CYCLE.index(current) if current in _STYLE_CYCLE else 0
    new_style = _STYLE_CYCLE[(idx + 1) % len(_STYLE_CYCLE)]
    cfg.set(f"menu_item_{item_key}_style", new_style)

    label = next((lbl for k, lbl in _MENU_ITEMS if k == item_key), "🛠 Admin Panel")
    dot = _STYLE_DOTS.get(new_style, "⚪")
    await query.answer(f"{dot} {label} → {new_style}", show_alert=False)
    await mm_menu(update, context)


async def mm_emoji_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how to attach a Telegram Premium custom emoji icon to a menu button."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    item_list = ", ".join(f"<code>{k}</code>" for k, _ in _MENU_ITEMS)
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
    await _safe_edit(query, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# Custom Buttons
# ─────────────────────────────────────────────────────────────────────────────

def _parse_custom_buttons() -> list[dict]:
    """Return the list of custom buttons stored in bot_config as JSON."""
    import json
    raw = cfg.get_str("main_menu_custom_buttons", "[]")
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items
    except Exception:
        pass
    return []


def _save_custom_buttons(buttons: list[dict]) -> None:
    import json
    cfg.set("main_menu_custom_buttons", json.dumps(buttons, ensure_ascii=False))


async def mm_custom_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📌 Custom Buttons submenu — list and manage custom main menu buttons."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    buttons = _parse_custom_buttons()
    rows: List[List[InlineKeyboardButton]] = []

    if buttons:
        for idx, btn in enumerate(buttons):
            label = btn.get("label", f"Button {idx + 1}")
            cb = btn.get("callback", "main_menu")
            rows.append([
                InlineKeyboardButton(f"🔘 {label} → {cb}", callback_data="mm:noop"),
                InlineKeyboardButton("🗑", callback_data=f"mm:custom:del:{idx}"),
            ])
        rows.append([InlineKeyboardButton("🗑 Clear All", callback_data="mm:custom:clear")])
    else:
        rows.append([InlineKeyboardButton("(no custom buttons)", callback_data="mm:noop")])

    rows.append([InlineKeyboardButton("➕ Add Custom Button", callback_data="mm:custom:add")])
    rows.append([InlineKeyboardButton("⬅️ Menu Manager", callback_data="mm:menu")])

    text = (
        "📌 <b>Custom Menu Buttons</b>\n\n"
        f"You have <b>{len(buttons)}</b> custom button(s).\n\n"
        "Custom buttons appear below the standard menu rows.\n"
        "Tap ➕ to add a new button via the config system."
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def mm_custom_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guide admin to add a custom button via the bot_config editor."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    text = (
        "📌 <b>Add Custom Button</b>\n\n"
        "To add a custom button, edit the <code>main_menu_custom_buttons</code> key "
        "in Admin → Bot Configuration (JSON array).\n\n"
        "Format:\n"
        "<pre>[\n"
        '  {"label": "🛍 Shop Now", "callback": "products", "style": "success"},\n'
        '  {"label": "📢 Channel", "url": "https://t.me/channel", "style": "primary"}\n'
        "]</pre>\n\n"
        "Each entry must have a <b>label</b> and either a <b>callback</b> or <b>url</b>. "
        "<b>style</b> (success/primary/danger) and <b>emoji_id</b> (a custom emoji ID) "
        "are both optional."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Custom Buttons", callback_data="mm:custom")],
    ])
    await _safe_edit(query, text, kb)


async def mm_custom_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a custom button by index."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    try:
        idx = int(parts[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid index.", show_alert=True)
        return

    buttons = _parse_custom_buttons()
    if 0 <= idx < len(buttons):
        removed = buttons.pop(idx)
        _save_custom_buttons(buttons)
        await query.answer(f"🗑 Removed: {removed.get('label', 'button')}", show_alert=False)
    else:
        await query.answer("❌ Button not found.", show_alert=True)
    await mm_custom_menu(update, context)


async def mm_custom_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all custom buttons."""
    query = update.callback_query

    if not _is_admin(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    _save_custom_buttons([])
    await query.answer("✅ All custom buttons cleared.", show_alert=False)
    await mm_custom_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# No-op (page labels)
# ─────────────────────────────────────────────────────────────────────────────

async def mm_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Central dispatcher for all mm:* callbacks
# ─────────────────────────────────────────────────────────────────────────────

async def mm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all mm:* callbacks to the appropriate handler."""
    query = update.callback_query
    data = query.data if query else ""

    if data == "mm:menu":
        await mm_menu(update, context)
    elif data.startswith("mm:status:"):
        await mm_set_status(update, context)
    elif data.startswith("mm:toggle:"):
        await mm_toggle_item(update, context)
    elif data.startswith("mm:style:"):
        await mm_cycle_style(update, context)
    elif data == "mm:colors_toggle":
        await mm_toggle_colors(update, context)
    elif data == "mm:all_colors_toggle":
        await mm_toggle_all_colors(update, context)
    elif data == "mm:colors_reset":
        await mm_reset_colors(update, context)
    elif data == "mm:emoji_help":
        await mm_emoji_help(update, context)
    elif data == "mm:custom":
        await mm_custom_menu(update, context)
    elif data == "mm:custom:add":
        await mm_custom_add(update, context)
    elif data.startswith("mm:custom:del:"):
        await mm_custom_delete(update, context)
    elif data == "mm:custom:clear":
        await mm_custom_clear(update, context)
    elif data == "mm:noop":
        await mm_noop(update, context)
    else:
        await query.answer()
