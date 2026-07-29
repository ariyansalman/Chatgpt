"""Admin Panel Design System — v1.

A single source of truth for every UI constant used across all admin handlers.
Import from here instead of hardcoding strings.  Future admin modules must use
these helpers so the panel stays visually consistent automatically.

Usage
─────
    from utils.admin_ui import H, BTN, PAG, badge, fmt_stat, STATUS

    text = H("📦", "Products") + "\n\n" + fmt_stat("Active", n) + "\n\n" + STATUS.on("Auto-restock")
    back = BTN.back("admin_menu")
    home = BTN.home()
    kb   = [[BTN.back("admin_menu"), BTN.home()], PAG.row(page, total, "prefix")]
"""
from __future__ import annotations

from telegram import InlineKeyboardButton as _IKB

# ─── Typography helpers ───────────────────────────────────────────────────────

def H(icon: str, title: str) -> str:
    """Bold section header: ``🛡️ <b>Admin Control Center</b>``"""
    return f"{icon} <b>{title}</b>"


def breadcrumb(icon: str, title: str, parent: str = "Admin") -> str:
    """Breadcrumb nav line: ``🏠 Admin  ›  📦 Products``"""
    return f"🏠 {parent}  ›  {icon} <b>{title}</b>"


def tagline(text: str) -> str:
    """Italic one-liner below a header."""
    return f"<i>{text}</i>"


def fmt_stat(label: str, value) -> str:
    """Single stat row: ``👥 Users: <b>1,234</b>``"""
    return f"{label}: <b>{value}</b>"


def fmt_code(value: str) -> str:
    """Inline monospace value for IDs, addresses, etc."""
    return f"<code>{value}</code>"


# ─── Counter badge ────────────────────────────────────────────────────────────

def badge(n: int) -> str:
    """Return `` (N)`` when N > 0, else empty string (for button labels)."""
    return f" ({n:,})" if n > 0 else ""


# ─── Status indicators ────────────────────────────────────────────────────────

class STATUS:
    """Consistent status dot helpers."""

    ON   = "🟢"
    OFF  = "🔴"
    WARN = "🟡"
    OK   = "✅"
    ERR  = "❌"
    INFO = "ℹ️"

    @staticmethod
    def on(label: str) -> str:
        return f"🟢 {label}"

    @staticmethod
    def off(label: str) -> str:
        return f"🔴 {label}"

    @staticmethod
    def warn(label: str) -> str:
        return f"🟡 {label}"

    @staticmethod
    def toggle(is_on: bool, label: str) -> str:
        return (STATUS.on if is_on else STATUS.off)(label)


# ─── Navigation buttons ───────────────────────────────────────────────────────

class BTN:
    """
    Factory for standard navigation InlineKeyboardButtons.

    All button *labels* (visible text) are defined here.
    Callback data is never modified — pass it through unchanged.
    """

    LABEL_BACK    = "🔙 Back"
    LABEL_HOME    = "🏠 Admin"
    LABEL_REFRESH = "↻ Refresh"
    LABEL_CONFIRM = "✅ Confirm"
    LABEL_CANCEL  = "❌ Cancel"
    LABEL_PREV    = "« Prev"
    LABEL_NEXT    = "Next »"
    LABEL_ENABLE  = "🟢 Enable"
    LABEL_DISABLE = "🔴 Disable"
    LABEL_EDIT    = "✏️ Edit"
    LABEL_DELETE  = "🗑 Delete"
    LABEL_ADD     = "➕ Add"
    LABEL_SAVE    = "💾 Save"

    @staticmethod
    def back(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_BACK, callback_data=cb)

    @staticmethod
    def home(cb: str = "acc:root") -> _IKB:
        return _IKB(BTN.LABEL_HOME, callback_data=cb)

    @staticmethod
    def refresh(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_REFRESH, callback_data=cb)

    @staticmethod
    def confirm(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_CONFIRM, callback_data=cb)

    @staticmethod
    def cancel(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_CANCEL, callback_data=cb)

    @staticmethod
    def prev(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_PREV, callback_data=cb)

    @staticmethod
    def next_(cb: str) -> _IKB:
        return _IKB(BTN.LABEL_NEXT, callback_data=cb)

    @staticmethod
    def toggle(is_on: bool, cb_on: str, cb_off: str,
               label_on: str = "🟢 ON", label_off: str = "🔴 OFF") -> _IKB:
        """Single toggle button that shows current state and flips it."""
        if is_on:
            return _IKB(label_on,  callback_data=cb_off)
        return     _IKB(label_off, callback_data=cb_on)

    @staticmethod
    def back_home(back_cb: str, home_cb: str = "acc:root") -> list[_IKB]:
        """Convenience: returns [Back, Admin] as a ready row list."""
        return [BTN.back(back_cb), BTN.home(home_cb)]


# ─── Pagination helpers ───────────────────────────────────────────────────────

class PAG:
    """
    Build pagination rows.  Page numbers are 0-indexed internally.
    """

    @staticmethod
    def row(page: int, total_pages: int,
            cb_prefix: str,
            show_counter: bool = True) -> list[_IKB]:
        """
        Returns a list of [« Prev] [P/T] [Next »] buttons filtered to only
        those that are relevant.  Pass this list as a keyboard row.

        ``cb_prefix`` is prepended to ``:{page}`` to form callback_data,
        e.g. ``"usr:list:0:desc"`` → pass ``"usr:list"`` and handle
        ``{prefix}:{page}`` in your handler (adjust as needed).
        """
        btns: list[_IKB] = []
        if page > 0:
            btns.append(_IKB(BTN.LABEL_PREV, callback_data=f"{cb_prefix}:{page - 1}"))
        if show_counter and total_pages > 1:
            btns.append(_IKB(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            btns.append(_IKB(BTN.LABEL_NEXT, callback_data=f"{cb_prefix}:{page + 1}"))
        return btns

    @staticmethod
    def simple_row(page: int, total_pages: int,
                   prev_cb: str, next_cb: str) -> list[_IKB]:
        """When prev/next callbacks are fully pre-formed, use this."""
        btns: list[_IKB] = []
        if page > 0:
            btns.append(_IKB(BTN.LABEL_PREV, callback_data=prev_cb))
        if total_pages > 1:
            btns.append(_IKB(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            btns.append(_IKB(BTN.LABEL_NEXT, callback_data=next_cb))
        return btns


# ─── Layout templates ─────────────────────────────────────────────────────────

def menu_text(icon: str, title: str, body: str = "") -> str:
    """
    Standard menu message body.

    Produces:
        🛡️ <b>Admin Control Center</b>

        {body}

    No separator lines, no "Select an option" footer.
    """
    parts = [H(icon, title)]
    if body:
        parts.append("")   # blank line
        parts.append(body)
    return "\n".join(parts)


def detail_text(icon: str, title: str, fields: list[tuple[str, str]]) -> str:
    """
    Standard detail card body.

    ``fields`` is a list of (label, value) pairs.
    Each pair renders as a single line: ``Label: <b>value</b>``
    """
    lines = [H(icon, title), ""]
    for lbl, val in fields:
        lines.append(f"{lbl}: <b>{val}</b>")
    return "\n".join(lines)


def confirm_text(icon: str, title: str, description: str) -> str:
    """Standard destructive action confirmation message."""
    return f"{icon} <b>{title}</b>\n\n{description}"
