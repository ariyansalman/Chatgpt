"""Decorator that makes admin conversation steps crash-safe.

Wrap any handler that runs inside a ConversationHandler. If the wrapped
function raises, the user gets a friendly one-line error, ``user_data`` is
cleared, and the conversation ends cleanly — instead of leaving the admin
stuck in a broken state that intercepts every subsequent message.
"""

from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)


def safe_conversation(*, cleanup_keys: tuple = ()):
    def deco(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
            try:
                return await func(update, context, *a, **kw)
            except Exception as e:  # noqa: BLE001
                logger.exception("Conversation step %s crashed: %s", func.__name__, e)
                for k in cleanup_keys:
                    context.user_data.pop(k, None)
                text = f"❌ Something went wrong: {type(e).__name__}: {str(e)[:200]}"
                try:
                    if update.callback_query:
                        await update.callback_query.answer()
                        await update.callback_query.message.reply_text(text)
                    elif update.message:
                        await update.message.reply_text(text)
                except Exception:  # noqa: BLE001
                    pass
                return ConversationHandler.END
        return wrapper
    return deco


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global /cancel — escape any stuck conversation."""
    context.user_data.clear()
    if update.message:
        await update.message.reply_text(
            "✅ Cancelled. Any in-progress action has been reset."
        )
    return ConversationHandler.END
