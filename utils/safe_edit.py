"""Safe wrapper around CallbackQuery.edit_message_text.

Telegram's ``edit_message_text`` can fail for a number of reasons that are
not really "errors" from the user's point of view — they just mean the
in-place edit can no longer happen and something else has to be shown
instead:

  • ``Message is not modified`` — the user tapped a button (e.g. "Back")
    that lands on the exact same text/markup already on screen. Not a
    failure at all, just Telegram refusing a no-op edit.
  • ``Message to edit not found`` / ``Message can't be edited`` — the
    original message was deleted (by the user, another admin, or a
    chat-cleanup job), is too old, or was never a bot message the API
    will let us touch.
  • ``Query is too old and response timeout expired`` — the callback
    itself is stale (long-running handler, bot restart while the tap was
    in flight, etc.); Telegram still lets *new* messages through even
    though it won't attach this particular edit to the old one.

Historically every one of these bubbled up as an unhandled ``BadRequest``,
which the global error handler could only convert into an apology — the
screen the user was looking at stayed stuck exactly as it was, and they
had to type /start again. ``safe_edit_message_text`` (and the matching
``telegram.CallbackQuery.edit_message_text`` monkeypatch installed by
``utils.global_callback_reliability``) fix that everywhere at once:
whenever the in-place edit truly cannot happen, a brand new message with
the same text/keyboard is sent instead, so the user is never left staring
at a dead screen.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from telegram import CallbackQuery
from telegram.error import BadRequest, Forbidden, TelegramError

logger = logging.getLogger(__name__)

# Telegram error strings where the *edit itself* is simply impossible and
# the only sane recovery is to send a fresh message instead of the edit.
_UNRECOVERABLE_EDIT_ERRORS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message identifier is not specified",
    "message to delete not found",
    "message can't be deleted",
    "chat not found",
    "query is too old",
    "query id is invalid",
    "message is too old",
)

# Send-message-compatible subset of edit_message_text kwargs. Everything
# else (e.g. inline_message_id-only concerns) is dropped on fallback.
_SEND_COMPATIBLE_KEYS = (
    "parse_mode",
    "entities",
    "disable_web_page_preview",
    "link_preview_options",
    "reply_markup",
    "message_thread_id",
    "business_connection_id",
)


async def _send_fallback_message(query: CallbackQuery, text: str, kwargs: dict) -> Optional[Any]:
    """Send a brand-new message carrying the same text/keyboard.

    Used whenever editing the original message is no longer possible.
    Returns the sent ``Message``, or ``None`` if there was truly nowhere
    to send it (e.g. a callback from an inline-mode result with no
    associated chat) or the send itself failed (e.g. bot blocked).
    """
    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        user = getattr(query, "from_user", None)
        chat_id = getattr(user, "id", None)
    if chat_id is None:
        logger.warning(
            "Cannot fall back to send_message for query %s — no chat context "
            "(likely an inline-mode callback with no reachable chat).",
            getattr(query, "id", "?"),
        )
        return None

    send_kwargs = {k: v for k, v in kwargs.items() if k in _SEND_COMPATIBLE_KEYS}
    try:
        bot = query.get_bot()
        return await bot.send_message(chat_id=chat_id, text=text, **send_kwargs)
    except TypeError:
        # Unexpected/incompatible kwarg for this PTB version — retry with
        # only the two fields that matter for every call site in this
        # project (text formatting + keyboard).
        minimal = {}
        if "parse_mode" in send_kwargs:
            minimal["parse_mode"] = send_kwargs["parse_mode"]
        if "reply_markup" in send_kwargs:
            minimal["reply_markup"] = send_kwargs["reply_markup"]
        try:
            bot = query.get_bot()
            return await bot.send_message(chat_id=chat_id, text=text, **minimal)
        except Exception:
            logger.exception("Fallback send_message (minimal kwargs) failed")
            return None
    except Forbidden:
        # User blocked the bot / left the chat — nothing more we can do.
        logger.info("Fallback send_message skipped: bot is blocked in chat %s", chat_id)
        return None
    except Exception:
        logger.exception("Fallback send_message failed after edit_message_text error")
        return None


async def safe_edit_message_text(query: CallbackQuery, text: str, **kwargs):
    """Edit a message, and if that's impossible for any reason, send a new one.

    Drop-in replacement for ``query.edit_message_text(...)``:
        await safe_edit_message_text(query, text, reply_markup=..., parse_mode="HTML")

    Guarantees:
      • A harmless "not modified" edit is treated as success (returns None,
        the on-screen message already shows this exact content).
      • Any other reason the edit can't happen (message gone, too old,
        chat gone, stale query, etc.) automatically falls back to sending
        a brand-new message with the same text/keyboard, so the user is
        never left looking at a dead screen.
      • Only truly unrecoverable situations (bot blocked, no chat at all)
        return ``None`` — logged, never raised.
    """
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            logger.debug("Ignored no-op edit_message_text for query %s", getattr(query, "id", "?"))
            return None
        if any(needle in msg for needle in _UNRECOVERABLE_EDIT_ERRORS):
            logger.info(
                "edit_message_text failed for query %s (%s) — sending a new message instead",
                getattr(query, "id", "?"), e,
            )
            return await _send_fallback_message(query, text, kwargs)
        # Unrecognized BadRequest — still don't leave the user stuck; log it
        # clearly (so it's easy to add to the recoverable list above) and
        # fall back the same way.
        logger.warning(
            "Unrecognized edit_message_text BadRequest for query %s: %s — falling back to send_message",
            getattr(query, "id", "?"), e,
        )
        return await _send_fallback_message(query, text, kwargs)
    except TelegramError as e:
        logger.warning(
            "Telegram error on edit_message_text for query %s: %s — falling back to send_message",
            getattr(query, "id", "?"), e,
        )
        return await _send_fallback_message(query, text, kwargs)
