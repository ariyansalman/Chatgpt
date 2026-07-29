"""
Amount Selection UI — single, reusable "💳 Add Funds" amount screen.
════════════════════════════════════════════════════════════════════════════

This is the ONE screen every top-up flow shows first, before any payment
gateway or payment method is chosen. It is presentation-only: it never
decides which gateways exist, never validates an amount, never creates a
payment, and never touches the database — same contract as
``services/payment_selection_ui.py`` and ``services/payment_ui.py``.

Why a future gateway never needs its own amount screen:
  • ``handlers/payment_handlers.py`` calls ``build_amount_selection_screen()``
    exactly once, from the single top-level "Add Funds" entry point
    (``topup_start``), *before* ``services/payment_selection_ui.py`` builds
    the payment-method screen.
  • The amount chosen here (preset button or custom text entry) is stored in
    ``context.user_data['topup_amount']`` and carried forward into the
    existing, unmodified payment-method selection + payment-creation
    pipeline — every gateway (current or future) reads that same value
    instead of asking for it again.
  • Registering a brand-new gateway (``services/payment_gateway_bootstrap.py``
    + ``services/payment_gateway_registry.py``) never requires touching this
    module: the gateway simply appears on the payment-method screen that
    follows this one, already knowing the amount.
"""
from __future__ import annotations

from typing import Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# The eight quick-pick amounts (USD), shown four-per-row in a compact 4×2
# grid, in this exact order. Adding/removing a preset here is the ONLY
# change ever needed to change the quick-pick amounts — every gateway
# automatically inherits it.
DEFAULT_PRESET_AMOUNTS: Tuple[float, ...] = (1, 2, 3, 5, 10, 15, 20, 25)

# How many preset buttons per row in the grid.
PRESETS_PER_ROW = 4

# Callback-data prefix for a preset amount button, e.g. "topup_amt_1".
PRESET_CALLBACK_PREFIX = "topup_amt_"
# Callback-data for the "✏️ Custom Amount" button.
CUSTOM_AMOUNT_CALLBACK = "topup_amt_custom"
# Callback-data this screen's payment pages fall back to for their "⬅️ Back"
# button (shared with every other payment page — see
# handlers/payment_handlers.py:cancel_topup). Unused directly on this
# specific screen, which uses BACK_TO_WALLET_CALLBACK below instead; kept
# for callers that reference it.
CANCEL_CALLBACK = "cancel"
# Callback-data for this screen's "🔙 Back" button. This is Step 1 of the
# top-up flow, so Back means leaving the flow entirely and returning to the
# Wallet screen it was opened from — NOT the shared "cancel" callback, which
# instead re-shows the Payment Method screen (that behavior is still correct
# for Back buttons found deeper in the flow, just not for this one).
BACK_TO_WALLET_CALLBACK = "topup_back_to_wallet"
# Callback-data for this screen's "🏠 Main Menu" button — the same global
# callback every other screen in the bot uses (see e.g.
# handlers/payment_handlers.py:topup_main_menu).
MAIN_MENU_CALLBACK = "main_menu"


def _format_preset_label(amount: float) -> str:
    """"$1" for whole-dollar presets, "$2.50" otherwise."""
    if float(amount).is_integer():
        return f"${int(amount)}"
    return f"${amount:.2f}"


def build_amount_selection_screen(
    preset_amounts: Sequence[float] = DEFAULT_PRESET_AMOUNTS,
    min_deposit: "float | None" = None,
    currency: str = "USD",
) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the "💳 Add Funds" amount-selection screen (text + keyboard) —
    a compact, premium marketplace-style layout: title, one-line subtitle,
    an optional minimum-deposit hint, a 4×2 preset grid, a full-width
    Custom Amount button, and a Back / Main Menu row.

    This is the exact same screen for every payment gateway — current and
    future — since it's rendered once, before any gateway is chosen.

    ``min_deposit``/``currency`` are purely informational display values —
    the caller reads them from the existing admin-configured settings and
    passes them in; this module still never enforces or decides them.
    """
    lines = ["💳 <b>Add Funds</b>", "Select the amount you want to add to your wallet."]
    if min_deposit is not None:
        lines.append(f"📋 Minimum: ${min_deposit:.2f} {currency}")
    text = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    # Preset buttons in a compact grid, PRESETS_PER_ROW per row, in the
    # order given.
    row: list[InlineKeyboardButton] = []
    for amount in preset_amounts:
        row.append(InlineKeyboardButton(
            f"💵 {_format_preset_label(amount)}",
            callback_data=f"{PRESET_CALLBACK_PREFIX}{_callback_amount(amount)}",
        ))
        if len(row) == PRESETS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("✏️ Custom Amount", callback_data=CUSTOM_AMOUNT_CALLBACK)])
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data=BACK_TO_WALLET_CALLBACK),
        InlineKeyboardButton("🏠 Main Menu", callback_data=MAIN_MENU_CALLBACK),
    ])

    return text, InlineKeyboardMarkup(rows)


def _callback_amount(amount: float) -> str:
    """Render an amount for use inside callback_data: "1" for whole dollars,
    "2.5" otherwise (Telegram callback_data has no room for ambiguity, and
    every current preset is a whole dollar amount anyway)."""
    if float(amount).is_integer():
        return str(int(amount))
    return str(float(amount))


def parse_preset_callback(callback_data: str) -> "float | None":
    """Inverse of the callback_data built above. Returns None if the string
    isn't a valid preset-amount callback (defensive — should never happen
    since the pattern is anchored in bot.py's CallbackQueryHandler)."""
    if not callback_data or not callback_data.startswith(PRESET_CALLBACK_PREFIX):
        return None
    raw = callback_data[len(PRESET_CALLBACK_PREFIX):]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
