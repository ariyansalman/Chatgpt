"""Enhanced Crypto Network Manager for every payment gateway.

Callback namespace: ``gcn:``

Networks are stored in ``bot_config`` as JSON under the key
``{gateway_key}_networks`` — zero database schema changes required.
Preserves ALL existing payment processing and API logic untouched.

Each network entry (stored in the JSON list):
    {
        "id":          int,    # auto-incrementing per gateway
        "name":        str,    # e.g. "USDT TRC20", "ERC20"
        "address":     str,    # deposit address
        "is_active":   bool,   # 🟢 Enabled / 🔴 Disabled
        "is_default":  bool,   # only one per gateway
        "min_deposit": float,  # 0 = no minimum
        "max_deposit": float,  # 0 = no maximum
        "priority":    int,    # sort order — lower = higher priority
        "auto_verify": bool,   # auto-verification on/off
    }

Callback patterns (all ≤ 64 bytes):
    gcn:list:{gw}         — list all networks
    gcn:add:{gw}          — start add-network conversation
    gcn:view:{gw}:{id}    — detail view
    gcn:tog:{gw}:{id}     — toggle enable/disable
    gcn:def:{gw}:{id}     — set as default
    gcn:copy:{gw}:{id}    — copy address
    gcn:qr:{gw}:{id}      — show QR code photo
    gcn:av:{gw}:{id}      — toggle auto_verify
    gcn:pup:{gw}:{id}     — priority up  (-1)
    gcn:pdn:{gw}:{id}     — priority down (+1)
    gcn:del:{gw}:{id}     — confirm delete
    gcn:delok:{gw}:{id}   — execute delete
    gcn:enm:{gw}:{id}     — edit name (start conv)
    gcn:eaddr:{gw}:{id}   — edit address (start conv)
    gcn:emin:{gw}:{id}    — edit min deposit (start conv)
    gcn:emax:{gw}:{id}    — edit max deposit (start conv)
    gcn:epri:{gw}:{id}    — edit priority number (start conv)
"""
from __future__ import annotations

import io
import json
import logging
from typing import Optional

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────
GCN_ADD_NAME  = 9300
GCN_ADD_ADDR  = 9301
GCN_EDIT_VAL  = 9302

# ── Gateway display labels ─────────────────────────────────────────────────
_GW_LABELS: dict[str, str] = {
    "bkash":       "📱 bKash",
    "nagad":       "🟠 Nagad",
    "stars":       "⭐ Telegram Stars",
    "cryptomus":   "💠 Cryptomus",
    "heleket":     "🟣 Heleket",
    "nowpayments": "🌐 NOWPayments",
    "zinipay":     "🇧🇩 ZiniPay",
    "binance_pay": "🟡 Binance Pay",
    "bybit_pay":   "💙 Bybit Pay",
    "manual":      "🏦 Manual",
}

_BACK_CB: dict[str, str] = {
    "bkash":       "admin_gw_view_bkash",
    "nagad":       "admin_gw_view_nagad",
    "stars":       "admin_stars_view",
    "cryptomus":   "admin_cryptomus_view",
    "heleket":     "admin_heleket_view",
    "nowpayments": "admin_nowpayments_view",
    "zinipay":     "admin_zinipay_view",
    "binance_pay": "admin_binance_view",
    "bybit_pay":   "admin_bybit_view",
}


# ── Storage helpers ────────────────────────────────────────────────────────

def _cfg_key(gw: str) -> str:
    return f"{gw}_networks"


def _load(gw: str) -> list[dict]:
    raw = cfg.get_str(_cfg_key(gw), "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(gw: str, networks: list[dict]) -> None:
    cfg.set(_cfg_key(gw), json.dumps(networks))


def _next_id(networks: list[dict]) -> int:
    if not networks:
        return 1
    return max((n.get("id") or 0) for n in networks) + 1


def _find(networks: list[dict], nid: int) -> Optional[dict]:
    for n in networks:
        if n.get("id") == nid:
            return n
    return None


def _gw_label(gw: str) -> str:
    return _GW_LABELS.get(gw, gw.replace("_", " ").title())


def _back_cb(gw: str) -> str:
    return _BACK_CB.get(gw, "admin_gateways")


def _sorted(networks: list[dict]) -> list[dict]:
    """Return networks sorted by priority asc, then id asc."""
    return sorted(networks, key=lambda n: (n.get("priority", 0), n.get("id", 0)))


# ── QR code generator ──────────────────────────────────────────────────────

def _make_qr_bytes(data: str) -> Optional[bytes]:
    """Generate QR code PNG bytes, or None if qrcode is unavailable."""
    try:
        import qrcode  # type: ignore
        from PIL import Image  # type: ignore  # noqa: F401

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=3,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        logger.warning("QR generation failed: %s", exc)
        return None


# ── Text helpers ───────────────────────────────────────────────────────────

def _status_icon(n: dict) -> str:
    return "🟢" if n.get("is_active", True) else "🔴"


def _format_amount(val: float) -> str:
    if val <= 0:
        return "No limit"
    if val == int(val):
        return f"${int(val):,}"
    return f"${val:,.2f}"


def _detail_text(gw: str, n: dict) -> str:
    status    = "🟢 Enabled" if n.get("is_active", True) else "🔴 Disabled"
    dflt      = "Yes ★" if n.get("is_default") else "No"
    address   = n.get("address") or "(not set)"
    min_d     = _format_amount(n.get("min_deposit", 0.0))
    max_d     = _format_amount(n.get("max_deposit", 0.0))
    auto_v    = "✅ On" if n.get("auto_verify", False) else "❌ Off"
    priority  = n.get("priority", 0)
    return (
        f"{_gw_label(gw)} — <b>🌐 Network Detail</b>\n\n"
        f"<b>Network:</b>       {n.get('name', '—')}\n"
        f"<b>Address:</b>       <code>{address}</code>\n"
        f"<b>Status:</b>        {status}\n"
        f"<b>Default:</b>       {dflt}\n"
        f"<b>Min Deposit:</b>   {min_d}\n"
        f"<b>Max Deposit:</b>   {max_d}\n"
        f"<b>Priority:</b>      {priority}\n"
        f"<b>Auto Verify:</b>   {auto_v}"
    )


def _list_text(gw: str, networks: list[dict]) -> str:
    count  = len(networks)
    active = sum(1 for n in networks if n.get("is_active", True))
    header = (
        f"{_gw_label(gw)} — <b>🌐 Network Manager</b>\n\n"
        f"Total: <b>{count}</b>  |  "
        f"🟢 <b>{active}</b> Enabled  🔴 <b>{count - active}</b> Disabled\n\n"
    )
    if not networks:
        return header + "No networks configured yet.\nTap ➕ to add the first network."
    return header + "Tap a network to manage it, or ➕ to add a new one."


# ── Keyboards ──────────────────────────────────────────────────────────────

def _list_keyboard(gw: str, networks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for n in _sorted(networks):
        icon  = _status_icon(n)
        dflt  = " ★" if n.get("is_default") else ""
        prio  = n.get("priority", 0)
        label = f"{icon} {n.get('name', 'Network')}{dflt}  [P:{prio}]"
        rows.append([InlineKeyboardButton(label, callback_data=f"gcn:view:{gw}:{n['id']}")])
    rows.append([InlineKeyboardButton("➕ Add Network", callback_data=f"gcn:add:{gw}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=_back_cb(gw))])
    return InlineKeyboardMarkup(rows)


def _detail_keyboard(gw: str, n: dict) -> InlineKeyboardMarkup:
    nid         = n["id"]
    toggle_lbl  = "🔴 Disable" if n.get("is_active", True) else "🟢 Enable"
    dflt_lbl    = "★ Default ✓" if n.get("is_default") else "☆ Set Default"
    av_lbl      = "🔔 Auto Verify: ON" if n.get("auto_verify", False) else "🔕 Auto Verify: OFF"
    address     = n.get("address", "")
    copy_btn    = (
        InlineKeyboardButton("📋 Copy Address", copy_text=CopyTextButton(text=address))
        if address else
        InlineKeyboardButton("📋 Copy Address", callback_data=f"gcn:copy:{gw}:{nid}")
    )
    return InlineKeyboardMarkup([
        # Row 1 — rename & edit address
        [
            InlineKeyboardButton("✏️ Rename",       callback_data=f"gcn:enm:{gw}:{nid}"),
            InlineKeyboardButton("📝 Edit Address",  callback_data=f"gcn:eaddr:{gw}:{nid}"),
        ],
        # Row 2 — copy & QR
        [
            copy_btn,
            InlineKeyboardButton("📷 QR Code",       callback_data=f"gcn:qr:{gw}:{nid}"),
        ],
        # Row 3 — default & toggle
        [
            InlineKeyboardButton(dflt_lbl,           callback_data=f"gcn:def:{gw}:{nid}"),
            InlineKeyboardButton(toggle_lbl,         callback_data=f"gcn:tog:{gw}:{nid}"),
        ],
        # Row 4 — min / max deposit
        [
            InlineKeyboardButton("💵 Min Deposit",   callback_data=f"gcn:emin:{gw}:{nid}"),
            InlineKeyboardButton("💰 Max Deposit",   callback_data=f"gcn:emax:{gw}:{nid}"),
        ],
        # Row 5 — priority controls
        [
            InlineKeyboardButton("⬆️ Priority",      callback_data=f"gcn:pup:{gw}:{nid}"),
            InlineKeyboardButton("✏️ Set Priority",  callback_data=f"gcn:epri:{gw}:{nid}"),
            InlineKeyboardButton("⬇️ Priority",      callback_data=f"gcn:pdn:{gw}:{nid}"),
        ],
        # Row 6 — auto verify
        [InlineKeyboardButton(av_lbl, callback_data=f"gcn:av:{gw}:{nid}")],
        # Row 7 — delete & back
        [
            InlineKeyboardButton("🗑 Delete Network", callback_data=f"gcn:del:{gw}:{nid}"),
        ],
        [InlineKeyboardButton("🔙 Back",             callback_data=f"gcn:list:{gw}")],
    ])


# ── List view ──────────────────────────────────────────────────────────────

async def gcn_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all networks for a gateway  (gcn:list:{gw})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    gw       = query.data.split(":", 2)[2]
    networks = _load(gw)
    try:
        await query.edit_message_text(
            _list_text(gw, networks),
            reply_markup=_list_keyboard(gw, networks),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Detail view ────────────────────────────────────────────────────────────

async def gcn_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show one network's detail + action buttons  (gcn:view:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Toggle Enable / Disable ────────────────────────────────────────────────

async def gcn_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable / Disable a network  (gcn:tog:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    n["is_active"] = not n.get("is_active", True)
    _save(gw, networks)
    new_status = "🟢 Enabled" if n["is_active"] else "🔴 Disabled"
    await query.answer(f"Network is now {new_status}.", show_alert=False)

    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Set Default ────────────────────────────────────────────────────────────

async def gcn_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a network as the default for its gateway  (gcn:def:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    if n.get("is_default"):
        await query.answer("Already the default.", show_alert=False)
        return

    for x in networks:
        x["is_default"] = x.get("id") == nid
    _save(gw, networks)

    await query.answer(f"✅ {n.get('name', 'Network')} set as default.", show_alert=False)
    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Copy address fallback ──────────────────────────────────────────────────

async def gcn_copy_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for clients that don't support CopyTextButton  (gcn:copy:{gw}:{id})."""
    query = update.callback_query
    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    address = (n or {}).get("address", "")
    if address:
        await query.answer(f"Address: {address}", show_alert=True)
    else:
        await query.answer("No address configured.", show_alert=True)


# ── QR Code ────────────────────────────────────────────────────────────────

async def gcn_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the deposit address as a QR code photo  (gcn:qr:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    address = n.get("address", "")
    if not address:
        await query.answer("⚠️ No address set for this network.", show_alert=True)
        return

    qr_bytes = _make_qr_bytes(address)
    if not qr_bytes:
        await query.answer("⚠️ QR generation failed — qrcode library not available.", show_alert=True)
        return

    caption = (
        f"📷 <b>QR Code — {n.get('name', 'Network')}</b>\n"
        f"{_gw_label(gw)}\n\n"
        f"<code>{address}</code>"
    )
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Network", callback_data=f"gcn:view:{gw}:{nid}")
    ]])
    await query.message.reply_photo(
        photo=InputFile(io.BytesIO(qr_bytes), filename="qr.png"),
        caption=caption,
        reply_markup=back_kb,
        parse_mode="HTML",
    )


# ── Toggle Auto Verify ─────────────────────────────────────────────────────

async def gcn_auto_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-verification on/off  (gcn:av:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    n["auto_verify"] = not n.get("auto_verify", False)
    _save(gw, networks)
    state = "ON ✅" if n["auto_verify"] else "OFF ❌"
    await query.answer(f"Auto Verification is now {state}.", show_alert=False)

    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Priority controls ──────────────────────────────────────────────────────

async def gcn_priority_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Decrease priority value by 1 (move higher in list)  (gcn:pup:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    n["priority"] = max(0, int(n.get("priority", 0)) - 1)
    _save(gw, networks)
    await query.answer(f"Priority → {n['priority']}", show_alert=False)

    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


async def gcn_priority_down(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Increase priority value by 1 (move lower in list)  (gcn:pdn:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    n["priority"] = int(n.get("priority", 0)) + 1
    _save(gw, networks)
    await query.answer(f"Priority → {n['priority']}", show_alert=False)

    try:
        await query.edit_message_text(
            _detail_text(gw, n),
            reply_markup=_detail_keyboard(gw, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Delete with confirmation ───────────────────────────────────────────────

async def gcn_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for deletion confirmation  (gcn:del:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            f"🗑 <b>Delete Network</b>\n\n"
            f"Are you sure you want to delete:\n"
            f"<b>{n.get('name', 'this network')}</b>?\n\n"
            f"This action cannot be undone.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete", callback_data=f"gcn:delok:{gw}:{nid}"),
                    InlineKeyboardButton("❌ Cancel",      callback_data=f"gcn:view:{gw}:{nid}"),
                ]
            ]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gcn_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute deletion  (gcn:delok:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    before = len(networks)
    networks = [x for x in networks if x.get("id") != nid]

    if len(networks) < before:
        # Re-assign default if we deleted the default
        if not any(x.get("is_default") for x in networks) and networks:
            # Prefer the first enabled; fall back to first available
            active = [x for x in networks if x.get("is_active", True)]
            (active or networks)[0]["is_default"] = True
        _save(gw, networks)
        await query.answer("🗑 Network deleted.", show_alert=False)
    else:
        await query.answer("Already removed.", show_alert=False)

    try:
        await query.edit_message_text(
            _list_text(gw, networks),
            reply_markup=_list_keyboard(gw, networks),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ──────────────────────────────────────────────────────────────────────────
# Conversation: Add Network  (name → address)
# ──────────────────────────────────────────────────────────────────────────

async def gcn_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: ask for network name  (gcn:add:{gw})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    gw = query.data.split(":", 2)[2]
    context.user_data["gcn_add"] = {"gw": gw}
    try:
        await query.edit_message_text(
            f"{_gw_label(gw)} — <b>➕ Add Network</b>\n\n"
            "Step 1 of 2\n\n"
            "Send the <b>network name</b>:\n"
            "Examples: <code>USDT TRC20</code>, <code>ERC20</code>, <code>BEP20</code>, <code>TON</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"gcn:list:{gw}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GCN_ADD_NAME


async def gcn_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive network name, ask for address."""
    name = (update.message.text or "").strip()
    add_data = context.user_data.get("gcn_add", {})
    gw = add_data.get("gw", "")

    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Send the network name:")
        return GCN_ADD_NAME

    add_data["name"] = name[:80]
    context.user_data["gcn_add"] = add_data
    await update.message.reply_text(
        f"{_gw_label(gw)} — <b>➕ Add Network</b>\n\n"
        f"Step 2 of 2\n\n"
        f"Network: <b>{name}</b>\n\n"
        "Send the <b>deposit address</b> for this network\n"
        "(or send <code>skip</code> to leave it blank for now):",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"gcn:list:{gw}")
        ]]),
        parse_mode="HTML",
    )
    return GCN_ADD_ADDR


async def gcn_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive address, save the new network."""
    address_raw = (update.message.text or "").strip()
    add_data    = context.user_data.pop("gcn_add", {})
    gw          = add_data.get("gw", "")
    name        = add_data.get("name", "Network")
    address     = "" if address_raw.lower() == "skip" else address_raw[:512]

    networks = _load(gw)
    is_first = len(networks) == 0
    new_net: dict = {
        "id":          _next_id(networks),
        "name":        name,
        "address":     address,
        "is_active":   True,
        "is_default":  is_first,
        "min_deposit": 0.0,
        "max_deposit": 0.0,
        "priority":    len(networks),
        "auto_verify": False,
    }
    networks.append(new_net)
    _save(gw, networks)

    await update.message.reply_text(
        f"✅ <b>Network added!</b>\n\n" + _detail_text(gw, new_net),
        reply_markup=_detail_keyboard(gw, new_net),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def gcn_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the add-network conversation."""
    context.user_data.pop("gcn_add", None)
    if update.callback_query:
        await update.callback_query.answer()
        gw = update.callback_query.data.split(":", 2)[2]
        networks = _load(gw)
        try:
            await update.callback_query.edit_message_text(
                _list_text(gw, networks),
                reply_markup=_list_keyboard(gw, networks),
                parse_mode="HTML",
            )
        except BadRequest:
            pass
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────
# Conversation: Edit field  (name / address / min / max / priority)
# ──────────────────────────────────────────────────────────────────────────

_EDIT_FIELD_LABELS = {
    "name":        ("network name",    "e.g. <code>USDT TRC20</code>"),
    "address":     ("deposit address", "full wallet address, or <code>clear</code> to remove"),
    "min_deposit": ("minimum deposit", "enter amount in USD, e.g. <code>10</code> — or <code>0</code> for no minimum"),
    "max_deposit": ("maximum deposit", "enter amount in USD, e.g. <code>1000</code> — or <code>0</code> for no maximum"),
    "priority":    ("priority number", "lower numbers appear first — e.g. <code>0</code>, <code>1</code>, <code>2</code>"),
}

_EDIT_STARTS = {
    "gcn:enm":   "name",
    "gcn:eaddr": "address",
    "gcn:emin":  "min_deposit",
    "gcn:emax":  "max_deposit",
    "gcn:epri":  "priority",
}


async def _gcn_edit_start_generic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, field: str
) -> int:
    """Generic edit-start: save context and ask for new value."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    parts = query.data.split(":")
    gw, nid = parts[2], int(parts[3])
    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await query.answer("⚠️ Network not found.", show_alert=True)
        return ConversationHandler.END

    label, hint = _EDIT_FIELD_LABELS[field]
    context.user_data["gcn_edit"] = {"gw": gw, "id": nid, "field": field}

    current = n.get(field, "")
    current_str = str(current) if current else "(not set)"

    try:
        await query.edit_message_text(
            f"{_gw_label(gw)} — <b>✏️ Edit {label.title()}</b>\n\n"
            f"Network: <b>{n.get('name', '—')}</b>\n"
            f"Current: <code>{current_str}</code>\n\n"
            f"Send the new <b>{label}</b>\n{hint}\n\n"
            "/cancel to abort",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"gcn:view:{gw}:{nid}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GCN_EDIT_VAL


async def gcn_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _gcn_edit_start_generic(update, context, "name")

async def gcn_edit_addr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _gcn_edit_start_generic(update, context, "address")

async def gcn_edit_min_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _gcn_edit_start_generic(update, context, "min_deposit")

async def gcn_edit_max_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _gcn_edit_start_generic(update, context, "max_deposit")

async def gcn_edit_pri_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _gcn_edit_start_generic(update, context, "priority")


async def gcn_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the new value, validate, save, return to detail view."""
    edit = context.user_data.pop("gcn_edit", None)
    if not edit:
        return ConversationHandler.END

    raw   = (update.message.text or "").strip()
    gw    = edit["gw"]
    nid   = edit["id"]
    field = edit["field"]
    label, _ = _EDIT_FIELD_LABELS[field]

    if not raw:
        await update.message.reply_text(f"❌ Cannot be empty. Send the new {label}:")
        context.user_data["gcn_edit"] = edit
        return GCN_EDIT_VAL

    networks = _load(gw)
    n = _find(networks, nid)
    if not n:
        await update.message.reply_text("❌ Network no longer exists.")
        return ConversationHandler.END

    # Validate + coerce based on field type
    if field == "name":
        if not raw:
            await update.message.reply_text("❌ Name cannot be empty. Try again:")
            context.user_data["gcn_edit"] = edit
            return GCN_EDIT_VAL
        n["name"] = raw[:80]

    elif field == "address":
        n["address"] = "" if raw.lower() == "clear" else raw[:512]

    elif field in ("min_deposit", "max_deposit"):
        try:
            val = float(raw.replace(",", "").replace("$", "").strip())
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid amount. Enter a positive number (e.g. <code>10</code>) or <code>0</code> for no limit:",
                parse_mode="HTML",
            )
            context.user_data["gcn_edit"] = edit
            return GCN_EDIT_VAL
        n[field] = round(val, 8)

    elif field == "priority":
        try:
            val = int(raw.strip())
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a non-negative integer (e.g. <code>0</code>, <code>1</code>):", parse_mode="HTML")
            context.user_data["gcn_edit"] = edit
            return GCN_EDIT_VAL
        n["priority"] = val

    _save(gw, networks)
    await update.message.reply_text(
        f"✅ Updated!\n\n" + _detail_text(gw, n),
        reply_markup=_detail_keyboard(gw, n),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def gcn_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel edit conversation and return to network detail."""
    edit = context.user_data.pop("gcn_edit", None)
    if update.callback_query:
        await update.callback_query.answer()
        if edit:
            gw  = edit["gw"]
            nid = edit["id"]
            networks = _load(gw)
            n = _find(networks, nid)
            if n:
                try:
                    await update.callback_query.edit_message_text(
                        _detail_text(gw, n),
                        reply_markup=_detail_keyboard(gw, n),
                        parse_mode="HTML",
                    )
                except BadRequest:
                    pass
    return ConversationHandler.END


async def gcn_edit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel during edit conversation."""
    context.user_data.pop("gcn_edit", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── ConversationHandler factories ──────────────────────────────────────────

def build_gcn_add_conv() -> ConversationHandler:
    """ConversationHandler for adding a new network to any gateway."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gcn_add_start, pattern=r"^gcn:add:[a-z_]+$"),
        ],
        states={
            GCN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, gcn_add_name)],
            GCN_ADD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, gcn_add_address)],
        },
        fallbacks=[
            CallbackQueryHandler(gcn_add_cancel, pattern=r"^gcn:list:[a-z_]+$"),
            CommandHandler("cancel", gcn_add_cancel),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


def build_gcn_edit_conv() -> ConversationHandler:
    """ConversationHandler for editing network fields (name/address/min/max/priority)."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gcn_edit_name_start, pattern=r"^gcn:enm:[a-z_]+:\d+$"),
            CallbackQueryHandler(gcn_edit_addr_start, pattern=r"^gcn:eaddr:[a-z_]+:\d+$"),
            CallbackQueryHandler(gcn_edit_min_start,  pattern=r"^gcn:emin:[a-z_]+:\d+$"),
            CallbackQueryHandler(gcn_edit_max_start,  pattern=r"^gcn:emax:[a-z_]+:\d+$"),
            CallbackQueryHandler(gcn_edit_pri_start,  pattern=r"^gcn:epri:[a-z_]+:\d+$"),
        ],
        states={
            GCN_EDIT_VAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gcn_edit_value),
                CallbackQueryHandler(gcn_edit_cancel, pattern=r"^gcn:view:[a-z_]+:\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", gcn_edit_cancel_cmd),
            CallbackQueryHandler(gcn_edit_cancel, pattern=r"^gcn:view:[a-z_]+:\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


# ── Registration helper ────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all crypto-network manager handlers with the Application."""
    # Conversation handlers first (higher priority)
    app.add_handler(build_gcn_add_conv())
    app.add_handler(build_gcn_edit_conv())

    # Simple callback handlers
    app.add_handler(CallbackQueryHandler(gcn_list,          pattern=r"^gcn:list:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(gcn_view,          pattern=r"^gcn:view:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_toggle,        pattern=r"^gcn:tog:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_set_default,   pattern=r"^gcn:def:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_copy_fallback, pattern=r"^gcn:copy:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_qr,            pattern=r"^gcn:qr:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_auto_verify,   pattern=r"^gcn:av:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_priority_up,   pattern=r"^gcn:pup:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_priority_down, pattern=r"^gcn:pdn:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_delete_confirm, pattern=r"^gcn:del:[a-z_]+:\d+$"))
    app.add_handler(CallbackQueryHandler(gcn_delete_execute, pattern=r"^gcn:delok:[a-z_]+:\d+$"))
