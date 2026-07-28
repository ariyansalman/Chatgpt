"""Generic Wallet Management for every payment gateway.

Callback namespace: ``gww:``

Wallets are stored in ``bot_config`` as JSON under the key
``{gateway_key}_wallets`` — zero database schema changes required.
Preserves all existing payment, deposit, verification, and commission logic.

Each wallet entry (stored in the JSON list):
    {
        "id":        int,   # auto-incrementing integer ID per gateway
        "label":     str,   # human-readable label  e.g. "USDT TRC20"
        "address":   str,   # wallet address / account number
        "is_active": bool,  # 🟢 Active / 🔴 Disabled
        "is_default":bool,  # only one wallet per gateway can be default
    }

Callback data patterns (all well under 64-byte limit):
    gww:list:{gw}         — list all wallets for a gateway
    gww:add:{gw}          — start add-wallet conversation
    gww:view:{gw}:{id}    — detail view for one wallet
    gww:edlbl:{gw}:{id}   — start edit-label conversation
    gww:edaddr:{gw}:{id}  — start edit-address conversation
    gww:del:{gw}:{id}     — confirm-delete screen
    gww:delok:{gw}:{id}   — execute deletion after confirmation
    gww:copy:{gw}:{id}    — copy address via CopyTextButton
    gww:def:{gw}:{id}     — set as default wallet
    gww:tog:{gw}:{id}     — toggle active / disabled
"""
from __future__ import annotations

import json
import logging

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
GWW_ADD_LABEL   = 9200
GWW_ADD_ADDRESS = 9201
GWW_EDIT_VALUE  = 9202

# ── Gateway display info ───────────────────────────────────────────────────
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
}

# Maps gateway_key → the callback_data that opens its main admin view
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

def _load(gw: str) -> list[dict]:
    raw = cfg.get_str(f"{gw}_wallets", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(gw: str, wallets: list[dict]) -> None:
    cfg.set(f"{gw}_wallets", json.dumps(wallets))


def _next_id(wallets: list[dict]) -> int:
    if not wallets:
        return 1
    return max((w.get("id") or 0) for w in wallets) + 1


def _find(wallets: list[dict], wallet_id: int) -> dict | None:
    for w in wallets:
        if w.get("id") == wallet_id:
            return w
    return None


def _gw_label(gw: str) -> str:
    return _GW_LABELS.get(gw, gw.replace("_", " ").title())


def _back_cb(gw: str) -> str:
    return _BACK_CB.get(gw, "admin_gateways")


# ── Keyboards ──────────────────────────────────────────────────────────────

def _list_keyboard(gw: str, wallets: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for w in wallets:
        status = "🟢" if w.get("is_active", True) else "🔴"
        dflt   = " ★" if w.get("is_default") else ""
        addr_preview = (w.get("address") or "")[:20]
        if len(w.get("address", "")) > 20:
            addr_preview += "…"
        label_text = f"{status} {w.get('label','Wallet')}{dflt}  {addr_preview}"
        rows.append([InlineKeyboardButton(label_text, callback_data=f"gww:view:{gw}:{w['id']}")])
    rows.append([InlineKeyboardButton("➕ Add Wallet", callback_data=f"gww:add:{gw}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=_back_cb(gw))])
    return InlineKeyboardMarkup(rows)


def _detail_keyboard(gw: str, w: dict) -> InlineKeyboardMarkup:
    wid = w["id"]
    toggle_label = "🔴 Disable" if w.get("is_active", True) else "🟢 Enable"
    default_label = "★ Default ✓" if w.get("is_default") else "☆ Set as Default"
    address = w.get("address", "")
    copy_btn = (
        InlineKeyboardButton("📋 Copy Address", copy_text=CopyTextButton(text=address))
        if address else
        InlineKeyboardButton("📋 Copy Address", callback_data=f"gww:copy:{gw}:{wid}")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Label",   callback_data=f"gww:edlbl:{gw}:{wid}"),
         InlineKeyboardButton("📝 Edit Address", callback_data=f"gww:edaddr:{gw}:{wid}")],
        [copy_btn],
        [InlineKeyboardButton(default_label, callback_data=f"gww:def:{gw}:{wid}")],
        [InlineKeyboardButton(toggle_label,  callback_data=f"gww:tog:{gw}:{wid}")],
        [InlineKeyboardButton("🌐 Manage Networks", callback_data=f"gcn:list:{gw}")],
        [InlineKeyboardButton("🗑 Delete",    callback_data=f"gww:del:{gw}:{wid}")],
        [InlineKeyboardButton("🔙 Back",     callback_data=f"gww:list:{gw}")],
    ])


def _detail_text(gw: str, w: dict) -> str:
    status  = "🟢 Active" if w.get("is_active", True) else "🔴 Disabled"
    dflt    = "Yes ★" if w.get("is_default") else "No"
    address = w.get("address") or "(not set)"
    return (
        f"{_gw_label(gw)} — <b>Wallet Detail</b>\n\n"
        f"<b>Label:</b>   {w.get('label','—')}\n"
        f"<b>Address:</b> <code>{address}</code>\n"
        f"<b>Status:</b>  {status}\n"
        f"<b>Default:</b> {dflt}"
    )


# ── List view ──────────────────────────────────────────────────────────────

async def gww_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all wallets configured for a gateway (gww:list:{gw})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    gw = query.data.split(":", 2)[2]
    wallets = _load(gw)
    count = len(wallets)
    active = sum(1 for w in wallets if w.get("is_active", True))
    text = (
        f"{_gw_label(gw)} — <b>💳 Wallet Manager</b>\n\n"
        f"Total wallets: <b>{count}</b>  |  Active: <b>{active}</b>\n\n"
        + ("Tap a wallet to manage it, or ➕ to add a new one."
           if wallets else
           "No wallets configured yet.\nTap ➕ to add the first wallet.")
    )
    try:
        await query.edit_message_text(text, reply_markup=_list_keyboard(gw, wallets), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Wallet detail view ─────────────────────────────────────────────────────

async def gww_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a single wallet's detail + actions (gww:view:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return

    try:
        await query.edit_message_text(_detail_text(gw, w), reply_markup=_detail_keyboard(gw, w), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Toggle active / disabled ───────────────────────────────────────────────

async def gww_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable / Disable a wallet (gww:tog:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return

    w["is_active"] = not w.get("is_active", True)
    _save(gw, wallets)

    new_status = "🟢 Active" if w["is_active"] else "🔴 Disabled"
    await query.answer(f"Wallet is now {new_status}.", show_alert=False)
    try:
        await query.edit_message_text(_detail_text(gw, w), reply_markup=_detail_keyboard(gw, w), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Set default wallet ─────────────────────────────────────────────────────

async def gww_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark a wallet as the default (gww:def:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return

    for other in wallets:
        other["is_default"] = (other.get("id") == wid)
    _save(gw, wallets)
    await query.answer("★ Set as default wallet.", show_alert=False)
    try:
        await query.edit_message_text(_detail_text(gw, w), reply_markup=_detail_keyboard(gw, w), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Copy address (fallback for clients that don't support CopyTextButton) ──

async def gww_copy_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Popup fallback for clients that don't support CopyTextButton."""
    query = update.callback_query
    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    address = (w.get("address") or "") if w else ""
    await query.answer(f"📋 {address}" if address else "⚠️ No address set.", show_alert=bool(address))


# ── Delete (confirmation screen) ──────────────────────────────────────────

async def gww_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a confirmation screen before deleting (gww:del:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"gww:delok:{gw}:{wid}")],
        [InlineKeyboardButton("❌ Cancel",      callback_data=f"gww:view:{gw}:{wid}")],
    ])
    try:
        await query.edit_message_text(
            f"⚠️ <b>Delete Wallet?</b>\n\n"
            f"Label:   {w.get('label','—')}\n"
            f"Address: <code>{w.get('address','—')}</code>\n\n"
            "This removes the wallet from the admin panel only.\n"
            "No payment logic, database, or active transactions are affected.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gww_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute wallet deletion after confirmation (gww:delok:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if w:
        wallets.remove(w)
        # If we removed the default, assign default to first active wallet
        if w.get("is_default") and wallets:
            active = [x for x in wallets if x.get("is_active", True)]
            if active:
                active[0]["is_default"] = True
        _save(gw, wallets)
        await query.answer("🗑 Wallet deleted.", show_alert=False)
    else:
        await query.answer("Already removed.", show_alert=False)

    # Go back to the wallet list
    count = len(wallets)
    active_count = sum(1 for x in wallets if x.get("is_active", True))
    text = (
        f"{_gw_label(gw)} — <b>💳 Wallet Manager</b>\n\n"
        f"Total wallets: <b>{count}</b>  |  Active: <b>{active_count}</b>\n\n"
        + ("Tap a wallet to manage it, or ➕ to add a new one."
           if wallets else
           "No wallets configured yet.\nTap ➕ to add the first wallet.")
    )
    try:
        await query.edit_message_text(text, reply_markup=_list_keyboard(gw, wallets), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Add Wallet conversation ────────────────────────────────────────────────

async def gww_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: ask for wallet label (gww:add:{gw})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    gw = query.data.split(":", 2)[2]
    context.user_data["gww_add"] = {"gw": gw}
    try:
        await query.edit_message_text(
            f"{_gw_label(gw)} — <b>➕ Add Wallet</b>\n\n"
            "Step 1 of 2\n\n"
            "Send a <b>label</b> for this wallet\n"
            "Examples: <code>USDT TRC20</code>, <code>BEP20 Address</code>, <code>bKash Personal</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"gww:list:{gw}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GWW_ADD_LABEL


async def gww_add_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive label, ask for address."""
    label = (update.message.text or "").strip()
    if not label:
        await update.message.reply_text("❌ Label cannot be empty. Please send a label:")
        return GWW_ADD_LABEL

    context.user_data["gww_add"]["label"] = label[:120]
    gw = context.user_data["gww_add"]["gw"]
    await update.message.reply_text(
        f"{_gw_label(gw)} — <b>➕ Add Wallet</b>\n\n"
        f"Step 2 of 2  |  Label: <b>{label}</b>\n\n"
        "Now send the <b>wallet address</b>:\n"
        "Examples: <code>TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>, "
        "<code>0xAbCd…</code>, <code>01712345678</code>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"gww:list:{gw}")
        ]]),
        parse_mode="HTML",
    )
    return GWW_ADD_ADDRESS


async def gww_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive address, save wallet, return to list."""
    address = (update.message.text or "").strip()
    if not address:
        await update.message.reply_text("❌ Address cannot be empty. Please send the wallet address:")
        return GWW_ADD_ADDRESS

    data = context.user_data.pop("gww_add", {})
    gw    = data.get("gw", "")
    label = data.get("label", "Wallet")

    wallets = _load(gw)
    is_first = len(wallets) == 0
    new_wallet = {
        "id":         _next_id(wallets),
        "label":      label,
        "address":    address[:512],
        "is_active":  True,
        "is_default": is_first,  # first wallet is default automatically
    }
    wallets.append(new_wallet)
    _save(gw, wallets)

    count = len(wallets)
    active_count = sum(1 for w in wallets if w.get("is_active", True))
    text = (
        f"✅ Wallet added.\n\n"
        f"{_gw_label(gw)} — <b>💳 Wallet Manager</b>\n\n"
        f"Total wallets: <b>{count}</b>  |  Active: <b>{active_count}</b>\n\n"
        "Tap a wallet to manage it, or ➕ to add a new one."
    )
    await update.message.reply_text(
        text,
        reply_markup=_list_keyboard(gw, wallets),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def gww_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("gww_add", None)
    if update.callback_query:
        await update.callback_query.answer()
        # Re-trigger the list view via callback data trick
        gw = update.callback_query.data.split(":", 2)[-1] if ":" in update.callback_query.data else ""
        if gw:
            wallets = _load(gw)
            count = len(wallets)
            active = sum(1 for w in wallets if w.get("is_active", True))
            text = (
                f"{_gw_label(gw)} — <b>💳 Wallet Manager</b>\n\n"
                f"Total wallets: <b>{count}</b>  |  Active: <b>{active}</b>\n\n"
                + ("Tap a wallet to manage it, or ➕ to add a new one."
                   if wallets else
                   "No wallets configured yet.\nTap ➕ to add the first wallet.")
            )
            try:
                await update.callback_query.edit_message_text(
                    text, reply_markup=_list_keyboard(gw, wallets), parse_mode="HTML"
                )
            except BadRequest:
                pass
    return ConversationHandler.END


# ── Edit Wallet conversation ───────────────────────────────────────────────

async def gww_edit_label_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: edit wallet label (gww:edlbl:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return ConversationHandler.END

    context.user_data["gww_edit"] = {"gw": gw, "id": wid, "field": "label"}
    try:
        await query.edit_message_text(
            f"{_gw_label(gw)} — <b>✏️ Edit Label</b>\n\n"
            f"Current label: <b>{w.get('label', '—')}</b>\n\n"
            "Send the new label:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"gww:view:{gw}:{wid}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GWW_EDIT_VALUE


async def gww_edit_address_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: edit wallet address (gww:edaddr:{gw}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    parts = query.data.split(":")
    gw, wid = parts[2], int(parts[3])
    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await query.answer("⚠️ Wallet not found.", show_alert=True)
        return ConversationHandler.END

    current = w.get("address") or "(not set)"
    context.user_data["gww_edit"] = {"gw": gw, "id": wid, "field": "address"}
    try:
        await query.edit_message_text(
            f"{_gw_label(gw)} — <b>📝 Edit Address</b>\n\n"
            f"Label: <b>{w.get('label', '—')}</b>\n"
            f"Current address: <code>{current}</code>\n\n"
            "Send the new wallet address\n"
            "(or <code>clear</code> to remove it):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"gww:view:{gw}:{wid}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return GWW_EDIT_VALUE


async def gww_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the new value, save it, return to wallet detail."""
    edit = context.user_data.pop("gww_edit", None)
    if not edit:
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    gw    = edit["gw"]
    wid   = edit["id"]
    field = edit["field"]

    if not value:
        await update.message.reply_text(f"❌ Value cannot be empty. Send the new {field}:")
        context.user_data["gww_edit"] = edit
        return GWW_EDIT_VALUE

    wallets = _load(gw)
    w = _find(wallets, wid)
    if not w:
        await update.message.reply_text("❌ Wallet no longer exists.")
        return ConversationHandler.END

    if field == "label":
        w["label"] = value[:120]
    elif field == "address":
        w["address"] = "" if value.lower() == "clear" else value[:512]

    _save(gw, wallets)
    await update.message.reply_text(
        f"✅ Updated.\n\n" + _detail_text(gw, w),
        reply_markup=_detail_keyboard(gw, w),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def gww_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("gww_edit", None)
    if update.callback_query:
        await update.callback_query.answer()
        # Re-render the wallet detail
        parts = update.callback_query.data.split(":")  # gww:view:{gw}:{id}
        if len(parts) >= 4:
            gw, wid = parts[2], int(parts[3])
            wallets = _load(gw)
            w = _find(wallets, wid)
            if w:
                try:
                    await update.callback_query.edit_message_text(
                        _detail_text(gw, w), reply_markup=_detail_keyboard(gw, w), parse_mode="HTML"
                    )
                except BadRequest:
                    pass
    return ConversationHandler.END


# ── Conversation handlers (factory functions) ──────────────────────────────

def build_gww_add_conv() -> ConversationHandler:
    """ConversationHandler for adding a new wallet to any gateway."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gww_add_start, pattern=r"^gww:add:[a-z_]+$"),
        ],
        states={
            GWW_ADD_LABEL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, gww_add_label)],
            GWW_ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, gww_add_address)],
        },
        fallbacks=[
            CallbackQueryHandler(gww_add_cancel, pattern=r"^gww:list:[a-z_]+$"),
            CommandHandler("cancel", gww_add_cancel),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


def build_gww_edit_conv() -> ConversationHandler:
    """ConversationHandler for editing label or address of a wallet."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gww_edit_label_start,   pattern=r"^gww:edlbl:[a-z_]+:\d+$"),
            CallbackQueryHandler(gww_edit_address_start, pattern=r"^gww:edaddr:[a-z_]+:\d+$"),
        ],
        states={
            GWW_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, gww_edit_value)],
        },
        fallbacks=[
            CallbackQueryHandler(gww_edit_cancel, pattern=r"^gww:view:[a-z_]+:\d+$"),
            CommandHandler("cancel", gww_edit_cancel),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
