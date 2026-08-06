"""Menu State — shared state helpers for the Admin Menu Manager.

Provides item accessors, audience resolution, persistence helpers,
and lightweight utility functions used across all menu sub-modules.
No rendering, preview, color, or layout logic lives here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from utils.bot_config import cfg
from utils.helpers import is_admin
from utils.menu_registry import (
    MENU_AUDIENCE_LABELS,
    get_item_label,
    get_menu_items_for_audience,
    get_menu_layout,
    is_item_enabled,
    is_item_visible,
    normalize_menu_audience,
    save_menu_layout,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

STATUS_LABELS = {
    "enabled":     "🟢 Enabled",
    "maintenance": "🟡 Maintenance",
    "disabled":    "🔴 Disabled",
}

# ─────────────────────────────────────────────────────────────────────────────
# Admin guard & safe edit
# ─────────────────────────────────────────────────────────────────────────────

def is_admin_user(update: Update) -> bool:
    """Return True if the update's effective user is an admin."""
    return is_admin(update.effective_user.id)


async def safe_edit(query, text: str, reply_markup=None) -> None:
    """Edit a message, silently swallowing 'Message is not modified' errors."""
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Audience helpers
# ─────────────────────────────────────────────────────────────────────────────

def active_audience(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the admin's currently active audience profile name."""
    return normalize_menu_audience(context.user_data.get("mm_audience", "users"))


def audience_label(value: str) -> str:
    """Human-readable label for an audience value."""
    return {
        "all":     "Everyone",
        "admin":   "Admins only",
        "user":    "Users only",
        "premium": "Premium only",
    }.get(value, "Everyone")


# ─────────────────────────────────────────────────────────────────────────────
# Item accessors
# ─────────────────────────────────────────────────────────────────────────────

def get_items(context: ContextTypes.DEFAULT_TYPE = None) -> List[dict]:
    """Return the active main-menu registry for the current audience."""
    aud = active_audience(context) if context is not None else "users"
    return [
        item for item in get_menu_items_for_audience(aud)
        if isinstance(item, dict) and item.get("key")
    ]


def get_item(key: str, context: ContextTypes.DEFAULT_TYPE = None) -> Optional[dict]:
    """Return one main-menu item by key, or None."""
    return next((item for item in get_items(context) if item["key"] == key), None)


def item_key_set(context: ContextTypes.DEFAULT_TYPE = None) -> set:
    """Return the set of all known main-menu item keys."""
    return {item["key"] for item in get_items(context)}


def item_display_name(item: dict) -> str:
    """Resolve the human-readable name for a menu item."""
    return get_item_label(item, "en")


def get_audience(key: str, item: dict) -> str:
    """Resolve the audience value for a menu item."""
    fallback = "admin" if item.get("admin_only") else item.get("audience", "all")
    return str(
        item.get("audience", cfg.get_str(f"menu_item_{key}_audience", fallback))
    ).lower()


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def save_items(items: List[dict], context: ContextTypes.DEFAULT_TYPE = None) -> None:
    """Persist layout metadata without changing callbacks or business logic."""
    aud = active_audience(context) if context is not None else "users"
    layout = get_menu_layout(aud)
    layout["items"] = items
    save_menu_layout(aud, layout)


def update_item(key: str, context: ContextTypes.DEFAULT_TYPE, **fields) -> bool:
    """Atomically load → mutate → save one main-menu item.

    Returns True if the key was found and saved, False otherwise.
    """
    items = get_items(context)
    matched = False
    for item in items:
        if item.get("key") == key:
            item.update(fields)
            matched = True
    if matched:
        save_items(items, context)
    return matched


def item_detail_text(item: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Build the detail text shown for a single main-menu item."""
    from handlers.menu_colors import get_item_style  # avoid circular at module level
    key = item["key"]
    visible = is_item_visible(item)
    enabled = is_item_enabled(item)
    style = get_item_style(key, context)
    aud = active_audience(context)
    return (
        f"📋 <b>{item_display_name(item)}</b>\n\n"
        f"ID: <code>{key}</code>\n"
        f"Visibility: <b>{'Shown' if visible else 'Hidden'}</b>\n"
        f"Action: <b>{'Enabled' if enabled else 'Disabled'}</b>\n"
        f"Profile: <b>{MENU_AUDIENCE_LABELS[aud]}</b>\n"
        f"Color: <b>{style}</b>\n"
        f"Position: row {item.get('row', 0)}, item {item.get('order', 0) + 1}\n\n"
        "All changes are saved immediately and apply the next time a menu is rendered."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot key
# ─────────────────────────────────────────────────────────────────────────────

_LAYOUT_SNAPSHOT_KEY = "main_menu_layout_saved_json"


def snapshot_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the bot_config key for the current audience's layout snapshot."""
    return f"{_LAYOUT_SNAPSHOT_KEY}_{active_audience(context)}"


# ─────────────────────────────────────────────────────────────────────────────
# Custom-button helpers (shared by menu_actions and menu_preview)
# ─────────────────────────────────────────────────────────────────────────────

def custom_bool(value) -> bool:
    """Coerce a truthy config value to bool."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def parse_custom_buttons(context: ContextTypes.DEFAULT_TYPE = None) -> list:
    """Return the custom buttons list for the active audience."""
    aud = active_audience(context) if context is not None else "users"
    buttons = get_menu_layout(aud).get("custom_buttons", [])
    return buttons if isinstance(buttons, list) else []


def save_custom_buttons(
    buttons: list, context: ContextTypes.DEFAULT_TYPE = None
) -> None:
    """Persist custom buttons without changing another role profile."""
    aud = active_audience(context) if context is not None else "users"
    layout = get_menu_layout(aud)
    layout["custom_buttons"] = buttons
    save_menu_layout(aud, layout)
