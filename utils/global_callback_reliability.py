"""Project-wide callback reliability & message-editing safety net.

This module centralizes fixes for the whole class of "button stopped
working" bugs (expired callback queries, invalid/renamed callback_data,
missing handlers, stuck/duplicate taps, failed message edits) WITHOUT
touching the ~570 individual ``CallbackQueryHandler`` registrations or the
~670 raw ``query.edit_message_text(...)`` call sites spread across the
handlers/ package. It follows the same "patch it once, centrally" pattern
already used by ``utils/global_button_colors.py`` for keyboard styling.

Three independent pieces, installed by calling ``install()`` once at
startup (before the ``Application`` is built) and ``register(application)``
once while building it:

1. ``CallbackQuery.edit_message_text`` is patched so that *every* call to
   it anywhere in the project — old or new, wrapped or not — automatically
   falls back to sending a brand-new message whenever the in-place edit is
   impossible (message deleted / too old / can't be edited / stale query
   / any other Telegram edit failure), instead of raising. See
   ``utils/safe_edit.py`` for the exact behavior.

2. ``CallbackQuery.answer`` is patched to never raise. A callback query
   that is already answered, expired ("query is too old"), or otherwise
   invalid is logged quietly instead of bubbling up as an unhandled
   exception.

3. ``register(application)`` adds two dispatcher-level handlers:
     • A group=-3 ``CallbackQueryHandler`` (matches every callback query,
       runs before anything else) that answers the tap immediately — so
       Telegram always clears the button's loading spinner within its
       response window, no matter how slow or broken the eventual handler
       turns out to be — and drops an immediate duplicate/double-tap of
       the same button before it can run business logic twice.
     • A catch-all ``CallbackQueryHandler`` added last in the default
       group, which only ever fires when no other handler (including
       every ConversationHandler's own state/fallback handlers) claimed
       the update — i.e. stale, renamed, or otherwise unroutable
       callback_data. Instead of Telegram just leaving the button inert,
       the user is answered and returned to the main menu.

Nothing here changes any handler's business logic, payment logic, or
callback_data. It only guarantees every tap gets a timely answer and every
screen either updates or is safely redrawn.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Tuple

from telegram import CallbackQuery, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

_PATCHED = False

# Telegram BadRequest strings that mean "this specific answer()/edit() call
# can't succeed, but it's not an application bug" — logged quietly rather
# than as a warning/exception.
_BENIGN_QUERY_ERRORS = (
    "query is too old",
    "query id is invalid",
    "query id invalid",
    "message is not modified",
    "callback query has already been answered",
)


def _is_benign(err: BaseException) -> bool:
    if not isinstance(err, BadRequest):
        return False
    msg = str(err).lower()
    return any(needle in msg for needle in _BENIGN_QUERY_ERRORS)


# ─────────────────────────────────────────────────────────────────────────
# 1) Global patch: CallbackQuery.edit_message_text always has a fallback
# ─────────────────────────────────────────────────────────────────────────

def _install_edit_patch() -> None:
    if getattr(CallbackQuery.edit_message_text, "_reliability_patched", False):
        return
    _orig_edit_message_text = CallbackQuery.edit_message_text

    async def _patched_edit_message_text(self, text, *args, **kwargs):
        # Reuse the exact same fallback logic as safe_edit_message_text,
        # but call the *original* unpatched method internally to avoid
        # recursion.
        try:
            return await _orig_edit_message_text(self, text, *args, **kwargs)
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                logger.debug("Ignored no-op edit_message_text for query %s", getattr(self, "id", "?"))
                return None
            logger.info(
                "Global patch: edit_message_text failed for query %s (%s) — "
                "sending a new message instead of crashing.",
                getattr(self, "id", "?"), e,
            )
        except TelegramError as e:
            logger.warning(
                "Global patch: Telegram error on edit_message_text for query %s: %s — "
                "sending a new message instead of crashing.",
                getattr(self, "id", "?"), e,
            )
        # Fall through to the shared fallback-send helper for every
        # non-"not modified" failure path above.
        from utils.safe_edit import _send_fallback_message
        return await _send_fallback_message(self, text, kwargs)

    _patched_edit_message_text._reliability_patched = True
    CallbackQuery.edit_message_text = _patched_edit_message_text
    logger.info("Global CallbackQuery.edit_message_text reliability patch installed.")


def _install_answer_patch() -> None:
    if getattr(CallbackQuery.answer, "_reliability_patched", False):
        return
    _orig_answer = CallbackQuery.answer

    async def _patched_answer(self, *args, **kwargs):
        try:
            return await _orig_answer(self, *args, **kwargs)
        except BadRequest as e:
            if not _is_benign(e):
                logger.warning("Unexpected error answering callback query %s: %s", getattr(self, "id", "?"), e)
            else:
                logger.debug("Benign answer() error for query %s: %s", getattr(self, "id", "?"), e)
            return False
        except TelegramError:
            logger.warning("Telegram error answering callback query %s", getattr(self, "id", "?"), exc_info=True)
            return False
        except Exception:  # noqa: BLE001 — an answer() failure must never crash a handler
            logger.exception("Unexpected error answering callback query %s", getattr(self, "id", "?"))
            return False

    _patched_answer._reliability_patched = True
    CallbackQuery.answer = _patched_answer
    logger.info("Global CallbackQuery.answer reliability patch installed.")


def install() -> None:
    """Patch CallbackQuery.edit_message_text / .answer once. Safe to call more than once."""
    global _PATCHED
    if _PATCHED:
        return
    _install_edit_patch()
    _install_answer_patch()
    _PATCHED = True


install()


# ─────────────────────────────────────────────────────────────────────────
# 2) Immediate-answer + duplicate-tap guard (runs before every other
#    handler, in its own handler group so it never shadows anything else)
# ─────────────────────────────────────────────────────────────────────────

_DEBOUNCE_WINDOW_SECONDS = 0.8
_MAX_TRACKED_TAPS_PER_USER = 40

# Per-process fallback store, used only for updates where PTB's per-user
# ``context.user_data`` isn't available (should not normally happen for a
# callback_query, but never risk an AttributeError over a debounce nicety).
_fallback_recent: Dict[Tuple[int, str], float] = {}


async def _callback_immediate_ack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer every callback query the instant it arrives, and drop an
    identical repeat tap that arrives while the first is still in flight.

    This runs in its own early handler group, so it executes for *every*
    callback query regardless of which (if any) handler downstream ends up
    matching it — the Telegram-side loading spinner clears immediately no
    matter how slow, broken, or entirely absent the eventual handler is.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    # Never let answering the tap itself block or crash the update.
    await query.answer()

    user = update.effective_user
    chat = getattr(query.message, "chat_id", None) if query.message else None
    now = time.monotonic()
    tap_key = (chat, query.data)

    try:
        recent = context.user_data.setdefault("_cb_recent_taps", {})
    except Exception:  # noqa: BLE001 — context.user_data unavailable for some reason
        uid = user.id if user else 0
        last = _fallback_recent.get((uid,) + tap_key)
        if last is not None and (now - last) < _DEBOUNCE_WINDOW_SECONDS:
            raise ApplicationHandlerStop
        _fallback_recent[(uid,) + tap_key] = now
        return

    last = recent.get(tap_key)
    if last is not None and (now - last) < _DEBOUNCE_WINDOW_SECONDS:
        logger.debug(
            "Dropping duplicate tap %r from user %s (double-tap within %.2fs)",
            query.data, user.id if user else "?", now - last,
        )
        # The tap was already answered above, so Telegram's UI is fine —
        # we just skip running any handler for it a second time.
        raise ApplicationHandlerStop
    recent[tap_key] = now

    # Keep the per-user debounce map from growing without bound over a
    # long session.
    if len(recent) > _MAX_TRACKED_TAPS_PER_USER:
        for k, _ in sorted(recent.items(), key=lambda kv: kv[1])[: len(recent) - _MAX_TRACKED_TAPS_PER_USER]:
            recent.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────
# 3) Catch-all fallback for stale / unroutable callback_data
# ─────────────────────────────────────────────────────────────────────────

async def _unhandled_callback_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler: only reached when no ConversationHandler state,
    fallback, or standalone CallbackQueryHandler claimed this callback_query
    (e.g. a renamed/removed callback_data, a button left over from before a
    bot restart/upgrade, or a Back/Cancel tap that fell outside every
    conversation's tracked states). Guarantees the tap is answered and the
    user lands on a working screen instead of a permanently inert button.
    """
    query = update.callback_query
    if query is None:
        return

    logger.warning(
        "Unhandled callback_data=%r from user=%s — no handler matched; routing to main menu.",
        query.data, update.effective_user.id if update.effective_user else None,
    )

    try:
        from utils import nav_state
        nav_state.reset(context)
    except Exception:  # noqa: BLE001
        pass

    try:
        from handlers.user_handlers import main_menu_callback
        await main_menu_callback(update, context)
        return
    except Exception:
        logger.exception("Fallback main_menu_callback failed for unhandled callback %r", query.data)

    # Absolute last resort if even the main-menu redraw failed.
    try:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ That button is no longer active. Send /start to return to the main menu.",
            )
    except Exception:  # noqa: BLE001
        logger.exception("Even the plain fallback message failed to send")


def register_immediate_ack(application) -> None:
    """Wire the group=-3 immediate-answer/debounce guard into ``application``.

    Safe to call as early as you like (right after building the
    ``Application``) — it lives in its own handler group, so it always
    runs first without shadowing anything registered afterwards.
    """
    application.add_handler(CallbackQueryHandler(_callback_immediate_ack), group=-3)


def register_catchall(application) -> None:
    """Wire the stale/unroutable-callback_data safety net into ``application``.

    IMPORTANT: this uses a pattern-less ``CallbackQueryHandler`` (matches
    every callback query) registered in the default group 0. Because PTB
    only runs the *first* matching handler within a group, this MUST be
    called last — after every other ``add_handler`` call for group 0
    (dedicated CallbackQueryHandlers and ConversationHandlers alike) — or
    it will shadow all of them instead of acting as a last resort. Call
    this immediately before ``application.add_error_handler(...)``.
    """
    application.add_handler(CallbackQueryHandler(_unhandled_callback_fallback), group=0)
