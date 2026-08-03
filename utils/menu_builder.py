"""Menu Builder — Generic Dynamic Menu Architecture.

Central registry for ALL bot menus (admin, submenus, user-facing).
The user-facing *main* menu is still managed by ``utils/menu_registry.py``
for backward compatibility; this module extends the same philosophy to
every other menu in the bot.

Every statically-structured menu is defined here as defaults and rendered
dynamically at runtime — no hardcoded buttons in keyboard-rendering code.
Admins can override any registered menu in full by storing JSON in
``bot_config`` under the key ``menu_<menu_id>_json``, or per-item via
``menu_<menu_id>_item_<key>_<attr>`` keys — without any code change or
deployment.

Public API
----------
register_menu(menu_id, items, description="")
    Declare (or replace) a menu definition at import time.

get_menu_keyboard(menu_id, audience, user_id, lang, runtime_overrides, extra_rows)
    Render a registered menu to an InlineKeyboardMarkup.

get_menu_items(menu_id)
    Return the active item list for a menu with all overrides applied.

get_item(menu_id, key)
    Return a single item with overrides applied.

update_item(menu_id, key, **attrs)
    Persist per-item attribute overrides to bot_config (admin-side API).

save_menu(menu_id, items)
    Persist a complete menu override to bot_config.

reset_menu(menu_id)
    Remove any stored override, reverting the menu to its registered default.

list_menus()
    Return all registered menu IDs.

Item schema
-----------
key             str   Stable identifier (unique within the menu).
label           str   Display text (raw string).
label_key       str   i18n key resolved at render time (optional).
callback        str   callback_data (mutually exclusive with ``url``).
url             str   External URL (mutually exclusive with ``callback``).
row             int   1-based row number.
order           int   Sort key within the row (lower = further left).
full_width      bool  Render alone on its own row.
visible         bool  Show or hide this item.
enabled         bool  If False, callback becomes ``menu_disabled:<key>``.
emoji           str   Emoji prefix prepended to the label.
emoji_id        str   Custom emoji ID (Bot API 9.4 icon).
style           str   ``"none"`` | ``"success"`` | ``"primary"`` | ``"danger"``.
audience        str   ``"all"`` | ``"admin"`` | ``"user"`` | ``"premium"``.
admin_only      bool  Shorthand for ``audience="admin"``.
default_visible bool  Initial value for ``visible`` (default True).
default_enabled bool  Initial value for ``enabled`` (default True).

None of the callback_data values defined here interact with payment logic,
wallet logic, order logic, business logic, APIs, routes, permissions, or
database schema — this module only describes menu *layout*.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .button_colors import (
    get_button_color,
    global_colors_enabled,
    telegram_style_for_color,
)
from .menu_registry import LEADING_EMOJI_RE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal registry: {menu_id: {"items": [...], "description": str}}
# ---------------------------------------------------------------------------
_MENU_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_menu(
    menu_id: str,
    items: List[Dict[str, Any]],
    description: str = "",
) -> None:
    """Register (or replace) a menu definition.

    Safe to call at module-import time.  Re-registering the same
    ``menu_id`` replaces it — later registrations win, so plugins or
    feature modules can extend the default set without forking this file.
    """
    _MENU_REGISTRY[menu_id] = {
        "items": deepcopy(items),
        "description": description,
    }
    logger.debug(
        "menu_builder: registered menu '%s' (%d items)", menu_id, len(items)
    )


def list_menus() -> List[str]:
    """Return all registered menu IDs."""
    return list(_MENU_REGISTRY.keys())


def get_menu_description(menu_id: str) -> str:
    """Return the human-readable description for a registered menu."""
    return _MENU_REGISTRY.get(menu_id, {}).get("description", menu_id)


# ---------------------------------------------------------------------------
# bot_config helpers (gracefully degrade if config is not ready)
# ---------------------------------------------------------------------------

def _cfg_get(key: str, default: str = "") -> str:
    try:
        from utils.bot_config import cfg
        return cfg.get_str(key, default)
    except Exception:
        return default


def _cfg_set(key: str, value: Any) -> None:
    try:
        from utils.bot_config import cfg
        cfg.set(key, value)
    except Exception:
        logger.debug(
            "menu_builder: could not write cfg key %s", key, exc_info=True
        )


def _read_json_cfg(key: str, default: Any) -> Any:
    raw = _cfg_get(key, "")
    if not raw.strip():
        return deepcopy(default)
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning(
            "menu_builder: invalid JSON in bot_config key %s; using default", key
        )
        return deepcopy(default)


def _cfg_key(menu_id: str) -> str:
    """bot_config key that holds a full JSON override for a menu."""
    return f"menu_{menu_id}_json"


def _item_cfg_prefix(menu_id: str, key: str) -> str:
    return f"menu_{menu_id}_item_{key}"


# ---------------------------------------------------------------------------
# Item resolution — merge defaults with stored per-item overrides
# ---------------------------------------------------------------------------

def _bool_cfg(value: str, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _resolve_item(menu_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Apply per-item bot_config overrides to a single item dict."""
    key = item.get("key", "")
    if not key:
        return deepcopy(item)
    prefix = _item_cfg_prefix(menu_id, key)
    result = deepcopy(item)

    # Per-item attribute overrides stored by an admin or programmatically.
    for attr, cfg_suffix in (
        ("visible",  "visible"),
        ("enabled",  "enabled"),
        ("label",    "label"),
        ("emoji",    "emoji"),
        ("emoji_id", "emoji_id"),
        ("style",    "style"),
        ("audience", "audience"),
        ("row",      "row"),
        ("order",    "order"),
    ):
        raw = _cfg_get(f"{prefix}_{cfg_suffix}", "").strip()
        if not raw:
            continue
        if attr in ("visible", "enabled"):
            result[attr] = _bool_cfg(raw, result.get(attr, True))
        elif attr in ("row", "order"):
            try:
                result[attr] = int(raw)
            except ValueError:
                pass
        else:
            result[attr] = raw

    # Fill in defaults for visibility / enabled if still absent.
    if "visible" not in result:
        result["visible"] = result.get("default_visible", True)
    if "enabled" not in result:
        result["enabled"] = result.get("default_enabled", True)

    return result


def get_menu_items(menu_id: str) -> List[Dict[str, Any]]:
    """Return the active item list for a menu with all overrides applied.

    Priority (highest first):
    1. Full-menu JSON override stored in bot_config (``menu_<id>_json``).
    2. Registered defaults.
    Then per-item cfg keys are applied on top of whichever source is used.
    """
    default_def = _MENU_REGISTRY.get(menu_id, {})
    default_items = default_def.get("items", [])

    override = _read_json_cfg(_cfg_key(menu_id), [])
    source = override if isinstance(override, list) and override else default_items

    return [
        _resolve_item(menu_id, item)
        for item in source
        if isinstance(item, dict)
    ]


def get_item(menu_id: str, key: str) -> Optional[Dict[str, Any]]:
    """Return a single item from a menu by key, with overrides applied."""
    return next(
        (item for item in get_menu_items(menu_id) if item.get("key") == key),
        None,
    )


def update_item(menu_id: str, key: str, **attrs: Any) -> None:
    """Persist per-item attribute overrides to bot_config.

    Only the listed attributes are written; other fields are untouched.
    This is the admin-facing API used by the Menu Manager handler.
    """
    prefix = _item_cfg_prefix(menu_id, key)
    for attr, value in attrs.items():
        _cfg_set(f"{prefix}_{attr}", value)


def save_menu(menu_id: str, items: List[Dict[str, Any]]) -> None:
    """Persist a complete menu item list to bot_config (full JSON override)."""
    _cfg_set(_cfg_key(menu_id), json.dumps(items, ensure_ascii=False))


def reset_menu(menu_id: str) -> None:
    """Remove any stored override, reverting the menu to registered defaults."""
    _cfg_set(_cfg_key(menu_id), "")
    # Also clear per-item overrides for all items in the registered default.
    default_items = _MENU_REGISTRY.get(menu_id, {}).get("items", [])
    for item in default_items:
        key = item.get("key", "")
        if not key:
            continue
        prefix = _item_cfg_prefix(menu_id, key)
        for suffix in ("visible", "enabled", "label", "emoji", "emoji_id",
                        "style", "audience", "row", "order"):
            _cfg_set(f"{prefix}_{suffix}", "")


# ---------------------------------------------------------------------------
# Audience filtering
# ---------------------------------------------------------------------------

def _item_audience(item: Dict[str, Any]) -> str:
    if item.get("admin_only"):
        return "admin"
    return str(item.get("audience", "all")).strip().lower()


def _passes_audience(item: Dict[str, Any], viewer: str) -> bool:
    """Return True if this item should be shown to the viewer's audience."""
    required = _item_audience(item)
    if required == "all":
        return True
    if required == "admin":
        return viewer == "admin"
    if required == "premium":
        return viewer in ("premium", "admin")
    if required == "user":
        return viewer in ("user", "admin")  # admins see everything
    return True


# ---------------------------------------------------------------------------
# Label resolution
# ---------------------------------------------------------------------------

def _resolve_label(item: Dict[str, Any], lang: str = "en") -> str:
    """Resolve display label, injecting emoji prefix when present."""
    if item.get("label"):
        label = str(item["label"])
    elif item.get("label_key"):
        try:
            from i18n import t
            label = t(item["label_key"], lang)
        except Exception:
            label = str(item.get("key", ""))
    else:
        label = str(item.get("key", ""))

    emoji = item.get("emoji", "")
    if emoji:
        # Strip any leading emoji already baked into the label so we never
        # duplicate it. Uses the same pattern as menu_registry.get_item_label
        # so every menu in the bot dedupes emoji identically.
        without_emoji = LEADING_EMOJI_RE.sub("", label).strip()
        label = f"{emoji} {without_emoji}".strip()
    return label


# ---------------------------------------------------------------------------
# Keyboard rendering
# ---------------------------------------------------------------------------

def _build_button(
    item: Dict[str, Any],
    lang: str = "en",
    colors_enabled: bool = True,
    simulate: bool = False,
) -> InlineKeyboardButton:
    """Build a single InlineKeyboardButton from an item dict.

    ``simulate=True`` replaces callback_data/url with the harmless
    ``"mm:noop"`` callback -- used by the admin Live Preview so it can call
    this exact renderer instead of keeping its own copy of the button-
    building logic (which would risk drifting from the real menu).
    """
    label = _resolve_label(item, lang)
    enabled = item.get("enabled", True)

    callback = item.get("callback")
    url = item.get("url")

    if simulate:
        callback, url = "mm:noop", None
    elif not enabled and callback:
        callback = f"menu_disabled:{item.get('key', '')}"

    # The global toggle always wins, full stop -- it overrides both the
    # per-item stored style below (an admin's custom color for this one
    # button) and the local `colors_enabled` flag some callers pass (kept
    # for backward compatibility; it can only turn colors off, never on).
    if colors_enabled and global_colors_enabled():
        selected_color = item.get("style") or get_button_color(
            item.get("callback") or item.get("key"),
            text=label,
        )
        style = telegram_style_for_color(selected_color)
    else:
        style = None
    emoji_id = item.get("emoji_id") or None

    kwargs: Dict[str, Any] = {}
    if callback is not None:
        kwargs["callback_data"] = callback
    if url is not None:
        kwargs["url"] = url

    try:
        return InlineKeyboardButton(
            label, style=style, icon_custom_emoji_id=emoji_id, **kwargs
        )
    except TypeError:
        # Older python-telegram-bot (<22.7) doesn't support style / emoji_id.
        return InlineKeyboardButton(label, **kwargs)


def _sorted_rows(
    items: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Group visible items into rows ordered by row-number then order."""
    by_row: Dict[Any, List[Dict[str, Any]]] = {}
    for item in items:
        if not item.get("visible", True):
            continue
        by_row.setdefault(item.get("row", 0), []).append(item)
    return [
        sorted(by_row[row_no], key=lambda it: it.get("order", 0))
        for row_no in sorted(by_row.keys())
    ]


def get_menu_keyboard(
    menu_id: str,
    audience: str = "all",
    user_id: int = None,
    lang: str = "en",
    runtime_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None,
    colors_enabled: bool = True,
    simulate: bool = False,
) -> InlineKeyboardMarkup:
    """Render a registered menu to an InlineKeyboardMarkup.

    Parameters
    ----------
    menu_id:
        Registered menu identifier (e.g. ``"admin_menu"``,
        ``"admin_products_menu"``).
    audience:
        Viewer's role: ``"all"`` | ``"admin"`` | ``"user"`` | ``"premium"``.
        Items whose ``audience`` doesn't match are silently hidden.
    user_id:
        Telegram user ID.  Used for an automatic admin check when
        ``audience`` is still ``"all"``.
    lang:
        Language code for i18n label resolution.
    runtime_overrides:
        ``{item_key: {attr: value, ...}}`` applied at render time ONLY —
        NOT persisted to bot_config.  Used for runtime-computed labels
        (e.g. a toggle button whose text reflects a live config value).
    extra_rows:
        Pre-built ``InlineKeyboardButton`` rows appended after all menu
        items.  Used for dynamic DB-driven content (e.g. a list of records
        fetched from the database) that cannot be stored in config.
    colors_enabled:
        Whether to apply ``style`` colors.  Defaults to True.
    simulate:
        When True, every button's callback_data/url is replaced with the
        harmless ``"mm:noop"`` callback.  Used by the admin Live Preview so
        it renders through this exact function instead of a hand-rolled
        copy that could drift from the real menu.
    """
    # Auto-resolve admin audience from user_id when not pre-resolved.
    if audience == "all" and user_id is not None:
        try:
            from utils.helpers import is_admin as _is_admin_check
            if _is_admin_check(user_id):
                audience = "admin"
        except Exception:
            pass

    items = get_menu_items(menu_id)

    # Apply runtime overrides (ephemeral — not written to config).
    if runtime_overrides:
        for item in items:
            key = item.get("key")
            if key and key in runtime_overrides:
                item.update(runtime_overrides[key])

    # Audience filter.
    items = [item for item in items if _passes_audience(item, audience)]

    keyboard: List[List[InlineKeyboardButton]] = []
    for row_items in _sorted_rows(items):
        packed: List[InlineKeyboardButton] = []
        for item in row_items:
            btn = _build_button(item, lang=lang, colors_enabled=colors_enabled, simulate=simulate)
            if item.get("full_width"):
                if packed:
                    keyboard.append(packed)
                    packed = []
                keyboard.append([btn])
            else:
                packed.append(btn)
        if packed:
            keyboard.append(packed)

    # Append extra (dynamic) rows at the end.
    if extra_rows:
        keyboard.extend(extra_rows)

    return InlineKeyboardMarkup(keyboard)


# ===========================================================================
# Built-in menu definitions
# All callback_data values are identical to the pre-existing hardcoded ones
# so no handler, route, or permission is affected by this refactor.
# ===========================================================================

# ---------------------------------------------------------------------------
# Admin main menu
# ---------------------------------------------------------------------------
register_menu("admin_menu", [
    {"key": "admin_products",  "label": "📦 Product Management", "callback": "admin_products",
     "row": 1, "order": 0, "full_width": True},
    {"key": "admin_users",     "label": "👥 User Management",    "callback": "admin_users",
     "row": 2, "order": 0, "full_width": True},
    {"key": "admin_orders",    "label": "🛒 Order Management",   "callback": "admin_orders",
     "row": 3, "order": 0, "full_width": True},
    {"key": "admin_tickets",   "label": "🎫 Support Tickets",    "callback": "admin_tickets",
     "row": 4, "order": 0, "full_width": True},
    {"key": "admin_analytics", "label": "📊 Analytics",          "callback": "admin_analytics",
     "row": 5, "order": 0, "full_width": True},
    {"key": "admin_settings",  "label": "⚙️ Store Settings",     "callback": "admin_settings",
     "row": 6, "order": 0, "full_width": True},
    {"key": "admin_broadcast", "label": "📢 Broadcast",          "callback": "admin_broadcast",
     "row": 7, "order": 0, "full_width": True},
    {"key": "menu_manager",       "label": "📋 User Menu Manager",   "callback": "mm:menu",
     "row": 8, "order": 0, "full_width": False},
    {"key": "admin_menu_manager", "label": "🗂 Admin Menu Manager",   "callback": "mm:amgr",
     "row": 8, "order": 1, "full_width": False},
    {"key": "activity_feed",      "label": "📝 Activity Logs",        "callback": "af:menu",
     "row": 9, "order": 0, "full_width": True},
    {"key": "exit_admin",         "label": "🔙 Exit Admin",           "callback": "main_menu",
     "row": 10, "order": 0, "full_width": True},
], description="Admin panel — main navigation")

# ---------------------------------------------------------------------------
# Admin product management menu
# ---------------------------------------------------------------------------
register_menu("admin_products_menu", [
    {"key": "create_product",    "label": "➕ Create Product",       "callback": "admin_create_product",
     "row": 1, "order": 0, "full_width": True},
    {"key": "edit_product",      "label": "✏️ Edit Product",         "callback": "admin_edit_product",
     "row": 2, "order": 0, "full_width": True},
    {"key": "manage_variants",   "label": "🎛️ Manage Variants",      "callback": "admin_variants",
     "row": 3, "order": 0, "full_width": True},
    {"key": "manage_inventory",  "label": "📦 Manage Inventory",     "callback": "admin_manage_inventory",
     "row": 4, "order": 0, "full_width": True},
    {"key": "manage_categories", "label": "📁 Manage Categories",    "callback": "admin_manage_categories",
     "row": 5, "order": 0, "full_width": True},
    {"key": "bulk_import",       "label": "📥 Bulk Import/Export",   "callback": "bpim:menu",
     "row": 6, "order": 0, "full_width": True},
    {"key": "back_admin",        "label": "🔙 Back",                  "callback": "admin_menu",
     "row": 7, "order": 0, "full_width": True},
], description="Admin product management sub-menu")

# ---------------------------------------------------------------------------
# Admin category management menu
# ---------------------------------------------------------------------------
register_menu("admin_category_menu", [
    {"key": "create_category",    "label": "➕ Create Category",    "callback": "admin_create_category",
     "row": 1, "order": 0, "full_width": True},
    {"key": "create_subcategory", "label": "➕ Create Subcategory", "callback": "admin_create_subcategory",
     "row": 2, "order": 0, "full_width": True},
    {"key": "edit_category",      "label": "✏️ Edit Category",      "callback": "admin_edit_category",
     "row": 3, "order": 0, "full_width": True},
    {"key": "edit_subcategory",   "label": "✏️ Edit Subcategory",   "callback": "admin_edit_subcategory",
     "row": 4, "order": 0, "full_width": True},
    {"key": "view_categories",    "label": "📋 View Categories",    "callback": "admin_view_categories",
     "row": 5, "order": 0, "full_width": True},
    {"key": "back_products",      "label": "🔙 Back",               "callback": "admin_products",
     "row": 6, "order": 0, "full_width": True},
], description="Admin category management sub-menu")

# ---------------------------------------------------------------------------
# Admin user management menu
# ---------------------------------------------------------------------------
register_menu("admin_user_menu", [
    {"key": "users_list",       "label": "📋 Users List",          "callback": "usr:list:0:desc",
     "row": 1, "order": 0, "full_width": True},
    {"key": "user_search",      "label": "🔍 User Search",         "callback": "usr:search",
     "row": 2, "order": 0, "full_width": True},
    {"key": "manual_payments",  "label": "📝 Manual Payments",     "callback": "mp:list:0:desc",
     "row": 3, "order": 0, "full_width": True},
    {"key": "bulk_user_mgr",    "label": "👥 Bulk User Manager",   "callback": "bum:menu",
     "row": 4, "order": 0, "full_width": True},
    {"key": "return_admin",     "label": "↩️ Return",              "callback": "admin_menu",
     "row": 5, "order": 0, "full_width": True},
], description="Admin user management sub-menu")

# ---------------------------------------------------------------------------
# Admin order management menu
# ---------------------------------------------------------------------------
register_menu("admin_order_menu", [
    {"key": "view_all_orders",     "label": "📋 View All Orders",      "callback": "admin_view_orders",
     "row": 1, "order": 0, "full_width": True},
    {"key": "view_disputes",       "label": "🚨 View Disputes",         "callback": "admin_view_disputes",
     "row": 2, "order": 0, "full_width": True},
    {"key": "manual_confirmation", "label": "✅ Manual Confirmation",   "callback": "admin_confirm_order",
     "row": 3, "order": 0, "full_width": True},
    {"key": "cancel_order",        "label": "❌ Cancel Order",           "callback": "admin_cancel_order",
     "row": 4, "order": 0, "full_width": True},
    {"key": "back_admin_orders",   "label": "🔙 Back",                   "callback": "admin_menu",
     "row": 5, "order": 0, "full_width": True},
], description="Admin order management sub-menu")

# ---------------------------------------------------------------------------
# Admin store settings menu
# Note: "currency_toggle" label is computed at render time by keyboards.py
# via ``runtime_overrides`` — the label stored here is just a fallback.
# ---------------------------------------------------------------------------
register_menu("admin_settings_menu", [
    {"key": "welcome_msg",       "label": "💬 Welcome Message",            "callback": "admin_welcome_msg",
     "row": 1,  "order": 0, "full_width": True},
    {"key": "store_logo",        "label": "🖼 Store Logo",                  "callback": "admin_store_logo",
     "row": 2,  "order": 0, "full_width": True},
    {"key": "support_username",  "label": "📞 Support Username",            "callback": "admin_support_username",
     "row": 3,  "order": 0, "full_width": True},
    {"key": "channel_username",  "label": "📢 Channel Username",            "callback": "admin_channel_username",
     "row": 4,  "order": 0, "full_width": True},
    {"key": "coupons",           "label": "🎟 Coupons / Promo Codes",       "callback": "admin_coupons",
     "row": 5,  "order": 0, "full_width": True},
    {"key": "display_currency",  "label": "💱 Display Currency",            "callback": "admin_currency",
     "row": 6,  "order": 0, "full_width": True},
    {"key": "currency_toggle",   "label": "🌐 Currency Toggle Button",      "callback": "admin_toggle_currency_btn",
     "row": 7,  "order": 0, "full_width": True},
    {"key": "referral_reward",   "label": "👑 Referral Reward",             "callback": "admin_referral_reward",
     "row": 8,  "order": 0, "full_width": True},
    {"key": "referral_toggle",   "label": "🔁 Toggle Referral Program",     "callback": "admin_referral_toggle",
     "row": 9,  "order": 0, "full_width": True},
    {"key": "loyalty_program",   "label": "🎁 Loyalty Program",             "callback": "admin_loyalty",
     "row": 10, "order": 0, "full_width": True},
    {"key": "delivery_msg_builder", "label": "📐 Delivery Message Builder",  "callback": "dmb:menu",
     "row": 11, "order": 0, "full_width": True},
    {"key": "accdel_settings",   "label": "📧 Account Delivery Settings",   "callback": "accdel:menu",
     "row": 12, "order": 0, "full_width": True},
    {"key": "bot_config",        "label": "🛠 Bot Configuration",           "callback": "admin_bot_config",
     "row": 13, "order": 0, "full_width": True},
    {"key": "back_settings",     "label": "🔙 Back",                        "callback": "admin_menu",
     "row": 14, "order": 0, "full_width": True},
], description="Admin store settings sub-menu")

# ---------------------------------------------------------------------------
# Admin broadcast menu
# ---------------------------------------------------------------------------
register_menu("admin_broadcast_menu", [
    {"key": "broadcast_text",  "label": "💬 Text Only Broadcast",    "callback": "admin_broadcast_text",
     "row": 1, "order": 0, "full_width": True},
    {"key": "broadcast_image", "label": "🖼 Image + Text Broadcast", "callback": "admin_broadcast_image",
     "row": 2, "order": 0, "full_width": True},
    {"key": "back_broadcast",  "label": "🔙 Back",                   "callback": "admin_menu",
     "row": 3, "order": 0, "full_width": True},
], description="Admin broadcast sub-menu")

# ---------------------------------------------------------------------------
# User account / profile menu (ua:profile callback leads here)
# Defined dynamically so future sections can be added without touching
# any handler code — just register them here.
# ---------------------------------------------------------------------------
register_menu("user_account_menu", [
    {"key": "ua_profile",    "label": "👤 My Profile",        "callback": "ua:profile",
     "row": 1, "order": 0, "full_width": True},
    {"key": "ua_orders",     "label": "📜 Order History",     "callback": "order_history",
     "row": 2, "order": 0, "full_width": True},
    {"key": "ua_wallet",     "label": "💳 Wallet",            "callback": "wallet",
     "row": 3, "order": 0, "full_width": True},
    {"key": "ua_refer",      "label": "🎁 Refer & Earn",      "callback": "refer",
     "row": 4, "order": 0, "full_width": True},
    {"key": "ua_back",       "label": "🏠 Main Menu",         "callback": "main_menu",
     "row": 5, "order": 0, "full_width": True},
], description="User account / profile menu")
