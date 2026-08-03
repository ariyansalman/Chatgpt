"""Central, persistent button-color manager.

The bot API currently exposes three background styles (success, primary and
danger).  The admin-facing color system intentionally has a richer, stable
palette so a button can keep its named color even when Telegram adds more
styles later.  ``telegram_style_for_color`` is the compatibility boundary:
unsupported named colors are rendered using the closest supported style while
their exact selection remains persisted.

This module only owns presentation metadata.  It never changes callback data,
permissions, routes, business logic, or database schema.
"""

from __future__ import annotations

import json
import random
from typing import Any


COLOR_DEFINITIONS = (
    ("green", "🟢 Green", "success"),
    ("blue", "🔵 Blue", "primary"),
    ("red", "🔴 Red", "danger"),
    ("yellow", "🟡 Yellow", "primary"),
    ("orange", "🟠 Orange", "danger"),
    ("purple", "🟣 Purple", "primary"),
    ("pink", "🩷 Pink", "danger"),
    ("black", "⚫ Black", "primary"),
    ("white", "⚪ White", None),
    ("brown", "🟤 Brown", "danger"),
    ("cyan", "🩵 Cyan", "primary"),
    ("lime", "💚 Lime", "success"),
)

COLOR_KEYS = tuple(color[0] for color in COLOR_DEFINITIONS)
COLOR_LABELS = {key: label for key, label, _ in COLOR_DEFINITIONS}
COLOR_EMOJIS = {key: label.split(" ", 1)[0] for key, label, _ in COLOR_DEFINITIONS}
TELEGRAM_STYLES = {
    key: telegram_style for key, _, telegram_style in COLOR_DEFINITIONS
}

_LEGACY_TO_COLOR = {
    "success": "green",
    "primary": "blue",
    "danger": "red",
    "none": "white",
    "default": "white",
    "": "white",
}
_COLOR_OVERRIDES_KEY = "button_color_overrides_json"

# Single source of truth for the built-in Main Menu items' default color.
# Every renderer and admin UI that needs "what color does this item start
# out as" reads this one table instead of keeping its own copy -- keyboards.py
# (real menu), the admin Menu Manager UI, and the bot_config seed defaults
# all derive from this dict so they can never drift apart.
DEFAULT_MENU_ITEM_COLORS: dict[str, str] = {
    "products": "green",
    "topup": "green",
    "wallet": "green",
    "orders": "blue",
    "support": "blue",
    "refer": "green",
    "account": "blue",
    "language": "blue",
    "settings": "blue",
    "admin": "red",
}


def normalize_color(value: Any, fallback: str = "white") -> str:
    """Return a canonical palette key while accepting old style names."""
    candidate = str(value or "").strip().lower()
    candidate = _LEGACY_TO_COLOR.get(candidate, candidate)
    return candidate if candidate in COLOR_KEYS else fallback


def color_label(value: Any) -> str:
    return COLOR_LABELS.get(normalize_color(value), COLOR_LABELS["white"])


def color_emoji(value: Any) -> str:
    return COLOR_EMOJIS.get(normalize_color(value), COLOR_EMOJIS["white"])


def telegram_style_for_color(value: Any) -> str | None:
    """Map a named color to the Bot API style supported by this installation."""
    return TELEGRAM_STYLES.get(normalize_color(value))


def _read_overrides() -> dict[str, str]:
    try:
        from utils.bot_config import cfg

        raw = cfg.get_str(_COLOR_OVERRIDES_KEY, "{}")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): normalize_color(value)
            for key, value in data.items()
            if str(key).strip()
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


def _write_overrides(overrides: dict[str, str]) -> None:
    from utils.bot_config import cfg

    cfg.set(
        _COLOR_OVERRIDES_KEY,
        json.dumps(
            {str(key): normalize_color(value) for key, value in overrides.items()},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def default_color_for_button(button_id: Any = "", text: Any = "") -> str:
    """Choose a stable default for a button without requiring registration.

    This keeps newly added buttons compatible automatically.  The rule is only
    a fallback; an explicit per-button override always wins.
    """
    haystack = f"{button_id or ''} {text or ''}".lower()
    if any(word in haystack for word in (
        "delete", "remove", "ban", "cancel", "refund", "reject", "decline",
        "block", "deny", "revoke", "clear", "discard", "stop", "disable",
        "suspend", "danger",
    )):
        return "red"
    if any(word in haystack for word in (
        "confirm", "approve", "accept", "pay", "buy", "purchase", "checkout",
        "submit", "save", "add_to_cart", "add_cart", "complete", "done",
        "yes_", "topup", "deposit", "withdraw", "claim", "redeem", "apply",
        "activate", "enable",
    )):
        return "green"
    return "blue"


_GLOBAL_COLORS_KEY = "global_button_colors_enabled"


def global_colors_enabled() -> bool:
    """The one place that answers "are bot-wide button colors ON?".

    Every keyboard builder that needs to decide whether to render a
    color should go through :func:`get_button_color` (which already
    calls this), not read the ``global_button_colors_enabled`` bot_config
    key directly -- keeping a single source of truth means the toggle
    can never drift out of sync between call sites again.
    """
    try:
        from utils.bot_config import cfg
        return cfg.get_bool(_GLOBAL_COLORS_KEY, True)
    except Exception:
        # Config may not be ready during early startup -- default to the
        # same "on" default the stored setting itself uses.
        return True


def get_button_color(
    button_id: Any = "",
    text: Any = "",
    fallback: Any = None,
) -> str | None:
    """The single source of truth for "what color should this button be".

    Every keyboard builder in the project should ask this function --
    directly or through :func:`telegram_style_for_color` fed by this
    function's return value -- instead of deciding colors on its own.

    Returns ``None`` when the global colors toggle is OFF. In that case
    nothing else runs: no per-button override, no automatic default, no
    random/cycle result -- callers must treat ``None`` as "render this
    button with Telegram's own default style, no exceptions". Stored
    per-button overrides are left completely untouched in the database;
    this function only ever decides whether they get *rendered* on this
    call, never whether they get *kept*.
    """
    if not global_colors_enabled():
        return None
    key = str(button_id or "").strip()
    if key:
        override = _read_overrides().get(key)
        if override:
            return override
    return normalize_color(
        fallback if fallback is not None else default_color_for_button(button_id, text),
        fallback="blue",
    )


def set_button_color(button_id: Any, color: Any) -> str:
    """Persist one button's color and return its canonical value."""
    key = str(button_id or "").strip()
    if not key:
        raise ValueError("button_id is required")
    canonical = normalize_color(color, fallback="blue")
    overrides = _read_overrides()
    overrides[key] = canonical
    _write_overrides(overrides)
    return canonical


def reset_button_color(button_id: Any) -> None:
    overrides = _read_overrides()
    overrides.pop(str(button_id or "").strip(), None)
    _write_overrides(overrides)


def reset_all_button_colors() -> None:
    _write_overrides({})


def cycle_color(value: Any) -> str:
    current = normalize_color(value, fallback=COLOR_KEYS[-1])
    return COLOR_KEYS[(COLOR_KEYS.index(current) + 1) % len(COLOR_KEYS)]


def random_color() -> str:
    return random.choice(COLOR_KEYS)