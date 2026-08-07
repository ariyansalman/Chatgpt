"""
Payment Selection UI — single, reusable "choose a payment method" component.
════════════════════════════════════════════════════════════════════════════

Presentation-only module. It never decides which gateways are enabled or
configured, never creates a payment, never touches wallets/deposits/DB, and
never invents a new callback_data for an existing gateway: every button is
still ``pay_<gateway_key>`` / ``pay_pm_<manual_method_id>``, exactly as
before, so every existing CallbackQueryHandler keeps routing unmodified.

Screens (redesigned menu structure only):
  1. 💰 SELECT PAYMENT METHOD  — Binance Pay, Bybit Pay, 🌐 USDT Networks,
     🪙 Other Coins, 🇧🇩 Local Payment, ⬅️ Back
  2. 🌐 SELECT USDT NETWORK    — TRON (TRC20), BNB Smart Chain (BEP20),
     Ethereum (ERC20), … any other USDT network, ⬅️ Back
  3. 🪙 SELECT COIN            — BTC, LTC, TON, BNB, SOL, POL, … , ⬅️ Back
  4. 🇧🇩 SELECT LOCAL PAYMENT  — bKash, Nagad, Rocket, …, ⬅️ Back

A brand-new gateway still shows up automatically: ``classify()`` buckets it
from keyword hints, exactly as before.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Full checkout flows that are always their own top-level button.
_TOP_LEVEL_KEYS = {"bybit_pay", "binance_pay"}

_CRYPTO_NETWORK_HINTS: Tuple[str, ...] = (
    "usdt", "usdc", "trc20", "bep20", "erc20", "ton", "sol", "avax", "avaxc",
    "matic", "pol", "arb", "op", "base", "ltc", "litecoin", "crypto", "coin",
    "bitcoin", "btc", "eth", "trx", "bnb", "cryptomus", "nowpayments",
    "heleket", "cryptobot",
)
_MOBILE_MONEY_BD_HINTS: Tuple[str, ...] = (
    "bkash", "nagad", "rocket", "upay", "zinipay", "mobile money", "bdt",
)

# A crypto gateway is a "USDT network" when it settles in USDT on a chain.
_USDT_HINTS: Tuple[str, ...] = ("usdt", "usdc", "trc20", "bep20", "erc20")

# Preferred display order inside each crypto submenu (spec order). Anything
# not listed is appended after, in the order it was received.
_USDT_NETWORK_ORDER: Tuple[str, ...] = (
    "bybit_trc20", "bybit_bep20", "bybit_erc20", "bybit_ton",
    "bybit_sol", "bybit_matic", "bybit_base", "bybit_arb",
    "bybit_op", "bybit_avaxc",
)
_OTHER_COIN_ORDER: Tuple[str, ...] = (
    "bybit_btc", "bybit_ltc", "bybit_ton_coin", "bybit_bnb",
    "bybit_sol_coin", "bybit_pol",
)

# Canonical (emoji, LABEL) overrides for the redesigned submenus. Keys not
# listed keep whatever label/emoji the gateway data already carries.
_USDT_NETWORK_DISPLAY = {
    "bybit_trc20": ("🔘", "TRON (TRC20)"),
    "bybit_bep20": ("🟢", "BNB SMART CHAIN (BEP20)"),
    "bybit_erc20": ("🔵", "ETHEREUM (ERC20)"),
    "bybit_ton":   ("🟣", "TON (USDT)"),
    "bybit_sol":   ("🟢", "SOLANA (USDT)"),
    "bybit_matic": ("🟪", "POLYGON (USDT)"),
    "bybit_base":  ("🔷", "BASE (USDT)"),
    "bybit_arb":   ("🔵", "ARBITRUM (USDT)"),
    "bybit_op":    ("🔴", "OPTIMISM (USDT)"),
    "bybit_avaxc": ("🔺", "AVALANCHE (USDT)"),
}
_OTHER_COIN_DISPLAY = {
    "bybit_btc": ("⚫", "BITCOIN (BTC)"),
    "bybit_ltc": ("⚪", "LITECOIN (LTC)"),
    "bybit_bnb": ("🟡", "BNB"),
    "bybit_pol": ("🟪", "POLYGON (POL)"),
}

_TOP_LEVEL_DISPLAY = {
    "binance_pay": ("🟡", "BINANCE PAY"),
    "bybit_pay": ("⚫", "BYBIT PAY"),
}


def classify(key: str, label: str = "") -> str:
    """Return "top" | "crypto_network" | "mobile_money_bd" for one gateway.

    Unchanged public contract — the crypto bucket is further split into
    USDT networks vs. other coins by :func:`classify_crypto`.
    """
    if key in _TOP_LEVEL_KEYS:
        return "top"
    text = f"{key} {label}".lower()
    if any(h in text for h in _MOBILE_MONEY_BD_HINTS):
        return "mobile_money_bd"
    if any(h in text for h in _CRYPTO_NETWORK_HINTS):
        return "crypto_network"
    return "top"


def classify_crypto(key: str, label: str = "") -> str:
    """Return "usdt_network" or "other_coin" for an already-crypto gateway."""
    text = f"{key} {label}".lower()
    return "usdt_network" if any(h in text for h in _USDT_HINTS) else "other_coin"


def _btn(gw: dict, label: Optional[str] = None, emoji: Optional[str] = None,
         callback_key: Optional[str] = None) -> InlineKeyboardButton:
    display_emoji = emoji or gw.get("emoji", "💳")
    text = f'{display_emoji} {label or gw["label"]}'
    if gw.get("_featured"):
        text += " ⭐"
    if gw.get("_recommended"):
        text += " 👍"
    return InlineKeyboardButton(text, callback_data=f'pay_{callback_key or gw["key"]}')


def _display_btn(gw: dict, table: dict) -> InlineKeyboardButton:
    override = table.get(gw["key"])
    if override:
        return _btn(gw, label=override[1], emoji=override[0])
    return _btn(gw, label=str(gw.get("label", gw["key"])).upper())


def _apply_dynamic_config(gateways: Optional[Iterable[dict]]) -> List[dict]:
    """Merge the admin-managed, database-driven payment networks
    (services/payment_networks.py) into the gateway list. Presentation only:
    every button still carries an EXISTING callback_data. Falls back to the
    incoming list untouched when nothing is configured."""
    try:
        from services.payment_networks import overlay_gateways
        return overlay_gateways(gateways)
    except Exception:  # noqa: BLE001
        return [dict(g) for g in (gateways or [])]


def _split(gateways: Optional[Iterable[dict]]):
    """Bucket gateways into (top, usdt_networks, other_coins, local)."""
    gateways = _apply_dynamic_config(gateways)
    top: List[dict] = []
    usdt: List[dict] = []
    coins: List[dict] = []
    mobile: List[dict] = []
    for gw in gateways or []:
        bucket = gw.get("_bucket") or classify(gw["key"], gw.get("label", ""))
        if bucket == "usdt":
            usdt.append(gw)
            continue
        if bucket == "coins":
            coins.append(gw)
            continue
        if bucket == "mobile":
            mobile.append(gw)
            continue
        if bucket == "crypto_network":
            if classify_crypto(gw["key"], gw.get("label", "")) == "usdt_network":
                usdt.append(gw)
            else:
                coins.append(gw)
        elif bucket == "mobile_money_bd":
            mobile.append(gw)
        else:
            top.append(gw)
    # Binance Pay leads, then Bybit Pay (spec order).
    priority = {"binance_pay": 0, "bybit_pay": 1}
    top.sort(key=lambda g: (
        0 if g.get("_order") is not None else 1,
        g.get("_order", 0),
        priority.get(g["key"], 2),
    ))
    return top, usdt, coins, mobile


def _ordered(items: Sequence[dict], order: Tuple[str, ...]) -> List[dict]:
    """Admin-defined display order wins; anything without one keeps the
    legacy hardcoded spec order, then insertion order."""
    rank = {key: i for i, key in enumerate(order)}
    return sorted(
        items,
        key=lambda g: (
            0 if g.get("_order") is not None else 1,
            g.get("_order", 0),
            rank.get(g["key"], len(rank)),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────
# Screen 1 — 💰 SELECT PAYMENT METHOD
# ─────────────────────────────────────────────────────────────────────────

def build_payment_selection_screen(
    gateways: Optional[Sequence[dict]],
    methods: Optional[Sequence] = (),
    amount: "float | None" = None,
    currency: str = "USD",
) -> Tuple[str, InlineKeyboardMarkup]:
    top, usdt, coins, mobile = _split(gateways)

    rows: List[List[InlineKeyboardButton]] = []
    for gw in top:
        override = _TOP_LEVEL_DISPLAY.get(gw["key"])
        if override:
            rows.append([_btn(gw, label=override[1], emoji=override[0])])
        else:
            rows.append([_btn(gw, label=str(gw.get("label", gw["key"])).upper())])

    for m in (methods or []):
        rows.append([InlineKeyboardButton(
            f"{m.emoji or '💳'} {str(m.name).upper()}", callback_data=f"pay_pm_{m.id}",
        )])

    if usdt:
        rows.append([InlineKeyboardButton("🌐 USDT NETWORKS", callback_data="topup_menu_crypto")])
    if coins:
        rows.append([InlineKeyboardButton("🪙 OTHER COINS", callback_data="topup_menu_coins")])
    if mobile or _dynamic_local_rows():
        rows.append([InlineKeyboardButton("🇧🇩 LOCAL PAYMENT", callback_data="topup_menu_mobile")])

    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="topup_back_to_amount")])

    keyboard = InlineKeyboardMarkup(rows)

    if amount is not None:
        text = (
            "💰 <b>SELECT PAYMENT METHOD</b>\n\n"
            f"Deposit Amount: <b>${amount:.2f} {currency}</b>\n\n"
            "Choose how you'd like to pay."
        )
    else:
        text = "💰 <b>SELECT PAYMENT METHOD</b>\n\nChoose how you'd like to pay."
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────
# Screen 2 — 🌐 SELECT USDT NETWORK
# ─────────────────────────────────────────────────────────────────────────

def build_crypto_networks_screen(gateways: Optional[Sequence[dict]]) -> Tuple[str, InlineKeyboardMarkup]:
    """USDT network submenu (same callback entry point as before)."""
    _, usdt, _, _ = _split(gateways)
    rows = [[_display_btn(gw, _USDT_NETWORK_DISPLAY)]
            for gw in _ordered(usdt, _USDT_NETWORK_ORDER)]
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="topup_menu_back")])
    text = "🌐 <b>SELECT USDT NETWORK</b>\n\nChoose the network you'll send USDT on."
    return text, InlineKeyboardMarkup(rows)


# Backwards-compatible alias for any caller that used the old name.
build_usdt_networks_screen = build_crypto_networks_screen


# ─────────────────────────────────────────────────────────────────────────
# Screen 3 — 🪙 SELECT COIN
# ─────────────────────────────────────────────────────────────────────────

def build_other_coins_screen(gateways: Optional[Sequence[dict]]) -> Tuple[str, InlineKeyboardMarkup]:
    _, _, coins, _ = _split(gateways)
    rows = [[_display_btn(gw, _OTHER_COIN_DISPLAY)]
            for gw in _ordered(coins, _OTHER_COIN_ORDER)]
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="topup_menu_back")])
    text = "🪙 <b>SELECT COIN</b>\n\nChoose the coin you'd like to pay with."
    return text, InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────
# Screen 4 — 🇧🇩 SELECT LOCAL PAYMENT
# ─────────────────────────────────────────────────────────────────────────

_MOBILE_MONEY_DISPLAY = {
    "bkash":  ("🩷", "BKASH"),
    "nagad":  ("🟠", "NAGAD"),
    "rocket": ("🔵", "ROCKET"),
    "upay":   ("🔵", "UPAY"),
}


def _dynamic_local_rows() -> Optional[List[List[InlineKeyboardButton]]]:
    """Rows generated from the admin-managed ``local_payment_providers``
    table. Returns None when the admin has not configured any provider yet,
    so the legacy rendering below keeps working untouched."""
    try:
        from services.local_payments import is_configured, local_buttons
        if not is_configured():
            return None
        rows: List[List[InlineKeyboardButton]] = []
        for b in local_buttons():
            label = b["label"]
            if b.get("_default"):
                label += " ⭐"
            rows.append([InlineKeyboardButton(
                f'{b["emoji"]} {label}', callback_data=f'pay_{b["key"]}')])
        return rows
    except Exception:  # noqa: BLE001
        return None


def build_mobile_money_screen(gateways: Optional[Sequence[dict]]) -> Tuple[str, InlineKeyboardMarkup]:
    """Render bKash / Nagad / Rocket / Upay as distinct buttons.

    Routing is unchanged: standalone ``pay_bkash`` / ``pay_nagad`` when those
    gateways are configured, otherwise the per-provider ZiniPay callbacks
    ``pay_zinipay_bkash`` / ``pay_zinipay_nagad`` / ``pay_zinipay_rocket`` /
    ``pay_zinipay_upay``. Only labels/emoji changed here.
    """
    dynamic_rows = _dynamic_local_rows()
    if dynamic_rows is not None:
        dynamic_rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="topup_menu_back")])
        return ("🇧🇩 <b>SELECT LOCAL PAYMENT</b>\n\nChoose your provider.",
                InlineKeyboardMarkup(dynamic_rows))

    _, _, _, mobile = _split(gateways)
    by_key = {gw["key"]: gw for gw in mobile}
    has_zinipay = "zinipay" in by_key

    zini_configured = {}
    if has_zinipay:
        try:
            from services.zinipay_payment import configured_providers
            zini_configured = configured_providers()
        except Exception:
            zini_configured = {}

    rows: List[List[InlineKeyboardButton]] = []
    used_keys = set()

    if "bkash" in by_key:
        emoji, label = _MOBILE_MONEY_DISPLAY["bkash"]
        rows.append([_btn(by_key["bkash"], label=label, emoji=emoji)])
        used_keys.add("bkash")
    elif has_zinipay and zini_configured.get("bkash"):
        emoji, label = _MOBILE_MONEY_DISPLAY["bkash"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_bkash")])

    if "nagad" in by_key:
        emoji, label = _MOBILE_MONEY_DISPLAY["nagad"]
        rows.append([_btn(by_key["nagad"], label=label, emoji=emoji)])
        used_keys.add("nagad")
    elif has_zinipay and zini_configured.get("nagad"):
        emoji, label = _MOBILE_MONEY_DISPLAY["nagad"]
        rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_nagad")])

    if has_zinipay:
        if zini_configured.get("rocket"):
            emoji, label = _MOBILE_MONEY_DISPLAY["rocket"]
            rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_rocket")])
        if zini_configured.get("upay"):
            emoji, label = _MOBILE_MONEY_DISPLAY["upay"]
            rows.append([_btn(by_key["zinipay"], label=label, emoji=emoji, callback_key="zinipay_upay")])
        used_keys.add("zinipay")

    for key, gw in by_key.items():
        if key not in used_keys:
            rows.append([_btn(gw, label=str(gw.get("label", key)).upper())])

    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="topup_menu_back")])

    text = "🇧🇩 <b>SELECT LOCAL PAYMENT</b>\n\nChoose your provider."
    return text, InlineKeyboardMarkup(rows)
