"""
Payment Selection UI — single, reusable "choose a payment method" component.
════════════════════════════════════════════════════════════════════════════

Every screen a user sees while picking how to add funds (the top-level
selector, the Crypto Networks submenu, the Mobile Money (BD) submenu) is
rendered by the three builder functions in this module, from the exact same
``gateways`` list that ``handlers/payment_handlers.py`` already builds today.

This module is presentation-only — same contract as services/payment_ui.py:
it never decides which gateways are enabled/configured, never creates a
payment, and never invents a new callback_data value for an existing
gateway. Every button's callback_data is still ``pay_<gateway_key>`` or
``pay_pm_<manual_method_id>``, exactly as before, so every existing
CallbackQueryHandler in bot.py keeps routing unmodified.

Scaling story — why a new gateway never needs a UI change:
  • ``classify()`` buckets a gateway key/label into "top" (its own button,
    e.g. Bybit Pay / Binance Pay), "crypto_network" (collapses into the
    🔗 Crypto Networks submenu), or "mobile_money_bd" (collapses into the
    🇧🇩 Mobile Money (BD) submenu) — purely from keyword hints, the same
    technique services/payment_ui.py already uses to auto-assign emoji to
    an unknown gateway.
  • Registering a brand-new gateway in services/payment_gateway_bootstrap.py
    and adding its key to the ``gateways`` list built by
    handlers/payment_handlers.py is the only step required for it to show
    up in the right screen here automatically.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Full checkout flows that are always their own top-level button. Note that
# a couple of these are registered with payment_type="crypto" in the
# gateway registry (services/payment_gateway_bootstrap.py) — that field
# describes settlement currency, not "is this an on-chain network picker",
# so it isn't what decides placement here.
_TOP_LEVEL_KEYS = {"bybit_pay", "binance_pay"}

# Keyword hints — mirrors the _EMOJI_HINTS table in services/payment_ui.py
# so classification and emoji-inference stay in sync without a shared
# lookup table. A brand-new gateway key/label is matched the same way an
# unknown gateway already gets a sensible emoji today.
_CRYPTO_NETWORK_HINTS: Tuple[str, ...] = (
    "usdt", "usdc", "trc20", "bep20", "erc20", "ton", "sol", "avax", "avaxc",
    "matic", "arb", "op", "base", "ltc", "litecoin", "crypto", "coin",
    "bitcoin", "btc", "eth", "trx", "bnb", "cryptomus", "nowpayments",
    "heleket", "cryptobot",
)
_MOBILE_MONEY_BD_HINTS: Tuple[str, ...] = (
    "bkash", "nagad", "rocket", "upay", "zinipay", "mobile money", "bdt",
)

# Preferred display order for the well-known crypto networks (spec order).
# Anything not listed here is appended after, in the order it was received —
# so a brand-new network still appears, just at the end of the list.
_CRYPTO_NETWORK_ORDER: Tuple[str, ...] = (
    "bybit_trc20", "bybit_bep20", "bybit_erc20", "bybit_ton",
    "bybit_sol", "bybit_avaxc", "bybit_ltc",
)


def classify(key: str, label: str = "") -> str:
    """Return "top" | "crypto_network" | "mobile_money_bd" for one gateway."""
    if key in _TOP_LEVEL_KEYS:
        return "top"
    text = f"{key} {label}".lower()
    if any(h in text for h in _MOBILE_MONEY_BD_HINTS):
        return "mobile_money_bd"
    if any(h in text for h in _CRYPTO_NETWORK_HINTS):
        return "crypto_network"
    return "top"


# Canonical display emoji for the top-level checkout buttons, applied
# regardless of whatever emoji happens to be stored on the gateway dict —
# keeps iconography consistent across the whole Add Funds flow without
# touching the underlying gateway data or its callback_data.
_TOP_LEVEL_DISPLAY_EMOJI = {
    "bybit_pay": "💳",
    "binance_pay": "💳",
}


def _btn(gw: dict, label: Optional[str] = None, emoji: Optional[str] = None,
         callback_key: Optional[str] = None) -> InlineKeyboardButton:
    display_emoji = emoji or _TOP_LEVEL_DISPLAY_EMOJI.get(gw["key"]) or gw.get("emoji", "💳")
    text = f'{display_emoji} {label or gw["label"]}'
    return InlineKeyboardButton(text, callback_data=f'pay_{callback_key or gw["key"]}')


def _split(gateways: Optional[Iterable[dict]]):
    top: List[dict] = []
    crypto: List[dict] = []
    mobile: List[dict] = []
    for gw in gateways or []:
        bucket = classify(gw["key"], gw.get("label", ""))
        if bucket == "crypto_network":
            crypto.append(gw)
        elif bucket == "mobile_money_bd":
            mobile.append(gw)
        else:
            top.append(gw)
    # Bybit Pay / Binance Pay always lead, in that order, when present.
    priority = {"bybit_pay": 0, "binance_pay": 1}
    top.sort(key=lambda g: priority.get(g["key"], 2))
    return top, crypto, mobile


# ─────────────────────────────────────────────────────────────────────────
# Screen 1 — 💳 Add Funds (top-level selector)
# ─────────────────────────────────────────────────────────────────────────

def build_payment_selection_screen(
    gateways: Optional[Sequence[dict]],
    methods: Optional[Sequence] = (),
    amount: "float | None" = None,
    currency: str = "USD",
) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the redesigned Add Funds screen: one row per top-level
    gateway/manual method, a Crypto Networks row (only if any crypto-network
    gateway is available), a Mobile Banking row (only if any is
    available), and Main Menu.

    ``amount``/``currency`` are purely informational — when the caller
    already knows the deposit amount (it was chosen on Step 1), this
    module renders it back to the user for confirmation; it never decides
    or validates the amount itself.
    """
    top, crypto, mobile = _split(gateways)

    rows: List[List[InlineKeyboardButton]] = [[_btn(gw)] for gw in top]

    for m in (methods or []):
        rows.append([InlineKeyboardButton(
            f"{m.emoji or '💳'} {m.name}", callback_data=f"pay_pm_{m.id}",
        )])

    if crypto:
        rows.append([InlineKeyboardButton("₿ Crypto Networks", callback_data="topup_menu_crypto")])
    if mobile:
        rows.append([InlineKeyboardButton("🇧🇩 Mobile Banking", callback_data="topup_menu_mobile")])

    # Back to the Amount Selection screen (Step 1) — distinct from
    # "topup_menu_back", which returns from the Crypto Networks / Mobile
    # Banking submenus to *this* screen. Always shown: this screen is only
    # ever reached after an amount has already been picked, so there is
    # always a previous step to return to.
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="topup_back_to_amount")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    if amount is not None:
        text = (
            "💳 <b>Add Funds</b>\n\n"
            "Deposit Amount:\n"
            f"💵 ${amount:.2f} {currency}\n\n"
            "Select your preferred payment method."
        )
    else:
        text = "💳 <b>Add Funds</b>\n\nSelect your preferred payment method."
    return text, InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────
# Screen 2 — 🔗 Crypto Networks
# ─────────────────────────────────────────────────────────────────────────

def build_crypto_networks_screen(gateways: Optional[Sequence[dict]]) -> Tuple[str, InlineKeyboardMarkup]:
    _, crypto, _ = _split(gateways)
    order = {key: i for i, key in enumerate(_CRYPTO_NETWORK_ORDER)}
    crypto_sorted = sorted(crypto, key=lambda g: order.get(g["key"], len(order)))

    rows: List[List[InlineKeyboardButton]] = [[_btn(gw)] for gw in crypto_sorted]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="topup_menu_back")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    text = "₿ <b>Crypto Networks</b>\n\nSelect your preferred network."
    return text, InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────
# Screen 3 — 🇧🇩 Mobile Money (BD)
# ─────────────────────────────────────────────────────────────────────────

# Canonical (emoji, display label) for the three BD mobile-money providers,
# per the redesign spec — applied regardless of which underlying gateway
# key currently serves that provider.
_MOBILE_MONEY_DISPLAY = {
    "bkash":  ("🩷", "bKash"),
    "nagad":  ("🧡", "Nagad"),
    "rocket": ("💜", "Rocket"),
    "upay":   ("🔵", "Upay"),
}


def build_mobile_money_screen(gateways: Optional[Sequence[dict]]) -> Tuple[str, InlineKeyboardMarkup]:
    """Render bKash / Nagad / Rocket / Upay as distinct buttons.

    bKash and Nagad each route to their own standalone gateway
    (``pay_bkash`` / ``pay_nagad``) when that gateway is configured, and
    fall back to the combined ZiniPay flow when the standalone one isn't.
    Rocket has no standalone flow today, so it always routes through
    ZiniPay. Any other BD mobile-money gateway added in the future (not
    bkash/nagad/zinipay) still appears automatically, using its own
    label/emoji, after these three.

    IMPORTANT: each ZiniPay-backed provider button gets its OWN callback_data
    — ``pay_zinipay_bkash`` / ``pay_zinipay_nagad`` /
    ``pay_zinipay_rocket`` / ``pay_zinipay_upay`` — instead of all providers
    sharing the plain ``pay_zinipay`` callback. Telegram has no notion of
    "which button in this row was tapped" beyond callback_data, so three
    buttons pointing at the same callback_data are indistinguishable to the
    bot and it can only ever show one (previously: always bKash). The
    generic ``pay_zinipay`` callback is left untouched for any other caller
    that still uses it.
    """
    _, _, mobile = _split(gateways)
    by_key = {gw["key"]: gw for gw in mobile}
    has_zinipay = "zinipay" in by_key

    rows: List[List[InlineKeyboardButton]] = []
    used_keys = set()

    if "bkash" in by_key:
        emoji, label = _MOBILE_MONEY_DISPLAY["bkash"]
        rows.append([_btn(by_key["bkash"], label=label, emoji=emoji)])
        used_keys.add("bkash")
    elif has_zinipay:
        emoji, label = _MOBILE_MONEY_DISPLAY["bkash"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_bkash")])

    if "nagad" in by_key:
        emoji, label = _MOBILE_MONEY_DISPLAY["nagad"]
        rows.append([_btn(by_key["nagad"], label=label, emoji=emoji)])
        used_keys.add("nagad")
    elif has_zinipay:
        emoji, label = _MOBILE_MONEY_DISPLAY["nagad"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_nagad")])

    if has_zinipay:
        emoji, label = _MOBILE_MONEY_DISPLAY["rocket"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_rocket")])
        used_keys.add("zinipay")

        emoji, label = _MOBILE_MONEY_DISPLAY["upay"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_upay")])

    # Any future BD mobile-money gateway that isn't bkash/nagad/zinipay.
    for key, gw in by_key.items():
        if key not in used_keys:
            rows.append([_btn(gw)])

    rows.append([InlineKeyboardButton("🔙 Back", callback_data="topup_menu_back")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    text = "🇧🇩 <b>Mobile Banking</b>\n\nSelect your preferred provider."
    return text, InlineKeyboardMarkup(rows)
