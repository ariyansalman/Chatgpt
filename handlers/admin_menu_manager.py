"""Admin Main Menu Manager — V20 (Refactored).

Callback namespace: mm:*

This module is a pure coordinator. All implementation detail has been
extracted into dedicated modules:

  handlers/menu_state.py   — state helpers, audience resolution, persistence
  handlers/menu_colors.py  — color management and color-picker logic
  handlers/menu_emojis.py  — premium emoji icon help
  handlers/menu_renderer.py — item markup and reorder helpers
  handlers/menu_preview.py  — live preview build and refresh
  handlers/menu_layout.py   — layout snapshot, restore, reset
  handlers/menu_actions.py  — per-item CRUD, custom buttons, admin menu manager

Public surface (unchanged):
  mm_menu()                     — Main Menu Manager dashboard
  mm_set_status()               — status: enabled/maintenance/disabled
  mm_toggle_item()              — toggle a menu item on or off
  mm_noop()                     — no-op (page labels)
  mm_dispatch()                 — central callback dispatcher
  build_mm_edit_conversation()  — inline rename/emoji ConversationHandler
  build_custom_button_conversation() — custom button ConversationHandler
  build_amgr_conversation()     — admin menu builder ConversationHandler

Integrates with utils.bot_config.  All status changes take immediate
effect via the cfg cache invalidation.  The Live Preview message is kept
as a separate message in the admin's chat and is refreshed automatically
after every configuration change.
"""

from __future__ import annotations

import json
import logging
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.bot_config import cfg
from utils.menu_registry import (
    MENU_AUDIENCE_LABELS,
    get_menu_layout,
    is_item_enabled,
    is_item_visible,
)

# ── Dedicated modules ─────────────────────────────────────────────────────────
from handlers.menu_state import (
    STATUS_LABELS,
    active_audience,
    is_admin_user,
    item_display_name,
    item_key_set,
    get_item,
    get_items,
    safe_edit,
    snapshot_key,
    update_item,
)
from handlers.menu_colors import (
    mm_cycle_style,
    mm_reset_button_color,
    mm_random_color,
    mm_cycle_color,
    mm_set_color,
    mm_color_picker,
    mm_reset_colors,
    mm_reset_all_colors,
    mm_toggle_all_colors,
    mm_toggle_colors,
)
from handlers.menu_emojis import mm_emoji_help
from handlers.menu_preview import mm_refresh_preview, mm_show_preview
from handlers.menu_layout import (
    mm_layout_load,
    mm_layout_reset,
    mm_layout_reset_confirm,
    mm_layout_save,
)
from handlers.menu_actions import (
    # Main-menu item handlers
    mm_item_detail,
    mm_toggle_visibility,
    mm_toggle_enabled,
    mm_set_audience,
    mm_move_item,
    mm_profile_menu,
    mm_set_profile,
    # Custom buttons
    mm_custom_menu,
    mm_custom_add,
    mm_custom_edit,
    mm_custom_delete,
    mm_custom_visibility,
    mm_custom_color_picker,
    mm_custom_color_action,
    mm_custom_move,
    mm_custom_clear,
    # Admin Menu Manager
    mm_amgr_list,
    mm_amgr_menu,
    mm_amgr_item_detail,
    mm_amgr_toggle_visibility,
    mm_amgr_toggle_enabled,
    mm_amgr_move_item,
    mm_amgr_cycle_style,
    mm_amgr_set_color,
    mm_amgr_color_action,
    mm_amgr_preview,
    mm_amgr_reset,
    mm_amgr_reset_confirm,
    mm_amgr_audopt,
    mm_amgr_set_audience,
    mm_amgr_delconfirm,
    mm_amgr_delitem,
    mm_amgr_deletemenu,
    mm_amgr_deletemenu_confirm,
    # Conversation builders
    build_mm_edit_conversation,
    build_custom_button_conversation,
    build_amgr_conversation,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Main Menu Manager Dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def mm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📋 Main Menu Manager — dashboard for controlling the user main menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    current_status = cfg.get_str("main_menu_status", "enabled")
    status_label = STATUS_LABELS.get(current_status, current_status)

    rows: List[List[InlineKeyboardButton]] = []

    # Status selector row
    status_row = []
    for s, lbl in STATUS_LABELS.items():
        marker = "✅ " if s == current_status else ""
        status_row.append(InlineKeyboardButton(
            f"{marker}{lbl}", callback_data=f"mm:status:{s}"
        ))
    rows.append(status_row)

    # Global color kill switch + reset
    audience = active_audience(context)
    colors_on = bool(get_menu_layout(audience).get("colors_enabled", True))
    rows.append([
        InlineKeyboardButton(
            f"👥 Profile: {MENU_AUDIENCE_LABELS[audience]}",
            callback_data="mm:profile",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            f"🎨 Colors: {'✅ ON' if colors_on else '🚫 OFF'}",
            callback_data="mm:colors_toggle"
        ),
        InlineKeyboardButton("🔁 Reset Colors Only", callback_data="mm:colors_reset"),
    ])
    rows.append([
        InlineKeyboardButton("♻ Reset All Colors", callback_data="mm:all_colors_reset"),
    ])

    # Bot-wide color switch — covers every OTHER keyboard in the bot
    all_colors_on = cfg.get_bool("global_button_colors_enabled", True)
    rows.append([
        InlineKeyboardButton(
            f"🌈 All Bot Buttons: {'✅ ON' if all_colors_on else '🚫 OFF'}",
            callback_data="mm:all_colors_toggle"
        ),
    ])

    rows.append([InlineKeyboardButton("━━━ Menu Items ━━━", callback_data="mm:noop")])

    for item in get_items(context):
        key = item["key"]
        rows.append([
            InlineKeyboardButton(
                f"{'👁' if is_item_visible(item) else '🚫'} {item_display_name(item)}",
                callback_data=f"mm:item:{key}",
            ),
            InlineKeyboardButton(
                f"{'✅' if is_item_enabled(item) else '⏸'} "
                f"{'Active' if is_item_enabled(item) else 'Disabled'}",
                callback_data=f"mm:item:{key}",
            ),
        ])

    rows.append([InlineKeyboardButton("━━━ Custom Buttons ━━━", callback_data="mm:noop")])
    rows.append([InlineKeyboardButton("🔧 Manage Custom Buttons", callback_data="mm:custom")])
    rows.append([InlineKeyboardButton("✨ Premium Emoji Icons (help)", callback_data="mm:emoji_help")])

    # Admin Menus section
    rows.append([InlineKeyboardButton("━━━ Admin Menus ━━━", callback_data="mm:noop")])
    rows.append([InlineKeyboardButton("🗂 Manage Admin Menus", callback_data="mm:amgr")])

    # Layout Management section
    rows.append([InlineKeyboardButton("━━━ Layout Management ━━━", callback_data="mm:noop")])
    rows.append([
        InlineKeyboardButton("📺 Live Preview", callback_data="mm:preview"),
        InlineKeyboardButton("💾 Save Layout",  callback_data="mm:layout:save"),
    ])
    saved_raw = cfg.get_str(snapshot_key(context), "")
    load_label = "📂 Load Layout"
    if saved_raw:
        try:
            snap = json.loads(saved_raw)
            saved_at = snap.get("saved_at", "")
            if saved_at:
                load_label = f"📂 Load ({saved_at[:16].replace('T', ' ')} UTC)"
        except Exception:
            pass
    rows.append([
        InlineKeyboardButton(load_label,           callback_data="mm:layout:load"),
        InlineKeyboardButton("🔄 Reset to Default", callback_data="mm:layout:reset"),
    ])

    rows.append([InlineKeyboardButton("⬅️ Admin Menu", callback_data="admin_menu")])

    # Build summary text
    items_status = []
    for item in get_items(context):
        items_status.append(
            f"  {'👁' if is_item_visible(item) else '🚫'} "
            f"{'✅' if is_item_enabled(item) else '⏸'} "
            f"{item_display_name(item)}"
        )

    preview_hint = (
        "Tap <b>📺 Live Preview</b> to send a preview message that auto-updates "
        "whenever you change anything — no need to use /start or open a new chat."
    )
    text = (
        "📋 <b>Main Menu Manager</b>\n\n"
        f"👥 Profile: <b>{MENU_AUDIENCE_LABELS[audience]}</b>\n"
        f"🔹 Status: <b>{status_label}</b>\n"
        f"🎨 Colors: <b>{'ON' if colors_on else 'OFF (showing default color everywhere)'}</b>\n\n"
        "<b>Menu Items:</b>\n"
        + "\n".join(items_status)
        + "\n\n"
        + preview_hint + "\n\n"
        "Use the profile button to manage Users, Premium Users, or Admins independently.\n"
        "Tap a status button to switch modes.\n"
        "Tap an item to manage visibility, enabled state, name, emoji, color, "
        "or position within this profile.\n"
        "Tap <b>🎨 Colors: ON/OFF</b> to instantly hide/restore all colors "
        "without losing your per-item settings.\n"
        "Tap <b>🔁 Reset Colors Only</b> to wipe custom color choices for "
        "every profile (emoji and labels are untouched). "
        "Use <b>🔄 Reset to Default</b> in Layout Management to restore the "
        "full factory layout (items, order, emoji, and colors) for every "
        "profile.\n"
        "Tap <b>🗂 Manage Admin Menus</b> to control admin panel sub-menus "
        "(Product Management, Orders, Settings, etc.) with the same per-item controls.\n"
        "<i>Requires the Telegram app to be updated to support colored "
        "buttons — older clients just show the default color.</i>"
    )

    await safe_edit(query, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Status Toggle
# ─────────────────────────────────────────────────────────────────────────────

async def mm_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set main menu status: enabled | maintenance | disabled."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    new_status = data.split(":", 2)[-1] if ":" in data else "enabled"
    if new_status not in ("enabled", "maintenance", "disabled"):
        await query.answer("❌ Invalid status.", show_alert=True)
        return

    cfg.set("main_menu_status", new_status)
    label = STATUS_LABELS.get(new_status, new_status)
    await query.answer(f"✅ Main menu set to {label}", show_alert=False)
    await mm_refresh_preview(context)
    await mm_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Item Toggle
# ─────────────────────────────────────────────────────────────────────────────

async def mm_toggle_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a single menu item on or off."""
    query = update.callback_query
    if not is_admin_user(update):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 3:
        await query.answer("❌ Invalid action.", show_alert=True)
        return

    item_key = parts[2]
    valid_keys = item_key_set(context)
    if item_key not in valid_keys:
        await query.answer("❌ Unknown menu item.", show_alert=True)
        return

    item = get_item(item_key, context)
    from utils.menu_registry import is_item_enabled as _is_enabled
    current = _is_enabled(item) if item else True
    if item:
        update_item(item_key, context, enabled=not current)

    label = item_display_name(item or {"key": item_key})
    action = "enabled" if not current else "disabled"
    await query.answer(f"{label} is now {action}.", show_alert=False)
    await mm_refresh_preview(context)
    await mm_menu(update, context)


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
    elif data == "mm:profile":
        await mm_profile_menu(update, context)
    elif data.startswith("mm:profile:"):
        await mm_set_profile(update, context)
    elif data.startswith("mm:item:"):
        await mm_item_detail(update, context)
    elif data.startswith("mm:visible:"):
        await mm_toggle_visibility(update, context)
    elif data.startswith("mm:enabled:"):
        await mm_toggle_enabled(update, context)
    elif data.startswith("mm:audience:"):
        await mm_set_audience(update, context)
    elif data.startswith("mm:moveup:") or data.startswith("mm:movedown:"):
        await mm_move_item(update, context)
    elif data.startswith("mm:status:"):
        await mm_set_status(update, context)
    elif data.startswith("mm:toggle:"):
        await mm_toggle_item(update, context)
    elif data.startswith("mm:color:"):
        await mm_set_color(update, context)
    elif data.startswith("mm:random:"):
        await mm_random_color(update, context)
    elif data.startswith("mm:cycle:"):
        await mm_cycle_color(update, context)
    elif data.startswith("mm:resetcolor:"):
        await mm_reset_button_color(update, context)
    elif data.startswith("mm:style:"):
        await mm_cycle_style(update, context)
    elif data == "mm:colors_toggle":
        await mm_toggle_colors(update, context)
    elif data == "mm:all_colors_toggle":
        await mm_toggle_all_colors(update, context)
    elif data == "mm:all_colors_reset":
        await mm_reset_all_colors(update, context)
    elif data == "mm:colors_reset":
        await mm_reset_colors(update, context)
    elif data == "mm:emoji_help":
        await mm_emoji_help(update, context)
    elif data == "mm:custom":
        await mm_custom_menu(update, context)
    elif data.startswith("mm:custom:visibility:"):
        await mm_custom_visibility(update, context)
    elif data.startswith("mm:custom:color:"):
        await mm_custom_color_picker(update, context)
    elif (data.startswith("mm:custom:setcolor:") or data.startswith("mm:custom:random:")
            or data.startswith("mm:custom:cycle:") or data.startswith("mm:custom:resetcolor:")):
        await mm_custom_color_action(update, context)
    elif data.startswith("mm:custom:up:") or data.startswith("mm:custom:down:"):
        await mm_custom_move(update, context)
    elif data.startswith("mm:custom:del:"):
        await mm_custom_delete(update, context)
    elif data == "mm:custom:clear":
        await mm_custom_clear(update, context)
    elif data == "mm:preview":
        await mm_show_preview(update, context)
    elif data == "mm:layout:save":
        await mm_layout_save(update, context)
    elif data == "mm:layout:load":
        await mm_layout_load(update, context)
    elif data == "mm:layout:reset":
        await mm_layout_reset(update, context)
    elif data == "mm:layout:reset:confirm":
        await mm_layout_reset_confirm(update, context)
    elif data == "mm:noop":
        await mm_noop(update, context)
    # ── Admin Menu Manager (mm:amgr:*) ──────────────────────────────────────
    elif data in ("mm:amgr", "mm:amgr:list"):
        await mm_amgr_list(update, context)
    elif data == "mm:amgr:createmenu":
        # Handled by the amgr ConversationHandler — no-op fallback.
        await query.answer()
    elif data.startswith("mm:amgr:") and ":item:" in data:
        await mm_amgr_item_detail(update, context)
    elif data.startswith("mm:amgr:") and ":visible:" in data:
        await mm_amgr_toggle_visibility(update, context)
    elif data.startswith("mm:amgr:") and ":enabled:" in data:
        await mm_amgr_toggle_enabled(update, context)
    elif data.startswith("mm:amgr:") and (":moveup:" in data or ":movedown:" in data):
        await mm_amgr_move_item(update, context)
    elif data.startswith("mm:amgr:") and ":color:" in data:
        await mm_amgr_set_color(update, context)
    elif data.startswith("mm:amgr:") and any(
        token in data for token in (":random:", ":cycle:", ":resetcolor:")
    ):
        await mm_amgr_color_action(update, context)
    elif data.startswith("mm:amgr:") and ":style:" in data:
        await mm_amgr_cycle_style(update, context)
    elif data.startswith("mm:amgr:") and ":audopt:" in data:
        await mm_amgr_audopt(update, context)
    elif data.startswith("mm:amgr:") and ":audience:" in data:
        await mm_amgr_set_audience(update, context)
    elif data.startswith("mm:amgr:") and ":delconfirm:" in data:
        await mm_amgr_delconfirm(update, context)
    elif data.startswith("mm:amgr:") and ":delitem:" in data:
        await mm_amgr_delitem(update, context)
    elif data.startswith("mm:amgr:") and ":deletemenu" in data:
        await mm_amgr_deletemenu(update, context)
    elif data.endswith(":reset:confirm") and data.startswith("mm:amgr:"):
        await mm_amgr_reset_confirm(update, context)
    elif data.endswith(":reset") and data.startswith("mm:amgr:"):
        await mm_amgr_reset(update, context)
    elif data.endswith(":preview") and data.startswith("mm:amgr:"):
        await mm_amgr_preview(update, context)
    elif data.startswith("mm:amgr:"):
        # mm:amgr:<menu_id> — show dashboard for a specific admin menu
        await mm_amgr_menu(update, context)
    else:
        await query.answer()
