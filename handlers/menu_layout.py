"""Menu Layout — layout snapshot, restore, and reset logic.

Centralises Save / Load / Reset-to-default for the Admin Menu Manager.
No rendering, preview, or color logic lives here.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.bot_config import cfg
from utils.menu_registry import (
    MENU_AUDIENCES,
    get_menu_layout,
    save_menu_layout,
)
from handlers.menu_colors import DEFAULT_ITEM_STYLE
from handlers.menu_state import (
    active_audience,
    get_items,
    is_admin_user,
    safe_edit,
    snapshot_key,
)

logger = logging.getLogger(__name__)

# Per-item attributes captured in every snapshot.
_LAYOUT_ITEM_ATTRS = ("visible", "enabled", "style", "audience", "label", "emoji")


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot helpers
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_current_layout(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Capture the complete current menu layout state as a serialisable dict."""
    audience = active_audience(context)
    layout = get_menu_layout(audience)
    per_item: dict = {}
    for item in get_items(context):
        key = item["key"]
        per_item[key] = {
            attr: item.get(attr, "")
            for attr in _LAYOUT_ITEM_ATTRS
        }
    return {
        "audience": audience,
        "layout": layout,
        "per_item": per_item,
        "custom_buttons": layout.get("custom_buttons", []),
        "colors_enabled": layout.get("colors_enabled", True),
        "all_colors_enabled": cfg.get_bool("global_button_colors_enabled", True),
        "status": cfg.get_str("main_menu_status", "enabled"),
        "saved_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def apply_layout_snapshot(
    snapshot: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Write a snapshot back into bot_config, restoring the saved layout."""
    audience = active_audience(context)
    layout = snapshot.get("layout")
    if not isinstance(layout, dict):
        layout = {
            "items": get_items(context),
            "custom_buttons": snapshot.get("custom_buttons", []),
            "colors_enabled": snapshot.get("colors_enabled", True),
        }
    save_menu_layout(audience, layout)
    cfg.set("global_button_colors_enabled", snapshot.get("all_colors_enabled", True))
    cfg.set("main_menu_status", snapshot.get("status", "enabled"))


def reset_to_defaults(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hard-reset every menu setting back to the factory defaults.

    Resets ALL audience profiles (Users, Premium, Admins) together, not
    just the one currently being edited. The three profiles used to drift
    independently -- e.g. resetting "Users" left stale custom emoji/colors
    sitting in "Admins" -- which is confusing since an admin's own /start
    renders the "Admins" profile. Syncing them here means Reset to Default
    always produces one consistent, predictable menu everywhere.

    Delegates to ``utils.menu_registry.reset_all_profiles_to_defaults`` --
    the same routine bot startup uses to auto-sync stale profiles after a
    deploy -- so both paths can never drift apart from each other either.

    Does NOT touch any business logic, callback names, or bot behaviour —
    only the layout/style/visibility metadata lives in bot_config.
    """
    from utils.menu_registry import reset_all_profiles_to_defaults
    reset_all_profiles_to_defaults()




# ─────────────────────────────────────────────────────────────────────────────
# Layout management handlers
# ─────────────────────────────────────────────────────────────────────────────

async def mm_layout_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snapshot the current layout to bot_config so it can be restored later."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    snap = snapshot_current_layout(context)
    cfg.set(snapshot_key(context), json.dumps(snap, ensure_ascii=False))
    saved_at = snap.get("saved_at", "")[:16].replace("T", " ")
    await query.answer(f"💾 Layout saved ({saved_at} UTC).", show_alert=True)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


async def mm_layout_load(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore the last explicitly saved layout snapshot."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    raw = cfg.get_str(snapshot_key(context), "")
    if not raw:
        await query.answer(
            "❌ No saved layout found. Use 💾 Save Layout first.",
            show_alert=True,
        )
        return

    try:
        snap = json.loads(raw)
    except Exception:
        await query.answer("❌ Saved layout is corrupted.", show_alert=True)
        return

    apply_layout_snapshot(snap, context)
    saved_at = snap.get("saved_at", "")[:16].replace("T", " ")
    await query.answer(
        f"📂 Layout restored from {saved_at} UTC.",
        show_alert=True,
    )
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)


async def mm_layout_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a confirmation page before executing a full reset-to-default."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    text = (
        "⚠️ <b>Reset to Default?</b>\n\n"
        "This will restore the factory layout for <b>every profile at once "
        "(Users, Premium, Admins)</b> so they can't drift apart:\n"
        "• All items shown and enabled\n"
        "• Default row/order restored\n"
        "• All custom labels, emojis, and colors wiped\n"
        "• Menu status reset to <b>Enabled</b>\n"
        "• Colors switch turned <b>ON</b> for all profiles\n\n"
        "<b>Custom buttons are NOT affected.</b>\n\n"
        "This cannot be undone unless you have a saved layout. "
        "Consider tapping <b>💾 Save Layout</b> first."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Reset to Default", callback_data="mm:layout:reset:confirm"),
            InlineKeyboardButton("⬅️ Back", callback_data="mm:menu"),
        ],
    ])
    await safe_edit(query, text, kb)


async def mm_layout_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute the full reset-to-default after admin confirmation."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    reset_to_defaults(context)
    await query.answer("🔄 Menu reset to factory defaults.", show_alert=True)
    from handlers.menu_preview import mm_refresh_preview
    await mm_refresh_preview(context)
    from handlers.admin_menu_manager import mm_menu
    await mm_menu(update, context)
