"""Global emoji style guide — the single source of truth for feature emojis.

Presentation-only. Importing or using these constants never changes any
callback_data, handler routing, database value, or business logic; they exist
so that one feature is represented by exactly one emoji everywhere it appears
(menus, buttons, titles, messages, notifications, popups, confirmations,
errors and success messages — user side and admin side alike).

Usage:
    from utils.emoji_guide import E
    InlineKeyboardButton(f"{E.WALLET} Wallet", callback_data="wallet")
"""
from __future__ import annotations


class E:
    # ── Core user features ────────────────────────────────────────────
    PRODUCTS = "🛒"
    WALLET = "💳"
    ORDERS = "📦"
    PROFILE = "👤"
    SUPPORT = "🎧"
    INVITE = "👥"
    LANGUAGE = "🌐"
    ADD_FUNDS = "💰"
    PAYMENT_HISTORY = "📜"
    PURCHASE = "🛍"
    COUPON = "🎟"
    NOTIFICATIONS = "🔔"
    SETTINGS = "⚙️"
    ADMIN_PANEL = "🛠"
    PIXEL_VERIFICATION = "🇬"

    # ── Shared navigation ─────────────────────────────────────────────
    BACK = "⬅️"
    MAIN_MENU = "🏠"
    CANCEL = "❌"
    CONFIRM = "✅"
    REFRESH = "🔄"

    # ── Shared status ─────────────────────────────────────────────────
    SUCCESS = "✅"
    ERROR = "❌"
    WARNING = "⚠️"
    PENDING = "⏳"
    INFO = "ℹ️"


#: feature key -> canonical emoji (handy for audits/tests)
EMOJI_MAP = {
    "products": E.PRODUCTS,
    "wallet": E.WALLET,
    "orders": E.ORDERS,
    "profile": E.PROFILE,
    "support": E.SUPPORT,
    "invite": E.INVITE,
    "language": E.LANGUAGE,
    "add_funds": E.ADD_FUNDS,
    "payment_history": E.PAYMENT_HISTORY,
    "purchase": E.PURCHASE,
    "coupon": E.COUPON,
    "notifications": E.NOTIFICATIONS,
    "settings": E.SETTINGS,
    "admin_panel": E.ADMIN_PANEL,
    "pixel_verification": E.PIXEL_VERIFICATION,
}

__all__ = ["E", "EMOJI_MAP"]
