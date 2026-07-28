"""Central registry for the bot's user-facing Main Menu.

This is the single source of truth for *which* items can appear on the
Main Menu and *where*. ``utils.keyboards.create_main_menu_keyboard`` no
longer hardcodes buttons/rows in code -- it simply renders whatever this
registry returns. Adding, removing, relabeling, or reordering a built-in
menu item never requires touching keyboards.py.

Each entry is a plain dict:

    key             stable id. Reused (unchanged) as the
                     ``menu_item_<key>_enabled`` / ``_style`` /
                     ``_emoji_id`` bot_config toggle names, so every
                     existing admin setting keeps working as-is.
    label_key       i18n key resolved through ``i18n.t()``. Use ``label``
                     instead for a raw, non-translated string.
    callback        callback_data for the button (mutually exclusive
                     with ``url``).
    url             external URL for the button (mutually exclusive
                     with ``callback``).
    row             1-based row number. Items sharing a row are packed
                     left-to-right in ``order`` sequence.
    order           sort key within a row.
    full_width      render alone on its own row.
    admin_only      only rendered for admins (checked in addition to
                     whatever caller-side admin check already applies).
    default_enabled initial value the *first* time
                     ``menu_item_<key>_enabled`` is read. Matches the
                     previous hardcoded default (every item on) unless
                     stated otherwise.

No business logic, callback names, or callback behavior is defined or
changed here -- this module only describes menu *layout*.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Matches one-or-more leading emoji already baked into a label, so that
# applying an emoji override never duplicates it (e.g. "🛒 Products" with
# override "🛍" becomes "🛍 Products", not "🛍 🛒 Products").
# This is the single shared definition — utils.menu_builder reuses it so
# every menu in the bot (main menu and admin sub-menus alike) strips
# leading emoji the same way.
LEADING_EMOJI_RE = re.compile(
    r"^\s*(?:[\U0001F1E6-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]+\s*)+"
)

# ─────────────────────────────────────────────────────────────────────────────
# Built-in defaults -- mirrors the pre-existing hardcoded layout exactly,
# so converting to this registry is a pure refactor with no visible change
# in behavior until an admin opts into the dynamic override below.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MENU_ITEMS: List[Dict[str, Any]] = [
    # Row 1 — Products (full-width)
    {"key": "products", "label_key": "main_menu.products", "callback": "products",
     "row": 1, "order": 0, "full_width": True, "emoji": "🛍️"},

    # Row 2 — Add Funds | Wallet
    {"key": "topup", "label_key": "main_menu.topup", "callback": "topup",
     "row": 2, "order": 0, "emoji": "💵"},
    {"key": "wallet", "label_key": "main_menu.wallet", "callback": "wallet",
     "row": 2, "order": 1, "emoji": "👛"},

    # Row 3 — My Orders | Referrals
    {"key": "orders", "label_key": "main_menu.order_history", "callback": "order_history",
     "row": 3, "order": 0, "emoji": "📦"},
    {"key": "refer", "label_key": "main_menu.refer", "callback": "refer",
     "row": 3, "order": 1, "emoji": "👥"},

    # Row 4 — Language | Support Center
    {"key": "language", "label_key": "language.menu_button", "callback": "language_menu",
     "row": 4, "order": 0, "emoji": "🌐"},
    {"key": "support", "label_key": "main_menu.support", "callback": "support_center",
     "row": 4, "order": 1, "emoji": "🎧"},

    # NOTE: the old full-width "👤 Profile" main-menu button has been
    # retired as part of a Main Menu simplification pass -- the primary
    # menu now only surfaces the handful of most-used actions. Profile /
    # Account info still lives at the same place it always has (see
    # handlers/account_features.py: account_menu / user_profile, callback
    # "ua:profile", unchanged), just reached via the /profile command
    # instead of a dedicated button, so it doesn't compete for space here.

    # Row 5 — Admin Panel (full-width, admin-only)
    {"key": "admin", "label_key": "main_menu.admin_panel", "callback": "admin_menu",
     "row": 5, "order": 0, "full_width": True, "admin_only": True, "emoji": "🛠️"},
]


class MenuConfigError(RuntimeError):
    """Raised when a menu configuration dictionary is structurally invalid.

    This is deliberately raised with a specific, actionable message so a bad
    edit to a menu registry fails loudly and clearly at import time, instead
    of surfacing later as a confusing ``KeyError``/``AttributeError`` from
    whatever code happens to touch the malformed entry first.
    """


def _validate_default_menu_items(items: List[Dict[str, Any]]) -> None:
    """Fail fast, with a clear message, if DEFAULT_MENU_ITEMS is malformed.

    Every entry must have a unique, non-empty ``key`` (it's reused verbatim
    as the ``menu_item_<key>_*`` bot_config setting names) plus enough
    fields to actually render a button. This does NOT require every
    historical key to still be present -- retiring a menu item (e.g.
    "account", see below) is a valid, intentional layout change.
    """
    seen_keys: set[str] = set()
    for idx, item in enumerate(items):
        key = item.get("key")
        if not key or not isinstance(key, str):
            raise MenuConfigError(
                f"utils/menu_registry.py: DEFAULT_MENU_ITEMS[{idx}] is "
                "missing a valid non-empty 'key' field."
            )
        if key in seen_keys:
            raise MenuConfigError(
                "utils/menu_registry.py: DEFAULT_MENU_ITEMS has a duplicate "
                f"menu item key: {key!r}."
            )
        seen_keys.add(key)
        if not item.get("callback") and not item.get("url"):
            raise MenuConfigError(
                f"utils/menu_registry.py: menu item {key!r} defines neither "
                "'callback' nor 'url' -- it can't render an actionable "
                "button."
            )
        if not item.get("label_key") and not item.get("label"):
            raise MenuConfigError(
                f"utils/menu_registry.py: menu item {key!r} defines neither "
                "'label_key' nor 'label' -- it has no text to display."
            )


_validate_default_menu_items(DEFAULT_MENU_ITEMS)

# bot_config key holding an optional JSON override of the list above.
# Shape: a JSON array of objects using the same fields documented at the
# top of this file. Admins/future tooling can add or reposition built-in
# items purely through config -- no deploy required.
_REGISTRY_CFG_KEY = "main_menu_items_json"

# Each audience owns a complete presentation profile.  The profile contains
# only menu metadata; callbacks and all feature behavior remain in the
# existing handlers.
MENU_AUDIENCES = ("users", "premium", "admins")
MENU_AUDIENCE_LABELS = {
    "users": "Users",
    "premium": "Premium Users",
    "admins": "Admins",
}
_PROFILE_CFG_KEYS = {
    "users": "main_menu_layout_users_json",
    "premium": "main_menu_layout_premium_json",
    "admins": "main_menu_layout_admins_json",
}

# ─────────────────────────────────────────────────────────────────────────────
# Deploy-time auto-sync
# ─────────────────────────────────────────────────────────────────────────────
#
# Profiles (Users/Premium/Admins) are saved to the DATABASE (bot_config), not
# the codebase. Editing DEFAULT_MENU_ITEMS and deploying new code therefore
# has no visible effect on its own -- old customizations (emoji, colors,
# labels) saved from a previous session keep overriding the new code's
# defaults, which looks like "the deploy didn't work." Bumping
# MENU_DEFAULTS_VERSION whenever DEFAULT_MENU_ITEMS changes lets startup
# detect that mismatch once per version and resync automatically -- no
# admin button-press required after a deploy. Manual customizations made
# *after* that automatic sync are left alone until the version is bumped
# again.
MENU_DEFAULTS_VERSION = 3
_MENU_DEFAULTS_VERSION_CFG_KEY = "main_menu_defaults_version"


def reset_all_profiles_to_defaults() -> None:
    """Rebuild every audience profile's built-in items from
    :data:`DEFAULT_MENU_ITEMS`. Custom buttons are preserved untouched.

    Context-free (no Telegram Update needed) so it can run both from the
    admin's "Reset to Default" button and automatically at bot startup.
    """
    from utils.bot_config import cfg
    from utils.button_colors import DEFAULT_MENU_ITEM_COLORS
    for audience in MENU_AUDIENCES:
        existing_layout = get_menu_layout(audience)
        items = deepcopy(DEFAULT_MENU_ITEMS)
        if audience in {"users", "premium"}:
            items = [item for item in items if not item.get("admin_only")]
        for item in items:
            item.update({
                "visible": True,
                "enabled": True,
                "style": DEFAULT_MENU_ITEM_COLORS.get(item["key"], "none"),
            })
        save_menu_layout(audience, {
            "items": items,
            "custom_buttons": existing_layout.get("custom_buttons", []),
            "colors_enabled": True,
        })
    try:
        cfg.set("main_menu_status", "enabled")
    except Exception:
        logger.debug("Could not reset main_menu_status", exc_info=True)


def sync_menu_defaults_on_startup() -> None:
    """Call once during bot startup (after bot_config is ready).

    If the stored profiles were last synced from an older
    ``MENU_DEFAULTS_VERSION``, resync them to the current
    ``DEFAULT_MENU_ITEMS`` now, then record the new version so this is a
    no-op on every subsequent restart until the version is bumped again.
    """
    try:
        from utils.bot_config import cfg
        stored = cfg.get_str(_MENU_DEFAULTS_VERSION_CFG_KEY, "")
        current_stored_version = int(stored) if stored.strip().isdigit() else 0
        if current_stored_version >= MENU_DEFAULTS_VERSION:
            return
        reset_all_profiles_to_defaults()
        cfg.set(_MENU_DEFAULTS_VERSION_CFG_KEY, str(MENU_DEFAULTS_VERSION))
        logger.info(
            "Main menu profiles auto-synced to defaults (v%s -> v%s).",
            current_stored_version, MENU_DEFAULTS_VERSION,
        )
    except Exception:
        logger.exception("Main menu auto-sync failed; menus keep their last saved state")


def get_menu_items() -> List[Dict[str, Any]]:
    """Return the active menu-item registry.

    Reads the optional ``main_menu_items_json`` override from bot_config.
    Falls back to :data:`DEFAULT_MENU_ITEMS` if no override is set, the
    override is empty/invalid, or bot_config isn't reachable (e.g. during
    early startup) -- so the Main Menu always has something to render.
    """
    try:
        from utils.bot_config import cfg
        raw = cfg.get_str(_REGISTRY_CFG_KEY, "")
        if raw and raw.strip():
            items = json.loads(raw)
            if isinstance(items, list) and items:
                return items
            logger.warning("main_menu_items_json override is empty/invalid; using defaults")
    except Exception:
        logger.debug("menu_registry: falling back to DEFAULT_MENU_ITEMS", exc_info=True)
    return DEFAULT_MENU_ITEMS


def _cfg_get(key: str, default: str = "") -> str:
    try:
        from utils.bot_config import cfg
        return cfg.get_str(key, default)
    except Exception:
        return default


def normalize_menu_audience(audience: str) -> str:
    """Return the canonical profile name used by storage and rendering."""
    aliases = {
        "user": "users",
        "regular": "users",
        "premium_users": "premium",
        "premium-user": "premium",
        "admin": "admins",
    }
    value = str(audience or "").strip().lower()
    return aliases.get(value, value if value in MENU_AUDIENCES else "users")


def get_menu_audience(user_id: int) -> str:
    """Resolve the one menu profile a viewer should receive."""
    try:
        from utils.helpers import is_admin
        if user_id is not None and is_admin(user_id):
            return "admins"
    except Exception:
        pass
    return "premium" if _is_premium_user(user_id) else "users"


def _legacy_layout(audience: str) -> Dict[str, Any]:
    """Build a profile from the pre-profile configuration."""
    audience = normalize_menu_audience(audience)
    items = []
    for source in get_menu_items():
        item = deepcopy(source)
        key = item["key"]
        item.update({
            "visible": _cfg_get(
                f"menu_item_{key}_visible",
                "true" if item.get("default_visible", True) else "false",
            ).strip().lower() in ("1", "true", "yes", "on", "y", "t"),
            "enabled": _cfg_get(
                f"menu_item_{key}_enabled",
                "true" if item.get("default_enabled", True) else "false",
            ).strip().lower() in ("1", "true", "yes", "on", "y", "t"),
            "label": _cfg_get(f"menu_item_{key}_label", ""),
            "emoji": _cfg_get(f"menu_item_{key}_emoji", item.get("emoji", "")),
            "emoji_id": _cfg_get(f"menu_item_{key}_emoji_id", ""),
            "style": _cfg_get(f"menu_item_{key}_style", ""),
        })
        legacy_audience = _cfg_get(
            f"menu_item_{key}_audience",
            "admin" if item.get("admin_only") else item.get("audience", "all"),
        ).strip().lower()
        if audience == "admins":
            allowed = legacy_audience in {"all", "admin"}
        elif audience == "premium":
            allowed = legacy_audience in {"all", "premium"}
        else:
            allowed = legacy_audience in {"all", "user"}
        item["visible"] = item["visible"] and allowed
        items.append(item)
    return {
        "items": items,
        "custom_buttons": _read_json_config("main_menu_custom_buttons", []),
        "colors_enabled": _cfg_get("main_menu_colors_enabled", "true").lower()
        in ("1", "true", "yes", "on"),
    }


def _read_json_config(key: str, default: Any) -> Any:
    raw = _cfg_get(key, "")
    if not raw.strip():
        return deepcopy(default)
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Invalid JSON in bot_config key %s; using fallback", key)
        return deepcopy(default)


def get_menu_layout(audience: str) -> Dict[str, Any]:
    """Return the independent presentation profile for an audience."""
    audience = normalize_menu_audience(audience)
    raw = _cfg_get(_PROFILE_CFG_KEYS[audience], "")
    if not raw.strip():
        return _legacy_layout(audience)
    try:
        layout = json.loads(raw)
        if isinstance(layout, dict) and isinstance(layout.get("items"), list):
            return layout
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Invalid menu profile for %s; using legacy settings", audience)
    return _legacy_layout(audience)


def save_menu_layout(audience: str, layout: Dict[str, Any]) -> None:
    """Persist a complete profile. Only menu presentation data is stored."""
    audience = normalize_menu_audience(audience)
    from utils.bot_config import cfg
    cfg.set(_PROFILE_CFG_KEYS[audience], json.dumps(layout, ensure_ascii=False))


def get_menu_items_for_audience(audience: str) -> List[Dict[str, Any]]:
    return [
        item for item in get_menu_layout(audience).get("items", [])
        if isinstance(item, dict) and item.get("key")
    ]


def get_custom_buttons_for_audience(audience: str) -> List[Dict[str, Any]]:
    buttons = get_menu_layout(audience).get("custom_buttons", [])
    return buttons if isinstance(buttons, list) else []


def menu_colors_enabled(audience: str) -> bool:
    return bool(get_menu_layout(audience).get("colors_enabled", True))


def get_item_label(item: Dict[str, Any], lang: str = "en") -> str:
    """Resolve a menu label, applying an admin-supplied rename and emoji."""
    if item.get("label"):
        label = str(item["label"])
    elif _cfg_get(f"menu_item_{item['key']}_label", ""):
        label = _cfg_get(f"menu_item_{item['key']}_label", "")
    elif item.get("label_key"):
        try:
            from i18n import t
            label = t(item["label_key"], lang)
        except Exception:
            label = str(item.get("label", item["key"]))
    else:
        label = str(item.get("label", item["key"]))

    emoji = item.get("emoji")
    if emoji is None:
        emoji = _cfg_get(f"menu_item_{item['key']}_emoji", item.get("emoji", ""))
    if emoji:
        # Built-in translations already begin with an emoji. Remove only that
        # leading visual token so changing the emoji does not duplicate it.
        without_emoji = LEADING_EMOJI_RE.sub("", label).strip()
        label = f"{emoji} {without_emoji}".strip()
    return label


def is_item_visible(item: Dict[str, Any]) -> bool:
    if "visible" in item:
        return bool(item["visible"])
    return _cfg_get(
        f"menu_item_{item['key']}_visible",
        "true" if item.get("default_visible", True) else "false",
    ).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def is_item_enabled(item: Dict[str, Any]) -> bool:
    if "enabled" in item:
        return bool(item["enabled"])
    return _cfg_get(
        f"menu_item_{item['key']}_enabled",
        "true" if item.get("default_enabled", True) else "false",
    ).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _is_premium_user(user_id: int) -> bool:
    """Use the bot's existing active subscription as its premium audience."""
    if user_id is None:
        return False
    try:
        from database import get_db_session
        from database.models import Subscription, User
        with get_db_session() as session:
            return session.query(Subscription.id).join(
                User, User.id == Subscription.user_id
            ).filter(
                User.telegram_id == user_id,
                Subscription.status == "active",
                Subscription.expires_at > datetime.utcnow(),
            ).first() is not None
    except Exception:
        logger.debug("Could not resolve premium menu audience", exc_info=True)
        return False


def sorted_rows(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group items into rows, ordered by row number then ``order``."""
    by_row: Dict[Any, List[Dict[str, Any]]] = {}
    for item in items:
        by_row.setdefault(item.get("row", 0), []).append(item)
    return [
        sorted(by_row[row_no], key=lambda it: it.get("order", 0))
        for row_no in sorted(by_row.keys())
    ]
