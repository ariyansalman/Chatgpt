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

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

# ─────────────────────────────────────────────────────────────────────────
# Visual constants
# ─────────────────────────────────────────────────────────────────────────

# NOTE: dashed/dotted divider lines have been retired project-wide (audit
# finding: "old layouts still use dashed separator lines"). Every card now
# separates sections with plain blank lines instead. DIVIDER is kept as an
# empty string (rather than deleted outright) so any stray call site that
# still concatenates it does not break, but it no longer renders anything.
DIVIDER = ""

# One source of truth for every gateway's display name + emoji, so "make
# ALL payment gateways use the exact same UI" only ever needs one edit.
GATEWAYS: dict[str, Tuple[str, str]] = {
    "binance_pay":  ("Binance Pay", "🟡"),
    "bybit_pay":    ("Bybit Pay", "🔷"),
    "nowpayments":  ("NOWPayments", "🟢"),
    "cryptomus":    ("Cryptomus", "🟣"),
    "heleket":      ("Heleket", "🟤"),
    # NOTE: "zinipay" itself is only a *gateway family* key (bKash / Nagad /
    # Rocket are all routed through the ZiniPay API) — it is never the
    # actual payment method a user picked, so its label must stay generic
    # and must never be shown as the payment method on a deposit. Every
    # real ZiniPay deposit stores which specific provider was used (see
    # ``resolve_zinipay_provider`` / ``zinipay_provider_meta`` below) and
    # callers must resolve + display that instead of this fallback.
    "zinipay":      ("Mobile Banking", "🇧🇩"),
    "usdt_trc20":   ("USDT (TRC20)", "💵"),
    "usdt_bep20":   ("USDT (BEP20)", "💵"),
    "usdt_erc20":   ("USDT (ERC20)", "💵"),
    "bkash":        ("bKash", "💗"),
    "nagad":        ("Nagad", "🧡"),
    "rocket":       ("Rocket", "💜"),
    "upay":         ("Upay", "🔵"),
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


def resolve_zinipay_provider(crypto_address: Optional[str]) -> Optional[str]:
    """Extract the specific bKash / Nagad / Rocket provider a ZiniPay
    deposit was actually created for.

    The provider the user picked is persisted on ``Transaction.crypto_address``
    at order-creation time using the format ``"bdt:<amount>:<provider>"`` (see
    ``_finish_zinipay_payment`` in handlers/payment_handlers.py). Returns
    ``None`` when the field is missing/empty (e.g. a legacy row created
    before providers were tracked), never a guess.
    """
    if not crypto_address or not crypto_address.startswith("bdt:"):
        return None
    parts = crypto_address.split(":")
    if len(parts) > 2 and parts[2]:
        return parts[2].strip().lower()
    return None


def zinipay_provider_meta(
    crypto_address: Optional[str] = None, provider: Optional[str] = None,
) -> Tuple[str, str]:
    """(label, emoji) for the ONE specific mobile money provider a ZiniPay
    deposit actually used — never the generic 'bKash • Nagad • Rocket' /
    'zinipay' combined label.

    Pass either the already-known ``provider`` string, or the Transaction's
    ``crypto_address`` to resolve it from. Falls back to a neutral generic
    label only when no specific provider can be determined at all.
    """
    p = (provider or resolve_zinipay_provider(crypto_address) or "").strip().lower()
    if p in GATEWAYS and p != "zinipay":
        return GATEWAYS[p]
    return GATEWAYS["zinipay"]  # ("Mobile Banking", "🇧🇩") generic fallback


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
# Real, destructive "❌ Cancel Deposit" — distinct from every "⬅️ Back" row
# in this file. Back is pure navigation and never touches a pending
# deposit (see handlers/payment_handlers.py). This button is the ONLY
# action in the whole Add Funds flow that actually cancels the
# in-progress deposit — see handlers/payment_handlers.py:deposit_cancel.
# ─────────────────────────────────────────────────────────────────────────

# Callback-data for the shared, genuinely-destructive Cancel action shown
# on every screen where the user is actively creating/completing a
# deposit (amount picker, method/provider/network pickers, the invoice /
# active payment page, and every Submit Transaction/Order ID prompt).
DEPOSIT_CANCEL_CALLBACK = "deposit_cancel"


def with_deposit_cancel(
    keyboard: InlineKeyboardMarkup,
    cancel_cb: str = DEPOSIT_CANCEL_CALLBACK,
    label: str = "❌ Cancel Deposit",
) -> InlineKeyboardMarkup:
    """Append a real "❌ Cancel Deposit" row to an existing keyboard,
    directly above its last row (conventionally a "⬅️ Back" / "🏠 Main
    Menu" row) so Cancel always sits next to, never on top of, existing
    navigation. This never removes, relabels, or repurposes any Back
    button — Back keeps navigating exactly as before; only this row
    actually cancels the deposit (see
    handlers/payment_handlers.py:deposit_cancel).
    """
    rows = [list(r) for r in keyboard.inline_keyboard]
    cancel_row = [InlineKeyboardButton(label, callback_data=cancel_cb)]
    if rows:
        rows.insert(len(rows) - 1, cancel_row)
    else:
        rows.append(cancel_row)
    return InlineKeyboardMarkup(rows)


def deposit_cancelled_card() -> str:
    """The one shared confirmation shown after a real deposit cancel."""
    return "✅ Deposit cancelled successfully."


def deposit_cancelled_keyboard(
    new_deposit_cb: str = "topup",
    back_cb: str = "topup_back_to_wallet",
) -> InlineKeyboardMarkup:
    """Buttons shown under the "✅ Deposit cancelled successfully." card:
    start a brand-new deposit immediately, or go back."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Create New Deposit", callback_data=new_deposit_cb)],
        [InlineKeyboardButton("🔙 Back", callback_data=back_cb)],
    ])


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
    lines = [f"{title_emoji} <b>{title}</b>", ""]
    for emoji, label, value in fields:
        row = _row(emoji, label, value)
        if row:
            lines.append(row)
    if status_key:
        lines.append("")
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
    "pending_review":  "Deposit Submitted",
    "approved":        "Deposit Approved",
    "rejected":        "Deposit Rejected",
    "expired":         "Deposit Expired",
    "cancelled":       "Deposit Cancelled",
    "failed":          "Deposit Failed",
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

# Compact deposit-status cards (Approved / Rejected / Expired / Cancelled /
# Failed) use a status-colored title emoji — never the gateway's own emoji —
# so every deposit outcome reads consistently at a glance, matching the
# same visual language as ``pending_review_card`` below.
_STAGE_TITLE_EMOJI = {
    "approved":  "✅",
    "rejected":  "❌",
    "expired":   "⌛",
    "cancelled": "🚫",
    "failed":    "⚠️",
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
    """Build a standardized, compact user-facing card for any gateway /
    stage. Deposit outcome stages (approved, rejected, expired, cancelled,
    failed) render through the exact same compact, label-free layout as
    ``pending_review_card`` — status-colored title emoji, plain
    "💳 <method>" / "💰 <amount>" lines, "🆔 <deposit id>", any labeled
    extra fields (e.g. "💵 Credited: <amount>"), and a closing note — so
    every deposit-related message in the bot looks visually identical.
    The title itself states the outcome (Deposit Approved / Rejected /
    ...), so no separate "Status:" line repeats it.

    ``stage`` is one of: created, waiting, pending_review, approved,
    rejected, expired, cancelled, failed.
    """
    label, gateway_emoji = gateway_meta(gateway_key, gateway_label_override)

    if stage not in _STAGE_TITLE_EMOJI:
        # Stages without a dedicated compact template (created / waiting /
        # pending_review) fall back to the original generic card renderer —
        # unchanged behavior for those, since they're not part of this
        # redesign and pending_review has its own dedicated function.
        fields = [
            ("💳", "Payment Method", label),
            ("💰", "Amount", copy_code(amount) if amount else None),
            ("🧾", "Deposit ID", copy_code(_display_deposit_id(order_id, created_at))),
            ("🔗", "Transaction ID", copy_code(txn_id) if txn_id else None),
        ]
        fields.extend(extra)
        return build_card(
            title=_STAGE_TITLE.get(stage, "Payment Update"),
            title_emoji=gateway_emoji,
            fields=fields,
            status_key=_STAGE_STATUS.get(stage, stage),
            note=note,
        )

    dep_id = _display_deposit_id(order_id, created_at)
    show_txn_id = bool(txn_id) and str(txn_id) != str(dep_id)

    lines: list[str] = [
        f"{_STAGE_TITLE_EMOJI[stage]} <b>{_STAGE_TITLE.get(stage, 'Deposit Update')}</b>",
        "",
        f"💳 {label}",
    ]
    if amount:
        lines.append(f"💰 {copy_code(amount)}")
    lines.append("")

    if dep_id:
        lines.append(f"🆔 {copy_code(dep_id)}")
    if show_txn_id:
        lines.append(f"🔗 {copy_code(txn_id)}")
    for field_emoji, field_label, value in extra:
        if value is None or value == "":
            continue
        lines.append(f"{field_emoji} <b>{field_label}:</b> {value}")

    if note:
        lines.append("")
        lines.append(note)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# THE unified payment INVOICE template ("waiting for payment" screen).
#
# One template, used by every gateway — current and future. It shows
# ONLY: Payment Method, Amount, Payment Destination, Network (crypto
# only), Deposit ID, Expiration, and one short instruction. No exchange
# rate, no long numbered steps, no duplicate "waiting" sections, no extra
# warnings, no extra spacing. Amount / Payment Destination / Deposit ID /
# Network / Expiration are always monospace (`<code>`) so Telegram lets
# the user tap-to-copy them directly — the native, silent equivalent of a
# "Copy" button (no popup, no confirmation message, ever).
#
# ``invoice_keyboard`` adds real "Copy" buttons on top of that using
# Telegram's native copy-to-clipboard button (``copy_text``, Bot API 8.0+)
# — tapping it copies the value with zero server round-trip, so there is
# no callback, no toast, and no way for it to ever send a message.
# ─────────────────────────────────────────────────────────────────────────

# Dynamic "Payment Destination" label per gateway family (spec: Binance /
# Bybit -> Pay ID, Crypto -> Wallet Address, bKash / Nagad / Rocket / Upay ->
# Send Money To). Anything not listed falls back to a sensible default so
# a brand-new gateway never needs to touch this file to look right.
_DESTINATION_LABELS: dict[str, str] = {
    "binance_pay": "Pay ID",
    "bybit_pay":   "Pay ID",
    "bkash":       "Send Money To",
    "nagad":       "Send Money To",
    "rocket":      "Send Money To",
    "upay":        "Send Money To",
    "zinipay":     "Send Money To",
}


def destination_label_for(gateway_key: Optional[str], *, is_crypto: bool = False) -> str:
    """Resolve the dynamic 'Payment Destination' label for a gateway."""
    if is_crypto:
        return "Wallet Address"
    key = (gateway_key or "").lower()
    if key in _DESTINATION_LABELS:
        return _DESTINATION_LABELS[key]
    if any(tag in key for tag in ("usdt", "usdc", "crypto", "trc20", "bep20", "erc20", "btc", "ltc")):
        return "Wallet Address"
    if any(tag in key for tag in ("bkash", "nagad", "rocket", "mobile")):
        return "Send Money To"
    return "Payment Destination"


def invoice_card(
    *,
    method_label: str,
    method_emoji: str = "💳",
    amount: str,
    destination_label: Optional[str] = None,
    destination_value: Optional[str] = None,
    network: Optional[str] = None,
    deposit_id=None,
    created_at=None,
    expires_at: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """Render THE one invoice card. Every gateway — built-in or added
    later — must go through this function so every payment invoice in the
    bot is guaranteed to look identical and contain only the approved
    fields. Do not add fields here; extend ``instruction`` instead."""
    lines = [f"{method_emoji} <b>{method_label}</b>", ""]

    lines.append("💰 <b>Amount</b>")
    lines.append(f"<code>{amount}</code>")

    if destination_value:
        label = destination_label or "Payment Destination"
        emoji = "🆔" if "pay id" in label.lower() else (
            "📥" if "wallet" in label.lower() else "📱"
        )
        lines.append("")
        lines.append(f"{emoji} <b>{label}</b>")
        lines.append(f"<code>{destination_value}</code>")

    if network:
        lines.append("")
        lines.append("🌐 <b>Network</b>")
        lines.append(f"<code>{network}</code>")

    dep = _display_deposit_id(deposit_id, created_at)
    if dep:
        lines.append("")
        lines.append("🧾 <b>Deposit ID</b>")
        lines.append(f"<code>{dep}</code>")

    if expires_at:
        lines.append("")
        lines.append("⏳ <b>Expires</b>")
        lines.append(f"<code>{expires_at}</code>")

    lines.append("")
    lines.append(instruction or "📌 Send the exact amount, then submit your Transaction ID.")

    return "\n".join(lines)


def invoice_keyboard(
    *,
    destination_value: Optional[str] = None,
    destination_copy_label: str = "Copy",
    amount_value: Optional[str] = None,
    pay_url: Optional[str] = None,
    pay_url_label: Optional[str] = None,
    submit_cb: Optional[str] = None,
    submit_label: str = "📄 Submit Transaction ID",
    cancel_cb: Optional[str] = "cancel",
    back_cb: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """The one action-row layout used by every invoice: optional silent
    Copy buttons, then Submit (or an external Pay link), then Cancel —
    identical order for every gateway. ``back_cb`` is optional and only
    used by screens that reopen an existing pending deposit (adds a
    trailing "⬅️ Back" row); every existing caller that doesn't pass it
    renders exactly as before.

    Copy buttons use Telegram's native ``copy_text`` button — the value is
    copied to the user's clipboard entirely client-side. No callback is
    fired, so nothing here can ever produce a confirmation message,
    alert, or popup.

    Navigation: every invoice screen shows exactly ONE "⬅️ Back" row —
    never a "❌ Cancel" — because Back here is pure navigation back to the
    Payment Method screen and never touches the pending deposit (see
    ``handlers/payment_handlers.py:cancel_topup`` /
    ``topup_back_to_methods``). ``back_cb`` and ``cancel_cb`` both resolve
    to that same non-destructive navigation today; ``back_cb`` wins when a
    caller supplies both so this never renders two Back rows. The
    ``cancel_cb`` parameter name is kept for backward compatibility with
    every existing call site.
    """
    rows: list[list[InlineKeyboardButton]] = []

    copy_row: list[InlineKeyboardButton] = []
    if destination_value:
        copy_row.append(InlineKeyboardButton(
            f"📋 {destination_copy_label}",
            copy_text=CopyTextButton(str(destination_value)),
        ))
    if amount_value:
        copy_row.append(InlineKeyboardButton(
            "💰 Copy Amount",
            copy_text=CopyTextButton(str(amount_value)),
        ))
    if copy_row:
        rows.append(copy_row)

    if pay_url:
        rows.append([InlineKeyboardButton(pay_url_label or "💳 Pay Now", url=pay_url)])
    elif submit_cb:
        rows.append([InlineKeyboardButton(submit_label, callback_data=submit_cb)])

    back_target = back_cb or cancel_cb
    if back_target:
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back_target)])

    keyboard = InlineKeyboardMarkup(rows)
    # This is the Active Payment Page — the user is actively completing a
    # deposit — so it always gets a real, destructive Cancel alongside its
    # non-destructive Back row (see ``with_deposit_cancel``).
    return with_deposit_cancel(keyboard)


# ─────────────────────────────────────────────────────────────────────────
# "Pending Deposit" notice — shown instead of a plain-text block whenever a
# user tries to start a new deposit while one is already in progress. One
# template for every gateway, exactly like ``invoice_card`` above: friendly
# wording, the approved field set, no raw technical phrasing, no internal
# database values. Presentation-only — callers still own all DB / gateway
# logic and only hand this module the display values.
# ─────────────────────────────────────────────────────────────────────────

def pending_deposit_card(
    *,
    method_label: str,
    method_emoji: str = "💳",
    amount: str,
    deposit_id=None,
    created_at=None,
    expires_at: Optional[str] = None,
) -> str:
    """Render the one 'you already have a deposit in progress' card.

    ``expires_at`` should already be a short, human phrase such as
    ``'12m 40s remaining'`` or ``'Expired'`` — this function only lays it
    out, it never computes durations itself.
    """
    lines = [
        "⚠️ <b>Pending Deposit</b>",
        "",
        "You already have a deposit in progress. Continue it or cancel it "
        "before starting a new one.",
        "",
        f"{method_emoji} <b>Payment Method</b>",
        f"<code>{method_label}</code>",
        "",
        "💰 <b>Amount</b>",
        f"<code>{amount}</code>",
    ]
    dep = _display_deposit_id(deposit_id, created_at)
    if dep:
        lines.append("")
        lines.append("🧾 <b>Deposit ID</b>")
        lines.append(f"<code>{dep}</code>")
    if expires_at:
        lines.append("")
        lines.append("⏳ <b>Expires In</b>")
        lines.append(f"<code>{expires_at}</code>")
    return "\n".join(lines)


def pending_deposit_keyboard(
    *,
    continue_cb: str,
    cancel_cb: str = "cancel_pending_deposit",
    back_cb: str = "topup_menu_back",
) -> InlineKeyboardMarkup:
    """Button layout for the Pending Deposit notice: Continue / Cancel /
    Back — always in this order, identical for every gateway.

    This is the ONE dedicated "Cancel Payment" menu in the payment flow:
    tapping "❌ Cancel Deposit" here is a deliberate, explicit choice to end
    the pending deposit (see ``handlers/payment_handlers.py:
    cancel_pending_deposit``), unlike every other Back button in the
    payment system, which never cancels anything. ``back_cb`` — a plain
    "⬅️ Back" row — leaves the pending deposit untouched.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Continue Deposit", callback_data=continue_cb)],
        [InlineKeyboardButton("❌ Cancel Deposit", callback_data=cancel_cb)],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
    ])


def binance_bybit_invoice(
    *, method_label: str, method_emoji: str, amount: str, pay_id: str,
    deposit_id=None, created_at=None, expires_at: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """Binance Pay / Bybit Pay (UID transfer) invoice — spec template."""
    return invoice_card(
        method_label=method_label, method_emoji=method_emoji, amount=amount,
        destination_label="Pay ID", destination_value=pay_id,
        deposit_id=deposit_id, created_at=created_at, expires_at=expires_at,
        instruction=instruction or "📌 Send the exact amount, then submit your Transaction ID.",
    )


def binance_pay_invoice(
    *, amount: str, pay_id: str, deposit_id=None, created_at=None,
    expires_at: Optional[str] = None,
) -> str:
    """Render the Binance Pay deposit screen.

    Binance Pay calls the user-supplied reference an Order ID.  Keep this
    presentation separate from the other gateway invoices so the Binance
    wording can be precise without changing any shared payment behavior.
    """
    dep = _display_deposit_id(deposit_id, created_at)
    lines = ["🟡 <b>Binance Pay</b>", ""]
    lines.append(f"💰 <b>Amount:</b> {copy_code(amount)}")
    lines.append(
        f"🆔 <b>Send To (Binance Pay ID):</b> "
        f"{copy_code(pay_id)}"
    )
    if dep:
        lines.append(f"🧾 <b>Deposit ID:</b> {copy_code(dep)}")
    if expires_at:
        lines.append(f"⏳ <b>Expires In:</b> {expires_at}")
    lines.extend([
        "",
        "📌 <b>Instructions:</b>",
        "Open Binance App → Pay → Send. Enter the Pay ID above and send "
        "the exact amount. After payment, click below and submit your Order ID.",
    ])
    return "\n".join(lines)


def binance_pay_keyboard(*, submit_cb: str, cancel_cb: str = "cancel",
                         back_cb: str = "topup_menu_back") -> InlineKeyboardMarkup:
    """The Binance Pay invoice actions, in the user-facing order."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Submit Order ID", callback_data=submit_cb)],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
    ])
    return with_deposit_cancel(keyboard)


def bybit_pay_invoice(
    *, amount: str, pay_id: str, deposit_id=None, created_at=None,
    expires_at: Optional[str] = None,
) -> str:
    """Premium Bybit Pay (UID Transfer) deposit screen.

    Matches the spec layout exactly:
        🟠 Bybit Pay
        💰 Amount: 1.00 USDT
        🆔 Send To (Bybit UID): 123456789
        🧾 Deposit ID: DEP-YYYYMMDD-XXXXXX
        ⏳ Expires In: 30 Minutes
        📌 Instructions: Open Bybit App → Assets → Pay → Send…
    """
    dep = _display_deposit_id(deposit_id, created_at)
    lines = ["🟠 <b>Bybit Pay</b>", ""]
    lines.append(f"💰 <b>Amount:</b> {copy_code(amount)}")
    lines.append(
        f"🆔 <b>Send To (Bybit UID):</b> "
        f"{copy_code(pay_id)}"
    )
    if dep:
        lines.append(f"🧾 <b>Deposit ID:</b> {copy_code(dep)}")
    if expires_at:
        lines.append(f"⏳ <b>Expires In:</b> {expires_at}")
    lines.extend([
        "",
        "📌 <b>Instructions:</b>",
        "Open Bybit App → Assets → Pay → Send. Enter the UID above and send "
        "the exact amount. After payment, click below and submit your Order ID.",
    ])
    return "\n".join(lines)


def bybit_pay_keyboard(*, submit_cb: str, cancel_cb: str = "cancel",
                       back_cb: str = "topup_menu_back") -> InlineKeyboardMarkup:
    """The Bybit Pay invoice actions — Submit Order ID + Back, plus a real
    Cancel. Amount and UID remain copyable through native copy controls."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Submit Order ID", callback_data=submit_cb)],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
    ])
    return with_deposit_cancel(keyboard)


# Network-code → (coin label, full network name for instructions, invoice emoji)
# Every supported Bybit on-chain / crypto network is listed here so the
# premium invoice renders consistently with zero per-network special-casing.
_CRYPTO_NETWORK_DISPLAY: dict = {
    "TRC20":  ("USDT (TRC20)",             "TRC20 (Tron)",          "🟡"),
    "BEP20":  ("USDT (BEP20)",             "BEP20 (BSC)",           "🟡"),
    "ERC20":  ("USDT (ERC20)",             "ERC20 (Ethereum)",      "🟡"),
    "TON":    ("USDT (TON)",               "TON",                   "🟡"),
    "SOL":    ("USDT (Solana)",            "Solana",                "🟡"),
    "AVAXC":  ("USDT (Avalanche C-Chain)", "Avalanche C-Chain",     "🟡"),
    "BASE":   ("USDT (Base)",              "Base",                  "🟡"),
    "ARBONE": ("USDT (Arbitrum One)",      "Arbitrum One",          "🟡"),
    "OP":     ("USDT (Optimism)",          "Optimism",              "🟡"),
    "MATIC":  ("USDT (Polygon)",           "Polygon (MATIC)",       "🟡"),
    "LTC":    ("Litecoin (LTC)",           "Litecoin (LTC)",        "🪙"),
}


def crypto_network_label(network: str) -> str:
    """Return the user-facing coin label for a given network code.
    E.g. 'BEP20' → 'USDT (BEP20)', 'LTC' → 'Litecoin (LTC)'.
    Purely display — callers must never use this for routing/logic."""
    net = (network or "").strip().upper()
    return _CRYPTO_NETWORK_DISPLAY.get(net, (f"USDT ({net})",))[0]


def crypto_invoice(
    *, network: str, amount: str, wallet_address: str,
    deposit_id=None, created_at=None, expires_at: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """Premium on-chain crypto (USDT/LTC/... on any network) invoice.

    Layout — spec-standardised, identical for every supported network:

        🟡 USDT Payment (BEP20)

        💰 Amount: 1.00 USDT
        📥 Wallet Address:
        0x8f3a…c92d1e
        🧾 Deposit ID: DEP-YYYYMMDD-XXXXXX
        ⏳ Expires In: 30 Minutes

        📌 Instructions:
        Send the exact amount … BEP20 (BSC) network only …
        ⚠️ Sending via the wrong network …

    Only the coin label, network name, and wallet address change per
    network — layout, spacing, icons, and button order are identical.
    Presentation-only; callers own all deposit and verification logic.
    """
    net = (network or "").strip().upper()
    coin_label, net_full, emoji = _CRYPTO_NETWORK_DISPLAY.get(
        net, (f"USDT ({net})", net, "🟡")
    )
    dep = _display_deposit_id(deposit_id, created_at)
    instr = instruction or (
        f"Send the exact amount to the wallet address above using the "
        f"{net_full} network only. After payment, click below and submit "
        "your Transaction Hash (TxHash).\n\n"
        "⚠️ Sending via the wrong network may result in permanent loss of funds."
    )
    lines = [f"{emoji} <b>{coin_label} Payment</b>", ""]
    lines.append(f"💰 <b>Amount:</b> {copy_code(amount)}")
    lines.append("📥 <b>Wallet Address:</b>")
    lines.append(f"{copy_code(wallet_address)}")
    if dep:
        lines.append(f"🧾 <b>Deposit ID:</b> {copy_code(dep)}")
    if expires_at:
        lines.append(f"⏳ <b>Expires In:</b> {expires_at}")
    lines.extend(["", "📌 <b>Instructions:</b>", instr])
    return "\n".join(lines)


def mobile_money_invoice(
    *, provider_label: str, provider_emoji: str, amount: str, send_to: str,
    deposit_id=None, created_at=None, expires_at: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """bKash / Nagad / Rocket / Upay invoice — premium inline layout.

    Shows ONLY the provider that was actually selected — never all providers
    at once. Only the provider name, color emoji, and wallet number change
    between providers; layout and spacing are identical for every method.
    """
    dep = _display_deposit_id(deposit_id, created_at)
    instr = instruction or (
        f"Send the exact amount via {provider_label} Send Money. "
        "After successful payment, click the button below and submit your TrxID."
    )
    lines = [f"{provider_emoji} <b>{provider_label} Payment</b>", ""]
    lines.append(f"💰 <b>Amount:</b> <code>{amount}</code>")
    lines.append(f"📲 <b>Send Money To:</b> <code>{send_to}</code>")
    if dep:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{dep}</code>")
    if expires_at:
        lines.append(f"⏳ <b>Expires In:</b> {expires_at}")
    lines.extend(["", "📌 <b>Instructions:</b>", instr])
    return "\n".join(lines)


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
        """While waiting for payment, every payment method — built-in or
        admin-added — renders through the exact same unified invoice
        template (``invoice_card``). Other lifecycle stages (rejected,
        expired, ...) keep the fuller status-card layout."""
        if self.stage in ("created", "waiting"):
            return invoice_card(
                method_label=self.name, method_emoji=self.emoji,
                amount=self.amount or "",
                destination_label=("Wallet Address" if self.network else "Payment Destination"),
                destination_value=self.account_number,
                network=self.network,
                deposit_id=self.deposit_id, created_at=self.created_at,
                expires_at=self.expires_at,
                instruction=self._auto_note() or self.instructions,
            )
        fields = [
            ("💳", "Payment Method", self.name),
            ("💰", "Amount", copy_code(self.amount) if self.amount else None),
            ("🧾", "Deposit ID", copy_code(_display_deposit_id(self.deposit_id, self.created_at))),
            ("🔗", "Transaction ID", copy_code(self.transaction_id) if self.transaction_id else None),
        ]
        fields.extend(self.extra_fields)
        return build_card(
            title=_STAGE_TITLE.get(self.stage, "Payment Update"),
            title_emoji=self.emoji,
            fields=fields,
            status_key=_STAGE_STATUS.get(self.stage, self.stage),
            note=self._auto_note(),
        )

    def keyboard(self) -> InlineKeyboardMarkup:
        """Build the identical action keyboard used by every payment method:
        optional silent Copy buttons, a 'Pay Now' link (only if a hosted
        checkout URL exists), and Cancel — never gateway-specific buttons."""
        if self.stage in ("created", "waiting"):
            return invoice_keyboard(
                destination_value=self.account_number,
                destination_copy_label="Copy Address" if self.network else "Copy",
                amount_value=self.amount,
                pay_url=self.pay_url, pay_url_label=f"💳 Pay with {self.name}",
                cancel_cb=self.cancel_cb,
            )
        rows = []
        if self.pay_url:
            rows.append([InlineKeyboardButton(f"💳 Pay with {self.name}", url=self.pay_url)])
        if self.cancel_cb:
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data=self.cancel_cb)])
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
# "Deposit Submitted" — clean fintech-style confirmation screen
#
# Design rules (enforced here, nowhere else):
#   • One premium layout — no dashed or dotted separator lines.
#   • No field is ever shown twice: if the Transaction ID the gateway/user
#     supplied is identical to the Deposit ID, only the Deposit ID appears.
#   • No internal IDs, no repeated status messages, no redundant text.
#   • Amount and Deposit ID are wrapped in <code> so the user can tap to
#     copy them on mobile — the single most-requested fintech UX detail.
#   • Every payment method (built-in or admin-created) renders through
#     this exact same template — only the label text changes — so a new
#     gateway never needs a new screen.
# ─────────────────────────────────────────────────────────────────────────

_DEFAULT_PENDING_REVIEW_NOTE = (
    "Your deposit has been received and is waiting for verification."
)


def pending_review_card(
    *,
    gateway_key: Optional[str] = None,
    payment_method: Optional[str] = None,
    amount: str,
    deposit_id=None,
    order_id=None,
    created_at=None,
    txn_id: Optional[str] = None,
    extra: Sequence[Tuple[str, str, object]] = (),
    note: Optional[str] = None,
    gateway_label_override: Optional[str] = None,
) -> str:
    """Build the single, compact 'Deposit Submitted' confirmation screen
    shown to a user right after they submit a payment / TXID / proof for
    manual review (this is also the screen shown when a gateway's
    automatic check fails and the deposit is queued for manual review).

    Layout is fixed at a handful of short, label-free lines — no more
    than one blank line between groups, no separators:

        🟡 Deposit Submitted

        💳 <method>
        💰 <amount>

        🆔 <deposit id>
        📌 Status: Pending Review

        <note>

    All displayed values are resolved dynamically — nothing is ever
    hardcoded per gateway.  A new payment method added tomorrow (by code
    or by an admin via Telegram) automatically uses this exact layout.
    """
    # ── Resolve payment method label ──────────────────────────────────────
    if payment_method:
        label = payment_method
    else:
        label, _emoji = gateway_meta(gateway_key, gateway_label_override)

    # ── Resolve deposit reference ─────────────────────────────────────────
    dep_id = _display_deposit_id(
        deposit_id if deposit_id is not None else order_id, created_at
    )

    # ── Deduplicate IDs: show Transaction ID only when it differs ─────────
    show_txn_id = bool(txn_id) and str(txn_id) != str(dep_id)

    # ── Build card ────────────────────────────────────────────────────────
    lines: list[str] = [
        "🟡 <b>Deposit Submitted</b>",
        "",
        f"💳 {label}",
    ]
    if amount:
        lines.append(f"💰 {copy_code(amount)}")
    lines.append("")

    if dep_id:
        lines.append(f"🆔 {copy_code(dep_id)}")

    if show_txn_id:
        lines.append(f"🔗 {copy_code(txn_id)}")

    # Any gateway-specific extra fields (e.g. amount actually received)
    for field_emoji, _field_label, value in extra:
        if value is None or value == "":
            continue
        lines.append(f"{field_emoji} {value}")

    lines.append("📌 <b>Status:</b> Pending Review")
    lines.append("")
    lines.append(note or _DEFAULT_PENDING_REVIEW_NOTE)

    return "\n".join(lines)


def pending_review_keyboard(
    *,
    history_cb: str = "wallet_history",
    support_cb: str = "support",
    menu_cb: str = "main_menu",
) -> InlineKeyboardMarkup:
    """Standard action keyboard for the 'Deposit Submitted' screen.

    Buttons (in order): 📜 Deposit History · 🎧 Support · ⬅️ Back to Menu.
    All callback_data values are passed in from the caller — no new routes
    are introduced here, and future payment methods get this keyboard for free.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Deposit History",  callback_data=history_cb)],
        [InlineKeyboardButton("🎧 Support",           callback_data=support_cb)],
        [InlineKeyboardButton("⬅️ Back to Menu",      callback_data=menu_cb)],
    ])


# ─────────────────────────────────────────────────────────────────────────
# Admin review card — compact premium moderation card
#
# THE single admin review card used for every payment method and every
# manual-review surface (generic manual, bKash/Nagad manual mode, the
# initial deposit-request notification, and Binance/Bybit/ZiniPay
# failed-auto-verification review). One template, one visual language,
# regardless of which gateway or which surface renders it.
#
# Layout is fixed and identical everywhere, grouped into short blocks with
# a single blank line between groups and no separator lines:
#
#   🔔 <title>
#
#   🆔 <deposit id>
#   <status emoji> <status text>
#
#   👤 <name> (@username)
#   🆔 <telegram id>
#
#   💳 <gateway>
#   💰 <amount>
#   🌐 <network>            (only for on-chain methods)
#   🔗 <txn id>              (only when it differs from the deposit id)
#   <any per-gateway extras>
#
#   🕒 <submitted>
#
#   ⚠ Auto Verify: <status word>      (pending-review cards only)
#   ❌/ℹ️ <reason>
#
#   <free-form note, e.g. submitted proof>
#
# Monospace (tap-to-copy) is kept for Deposit ID, Telegram ID, Amount, and
# Transaction ID — everything else is plain text. Any field with nothing
# to show is simply omitted — never a placeholder line or an empty
# "Label:" — and no group ever prints more than one blank line before it.
# ─────────────────────────────────────────────────────────────────────────

_ADMIN_TITLES: dict[str, str] = {
    "pending_review":  "🔔 New Deposit Request",
    "approved":        "✅ Deposit Approved",
    "rejected":        "❌ Deposit Rejected",
    "expired":         "⌛ Deposit Expired",
    "cancelled":       "🚫 Deposit Cancelled",
    "failed":          "⚠️ Deposit Failed",
    "waiting_payment": "🔔 Deposit Request",
}


def _status_parts(status_key: str) -> Tuple[str, str]:
    """Split a status badge ('🟡 Pending Review') into its emoji and text
    so the compact admin card can show the emoji on the label line and
    the plain text on the value line, instead of repeating it."""
    raw = STATUS_BADGES.get(status_key, status_key)
    if " " in raw:
        emoji, text = raw.split(" ", 1)
        return emoji, text
    return "•", raw


_VERIFICATION_STATUS_LABEL = {
    "failed":         "Failed",
    "not_applicable": "Not Applicable",
}

# icon shown on the reason line — a real failure gets ❌, "no automated
# check for this method" gets a neutral ℹ️ instead (it isn't a failure).
_VERIFICATION_ICON = {
    "failed":         "❌",
    "not_applicable": "ℹ️",
}

_DEFAULT_VERIFICATION_REASON = {
    "failed": "Transaction could not be verified automatically — manual review required.",
    "not_applicable": "This payment method has no automatic verification — manual review required.",
}


def admin_review_card(
    *,
    gateway_key: Optional[str],
    amount: str,
    order_id=None,
    created_at=None,
    txn_id: Optional[str] = None,
    customer_name: Optional[str] = None,
    full_name: Optional[str] = None,
    username: Optional[str] = None,
    user_id=None,
    network: Optional[str] = None,
    verification_result: Optional[str] = None,
    verification_status: Optional[str] = None,
    verification_reason: Optional[str] = None,
    time_str: Optional[str] = None,
    status_key: str = "pending_review",
    extra: Sequence[Tuple[str, str, object]] = (),
    note: Optional[str] = None,
    gateway_label_override: Optional[str] = None,
) -> str:
    """Build THE single admin review card. See module comment block above
    for the layout contract.

    Name resolution for the 👤 User block: the customer's name and
    ``@username`` render on one line, and their Telegram ID on its own
    line right below — prefers ``full_name``; falls back to a
    caller-supplied ``customer_name`` string (legacy callers that already
    pre-formatted it). Any piece with nothing to show (no username, no
    name, no id) is simply omitted — never a placeholder like "(no
    username)".

    Auto-verification results are never shown as a raw provider/exception
    string. Pass ``verification_status`` ("failed" or "not_applicable")
    to render the standardized "⚠ Auto Verify" line below; optionally
    pair it with a short, human-written ``verification_reason`` (e.g.
    "Payment not found in Binance account history"). This only appears on
    the pending-review card — once a deposit is approved or rejected, the
    verification detail is no longer relevant to show. ``verification_result``
    remains supported as a plain legacy field for any caller that hasn't
    migrated to the structured version yet.
    """
    gateway_label, _ = gateway_meta(gateway_key, gateway_label_override)
    deposit_id = _display_deposit_id(order_id, created_at)
    status_emoji, status_text = _status_parts(status_key)
    title = _ADMIN_TITLES.get(status_key, "🔔 Deposit Update")

    name_line = full_name or customer_name
    username_suffix = f" (@{username.lstrip('@')})" if username else ""
    submitted = time_str or (
        created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if created_at else now_str()
    )

    lines = [f"<b>{title}</b>", ""]

    # 🆔 Deposit ID + status, grouped — no repeated "Status:" label line.
    if deposit_id:
        lines.append(f"🆔 <code>{deposit_id}</code>")
    lines.append(f"{status_emoji} {status_text}")
    lines.append("")

    # 👤 Customer — name (+username) on one line, Telegram ID right below.
    user_lines = []
    if name_line:
        user_lines.append(f"👤 {name_line}{username_suffix}")
    elif username_suffix:
        user_lines.append(f"👤 {username_suffix.strip(' ()')}")
    if user_id is not None:
        user_lines.append(f"🆔 <code>{user_id}</code>")
    if user_lines:
        lines.extend(user_lines)
        lines.append("")

    # 💳 Payment details, grouped — gateway, amount, network, txn id, extras.
    payment_lines = []
    if gateway_label:
        payment_lines.append(f"💳 {gateway_label}")
    if amount:
        payment_lines.append(f"💰 <code>{amount}</code>")
    # Network only ever appears when the caller actually has one to show —
    # non-blockchain methods (mobile wallets, card gateways, etc.) simply
    # never pass a value here, so the row never renders a placeholder.
    if network:
        payment_lines.append(f"🌐 {network}")
    # Skip Transaction ID when it's identical to the Deposit ID — the same
    # reference never needs to be shown twice.
    if txn_id and str(txn_id) != str(deposit_id):
        payment_lines.append(f"🔗 <code>{txn_id}</code>")
    for field_emoji, _field_label, value in extra:
        if value not in (None, ""):
            payment_lines.append(f"{field_emoji} {value}")
    if payment_lines:
        lines.extend(payment_lines)
        lines.append("")

    # 🕒 Submitted
    if submitted:
        lines.append(f"🕒 {submitted}")
        lines.append("")

    # ⚠ Auto Verify — one compact status + reason line pair. Never a raw
    # provider/exception string: callers supply a short human reason, and
    # a safe generic fallback is used when they don't.
    if verification_status and status_key == "pending_review":
        status_word = _VERIFICATION_STATUS_LABEL.get(verification_status, "Failed")
        reason_icon = _VERIFICATION_ICON.get(verification_status, "❌")
        reason_text = verification_reason or _DEFAULT_VERIFICATION_REASON.get(
            verification_status, _DEFAULT_VERIFICATION_REASON["failed"]
        )
        lines.append(f"⚠ Auto Verify: {status_word}")
        lines.append(f"{reason_icon} {reason_text}")
        lines.append("")
    elif verification_result:
        lines.append(f"⚠ Verify: {verification_result}")
        lines.append("")

    if note:
        lines.append(note)
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def admin_resolution_suffix(action: str, actor_label: str, reason: Optional[str] = None) -> str:
    """Small standardized suffix appended to an admin card once resolved,
    e.g. '\\n\\n🟢 Approved by @admin'. Keeps admin history readable while
    still relying on the same status vocabulary as everywhere else."""
    badge = {
        "approved": "🟢 Approved",
        "rejected": "🔴 Rejected",
        "verified": "🟢 Verified & Approved",
    }.get(action, action)
    out = f"\n\n{badge} by {actor_label}"
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


def _extract_user_id_from_cb(view_user_cb: Optional[str]) -> Optional[str]:
    """Best-effort pull of the trailing Telegram user id out of a
    ``view_user_cb`` like ``admin_view_user_pmv_123456789`` so the
    keyboard can offer a direct "Message User" link without any caller
    having to pass the id separately or a new callback/handler being
    introduced — the link is a plain ``tg://user?id=`` deep link, not a
    bot callback, so it needs no routing of its own.
    """
    if not view_user_cb:
        return None
    tail = view_user_cb.rsplit("_", 1)[-1]
    return tail if tail.isdigit() else None


def admin_review_keyboard(
    *,
    verify_cb: Optional[str] = None,
    approve_cb: Optional[str] = None,
    reject_cb: Optional[str] = None,
    view_user_cb: Optional[str] = None,
    history_cb: Optional[str] = None,
    back_cb: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Standard admin review keyboard. Order is always:
    ✅ Approve / ❌ Reject, then 🔄 Verify Again / 👤 View User, then
    💬 Message User / 📜 Deposit History, then ⬅ Back. A button is omitted
    only if its callback wasn't provided (e.g. some gateways have no
    automated re-verification, or a caller has no natural "back"
    destination), but relative order among the buttons that *are* present
    never changes.
    """
    row1, row2, row3, row4 = [], [], [], []
    if approve_cb:
        row1.append(InlineKeyboardButton("✅ Approve", callback_data=approve_cb))
    if reject_cb:
        row1.append(InlineKeyboardButton("❌ Reject", callback_data=reject_cb))
    if verify_cb:
        row2.append(InlineKeyboardButton("🔄 Verify Again", callback_data=verify_cb))
    if view_user_cb:
        row2.append(InlineKeyboardButton("👤 View User", callback_data=view_user_cb))
    msg_user_id = _extract_user_id_from_cb(view_user_cb)
    if msg_user_id:
        row3.append(InlineKeyboardButton("💬 Message User", url=f"tg://user?id={msg_user_id}"))
    if history_cb:
        row3.append(InlineKeyboardButton("📜 Deposit History", callback_data=history_cb))
    if back_cb:
        row4.append(InlineKeyboardButton("⬅ Back", callback_data=back_cb))
    rows = [r for r in (row1, row2, row3, row4) if r]
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

    Compact inline layout — identical for every gateway (mobile banking,
    crypto, card, etc.); only the payment_method label changes:

        ✅ Deposit Successful!

        💵 Amount Credited: $10.00 USD
        💳 Payment Method: Bybit Pay
        🎁 Bonus: +1.00 USD     ← only when a bonus was applied
        🧾 Deposit ID: DEP-YYYYMMDD-XXXXXX
        🕒 Time: Just now

        🎉 Your wallet has been credited successfully. Thank you for using our service!
    """
    lines = ["✅ <b>Deposit Successful!</b>", ""]
    lines.append(f"💵 <b>Amount Credited:</b> {amount}")
    lines.append(f"💳 <b>Payment Method:</b> {payment_method}")
    if bonus_line:
        lines.append(f"🎁 <b>Bonus:</b> {bonus_line}")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{deposit_id}</code>")
    lines.append("🕒 <b>Time:</b> Just now")
    lines.extend([
        "",
        "🎉 Your wallet has been credited successfully. Thank you for using our service!",
    ])
    return "\n".join(lines)


def binance_deposit_success_card(
    *,
    amount: str,
    deposit_id: Optional[str] = None,
    bonus_line: Optional[str] = None,
) -> str:
    """Binance Pay success receipt, isolated from other gateway UIs."""
    lines = ["✅ <b>Deposit Successful!</b>", ""]
    lines.append(f"💵 <b>Amount Credited:</b> {amount}")
    lines.append("💳 <b>Payment Method:</b> Binance Pay")
    if bonus_line:
        lines.append(f"🎁 <b>Bonus:</b> {bonus_line}")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> {deposit_id}")
    lines.append("🕒 <b>Time:</b> Just now")
    lines.extend([
        "",
        "🎉 Your wallet has been credited successfully. Thank you for using our service!",
    ])
    return "\n".join(lines)


def deposit_success_keyboard() -> None:
    """No inline keyboard is shown after a successful deposit.
    The success message ends at the confirmation text."""
    return None


# ─────────────────────────────────────────────────────────────────────────
# Universal "Verifying Payment" status flow
#
# ONE shared visual language for the moment between "user just submitted a
# TXID / Transaction ID / TrxID / Order ID / payment proof" and "we have a
# final answer" — used identically by every gateway (Binance Pay, Bybit
# Pay, every on-chain network, bKash, Nagad, Rocket, generic manual, and
# any gateway added later) so the user is never left staring at their own
# input with no feedback while auto-verification runs.
#
#   1. verifying_card() / verifying_keyboard() — shown the instant the
#      submission is received, before any verification call is made.
#      Buttons are swapped for inert "noop" buttons (see bot.py's shared
#      no-op handler) so nothing is tappable while the check is running.
#   2. verification_in_progress_card() — shown when auto-verification
#      could not reach a definitive answer within its automatic attempts
#      but hasn't been rejected either (e.g. the network confirmation is
#      just slow) — a softer state than a hard failure.
#   3. On a definitive outcome, callers reuse the existing
#      deposit_success_card()/deposit_success_keyboard() (verified) or
#      pending_review_card()/pending_review_keyboard() (queued for admin
#      manual review) — unchanged, so the final screens look exactly as
#      they always have.
# ─────────────────────────────────────────────────────────────────────────

def verifying_card() -> str:
    """Shown immediately after the user submits a TXID / Transaction ID /
    TrxID / Order ID / payment proof, for every gateway, while automatic
    verification runs. Never skipped and never left showing indefinitely —
    callers always follow up with a definitive edit once verification
    resolves."""
    return (
        "⏳ <b>Verifying Payment</b>\n\n"
        "Your payment information has been received.\n\n"
        "🔍 Verifying your payment...\n"
        "⏱ Please wait a few seconds."
    )


def binance_verifying_card(*, order_id: str, deposit_id: Optional[str] = None) -> str:
    """Binance Pay verification state with the two user-safe references."""
    lines = [
        "🔎 <b>Verifying Your Payment</b>",
        "",
        "Please wait while we verify your transaction.",
        "",
        f"🧾 <b>Order ID:</b> {copy_code(order_id)}",
    ]
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> {copy_code(deposit_id)}")
    lines.extend([
        "",
        "⏳ This usually takes a few seconds...",
    ])
    return "\n".join(lines)


def verifying_keyboard() -> InlineKeyboardMarkup:
    """All action buttons 'disabled' while auto-verification is running.
    Real Telegram buttons can't be greyed out, so — consistent with this
    bot's existing no-op convention (pagination labels, section dividers)
    — this shows a single inert button routed to the shared noop handler
    instead of a live action, rather than leaving the impression that
    tapping something will do anything right now."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Verifying…", callback_data="noop")],
    ])


def crypto_verifying_card(
    txhash: Optional[str] = None,
    deposit_id: Optional[str] = None,
) -> str:
    """Premium 'Verifying Your Payment' screen for on-chain crypto deposits
    (USDT TRC20/BEP20/ERC20, LTC, SOL, TON, etc.). Shows the submitted
    TxHash and Deposit ID so the user can confirm the right transaction is
    being checked. Presentation-only — callers own all verification logic.
    """
    lines = ["🔎 <b>Verifying Your Payment</b>", ""]
    lines.append("Please wait while we confirm your transaction on the blockchain.")
    lines.append("")
    if txhash:
        lines.append(f"🧾 <b>TxHash:</b> <code>{txhash}</code>")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{deposit_id}</code>")
    lines.append("")
    lines.append("⏳ Waiting for network confirmation...")
    return "\n".join(lines)


def crypto_blockchain_confirmation_pending_card() -> str:
    """Shown while a crypto transaction has been found on-chain but has not
    yet accumulated enough confirmations for automatic credit. Clean,
    reassuring copy — the user does not need to do anything; they will be
    notified automatically. No IDs shown (callers can append them if needed).
    Presentation-only.
    """
    return (
        "⏳ <b>Verification in Progress</b>\n\n"
        "Your transaction is currently waiting for blockchain confirmations. "
        "You will receive an automated notification as soon as your wallet is credited.\n\n"
        "⏱ This may take a few minutes depending on network congestion."
    )


def crypto_verification_pending_card(
    deposit_id: Optional[str] = None,
    txhash: Optional[str] = None,
) -> str:
    """Shown when auto-verification of a crypto deposit could not complete
    (API timeout, node unavailable, explorer down, etc.) and the deposit
    has been queued for admin manual review.

    Unlike the generic pending card, this one DOES show the Deposit ID and
    TxHash — the user submitted them and the spec requires they are visible
    here so the user can reference their ticket if needed. The deposit is
    still in the queue; callers never change any verification logic.
    """
    lines = ["⏳ <b>Verification in Progress</b>", ""]
    lines.append(
        "Your transaction could not be verified automatically at this time."
    )
    lines.append("")
    lines.append(
        "Your deposit has been placed in the Pending Review queue and will be reviewed shortly."
    )
    if deposit_id or txhash:
        lines.append("")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{deposit_id}</code>")
    if txhash:
        lines.append(f"🔗 <b>TxHash:</b> <code>{txhash}</code>")
    lines.extend([
        "",
        "⏱ <b>Estimated Review Time:</b> 5–30 Minutes",
        "",
        "You will receive an automatic notification once your wallet has been credited.",
    ])
    return "\n".join(lines)


def bybit_verifying_card(
    order_id: Optional[str] = None,
    deposit_id: Optional[str] = None,
) -> str:
    """Premium 'Verifying Your Payment' screen for Bybit Pay (UID Transfer).
    Shows the submitted Order ID and the Deposit ID so the user can confirm
    they are waiting on the right order. Presentation-only — callers still
    own all verification logic.
    """
    lines = ["🔎 <b>Verifying Your Payment</b>", ""]
    lines.append("Please wait while we verify your transaction.")
    lines.append("")
    if order_id:
        lines.append(f"🧾 <b>Order ID:</b> <code>{order_id}</code>")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{deposit_id}</code>")
    lines.append("")
    lines.append("⏳ This usually takes a few seconds...")
    return "\n".join(lines)


def bybit_verification_pending_card() -> str:
    """Shown to the user when a Bybit Pay payment is queued for admin
    manual review (auto-verification could not confirm it).

    Friendly, reassuring copy — no technical error details, no internal
    IDs. The deposit is still being processed; the user does not need to
    do anything.
    """
    return (
        "⏳ <b>Verification in Progress</b>\n\n"
        "Your payment is currently under review. You will receive an "
        "automated notification as soon as your wallet is credited.\n\n"
        "⏱ This usually takes a few minutes."
    )


def mobile_money_verifying_card(
    txid: Optional[str] = None,
    deposit_id: Optional[str] = None,
) -> str:
    """Premium 'Verifying Your Payment' screen for mobile-money gateways
    (bKash / Nagad / Rocket / Upay). Shows the submitted TrxID and the
    Deposit ID so the user can confirm they are waiting on the right order.
    Presentation-only — callers still own all verification logic.
    """
    lines = ["🔎 <b>Verifying Your Payment</b>", ""]
    lines.append("Please wait while we verify your transaction.")
    lines.append("")
    if txid:
        lines.append(f"🧾 <b>TrxID:</b> <code>{txid}</code>")
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> <code>{deposit_id}</code>")
    lines.append("")
    lines.append("⏳ This usually takes a few seconds...")
    return "\n".join(lines)


def mobile_money_verification_pending_card() -> str:
    """Shown to the user when a bKash / Nagad / Rocket / Upay payment is
    queued for admin manual review (auto-verification could not confirm it).

    Friendly, reassuring copy — no technical error details, no internal
    IDs. Matches the Binance Pay / Bybit Pay equivalent tone. The deposit
    is still being processed; the user does not need to do anything.
    """
    return (
        "⏳ <b>Verification in Progress</b>\n\n"
        "Your payment is currently under review. You will receive an "
        "automated notification as soon as your wallet is credited.\n\n"
        "⏱ This usually takes a few minutes."
    )


def verification_in_progress_card(
    *,
    gateway_key: Optional[str] = None,
    gateway_label_override: Optional[str] = None,
    amount: Optional[str] = None,
    order_id=None,
    txn_id: Optional[str] = None,
) -> str:
    """Shown when automatic verification exhausted its attempts without a
    definitive success OR failure — e.g. the network confirmation is just
    running slower than usual. Distinct from pending_review_card (used
    when verification came back with a definitive negative result and the
    deposit has been queued for admin review): this is a softer "still
    working on it" state."""
    label, _emoji = gateway_meta(gateway_key, gateway_label_override)
    dep_id = _display_deposit_id(order_id)
    show_txn_id = bool(txn_id) and str(txn_id) != str(dep_id)

    lines = ["🔄 <b>Verification In Progress</b>", "", f"💳 {label}"]
    if amount:
        lines.append(f"💰 {copy_code(amount)}")
    lines.append("")
    if dep_id:
        lines.append(f"🆔 {copy_code(dep_id)}")
    if show_txn_id:
        lines.append(f"🔗 {copy_code(txn_id)}")
    lines.append("")
    lines.append(
        "We're still confirming this payment — this can take a little "
        "longer than usual. You'll be notified the moment it's verified, "
        "and our team is on standby if it needs a closer look."
    )
    return "\n".join(lines)


def binance_verification_pending_card(
    *, order_id: str, deposit_id: Optional[str] = None,
) -> str:
    """Binance Pay copy for an automatic check awaiting final review."""
    lines = [
        "⏳ <b>Verification in Progress</b>",
        "",
        "Your payment is currently under review. You will receive an "
        "automated notification as soon as your wallet is credited.",
        "",
        f"🧾 <b>Order ID:</b> {copy_code(order_id)}",
    ]
    if deposit_id:
        lines.append(f"🧾 <b>Deposit ID:</b> {copy_code(deposit_id)}")
    lines.extend([
        "",
        "⏱ This usually takes a few minutes.",
    ])
    return "\n".join(lines)


async def edit_or_reply(message, text: str, *, reply_markup=None, parse_mode: str = 'HTML'):
    """Edit an existing status message in place; if that fails for any
    reason (deleted, too old, never sent), send a fresh reply instead so
    the user always ends up with a final answer. Returns the Message that
    ends up carrying the final text, so callers can keep chaining edits
    (e.g. remember_pending_message) off of it."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return message
    except Exception:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def payment_failed_keyboard(retry_cb: str = "topup") -> InlineKeyboardMarkup:
    """Standard keyboard shown whenever a payment could not go through:
    🔄 Try Again · 📞 Contact Support · 🏠 Back to Menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data=retry_cb)],
        [InlineKeyboardButton("📞 Contact Support", callback_data="support")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def still_pending_keyboard(
    resubmit_cb: Optional[str] = None,
    *,
    resubmit_label: str = "🔄 Submit TXID Again",
) -> InlineKeyboardMarkup:
    """Standard keyboard shown after the user cancels a TXID-submission
    mini-step (the order itself is still PENDING, only the "enter your
    TXID" prompt was dismissed). Without this keyboard the user is left
    on a plain text message with no buttons — a dead end. If
    ``resubmit_cb`` is given (e.g. ``"bybit_submit:123"``), offer a direct
    "Submit TXID Again" button; wallet / support / menu are always shown.
    """
    rows = []
    if resubmit_cb:
        rows.append([InlineKeyboardButton(resubmit_label, callback_data=resubmit_cb)])
    rows.append([InlineKeyboardButton("👛 My Wallet", callback_data="wallet"),
                 InlineKeyboardButton("📞 Support", callback_data="support")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def payment_expired_keyboard() -> InlineKeyboardMarkup:
    """Standard keyboard shown whenever a payment window expires:
    💳 Create New Deposit · 🔄 Generate New Payment · 📜 Deposit History ·
    👛 My Wallet · 🏠 Back to Menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Create New Deposit", callback_data="topup")],
        [InlineKeyboardButton("🔄 Generate New Payment", callback_data="topup")],
        [InlineKeyboardButton("📜 Deposit History", callback_data="wallet_history"),
         InlineKeyboardButton("👛 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


# ─────────────────────────────────────────────────────────────────────────
# Single source of truth for "how many deposits are waiting for a human
# right now" — every screen that shows a Pending Deposits counter (the
# Payments dashboard badge, the Payments menu header, the Pending Deposits
# list header) must call THIS function instead of writing its own filter,
# or the numbers drift apart the moment one of them is edited and the
# others aren't (the exact bug this audit was asked to fix).
#
# Two independent DB states can mean "a deposit is waiting for manual
# review" in this project, and both are counted:
#   1. Transaction rows for the reviewable payment methods (generic Manual
#      Payment methods + bKash/Nagad Manual mode) sitting in PENDING /
#      AWAITING_CONFIRMATION.
#   2. PendingManualVerification rows — a Binance Pay / Bybit Pay / ZiniPay
#      submission whose automatic API check failed and is now queued for a
#      human decision (see database/models.py docstring on that table).
# This module does not alter what created those rows or how they are
# resolved — it only reads the same two states everything else already
# reads, and reports them consistently.
# ─────────────────────────────────────────────────────────────────────────

def reviewable_methods():
    """The payment methods whose PENDING/AWAITING_CONFIRMATION Transaction
    rows represent a deposit genuinely waiting on a human (as opposed to a
    gateway still waiting on its own webhook/API confirmation).

    Sourced from the central Payment Gateway Registry (see
    services/payment_gateway_registry.py / payment_gateway_bootstrap.py)
    instead of a hardcoded tuple — a newly registered gateway with
    verification_mode="manual"/"hybrid" is picked up automatically.
    """
    from database.models import PaymentMethod
    from services.payment_gateway_bootstrap import ensure_bootstrapped
    from services.payment_workflow import reviewable_payment_methods

    ensure_bootstrapped()
    return reviewable_payment_methods(PaymentMethod)


def pending_tx_statuses():
    """The Transaction statuses that mean 'not yet resolved'."""
    from database.models import TransactionStatus
    return (TransactionStatus.PENDING, TransactionStatus.AWAITING_CONFIRMATION)


def pending_deposit_rows(session, sort_desc: bool = True):
    """Load the live pending-deposit rows used by the admin UI.

    The list itself is the authoritative result for the screen.  Callers
    derive the displayed count and empty/list branch from this same result
    instead of running a separate count query that can disagree with the
    rows rendered immediately afterwards.
    """
    from database.models import Transaction

    col = Transaction.created_at.desc() if sort_desc else Transaction.created_at.asc()
    return (
        session.query(Transaction)
        .filter(
            Transaction.payment_method.in_(reviewable_methods()),
            Transaction.status.in_(pending_tx_statuses()),
        )
        .order_by(col)
        .all()
    )


def pending_pmv_rows(session, sort_desc: bool = True):
    """Load the live pending PendingManualVerification rows — the failed
    auto-verification queue for gateways like Binance Pay, Bybit Pay and
    ZiniPay (bKash/Nagad/Rocket). Gateway-agnostic: returns every row with
    status == "pending" regardless of which gateway created it, so a newly
    registered gateway's failed verifications appear here automatically
    with no code change.

    This is the PMV-table counterpart to ``pending_deposit_rows`` (which
    covers the Transaction-table side of the same unified Pending Deposits
    queue). Both are combined by the admin UI (see
    handlers/admin_pending_deposits.py) into ONE list, matching the
    "only failed auto verification reaches admin" workflow requirement —
    no gateway is ever hardcoded out of that list.
    """
    from database.models import PendingManualVerification

    col = (
        PendingManualVerification.created_at.desc()
        if sort_desc else PendingManualVerification.created_at.asc()
    )
    return (
        session.query(PendingManualVerification)
        .filter(PendingManualVerification.status == "pending")
        .order_by(col)
        .all()
    )


def count_pending_deposits(session) -> dict:
    """Return the live, authoritative pending-review counts.

    {
      "deposits": N,               # reviewable Transaction rows pending
      "gateway_verifications": M,  # PendingManualVerification rows pending
      "total": N + M,
    }
    """
    from sqlalchemy import func as _f
    from database.models import Transaction, PendingManualVerification

    # Keep the displayed counter on the exact same live row result used by
    # the Pending Deposits list and its empty-state branch.
    deposits = len(pending_deposit_rows(session))
    gateway_verifications = (
        session.query(_f.count(PendingManualVerification.id))
        .filter(PendingManualVerification.status == "pending")
        .scalar() or 0
    )
    return {
        "deposits": deposits,
        "gateway_verifications": gateway_verifications,
        "total": deposits + gateway_verifications,
    }


def copy_code(value) -> str:
    """Wrap a value in <code> so Telegram lets the user tap-to-copy it —
    the native equivalent of a 'Copy' button for amounts, addresses,
    payment numbers, and transaction IDs."""
    if value is None or value == "":
        return ""
    return f"<code>{value}</code>"


# ─────────────────────────────────────────────────────────────────────────
# Manual Transaction ID submission screen — ONE spec-standardized prompt
# used by every gateway (Binance Pay, Bybit Pay, Crypto Networks, bKash,
# Nagad, Rocket, ZiniPay, and any future manual-verification gateway).
#
# Categories (label auto-selected — never hardcoded per gateway):
#   "binance_bybit"  -> Transaction ID (Order ID)
#   "crypto"         -> TXID (Transaction Hash)
#   "mobile_money"   -> TrxID
#
# Adding a brand-new gateway later never means writing a new prompt: pick
# whichever of the three categories it belongs to (or fall back to the
# generic "Transaction ID") and call submit_txid_prompt(). This is the
# only place the wording lives, so it can never drift between gateways.
# ─────────────────────────────────────────────────────────────────────────

TXID_LABELS: dict[str, str] = {
    "binance_bybit": "Transaction ID (Order ID)",
    "crypto":        "TXID (Transaction Hash)",
    "mobile_money":  "TrxID",
}

TXID_EXAMPLES: dict[str, str] = {
    "binance_bybit": "1839250620476598272",
    "crypto":        "0x9f2e1a4b7c3d8e5f6a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7",
    "mobile_money":  "9XT7K2P1QA",
}


def txid_category_for(name: Optional[str]) -> str:
    """Infer the submission category ('binance_bybit' / 'crypto' /
    'mobile_money') from a gateway or manual-method name, so any
    existing or future payment method — including admin-created ones —
    automatically gets the correct label with zero extra configuration."""
    key = (name or "").lower()
    if any(tag in key for tag in ("binance", "bybit")):
        return "binance_bybit"
    if any(tag in key for tag in (
        "usdt", "usdc", "crypto", "trc20", "bep20", "erc20",
        "btc", "bitcoin", "eth", "ltc", "trx", "bnb", "ton",
    )):
        return "crypto"
    if any(tag in key for tag in ("bkash", "nagad", "rocket", "upay", "mobile")):
        return "mobile_money"
    return "generic"


def txid_label(category: str) -> str:
    """Resolve the correct field label for a submission category, falling
    back to a sensible generic label for anything not yet categorized."""
    return TXID_LABELS.get(category, "Transaction ID")


def txid_example(category: str) -> str:
    return TXID_EXAMPLES.get(category, "1839250620476598272")


def submit_txid_prompt(
    category: str,
    *,
    example_value: Optional[str] = None,
    cancel_cb: str = "cancel",
    provider_name: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """The ONE 'submit your Transaction ID' screen — clean, compact,
    identical shape for every gateway; only the label and example change
    per category. Returns (text, keyboard); caller sends with parse_mode='HTML'.

    ``provider_name`` is optional — when supplied for mobile-money gateways
    (e.g. "bKash"), it personalises the prompt line; omitting it falls back
    to a generic label. Never changes any gateway logic.

    The button is "⬅️ Back", not "❌ Cancel" — tapping it returns to the
    Payment Details (invoice) screen the user came from, without touching
    the still-pending deposit (see e.g. ``binance_cancel_submit`` /
    ``bybit_cancel_submit`` / ``zinipay_cancel_submit`` in
    handlers/payment_handlers.py). The ``cancel_cb`` parameter name is kept
    for backward compatibility with every existing call site.
    """
    label = txid_label(category)
    example = example_value or txid_example(category)

    if category == "mobile_money":
        method_name = provider_name or "Mobile Banking"
        text = (
            f"🧾 <b>Enter Transaction ID</b>\n\n"
            f"Please enter your {method_name} Transaction ID (TrxID) below.\n\n"
            f"💡 Example: <code>{example}</code>"
        )
    elif category == "crypto":
        text = (
            "🧾 <b>Enter Transaction Hash</b>\n\n"
            "Please enter your Transaction Hash (TxHash) below.\n\n"
            f"💡 Example: {copy_code(example)}\n"
            "ℹ️ You'll find this in your wallet or exchange's transaction history."
        )
    else:
        text = (
            f"🧾 <b>Enter {label}</b>\n\n"
            f"Please enter your {label} below.\n\n"
            f"💡 Example: <code>{example}</code>"
        )
    keyboard = with_deposit_cancel(
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=cancel_cb)]])
    )
    return text, keyboard


def binance_order_id_prompt(
    *, example_value: Optional[str] = None,
    cancel_cb: str = "cancel",
) -> Tuple[str, InlineKeyboardMarkup]:
    """The Binance Pay Order ID screen.

    "⬅️ Back" returns to the Binance Pay Payment Details (invoice) screen
    without touching the still-pending deposit — see
    ``handlers/payment_handlers.py:binance_cancel_submit``.
    """
    example = example_value or "1234567890123456789"
    text = (
        "🧾 <b>Enter Order ID</b>\n\n"
        "Please enter your Binance Pay Order ID below.\n\n"
        f"💡 Example: {copy_code(example)}\n"
        "ℹ️ You'll find this in your Binance App → Pay → Transaction History."
    )
    return text, with_deposit_cancel(InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=cancel_cb)],
    ]))


def bybit_order_id_prompt(
    *, example_value: Optional[str] = None,
    cancel_cb: str = "cancel",
) -> Tuple[str, InlineKeyboardMarkup]:
    """The Bybit Pay Order ID screen — spec layout:

        🧾 Enter Order ID

        Please enter your Bybit Pay Order ID below.

        💡 Example: 1234567890123456789
        ℹ️ You'll find this in your Bybit App → Assets → Pay → Transaction History.

        [⬅️ Back]

    "⬅️ Back" returns to the Bybit Pay Payment Details (invoice) screen
    without touching the still-pending deposit — see
    ``handlers/payment_handlers.py:bybit_cancel_submit``.
    """
    example = example_value or "1234567890123456789"
    text = (
        "🧾 <b>Enter Order ID</b>\n\n"
        "Please enter your Bybit Pay Order ID below.\n\n"
        f"💡 Example: {copy_code(example)}\n"
        "ℹ️ You'll find this in your Bybit App → Assets → Pay → Transaction History."
    )
    return text, with_deposit_cancel(InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=cancel_cb)],
    ]))
