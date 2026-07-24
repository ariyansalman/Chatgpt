"""Safe wrapper around CallbackQuery.edit_message_text.

Telegram raises ``BadRequest: Message is not modified`` whenever a bot tries
to edit a message with text/markup that is byte-for-byte identical to what's
already on screen — e.g. a user tapping "🔙 Back" and landing on the exact
same menu they came from. This is not a real error, just Telegram refusing a
no-op edit, but python-telegram-bot surfaces it as an unhandled exception if
callers don't guard for it. ``safe_edit_message_text`` swallows *only* that
specific case and re-raises everything else so real failures aren't hidden.
"""

from __future__ import annotations

import logging

from telegram import CallbackQuery
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


async def safe_edit_message_text(query: CallbackQuery, text: str, **kwargs):
    """Call ``query.edit_message_text`` and ignore a harmless 'not modified' error.

    Usage is a drop-in replacement:
        await safe_edit_message_text(query, text, reply_markup=..., parse_mode="HTML")
    """
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.debug("Ignored no-op edit_message_text for query %s", getattr(query, "id", "?"))
            return None
        raise
