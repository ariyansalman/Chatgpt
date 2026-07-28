"""Admin panel controls for the Bybit Pay payment gateway.

Extended from the original to support:
  - Setting API Key / Secret via the Telegram admin panel (stored in
    PaymentGatewayConfig; env vars are used as fallback).
  - Viewing and resolving pending manual verifications (cases where the
    Bybit API could not automatically confirm a TXID).
  - Editing per-network display names (e.g. TRC20 → USDT (TRC20)).
  - Full address manager per network (view/copy/edit/clear with validation).
  - Network enable/disable validation (cannot enable without a valid address).
  - Min/Max cross-validation (min cannot exceed max).
  - Bonus quick-select buttons (0%, 2%, 5%, 10%, or custom).
  - Paginated payment logs with deposit ID, network, address, amounts, timestamps.
  - Active network list with ✅ status icons.
  - Refresh connection reloads credentials, network list, balances, and status.
"""
from __future__ import annotations

import asyncio
import json
import logging

from telegram import CopyTextButton, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session
from database.models import (
    PaymentGatewayConfig, PendingManualVerification,
)
from utils.permissions import has_permission
from utils.bot_config import cfg
from services.bybit_pay import ALL_NETWORKS

logger = logging.getLogger(__name__)

# ─── Conversation states ───────────────────────────────────────────────────
(
    BYBIT_EDIT_UID,
    BYBIT_EDIT_WALLET_TRC20,
    BYBIT_EDIT_WALLET_BEP20,
    BYBIT_EDIT_WALLET_ERC20,
    BYBIT_EDIT_MIN,
    BYBIT_EDIT_MAX,
    BYBIT_EDIT_EXPIRY,
    BYBIT_EDIT_BONUS,
    BYBIT_EDIT_INSTRUCTIONS,
    BYBIT_EDIT_API_KEY,
    BYBIT_EDIT_API_SECRET,
    BYBIT_EDIT_WALLET_LTC,
    BYBIT_EDIT_WALLET_AVAXC,
    BYBIT_EDIT_WALLET_TON,
    BYBIT_EDIT_WALLET_BASE,
    BYBIT_EDIT_WALLET_ARB,
    BYBIT_EDIT_WALLET_OP,
    BYBIT_EDIT_WALLET_MATIC,
    BYBIT_EDIT_WALLET_SOL,
    BYBIT_EDIT_NETNAME,         # edit per-network display name
) = range(20)

WALLET_FIELD_BY_NETWORK = {
    "TRC20": "bybit_wallet_trc20",
    "BEP20": "bybit_wallet_bep20",
    "ERC20": "bybit_wallet_erc20",
    "LTC": "bybit_wallet_ltc",
    "AVAXC": "bybit_wallet_avaxc",
    "TON": "bybit_wallet_ton",
    "BASE": "bybit_wallet_base",
    "ARBONE": "bybit_wallet_arb",
    "OP": "bybit_wallet_op",
    "MATIC": "bybit_wallet_matic",
    "SOL": "bybit_wallet_sol",
}

# ─── Default friendly display names for networks ──────────────────────────
_DEFAULT_NET_DISPLAY: dict[str, str] = {
    "TRC20":  "USDT TRC20",
    "BEP20":  "USDT BEP20",
    "ERC20":  "USDT ERC20",
    "LTC":    "LTC",
    "AVAXC":  "USDT Avalanche C",
    "TON":    "USDT TON",
    "BASE":   "USDT Base",
    "ARBONE": "USDT Arbitrum",
    "OP":     "USDT Optimism",
    "MATIC":  "USDT Polygon",
    "SOL":    "USDT Solana",
}

# bot_config key for storing custom display names
_NET_NAMES_CFG_KEY = "bybit_net_display_names"

# Minimum valid address length for basic validation
_MIN_ADDR_LEN = 10


# ─── Network display name helpers ─────────────────────────────────────────

def _load_net_names() -> dict[str, str]:
    """Load all custom network display names from bot_config."""
    raw = cfg.get_str(_NET_NAMES_CFG_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_net_names(names: dict[str, str]) -> None:
    """Persist all custom network display names to bot_config."""
    cfg.set(_NET_NAMES_CFG_KEY, json.dumps(names))


def _net_display_name(net: str) -> str:
    """Return the admin-configured display name, or the default friendly name."""
    custom = _load_net_names()
    if net in custom and custom[net].strip():
        return custom[net].strip()
    return _DEFAULT_NET_DISPLAY.get(net, net)


# ─── Config helpers ────────────────────────────────────────────────────────

def _get_or_create_config(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway="bybit_pay").first()
    if not row:
        row = PaymentGatewayConfig(
            gateway="bybit_pay", is_enabled=False,
            bybit_allowed_networks="TRC20,BEP20,ERC20,LTC,AVAXC,TON,BASE,ARBONE,OP,MATIC,SOL",
            bybit_order_expiry_minutes=30,
            bybit_bonus_percent=0.0,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _get_config_dict() -> dict:
    with get_db_session() as session:
        row = _get_or_create_config(session)
        return {
            "enabled": bool(row.is_enabled),
            "uid": row.bybit_uid or "",
            "wallets": {
                "TRC20": row.bybit_wallet_trc20 or "",
                "BEP20": row.bybit_wallet_bep20 or "",
                "ERC20": row.bybit_wallet_erc20 or "",
                "LTC": row.bybit_wallet_ltc or "",
                "AVAXC": row.bybit_wallet_avaxc or "",
                "TON": row.bybit_wallet_ton or "",
                "BASE": row.bybit_wallet_base or "",
                "ARBONE": row.bybit_wallet_arb or "",
                "OP": row.bybit_wallet_op or "",
                "MATIC": row.bybit_wallet_matic or "",
                "SOL": row.bybit_wallet_sol or "",
            },
            "allowed_networks": [
                n.strip().upper()
                for n in (row.bybit_allowed_networks or "TRC20,BEP20,ERC20,LTC,AVAXC,TON,BASE,ARBONE,OP,MATIC,SOL").split(",")
                if n.strip()
            ],
            "min_amount": row.bybit_min_amount or 0.0,
            "max_amount": row.bybit_max_amount or 0.0,
            "order_expiry_minutes": row.bybit_order_expiry_minutes or 30,
            "bonus_percent": row.bybit_bonus_percent or 0.0,
            "instructions": row.bybit_instructions or "",
            "has_db_api_key": bool(row.bybit_api_key),
            "has_db_api_secret": bool(row.bybit_api_secret),
            "api_key_masked": _mask(row.bybit_api_key),
        }


def _mask(value: str | None) -> str:
    if not value or len(value) < 8:
        return "(not set)"
    return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"


def _is_address_valid(address: str) -> bool:
    """Basic address validation: must be at least 10 chars and non-empty."""
    return bool(address and len(address.strip()) >= _MIN_ADDR_LEN)


# ─── Status helpers ────────────────────────────────────────────────────────

def _quick_status_label() -> str:
    from services.bybit_pay import BybitPayService
    svc = BybitPayService()
    if not svc.is_configured():
        return "⚪ Not Configured"
    src = "DB" if svc.credentials_source == "db" else "env var"
    return f"⚙️ Key loaded from {src} — tap 🧪 Test to verify live"


async def _api_status_label() -> str:
    from services.bybit_pay import BybitPayService
    svc = BybitPayService()
    if not svc.is_configured():
        return "⚪ Not Configured"
    ok, msg = await asyncio.to_thread(svc.test_connection)
    src = "DB" if svc.credentials_source == "db" else "env var"
    return f"✅ Connected ({src})" if ok else f"❌ {msg} (source: {src})"


# ─── Keyboards ────────────────────────────────────────────────────────────

def _detail_keyboard(cfg_dict: dict, show_api: bool = False) -> InlineKeyboardMarkup:
    toggle_label   = "🚫 Disable" if cfg_dict["enabled"] else "✅ Enable"
    api_vis_label  = "🙈 Hide API Credentials" if show_api else "👁 Show API Credentials"
    net_rows = []
    for net in ALL_NETWORKS:
        enabled = net in cfg_dict["allowed_networks"]
        wallet = cfg_dict["wallets"].get(net, "")
        display_name = _net_display_name(net)
        # Status indicator: ✅ enabled with address, ⚪ enabled but no address, ⬜ disabled
        if enabled and wallet:
            status_icon = "✅"
        elif enabled and not wallet:
            status_icon = "⚪"
        else:
            status_icon = "⬜"
        label = f"{status_icon} {display_name}" + (
            f" ({wallet[:10]}…)" if len(wallet) > 10 else (f" ({wallet})" if wallet else " ⚠️ no addr")
        )
        net_rows.append([
            InlineKeyboardButton(label, callback_data=f"admin_bybit_toggle_net_{net}"),
            InlineKeyboardButton("✏️ Addr", callback_data=f"admin_bybit_addr_mgr_{net}"),
            InlineKeyboardButton("📝 Name", callback_data=f"admin_bybit_editname_{net}"),
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆔 Edit UID",      callback_data="admin_bybit_edit_uid"),
            InlineKeyboardButton("🔑 Edit API Key",  callback_data="admin_bybit_edit_apikey"),
        ],
        [
            InlineKeyboardButton("🔒 Edit API Secret", callback_data="admin_bybit_edit_apisecret"),
            InlineKeyboardButton(api_vis_label,         callback_data="admin_bybit_toggle_api_show"),
        ],
        *net_rows,
        [
            InlineKeyboardButton("💵 Min Amount",  callback_data="admin_bybit_edit_min"),
            InlineKeyboardButton("💰 Max Amount",  callback_data="admin_bybit_edit_max"),
        ],
        [InlineKeyboardButton("⏱ Order Expiry (min)", callback_data="admin_bybit_edit_expiry")],
        [InlineKeyboardButton("🎁 Bonus %",             callback_data="admin_bybit_bonus_menu")],
        [InlineKeyboardButton("📝 Payment Instructions", callback_data="admin_bybit_edit_instructions")],
        [
            InlineKeyboardButton("🧪 Test API",          callback_data="admin_bybit_test"),
            InlineKeyboardButton("🔄 Refresh Connection", callback_data="admin_bybit_refresh"),
        ],
        [
            InlineKeyboardButton("📋 Pending Verifications", callback_data="admin_bybit_pending"),
            InlineKeyboardButton("📜 Payment Logs",          callback_data="admin_bybit_logs"),
        ],
        [InlineKeyboardButton("💳 Wallet Manager", callback_data="gww:list:bybit_pay")],
        [InlineKeyboardButton(toggle_label,         callback_data="admin_bybit_toggle")],
        [InlineKeyboardButton("🔙 Back",            callback_data="admin_gateways")],
    ])


def _summary_text(cfg_dict: dict, api_status: str = "⚪ Not Configured", show_api: bool = False) -> str:
    status = "✅ Enabled" if cfg_dict["enabled"] else "🚫 Disabled"
    wallets_line = "\n".join(
        f"  {_net_display_name(n)}: <code>{cfg_dict['wallets'][n][:24]}…</code>"
        if len(cfg_dict["wallets"].get(n, "")) > 24
        else f"  {_net_display_name(n)}: <code>{cfg_dict['wallets'].get(n) or '(not set)'}</code>"
        for n in ALL_NETWORKS
    )
    if show_api:
        with get_db_session() as _s:
            _row = _get_or_create_config(_s)
            _key = _row.bybit_api_key or "(not set)"
            _sec = _row.bybit_api_secret or "(not set)"
        api_creds = (
            f"API Key:    <code>{_key}</code>\n"
            f"API Secret: <code>{_sec}</code>\n"
        )
    else:
        api_creds = (
            f"API Key: {cfg_dict['api_key_masked']}\n"
            if cfg_dict["has_db_api_key"]
            else "API Key: (not set in DB — using env var)\n"
            if cfg_dict["has_db_api_key"] is False and not cfg_dict["api_key_masked"].startswith("(")
            else "API Key: (not set)\n"
        )

    # Active networks list — vertical with ✅ icons
    networks_with_addr = [
        n for n in cfg_dict["allowed_networks"]
        if cfg_dict["wallets"].get(n)
    ]
    if networks_with_addr:
        active_list = "\n".join(f"  ✅ {_net_display_name(n)}" for n in networks_with_addr)
    else:
        active_list = "  (none active)"

    return (
        "💙 <b>Bybit Pay</b>\n\n"
        f"Status:     {status}\n"
        f"API Status: {api_status}\n"
        f"Bybit UID:  <code>{cfg_dict['uid'] or '(not set)'}</code>\n"
        f"{api_creds}"
        f"Deposit addresses:\n{wallets_line}\n\n"
        f"<b>Active Networks:</b>\n{active_list}\n\n"
        f"Min amount: ${cfg_dict['min_amount']:.2f}\n"
        f"Max amount: {('$' + format(cfg_dict['max_amount'], '.2f')) if cfg_dict['max_amount'] else 'No limit'}\n"
        f"Order expiry: {cfg_dict['order_expiry_minutes']} minutes\n"
        f"Bonus: {cfg_dict['bonus_percent']:.2f}%\n\n"
        "Verified via Bybit V5 API (GET /v5/asset/deposit/query-*) — READ-ONLY.\n\n"
        "🔑 Set API Key/Secret via the buttons below (DB) or via env vars.\n"
        "📝 Tap the Name button next to any network to rename its display label.\n"
        "✏️ Tap Addr to manage deposit addresses (view/copy/edit/clear)."
    )


# ─── Main view / toggle / test ─────────────────────────────────────────────

async def admin_bybit_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    show_api = context.user_data.get("bybit_show_api", False)
    cfg_dict = _get_config_dict()
    status = await _api_status_label()
    try:
        await query.edit_message_text(
            _summary_text(cfg_dict, status, show_api=show_api),
            reply_markup=_detail_keyboard(cfg_dict, show_api=show_api),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_bybit_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg_dict = _get_config_dict()
    if not cfg_dict["enabled"]:
        from services.bybit_pay import BybitPayService
        svc = BybitPayService()
        if not cfg_dict["uid"] and not any(cfg_dict["wallets"].values()):
            await query.answer("⚠️ Set a Bybit UID or at least one deposit address before enabling.", show_alert=True)
            return
        if not svc.is_configured():
            await query.answer(
                "⚠️ Set BYBIT_API_KEY / BYBIT_API_SECRET (via panel or env var) before enabling.",
                show_alert=True,
            )
            return
        ok, _msg = await asyncio.to_thread(svc.test_connection)
        if not ok:
            await query.answer("⚠️ Bybit API test failed — fix credentials before enabling.", show_alert=True)
            return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.is_enabled = not row.is_enabled
        session.commit()

    await admin_bybit_view(update, context)


async def admin_bybit_toggle_network(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    network = query.data.split("admin_bybit_toggle_net_", 1)[-1].upper()
    if network not in ALL_NETWORKS:
        return

    cfg_dict = _get_config_dict()
    current = list(cfg_dict["allowed_networks"])

    if network not in current:
        # ── Validate before enabling ──────────────────────────────────────
        wallet = cfg_dict["wallets"].get(network, "")
        if not _is_address_valid(wallet):
            display_name = _net_display_name(network)
            await query.answer(
                f"⚪ Cannot enable {display_name}.\n"
                f"Please set a valid deposit address first using the ✏️ Addr button.",
                show_alert=True,
            )
            return
        current.append(network)
    else:
        current.remove(network)

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_allowed_networks = ",".join(current)
        session.commit()

    await admin_bybit_view(update, context)


async def admin_bybit_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🧪 Testing…")
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.bybit_pay import BybitPayService
    svc = BybitPayService()
    ok, msg = await asyncio.to_thread(svc.test_connection)
    result_msg = f"{'🟢 Connected' if ok else '🔴 Connection Failed'}: {msg}"
    await query.answer(result_msg, show_alert=True)
    await admin_bybit_view(update, context)


async def admin_bybit_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 Refresh Connection — reload credentials, network list, balances, status."""
    query = update.callback_query
    await query.answer("🔄 Refreshing…")
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    from services.bybit_pay import BybitPayService
    svc = BybitPayService()
    ok, msg = await asyncio.to_thread(svc.test_connection)
    status_msg = f"✅ {msg}" if ok else f"❌ {msg}"
    await query.answer(f"🔄 Connection refresh: {status_msg}", show_alert=False)

    show_api = context.user_data.get("bybit_show_api", False)
    cfg_dict = _get_config_dict()
    src = "DB" if svc.credentials_source == "db" else "env var"
    full_status = (f"✅ Connected ({src})" if ok else f"❌ {msg} (source: {src})")
    try:
        await query.edit_message_text(
            _summary_text(cfg_dict, full_status, show_api=show_api),
            reply_markup=_detail_keyboard(cfg_dict, show_api=show_api),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_bybit_toggle_api_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """👁 / 🙈 Toggle visibility of API Key / Secret in the detail view."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    current = context.user_data.get("bybit_show_api", False)
    context.user_data["bybit_show_api"] = not current
    await admin_bybit_view(update, context)


# ─── Address Manager (per-network submenu) ─────────────────────────────────

async def admin_bybit_addr_mgr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✏️ Addr — address manager submenu for a specific network."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    net = query.data.replace("admin_bybit_addr_mgr_", "").upper()
    if net not in ALL_NETWORKS:
        await query.answer("⚠️ Unknown network.", show_alert=True)
        return

    cfg_dict = _get_config_dict()
    address = cfg_dict["wallets"].get(net, "")
    display_name = _net_display_name(net)
    enabled = net in cfg_dict["allowed_networks"]

    if address:
        addr_line = f"<code>{address}</code>"
        status_icon = "✅" if enabled else "⬜"
        status_label = f"{status_icon} {'Enabled' if enabled else 'Disabled'}"
    else:
        addr_line = "<i>(not set)</i>"
        status_label = "⚪ Not Configured"

    copy_btn = (
        InlineKeyboardButton("📋 Copy Address", copy_text=CopyTextButton(text=address))
        if address
        else InlineKeyboardButton("📋 No Address to Copy", callback_data=f"admin_bybit_addr_mgr_{net}")
    )

    rows = [
        [copy_btn],
        [InlineKeyboardButton("✏️ Edit Address", callback_data=f"admin_bybit_edit_wallet_{net}")],
    ]
    if address:
        rows.append([
            InlineKeyboardButton("🗑 Clear Address", callback_data=f"admin_bybit_addr_clear_{net}")
        ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_bybit_view")])

    try:
        await query.edit_message_text(
            f"💙 <b>Bybit Pay — Address Manager</b>\n\n"
            f"Network:  <b>{display_name}</b>  (<code>{net}</code>)\n"
            f"Status:   {status_label}\n"
            f"Address:\n{addr_line}\n\n"
            "Select an action:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_bybit_addr_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmation screen before clearing a network's deposit address."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    net = query.data.replace("admin_bybit_addr_clear_", "").upper()
    if net not in ALL_NETWORKS:
        return

    display_name = _net_display_name(net)
    cfg_dict = _get_config_dict()
    address = cfg_dict["wallets"].get(net, "")

    try:
        await query.edit_message_text(
            f"🗑 <b>Clear Deposit Address</b>\n\n"
            f"Network: <b>{display_name}</b>\n"
            f"Address: <code>{address or '(empty)'}</code>\n\n"
            "⚠️ This will remove the deposit address and <b>disable</b> this network "
            "for new deposits. Existing deposits are NOT affected.\n\n"
            "Are you sure?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Clear", callback_data=f"admin_bybit_addr_clear_ok_{net}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"admin_bybit_addr_mgr_{net}"),
                ]
            ]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_bybit_addr_clear_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute clearing a network's deposit address and disable the network."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    net = query.data.replace("admin_bybit_addr_clear_ok_", "").upper()
    field = WALLET_FIELD_BY_NETWORK.get(net)
    if not field:
        await query.answer("⚠️ Unknown network.", show_alert=True)
        return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        setattr(row, field, "")
        # Also remove from allowed_networks (disable the network)
        current = [n.strip().upper() for n in (row.bybit_allowed_networks or "").split(",") if n.strip()]
        if net in current:
            current.remove(net)
        row.bybit_allowed_networks = ",".join(current)
        session.commit()

    display_name = _net_display_name(net)
    await query.answer(f"✅ Address cleared and {display_name} disabled.", show_alert=False)
    await admin_bybit_view(update, context)


# ─── Bonus Quick-Select ────────────────────────────────────────────────────

async def admin_bybit_bonus_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🎁 Bonus % — show quick-select options."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg_dict = _get_config_dict()
    current_bonus = cfg_dict["bonus_percent"]

    try:
        await query.edit_message_text(
            f"🎁 <b>Bybit Pay — Bonus %</b>\n\n"
            f"Current bonus: <b>{current_bonus:.2f}%</b>\n\n"
            "Select the deposit bonus percentage.\n"
            "This bonus is automatically applied during deposit calculations.\n\n"
            "Example: 5% bonus on a $100 deposit → $105 credited.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("0%",   callback_data="admin_bybit_bonus_set_0"),
                    InlineKeyboardButton("2%",   callback_data="admin_bybit_bonus_set_2"),
                    InlineKeyboardButton("5%",   callback_data="admin_bybit_bonus_set_5"),
                    InlineKeyboardButton("10%",  callback_data="admin_bybit_bonus_set_10"),
                ],
                [InlineKeyboardButton("✏️ Custom %", callback_data="admin_bybit_edit_bonus")],
                [InlineKeyboardButton("🔙 Back",     callback_data="admin_bybit_view")],
            ]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def admin_bybit_bonus_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick-set bonus % to a predefined value (0/2/5/10)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    value_str = query.data.replace("admin_bybit_bonus_set_", "")
    try:
        value = float(value_str)
    except ValueError:
        await query.answer("⚠️ Invalid value.", show_alert=True)
        return

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_bonus_percent = value
        session.commit()

    await query.answer(f"✅ Bonus set to {value:.0f}%", show_alert=False)
    await admin_bybit_view(update, context)


# ─── Payment Logs (paginated) ─────────────────────────────────────────────

async def admin_bybit_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📜 Paginated Bybit Pay payment logs."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Parse page number from callback data
    data = query.data or "admin_bybit_logs"
    page = 0
    if "_p" in data and data.rsplit("_p", 1)[-1].isdigit():
        page = int(data.rsplit("_p", 1)[-1])

    per_page = 10

    try:
        from database.models import BybitPayTransaction
        with get_db_session() as session:
            total = session.query(BybitPayTransaction).count()
            offset = page * per_page
            rows = (
                session.query(BybitPayTransaction)
                .order_by(BybitPayTransaction.verified_at.desc())
                .offset(offset)
                .limit(per_page)
                .all()
            )
            if not rows and page == 0:
                try:
                    await query.edit_message_text(
                        "📜 <b>Bybit Pay — Payment Logs</b>\n\nNo verified transactions yet.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔙 Back", callback_data="admin_bybit_view")
                        ]]),
                        parse_mode="HTML",
                    )
                except BadRequest:
                    pass
                return

            total_pages = max(1, (total + per_page - 1) // per_page)
            lines = [f"📜 <b>Bybit Pay — Payment Logs</b> (page {page + 1}/{total_pages}, total: {total})\n"]
            for tx in rows:
                created = tx.verified_at.strftime("%m-%d %H:%M") if tx.verified_at else "?"
                updated = tx.verified_at.strftime("%m-%d %H:%M") if tx.verified_at else "?"
                net = f"/{tx.network}" if tx.network else ""
                txid_short = (tx.transaction_id or "")[:16] + "…"
                addr = ""
                # Try to get address from config if onchain
                if tx.network:
                    _cfg = _get_config_dict()
                    addr_full = _cfg["wallets"].get(tx.network or "", "")
                    if addr_full:
                        addr = f"\n  Addr: <code>{addr_full[:20]}…</code>" if len(addr_full) > 20 else f"\n  Addr: <code>{addr_full}</code>"
                lines.append(
                    f"<b>#{tx.id}</b>  [{created}]\n"
                    f"  Type: {tx.payment_type}{net}\n"
                    f"  Amount: {tx.received_amount} {tx.currency}\n"
                    f"  TXID: <code>{txid_short}</code>{addr}\n"
                    f"  Status: ✅ Verified at {updated}"
                )

    except Exception as exc:
        logger.error("admin_bybit_logs error: %s", exc)
        lines = ["📜 <b>Bybit Pay — Payment Logs</b>\n\n❌ Error loading logs."]
        total = 0
        total_pages = 1
        page = 0
        per_page = 10

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_bybit_logs_p{page - 1}"))
    if (page + 1) < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️ Next", callback_data=f"admin_bybit_logs_p{page + 1}"))

    kb_rows = []
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_bybit_logs")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_bybit_view")])

    try:
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─── Pending verifications view ────────────────────────────────────────────

async def admin_bybit_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        rows = (
            session.query(PendingManualVerification)
            .filter_by(gateway="bybit_pay", status="pending")
            .order_by(PendingManualVerification.created_at.desc())
            .limit(10)
            .all()
        )
        if not rows:
            try:
                await query.edit_message_text(
                    "✅ No pending Bybit Pay verifications.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_bybit_pending")],
                        [InlineKeyboardButton("🔙 Back", callback_data="admin_bybit_view")]
                    ]),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        lines = ["📋 <b>Pending Bybit Pay Verifications</b>\n"]
        keyboard_rows = []
        for pmv in rows:
            extra = ""
            if pmv.payment_type:
                extra = f" ({pmv.payment_type}"
                if pmv.network:
                    extra += f"/{pmv.network}"
                extra += ")"
            created_str = pmv.created_at.strftime('%Y-%m-%d %H:%M') if pmv.created_at else "?"
            lines.append(
                f"• <b>Deposit #{pmv.id}</b>  [Order #{pmv.internal_order_id}]{extra}\n"
                f"  User ID: <code>{pmv.telegram_user_id if hasattr(pmv, 'telegram_user_id') else '—'}</code>\n"
                f"  Network: {pmv.network or '—'}\n"
                f"  Amount: {pmv.amount} {pmv.currency}\n"
                f"  TXID: <code>{pmv.submitted_txid}</code>\n"
                f"  Status: {pmv.auto_outcome or 'pending'}\n"
                f"  Time: {created_str}\n"
            )
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"✅ Approve #{pmv.id}",
                    callback_data=f"admin_bybit_approve_{pmv.internal_order_id}_{pmv.id}",
                ),
                InlineKeyboardButton(
                    f"❌ Reject #{pmv.id}",
                    callback_data=f"admin_bybit_reject_{pmv.internal_order_id}_{pmv.id}",
                ),
            ])

    keyboard_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_bybit_pending")])
    keyboard_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_bybit_view")])
    try:
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─── Field editors ─────────────────────────────────────────────────────────

async def _edit_start(update, context, prompt: str, state):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END
    try:
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_bybit_view")]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return state


async def admin_bybit_edit_uid_start(update, context):
    return await _edit_start(update, context, "💬 Send the Bybit UID to show users (your numeric Bybit account UID).", BYBIT_EDIT_UID)


async def admin_bybit_edit_uid_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return BYBIT_EDIT_UID
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_uid = value[:64]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_trc20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT TRC20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_TRC20)


async def admin_bybit_edit_wallet_trc20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_trc20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_bep20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT BEP20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_BEP20)


async def admin_bybit_edit_wallet_bep20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_bep20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_ton_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT TON deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_TON)


async def admin_bybit_edit_wallet_ton_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_ton = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_base_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Base (Coinbase Base L2) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_BASE)


async def admin_bybit_edit_wallet_base_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_base = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_arb_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Arbitrum One deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_ARB)


async def admin_bybit_edit_wallet_arb_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_arb = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_op_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Optimism deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_OP)


async def admin_bybit_edit_wallet_op_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_op = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_matic_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Polygon (MATIC) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_MATIC)


async def admin_bybit_edit_wallet_matic_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_matic = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_sol_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Solana deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_SOL)


async def admin_bybit_edit_wallet_sol_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_sol = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_avaxc_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Avalanche C-Chain deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_AVAXC)


async def admin_bybit_edit_wallet_avaxc_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_avaxc = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_ltc_start(update, context):
    return await _edit_start(update, context, "💬 Send the LTC (Litecoin) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_LTC)


async def admin_bybit_edit_wallet_ltc_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_ltc = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_erc20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT ERC20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_ERC20)


async def admin_bybit_edit_wallet_erc20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_erc20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_min_start(update, context):
    cfg_dict = _get_config_dict()
    return await _edit_start(
        update, context,
        f"💬 Send minimum top-up amount in USD (e.g. 5), or 0 for no minimum.\n"
        f"Current: <b>${cfg_dict['min_amount']:.2f}</b>",
        BYBIT_EDIT_MIN,
    )


async def admin_bybit_edit_min_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BYBIT_EDIT_MIN

    # Cross-validate: min cannot exceed max
    cfg_dict = _get_config_dict()
    max_val = cfg_dict["max_amount"]
    if max_val > 0 and value > max_val:
        await update.message.reply_text(
            f"❌ Minimum (${value:.2f}) cannot exceed Maximum (${max_val:.2f}).\n"
            "Please enter a lower value:"
        )
        return BYBIT_EDIT_MIN

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_min_amount = value
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_max_start(update, context):
    cfg_dict = _get_config_dict()
    return await _edit_start(
        update, context,
        f"💬 Send maximum top-up amount in USD (e.g. 500), or 0 for no maximum.\n"
        f"Current: <b>{'$' + format(cfg_dict['max_amount'], '.2f') if cfg_dict['max_amount'] else 'No limit'}</b>",
        BYBIT_EDIT_MAX,
    )


async def admin_bybit_edit_max_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BYBIT_EDIT_MAX

    # Cross-validate: max cannot be less than min (unless max is 0 = unlimited)
    if value > 0:
        cfg_dict = _get_config_dict()
        min_val = cfg_dict["min_amount"]
        if min_val > 0 and value < min_val:
            await update.message.reply_text(
                f"❌ Maximum (${value:.2f}) cannot be less than Minimum (${min_val:.2f}).\n"
                "Please enter a higher value (or 0 for no limit):"
            )
            return BYBIT_EDIT_MAX

    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_max_amount = value if value > 0 else None
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_expiry_start(update, context):
    return await _edit_start(update, context, "💬 Send order expiry time in minutes (e.g. 30). Minimum: 5.", BYBIT_EDIT_EXPIRY)


async def admin_bybit_edit_expiry_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int((update.message.text or "").strip())
        if value < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid integer of at least 5.")
        return BYBIT_EDIT_EXPIRY
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_order_expiry_minutes = value
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_bonus_start(update, context):
    return await _edit_start(update, context, "💬 Send bonus percentage (e.g. 5 for +5%), or 0 for no bonus.", BYBIT_EDIT_BONUS)


async def admin_bybit_edit_bonus_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BYBIT_EDIT_BONUS
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_bonus_percent = value
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_instructions_start(update, context):
    cfg_dict = _get_config_dict()
    current = cfg_dict["instructions"]
    preview = ""
    if current:
        preview = f"\n\nCurrent instructions:\n<blockquote>{current[:300]}{'…' if len(current) > 300 else ''}</blockquote>"
    return await _edit_start(
        update, context,
        f"💬 Send payment instructions shown on the deposit page "
        f"(or 'default' to reset).{preview}",
        BYBIT_EDIT_INSTRUCTIONS,
    )


async def admin_bybit_edit_instructions_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return BYBIT_EDIT_INSTRUCTIONS
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_instructions = "" if value.lower() == "default" else value[:2000]
        session.commit()
    cfg_dict = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg_dict, _quick_status_label()), reply_markup=_detail_keyboard(cfg_dict), parse_mode="HTML")
    return ConversationHandler.END


# ─── API Key / Secret editors ──────────────────────────────────────────────

async def admin_bybit_edit_apikey_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _edit_start(
        update, context,
        "🔑 <b>Set Bybit API Key</b>\n\n"
        "Send your Bybit V5 API Key (read-only permissions: Assets).\n\n"
        "⚠️ Stored in the database, takes priority over BYBIT_API_KEY env var.\n\n"
        "Send <code>clear</code> to remove the DB key and fall back to the env var.",
        BYBIT_EDIT_API_KEY,
    )


async def admin_bybit_edit_apikey_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send the API Key or 'clear'.")
        return BYBIT_EDIT_API_KEY
    try:
        await update.message.delete()
    except Exception:
        pass
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_api_key = None if value.lower() == "clear" else value
        session.commit()
    cfg_dict = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Key {action}.\n\n" + _summary_text(cfg_dict, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg_dict),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_bybit_edit_apisecret_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _edit_start(
        update, context,
        "🔒 <b>Set Bybit API Secret</b>\n\n"
        "Send your Bybit V5 API Secret.\n\n"
        "⚠️ Stored in the database, takes priority over BYBIT_API_SECRET env var.\n\n"
        "Send <code>clear</code> to remove the DB secret.",
        BYBIT_EDIT_API_SECRET,
    )


async def admin_bybit_edit_apisecret_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send the API Secret or 'clear'.")
        return BYBIT_EDIT_API_SECRET
    try:
        await update.message.delete()
    except Exception:
        pass
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_api_secret = None if value.lower() == "clear" else value
        session.commit()
    cfg_dict = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Secret {action}.\n\n" + _summary_text(cfg_dict, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg_dict),
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ─── Network display name editor ──────────────────────────────────────────

async def admin_bybit_editname_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: start editing a network's display name (admin_bybit_editname_{NET})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    net = query.data.replace("admin_bybit_editname_", "").upper()
    if net not in ALL_NETWORKS:
        await query.answer("⚠️ Unknown network.", show_alert=True)
        return ConversationHandler.END

    current_name = _net_display_name(net)
    default_name = _DEFAULT_NET_DISPLAY.get(net, net)
    context.user_data["bybit_editing_netname"] = net

    try:
        await query.edit_message_text(
            f"📝 <b>Edit Network Display Name</b>\n\n"
            f"Network code: <code>{net}</code>\n"
            f"Current name: <b>{current_name}</b>\n"
            f"Default name: {default_name}\n\n"
            f"Send the new display name for this network.\n"
            f"Examples:\n"
            f"  <code>USDT TRC20</code>\n"
            f"  <code>USDT BEP20</code>\n"
            f"  <code>USDT ERC20</code>\n\n"
            f"Send <code>default</code> to reset to the default name.\n"
            f"The internal network code (<code>{net}</code>) is never changed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="admin_bybit_view")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return BYBIT_EDIT_NETNAME


async def admin_bybit_editname_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new network display name and save it."""
    value = (update.message.text or "").strip()
    net = context.user_data.pop("bybit_editing_netname", "")

    if not net:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❌ Name cannot be empty. Send the display name:")
        context.user_data["bybit_editing_netname"] = net
        return BYBIT_EDIT_NETNAME

    names = _load_net_names()

    if value.lower() == "default":
        names.pop(net, None)
        _save_net_names(names)
        saved_name = _DEFAULT_NET_DISPLAY.get(net, net)
        await update.message.reply_text(
            f"✅ Network <code>{net}</code> reset to default name: <b>{saved_name}</b>",
            parse_mode="HTML",
        )
    else:
        names[net] = value[:80]
        _save_net_names(names)
        await update.message.reply_text(
            f"✅ Network <code>{net}</code> renamed to: <b>{value[:80]}</b>",
            parse_mode="HTML",
        )

    cfg_dict = _get_config_dict()
    await update.message.reply_text(
        _summary_text(cfg_dict, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg_dict),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_bybit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bybit_editing_netname", None)
    await admin_bybit_view(update, context)
    return ConversationHandler.END


def build_bybit_edit_conv():
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, CommandHandler
    from utils.safe_conversation import cancel_command

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_bybit_edit_uid_start, pattern="^admin_bybit_edit_uid$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_trc20_start, pattern="^admin_bybit_edit_wallet_TRC20$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_bep20_start, pattern="^admin_bybit_edit_wallet_BEP20$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_erc20_start, pattern="^admin_bybit_edit_wallet_ERC20$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_ltc_start, pattern="^admin_bybit_edit_wallet_LTC$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_avaxc_start, pattern="^admin_bybit_edit_wallet_AVAXC$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_ton_start, pattern="^admin_bybit_edit_wallet_TON$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_base_start, pattern="^admin_bybit_edit_wallet_BASE$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_arb_start, pattern="^admin_bybit_edit_wallet_ARBONE$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_op_start, pattern="^admin_bybit_edit_wallet_OP$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_matic_start, pattern="^admin_bybit_edit_wallet_MATIC$"),
            CallbackQueryHandler(admin_bybit_edit_wallet_sol_start, pattern="^admin_bybit_edit_wallet_SOL$"),
            CallbackQueryHandler(admin_bybit_edit_min_start, pattern="^admin_bybit_edit_min$"),
            CallbackQueryHandler(admin_bybit_edit_max_start, pattern="^admin_bybit_edit_max$"),
            CallbackQueryHandler(admin_bybit_edit_expiry_start, pattern="^admin_bybit_edit_expiry$"),
            CallbackQueryHandler(admin_bybit_edit_bonus_start, pattern="^admin_bybit_edit_bonus$"),
            CallbackQueryHandler(admin_bybit_edit_instructions_start, pattern="^admin_bybit_edit_instructions$"),
            CallbackQueryHandler(admin_bybit_edit_apikey_start, pattern="^admin_bybit_edit_apikey$"),
            CallbackQueryHandler(admin_bybit_edit_apisecret_start, pattern="^admin_bybit_edit_apisecret$"),
            # Network display name editor
            CallbackQueryHandler(admin_bybit_editname_start, pattern=r"^admin_bybit_editname_[A-Z0-9]+$"),
        ],
        states={
            BYBIT_EDIT_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_uid_value)],
            BYBIT_EDIT_WALLET_TRC20: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_trc20_value)],
            BYBIT_EDIT_WALLET_BEP20: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_bep20_value)],
            BYBIT_EDIT_WALLET_ERC20: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_erc20_value)],
            BYBIT_EDIT_WALLET_LTC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_ltc_value)],
            BYBIT_EDIT_WALLET_AVAXC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_avaxc_value)],
            BYBIT_EDIT_WALLET_TON: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_ton_value)],
            BYBIT_EDIT_WALLET_BASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_base_value)],
            BYBIT_EDIT_WALLET_ARB: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_arb_value)],
            BYBIT_EDIT_WALLET_OP: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_op_value)],
            BYBIT_EDIT_WALLET_MATIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_matic_value)],
            BYBIT_EDIT_WALLET_SOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_wallet_sol_value)],
            BYBIT_EDIT_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_min_value)],
            BYBIT_EDIT_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_max_value)],
            BYBIT_EDIT_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_expiry_value)],
            BYBIT_EDIT_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_bonus_value)],
            BYBIT_EDIT_INSTRUCTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_instructions_value)],
            BYBIT_EDIT_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_apikey_value)],
            BYBIT_EDIT_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_edit_apisecret_value)],
            BYBIT_EDIT_NETNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bybit_editname_value)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_bybit_cancel, pattern="^admin_bybit_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )


def register_extra_handlers(application) -> None:
    """Register all extra (non-conversation) Bybit Pay handlers."""
    from telegram.ext import CallbackQueryHandler as CQH
    application.add_handler(CQH(admin_bybit_addr_mgr,        pattern=r"^admin_bybit_addr_mgr_[A-Z0-9]+$"))
    application.add_handler(CQH(admin_bybit_addr_clear,      pattern=r"^admin_bybit_addr_clear_[A-Z0-9]+$"))
    application.add_handler(CQH(admin_bybit_addr_clear_ok,   pattern=r"^admin_bybit_addr_clear_ok_[A-Z0-9]+$"))
    application.add_handler(CQH(admin_bybit_bonus_menu,      pattern="^admin_bybit_bonus_menu$"))
    application.add_handler(CQH(admin_bybit_bonus_set,       pattern=r"^admin_bybit_bonus_set_\d+$"))
    application.add_handler(CQH(admin_bybit_logs,            pattern=r"^admin_bybit_logs_p\d+$"))
