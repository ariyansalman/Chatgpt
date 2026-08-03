"""Dynamic navigation-state tracking for the Admin Panel.

Problem this solves
--------------------
Every admin screen previously hardcoded its own "Back"/"Cancel"
``callback_data`` destination as a literal string baked into that
screen's keyboard. That works fine when a screen only ever has one
possible parent -- but several places in the codebase share one
generic handler (e.g. ``admin_conversations.cancel_conversation``) as
the fallback for multiple *unrelated* conversations, and that shared
handler could only point to one hardcoded destination. Result: cancel
an in-progress product edit and you'd land on the Category menu, not
the Products menu ("random menu switching").

This module does not replace any existing callback_data, route, or
menu-building function. It only stores *which screen the user is
currently in* / *which screen they should return to*, per user, in
``context.user_data`` (already the standard PTB per-user store), so a
shared handler can look this up instead of guessing.

Nothing here touches business logic, payment logic, wallet logic,
order logic, the database, APIs, routes, or permissions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_STACK_KEY = "_nav_stack"           # list[str] of callback_data, root-first
_CONV_HOME_KEY = "_nav_conv_home"   # str callback_data — where /cancel should land
_LAST_MSG_KEY = "_nav_last_msg"     # (chat_id, message_id) of the currently-shown admin screen


# ─────────────────────────────────────────────────────────────────────────
# Back stack (per-user breadcrumb trail)
# ─────────────────────────────────────────────────────────────────────────

def enter_screen(context, screen_cb: str) -> None:
    """Register that the user just navigated *to* ``screen_cb``.

    Call this at the top of a menu-rendering handler, right after you
    know which screen you're about to draw. Re-entering the screen
    already on top of the stack (e.g. a refresh tap) is a no-op -- it
    never pushes a duplicate frame. Re-entering a screen that's further
    down the stack (the user pressed Back, or otherwise returned to a
    screen they'd already visited) truncates everything above it, so
    the trail reflects where they actually are instead of accumulating
    stale duplicate frames that would throw off later parent_of/go_back
    lookups.
    """
    stack = context.user_data.setdefault(_STACK_KEY, [])
    if stack and screen_cb in stack:
        idx = len(stack) - 1 - stack[::-1].index(screen_cb)
        del stack[idx + 1:]
        return
    stack.append(screen_cb)


def parent_of(context, screen_cb: str, default: str = "admin_menu") -> str:
    """Return the callback_data that should be shown when the user
    presses Back while looking at ``screen_cb``.

    Looks the screen up in the tracked stack and returns whatever is
    directly beneath it (the screen that was open immediately before
    it). Falls back to ``default`` if the screen isn't tracked (e.g.
    the user deep-linked in, or the bot restarted and lost in-memory
    state) so Back is never left dangling.
    """
    stack = context.user_data.get(_STACK_KEY) or []
    if screen_cb in stack:
        idx = len(stack) - 1 - stack[::-1].index(screen_cb)
        if idx > 0:
            return stack[idx - 1]
        return default
    if len(stack) >= 2:
        return stack[-2]
    return default


def go_back(context, screen_cb: str, default: str = "admin_menu") -> str:
    """Pop ``screen_cb`` (and anything pushed after it) off the stack
    and return the callback_data of the screen the user lands on.
    """
    stack = context.user_data.get(_STACK_KEY) or []
    if screen_cb in stack:
        idx = len(stack) - 1 - stack[::-1].index(screen_cb)
        del stack[idx:]
    dest = stack[-1] if stack else default
    return dest


def reset(context, root_cb: str = "admin_menu") -> None:
    """Clear the breadcrumb trail back to a single root screen.

    Use on Cancel, or whenever a workflow exits safely and any deeper
    history it left behind should no longer be reachable via Back.
    """
    context.user_data[_STACK_KEY] = [root_cb]


# ─────────────────────────────────────────────────────────────────────────
# Conversation "home" (for shared /cancel fallbacks)
# ─────────────────────────────────────────────────────────────────────────

def set_conversation_home(context, screen_cb: str) -> None:
    """Record which menu a just-started conversation should return to
    if the admin cancels out of it. Call once, at the entry point.
    """
    context.user_data[_CONV_HOME_KEY] = screen_cb


def pop_conversation_home(context, default: str = "admin_menu") -> str:
    """Read and clear the recorded cancel-destination for the
    conversation that is ending right now.
    """
    return context.user_data.pop(_CONV_HOME_KEY, default)


# ─────────────────────────────────────────────────────────────────────────
# Duplicate-message prevention
# ─────────────────────────────────────────────────────────────────────────

def remember_message(context, chat_id: Any, message_id: Any) -> None:
    context.user_data[_LAST_MSG_KEY] = (chat_id, message_id)


def is_current_message(context, chat_id: Any, message_id: Any) -> bool:
    return context.user_data.get(_LAST_MSG_KEY) == (chat_id, message_id)


async def render(update, context, text: str, reply_markup=None, screen_cb: Optional[str] = None, **kwargs):
    """Preferred way to draw an admin screen: always edits the
    existing message when one is available (never opens a duplicate
    message thread), falls back to sending once if there's truly
    nothing to edit, and records the navigation frame.

    Drop-in for the common ``query.edit_message_text(...)`` call:
        await nav_state.render(update, context, text, reply_markup=kb, screen_cb="pd:list")
    """
    from utils.safe_edit import safe_edit_message_text

    if screen_cb:
        enter_screen(context, screen_cb)

    query = getattr(update, "callback_query", None)
    if query is not None:
        msg = await safe_edit_message_text(query, text, reply_markup=reply_markup, **kwargs)
        target = msg or getattr(query, "message", None)
        if target is not None:
            remember_message(context, target.chat_id, target.message_id)
        return target

    if update.message is not None:
        sent = await update.message.reply_text(text, reply_markup=reply_markup, **kwargs)
        remember_message(context, sent.chat_id, sent.message_id)
        return sent
    return None
