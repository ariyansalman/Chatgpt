"""V41 — Admin API Key & Integration Manager.

Callback namespace: ``aim:*``

Callbacks handled
─────────────────
aim:menu                      — Main dashboard / health overview
aim:list:PAGE                 — Paginated integration list
aim:view:ID                   — Integration detail
aim:enable:ID                 — Set status → enabled
aim:disable:ID                — Set status → disabled
aim:maintenance:ID            — Set status → maintenance
aim:del_ask:ID                — Delete confirmation
aim:del_ok:ID                 — Execute delete
aim:test:ID                   — Run live health check now
aim:logs:ID:PAGE              — View connection logs
aim:settings                  — Settings panel
aim:settings:status:VAL       — Set global AIM status
aim:settings:toggle:KEY       — Toggle bool config key
aim:retry:ID                  — Retry connection now
aim:rotate_ask:ID             — API key rotate confirmation
aim:rotate_ok:ID              — Clear/reset API key

ConversationHandler entries:
  aim:add                     — Add new integration wizard
  aim:edit:ID:FIELD           — Edit a single field
  aim:set_key:ID              — Set/update API key (secure)
  aim:set_secret:ID           — Set/update API secret (secure)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import ApiIntegration, ApiConnectionLog
from services import api_integration_service as ais
from utils.helpers import is_admin
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

# ─── constants ───────────────────────────────────────────────────────────────
_PAGE_SIZE = 8
_LOG_PAGE_SIZE = 10

# ConversationHandler states
_S_AIM_NAME     = 8001
_S_AIM_PROVIDER = 8002
_S_AIM_TYPE     = 8003
_S_AIM_KEY      = 8004
_S_AIM_SECRET   = 8005
_S_AIM_URL      = 8006
_S_AIM_EDIT_VAL = 8010
_S_AIM_SK_VAL   = 8011
_S_AIM_SS_VAL   = 8012

_API_TYPES = [
    ("telegram", "🤖 Telegram"),
    ("payment",  "💳 Payment"),
    ("database", "🗄 Database"),
    ("smtp",     "📧 SMTP"),
    ("webhook",  "🌐 Webhook"),
    ("custom",   "🔧 Custom"),
]

_STATUS_EMOJI_MAP = {
    "connected": "🟢", "slow": "🟡", "warning": "🟠",
    "offline": "🔴",   "unknown": "⚫",
}

_BOOL_SETTINGS = [
    ("aim_auto_health_check", "🔄 Auto Health Check"),
    ("aim_auto_retry",        "🔁 Auto Retry"),
]

_EDITABLE_FIELDS = {
    "name":        ("Name",        "str"),
    "provider":    ("Provider",    "str"),
    "base_url":    ("Base URL",    "str"),
    "webhook_url": ("Webhook URL", "str"),
    "version":     ("Version",     "str"),
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _require_admin(uid: int) -> bool:
    return is_admin(uid)


async def _deny(update: Update) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer("⛔ Access denied.", show_alert=True)


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                      disable_web_page_preview=True)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                try:
                    await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                               disable_web_page_preview=True)
                except Exception:
                    pass
    else:
        msg = getattr(update, "message", None)
        if msg:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)


def _back_btn(label: str = "🔙 Back", data: str = "aim:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _conn_status_line(integ) -> str:
    cs = integ.connection_status or "unknown"
    em = _STATUS_EMOJI_MAP.get(cs, "⚫")
    rt = f" ({integ.response_time_ms} ms)" if integ.response_time_ms else ""
    return f"{em} {cs.title()}{rt}"


def _status_label(key: str = "aim_status") -> str:
    s = cfg.get_str(key, "enabled")
    return {"enabled": "🟢 Enabled", "maintenance": "🟡 Maintenance",
            "disabled": "🔴 Disabled"}.get(s, s)


# ─── main menu ───────────────────────────────────────────────────────────────

async def aim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return

    with get_db_session() as session:
        total = session.query(ApiIntegration).filter_by(is_active=True).count()
        from sqlalchemy import func
        status_counts = dict(
            session.query(ApiIntegration.connection_status, func.count())
            .filter_by(is_active=True)
            .group_by(ApiIntegration.connection_status)
            .all()
        )

    lines = []
    for st, em in _STATUS_EMOJI_MAP.items():
        c = status_counts.get(st, 0)
        if c > 0:
            lines.append(f"{em} {st.title()}: <b>{c}</b>")

    text = (
        "🔑 <b>API & Integration Manager</b>\n\n"
        f"Status: {_status_label()}\n"
        f"Total Integrations: <b>{total}</b>\n\n"
        "<b>Connection Status:</b>\n" + ("\n".join(lines) if lines else "No data yet.")
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Integrations", callback_data="aim:list:0"),
         InlineKeyboardButton("➕ Add New", callback_data="aim:add")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="aim:settings"),
         InlineKeyboardButton("🔄 Check All", callback_data="aim:check_all")],
        [_back_btn("🔙 Admin Panel", "admin_menu")],
    ])
    await _send(update, text, kb)


# ─── integration list ─────────────────────────────────────────────────────────

async def aim_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        page = int(q.data.split(":")[-1])
    except Exception:
        page = 0

    with get_db_session() as session:
        total = session.query(ApiIntegration).filter_by(is_active=True).count()
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        items = (
            session.query(ApiIntegration)
            .filter_by(is_active=True)
            .order_by(ApiIntegration.api_type, ApiIntegration.name)
            .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE)
            .all()
        )
        rows = [
            (i.id, i.name, i.provider, i.api_type, i.status, i.connection_status)
            for i in items
        ]

    buttons = []
    for iid, name, provider, atype, status, cs in rows:
        cs_em = _STATUS_EMOJI_MAP.get(cs, "⚫")
        st_em = "🟢" if status == "enabled" else ("🟡" if status == "maintenance" else "🔴")
        buttons.append([InlineKeyboardButton(
            f"{cs_em}{st_em} {name[:30]}",
            callback_data=f"aim:view:{iid}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"aim:list:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"aim:list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("➕ Add New", callback_data="aim:add"),
        _back_btn("🔙 Back", "aim:menu"),
    ])
    text = f"📋 <b>Integrations</b> — Page {page+1}/{total_pages} ({total} total)"
    await _send(update, text, InlineKeyboardMarkup(buttons))


# ─── integration detail ───────────────────────────────────────────────────────

async def aim_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return

    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if not integ:
            await q.answer("Not found", show_alert=True)
            return
        last_check = integ.last_check_at.strftime("%Y-%m-%d %H:%M") if integ.last_check_at else "Never"
        last_ok = integ.last_success_at.strftime("%Y-%m-%d %H:%M") if integ.last_success_at else "Never"
        last_err = integ.last_error_at.strftime("%Y-%m-%d %H:%M") if integ.last_error_at else "Never"
        # Display only hints — NEVER the actual key
        key_display = ais.mask_for_display(integ.api_key_hint)
        secret_display = ais.mask_for_display(integ.api_secret_hint)
        text = (
            f"🔑 <b>{integ.name}</b>\n\n"
            f"Provider: {integ.provider}\n"
            f"Type: {integ.api_type}\n"
            f"Status: {integ.status.title()}\n"
            f"Connection: {_conn_status_line(integ)}\n"
            f"API Key: <code>{key_display}</code>\n"
            f"API Secret: <code>{secret_display}</code>\n"
            f"Base URL: {integ.base_url or '—'}\n"
            f"Webhook URL: {integ.webhook_url or '—'}\n"
            f"Version: {integ.version or '—'}\n"
            f"Last Check: {last_check}\n"
            f"Last Success: {last_ok}\n"
            f"Last Error: {last_err}\n"
        )
        if integ.last_error_message:
            err_preview = integ.last_error_message[:120]
            text += f"Error: <i>{err_preview}</i>\n"
        built_in_note = " (built-in)" if integ.is_built_in else ""
        text += f"\n<i>ID {integ.id}{built_in_note}</i>"

    kb_rows = [
        [InlineKeyboardButton("🧪 Test Now", callback_data=f"aim:test:{iid}"),
         InlineKeyboardButton("📋 Logs", callback_data=f"aim:logs:{iid}:0")],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"aim:edit:{iid}"),
         InlineKeyboardButton("🔑 Set Key", callback_data=f"aim:set_key:{iid}")],
        [InlineKeyboardButton("🔒 Set Secret", callback_data=f"aim:set_secret:{iid}")],
    ]
    if not integ.is_built_in:
        # Status toggles — only for non-built-in or if admin wants to override
        kb_rows.append([
            InlineKeyboardButton("🟢 Enable",  callback_data=f"aim:enable:{iid}"),
            InlineKeyboardButton("🟡 Maint.",  callback_data=f"aim:maintenance:{iid}"),
            InlineKeyboardButton("🔴 Disable", callback_data=f"aim:disable:{iid}"),
        ])
        kb_rows.append([
            InlineKeyboardButton("🔄 Rotate Key", callback_data=f"aim:rotate_ask:{iid}"),
            InlineKeyboardButton("🗑 Delete",    callback_data=f"aim:del_ask:{iid}"),
        ])
    else:
        kb_rows.append([
            InlineKeyboardButton("🟢 Enable",  callback_data=f"aim:enable:{iid}"),
            InlineKeyboardButton("🟡 Maint.",  callback_data=f"aim:maintenance:{iid}"),
            InlineKeyboardButton("🔴 Disable", callback_data=f"aim:disable:{iid}"),
        ])
    kb_rows.append([_back_btn("🔙 Integrations", "aim:list:0")])
    await _send(update, text, InlineKeyboardMarkup(kb_rows))


# ─── status changes ───────────────────────────────────────────────────────────

async def _aim_set_status(update: Update, iid: int, new_status: str) -> None:
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if integ:
            integ.status = new_status
            integ.updated_at = datetime.utcnow()
            session.commit()
            log_admin_action(update.effective_user.id, "aim.status.change",
                             details=f"id={iid} status={new_status}")
    q = update.callback_query
    if q:
        await q.answer(f"✅ Status → {new_status}", show_alert=False)
    await aim_view(with_data(update, f"aim:view:{iid}"), update._context if hasattr(update, "_context") else None)


async def aim_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        i = session.query(ApiIntegration).filter_by(id=iid).first()
        if i:
            i.status = "enabled"
            i.updated_at = datetime.utcnow()
            session.commit()
    await q.answer("🟢 Enabled", show_alert=False)
    await aim_view(with_data(update, f"aim:view:{iid}"), context)


async def aim_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        i = session.query(ApiIntegration).filter_by(id=iid).first()
        if i:
            i.status = "disabled"
            i.updated_at = datetime.utcnow()
            session.commit()
    await q.answer("🔴 Disabled", show_alert=False)
    await aim_view(with_data(update, f"aim:view:{iid}"), context)


async def aim_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        i = session.query(ApiIntegration).filter_by(id=iid).first()
        if i:
            i.status = "maintenance"
            i.updated_at = datetime.utcnow()
            session.commit()
    await q.answer("🟡 Maintenance mode", show_alert=False)
    await aim_view(with_data(update, f"aim:view:{iid}"), context)


# ─── test connection ──────────────────────────────────────────────────────────

async def aim_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🔄 Testing connection…", show_alert=False)
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    import asyncio
    await asyncio.to_thread(ais.run_health_check, iid)
    await aim_view(with_data(update, f"aim:view:{iid}"), context)


async def aim_check_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🔄 Running all health checks…", show_alert=False)
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        with get_db_session() as session:
            ids = [i.id for i in session.query(ApiIntegration).filter_by(is_active=True).all()]
        import asyncio
        await asyncio.to_thread(lambda: [ais.run_health_check(iid) for iid in ids])
    except Exception:
        logger.exception("aim_check_all failed")
    await aim_menu(update, context)


# ─── logs ─────────────────────────────────────────────────────────────────────

async def aim_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        parts = q.data.split(":")   # aim:logs:ID:PAGE
        iid = int(parts[2])
        page = int(parts[3])
    except Exception:
        return

    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        name = integ.name if integ else f"ID {iid}"
        total = session.query(ApiConnectionLog).filter_by(integration_id=iid).count()
        total_pages = max(1, (total + _LOG_PAGE_SIZE - 1) // _LOG_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        logs = (
            session.query(ApiConnectionLog)
            .filter_by(integration_id=iid)
            .order_by(ApiConnectionLog.checked_at.desc())
            .offset(page * _LOG_PAGE_SIZE).limit(_LOG_PAGE_SIZE)
            .all()
        )
        lines = []
        for log in logs:
            em = _STATUS_EMOJI_MAP.get(log.status, "⚫")
            ts = log.checked_at.strftime("%m-%d %H:%M") if log.checked_at else "?"
            rt = f" {log.response_time_ms}ms" if log.response_time_ms else ""
            err = f" — {log.error_message[:40]}" if log.error_message else ""
            lines.append(f"{em} {ts}{rt}{err}")

    text = (
        f"📋 <b>Connection Logs: {name}</b>\n"
        f"Page {page+1}/{total_pages} ({total} total)\n\n"
        + ("\n".join(lines) if lines else "No logs yet.")
    )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"aim:logs:{iid}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"aim:logs:{iid}:{page+1}"))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([_back_btn("🔙 Back", f"aim:view:{iid}")])
    await _send(update, text, InlineKeyboardMarkup(buttons))


# ─── delete ───────────────────────────────────────────────────────────────────

async def aim_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        name = integ.name if integ else f"ID {iid}"
    text = (
        f"🗑 <b>Delete Integration</b>\n\nIntegration: <b>{name}</b>\n\n"
        "This will permanently delete the integration and all its connection logs. Continue?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Delete", callback_data=f"aim:del_ok:{iid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"aim:view:{iid}")],
    ])
    await _send(update, text, kb)


async def aim_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if integ:
            session.delete(integ)
            session.commit()
            log_admin_action(update.effective_user.id, "aim.delete",
                             details=f"id={iid} name={integ.name}")
    await q.answer("✅ Deleted.", show_alert=True)
    await aim_list(with_data(update, "aim:list:0"), context)


# ─── rotate key ───────────────────────────────────────────────────────────────

async def aim_rotate_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    text = (
        "🔄 <b>Rotate API Key</b>\n\n"
        "This will clear the current API key. You will need to enter a new one immediately after. "
        "The integration will be set to maintenance mode until a new key is provided. Continue?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Clear Key", callback_data=f"aim:rotate_ok:{iid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"aim:view:{iid}")],
    ])
    await _send(update, text, kb)


async def aim_rotate_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if integ:
            integ.api_key_masked = None
            integ.api_key_hint = None
            integ.status = "maintenance"
            integ.connection_status = "unknown"
            integ.updated_at = datetime.utcnow()
            session.commit()
            log_admin_action(update.effective_user.id, "aim.rotate_key",
                             details=f"id={iid}")
    await q.answer("✅ Key cleared. Integration set to maintenance.", show_alert=True)
    await aim_view(with_data(update, f"aim:view:{iid}"), context)


# ─── settings ────────────────────────────────────────────────────────────────

async def aim_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    timeout = cfg.get_int("aim_timeout_seconds", 10)
    retries = cfg.get_int("aim_retry_count", 3)
    interval = cfg.get_int("aim_health_check_interval_minutes", 15)
    retention = cfg.get_int("aim_log_retention_days", 30)
    text = (
        "⚙️ <b>API Manager Settings</b>\n\n"
        f"Status: {_status_label()}\n"
        f"Timeout: {timeout}s\n"
        f"Retry Count: {retries}\n"
        f"Health Check Interval: {interval} min\n"
        f"Log Retention: {retention} days\n"
    )
    for key, label in _BOOL_SETTINGS:
        val = "✅ ON" if cfg.get_bool(key, True) else "❌ OFF"
        text += f"\n{label}: {val}"

    status_btns = [
        InlineKeyboardButton("🟢 Enable",      callback_data="aim:settings:status:enabled"),
        InlineKeyboardButton("🟡 Maintenance", callback_data="aim:settings:status:maintenance"),
        InlineKeyboardButton("🔴 Disable",     callback_data="aim:settings:status:disabled"),
    ]
    toggle_rows = [
        [InlineKeyboardButton(f"Toggle: {label}", callback_data=f"aim:settings:toggle:{key}")]
        for key, label in _BOOL_SETTINGS
    ]
    kb = InlineKeyboardMarkup(
        [status_btns] + toggle_rows + [[_back_btn("🔙 Back", "aim:menu")]]
    )
    await _send(update, text, kb)


async def aim_settings_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    data = q.data
    parts = data.split(":")
    if len(parts) < 4:
        return
    action = parts[2]
    value = parts[3]
    if action == "status":
        cfg.set("aim_status", value)
        await q.answer(f"API Manager → {value}", show_alert=True)
    elif action == "toggle":
        current = cfg.get_bool(value, True)
        cfg.set(value, not current)
        await q.answer(f"{value} → {'ON' if not current else 'OFF'}", show_alert=True)
    await aim_settings(update, context)


# ─── add integration conversation ────────────────────────────────────────────

async def aim_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    context.user_data["new_aim"] = {}
    try:
        await q.edit_message_text(
            "➕ <b>Add Integration</b>\n\nStep 1/4: Enter the integration <b>name</b>:",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_AIM_NAME


async def aim_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty.")
        return _S_AIM_NAME
    context.user_data["new_aim"]["name"] = name
    await update.message.reply_text(
        "Step 2/4: Enter the <b>provider</b> name (e.g. Stripe, Telegram, PostgreSQL):",
        parse_mode="HTML",
    )
    return _S_AIM_PROVIDER


async def aim_add_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    provider = (update.message.text or "").strip()
    if not provider:
        await update.message.reply_text("❌ Provider cannot be empty.")
        return _S_AIM_PROVIDER
    context.user_data["new_aim"]["provider"] = provider
    type_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"aim_type:{code}")]
        for code, label in _API_TYPES
    ])
    await update.message.reply_text(
        "Step 3/4: Choose the <b>integration type</b>:", reply_markup=type_kb, parse_mode="HTML"
    )
    return _S_AIM_TYPE


async def aim_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    atype = q.data.split(":")[-1]
    context.user_data["new_aim"]["api_type"] = atype
    try:
        await q.edit_message_text(
            "Step 4/4: Enter the <b>base URL</b> (used for health checks), or send <code>skip</code>:",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_AIM_URL


async def aim_add_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    url = None if raw.lower() == "skip" else raw
    nd = context.user_data.get("new_aim", {})
    nd["base_url"] = url
    with get_db_session() as session:
        integ = ApiIntegration(
            name=nd.get("name", "Integration"),
            provider=nd.get("provider", "Custom"),
            api_type=nd.get("api_type", "custom"),
            base_url=url,
        )
        session.add(integ)
        session.commit()
        log_admin_action(update.effective_user.id, "aim.add",
                         details=f"name={integ.name} type={integ.api_type}")
        iid = integ.id
    await update.message.reply_text(
        f"✅ Integration <b>{nd.get('name')}</b> added (ID {iid}).\n\n"
        "Use the Integration Manager to set API keys securely.",
        parse_mode="HTML",
    )
    context.user_data.pop("new_aim", None)
    return ConversationHandler.END


async def aim_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_aim", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── edit integration field ───────────────────────────────────────────────────

async def aim_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return
    try:
        parts = q.data.split(":")  # aim:edit:ID
        iid = int(parts[2])
    except Exception:
        return
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"aim:edit_field:{iid}:{field}")]
        for field, (label, _) in _EDITABLE_FIELDS.items()
    ]
    buttons.append([_back_btn("🔙 Back", f"aim:view:{iid}")])
    await _send(update, "✏️ <b>Edit Integration</b> — choose a field:",
                InlineKeyboardMarkup(buttons))


async def aim_edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        parts = q.data.split(":")  # aim:edit_field:ID:FIELD
        iid = int(parts[2])
        field = parts[3]
    except Exception:
        return ConversationHandler.END
    label, _ = _EDITABLE_FIELDS.get(field, (field, "str"))
    context.user_data["aim_edit"] = {"id": iid, "field": field}
    try:
        await q.edit_message_text(
            f"✏️ <b>Edit {label}</b>\n\nEnter the new value:", parse_mode="HTML"
        )
    except BadRequest:
        pass
    return _S_AIM_EDIT_VAL


async def aim_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    ed = context.user_data.get("aim_edit", {})
    iid = ed.get("id")
    field = ed.get("field")
    if not iid or not field:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if not integ:
            await update.message.reply_text("❌ Not found.")
            return ConversationHandler.END
        setattr(integ, field, raw or None)
        integ.updated_at = datetime.utcnow()
        session.commit()
    await update.message.reply_text(f"✅ <b>{field}</b> updated.", parse_mode="HTML")
    context.user_data.pop("aim_edit", None)
    return ConversationHandler.END


async def aim_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("aim_edit", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── set API key (secure) conversation ───────────────────────────────────────

async def aim_set_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return ConversationHandler.END
    context.user_data["aim_sk"] = iid
    try:
        await q.edit_message_text(
            "🔑 <b>Set API Key</b>\n\n"
            "⚠️ Send the API key now. This message will be processed securely.\n"
            "The key will NEVER be shown in the UI after saving.\n\n"
            "Send <code>clear</code> to remove the current key.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_AIM_SK_VAL


async def aim_set_key_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    iid = context.user_data.get("aim_sk")
    raw = (update.message.text or "").strip()
    # Immediately delete the user's message to avoid key exposure in chat
    try:
        await update.message.delete()
    except Exception:
        pass
    if raw.lower() == "clear":
        masked, hint = None, None
        msg = "✅ API key cleared."
    else:
        masked, hint = ais._mask_key(raw)
        msg = f"✅ API key saved (hint: <code>{ais.mask_for_display(hint)}</code>)."
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if integ:
            integ.api_key_masked = masked
            integ.api_key_hint = hint
            integ.updated_at = datetime.utcnow()
            session.commit()
            log_admin_action(update.effective_user.id, "aim.set_key",
                             details=f"id={iid} hint={hint}")
    await update.message.reply_text(msg, parse_mode="HTML")
    context.user_data.pop("aim_sk", None)
    return ConversationHandler.END


async def aim_set_secret_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not _require_admin(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END
    try:
        iid = int(q.data.split(":")[-1])
    except Exception:
        return ConversationHandler.END
    context.user_data["aim_ss"] = iid
    try:
        await q.edit_message_text(
            "🔒 <b>Set API Secret</b>\n\n"
            "⚠️ Send the API secret now. It will be saved securely and never displayed.\n\n"
            "Send <code>clear</code> to remove the current secret.",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    return _S_AIM_SS_VAL


async def aim_set_secret_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    iid = context.user_data.get("aim_ss")
    raw = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if raw.lower() == "clear":
        masked, hint = None, None
        msg = "✅ API secret cleared."
    else:
        masked, hint = ais._mask_key(raw)
        msg = f"✅ API secret saved (hint: <code>{ais.mask_for_display(hint)}</code>)."
    with get_db_session() as session:
        integ = session.query(ApiIntegration).filter_by(id=iid).first()
        if integ:
            integ.api_secret_masked = masked
            integ.api_secret_hint = hint
            integ.updated_at = datetime.utcnow()
            session.commit()
            log_admin_action(update.effective_user.id, "aim.set_secret",
                             details=f"id={iid} hint={hint}")
    await update.message.reply_text(msg, parse_mode="HTML")
    context.user_data.pop("aim_ss", None)
    return ConversationHandler.END


async def aim_sk_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("aim_sk", None)
    context.user_data.pop("aim_ss", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── central dispatch ─────────────────────────────────────────────────────────

async def aim_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data if q else ""
    if data == "aim:menu":
        return await aim_menu(update, context)
    if data.startswith("aim:list:"):
        return await aim_list(update, context)
    if data.startswith("aim:view:"):
        return await aim_view(update, context)
    if data.startswith("aim:enable:"):
        return await aim_enable(update, context)
    if data.startswith("aim:disable:"):
        return await aim_disable(update, context)
    if data.startswith("aim:maintenance:"):
        return await aim_maintenance(update, context)
    if data.startswith("aim:test:"):
        return await aim_test(update, context)
    if data == "aim:check_all":
        return await aim_check_all(update, context)
    if data.startswith("aim:logs:"):
        return await aim_logs(update, context)
    if data.startswith("aim:del_ask:"):
        return await aim_del_ask(update, context)
    if data.startswith("aim:del_ok:"):
        return await aim_del_ok(update, context)
    if data.startswith("aim:rotate_ask:"):
        return await aim_rotate_ask(update, context)
    if data.startswith("aim:rotate_ok:"):
        return await aim_rotate_ok(update, context)
    if data.startswith("aim:edit:") and not data.startswith("aim:edit_field:"):
        return await aim_edit_start(update, context)
    if data == "aim:settings":
        return await aim_settings(update, context)
    if data.startswith("aim:settings:"):
        return await aim_settings_dispatch(update, context)
    if q:
        await q.answer("Unknown action.", show_alert=False)


# ─── handler registration ─────────────────────────────────────────────────────

def build_aim_add_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aim_add_start, pattern=r"^aim:add$")],
        states={
            _S_AIM_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_add_name)],
            _S_AIM_PROVIDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_add_provider)],
            _S_AIM_TYPE:     [CallbackQueryHandler(aim_add_type, pattern=r"^aim_type:")],
            _S_AIM_URL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_add_url)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, aim_add_cancel),
            CallbackQueryHandler(aim_add_cancel, pattern=r"^aim:menu$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_aim_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(
            aim_edit_field_start, pattern=r"^aim:edit_field:\d+:\w+$"
        )],
        states={
            _S_AIM_EDIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_edit_value)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, aim_edit_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_aim_set_key_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aim_set_key_start, pattern=r"^aim:set_key:\d+$")],
        states={
            _S_AIM_SK_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_set_key_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, aim_sk_cancel),
            CallbackQueryHandler(aim_sk_cancel, pattern=r"^aim:"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_aim_set_secret_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(aim_set_secret_start, pattern=r"^aim:set_secret:\d+$")],
        states={
            _S_AIM_SS_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, aim_set_secret_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, aim_sk_cancel),
            CallbackQueryHandler(aim_sk_cancel, pattern=r"^aim:"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def register_handlers(app) -> None:
    app.add_handler(build_aim_add_conv())
    app.add_handler(build_aim_edit_conv())
    app.add_handler(build_aim_set_key_conv())
    app.add_handler(build_aim_set_secret_conv())
    # Central dispatcher for all remaining aim:* callbacks
    app.add_handler(CallbackQueryHandler(aim_dispatch, pattern=r"^aim:"))
