"""Admin Exchange Rate Manager — V39.

Callback namespace: aerm:*

Admin capabilities:
  • View all configured currency pairs with live rates
  • Add / remove / enable / disable a pair
  • Set manual rate, buy rate, sell rate
  • Set margin (spread %)
  • Lock / unlock a pair (prevent auto-updates)
  • Manually refresh rates (single pair or all)
  • Configure auto-update interval per pair
  • View rate history and update log
  • Dashboard with daily stats
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, CallbackQueryHandler, ConversationHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from services import exchange_rate_service as ers
from utils.bot_config import cfg
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# Conversation states
ADD_PAIR_FROM, ADD_PAIR_TO, ADD_PAIR_RATE, ADD_PAIR_INTERVAL = range(100, 104)
SET_RATE_STATE, SET_MARGIN_STATE, SET_INTERVAL_STATE = range(110, 113)


def _back_kb(cb: str = "aerm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _status_icon(status: str, locked: bool) -> str:
    if locked:
        return "🔒"
    return {"enabled": "🟢", "disabled": "🔴", "maintenance": "🟡"}.get(status, "⚪")


def _fmt_rate(rate: Optional[float]) -> str:
    if rate is None or rate == 0:
        return "N/A"
    if rate >= 100:
        return f"{rate:,.4f}"
    if rate >= 1:
        return f"{rate:.6f}"
    return f"{rate:.8f}"


def _source_label(source: str) -> str:
    return {"manual": "Manual", "auto_api": "Auto API",
            "fixed": "Fixed", "custom": "Custom"}.get(source, source)


# ─── Main Menu / Dashboard ────────────────────────────────────────────────────

async def aerm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 Exchange Rate Manager main screen."""
    q = update.callback_query
    if q:
        await q.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        if q:
            await q.answer("⛔ Permission denied.", show_alert=True)
        return

    pairs = ers.get_all_pairs()
    stats = ers.get_dashboard_stats()

    status_val = cfg.get("exchange_rate_manager_status", "enabled")
    status_line = {"enabled": "🟢 Operational", "maintenance": "🟡 Maintenance",
                   "disabled": "🔴 Disabled"}.get(status_val, status_val)

    lines = [
        "🔄 <b>Exchange Rate Manager</b>\n",
        f"Status: <b>{status_line}</b>",
        f"Active pairs: <b>{stats.get('active_pairs', 0)}/{stats.get('total_pairs', 0)}</b>",
        f"Updates today: <b>{stats.get('updates_today', 0)}</b>  "
        f"Failed: <b>{stats.get('failed_updates_today', 0)}</b>",
        f"Locked pairs: <b>{stats.get('locked_pairs', 0)}</b>",
        "\n<b>Pairs:</b>",
    ]

    kb_rows = []
    for p in pairs:
        icon    = _status_icon(p["status"], p["is_locked"])
        mid     = _fmt_rate(p["mid_rate"])
        name    = p["display_name"]
        src_lbl = _source_label(p["rate_source"])
        lines.append(f"{icon} <b>{name}</b>: {mid}  [{src_lbl}]")
        kb_rows.append([
            InlineKeyboardButton(
                f"{icon} {name}",
                callback_data=f"aerm:pair:{p['id']}",
            )
        ])

    kb_rows.append([
        InlineKeyboardButton("➕ Add Pair",      callback_data="aerm:add:start"),
        InlineKeyboardButton("🔃 Refresh All",   callback_data="aerm:refresh_all"),
    ])
    kb_rows.append([
        InlineKeyboardButton("📊 Rate History",  callback_data="aerm:history:0"),
        InlineKeyboardButton("🔙 Back",          callback_data="acc:root"),
    ])

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup(kb_rows)
    try:
        if q:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aerm_menu: %s", e)


# ─── Pair detail ──────────────────────────────────────────────────────────────

async def aerm_pair_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detail for a single exchange rate pair."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0

    pairs = ers.get_all_pairs()
    pair  = next((p for p in pairs if p["id"] == pair_id), None)
    if not pair:
        await q.answer("Pair not found.", show_alert=True)
        return

    icon     = _status_icon(pair["status"], pair["is_locked"])
    name     = pair["display_name"]
    prev_mid = pair.get("previous_mid_rate", 0)
    change   = ""
    if prev_mid and pair["mid_rate"] and prev_mid > 0:
        pct = ((pair["mid_rate"] - prev_mid) / prev_mid) * 100
        arrow = "📈" if pct >= 0 else "📉"
        change = f"  {arrow} {pct:+.2f}% vs prev"

    last_upd = (pair["last_updated"].strftime("%Y-%m-%d %H:%M")
                if pair["last_updated"] else "Never")
    last_err  = pair.get("last_update_error") or "None"

    lines = [
        f"{icon} <b>{name}</b>\n",
        f"Mid Rate: <b>{_fmt_rate(pair['mid_rate'])}</b>{change}",
        f"Buy Rate: <b>{_fmt_rate(pair['buy_rate'])}</b>",
        f"Sell Rate: <b>{_fmt_rate(pair['sell_rate'])}</b>",
        f"Margin: <b>{pair['margin_pct']:.2f}%</b>",
        f"Override: <b>{_fmt_rate(pair['manual_override_rate'])}</b>",
        "",
        f"Source: <b>{_source_label(pair['rate_source'])}</b>",
        f"Auto-update: every <b>{pair['auto_update_interval']} min</b>",
        f"Locked: <b>{'Yes' if pair['is_locked'] else 'No'}</b>",
        f"Status: <b>{pair['status']}</b>",
        "",
        f"Last updated: <b>{last_upd}</b>",
        f"Updates today: <b>{pair['updates_today']}</b>  "
        f"Failed: <b>{pair['failed_updates_today']}</b>",
        f"Last error: <i>{last_err[:80]}</i>",
    ]

    lock_label   = "🔓 Unlock" if pair["is_locked"] else "🔒 Lock"
    lock_action  = "unlock" if pair["is_locked"] else "lock"
    toggle_label = "🔴 Disable" if pair["status"] == "enabled" else "🟢 Enable"
    toggle_st    = "disabled" if pair["status"] == "enabled" else "enabled"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set Rate",    callback_data=f"aerm:set_rate:{pair_id}"),
         InlineKeyboardButton("📊 Set Margin",  callback_data=f"aerm:set_margin:{pair_id}")],
        [InlineKeyboardButton("⏱ Set Interval", callback_data=f"aerm:set_interval:{pair_id}"),
         InlineKeyboardButton("🔃 Refresh Now", callback_data=f"aerm:refresh:{pair_id}")],
        [InlineKeyboardButton(lock_label,        callback_data=f"aerm:lock:{pair_id}:{lock_action}"),
         InlineKeyboardButton(toggle_label,      callback_data=f"aerm:toggle:{pair_id}:{toggle_st}")],
        [InlineKeyboardButton("📜 History",     callback_data=f"aerm:hist:{pair_id}"),
         InlineKeyboardButton("🗑 Remove Pair", callback_data=f"aerm:remove:{pair_id}")],
        [InlineKeyboardButton("🔙 Back",        callback_data="aerm:menu")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aerm_pair_detail: %s", e)


# ─── Refresh ─────────────────────────────────────────────────────────────────

async def aerm_refresh_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually refresh a single pair's rate."""
    q = update.callback_query
    await q.answer("🔄 Refreshing…")
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        result = ers.refresh_pair(pair_id, force=True, actor_id=update.effective_user.id,
                                  actor_type="admin")
        log_admin_action(update.effective_user.id, "exchange_rate.manual_refresh",
                         target_type="rate_pair", target_id=pair_id)
        await q.answer(f"✅ Rate updated: {_fmt_rate(result['mid_rate'])}")
    except Exception as e:
        await q.answer(f"❌ Refresh failed: {e}", show_alert=True)
    # Re-render pair detail
    await aerm_pair_detail(update, context)


async def aerm_refresh_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refresh all auto-update pairs."""
    q = update.callback_query
    await q.answer("🔄 Refreshing all pairs…")
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        result = ers.refresh_all_pairs(force=True)
        log_admin_action(update.effective_user.id, "exchange_rate.refresh_all")
        await q.answer(
            f"✅ Done: {result['success']} updated, {result['skipped']} skipped, "
            f"{result['failed']} failed.",
            show_alert=True,
        )
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
    await aerm_menu(update, context)


# ─── Lock / Toggle ────────────────────────────────────────────────────────────

async def aerm_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    action  = parts[3] if len(parts) >= 4 else "lock"
    locked  = (action == "lock")
    try:
        ers.lock_pair(pair_id, locked, actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, f"exchange_rate.{action}",
                         target_type="rate_pair", target_id=pair_id)
        await q.answer(f"✅ Pair {'locked' if locked else 'unlocked'}.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
    await aerm_pair_detail(update, context)


async def aerm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    status  = parts[3] if len(parts) >= 4 else "enabled"
    try:
        ers.set_pair_status(pair_id, status, actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, f"exchange_rate.status_{status}",
                         target_type="rate_pair", target_id=pair_id)
        await q.answer(f"✅ Pair is now {status}.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
    await aerm_pair_detail(update, context)


# ─── Remove Pair ─────────────────────────────────────────────────────────────

async def aerm_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable (not delete) a pair."""
    q = update.callback_query
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    try:
        ers.set_pair_status(pair_id, "disabled", actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "exchange_rate.remove",
                         target_type="rate_pair", target_id=pair_id)
        await q.answer("✅ Pair disabled.")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
    await aerm_menu(update, context)


# ─── Rate History ─────────────────────────────────────────────────────────────

async def aerm_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show rate history for a pair."""
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return

    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0

    pairs = ers.get_all_pairs()
    pair  = next((p for p in pairs if p["id"] == pair_id), None)
    name  = pair["display_name"] if pair else f"Pair #{pair_id}"

    history = ers.get_pair_history(pair_id, limit=15)
    lines   = [f"📜 <b>{name} — Rate History</b>\n"]
    if not history:
        lines.append("<i>No history yet.</i>")
    else:
        for h in history:
            when = h["recorded_at"].strftime("%m/%d %H:%M") if h["recorded_at"] else "?"
            lines.append(
                f"{when}  Mid: <b>{_fmt_rate(h['mid_rate'])}</b>  "
                f"Buy: {_fmt_rate(h['buy_rate'])}  "
                f"Sell: {_fmt_rate(h['sell_rate'])}  "
                f"[{_source_label(h['source'])}]"
            )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=f"aerm:pair:{pair_id}")],
    ])
    try:
        await q.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("aerm_history: %s", e)


# ─── Set Rate (conversation) ──────────────────────────────────────────────────

async def aerm_set_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    pairs   = ers.get_all_pairs()
    pair    = next((p for p in pairs if p["id"] == pair_id), None)
    if not pair:
        await q.answer("Pair not found.", show_alert=True)
        return ConversationHandler.END
    context.user_data["rate_pair_id"] = pair_id
    name = pair["display_name"]
    try:
        await q.edit_message_text(
            f"✏️ <b>Set Rate — {name}</b>\n\n"
            f"Current: <b>{_fmt_rate(pair['mid_rate'])}</b>\n\n"
            f"Enter the new mid rate (how many {pair['to_currency']} = 1 {pair['from_currency']}):\n\n"
            f"/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return SET_RATE_STATE


async def aerm_set_rate_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number:")
        return SET_RATE_STATE
    if rate <= 0:
        await update.message.reply_text("❌ Rate must be > 0:")
        return SET_RATE_STATE
    pair_id = context.user_data.pop("rate_pair_id", None)
    if not pair_id:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END
    try:
        ers.update_pair_manual_rate(pair_id, rate, actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "exchange_rate.set_manual_rate",
                         target_type="rate_pair", target_id=pair_id,
                         details=f"rate={rate}")
        await update.message.reply_text(f"✅ Rate updated to <b>{_fmt_rate(rate)}</b>.",
                                         parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


async def aerm_rate_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


def build_set_rate_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aerm_set_rate_start, pattern=r"^aerm:set_rate:\d+$")],
        states={
            SET_RATE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_set_rate_value)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aerm_rate_cancel)],
        allow_reentry=True,
    )


# ─── Set Margin (conversation) ────────────────────────────────────────────────

async def aerm_set_margin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    pairs   = ers.get_all_pairs()
    pair    = next((p for p in pairs if p["id"] == pair_id), None)
    if not pair:
        await q.answer("Pair not found.", show_alert=True)
        return ConversationHandler.END
    context.user_data["margin_pair_id"] = pair_id
    try:
        await q.edit_message_text(
            f"📊 <b>Set Margin — {pair['display_name']}</b>\n\n"
            f"Current margin: <b>{pair['margin_pct']:.2f}%</b>\n\n"
            f"Enter the spread margin in % (e.g. 2.5):\n0 = no spread\n\n/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return SET_MARGIN_STATE


async def aerm_set_margin_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        margin = float((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number (e.g. 2.5):")
        return SET_MARGIN_STATE
    if margin < 0:
        await update.message.reply_text("❌ Margin must be ≥ 0:")
        return SET_MARGIN_STATE
    pair_id = context.user_data.pop("margin_pair_id", None)
    if not pair_id:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END
    try:
        ers.set_pair_margin(pair_id, margin, actor_id=update.effective_user.id)
        log_admin_action(update.effective_user.id, "exchange_rate.set_margin",
                         target_type="rate_pair", target_id=pair_id,
                         details=f"margin={margin}%")
        await update.message.reply_text(f"✅ Margin updated to <b>{margin:.2f}%</b>.",
                                         parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


def build_set_margin_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aerm_set_margin_start, pattern=r"^aerm:set_margin:\d+$")],
        states={
            SET_MARGIN_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_set_margin_value)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aerm_rate_cancel)],
        allow_reentry=True,
    )


# ─── Set Interval (conversation) ─────────────────────────────────────────────

async def aerm_set_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    parts   = (q.data or "").split(":")
    pair_id = int(parts[2]) if len(parts) >= 3 else 0
    pairs   = ers.get_all_pairs()
    pair    = next((p for p in pairs if p["id"] == pair_id), None)
    if not pair:
        await q.answer("Pair not found.", show_alert=True)
        return ConversationHandler.END
    context.user_data["interval_pair_id"] = pair_id

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1 min",  callback_data=f"aerm_int:{pair_id}:1"),
         InlineKeyboardButton("5 min",  callback_data=f"aerm_int:{pair_id}:5"),
         InlineKeyboardButton("15 min", callback_data=f"aerm_int:{pair_id}:15")],
        [InlineKeyboardButton("30 min", callback_data=f"aerm_int:{pair_id}:30"),
         InlineKeyboardButton("1 hour", callback_data=f"aerm_int:{pair_id}:60"),
         InlineKeyboardButton("Manual", callback_data=f"aerm_int:{pair_id}:0")],
    ])
    try:
        await q.edit_message_text(
            f"⏱ <b>Set Auto-Update Interval — {pair['display_name']}</b>\n\n"
            f"Current: every <b>{pair['auto_update_interval']} min</b>\n"
            f"0 = manual only\n\nSelect interval or type a custom value in minutes:",
            reply_markup=kb, parse_mode="HTML",
        )
    except BadRequest:
        pass
    return SET_INTERVAL_STATE


async def aerm_set_interval_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts   = (q.data or "").split(":")
    pair_id = int(parts[1]) if len(parts) >= 2 else 0
    minutes = int(parts[2]) if len(parts) >= 3 else 60
    context.user_data.pop("interval_pair_id", None)
    try:
        from database import get_db_session
        from database.models import ExchangeRatePair
        with get_db_session() as s:
            pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
            if pair:
                pair.auto_update_interval = minutes
                s.commit()
        await q.answer(f"✅ Interval set to {minutes} min.")
        log_admin_action(update.effective_user.id, "exchange_rate.set_interval",
                         target_type="rate_pair", target_id=pair_id,
                         details=f"interval={minutes}min")
    except Exception as e:
        await q.answer(f"❌ {e}", show_alert=True)
    # Navigate back to pair detail
    await aerm_pair_detail(with_data(update, f"aerm:pair:{pair_id}"), context)
    return ConversationHandler.END


async def aerm_set_interval_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        minutes = int((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a whole number of minutes (e.g. 30):")
        return SET_INTERVAL_STATE
    if minutes < 0:
        await update.message.reply_text("❌ Must be ≥ 0:")
        return SET_INTERVAL_STATE
    pair_id = context.user_data.pop("interval_pair_id", None)
    if not pair_id:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END
    try:
        from database import get_db_session
        from database.models import ExchangeRatePair
        with get_db_session() as s:
            pair = s.query(ExchangeRatePair).filter_by(id=pair_id).first()
            if pair:
                pair.auto_update_interval = minutes
                s.commit()
        await update.message.reply_text(f"✅ Interval set to {minutes} min.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


def build_set_interval_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aerm_set_interval_start,
                                           pattern=r"^aerm:set_interval:\d+$")],
        states={
            SET_INTERVAL_STATE: [
                CallbackQueryHandler(aerm_set_interval_quick, pattern=r"^aerm_int:\d+:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_set_interval_text),
            ],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aerm_rate_cancel)],
        allow_reentry=True,
    )


# ─── Add Pair (conversation) ──────────────────────────────────────────────────

async def aerm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await q.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END
    try:
        await q.edit_message_text(
            "➕ <b>Add Exchange Rate Pair</b>\n\n"
            "Enter the FROM currency code (e.g. USD, BTC):\n\n/cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return ADD_PAIR_FROM


async def aerm_add_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()
    if not code or len(code) > 16:
        await update.message.reply_text("❌ Invalid. Enter 2-16 letter code:")
        return ADD_PAIR_FROM
    context.user_data["pair_from"] = code
    await update.message.reply_text(f"Enter the TO currency code (e.g. BDT, USD):")
    return ADD_PAIR_TO


async def aerm_add_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = (update.message.text or "").strip().upper()
    if not code or len(code) > 16:
        await update.message.reply_text("❌ Invalid. Enter 2-16 letter code:")
        return ADD_PAIR_TO
    from_code = context.user_data.get("pair_from", "")
    if code == from_code:
        await update.message.reply_text("❌ FROM and TO must differ:")
        return ADD_PAIR_TO
    context.user_data["pair_to"] = code
    await update.message.reply_text(
        f"Enter the initial rate: how many {code} = 1 {from_code}?\n(0 = fetch from API)"
    )
    return ADD_PAIR_RATE


async def aerm_add_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number:")
        return ADD_PAIR_RATE
    context.user_data["pair_rate"] = rate if rate > 0 else None
    await update.message.reply_text(
        "Auto-update interval in minutes (e.g. 60). 0 = manual only:"
    )
    return ADD_PAIR_INTERVAL


async def aerm_add_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mins = int((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a whole number of minutes:")
        return ADD_PAIR_INTERVAL

    from_code = context.user_data.pop("pair_from", "")
    to_code   = context.user_data.pop("pair_to", "")
    init_rate = context.user_data.pop("pair_rate", None)
    if not from_code or not to_code:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END

    source = "auto_api" if mins > 0 else "manual"
    try:
        result = ers.add_pair(
            from_code, to_code,
            rate_source=source,
            auto_update_interval=max(mins, 0),
            mid_rate=init_rate,
        )
        log_admin_action(update.effective_user.id, "exchange_rate.add_pair",
                         details=f"{from_code}/{to_code} interval={mins}min")
        await update.message.reply_text(
            f"✅ Pair <b>{result['display_name']}</b> added!\n"
            f"Rate: {_fmt_rate(result['mid_rate'])}",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
    return ConversationHandler.END


async def aerm_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


def build_add_pair_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aerm_add_start, pattern=r"^aerm:add:start$")],
        states={
            ADD_PAIR_FROM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_add_from)],
            ADD_PAIR_TO:       [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_add_to)],
            ADD_PAIR_RATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_add_rate)],
            ADD_PAIR_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, aerm_add_interval)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aerm_add_cancel)],
        allow_reentry=True,
    )


# ─── General dispatcher ───────────────────────────────────────────────────────

async def aerm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route aerm:* callbacks not handled by conversations."""
    q    = update.callback_query
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await aerm_menu(update, context)
    elif action == "pair":
        await aerm_pair_detail(update, context)
    elif action == "refresh":
        await aerm_refresh_pair(update, context)
    elif action == "refresh_all":
        await aerm_refresh_all(update, context)
    elif action == "lock":
        await aerm_lock(update, context)
    elif action == "toggle":
        await aerm_toggle(update, context)
    elif action == "remove":
        await aerm_remove(update, context)
    elif action == "hist":
        await aerm_history(update, context)
    elif action == "history":
        # Global history view (no pair_id)
        await q.answer()
        from database.models import ExchangeRateHistory
        from database import get_db_session
        from sqlalchemy import desc
        with get_db_session() as s:
            rows = (s.query(ExchangeRateHistory)
                    .order_by(desc(ExchangeRateHistory.recorded_at))
                    .limit(15).all())
            lines = ["📜 <b>Exchange Rate History (All Pairs)</b>\n"]
            for r in rows:
                when = r.recorded_at.strftime("%m/%d %H:%M") if r.recorded_at else "?"
                lines.append(
                    f"{r.from_currency}/{r.to_currency}  "
                    f"<b>{_fmt_rate(r.mid_rate)}</b>  [{_source_label(r.source)}]  {when}"
                )
        kb = _back_kb("aerm:menu")
        try:
            await q.edit_message_text("\n".join(lines) or "No history yet.",
                                      reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                pass
    else:
        await q.answer()
        await aerm_menu(update, context)


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all admin exchange rate manager handlers."""
    app.add_handler(build_add_pair_conv())
    app.add_handler(build_set_rate_conv())
    app.add_handler(build_set_margin_conv())
    app.add_handler(build_set_interval_conv())
    # General dispatcher for all other aerm:* callbacks
    app.add_handler(CallbackQueryHandler(aerm_dispatch, pattern=r"^aerm:.+$"))
