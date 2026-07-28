"""
Payment Gateway Admin Panel — Premium UI  (V1)
═══════════════════════════════════════════════

Single-file, self-contained premium admin panel for every payment-gateway
operation via Telegram. Built against the existing service layer — it
NEVER touches business logic, database schema, wallet-credit paths, webhook
handlers, or existing callback_data strings outside this module.

Public surface
──────────────
  build_main_menu()             → (text, InlineKeyboardMarkup)
  build_gateway_list()          → (text, InlineKeyboardMarkup)
  build_gateway_detail(gw_id)   → (text, InlineKeyboardMarkup)
  build_wallet_panel()          → (text, InlineKeyboardMarkup)
  build_network_panel()         → (text, InlineKeyboardMarkup)
  build_transaction_panel()     → (text, InlineKeyboardMarkup)
  build_search_menu()           → (text, InlineKeyboardMarkup)
  build_bulk_menu(entity)       → (text, InlineKeyboardMarkup)
  build_bulk_confirm(action, entity, ids)  → (text, InlineKeyboardMarkup)
  build_config_panel()          → (text, InlineKeyboardMarkup)
  build_status_overview()       → (text, InlineKeyboardMarkup)

  parse_callback(data)          → dict   (decode any pgadmin_ callback)

Callback-data format (all prefixed "pgadmin_"):
  pgadmin_main              — return to main menu
  pgadmin_gw_list           — full gateway list
  pgadmin_gw_detail:<id>    — single gateway detail
  pgadmin_gw_enable:<id>    — enable gateway
  pgadmin_gw_disable:<id>   — disable gateway
  pgadmin_gw_maintenance:<id> — set maintenance mode
  pgadmin_gw_refresh:<id>   — refresh single gateway status
  pgadmin_gw_reload:<id>    — reload gateway config
  pgadmin_wallet_panel      — wallet management panel
  pgadmin_wallet_search     — prompt: enter wallet query
  pgadmin_network_panel     — network management panel
  pgadmin_network_search    — prompt: enter network query
  pgadmin_tx_panel          — transaction management panel
  pgadmin_tx_search         — prompt: enter transaction query
  pgadmin_search_menu       — search-hub menu
  pgadmin_bulk:<entity>     — bulk-ops menu for entity
  pgadmin_bulk_enable:<entity>   — confirm bulk-enable
  pgadmin_bulk_disable:<entity>  — confirm bulk-disable
  pgadmin_bulk_delete:<entity>   — confirm bulk-delete
  pgadmin_bulk_confirm:<action>:<entity>  — execute confirmed bulk op
  pgadmin_refresh_all       — refresh status of all gateways
  pgadmin_reload_config     — reload entire gateway configuration
  pgadmin_status_overview   — full status dashboard
  pgadmin_config_panel      — configuration panel
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants / helpers
# ═══════════════════════════════════════════════════════════════════════════

CB = "pgadmin"          # callback-data root prefix

# Status display mapping
STATUS_EMOJI = {
    "enabled":     "🟢",
    "disabled":    "🔴",
    "maintenance": "🟡",
    "manual":      "🟡",   # manual mode treated as maintenance indicator
    "auto":        "🟢",
    "active":      "🟢",
    "inactive":    "🔴",
    "frozen":      "❄️",
    "unknown":     "⚪",
}

STATUS_LABEL = {
    "enabled":     "Enabled",
    "disabled":    "Disabled",
    "maintenance": "Maintenance",
    "manual":      "Manual Mode",
    "auto":        "Auto Mode",
    "active":      "Active",
    "inactive":    "Inactive",
    "frozen":      "Frozen",
    "unknown":     "Unknown",
}

# Gateway type display
TYPE_EMOJI = {
    "crypto":        "🔐",
    "mobile_wallet": "📱",
    "wallet":        "💼",
    "manual":        "✍️",
    "card":          "💳",
    "gateway":       "🌐",
}

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
SECTION  = "▸"


def _cb(*parts: str) -> str:
    """Build a pgadmin_ callback-data string."""
    return CB + "_" + ":".join(str(p) for p in parts)


def _status_badge(enabled: bool, mode: str = "enabled") -> str:
    """Return a coloured status badge string."""
    if not enabled:
        return f"{STATUS_EMOJI['disabled']} {STATUS_LABEL['disabled']}"
    if mode in ("maintenance", "manual"):
        return f"{STATUS_EMOJI['maintenance']} {STATUS_LABEL[mode]}"
    return f"{STATUS_EMOJI['enabled']} {STATUS_LABEL['enabled']}"


def _rows_of_2(buttons: List[InlineKeyboardButton]) -> List[List[InlineKeyboardButton]]:
    """Pack a flat list of buttons into rows of 2 (last row may have 1)."""
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])
    return rows


def _nav_row(back_cb: str, home: bool = True) -> List[InlineKeyboardButton]:
    """Standard navigation row (⬅ Back  |  🏠 Menu)."""
    row = [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]
    if home:
        row.append(InlineKeyboardButton("🏠 Menu", callback_data=_cb("main")))
    return row


# ═══════════════════════════════════════════════════════════════════════════
# Data helpers  (best-effort — never raise)
# ═══════════════════════════════════════════════════════════════════════════

def _get_gateway_descriptors() -> List[Any]:
    """Return all GatewayDescriptor objects from the registry, or []."""
    try:
        from services.payment_gateway_bootstrap import ensure_bootstrapped
        from services.payment_gateway_registry import registry
        ensure_bootstrapped()
        return registry.all()
    except Exception:
        logger.exception("_get_gateway_descriptors failed")
        return []


def _get_gateway_db_row(gateway_id: str) -> Optional[Any]:
    """Return a PaymentGatewayConfig row for gateway_id, or None."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        with get_db_session() as s:
            return s.query(PaymentGatewayConfig).filter_by(gateway=gateway_id).first()
    except Exception:
        logger.exception("_get_gateway_db_row failed for %s", gateway_id)
        return None


def _get_wallet_currencies() -> List[Dict]:
    """Return all WalletCurrencyConfig rows as dicts, or []."""
    try:
        from services.multicurrency_wallet import get_all_currencies
        return get_all_currencies()
    except Exception:
        logger.exception("_get_wallet_currencies failed")
        return []


def _get_wallet_stats() -> Dict:
    """Return admin wallet stats dict, or {}."""
    try:
        from services.multicurrency_wallet import get_admin_wallet_stats
        return get_admin_wallet_stats()
    except Exception:
        logger.exception("_get_wallet_stats failed")
        return {}


def _gateway_effective_status(descriptor) -> str:
    """Derive a unified status string from a GatewayDescriptor + DB row."""
    try:
        row = _get_gateway_db_row(descriptor.gateway_id)
        is_enabled = row.is_enabled if row else descriptor.is_enabled()
        mode = (getattr(row, "mode", None) or "auto").lower()
        if not is_enabled:
            return "disabled"
        if mode == "manual":
            return "maintenance"
        return "enabled"
    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# 1.  Main Menu
# ═══════════════════════════════════════════════════════════════════════════

def build_main_menu() -> Tuple[str, InlineKeyboardMarkup]:
    """Premium main menu for the Payment Gateway Admin Panel."""
    now = datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")

    descriptors = _get_gateway_descriptors()
    total   = len(descriptors)
    enabled = sum(1 for d in descriptors if _gateway_effective_status(d) == "enabled")
    maint   = sum(1 for d in descriptors if _gateway_effective_status(d) == "maintenance")
    disabled = total - enabled - maint

    text = (
        f"💳 <b>Payment Gateway Admin</b>\n"
        f"{DIVIDER}\n"
        f"🕐 <i>{now}</i>\n\n"
        f"{SECTION} <b>Status Summary</b>\n"
        f"  🟢 Enabled: <b>{enabled}</b>   "
        f"🔴 Disabled: <b>{disabled}</b>   "
        f"🟡 Maintenance: <b>{maint}</b>\n\n"
        f"{SECTION} Select a section to manage:"
    )

    keyboard = [
        # Row 1 — Gateway & Status
        [
            InlineKeyboardButton("💳 Gateways", callback_data=_cb("gw_list")),
            InlineKeyboardButton("📊 Status Overview", callback_data=_cb("status_overview")),
        ],
        # Row 2 — Wallet & Networks
        [
            InlineKeyboardButton("💼 Wallets", callback_data=_cb("wallet_panel")),
            InlineKeyboardButton("🌐 Networks", callback_data=_cb("network_panel")),
        ],
        # Row 3 — Transactions & Search
        [
            InlineKeyboardButton("📋 Transactions", callback_data=_cb("tx_panel")),
            InlineKeyboardButton("🔍 Search", callback_data=_cb("search_menu")),
        ],
        # Row 4 — Bulk & Config
        [
            InlineKeyboardButton("⚡ Bulk Actions", callback_data=_cb("bulk", "gateways")),
            InlineKeyboardButton("⚙️ Configuration", callback_data=_cb("config_panel")),
        ],
        # Row 5 — Global controls
        [
            InlineKeyboardButton("🔃 Refresh Status", callback_data=_cb("refresh_all")),
            InlineKeyboardButton("♻️ Reload Config", callback_data=_cb("reload_config")),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 2.  Gateway List
# ═══════════════════════════════════════════════════════════════════════════

def build_gateway_list() -> Tuple[str, InlineKeyboardMarkup]:
    """List all registered payment gateways with live status badges."""
    descriptors = _get_gateway_descriptors()

    if not descriptors:
        text = (
            "💳 <b>Payment Gateways</b>\n"
            f"{DIVIDER}\n"
            "⚠️ No gateways registered.\n"
            "Call <code>bootstrap_gateways()</code> at startup."
        )
        keyboard = [[InlineKeyboardButton("🏠 Menu", callback_data=_cb("main"))]]
        return text, InlineKeyboardMarkup(keyboard)

    lines = [f"💳 <b>Payment Gateways</b>", DIVIDER]

    # Group by type
    groups: Dict[str, List] = {}
    for d in descriptors:
        groups.setdefault(d.payment_type, []).append(d)

    type_order = ["crypto", "mobile_wallet", "wallet", "manual", "card", "gateway"]
    type_label = {
        "crypto":        "🔐 Crypto",
        "mobile_wallet": "📱 Mobile Wallets",
        "wallet":        "💼 Wallets",
        "manual":        "✍️ Manual",
        "card":          "💳 Card",
        "gateway":       "🌐 Gateway",
    }

    for gtype in type_order + [t for t in groups if t not in type_order]:
        if gtype not in groups:
            continue
        lines.append(f"\n{type_label.get(gtype, gtype.title())}")
        for d in groups[gtype]:
            status = _gateway_effective_status(d)
            emoji  = STATUS_EMOJI.get(status, "⚪")
            lines.append(f"  {emoji} <b>{d.display_name}</b>")

    lines.append(f"\n{DIVIDER}")
    lines.append(f"Total: <b>{len(descriptors)}</b> gateways · Tap one to manage")
    text = "\n".join(lines)

    # Build gateway buttons (2 per row), sorted by name
    gw_buttons: List[InlineKeyboardButton] = []
    for d in sorted(descriptors, key=lambda x: x.display_name):
        status = _gateway_effective_status(d)
        badge  = STATUS_EMOJI.get(status, "⚪")
        gw_buttons.append(
            InlineKeyboardButton(
                f"{badge} {d.display_name}",
                callback_data=_cb("gw_detail", d.gateway_id),
            )
        )

    keyboard = _rows_of_2(gw_buttons)

    # Action row
    keyboard.append([
        InlineKeyboardButton("🔃 Refresh All", callback_data=_cb("refresh_all")),
        InlineKeyboardButton("⚡ Bulk Actions", callback_data=_cb("bulk", "gateways")),
    ])
    keyboard.append(_nav_row(_cb("main")))
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 3.  Gateway Detail
# ═══════════════════════════════════════════════════════════════════════════

def build_gateway_detail(gateway_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Detail panel for a single payment gateway."""
    try:
        from services.payment_gateway_registry import registry
        d = registry.get(gateway_id)
    except Exception:
        d = None

    if not d:
        text = f"⚠️ Gateway <code>{gateway_id}</code> not found."
        keyboard = [_nav_row(_cb("gw_list"))]
        return text, InlineKeyboardMarkup(keyboard)

    row   = _get_gateway_db_row(gateway_id)
    status = _gateway_effective_status(d)
    badge  = STATUS_EMOJI.get(status, "⚪")
    mode   = (getattr(row, "mode", None) or "auto").lower() if row else "auto"
    is_enabled = row.is_enabled if row else d.is_enabled()

    type_emoji = TYPE_EMOJI.get(d.payment_type, "🌐")

    lines = [
        f"{badge} <b>{d.display_name}</b>",
        DIVIDER,
        f"{SECTION} ID: <code>{d.gateway_id}</code>",
        f"{SECTION} Type: {type_emoji} {d.payment_type.replace('_', ' ').title()}",
        f"{SECTION} Status: {_status_badge(is_enabled, mode)}",
        f"{SECTION} Currency: <b>{d.currency}</b>",
    ]

    if d.network:
        lines.append(f"{SECTION} Network: <b>{d.network}</b>")

    verify_mode = d.verification_mode.title()
    lines.append(f"{SECTION} Verification: <b>{verify_mode}</b>")

    caps = []
    if d.supports_webhook:          caps.append("Webhook")
    if d.supports_auto_verification: caps.append("Auto-Verify")
    if d.supports_manual_review:    caps.append("Manual Review")
    if d.supports_manual_toggle:    caps.append("Mode Toggle")
    if caps:
        lines.append(f"{SECTION} Capabilities: {', '.join(caps)}")

    if row:
        if getattr(row, "merchant_uuid", None):
            lines.append(f"{SECTION} Merchant: <code>••••{row.merchant_uuid[-4:]}</code>")
        if hasattr(row, "binance_allowed_currencies") and getattr(row, "binance_allowed_currencies", None):
            lines.append(f"{SECTION} Currencies: {row.binance_allowed_currencies}")
        if hasattr(row, "bybit_allowed_networks") and getattr(row, "bybit_allowed_networks", None):
            lines.append(f"{SECTION} Networks: {row.bybit_allowed_networks}")

    if row and getattr(row, "updated_at", None):
        lines.append(f"\n🕐 Updated: {row.updated_at.strftime('%d %b %Y %H:%M')}")

    lines.append(DIVIDER)
    text = "\n".join(lines)

    keyboard: List[List[InlineKeyboardButton]] = []

    # Row 1: Enable / Disable
    if not is_enabled:
        keyboard.append([
            InlineKeyboardButton("✅ Enable", callback_data=_cb("gw_enable", gateway_id)),
            InlineKeyboardButton("🔃 Refresh", callback_data=_cb("gw_refresh", gateway_id)),
        ])
    else:
        row1 = [InlineKeyboardButton("🔴 Disable", callback_data=_cb("gw_disable", gateway_id))]
        if d.supports_manual_toggle:
            label = "🟡 Set Manual" if mode == "auto" else "🟢 Set Auto"
            row1.append(InlineKeyboardButton(label, callback_data=_cb("gw_maintenance", gateway_id)))
        else:
            row1.append(InlineKeyboardButton("🔃 Refresh", callback_data=_cb("gw_refresh", gateway_id)))
        keyboard.append(row1)

    # Row 2: Refresh & Reload
    keyboard.append([
        InlineKeyboardButton("🔃 Refresh Status", callback_data=_cb("gw_refresh", gateway_id)),
        InlineKeyboardButton("♻️ Reload Config", callback_data=_cb("gw_reload", gateway_id)),
    ])

    # Row 3: Nav
    keyboard.append(_nav_row(_cb("gw_list")))
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 4.  Status Overview
# ═══════════════════════════════════════════════════════════════════════════

def build_status_overview() -> Tuple[str, InlineKeyboardMarkup]:
    """Full status dashboard across gateways, wallets, and services."""
    descriptors = _get_gateway_descriptors()
    wallet_stats = _get_wallet_stats()
    currencies = _get_wallet_currencies()
    now = datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")

    lines = [
        "📊 <b>Status Overview</b>",
        DIVIDER,
        f"🕐 <i>{now}</i>",
        "",
        f"<b>💳 Payment Gateways</b>",
    ]

    enabled_gws, maint_gws, disabled_gws = [], [], []
    for d in descriptors:
        st = _gateway_effective_status(d)
        if st == "enabled":      enabled_gws.append(d)
        elif st == "maintenance": maint_gws.append(d)
        else:                     disabled_gws.append(d)

    if enabled_gws:
        lines.append(f"  🟢 <b>Enabled ({len(enabled_gws)})</b>: "
                     + ", ".join(d.display_name for d in enabled_gws))
    if maint_gws:
        lines.append(f"  🟡 <b>Maintenance ({len(maint_gws)})</b>: "
                     + ", ".join(d.display_name for d in maint_gws))
    if disabled_gws:
        lines.append(f"  🔴 <b>Disabled ({len(disabled_gws)})</b>: "
                     + ", ".join(d.display_name for d in disabled_gws))

    if not descriptors:
        lines.append("  ⚠️ No gateways registered")

    # Wallet section
    lines.append("")
    lines.append("<b>💼 Wallets</b>")
    if wallet_stats:
        total_wallets   = wallet_stats.get("total_wallets", 0)
        frozen_wallets  = wallet_stats.get("frozen_wallets", 0)
        enabled_curr    = wallet_stats.get("enabled_currencies", 0)
        total_curr      = wallet_stats.get("total_currencies", 0)
        lines.append(f"  📂 Currencies: {enabled_curr}/{total_curr} enabled")
        lines.append(f"  👛 Wallets: {total_wallets} total · ❄️ {frozen_wallets} frozen")
    else:
        lines.append("  ⚠️ Wallet stats unavailable")

    # Currency status breakdown
    if currencies:
        lines.append("")
        lines.append("<b>🌐 Currency Status</b>")
        for c in currencies[:8]:  # cap at 8 for compactness
            st = (c.get("status") or "unknown").lower()
            em = STATUS_EMOJI.get(st, "⚪")
            sym = c.get("symbol", "")
            lines.append(f"  {em} {sym} <b>{c['code']}</b> — {st.title()}")
        if len(currencies) > 8:
            lines.append(f"  … and {len(currencies) - 8} more")

    lines.append(f"\n{DIVIDER}")
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("💳 Manage Gateways", callback_data=_cb("gw_list")),
            InlineKeyboardButton("💼 Manage Wallets", callback_data=_cb("wallet_panel")),
        ],
        [
            InlineKeyboardButton("🔃 Refresh All", callback_data=_cb("refresh_all")),
            InlineKeyboardButton("♻️ Reload Config", callback_data=_cb("reload_config")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 5.  Wallet Panel
# ═══════════════════════════════════════════════════════════════════════════

def build_wallet_panel() -> Tuple[str, InlineKeyboardMarkup]:
    """Wallet management hub with per-currency status."""
    currencies = _get_wallet_currencies()
    stats      = _get_wallet_stats()

    enabled_count  = sum(1 for c in currencies if c.get("is_enabled"))
    disabled_count = len(currencies) - enabled_count
    frozen_count   = sum(1 for c in currencies if c.get("is_frozen"))

    lines = [
        "💼 <b>Wallet Management</b>",
        DIVIDER,
        f"  📂 Total currencies: <b>{len(currencies)}</b>",
        f"  🟢 Enabled: <b>{enabled_count}</b>  "
        f"🔴 Disabled: <b>{disabled_count}</b>  "
        f"❄️ Frozen: <b>{frozen_count}</b>",
    ]

    if stats:
        lines.append(f"  👛 Total wallets: <b>{stats.get('total_wallets', 0)}</b>")

    if currencies:
        lines.append(f"\n<b>Currencies</b>")
        for c in currencies:
            st = (c.get("status") or ("enabled" if c.get("is_enabled") else "disabled")).lower()
            em = STATUS_EMOJI.get(st, "⚪")
            frozen_tag = " ❄️" if c.get("is_frozen") else ""
            crypto_tag = " · Crypto" if c.get("is_crypto") else ""
            lines.append(f"  {em} <b>{c['code']}</b> {c.get('symbol','')} "
                         f"— {st.title()}{crypto_tag}{frozen_tag}")

    lines.append(DIVIDER)
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("🔍 Search Wallet", callback_data=_cb("wallet_search")),
            InlineKeyboardButton("📊 Stats", callback_data=_cb("status_overview")),
        ],
        [
            InlineKeyboardButton("✅ Bulk Enable", callback_data=_cb("bulk_enable", "wallets")),
            InlineKeyboardButton("🔴 Bulk Disable", callback_data=_cb("bulk_disable", "wallets")),
        ],
        [
            InlineKeyboardButton("🗑️ Bulk Delete", callback_data=_cb("bulk_delete", "wallets")),
            InlineKeyboardButton("🔃 Refresh", callback_data=_cb("refresh_all")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 6.  Network Panel
# ═══════════════════════════════════════════════════════════════════════════

def build_network_panel() -> Tuple[str, InlineKeyboardMarkup]:
    """Network management panel — shows per-gateway networks + health."""
    descriptors = _get_gateway_descriptors()

    # Build a deduplicated list of networks
    networks: Dict[str, List[str]] = {}  # network_name → [gateway_ids]
    for d in descriptors:
        nets: List[str] = []
        if d.network:
            nets.append(d.network)
        # Bybit has multiple networks stored in allowed_networks config
        row = _get_gateway_db_row(d.gateway_id)
        if row and hasattr(row, "bybit_allowed_networks") and getattr(row, "bybit_allowed_networks", None):
            nets.extend(n.strip() for n in row.bybit_allowed_networks.split(",") if n.strip())
        for net in nets:
            networks.setdefault(net, []).append(d.gateway_id)

    lines = [
        "🌐 <b>Network Management</b>",
        DIVIDER,
        f"  📡 Configured networks: <b>{len(networks)}</b>",
        "",
    ]

    if networks:
        lines.append("<b>Networks</b>")
        for net, gw_ids in sorted(networks.items()):
            gw_names = []
            for gid in gw_ids:
                try:
                    from services.payment_gateway_registry import registry
                    d = registry.get(gid)
                    if d:
                        gw_names.append(d.display_name)
                except Exception:
                    gw_names.append(gid)
            lines.append(f"  🔗 <b>{net}</b> — {', '.join(gw_names)}")
    else:
        lines.append("  ⚠️ No networks configured")

    lines.append(DIVIDER)
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("🔍 Search Network", callback_data=_cb("network_search")),
            InlineKeyboardButton("📊 Overview", callback_data=_cb("status_overview")),
        ],
        [
            InlineKeyboardButton("✅ Bulk Enable", callback_data=_cb("bulk_enable", "networks")),
            InlineKeyboardButton("🔴 Bulk Disable", callback_data=_cb("bulk_disable", "networks")),
        ],
        [
            InlineKeyboardButton("🗑️ Bulk Delete", callback_data=_cb("bulk_delete", "networks")),
            InlineKeyboardButton("🔃 Refresh", callback_data=_cb("refresh_all")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 7.  Transaction Panel
# ═══════════════════════════════════════════════════════════════════════════

def build_transaction_panel() -> Tuple[str, InlineKeyboardMarkup]:
    """Transaction management panel with live stats."""
    stats: Dict[str, int] = {}
    try:
        from database import get_db_session
        from database.models import Transaction, TransactionStatus
        from sqlalchemy import func as sqlfunc
        with get_db_session() as s:
            rows = (
                s.query(TransactionStatus, sqlfunc.count(Transaction.id))
                .join(Transaction, Transaction.status == TransactionStatus, isouter=True)
                .group_by(Transaction.status)
                .all()
            )
            for status_val, count in s.query(
                Transaction.status, sqlfunc.count(Transaction.id)
            ).group_by(Transaction.status).all():
                if status_val:
                    stats[status_val.value] = count
    except Exception:
        logger.exception("build_transaction_panel stats failed")

    total = sum(stats.values())
    pending   = stats.get("pending", 0) + stats.get("awaiting_confirmation", 0)
    completed = stats.get("completed", 0)
    failed    = stats.get("failed", 0) + stats.get("rejected", 0) + stats.get("expired", 0)

    lines = [
        "📋 <b>Transaction Management</b>",
        DIVIDER,
        f"  📊 Total: <b>{total}</b>",
        f"  🟡 Pending: <b>{pending}</b>",
        f"  🟢 Completed: <b>{completed}</b>",
        f"  🔴 Failed/Rejected: <b>{failed}</b>",
    ]

    if stats:
        lines.append("")
        lines.append("<b>Breakdown</b>")
        status_display = {
            "pending":                "🕐 Pending",
            "awaiting_confirmation":  "👀 Awaiting Confirmation",
            "completed":              "✅ Completed",
            "expired":                "⏰ Expired",
            "cancelled":              "🚫 Cancelled",
            "failed":                 "❌ Failed",
            "rejected":               "🛑 Rejected",
        }
        for key, label in status_display.items():
            count = stats.get(key, 0)
            if count > 0:
                lines.append(f"  {label}: <b>{count}</b>")

    lines.append(DIVIDER)
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("🔍 Search Transaction", callback_data=_cb("tx_search")),
            InlineKeyboardButton("📊 Status Overview", callback_data=_cb("status_overview")),
        ],
        [
            InlineKeyboardButton("✅ Bulk Enable", callback_data=_cb("bulk_enable", "transactions")),
            InlineKeyboardButton("🔴 Bulk Disable", callback_data=_cb("bulk_disable", "transactions")),
        ],
        [
            InlineKeyboardButton("🗑️ Bulk Delete", callback_data=_cb("bulk_delete", "transactions")),
            InlineKeyboardButton("🔃 Refresh", callback_data=_cb("refresh_all")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 8.  Search Menu
# ═══════════════════════════════════════════════════════════════════════════

def build_search_menu() -> Tuple[str, InlineKeyboardMarkup]:
    """Unified search hub — choose what to search."""
    text = (
        "🔍 <b>Search</b>\n"
        f"{DIVIDER}\n"
        "Search across gateways, wallets, networks, and transactions.\n\n"
        "Tap a category to start:"
    )
    keyboard = [
        [
            InlineKeyboardButton("🔍 Search Wallet", callback_data=_cb("wallet_search")),
            InlineKeyboardButton("🌐 Search Network", callback_data=_cb("network_search")),
        ],
        [
            InlineKeyboardButton("📋 Search Transaction", callback_data=_cb("tx_search")),
            InlineKeyboardButton("💳 Search Gateway", callback_data=_cb("gw_search")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 9.  Search prompt screens (text instructions for inline entry)
# ═══════════════════════════════════════════════════════════════════════════

def build_wallet_search_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    """Prompt admin to type a wallet/currency search query."""
    text = (
        "🔍 <b>Search Wallet / Currency</b>\n"
        f"{DIVIDER}\n"
        "Type a currency <b>code</b>, <b>name</b>, or <b>symbol</b> to search.\n\n"
        "<i>Examples:</i> <code>USDT</code> · <code>Bitcoin</code> · <code>BDT</code>\n\n"
        "➡️ Send your search query as a message now."
    )
    keyboard = [_nav_row(_cb("wallet_panel"))]
    return text, InlineKeyboardMarkup(keyboard)


def build_network_search_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    """Prompt admin to type a network search query."""
    text = (
        "🌐 <b>Search Network</b>\n"
        f"{DIVIDER}\n"
        "Type a <b>network name</b> to search.\n\n"
        "<i>Examples:</i> <code>TRC20</code> · <code>BEP20</code> · <code>bKash</code>\n\n"
        "➡️ Send your search query as a message now."
    )
    keyboard = [_nav_row(_cb("network_panel"))]
    return text, InlineKeyboardMarkup(keyboard)


def build_transaction_search_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    """Prompt admin to type a transaction search query."""
    text = (
        "📋 <b>Search Transaction</b>\n"
        f"{DIVIDER}\n"
        "Search by <b>TxID</b>, <b>user ID</b>, <b>amount</b>, or <b>status</b>.\n\n"
        "<i>Examples:</i> <code>TX123456</code> · <code>pending</code> · <code>50.00</code>\n\n"
        "➡️ Send your search query as a message now."
    )
    keyboard = [_nav_row(_cb("tx_panel"))]
    return text, InlineKeyboardMarkup(keyboard)


def build_gateway_search_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    """Prompt admin to type a gateway search query."""
    text = (
        "💳 <b>Search Gateway</b>\n"
        f"{DIVIDER}\n"
        "Type a <b>gateway name</b> or <b>ID</b> to search.\n\n"
        "<i>Examples:</i> <code>binance</code> · <code>bKash</code> · <code>cryptomus</code>\n\n"
        "➡️ Send your search query as a message now."
    )
    keyboard = [_nav_row(_cb("search_menu"))]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 10.  Search Results
# ═══════════════════════════════════════════════════════════════════════════

def build_wallet_search_results(query: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Show wallet/currency search results for ``query``."""
    currencies = _get_wallet_currencies()
    q = query.strip().lower()
    matches = [
        c for c in currencies
        if q in c.get("code", "").lower()
        or q in c.get("name", "").lower()
        or q in c.get("symbol", "").lower()
    ]

    if not matches:
        text = (
            f"🔍 <b>Search: {query}</b>\n"
            f"{DIVIDER}\n"
            "❌ No currencies found.\n"
            "Try a different search term."
        )
    else:
        lines = [f"🔍 <b>Search: {query}</b>", DIVIDER,
                 f"Found <b>{len(matches)}</b> result(s):", ""]
        for c in matches:
            st  = (c.get("status") or ("enabled" if c.get("is_enabled") else "disabled")).lower()
            em  = STATUS_EMOJI.get(st, "⚪")
            frozen = " ❄️" if c.get("is_frozen") else ""
            lines.append(f"{em} <b>{c['code']}</b> {c.get('symbol', '')} "
                         f"— {c['name']}{frozen}")
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔍 Search Again", callback_data=_cb("wallet_search")),
         InlineKeyboardButton("💼 Wallets", callback_data=_cb("wallet_panel"))],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_network_search_results(query: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Show network search results for ``query``."""
    descriptors = _get_gateway_descriptors()
    q = query.strip().lower()

    found_networks: Dict[str, List[str]] = {}
    for d in descriptors:
        nets: List[str] = []
        if d.network:
            nets.append(d.network)
        row = _get_gateway_db_row(d.gateway_id)
        if row and hasattr(row, "bybit_allowed_networks") and getattr(row, "bybit_allowed_networks", None):
            nets.extend(n.strip() for n in row.bybit_allowed_networks.split(",") if n.strip())
        for net in nets:
            if q in net.lower():
                found_networks.setdefault(net, []).append(d.display_name)

    if not found_networks:
        text = (f"🌐 <b>Search: {query}</b>\n{DIVIDER}\n"
                "❌ No networks found.\nTry a different search term.")
    else:
        lines = [f"🌐 <b>Search: {query}</b>", DIVIDER,
                 f"Found <b>{len(found_networks)}</b> network(s):", ""]
        for net, gw_names in sorted(found_networks.items()):
            lines.append(f"🔗 <b>{net}</b> — {', '.join(gw_names)}")
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔍 Search Again", callback_data=_cb("network_search")),
         InlineKeyboardButton("🌐 Networks", callback_data=_cb("network_panel"))],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_transaction_search_results(query: str, limit: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    """Show transaction search results for ``query`` (TxID / user ID / status / amount)."""
    results: List[Dict] = []
    try:
        from database import get_db_session
        from database.models import Transaction, TransactionStatus
        from sqlalchemy import or_, cast, String
        q = query.strip().lower()
        with get_db_session() as s:
            base = s.query(Transaction)
            rows = base.filter(
                or_(
                    Transaction.txid.ilike(f"%{q}%"),
                    cast(Transaction.id, String).ilike(f"%{q}%"),
                    cast(Transaction.user_id, String).ilike(f"%{q}%"),
                    cast(Transaction.amount, String).ilike(f"%{q}%"),
                )
            ).order_by(Transaction.created_at.desc()).limit(limit).all()
            for r in rows:
                results.append({
                    "id":      r.id,
                    "user_id": r.user_id,
                    "amount":  r.amount,
                    "status":  r.status.value if r.status else "unknown",
                    "method":  r.payment_method.value if r.payment_method else "?",
                    "created": r.created_at.strftime("%d %b %H:%M") if r.created_at else "?",
                    "txid":    r.txid or "",
                })
    except Exception:
        logger.exception("build_transaction_search_results failed for query=%s", query)

    if not results:
        text = (f"📋 <b>Search: {query}</b>\n{DIVIDER}\n"
                "❌ No transactions found.\nTry TxID, user ID, amount, or status.")
    else:
        lines = [f"📋 <b>Search: {query}</b>", DIVIDER,
                 f"Found <b>{len(results)}</b> result(s):", ""]
        for r in results:
            st_em = STATUS_EMOJI.get(r["status"], "⚪")
            txid_str = f" · <code>{r['txid'][:12]}…</code>" if r["txid"] else ""
            lines.append(
                f"{st_em} #<b>{r['id']}</b> · ${r['amount']:.2f} "
                f"· {r['method'].upper()} · {r['created']}{txid_str}"
            )
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔍 Search Again", callback_data=_cb("tx_search")),
         InlineKeyboardButton("📋 Transactions", callback_data=_cb("tx_panel"))],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_gateway_search_results(query: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Show gateway search results for ``query``."""
    descriptors = _get_gateway_descriptors()
    q = query.strip().lower()
    matches = [
        d for d in descriptors
        if q in d.gateway_id.lower() or q in d.display_name.lower()
    ]

    if not matches:
        text = (f"💳 <b>Search: {query}</b>\n{DIVIDER}\n"
                "❌ No gateways found.\nTry a different search term.")
        keyboard = [
            [InlineKeyboardButton("🔍 Search Again", callback_data=_cb("gw_search")),
             InlineKeyboardButton("💳 Gateways", callback_data=_cb("gw_list"))],
            _nav_row(_cb("main")),
        ]
    else:
        lines = [f"💳 <b>Search: {query}</b>", DIVIDER,
                 f"Found <b>{len(matches)}</b> gateway(s):", ""]
        gw_buttons: List[InlineKeyboardButton] = []
        for d in matches:
            st  = _gateway_effective_status(d)
            em  = STATUS_EMOJI.get(st, "⚪")
            lines.append(f"{em} <b>{d.display_name}</b> · {d.currency}")
            gw_buttons.append(
                InlineKeyboardButton(f"{em} {d.display_name}",
                                     callback_data=_cb("gw_detail", d.gateway_id))
            )
        text = "\n".join(lines)
        keyboard = _rows_of_2(gw_buttons)
        keyboard.append([InlineKeyboardButton("🔍 Search Again", callback_data=_cb("gw_search")),
                         InlineKeyboardButton("💳 All", callback_data=_cb("gw_list"))])
        keyboard.append(_nav_row(_cb("main")))

    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 11.  Bulk Actions
# ═══════════════════════════════════════════════════════════════════════════

_ENTITY_LABELS = {
    "gateways":     ("💳", "Payment Gateways"),
    "wallets":      ("💼", "Wallets / Currencies"),
    "networks":     ("🌐", "Networks"),
    "transactions": ("📋", "Transactions"),
}


def build_bulk_menu(entity: str = "gateways") -> Tuple[str, InlineKeyboardMarkup]:
    """Bulk-operations hub for the given entity type."""
    emoji, label = _ENTITY_LABELS.get(entity, ("📦", entity.title()))
    back_cb = {
        "gateways":     _cb("gw_list"),
        "wallets":      _cb("wallet_panel"),
        "networks":     _cb("network_panel"),
        "transactions": _cb("tx_panel"),
    }.get(entity, _cb("main"))

    text = (
        f"⚡ <b>Bulk Actions — {emoji} {label}</b>\n"
        f"{DIVIDER}\n"
        "Choose an action to apply to all selected records.\n\n"
        "⚠️ <i>All bulk operations require confirmation.</i>"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ Bulk Enable",  callback_data=_cb("bulk_enable",  entity)),
            InlineKeyboardButton("🔴 Bulk Disable", callback_data=_cb("bulk_disable", entity)),
        ],
        [
            InlineKeyboardButton("🗑️ Bulk Delete",  callback_data=_cb("bulk_delete",  entity)),
            InlineKeyboardButton("🔃 Refresh All",  callback_data=_cb("refresh_all")),
        ],
        _nav_row(back_cb),
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_bulk_confirm(action: str, entity: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Confirmation screen before executing a bulk operation."""
    emoji, label = _ENTITY_LABELS.get(entity, ("📦", entity.title()))

    action_labels = {
        "bulk_enable":  ("✅ Enable All",  "🟢"),
        "bulk_disable": ("🔴 Disable All", "🔴"),
        "bulk_delete":  ("🗑️ Delete All",  "⚠️"),
    }
    act_label, act_emoji = action_labels.get(action, (action.title(), "⚡"))

    danger_msg = ""
    if action == "bulk_delete":
        danger_msg = "\n\n⛔ <b>WARNING: This action cannot be undone!</b>"

    text = (
        f"{act_emoji} <b>Confirm: {act_label}</b>\n"
        f"{DIVIDER}\n"
        f"Entity: {emoji} <b>{label}</b>\n"
        f"Action: <b>{act_label}</b>{danger_msg}\n\n"
        "Are you sure you want to proceed?"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=_cb("bulk_confirm", action, entity)),
            InlineKeyboardButton("❌ Cancel",  callback_data=_cb("bulk", entity)),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def execute_bulk_action(action: str, entity: str) -> Tuple[bool, str]:
    """Execute a confirmed bulk action. Returns (success, message)."""
    try:
        if entity == "gateways":
            return _bulk_gateways(action)
        elif entity == "wallets":
            return _bulk_wallets(action)
        elif entity == "transactions":
            return _bulk_transactions(action)
        else:
            return False, f"Bulk action on '{entity}' is not supported."
    except Exception as exc:
        logger.exception("execute_bulk_action failed: action=%s entity=%s", action, entity)
        return False, f"Error: {exc}"


def _bulk_gateways(action: str) -> Tuple[bool, str]:
    from database import get_db_session
    from database.models import PaymentGatewayConfig
    descriptors = _get_gateway_descriptors()
    if not descriptors:
        return False, "No gateways found."

    if action == "bulk_delete":
        return False, "Gateway deletion is not supported via bulk — use individual gateway config."

    enabled = (action == "bulk_enable")
    with get_db_session() as s:
        for d in descriptors:
            row = s.query(PaymentGatewayConfig).filter_by(gateway=d.gateway_id).first()
            if row:
                row.is_enabled = enabled
            else:
                s.add(PaymentGatewayConfig(gateway=d.gateway_id, is_enabled=enabled))
        s.commit()
    verb = "enabled" if enabled else "disabled"
    return True, f"✅ {len(descriptors)} gateways {verb} successfully."


def _bulk_wallets(action: str) -> Tuple[bool, str]:
    from database import get_db_session
    from database.models import WalletCurrencyConfig

    with get_db_session() as s:
        rows = s.query(WalletCurrencyConfig).all()
        if not rows:
            return False, "No currencies found."

        if action == "bulk_delete":
            count = len(rows)
            for row in rows:
                s.delete(row)
            s.commit()
            return True, f"🗑️ {count} currencies deleted."

        enabled = (action == "bulk_enable")
        for row in rows:
            row.is_enabled = enabled
        s.commit()
        verb = "enabled" if enabled else "disabled"
        return True, f"✅ {len(rows)} currencies {verb} successfully."


def _bulk_transactions(action: str) -> Tuple[bool, str]:
    """Bulk actions on transactions — only delete terminal ones."""
    from database import get_db_session
    from database.models import Transaction, TransactionStatus

    if action == "bulk_delete":
        terminal = TransactionStatus.terminal_non_blocking()
        with get_db_session() as s:
            rows = s.query(Transaction).filter(Transaction.status.in_(terminal)).all()
            count = len(rows)
            for row in rows:
                s.delete(row)
            s.commit()
        return True, f"🗑️ {count} terminal transactions deleted."

    return False, "Only bulk delete is supported for transactions."


# ═══════════════════════════════════════════════════════════════════════════
# 12.  Configuration Panel
# ═══════════════════════════════════════════════════════════════════════════

def build_config_panel() -> Tuple[str, InlineKeyboardMarkup]:
    """Configuration management panel."""
    descriptors = _get_gateway_descriptors()

    lines = [
        "⚙️ <b>Configuration</b>",
        DIVIDER,
        f"  💳 Registered gateways: <b>{len(descriptors)}</b>",
        "",
        "<b>Actions</b>",
        "  ♻️ <b>Reload Config</b> — re-run gateway bootstrap from scratch",
        "  🔃 <b>Refresh Status</b> — re-check live enabled/mode flags from DB",
        "  📊 <b>Status Overview</b> — current gateway + wallet health summary",
    ]
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("♻️ Reload Config",  callback_data=_cb("reload_config")),
            InlineKeyboardButton("🔃 Refresh Status", callback_data=_cb("refresh_all")),
        ],
        [
            InlineKeyboardButton("📊 Status Overview", callback_data=_cb("status_overview")),
            InlineKeyboardButton("💳 Gateways",        callback_data=_cb("gw_list")),
        ],
        _nav_row(_cb("main")),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ═══════════════════════════════════════════════════════════════════════════
# 13.  Refresh / Reload handlers
# ═══════════════════════════════════════════════════════════════════════════

def execute_refresh_all() -> Tuple[bool, str]:
    """Re-check live enabled/mode flags for every gateway. Returns (ok, msg)."""
    try:
        descriptors = _get_gateway_descriptors()
        statuses: List[str] = []
        for d in descriptors:
            st = _gateway_effective_status(d)
            em = STATUS_EMOJI.get(st, "⚪")
            statuses.append(f"{em} {d.display_name}: {st.title()}")
        summary = "\n".join(statuses) if statuses else "No gateways found."
        return True, f"🔃 <b>Status Refreshed</b>\n{DIVIDER}\n{summary}"
    except Exception as exc:
        logger.exception("execute_refresh_all failed")
        return False, f"❌ Refresh failed: {exc}"


def execute_refresh_gateway(gateway_id: str) -> Tuple[bool, str]:
    """Re-check a single gateway's status."""
    try:
        from services.payment_gateway_registry import registry
        d = registry.get(gateway_id)
        if not d:
            return False, f"Gateway '{gateway_id}' not found."
        st = _gateway_effective_status(d)
        em = STATUS_EMOJI.get(st, "⚪")
        return True, f"🔃 <b>{d.display_name}</b>\n{DIVIDER}\nStatus: {em} {st.title()}"
    except Exception as exc:
        logger.exception("execute_refresh_gateway failed for %s", gateway_id)
        return False, f"❌ Refresh failed: {exc}"


def execute_reload_config() -> Tuple[bool, str]:
    """Reset the bootstrap flag and re-run gateway bootstrap."""
    try:
        import services.payment_gateway_bootstrap as _boot
        _boot._bootstrapped = False   # reset idempotency guard
        _boot.bootstrap_gateways()
        _boot._bootstrapped = True
        from services.payment_gateway_registry import registry
        count = len(registry.all())
        return True, f"♻️ <b>Configuration Reloaded</b>\n{DIVIDER}\n✅ {count} gateways re-registered."
    except Exception as exc:
        logger.exception("execute_reload_config failed")
        return False, f"❌ Reload failed: {exc}"


def execute_gateway_enable(gateway_id: str, enabled: bool) -> Tuple[bool, str]:
    """Enable or disable a single gateway in PaymentGatewayConfig."""
    try:
        from database import get_db_session
        from database.models import PaymentGatewayConfig
        from services.payment_gateway_registry import registry
        d = registry.get(gateway_id)
        name = d.display_name if d else gateway_id
        with get_db_session() as s:
            row = s.query(PaymentGatewayConfig).filter_by(gateway=gateway_id).first()
            if row:
                row.is_enabled = enabled
            else:
                s.add(PaymentGatewayConfig(gateway=gateway_id, is_enabled=enabled))
            s.commit()
        verb = "enabled" if enabled else "disabled"
        em   = "🟢" if enabled else "🔴"
        return True, f"{em} <b>{name}</b> {verb} successfully."
    except Exception as exc:
        logger.exception("execute_gateway_enable failed for %s", gateway_id)
        return False, f"❌ Failed: {exc}"


def execute_gateway_maintenance(gateway_id: str) -> Tuple[bool, str]:
    """Toggle bKash/Nagad-style manual mode for a gateway."""
    try:
        from services.gateway_manual_mode import toggle_mode
        new_mode = toggle_mode(gateway_id)
        em    = "🟡" if new_mode == "manual" else "🟢"
        label = "Manual / Maintenance" if new_mode == "manual" else "Auto / Enabled"
        return True, f"{em} Gateway <code>{gateway_id}</code> → <b>{label}</b>"
    except Exception as exc:
        logger.exception("execute_gateway_maintenance failed for %s", gateway_id)
        return False, f"❌ Failed: {exc}"


def execute_reload_gateway(gateway_id: str) -> Tuple[bool, str]:
    """Reload a single gateway config by re-bootstrapping just that entry."""
    try:
        # Re-running bootstrap is idempotent (last-write-wins)
        execute_reload_config()
        return execute_refresh_gateway(gateway_id)
    except Exception as exc:
        return False, f"❌ Reload failed: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
# 14.  Callback data parser
# ═══════════════════════════════════════════════════════════════════════════

def parse_callback(data: str) -> Optional[Dict[str, Any]]:
    """
    Parse any ``pgadmin_*`` callback-data string into a structured dict.

    Returns None if the data doesn't start with the ``pgadmin_`` prefix.

    Result fields:
      "action"  — the primary action key (e.g. "gw_detail")
      "args"    — list of positional args after the action (may be empty)
    """
    if not data.startswith(CB + "_"):
        return None
    remainder = data[len(CB) + 1:]
    parts = remainder.split(":")
    return {"action": parts[0], "args": parts[1:]}


# ═══════════════════════════════════════════════════════════════════════════
# 15.  Master dispatcher — wire every callback to the right builder
# ═══════════════════════════════════════════════════════════════════════════

async def dispatch(
    callback_data: str,
    query_text: Optional[str] = None,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """
    Dispatch a ``pgadmin_*`` callback and return (text, keyboard) or None.

    ``query_text`` is the user's free-text input, used only for search
    result actions (``wallet_search``, ``network_search``, ``tx_search``,
    ``gw_search`` when they carry a query).

    Usage in a handler:

        parsed = parse_callback(data)
        if parsed:
            result = await dispatch(data, query_text=user_message)
            if result:
                text, keyboard = result
                await query.edit_message_text(text, reply_markup=keyboard,
                                              parse_mode="HTML")
    """
    parsed = parse_callback(callback_data)
    if not parsed:
        return None

    action = parsed["action"]
    args   = parsed["args"]

    # ── Navigation / menu ──────────────────────────────────────────────
    if action == "main":
        return build_main_menu()

    if action == "gw_list":
        return build_gateway_list()

    if action == "gw_detail" and args:
        return build_gateway_detail(args[0])

    if action == "wallet_panel":
        return build_wallet_panel()

    if action == "network_panel":
        return build_network_panel()

    if action == "tx_panel":
        return build_transaction_panel()

    if action == "search_menu":
        return build_search_menu()

    if action == "status_overview":
        return build_status_overview()

    if action == "config_panel":
        return build_config_panel()

    # ── Search prompts ─────────────────────────────────────────────────
    if action == "wallet_search":
        if query_text:
            return build_wallet_search_results(query_text)
        return build_wallet_search_prompt()

    if action == "network_search":
        if query_text:
            return build_network_search_results(query_text)
        return build_network_search_prompt()

    if action == "tx_search":
        if query_text:
            return build_transaction_search_results(query_text)
        return build_transaction_search_prompt()

    if action == "gw_search":
        if query_text:
            return build_gateway_search_results(query_text)
        return build_gateway_search_prompt()

    # ── Bulk menus ─────────────────────────────────────────────────────
    if action == "bulk" and args:
        return build_bulk_menu(args[0])

    if action in ("bulk_enable", "bulk_disable", "bulk_delete") and args:
        return build_bulk_confirm(action, args[0])

    if action == "bulk_confirm" and len(args) >= 2:
        bulk_action, entity = args[0], args[1]
        ok, msg = execute_bulk_action(bulk_action, entity)
        back_cb = {
            "gateways":     _cb("gw_list"),
            "wallets":      _cb("wallet_panel"),
            "networks":     _cb("network_panel"),
            "transactions": _cb("tx_panel"),
        }.get(entity, _cb("main"))
        icon = "✅" if ok else "❌"
        text = f"{icon} <b>Bulk Action Result</b>\n{DIVIDER}\n{msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=back_cb),
             InlineKeyboardButton("🏠 Menu", callback_data=_cb("main"))],
        ])
        return text, keyboard

    # ── Gateway actions ────────────────────────────────────────────────
    if action == "gw_enable" and args:
        ok, msg = execute_gateway_enable(args[0], True)
        text = f"{'✅' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Gateway", callback_data=_cb("gw_detail", args[0])),
             InlineKeyboardButton("🏠 Menu",    callback_data=_cb("main"))],
        ])
        return text, keyboard

    if action == "gw_disable" and args:
        ok, msg = execute_gateway_enable(args[0], False)
        text = f"{'✅' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Gateway", callback_data=_cb("gw_detail", args[0])),
             InlineKeyboardButton("🏠 Menu",    callback_data=_cb("main"))],
        ])
        return text, keyboard

    if action == "gw_maintenance" and args:
        ok, msg = execute_gateway_maintenance(args[0])
        text = f"{'✅' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Gateway", callback_data=_cb("gw_detail", args[0])),
             InlineKeyboardButton("🏠 Menu",    callback_data=_cb("main"))],
        ])
        return text, keyboard

    if action == "gw_refresh" and args:
        ok, msg = execute_refresh_gateway(args[0])
        text = f"{'🔃' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Gateway", callback_data=_cb("gw_detail", args[0])),
             InlineKeyboardButton("💳 All",     callback_data=_cb("gw_list"))],
        ])
        return text, keyboard

    if action == "gw_reload" and args:
        ok, msg = execute_reload_gateway(args[0])
        text = f"{'♻️' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Gateway", callback_data=_cb("gw_detail", args[0])),
             InlineKeyboardButton("🏠 Menu",    callback_data=_cb("main"))],
        ])
        return text, keyboard

    # ── Global actions ─────────────────────────────────────────────────
    if action == "refresh_all":
        ok, msg = execute_refresh_all()
        text = msg
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Gateways", callback_data=_cb("gw_list")),
             InlineKeyboardButton("🏠 Menu",     callback_data=_cb("main"))],
        ])
        return text, keyboard

    if action == "reload_config":
        ok, msg = execute_reload_config()
        text = f"{'♻️' if ok else '❌'} {msg}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Gateways", callback_data=_cb("gw_list")),
             InlineKeyboardButton("🏠 Menu",     callback_data=_cb("main"))],
        ])
        return text, keyboard

    logger.debug("Unhandled pgadmin callback: %s", callback_data)
    return None
