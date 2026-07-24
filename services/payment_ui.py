"""
Centralized, premium UI / formatting layer for every payment-related
Telegram message in the bot — both user-facing payment cards and admin
review notifications.

Design goals (see redesign spec):
  • Every gateway (Binance Pay, Bybit Pay, NOWPayments, Cryptomus, Heleket,
    ZiniPay, USDT TRC20/BEP20/ERC20, bKash, Nagad, Rocket, ...) renders
    through the exact same card layout — only the gateway name and its
    payment-specific fields change.
  • Standardized status badges: 🟡 Pending Review / 🟢 Approved /
    🔴 Rejected / 🔵 Waiting for Payment.
  • Standardized field order: 💳 Gateway → 💰 Amount → 🧾 Deposit ID →
    🔗 Transaction ID → 👤 Customer → 🆔 User ID → 🕒 Time → status.
  • Standardized admin action buttons, always in this order:
    🔄 Verify Again, ✅ Approve, ❌ Reject, 👤 View User.

IMPORTANT: This module is presentation-only. It never touches payment
logic, database state, gateway APIs, wallet logic, or callback routing —
callers keep building callback_data / doing DB work exactly as before and
simply hand the *values* to this module to get back polished `text` +
`InlineKeyboardMarkup` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ─────────────────────────────────────────────────────────────────────────
# Visual constants
# ─────────────────────────────────────────────────────────────────────────

DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

# One source of truth for every gateway's display name + emoji, so "make
# ALL payment gateways use the exact same UI" only ever needs one edit.
GATEWAYS: dict[str, Tuple[str, str]] = {
    "binance_pay":  ("Binance Pay", "🟡"),
    "bybit_pay":    ("Bybit Pay", "🔷"),
    "nowpayments":  ("NOWPayments", "🟢"),
    "cryptomus":    ("Cryptomus", "🟣"),
    "heleket":      ("Heleket", "🟤"),
    "zinipay":      ("bKash • Nagad • Rocket", "🇧🇩"),
    "usdt_trc20":   ("USDT (TRC20)", "💵"),
    "usdt_bep20":   ("USDT (BEP20)", "💵"),
    "usdt_erc20":   ("USDT (ERC20)", "💵"),
    "bkash":        ("bKash", "💗"),
    "nagad":        ("Nagad", "🟠"),
    "rocket":       ("Rocket", "🚀"),
    "manual":       ("Manual Payment", "🧾"),
    "card":         ("Card Payment", "💳"),
    "stars":        ("Telegram Stars", "⭐"),
    "cryptobot":    ("CryptoBot", "🤖"),
}


def gateway_meta(key: Optional[str], fallback_label: Optional[str] = None,
                  fallback_emoji: Optional[str] = None) -> Tuple[str, str]:
    """Look up (label, emoji) for a gateway key.

    ``GATEWAYS`` above is only a *cosmetic* polish table for the gateways
    we happen to know about today — it is never required. Any key that
    isn't in it (a brand-new gateway added tomorrow, an admin-created
    manual method, etc.) still gets a sensible label (humanized from the
    key) and a sensible emoji (inferred from common keywords in the name),
    so a new payment method never has to touch this file to look right.
    """
    if key and key in GATEWAYS:
        return GATEWAYS[key]
    label = fallback_label or (key.replace("_", " ").title() if key else "Payment")
    return (label, fallback_emoji or _infer_emoji(label))


_EMOJI_HINTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("usdt", "usdc", "trc20", "bep20", "erc20", "crypto", "coin", "bitcoin", "btc", "eth", "ltc", "trx", "bnb", "ton"), "🪙"),
    (("bkash", "nagad", "rocket", "upay", "mobile"), "📱"),
    (("card", "visa", "mastercard", "stripe"), "💳"),
    (("bank", "wire", "transfer", "iban"), "🏦"),
    (("star",), "⭐"),
    (("paypal", "skrill", "wise", "payoneer"), "🌐"),
)


def _infer_emoji(label: str) -> str:
    lower = label.lower()
    for keywords, emoji in _EMOJI_HINTS:
        if any(kw in lower for kw in keywords):
            return emoji
    return "💳"


# Standardized status badges (exact wording per spec).
STATUS_BADGES: dict[str, str] = {
    "pending_review":  "🟡 Pending Review",
    "approved":        "🟢 Approved",
    "rejected":        "🔴 Rejected",
    "waiting_payment": "🔵 Waiting for Payment",
    "created":         "🔵 Waiting for Payment",
    "waiting":         "🔵 Waiting for Payment",
    "expired":         "⚪ Expired",
    "cancelled":       "⚫ Cancelled",
    "failed":          "❌ Failed",
}


def status_badge(key: str) -> str:
    return STATUS_BADGES.get(key, key)


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def customer_display(username: Optional[str], telegram_id) -> str:
    if username:
        uname = username.lstrip("@")
        return f"@{uname}"
    return f"User {telegram_id}" if telegram_id is not None else "—"


# ─────────────────────────────────────────────────────────────────────────
# Generic card renderer
# ─────────────────────────────────────────────────────────────────────────

def _row(emoji: str, label: str, value) -> Optional[str]:
    if value is None or value == "":
        return None
    return f"{emoji} <b>{label}:</b> {value}"


def build_card(
    *,
    title: str,
    title_emoji: str = "💳",
    fields: Sequence[Tuple[str, str, object]] = (),
    status_key: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Render one premium, consistently-spaced card.

    ``fields`` is a sequence of ``(emoji, label, value)`` rows. Rows whose
    value is falsy are skipped automatically, so the exact same renderer
    works for every gateway / lifecycle stage without special-casing.
    """
    lines = [f"{title_emoji} <b>{title}</b>", DIVIDER, ""]
    for emoji, label, value in fields:
        row = _row(emoji, label, value)
        if row:
            lines.append(row)
    if status_key:
        lines.append("")
        lines.append(DIVIDER)
        lines.append(f"{status_badge(status_key)}")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# User-facing payment cards
# ─────────────────────────────────────────────────────────────────────────

_STAGE_TITLE = {
    "created":        "Payment Created",
    "waiting":         "Waiting for Payment",
    "pending_review":  "Payment Under Review",
    "approved":        "Payment Approved",
    "rejected":        "Payment Rejected",
    "expired":         "Payment Expired",
    "cancelled":       "Payment Cancelled",
    "failed":          "Payment Failed",
}

_STAGE_STATUS = {
    "created":        "waiting_payment",
    "waiting":        "waiting_payment",
    "pending_review": "pending_review",
    "approved":       "approved",
    "rejected":       "rejected",
    "expired":        "expired",
    "cancelled":      "cancelled",
    "failed":         "failed",
}


def _display_deposit_id(order_id, created_at=None) -> Optional[str]:
    """Render any raw deposit reference the same, user-safe way everywhere:
    a human ``DEP-YYYYMMDD-NNNNNN`` reference — never a bare internal
    database id such as ``#123``.

    Accepts either a raw numeric id (formatted on the fly) or a string
    that has already been formatted upstream (passed through as-is, so
    callers that already computed the reference never get double-formatted).
    """
    if order_id is None:
        return None
    if isinstance(order_id, str) and order_id.startswith("DEP-"):
        return order_id
    try:
        return format_deposit_id(order_id, created_at)
    except (TypeError, ValueError):
        return str(order_id)


def user_payment_card(
    *,
    gateway_key: Optional[str],
    stage: str,
    amount: str,
    order_id=None,
    created_at=None,
    txn_id: Optional[str] = None,
    extra: Sequence[Tuple[str, str, object]] = (),
    note: Optional[str] = None,
    gateway_label_override: Optional[str] = None,
) -> str:
    """Build a standardized user-facing card for any gateway / stage.

    ``stage`` is one of: created, waiting, pending_review, approved,
    rejected, expired, cancelled.
    """
    label, emoji = gateway_meta(gateway_key, gateway_label_override)
    fields = [
        ("💳", "Gateway", label),
        ("💰", "Amount", amount),
        ("🧾", "Deposit ID", _display_deposit_id(order_id, created_at)),
        ("🔗", "Transaction ID", txn_id),
    ]
    fields.extend(extra)
    return build_card(
        title=_STAGE_TITLE.get(stage, "Payment Update"),
        title_emoji=emoji,
        fields=fields,
        status_key=_STAGE_STATUS.get(stage, stage),
        note=note,
    )


# ─────────────────────────────────────────────────────────────────────────
# PaymentMethodView — THE single dynamic contract.
#
# Every payment method — built-in (Binance Pay, Bybit Pay, ZiniPay,
# CryptoBot, NOWPayments, Cryptomus, Heleket, Card, Stars, ...) or added
# later purely through admin config (USDT TRC20/BEP20/ERC20, Stripe,
# PayPal, Skrill, Wise, a new local mobile wallet, ...) — renders through
# THIS dataclass and nothing else. No gateway name is ever special-cased
# in .render() / .keyboard(): they only read whatever fields the caller
# populated. Add a brand-new gateway tomorrow, populate this dataclass
# with its data, and it automatically looks and behaves exactly like
# every other payment method — no new template, no new keyboard, no
# edit to this file required.
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class PaymentMethodView:
    name: str                              # "Binance Pay", "USDT TRC20", "Stripe", ...
    emoji: str = "💳"
    stage: str = "waiting"                 # created/waiting/pending_review/approved/rejected/expired/cancelled/failed
    amount: Optional[str] = None
    deposit_id: object = None              # raw tx id (auto-formatted) or a pre-formatted DEP-... string
    created_at: Optional[datetime] = None
    transaction_id: Optional[str] = None   # gateway/user-submitted TXID, once known
    account_label: Optional[str] = None    # "Send To" / recipient name
    account_number: Optional[str] = None   # address / phone number / wallet / IBAN
    network: Optional[str] = None          # "TRC20", "USDT / USDC", "BDT", ...
    instructions: Optional[str] = None     # free-form how-to-pay text
    notes: Optional[str] = None            # explicit note; auto-derived from the flags below if omitted
    expires_at: Optional[str] = None       # human string: "30 minutes", a timestamp, etc.
    pay_url: Optional[str] = None          # external hosted-checkout / invoice link, if any
    requires_txid: bool = False
    requires_proof: bool = False
    cancel_cb: Optional[str] = "cancel"
    extra_fields: Sequence[Tuple[str, str, object]] = field(default_factory=tuple)

    def _auto_note(self) -> Optional[str]:
        if self.notes:
            return self.notes
        if self.stage not in ("created", "waiting"):
            return None
        if self.requires_txid:
            return ("📝 After sending the payment, reply here with your "
                     "Transaction ID (TXID) to continue.")
        if self.requires_proof:
            return ("📸 After sending the payment, reply here with a "
                     "screenshot as proof of payment.")
        if self.pay_url:
            return "👉 Tap the button below to complete your payment."
        return None

    def render(self) -> str:
        """Build the identical card layout used by every payment method."""
        fields = [
            ("💳", "Gateway", self.name),
            ("💰", "Amount", copy_code(self.amount) if self.amount else None),
            ("🧾", "Deposit ID", _display_deposit_id(self.deposit_id, self.created_at)),
            ("🔗", "Transaction ID", self.transaction_id),
            ("🏷", "Send To", self.account_label),
            ("🔢", "Payment Number", copy_code(self.account_number) if self.account_number else None),
            ("🌐", "Network / Currency", self.network),
            ("⏱", "Expires", self.expires_at),
        ]
        fields.extend(self.extra_fields)
        if self.instructions:
            fields.append(("📋", "Instructions", self.instructions))
        return build_card(
            title=_STAGE_TITLE.get(self.stage, "Payment Update"),
            title_emoji=self.emoji,
            fields=fields,
            status_key=_STAGE_STATUS.get(self.stage, self.stage),
            note=self._auto_note(),
        )

    def keyboard(self) -> InlineKeyboardMarkup:
        """Build the identical action keyboard used by every payment method:
        an optional 'Pay Now' link (only if a hosted checkout URL exists)
        plus Cancel — never gateway-specific buttons."""
        rows = []
        if self.pay_url:
            rows.append([InlineKeyboardButton(f"💳 Pay with {self.name}", url=self.pay_url)])
        if self.cancel_cb:
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data=self.cancel_cb)])
        return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])

    @classmethod
    def from_manual_method(cls, method, transaction, *, stage: str = "waiting") -> "PaymentMethodView":
        """Build a view straight from an admin-configured ``ManualPaymentMethod``
        row + its ``Transaction`` — this is the live proof that a brand-new
        payment method an admin creates in Telegram (no code, no deploy)
        renders through the exact same premium screen as every built-in
        gateway."""
        return cls(
            name=method.name,
            emoji=method.emoji or "💳",
            stage=stage,
            amount=f"${transaction.amount:.2f}",
            deposit_id=transaction.id,
            created_at=transaction.created_at,
            account_label=method.account_label,
            account_number=method.account_number,
            instructions=method.instructions,
            requires_txid=bool(method.require_txid),
            requires_proof=bool(method.require_proof),
        )


# ─────────────────────────────────────────────────────────────────────────
# Admin review card
# ─────────────────────────────────────────────────────────────────────────

def admin_review_card(
    *,
    gateway_key: Optional[str],
    amount: str,
    order_id=None,
    created_at=None,
    txn_id: Optional[str] = None,
    customer_name: Optional[str] = None,
    user_id=None,
    time_str: Optional[str] = None,
    status_key: str = "pending_review",
    extra: Sequence[Tuple[str, str, object]] = (),
    note: Optional[str] = None,
    gateway_label_override: Optional[str] = None,
) -> str:
    """Build the identical admin review card used for every gateway."""
    label, emoji = gateway_meta(gateway_key, gateway_label_override)
    fields = [
        ("💳", "Gateway", label),
        ("💰", "Amount", amount),
        ("🧾", "Deposit ID", _display_deposit_id(order_id, created_at)),
        ("🔗", "Transaction ID", txn_id),
        ("👤", "Customer", customer_name),
        ("🆔", "User ID", user_id),
        ("🕒", "Time", time_str or now_str()),
    ]
    fields.extend(extra)
    return build_card(
        title="Payment Review",
        title_emoji="🛎️",
        fields=fields,
        status_key=status_key,
        note=note,
    )


def admin_resolution_suffix(action: str, actor_label: str, reason: Optional[str] = None) -> str:
    """Small standardized suffix appended to an admin card once resolved,
    e.g. '\\n\\n🟢 Approved by @admin'. Keeps admin history readable while
    still relying on the same status vocabulary as everywhere else."""
    badge = {
        "approved": "🟢 Approved",
        "rejected": "🔴 Rejected",
        "verified": "🟢 Verified & Approved",
    }.get(action, action)
    out = f"\n\n{DIVIDER}\n{badge} by {actor_label}"
    if reason:
        out += f"\n📝 Reason: {reason}"
    return out


# ─────────────────────────────────────────────────────────────────────────
# Admin action keyboard — always the same 4 buttons, same order
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# Best-effort registry so the "could not be verified automatically" notice
# we sent the user can be cleaned up once an admin resolves it (requirement:
# after approval the user should only see the final status). This is an
# in-process cache only — no DB/schema changes — so it's a best-effort nicety
# that works whenever the bot process hasn't restarted between submission
# and resolution, which is the overwhelmingly common case.
# ─────────────────────────────────────────────────────────────────────────

_pending_user_messages: dict = {}


def remember_pending_message(pmv_id: int, chat_id, message_id) -> None:
    if pmv_id is not None and chat_id is not None and message_id is not None:
        _pending_user_messages[pmv_id] = (chat_id, message_id)


def pop_pending_message(pmv_id: int):
    return _pending_user_messages.pop(pmv_id, None)


async def clear_pending_user_message(bot, pmv_id: int) -> None:
    """Best-effort: delete the earlier 'could not verify automatically'
    message in the user's chat now that a final status has been reached."""
    ref = pop_pending_message(pmv_id)
    if not ref:
        return
    chat_id, message_id = ref
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def admin_review_keyboard(
    *,
    verify_cb: Optional[str] = None,
    approve_cb: Optional[str] = None,
    reject_cb: Optional[str] = None,
    view_user_cb: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Standard admin review keyboard. Order is always:
    🔄 Verify Again, ✅ Approve, ❌ Reject, 👤 View User.
    A button is omitted only if its callback wasn't provided (e.g. some
    gateways have no automated re-verification), but relative order among
    the buttons that *are* present never changes.
    """
    row1, row2 = [], []
    if verify_cb:
        row1.append(InlineKeyboardButton("🔄 Verify Again", callback_data=verify_cb))
    if approve_cb:
        row1.append(InlineKeyboardButton("✅ Approve", callback_data=approve_cb))
    if reject_cb:
        row2.append(InlineKeyboardButton("❌ Reject", callback_data=reject_cb))
    if view_user_cb:
        row2.append(InlineKeyboardButton("👤 View User", callback_data=view_user_cb))
    rows = [r for r in (row1, row2) if r]
    return InlineKeyboardMarkup(rows or [[InlineKeyboardButton("🔄 Refresh", callback_data="noop")]])


# ─────────────────────────────────────────────────────────────────────────
# Deposit Success — premium confirmation card + keyboard
# ─────────────────────────────────────────────────────────────────────────

def format_deposit_id(tx_id, created_at=None) -> str:
    """Generate a human-readable deposit reference from the transaction row.

    Format: DEP-YYYYMMDD-XXXXXX  (e.g. DEP-20260722-000163)
    No new DB columns needed — derived entirely from the existing
    transaction ``id`` (integer PK) and ``created_at`` timestamp.
    """
    if created_at is None:
        created_at = datetime.utcnow()
    date_str = created_at.strftime("%Y%m%d")
    return f"DEP-{date_str}-{int(tx_id):06d}"


def deposit_success_card(
    *,
    amount: str,
    payment_method: str,
    deposit_id: Optional[str] = None,
    bonus_line: Optional[str] = None,
) -> str:
    """Build a premium 'Deposit Successful' confirmation card.

    Layout — compact, mobile-optimised, no dividers:

        ✅ Deposit Successful

        💰 Amount Credited
        $10.00 USD

        💳 Payment Method
        Binance Pay

        🎁 Bonus                ← only when a bonus was applied
        +1.00 USD

        🧾 Deposit ID           ← only when deposit_id is provided
        DEP-20260722-000163

        👛 Your wallet has been updated successfully.
    """
    lines = ["✅ <b>Deposit Successful</b>", ""]
    lines.append("💰 <b>Amount Credited</b>")
    lines.append(amount)
    if bonus_line:
        lines.append("")
        lines.append("🎁 <b>Bonus</b>")
        lines.append(bonus_line)
    lines.append("")
    lines.append("💳 <b>Payment Method</b>")
    lines.append(payment_method)
    if deposit_id:
        lines.append("")
        lines.append("🧾 <b>Deposit ID</b>")
        lines.append(deposit_id)
    lines.append("")
    lines.append("👛 Your wallet has been updated successfully.")
    return "\n".join(lines)


def deposit_success_keyboard() -> InlineKeyboardMarkup:
    """Standard keyboard shown after every successful deposit:
    👛 Open Wallet · 📜 Deposit History · 🛍 Continue Shopping."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Open Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📜 Deposit History", callback_data="wallet_history"),
         InlineKeyboardButton("🛍 Continue Shopping", callback_data="products")],
    ])


def payment_failed_keyboard(retry_cb: str = "topup") -> InlineKeyboardMarkup:
    """Standard keyboard shown whenever a payment could not go through:
    🔄 Try Again · 📞 Contact Support · 🏠 Back to Menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data=retry_cb)],
        [InlineKeyboardButton("📞 Contact Support", callback_data="support")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
    ])


def payment_expired_keyboard() -> InlineKeyboardMarkup:
    """Standard keyboard shown whenever a payment window expires:
    💳 Create New Deposit · 🔄 Generate New Payment · 📜 Deposit History ·
    👛 My Wallet · 🏠 Back to Menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Create New Deposit", callback_data="topup")],
        [InlineKeyboardButton("🔄 Generate New Payment", callback_data="topup")],
        [InlineKeyboardButton("📜 Deposit History", callback_data="wallet_history"),
         InlineKeyboardButton("👛 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
    ])


def copy_code(value) -> str:
    """Wrap a value in <code> so Telegram lets the user tap-to-copy it —
    the native equivalent of a 'Copy' button for amounts, addresses,
    payment numbers, and transaction IDs."""
    if value is None or value == "":
        return ""
    return f"<code>{value}</code>"
