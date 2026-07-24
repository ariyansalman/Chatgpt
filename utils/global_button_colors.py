"""Global colored-button patch (Bot API 9.4 `style` field).

``utils/keyboards.py`` already colors the main menu via its own
``_styled_button()`` helper. This module extends the *same* colored-button
feature to every other ``InlineKeyboardButton`` created anywhere in the
codebase (150+ handler files, ~3000 buttons total) WITHOUT editing every
call site by hand.

How it works
------------
We monkey-patch ``telegram.InlineKeyboardButton.__init__`` so that any
button built *without* an explicit ``style=`` kwarg gets a sensible
default color, picked from its ``callback_data`` (or its text, for URL
buttons that have no callback_data) using simple keyword rules. Buttons
that already pass ``style=`` explicitly -- the main menu, and any
admin-defined custom buttons -- are left completely alone.

Import this once, early, before the bot starts polling/serving webhooks
(see ``bot.py``). Because this patches the *class's* ``__init__``
(shared by every reference to it), it doesn't matter which order other
modules import ``InlineKeyboardButton`` in -- only that this patch has
run before any button is actually *constructed*, which happens later,
while handling updates.

Admin toggle: Admin Panel -> Menu Manager -> "🌈 All Bot Buttons"
(stored as ``global_button_colors_enabled`` in bot_config, default ON).
Turning it off restores plain, uncolored buttons everywhere this patch
covers. It's independent from ``main_menu_colors_enabled``, which only
controls the main menu.
"""

import logging

from telegram import InlineKeyboardButton as _Btn

logger = logging.getLogger(__name__)

_ORIG_INIT = _Btn.__init__
_PATCHED = False

# Ordered (style, keywords) rules, matched against callback_data first
# and button text as a fallback. First match wins, so destructive /
# money-related actions are listed before the generic default.
_RULES = [
    ("danger", (
        "delete", "remove", "ban", "cancel", "refund", "reject", "decline",
        "block", "deny", "revoke", "clear", "discard", "no_", "_no",
        "stop", "disable", "suspend", "delete_account",
    )),
    ("success", (
        "confirm", "approve", "accept", "pay", "buy", "purchase", "checkout",
        "submit", "save", "add_to_cart", "add_cart", "complete", "done",
        "yes_", "_yes", "topup", "deposit", "withdraw", "claim", "redeem",
        "apply", "activate", "enable",
    )),
]
_DEFAULT_STYLE = "primary"  # everything else: back, menu, view, list, page...


def _pick_style(callback_data, text):
    haystack = (callback_data or text or "").lower()
    for style, keywords in _RULES:
        if any(kw in haystack for kw in keywords):
            return style
    return _DEFAULT_STYLE


def _colors_enabled() -> bool:
    try:
        from utils.bot_config import cfg
        return cfg.get_bool("global_button_colors_enabled", True)
    except Exception:
        # Config not ready yet (e.g. very early startup) -- default to on.
        return True


def _patched_init(self, text, *args, **kwargs):
    if "style" not in kwargs and _colors_enabled():
        kwargs["style"] = _pick_style(kwargs.get("callback_data"), text)
    try:
        _ORIG_INIT(self, text, *args, **kwargs)
    except TypeError:
        # Installed python-telegram-bot < 22.7 doesn't know `style` /
        # `icon_custom_emoji_id` yet -- degrade to a plain button instead
        # of crashing every keyboard in the bot.
        kwargs.pop("style", None)
        kwargs.pop("icon_custom_emoji_id", None)
        _ORIG_INIT(self, text, *args, **kwargs)


def install() -> None:
    """Patch InlineKeyboardButton once. Safe to call more than once."""
    global _PATCHED
    if _PATCHED:
        return
    _Btn.__init__ = _patched_init
    _PATCHED = True
    logger.info("Global button colors patch installed (Bot API 9.4 style).")


install()
