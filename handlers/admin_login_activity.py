"""Login Activity & Device Management — Admin Panel (V32).

Callback namespace: lam:*

Routes
------
lam:home                      — Dashboard with stats
lam:sessions[:<pg>]           — Active sessions list (paginated)
lam:sessions:all[:<pg>]       — All sessions (active + ended)
lam:history[:<pg>]            — All login records (paginated)
lam:history:sus[:<pg>]        — Suspicious logins only
lam:user:<db_uid>[:<pg>]      — Login history for a specific user
lam:devices[:<pg>]            — All registered devices
lam:force_logout:<db_uid>     — Force logout all sessions for a user
lam:settings                  — Settings panel
lam:setstatus:<v>             — Set feature status (enabled|maintenance|disabled)
lam:toggle:<key>              — Toggle a boolean config key
lam:setval:<key>:<val>        — Set an int/select config value
lam:noop                      — No-op (page labels)
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CallbackQueryHandler

from utils.permissions import has_permission
from utils.bot_config import cfg
from services.login_activity import (
    get_login_stats,
    get_all_sessions, get_all_sessions_count,
    get_all_login_records, get_all_login_records_count,
    get_all_devices, get_all_devices_count,
    get_user_login_history_admin, get_login_history_count,
    force_logout_user, get_all_sessions as _all_ses,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8   # rows per list page


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _status() -> str:
    return str(cfg.get("lam_status", "enabled") or "enabled")


def _semoji(st: str) -> str:
    return {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(st, "⚪")


def _fmt(dt: datetime | None) -> str:
    if not dt:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M")


def _uname(row: dict) -> str:
    u = row.get("username")
    return f"@{u}" if u else f"TG#{row.get('telegram_id', '?')}"


async def _safe_edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _check_access(tg_id: int) -> bool:
    return has_permission(tg_id, "manage_users")


def _back_home() -> list:
    return [[InlineKeyboardButton("🔙 Back to Login Activity", callback_data="lam:home")]]


# ─── Dashboard ────────────────────────────────────────────────────────────────

def _home_text(stats: dict) -> str:
    st = _status()
    se = _semoji(st)
    return (
        "🔐 <b>Login Activity & Device Manager</b>\n"
        f"Status: {se} {st.title()}\n\n"
        "📊 <b>Sessions</b>\n"
        f"  🟢 Active Sessions:   <b>{stats.get('active_sessions', 0)}</b>\n"
        f"  🔴 Ended Sessions:    <b>{stats.get('logged_out_sessions', 0)}</b>\n\n"
        "📅 <b>Login Events</b>\n"
        f"  Today:    <b>{stats.get('today_logins', 0)}</b>\n"
        f"  Weekly:   <b>{stats.get('weekly_logins', 0)}</b>\n"
        f"  Monthly:  <b>{stats.get('monthly_logins', 0)}</b>\n\n"
        "📱 <b>Devices</b>\n"
        f"  Total Devices:        <b>{stats.get('total_devices', 0)}</b>\n"
        f"  New Today:            <b>{stats.get('new_devices_today', 0)}</b>\n\n"
        "⚠️ <b>Security</b>\n"
        f"  Suspicious Logins:    <b>{stats.get('suspicious_logins', 0)}</b>\n"
        f"  New Device Logins:    <b>{stats.get('new_device_logins', 0)}</b>\n"
    )


def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Active Sessions",  callback_data="lam:sessions:0"),
         InlineKeyboardButton("📋 All Sessions",     callback_data="lam:sessions:all:0")],
        [InlineKeyboardButton("📜 Login History",   callback_data="lam:history:0"),
         InlineKeyboardButton("⚠️ Suspicious",      callback_data="lam:history:sus:0")],
        [InlineKeyboardButton("📱 Devices",          callback_data="lam:devices:0")],
        [InlineKeyboardButton("⚙️ Settings",         callback_data="lam:settings")],
        [InlineKeyboardButton("🔙 Admin Panel",      callback_data="acc:root")],
    ])


async def lam_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    stats = get_login_stats()
    await _safe_edit(query, _home_text(stats), _home_kb())


# ─── Active sessions list ─────────────────────────────────────────────────────

async def lam_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Active or all sessions list — lam:sessions:<pg> / lam:sessions:all:<pg>"""
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data  # lam:sessions:<pg>  or  lam:sessions:all:<pg>
    parts = data.split(":")
    # parts: ["lam", "sessions", <pg>]  or  ["lam", "sessions", "all", <pg>]
    active_only = True
    try:
        if len(parts) >= 4 and parts[2] == "all":
            active_only = False
            page = int(parts[3])
        elif len(parts) >= 3 and parts[2] != "all":
            page = int(parts[2])
        else:
            page = 0
    except (ValueError, IndexError):
        page = 0

    total = get_all_sessions_count(active_only=active_only)
    rows  = get_all_sessions(limit=_PAGE_SIZE, offset=page * _PAGE_SIZE,
                             active_only=active_only)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    title = "🟢 Active Sessions" if active_only else "📋 All Sessions"
    base  = "lam:sessions" if active_only else "lam:sessions:all"

    text = f"{title}  <i>({total} total, page {page + 1}/{total_pages})</i>\n\n"
    kb_rows = []

    for r in rows:
        uname = f"@{r['username']}" if r.get("username") else f"TG#{r['telegram_id']}"
        stat  = "🟢" if r["is_active"] else "🔴"
        last  = _fmt(r.get("last_active_at"))
        text += f"{stat} <b>{uname}</b>  — Last active: {last}\n"
        kb_rows.append([InlineKeyboardButton(
            f"{stat} {uname} [{_fmt(r.get('created_at'))}]",
            callback_data=f"lam:force_logout:{r['user_id']}",
        )])

    if not rows:
        text += "<i>No sessions found.</i>\n"

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{base}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lam:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"{base}:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)

    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Login history (all users) ────────────────────────────────────────────────

async def lam_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """All login records — lam:history:<pg>  or  lam:history:sus:<pg>"""
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data  = query.data
    parts = data.split(":")
    # lam:history:<pg>   or   lam:history:sus:<pg>
    suspicious_only = False
    try:
        if len(parts) >= 4 and parts[2] == "sus":
            suspicious_only = True
            page = int(parts[3])
        elif len(parts) >= 3 and parts[2] != "sus":
            page = int(parts[2])
        else:
            page = 0
    except (ValueError, IndexError):
        page = 0

    total = get_all_login_records_count(suspicious_only=suspicious_only)
    rows  = get_all_login_records(
        limit=_PAGE_SIZE, offset=page * _PAGE_SIZE,
        suspicious_only=suspicious_only,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    base  = "lam:history:sus" if suspicious_only else "lam:history"
    title = "⚠️ Suspicious Logins" if suspicious_only else "📜 Login History"

    text = f"{title}  <i>({total} total, page {page + 1}/{total_pages})</i>\n\n"
    kb_rows = []

    for r in rows:
        uname = f"@{r['username']}" if r.get("username") else f"TG#{r['telegram_id']}"
        sus   = "⚠️" if r["is_suspicious"] else ("📱" if r["is_new_device"] else "🔔")
        when  = _fmt(r.get("created_at"))
        loc   = r.get("language_code") or "N/A"
        text += f"{sus} <b>{uname}</b>  {when}  lang:{loc}\n"
        kb_rows.append([InlineKeyboardButton(
            f"{sus} {uname} — {when}",
            callback_data=f"lam:user:{r['user_id']}:0",
        )])

    if not rows:
        text += "<i>No records found.</i>\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{base}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lam:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"{base}:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)

    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Per-user login history ───────────────────────────────────────────────────

async def lam_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Login history for a specific user — lam:user:<db_uid>[:<pg>]"""
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = query.data.split(":")
    try:
        db_uid = int(parts[2])
        page   = int(parts[3]) if len(parts) >= 4 else 0
    except (ValueError, IndexError):
        await lam_home(update, context)
        return

    total = get_login_history_count(db_uid)
    rows  = get_user_login_history_admin(db_uid, limit=_PAGE_SIZE,
                                         offset=page * _PAGE_SIZE)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    text = (
        f"📜 <b>Login History</b>  (User DB#{db_uid})\n"
        f"<i>{total} records, page {page + 1}/{total_pages}</i>\n\n"
    )
    kb_rows = []

    for r in rows:
        sus  = "⚠️" if r["is_suspicious"] else ("📱" if r["is_new_device"] else "🔔")
        when = _fmt(r.get("created_at"))
        loc  = r.get("language_code") or "N/A"
        ip   = r.get("ip_address") or "N/A"
        text += f"{sus} {when}  lang:{loc}  IP:{ip}\n"

    if not rows:
        text += "<i>No login records found for this user.</i>\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"lam:user:{db_uid}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lam:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"lam:user:{db_uid}:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)

    kb_rows.append([InlineKeyboardButton(
        "🔴 Force Logout User",
        callback_data=f"lam:force_logout:{db_uid}",
    )])
    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Devices list ─────────────────────────────────────────────────────────────

async def lam_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """All registered devices — lam:devices:<pg>"""
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    total = get_all_devices_count()
    rows  = get_all_devices(limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    text = (
        f"📱 <b>Device Registry</b>  ({total} total, "
        f"page {page + 1}/{total_pages})\n\n"
    )
    kb_rows = []

    for r in rows:
        uname  = f"@{r['username']}" if r.get("username") else f"TG#{r['telegram_id']}"
        trust  = "✅" if r["is_trusted"] else "❔"
        loc    = r.get("language_code") or "N/A"
        logins = r.get("login_count", 0)
        last   = _fmt(r.get("last_seen_at"))
        text += f"{trust} <b>{uname}</b>  lang:{loc}  logins:{logins}  last:{last}\n"
        kb_rows.append([InlineKeyboardButton(
            f"{trust} {uname} — {logins} logins",
            callback_data=f"lam:user:{r['user_id']}:0",
        )])

    if not rows:
        text += "<i>No devices registered yet.</i>\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"lam:devices:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lam:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"lam:devices:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)

    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Force logout ──────────────────────────────────────────────────────────────

async def lam_force_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force logout all sessions for a user — lam:force_logout:<db_uid>"""
    query = update.callback_query
    await query.answer()
    if not _check_access(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    count = force_logout_user(db_uid)
    await query.answer(
        f"✅ Terminated {count} session(s) for user DB#{db_uid}.",
        show_alert=True,
    )
    stats = get_login_stats()
    await _safe_edit(query, _home_text(stats), _home_kb())


# ─── Settings ─────────────────────────────────────────────────────────────────

_BOOL_KEYS: list[tuple[str, str]] = [
    ("lam_track_history",    "Track Login History"),
    ("lam_track_devices",    "Track Devices"),
    ("lam_track_ip",         "Track IP Address"),
    ("lam_track_location",   "Track Location"),
    ("lam_notify_new_login", "Notify on New Login"),
    ("lam_notify_new_device","Notify on New Device"),
]

_SELECT_KEYS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("lam_max_history", "Max Login History", [
        ("30", "30 entries"), ("50", "50 entries"),
        ("100", "100 entries"), ("0", "Unlimited"),
    ]),
    ("lam_session_expiry_days", "Session Expiration", [
        ("1", "1 Day"), ("7", "7 Days"), ("30", "30 Days"),
        ("90", "90 Days"), ("0", "Never"),
    ]),
    ("lam_max_sessions", "Max Active Sessions", [
        ("1", "1"), ("2", "2"), ("3", "3"),
        ("5", "5"), ("0", "Unlimited"),
    ]),
]


def _settings_text() -> str:
    st = _status()
    se = _semoji(st)
    lines = [
        "⚙️ <b>Login Activity Settings</b>\n",
        f"Status: {se} {st.title()}\n",
        "━" * 24 + "\n",
        "<b>Feature Toggles</b>\n",
    ]
    for key, label in _BOOL_KEYS:
        val = cfg.get_bool(key, True)
        em  = "🟢 ON" if val else "🔴 OFF"
        lines.append(f"  {label}: {em}\n")
    lines.append("\n<b>Limits</b>\n")
    for key, label, opts in _SELECT_KEYS:
        cur = str(cfg.get(key, opts[0][0]) or opts[0][0])
        cur_label = next((lbl for v, lbl in opts if v == cur), cur)
        lines.append(f"  {label}: <b>{cur_label}</b>\n")
    return "".join(lines)


def _settings_kb() -> InlineKeyboardMarkup:
    rows = []
    # Status row
    rows.append([
        InlineKeyboardButton("🟢 Enable",      callback_data="lam:setstatus:enabled"),
        InlineKeyboardButton("🟡 Maintenance", callback_data="lam:setstatus:maintenance"),
        InlineKeyboardButton("🔴 Disable",     callback_data="lam:setstatus:disabled"),
    ])
    # Bool toggles
    for key, label in _BOOL_KEYS:
        val = cfg.get_bool(key, True)
        em  = "🟢 ON" if val else "🔴 OFF"
        rows.append([InlineKeyboardButton(
            f"Toggle: {label} [{em}]",
            callback_data=f"lam:toggle:{key}",
        )])
    # Select keys
    for key, label, opts in _SELECT_KEYS:
        cur = str(cfg.get(key, opts[0][0]) or opts[0][0])
        sub = []
        for val, lbl in opts:
            marker = "✅" if val == cur else ""
            sub.append(InlineKeyboardButton(
                f"{marker}{lbl}", callback_data=f"lam:setval:{key}:{val}"))
        # Wrap in groups of 4
        for i in range(0, len(sub), 4):
            rows.append(sub[i:i + 4])
    rows.extend(_back_home())
    return InlineKeyboardMarkup(rows)


async def lam_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    await _safe_edit(query, _settings_text(), _settings_kb())


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def lam_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central dispatcher for lam:* callbacks."""
    query = update.callback_query
    data  = query.data or ""
    parts = data.split(":")

    # parts[1] is the action word
    action = parts[1] if len(parts) >= 2 else ""

    if action == "home":
        await lam_home(update, context)
        return

    if action == "sessions":
        await lam_sessions(update, context)
        return

    if action == "history":
        await lam_history(update, context)
        return

    if action == "user":
        await lam_user(update, context)
        return

    if action == "devices":
        await lam_devices(update, context)
        return

    if action == "force_logout":
        await lam_force_logout(update, context)
        return

    if action == "settings":
        await lam_settings(update, context)
        return

    if action == "setstatus":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True)
            return
        await query.answer()
        val = parts[2] if len(parts) >= 3 else ""
        if val in ("enabled", "maintenance", "disabled"):
            cfg.set("lam_status", val)
        await _safe_edit(query, _settings_text(), _settings_kb())
        return

    if action == "toggle":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True)
            return
        await query.answer()
        key = parts[2] if len(parts) >= 3 else ""
        valid = {k for k, _ in _BOOL_KEYS}
        if key in valid:
            cfg.set(key, not cfg.get_bool(key, True))
        await _safe_edit(query, _settings_text(), _settings_kb())
        return

    if action == "setval":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True)
            return
        await query.answer()
        key = parts[2] if len(parts) >= 3 else ""
        val = parts[3] if len(parts) >= 4 else ""
        valid = {k for k, _, _ in _SELECT_KEYS}
        if key in valid:
            try:
                cfg.set(key, int(val))
                await query.answer(f"✅ {key} = {val}", show_alert=False)
            except ValueError:
                pass
        await _safe_edit(query, _settings_text(), _settings_kb())
        return

    if action == "noop":
        await query.answer()
        return

    # Fallback — show home
    await query.answer()
    stats = get_login_stats()
    await _safe_edit(query, _home_text(stats), _home_kb())


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    application.add_handler(
        CallbackQueryHandler(lam_dispatch, pattern=r"^lam:")
    )
