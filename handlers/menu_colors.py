"""Menu Colors — all color management for the Admin Menu Manager.

Centralises color picking, toggling, resetting, cycling, and random
assignment for both the user main menu and the admin sub-menus.

No rendering (button markup) and no preview logic lives here.
"""

from __future__ import annotations

import logging
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.button_colors import (
    COLOR_KEYS,
    COLOR_LABELS,
    DEFAULT_MENU_ITEM_COLORS,
    color_emoji,
    color_label,
    cycle_color,
    default_color_for_button,
    get_button_color,
    normalize_color,
    random_color,
    reset_all_button_colors,
    set_button_color,
)
from utils.menu_registry import (
    MENU_AUDIENCES,
    get_menu_layout,
    save_menu_layout,
)

from handlers.menu_state import (
    active_audience,
    get_item,
    get_items,
    is_admin_user,
    item_display_name,
    safe_edit,
    update_item,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared color constants
# ─────────────────────────────────────────────────────────────────────────────

# One dot per palette color, derived from the central button_colors table.
STYLE_DOTS: dict[str, str] = {key: color_emoji(key) for key in COLOR_KEYS}

# The single canonical defaults table — same source used by keyboards.py so
# the admin UI and the real menu can never show different defaults.
DEFAULT_ITEM_STYLE: dict[str, str] = DEFAULT_MENU_ITEM_COLORS


# ─────────────────────────────────────────────────────────────────────────────
# Item style accessors
# ─────────────────────────────────────────────────────────────────────────────

def get_item_style(key: str, context: ContextTypes.DEFAULT_TYPE = None) -> str:
    """Return the resolved color/style name for a main-menu item."""
    item = get_item(key, context)
    if item and item.get("style"):
        return normalize_color(item["style"], fallback=DEFAULT_ITEM_STYLE.get(key, "blue"))
    return DEFAULT_ITEM_STYLE.get(key, "none")


# ─────────────────────────────────────────────────────────────────────────────
# Shared color-picker row builder
# ─────────────────────────────────────────────────────────────────────────────

def color_picker_rows(prefix: str, current: str) -> list[list[InlineKeyboardButton]]:
    """Build a complete inline color-picker grid for any managed button."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key in COLOR_KEYS:
        marker = "✅ " if key == normalize_color(current) else ""
        row.append(InlineKeyboardButton(
            f"{marker}{COLOR_LABELS[key]}",
            callback_data=f"{prefix}:{key}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def amgr_color_picker_rows(
    menu_id: str, key: str, current: str
) -> list[list[InlineKeyboardButton]]:
    """Color-picker rows scoped to an admin-menu item."""
    return color_picker_rows(f"mm:amgr:{menu_id}:color:{key}", current)


# ─────────────────────────────────────────────────────────────────────────────
# Global color toggle handlers
# ─────────────────────────────────────────────────────────────────────────────

async def mm_toggle_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instantly hide/restore ALL main-menu button colors (per-item settings kept)."""
    from utils.bot_config import cfg
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    aud = active_audience(context)
    layout = get_menu_layout(aud)
    current = bool(layout.get("colors_enabled", True))
    layout["colors_enabled"] = not current
    save_menu_layout(aud, layout)
    await query.answer(
        "🚫 Colors turned OFF (buttons will show default color)." if current
        else "✅ Colors turned back ON.",
        show_alert=False,
    )
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


async def mm_toggle_all_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instantly enable/disable colored buttons across the ENTIRE bot."""
    from utils.bot_config import cfg
    from utils.button_colors import global_colors_enabled
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    current = global_colors_enabled()
    cfg.set("global_button_colors_enabled", not current)
    await query.answer(
        "🚫 All bot buttons turned OFF (default color everywhere)." if current
        else "✅ All bot buttons colored back ON.",
        show_alert=False,
    )
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


async def mm_reset_all_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset color metadata across all managed profiles and registered menus."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    reset_all_button_colors()

    # Main-menu profiles: only style/color fields are changed.
    for audience in MENU_AUDIENCES:
        layout = get_menu_layout(audience)
        for item in layout.get("items", []):
            if isinstance(item, dict):
                item["style"] = DEFAULT_ITEM_STYLE.get(item.get("key"), "blue")
        for button in layout.get("custom_buttons", []):
            if isinstance(button, dict):
                button["color"] = "white"
                button.pop("style", None)
        save_menu_layout(audience, layout)

    # Registered menus: preserve every field except presentation color.
    try:
        from utils.menu_builder import list_menus, get_menu_items, save_menu
        for menu_id in list_menus():
            items = get_menu_items(menu_id)
            for item in items:
                if isinstance(item, dict):
                    item["style"] = default_color_for_button(
                        item.get("callback") or item.get("key"),
                        item.get("label", ""),
                    )
            save_menu(menu_id, items)
    except Exception:
        logger.debug("Could not reset registered menu colors", exc_info=True)

    await query.answer("♻ All button colors reset.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


async def mm_reset_colors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset button colors to their recommended defaults, for every profile
    (Users, Premium, Admins) at once -- same reasoning as the Layout reset:
    resetting only the currently-open profile is what let "Admins" silently
    keep stale colors after "Users" was reset."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    for aud in MENU_AUDIENCES:
        layout = get_menu_layout(aud)
        for item in layout.get("items", []):
            if isinstance(item, dict):
                item["style"] = DEFAULT_ITEM_STYLE.get(item.get("key"), "blue")
        for button in layout.get("custom_buttons", []):
            if isinstance(button, dict):
                button["color"] = "white"
                button.pop("style", None)
        layout["colors_enabled"] = True
        save_menu_layout(aud, layout)
    await query.answer("♻ Button colors reset for all profiles.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Per-item color handlers (main menu)
# ─────────────────────────────────────────────────────────────────────────────

async def mm_color_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all available colors for one main-menu button."""
    import html
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":", 2)
    key = parts[2] if len(parts) == 3 else ""
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return
    current = get_item_style(key, context)
    rows = color_picker_rows(f"mm:color:{key}", current)
    rows.extend([
        [
            InlineKeyboardButton("🎲 Random Color", callback_data=f"mm:random:{key}"),
            InlineKeyboardButton("🔄 Cycle Colors", callback_data=f"mm:cycle:{key}"),
        ],
        [
            InlineKeyboardButton("♻ Reset Button Color", callback_data=f"mm:resetcolor:{key}"),
        ],
        [InlineKeyboardButton("⬅️ Button Settings", callback_data=f"mm:item:{key}")],
    ])
    await query.answer()
    await safe_edit(
        query,
        f"🎨 <b>Change Color</b>\n\n"
        f"<b>{html.escape(item_display_name(item))}</b>\n"
        f"Current: <b>{html.escape(color_label(current))}</b>\n\n"
        "Choose a color. The live preview refreshes immediately.",
        InlineKeyboardMarkup(rows),
    )


async def mm_set_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist one selected main-menu color and refresh the live preview."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("❌ Invalid color.", show_alert=True)
        return
    key, selected = parts[2], normalize_color(parts[3], fallback="")
    if not get_item(key, context) or not selected:
        await query.answer("❌ Unknown color or menu item.", show_alert=True)
        return
    update_item(key, context, style=selected)
    await query.answer(f"{color_emoji(selected)} {color_label(selected)} applied.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.menu_actions import mm_item_detail
    await mm_item_detail(update, context)


async def mm_random_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Assign a random palette color to one main-menu button."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":", 2)
    key = parts[2] if len(parts) == 3 else ""
    if not get_item(key, context):
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return
    selected = random_color()
    update_item(key, context, style=selected)
    await query.answer(f"🎲 {color_label(selected)} applied.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.menu_actions import mm_item_detail
    await mm_item_detail(update, context)


async def mm_cycle_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Advance one main-menu button through the complete palette."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":", 2)
    key = parts[2] if len(parts) == 3 else ""
    item = get_item(key, context)
    if not item:
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return
    selected = cycle_color(get_item_style(key, context))
    update_item(key, context, style=selected)
    await query.answer(f"🔄 {color_label(selected)} applied.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.menu_actions import mm_item_detail
    await mm_item_detail(update, context)


async def mm_reset_button_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset one main-menu button color without affecting other buttons."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = (query.data or "").split(":", 2)
    key = parts[2] if len(parts) == 3 else ""
    if not get_item(key, context):
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return
    update_item(key, context, style=DEFAULT_ITEM_STYLE.get(key, "blue"))
    await query.answer("♻ Button color reset.", show_alert=False)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.menu_actions import mm_item_detail
    await mm_item_detail(update, context)


async def mm_cycle_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backward-compatible alias for the old color callback."""
    await mm_color_picker(update, context)
