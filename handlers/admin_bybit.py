"""Admin panel controls for the Bybit Pay payment gateway.

Extended from the original to support:
  - Setting API Key / Secret via the Telegram admin panel (stored in
    PaymentGatewayConfig; env vars are used as fallback).
  - Viewing and resolving pending manual verifications (cases where the
    Bybit API could not automatically confirm a TXID).
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session
from database.models import (
    PaymentGatewayConfig, PendingManualVerification,
)
from utils.permissions import has_permission
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
) = range(19)

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

def _detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Disable" if cfg["enabled"] else "✅ Enable"
    net_rows = []
    for net in ALL_NETWORKS:
        enabled = net in cfg["allowed_networks"]
        wallet = cfg["wallets"].get(net, "")
        label = f"{'✅' if enabled else '⬜'} {net}" + (f" ({wallet[:10]}…)" if wallet else " ⚠️ no addr")
        net_rows.append([
            InlineKeyboardButton(label, callback_data=f"admin_bybit_toggle_net_{net}"),
            InlineKeyboardButton(f"✏️ Addr", callback_data=f"admin_bybit_edit_wallet_{net}"),
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆔 Bybit UID", callback_data="admin_bybit_edit_uid")],
        *net_rows,
        [
            InlineKeyboardButton("💵 Min Amount", callback_data="admin_bybit_edit_min"),
            InlineKeyboardButton("💰 Max Amount", callback_data="admin_bybit_edit_max"),
        ],
        [InlineKeyboardButton("⏱ Order Expiry (min)", callback_data="admin_bybit_edit_expiry")],
        [InlineKeyboardButton("🎁 Bonus %", callback_data="admin_bybit_edit_bonus")],
        [InlineKeyboardButton("📝 Payment Instructions", callback_data="admin_bybit_edit_instructions")],
        [
            InlineKeyboardButton("🔑 API Key", callback_data="admin_bybit_edit_apikey"),
            InlineKeyboardButton("🔒 API Secret", callback_data="admin_bybit_edit_apisecret"),
        ],
        [InlineKeyboardButton("📋 Pending Verifications", callback_data="admin_bybit_pending")],
        [InlineKeyboardButton("🧪 Test Bybit API", callback_data="admin_bybit_test")],
        [InlineKeyboardButton(toggle_label, callback_data="admin_bybit_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")],
    ])


def _summary_text(cfg: dict, api_status: str = "⚪ Not Configured") -> str:
    status = "✅ Enabled" if cfg["enabled"] else "🚫 Disabled"
    networks_with = [n for n in cfg["allowed_networks"] if cfg["wallets"].get(n)]
    key_line = f"API Key: {cfg['api_key_masked']}\n" if cfg["has_db_api_key"] else ""
    wallets_line = "\n".join(
        f"  {n}: <code>{cfg['wallets'][n][:24]}…</code>" if len(cfg["wallets"].get(n, "")) > 24
        else f"  {n}: <code>{cfg['wallets'].get(n) or '(not set)'}</code>"
        for n in ALL_NETWORKS
    )
    return (
        "💙 <b>Bybit Pay</b>\n\n"
        f"Status: {status}\n"
        f"API Status: {api_status}\n"
        f"Bybit UID: <code>{cfg['uid'] or '(not set)'}</code>\n"
        f"{key_line}"
        f"Deposit addresses:\n{wallets_line}\n"
        f"Active networks: {', '.join(networks_with) or '(none)'}\n"
        f"Min amount: ${cfg['min_amount']:.2f}\n"
        f"Max amount: {('$' + format(cfg['max_amount'], '.2f')) if cfg['max_amount'] else 'No limit'}\n"
        f"Order expiry: {cfg['order_expiry_minutes']} minutes\n"
        f"Bonus: {cfg['bonus_percent']:.2f}%\n\n"
        "Verified via Bybit V5 API (GET /v5/asset/deposit/query-*) — READ-ONLY.\n\n"
        "🔑 Set API Key/Secret via the buttons below (DB) or via env vars."
    )


# ─── Main view / toggle / test ─────────────────────────────────────────────

async def admin_bybit_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    cfg = _get_config_dict()
    status = await _api_status_label()
    try:
        await query.edit_message_text(
            _summary_text(cfg, status),
            reply_markup=_detail_keyboard(cfg),
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

    cfg = _get_config_dict()
    if not cfg["enabled"]:
        from services.bybit_pay import BybitPayService
        svc = BybitPayService()
        if not cfg["uid"] and not any(cfg["wallets"].values()):
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

    with get_db_session() as session:
        row = _get_or_create_config(session)
        current = [n.strip().upper() for n in (row.bybit_allowed_networks or "").split(",") if n.strip()]
        if network in current:
            current.remove(network)
        else:
            current.append(network)
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
    await query.answer(f"{'✅' if ok else '❌'} {msg}", show_alert=True)
    await admin_bybit_view(update, context)


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
            lines.append(
                f"• <b>#{pmv.id}</b> — Order #{pmv.internal_order_id}{extra}\n"
                f"  TXID: <code>{pmv.submitted_txid}</code>\n"
                f"  Amount: {pmv.amount} {pmv.currency}\n"
                f"  Outcome: {pmv.auto_outcome or 'unknown'}\n"
                f"  At: {pmv.created_at.strftime('%Y-%m-%d %H:%M') if pmv.created_at else '?'}\n"
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_bybit_view")]]),
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
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_trc20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT TRC20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_TRC20)


async def admin_bybit_edit_wallet_trc20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_trc20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_bep20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT BEP20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_BEP20)


async def admin_bybit_edit_wallet_bep20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_bep20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_ton_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT TON deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_TON)


async def admin_bybit_edit_wallet_ton_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_ton = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_base_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Base (Coinbase Base L2) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_BASE)


async def admin_bybit_edit_wallet_base_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_base = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_arb_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Arbitrum One deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_ARB)


async def admin_bybit_edit_wallet_arb_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_arb = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_op_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Optimism deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_OP)


async def admin_bybit_edit_wallet_op_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_op = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_matic_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Polygon (MATIC) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_MATIC)


async def admin_bybit_edit_wallet_matic_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_matic = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_sol_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Solana deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_SOL)


async def admin_bybit_edit_wallet_sol_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_sol = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_avaxc_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT Avalanche C-Chain deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_AVAXC)


async def admin_bybit_edit_wallet_avaxc_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_avaxc = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_ltc_start(update, context):
    return await _edit_start(update, context, "💬 Send the LTC (Litecoin) deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_LTC)


async def admin_bybit_edit_wallet_ltc_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_ltc = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_wallet_erc20_start(update, context):
    return await _edit_start(update, context, "💬 Send the USDT ERC20 deposit address, or 'clear' to remove.", BYBIT_EDIT_WALLET_ERC20)


async def admin_bybit_edit_wallet_erc20_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_wallet_erc20 = "" if value.lower() == "clear" else value[:255]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_min_start(update, context):
    return await _edit_start(update, context, "💬 Send minimum top-up amount in USD (e.g. 5), or 0 for no minimum.", BYBIT_EDIT_MIN)


async def admin_bybit_edit_min_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BYBIT_EDIT_MIN
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_min_amount = value
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_max_start(update, context):
    return await _edit_start(update, context, "💬 Send maximum top-up amount in USD (e.g. 500), or 0 for no maximum.", BYBIT_EDIT_MAX)


async def admin_bybit_edit_max_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float((update.message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid non-negative number.")
        return BYBIT_EDIT_MAX
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_max_amount = value if value > 0 else None
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
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
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
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
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
    return ConversationHandler.END


async def admin_bybit_edit_instructions_start(update, context):
    return await _edit_start(update, context, "💬 Send payment instructions (or 'default' to reset).", BYBIT_EDIT_INSTRUCTIONS)


async def admin_bybit_edit_instructions_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("❌ Please send a non-empty value.")
        return BYBIT_EDIT_INSTRUCTIONS
    with get_db_session() as session:
        row = _get_or_create_config(session)
        row.bybit_instructions = "" if value.lower() == "default" else value[:2000]
        session.commit()
    cfg = _get_config_dict()
    await update.message.reply_text(_summary_text(cfg, _quick_status_label()), reply_markup=_detail_keyboard(cfg), parse_mode="HTML")
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
    cfg = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Key {action}.\n\n" + _summary_text(cfg, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg),
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
    cfg = _get_config_dict()
    action = "cleared" if value.lower() == "clear" else "saved"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ API Secret {action}.\n\n" + _summary_text(cfg, _quick_status_label()),
        reply_markup=_detail_keyboard(cfg),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_bybit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        },
        fallbacks=[
            CallbackQueryHandler(admin_bybit_cancel, pattern="^admin_bybit_view$"),
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )
