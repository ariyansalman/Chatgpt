"""Reliability helpers for Telegram inline-button (CallbackQuery) handlers.

Why this exists
────────────────────────────────────────────────────────────────────────
Users occasionally reported that tapping a payment-method button (Bybit
Pay, Binance Pay, Crypto Networks, Mobile Money, ...) left the button
highlighted with nothing happening. Root causes, all addressed here:

  1. A handler did real work (constructing a payment-gateway service,
     which reads config from the database) *before* calling
     ``query.answer()``. If that work was slow (DB contention, a locked
     row, a slow query) or raised, Telegram never received an answer for
     the tap in a timely fashion, so the client kept the button in its
     "loading" state.
  2. An unhandled exception raised anywhere in a callback handler bubbled
     up to the bot's generic error handler, which notifies the admin but
     has no way to know which screen to redraw for the user — so the tap
     silently went nowhere from the user's point of view.
  3. Fast double-taps (or a slow first tap plus an impatient second tap)
     could re-enter the same handler concurrently and create two orders /
     two invoices for a single top-up.

``guarded_callback`` wraps a CallbackQueryHandler-bound coroutine so that,
regardless of what the wrapped function does:

  • the callback query is answered immediately and exactly once, even if
    the handler raises before reaching its own ``query.answer()`` call;
  • a second overlapping tap from the same user is acknowledged (so the
    button never stays stuck) but the underlying handler is *not* run
    again while the first tap is still being processed — no duplicate
    request/order is created;
  • any exception raised while handling the tap is caught, logged, and
    turned into a short, friendly message instead of a silent hang or a
    raw crash, and the bot stays fully responsive for the next tap.

``safe_answer`` is the same idempotent, error-swallowing
``query.answer()`` used internally, exported so individual handlers that
need to do slow work (e.g. call out to a payment gateway) can answer the
tap immediately, *before* that work starts, and safely call it again
afterwards (e.g. to show a validation alert) without worrying about
double-answering an already-answered/expired query.
"""
from __future__ import annotations

import functools
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Errors Telegram returns for a callback query that is already
# answered / expired / no longer valid. Not actionable — retrying or
# alerting the admin about these would just be noise.
_BENIGN_ANSWER_ERRORS = (
    "query is too old",
    "query id is invalid",
    "query id invalid",
    "message is not modified",
)


def _is_benign(err: BaseException) -> bool:
    if not isinstance(err, BadRequest):
        return False
    msg = str(err).lower()
    return any(needle in msg for needle in _BENIGN_ANSWER_ERRORS)


async def safe_answer(query, text: Optional[str] = None, show_alert: bool = False) -> bool:
    """Answer a callback query, never raising.

    Safe to call more than once for the same query (e.g. once for an
    immediate blank ack, later with an alert message) — a benign
    "already answered / too old" error from Telegram is swallowed
    quietly. Returns True if the answer call reached Telegram
    successfully, False otherwise (caller usually doesn't need to care).
    """
    if query is None:
        return False
    try:
        if text is not None:
            await query.answer(text, show_alert=show_alert)
        else:
            await query.answer()
        return True
    except BadRequest as e:
        if not _is_benign(e):
            logger.warning("Unexpected error answering callback query: %s", e)
        return False
    except TelegramError:
        logger.warning("Telegram error answering callback query", exc_info=True)
        return False
    except Exception:  # noqa: BLE001 — never let an answer() failure crash a handler
        logger.exception("Unexpected error answering callback query")
        return False


def _retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])


def _busy_key(user_id) -> str:
    return f"_cb_busy::{user_id}"


def guarded_callback(fallback_state=None, busy_alert: str = "⏳ Still working on your last tap — one moment."):
    """Decorator for CallbackQueryHandler-bound coroutines.

    Args:
        fallback_state: the ConversationHandler state to return if the
            wrapped handler raises, or if this tap is dropped as a
            duplicate of one already in flight. Pass whatever the
            conversation's error/cancel path already uses (commonly
            ``ConversationHandler.END``).
        busy_alert: short toast shown when a duplicate/overlapping tap is
            ignored while the first one is still processing.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            query = getattr(update, "callback_query", None)
            user = update.effective_user
            uid = user.id if user else None
            key = _busy_key(uid) if uid is not None else None

            # ---- duplicate / overlapping tap guard -----------------------
            # If a previous tap from this user is still being processed
            # (e.g. they double-tapped, or tapped again while a slow
            # gateway/DB call from the first tap was still running), don't
            # re-enter the handler and don't create a second request.
            # Still answer immediately so the second tap's button never
            # stays stuck highlighted.
            if key is not None and context.user_data.get(key):
                await safe_answer(query, busy_alert)
                return fallback_state

            if key is not None:
                context.user_data[key] = True

            # PERF: acknowledge the tap immediately, before the wrapped handler
            # does any work (DB queries, gateway calls, etc.). Telegram shows the
            # button's loading spinner until answer() is received, so answering
            # up front — rather than only guaranteeing an eventual answer via the
            # except-block fallback below — is what makes every button feel
            # instant regardless of how long the handler's own processing takes.
            # safe_answer() is idempotent/never raises, and a handler that also
            # calls query.answer() itself later (e.g. with an alert) still works
            # fine — Telegram/​safe_answer just treats the repeat call as benign.
            await safe_answer(query)
            try:
                return await func(update, context, *args, **kwargs)
            except Exception:
                data = getattr(query, "data", None)
                logger.exception("Unhandled error in callback handler %r (data=%r)", func.__name__, data)

                # Guarantee a response even if the handler blew up before
                # ever calling query.answer() itself.
                await safe_answer(query)

                friendly = (
                    "⚠️ Something went wrong loading that option.\n"
                    "Please try again in a moment."
                )
                try:
                    if query is not None:
                        try:
                            await query.edit_message_text(friendly, reply_markup=_retry_keyboard())
                        except BadRequest:
                            # Original message may already be gone/unmodifiable —
                            # fall back to a fresh message so the user still
                            # sees *something* instead of dead silence.
                            if update.effective_chat:
                                await context.bot.send_message(
                                    chat_id=update.effective_chat.id, text=friendly,
                                    reply_markup=_retry_keyboard(),
                                )
                    elif update.effective_chat:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id, text=friendly,
                            reply_markup=_retry_keyboard(),
                        )
                except Exception:  # noqa: BLE001 — never let error reporting itself crash
                    logger.exception("Failed to send friendly error message to user")

                return fallback_state
            finally:
                if key is not None:
                    context.user_data.pop(key, None)
        return wrapper
    return decorator
