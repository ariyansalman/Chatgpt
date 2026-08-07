"""Menu Renderer — all rendering helpers for the Admin Menu Manager.

Centralises item detail text/markup, move-slot logic, and item
markup construction. Preview and live-keyboard building stay in
menu_preview.py; color-picker rows stay in menu_colors.py.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.menu_registry import (
    MENU_AUDIENCE_LABELS,
    is_item_enabled,
    is_item_visible,
)
from handlers.menu_colors import STYLE_DOTS, get_item_style
from handlers.menu_state import (
    active_audience,
    item_display_name,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Item detail markup
# ─────────────────────────────────────────────────────────────────────────────

def build_item_markup(
    item: dict, context: ContextTypes.DEFAULT_TYPE
) -> InlineKeyboardMarkup:
    """Build the full control keyboard for one main-menu item."""
    key = item["key"]
    visible = is_item_visible(item)
    enabled = is_item_enabled(item)
    aud = active_audience(context)
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'👁 Hide' if visible else '👁 Show'}",
                callback_data=f"mm:visible:{key}",
            ),
            InlineKeyboardButton(
                f"{'🚫 Disable' if enabled else '✅ Enable'}",
                callback_data=f"mm:enabled:{key}",
            ),
        ],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"mm:editname:{key}"),
            InlineKeyboardButton("😀 Emoji",  callback_data=f"mm:editemoji:{key}"),
        ],
        [
            InlineKeyboardButton("⬆️ Move Up",   callback_data=f"mm:moveup:{key}"),
            InlineKeyboardButton("⬇️ Move Down", callback_data=f"mm:movedown:{key}"),
        ],
        [
            InlineKeyboardButton(
                f"{STYLE_DOTS.get(get_item_style(key, context), '⚪')} Change Color",
                callback_data=f"mm:style:{key}",
            ),
        ],
        [
            InlineKeyboardButton("🎲 Random Color", callback_data=f"mm:random:{key}"),
            InlineKeyboardButton("🔄 Cycle Colors", callback_data=f"mm:cycle:{key}"),
        ],
        [
            InlineKeyboardButton("♻ Reset Button Color", callback_data=f"mm:resetcolor:{key}"),
        ],
        [
            InlineKeyboardButton(
                f"👤 Profile: {MENU_AUDIENCE_LABELS[aud]}",
                callback_data="mm:profile",
            ),
        ],
        [InlineKeyboardButton("⬅️ Menu Items", callback_data="mm:menu")],
    ]
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Item reordering helpers
# ─────────────────────────────────────────────────────────────────────────────

def sorted_item_slots(items: List[dict]) -> List[tuple]:
    """Return the existing row/order slots in visual order."""
    return sorted(
        {(int(item.get("row", 0)), int(item.get("order", 0))) for item in items},
        key=lambda slot: (slot[0], slot[1]),
    )


def save_reordered(
    items: List[dict],
    direction: str,
    key: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Swap one item with its visual neighbor without touching its callback.

    Mutates *items* in place and persists via save_items.
    Returns True on success.
    """
    from handlers.menu_state import save_items
    current = next((item for item in items if item["key"] == key), None)
    if current is None:
        return False

    slots = sorted_item_slots(items)
    occupied = {
        (int(item.get("row", 0)), int(item.get("order", 0))): item
        for item in items
    }
    current_slot = (int(current.get("row", 0)), int(current.get("order", 0)))
    try:
        current_index = slots.index(current_slot)
    except ValueError:
        return False
    neighbor_index = current_index - 1 if direction == "up" else current_index + 1
    if neighbor_index < 0 or neighbor_index >= len(slots):
        return False

    neighbor = occupied[slots[neighbor_index]]
    neighbor_slot = slots[neighbor_index]
    current["row"], current["order"] = neighbor_slot
    neighbor["row"], neighbor["order"] = current_slot
    save_items(items, context)
    return True
