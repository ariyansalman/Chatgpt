"""Global dynamic colored-button patch (Bot API 9.4 ``style`` field).

The patch keeps color behavior centralized for all keyboard call sites. A
button with an explicit ``style`` remains untouched; every other button gets
its persistent per-button color override or an automatic default. Callback
data and all business behavior remain unchanged.
"""

import logging

from telegram import InlineKeyboardButton as _Btn

from .button_colors import get_button_color, telegram_style_for_color

logger = logging.getLogger(__name__)

_ORIG_INIT = _Btn.__init__
_PATCHED = False


def _patched_init(self, text, *args, **kwargs):
    if "style" not in kwargs:
        button_id = kwargs.get("callback_data") or text
        # get_button_color() is the single source of truth for whether a
        # color renders at all. It returns None outright when the global
        # toggle is off, so telegram_style_for_color(None) always yields
        # None here too -- no separate toggle check needed in this file.
        kwargs["style"] = telegram_style_for_color(
            get_button_color(button_id, text=text)
        )
    try:
        _ORIG_INIT(self, text, *args, **kwargs)
    except TypeError:
        # Keep compatibility with older python-telegram-bot installations.
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
    logger.info("Global dynamic button colors patch installed.")


install()