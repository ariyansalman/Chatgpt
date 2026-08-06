"""Global conversation-state guard.

Problem
-------
This bot registers dozens of independent ``telegram.ext.ConversationHandler``
instances — one per input flow (Quantity, Deposit, Search, Coupon, Support
ticket, and every other multi-step prompt). Each one tracks *its own*
"what step is this user on" state completely independently, keyed by
``(chat_id, user_id)`` internally. Nothing tells one ConversationHandler
that the user just started a different flow, or that they pressed a
*global* navigation button (Main Menu, Products, Wallet, Support, /start).

Symptom this fixes
-------------------
A user gets halfway through, say, typing a purchase quantity, then taps
"Main Menu". The Main Menu renders correctly — that button is a plain
CallbackQueryHandler, not part of the quantity conversation — but the
quantity conversation's own internal state is never told the flow ended,
because global nav buttons aren't (and shouldn't have to be) registered as
a ``fallback`` on every single conversation in the codebase. The next
plain-text message the user sends (e.g. typing a product search, or a
Support message) is then silently swallowed by the still-active quantity
conversation instead of reaching the flow the user actually wants — an old
state "leaking" into a new one.

Fix
---
This module keeps a registry of every ConversationHandler the bot creates
(built automatically from ``application.handlers`` — see ``discover``).
A single, always-runs-first, never-blocks ``TypeHandler`` watches for the
small set of *global navigation* triggers — ``/start`` and the top-level
Main Menu / Products / Wallet / Support / Back buttons — and, only when one
of those fires, force-ends that user's tracked state in *every*
ConversationHandler before the real handler for the button runs. That
guarantees at most one ConversationHandler can ever be "waiting" on a given
user at a time.

Nothing about callback_data, business logic, database access, or UI is
touched. This purely resets which ConversationHandler (if any) is allowed
to intercept the user's *next* message.
"""

from __future__ import annotations

import logging
from typing import List

from telegram import Update
from telegram.ext import Application, ContextTypes, ConversationHandler, TypeHandler

logger = logging.getLogger(__name__)

# callback_data values that represent *global* navigation — i.e. the user is
# leaving whatever flow they were in to jump to a top-level screen. Sourced
# from utils/menu_registry.py's default item callbacks plus the handful of
# legacy/back aliases used throughout the codebase (utils/keyboards.py).
GLOBAL_NAV_CALLBACKS = {
    "main_menu",
    "back",
    "products",
    "wallet",
    "support_center",
    "support",
    "topup",
    "order_history",
    "refer",
    "language_menu",
    "admin_menu",
}

_registry: List[ConversationHandler] = []


def discover(application: Application) -> None:
    """Walk every handler group on ``application`` and record every
    ConversationHandler found. Call once, after *all* handlers (including
    ones added inside other modules' own ``register_handlers(application)``
    functions) have been registered. Safe to call more than once — it
    de-duplicates by identity, so re-running it after later handlers are
    added just picks up whatever is new.
    """
    seen = {id(c) for c in _registry}
    for group_handlers in application.handlers.values():
        for h in group_handlers:
            if isinstance(h, ConversationHandler) and id(h) not in seen:
                _registry.append(h)
                seen.add(id(h))


def _key_for(conv: ConversationHandler, update: Update):
    """Reproduce the (chat_id, user_id[, message_id]) key a given
    ConversationHandler would compute for this update, so we can look up
    (and clear) that exact entry in its internal state table.
    """
    getter = getattr(conv, "_get_key", None)
    if callable(getter):
        try:
            return getter(update)
        except Exception:
            pass
    # Manual fallback, replicating ConversationHandler's own key logic, in
    # case the private helper above is ever renamed by a future PTB release.
    key = []
    chat = update.effective_chat
    user = update.effective_user
    if getattr(conv, "per_chat", True) and chat is not None:
        key.append(chat.id)
    if getattr(conv, "per_user", True) and user is not None:
        key.append(user.id)
    if getattr(conv, "per_message", False) and update.callback_query is not None:
        key.append(
            update.callback_query.inline_message_id
            or update.callback_query.message.message_id
        )
    return tuple(key)


def end_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force every tracked ConversationHandler to forget this user's
    current step, wherever they were. Idempotent and safe to call on every
    update — handlers with no tracked state for this user are left alone.
    """
    for conv in _registry:
        try:
            key = _key_for(conv, update)
            conversations = getattr(conv, "_conversations", None)
            if not conversations or key not in conversations:
                continue  # this handler has no tracked step for this user

            # Prefer the (public, documented) update_state API — it also
            # handles persistence and cancels any pending timeout job for
            # us. Only reach into the private dict directly if that isn't
            # available for some reason.
            updater = getattr(conv, "update_state", None)
            if callable(updater):
                try:
                    updater(conv.END, key)
                    continue
                except Exception:
                    pass

            conversations.pop(key, None)
            timeout_jobs = getattr(conv, "_conversation_timeout_jobs", None)
            if timeout_jobs:
                job = timeout_jobs.pop(key, None)
                if job is not None:
                    try:
                        job.schedule_removal()
                    except Exception:
                        pass
        except Exception:
            logger.debug("conversation_guard: failed to reset %r", conv, exc_info=True)


def _is_global_nav(update: Update) -> bool:
    msg = update.message
    if msg is not None and msg.text:
        first_word = msg.text.split()[0] if msg.text.split() else ""
        if first_word.split("@")[0] == "/start":
            return True
    query = update.callback_query
    if query is not None and query.data in GLOBAL_NAV_CALLBACKS:
        return True
    return False


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_global_nav(update):
        end_all(update, context)


def install(application: Application, group: int = -4) -> None:
    """Register the guard so it runs before every other handler group.

    Group ``-4`` is deliberately earlier than every other group already in
    use by this bot (``-3`` immediate-ack, ``-2`` activity tracking, ``-1``
    maintenance/anti-spam gates, ``0`` everything else) so this guard is
    guaranteed to see — and act on — every update before anything else
    does, including before the group-``-3`` CallbackQueryHandler that would
    otherwise "consume" every callback query first and prevent a
    TypeHandler registered in that same group from ever running.

    Call once, at the very end of ``main()``, after every other
    ``add_handler`` / ``register_handlers`` call — that way ``discover()``
    is guaranteed to see the complete set of ConversationHandlers.
    """
    discover(application)
    application.add_handler(TypeHandler(Update, _guard), group=group)
    logger.info(
        "conversation_guard: installed, tracking %d ConversationHandler(s)",
        len(_registry),
    )
