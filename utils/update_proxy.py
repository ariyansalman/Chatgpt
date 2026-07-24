"""
Thin proxy helpers for python-telegram-bot v20+.

PTB v20 freezes all Telegram objects — you cannot set `.data` on a
CallbackQuery. The old pattern of `query.data = "target"; await handler(...)`
no longer works.

Use `with_data(update, new_data)` to produce a wrapper that presents a
different `.data` value without touching the frozen object:

    await some_handler(with_data(update, "target:foo:123"), context)

All other attributes (message, effective_user, etc.) pass through untouched.
"""

from __future__ import annotations
from typing import Any


class _QueryProxy:
    """Proxies every attribute of a CallbackQuery except `.data`."""

    __slots__ = ("_q", "_data")

    def __init__(self, query: Any, data: str) -> None:
        object.__setattr__(self, "_q", query)
        object.__setattr__(self, "_data", data)

    @property
    def data(self) -> str:  # type: ignore[override]
        return object.__getattribute__(self, "_data")

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_q"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # forward mutations to the underlying object so answer() etc. still work
        if name in ("_q", "_data"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_q"), name, value)


class _UpdateProxy:
    """Proxies every attribute of an Update, replacing callback_query."""

    __slots__ = ("_u", "_q")

    def __init__(self, update: Any, query_proxy: _QueryProxy) -> None:
        object.__setattr__(self, "_u", update)
        object.__setattr__(self, "_q", query_proxy)

    @property
    def callback_query(self) -> _QueryProxy:  # type: ignore[override]
        return object.__getattribute__(self, "_q")

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_u"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_u", "_q"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_u"), name, value)


def with_data(update: Any, new_data: str) -> _UpdateProxy:
    """Return a proxy update where ``callback_query.data == new_data``."""
    qp = _QueryProxy(update.callback_query, new_data)
    return _UpdateProxy(update, qp)
