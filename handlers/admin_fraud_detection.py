"""Smart Fraud Detection System — Admin Panel (V31).

Callback namespace: fds:*

Routes
------
fds:home                     — Fraud Detection Center dashboard
fds:list[:<level>][:<pg>]    — list flagged users (all|medium|high|critical)
fds:user:<db_uid>            — user risk detail card
fds:scan:<tg_uid>            — re-scan a user
fds:scan_all                 — queue full-scan of all users
fds:approve:<db_uid>         — clear flags / approve clean
fds:freeze:<db_uid>          — freeze wallet
fds:unfreeze:<db_uid>        — unfreeze wallet
fds:suspend:<db_uid>         — suspend account
fds:unsuspend:<db_uid>       — unsuspend account
fds:whitelist:<db_uid>       — whitelist (skip checks)
fds:blacklist:<db_uid>       — blacklist
fds:logs:<db_uid>[:<pg>]     — detection history for a user
fds:settings                 — settings panel
fds:setstatus:<v>            — set feature status
fds:toggle:<key>             — toggle boolean config key
fds:setint:<key>:<val>       — set integer config value
fds:search                   — prompt search (via ConversationHandler entry)
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CallbackQueryHandler

from utils.permissions import has_permission
from utils.bot_config import cfg
from services.fraud_detection import (
    run_checks, get_risk, get_fraud_stats,
    get_flagged_users, get_user_logs,
    admin_set_state, admin_clear_risk,
    _level_emoji, CHECK_DEFS,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8          # users per list page
_LOG_PAGE  = 10         # log entries per page

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fp(v: float) -> str:
    try:
        from utils import format_price
        return format_price(v)
    except Exception:
        return f"${v:.2f}"


async def _safe_edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _check_access(update: Update) -> bool:
    return has_permission(update.effective_user.id, "manage_users")


def _level_label(level: str) -> str:
    emoji = _level_emoji(level)
    return f"{emoji} {level.title()}"


# ─── Home Dashboard ───────────────────────────────────────────────────────────

def _build_home_text(stats: dict) -> str:
    status = cfg.get("fds_status", "enabled")
    semoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "⚪")
    return (
        "🔍 <b>Fraud Detection Center</b>\n"
        f"Status: {semoji} {status.title()}\n\n"
        "📊 <b>Overview</b>\n"
        f"  Total Flagged:     <b>{stats.get('total_alerts', 0)}</b>\n"
        f"  🟠 High Risk:      <b>{stats.get('high_risk', 0)}</b>\n"
        f"  🔴 Critical Risk:  <b>{stats.get('critical_risk', 0)}</b>\n"
        f"  🔒 Frozen Wallets: <b>{stats.get('frozen', 0)}</b>\n"
        f"  ⏸ Suspended:      <b>{stats.get('suspended', 0)}</b>\n\n"
        "📅 <b>Detection Events</b>\n"
        f"  Today:   <b>{stats.get('today', 0)}</b>\n"
        f"  Weekly:  <b>{stats.get('weekly', 0)}</b>\n"
        f"  Monthly: <b>{stats.get('monthly', 0)}</b>\n"
    )


def _build_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟠 High Risk",     callback_data="fds:list:high:0"),
         InlineKeyboardButton("🔴 Critical",      callback_data="fds:list:critical:0")],
        [InlineKeyboardButton("🟡 Medium Risk",   callback_data="fds:list:medium:0"),
         InlineKeyboardButton("📋 All Flagged",   callback_data="fds:list:all:0")],
        [InlineKeyboardButton("🔄 Scan All Users", callback_data="fds:scan_all")],
        [InlineKeyboardButton("⚙️ Settings",       callback_data="fds:settings")],
        [InlineKeyboardButton("🔙 Back",           callback_data="acc:root")],
    ])


# ─── User List ────────────────────────────────────────────────────────────────

def _build_list_text(users: list[dict], level: str, page: int, total_hint: int) -> str:
    level_str = "All Flagged" if level == "all" else f"{_level_label(level)} Risk Users"
    header = f"🔍 <b>Fraud Detection — {level_str}</b>\nPage {page + 1}\n\n"
    if not users:
        return header + "<i>No users found at this risk level.</i>"
    lines = [header]
    for u in users:
        em = _level_emoji(u["risk_level"])
        name = f"@{u['username']}" if u.get("username") else f"ID:{u['telegram_id']}"
        frozen   = " 🔒" if u.get("is_frozen")    else ""
        suspended= " ⏸" if u.get("is_suspended") else ""
        wl       = " ✅" if u.get("is_whitelisted") else ""
        lines.append(
            f"{em} {name}{frozen}{suspended}{wl} — score <b>{u['risk_score']}</b>"
        )
    return "\n".join(lines)


def _build_list_kb(users: list[dict], level: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        name = f"@{u['username']}" if u.get("username") else f"TG:{u['telegram_id']}"
        rows.append([InlineKeyboardButton(
            f"{_level_emoji(u['risk_level'])} {name}",
            callback_data=f"fds:user:{u['db_user_id']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"fds:list:{level}:{page-1}"))
    if len(users) == _PAGE_SIZE:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"fds:list:{level}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="fds:home")])
    return InlineKeyboardMarkup(rows)


# ─── User Detail ──────────────────────────────────────────────────────────────

def _build_user_text(db_uid: int, risk: dict, logs: list[dict]) -> str:
    level   = risk.get("risk_level", "low")
    score   = risk.get("risk_score", 0)
    flags   = risk.get("flags", [])
    frozen  = risk.get("is_frozen", False)
    susp    = risk.get("is_suspended", False)
    wl      = risk.get("is_whitelisted", False)
    bl      = risk.get("is_blacklisted", False)
    checked = risk.get("last_checked")
    checked_str = checked.strftime("%Y-%m-%d %H:%M") if isinstance(checked, datetime) else "Never"

    flag_names = {c: lbl for c, lbl, _ in CHECK_DEFS}
    flag_lines = "\n".join(f"  • {flag_names.get(f, f)}" for f in flags) or "  None"

    last_logs = "\n".join(
        f"  [{r['created_at'].strftime('%m/%d %H:%M') if isinstance(r['created_at'], datetime) else '?'}] "
        f"{r['check_type']} (+{r['delta']}) → {r['action'] or '—'}"
        for r in logs[:5]
    ) or "  No recent events."

    state_line = " ".join(filter(None, [
        "🔒 Frozen"      if frozen else "",
        "⏸ Suspended"   if susp   else "",
        "✅ Whitelisted" if wl     else "",
        "🚫 Blacklisted" if bl     else "",
    ])) or "Normal"

    return (
        f"🔍 <b>User Risk Report</b>  (DB#{db_uid})\n\n"
        f"Risk: {_level_emoji(level)} <b>{level.upper()}</b>  (score: {score})\n"
        f"State: {state_line}\n"
        f"Last Scan: {checked_str}\n\n"
        f"<b>Active Flags</b>:\n{flag_lines}\n\n"
        f"<b>Recent Events</b>:\n{last_logs}"
    )


def _build_user_kb(db_uid: int, risk: dict, level: str) -> InlineKeyboardMarkup:
    frozen  = risk.get("is_frozen",      False)
    susp    = risk.get("is_suspended",   False)
    wl      = risk.get("is_whitelisted", False)
    bl      = risk.get("is_blacklisted", False)
    tg_id   = risk.get("telegram_id",   db_uid)

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔄 Re-Scan",       callback_data=f"fds:scan:{tg_id}"),
         InlineKeyboardButton("✅ Approve/Clear",  callback_data=f"fds:approve:{db_uid}")],
        [
            InlineKeyboardButton("🔓 Unfreeze" if frozen else "🔒 Freeze Wallet",
                                 callback_data=f"fds:{'unfreeze' if frozen else 'freeze'}:{db_uid}"),
            InlineKeyboardButton("▶ Unsuspend" if susp else "⏸ Suspend",
                                 callback_data=f"fds:{'unsuspend' if susp else 'suspend'}:{db_uid}"),
        ],
        [
            InlineKeyboardButton("✅ Whitelist" if not wl else "❌ Remove Whitelist",
                                 callback_data=f"fds:{'whitelist' if not wl else 'unwhitelist'}:{db_uid}"),
            InlineKeyboardButton("🚫 Blacklist" if not bl else "🟢 Remove Blacklist",
                                 callback_data=f"fds:{'blacklist' if not bl else 'unblacklist'}:{db_uid}"),
        ],
        [InlineKeyboardButton("📋 Full History", callback_data=f"fds:logs:{db_uid}:0")],
        [InlineKeyboardButton("🔙 Back",          callback_data=f"fds:list:{level}:0")],
    ]
    return InlineKeyboardMarkup(rows)


# ─── Detection Log History ────────────────────────────────────────────────────

def _build_logs_text(db_uid: int, logs: list[dict], page: int) -> str:
    header = f"📋 <b>Detection History</b>  DB#{db_uid}\nPage {page + 1}\n\n"
    if not logs:
        return header + "<i>No fraud events recorded.</i>"
    flag_names = {c: lbl for c, lbl, _ in CHECK_DEFS}
    lines = [header]
    for r in logs:
        when = r["created_at"].strftime("%Y-%m-%d %H:%M") if isinstance(r["created_at"], datetime) else "?"
        ctype = flag_names.get(r["check_type"], r["check_type"])
        delta = f"+{r['delta']}" if r["delta"] >= 0 else str(r["delta"])
        act = r["action"] or "—"
        det = (f"\n       <i>{r['details'][:80]}</i>" if r.get("details") else "")
        lines.append(f"<code>{when}</code> {ctype} ({delta})\n       → {act}{det}\n")
    return "\n".join(lines)


def _build_logs_kb(db_uid: int, page: int, has_more: bool) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"fds:logs:{db_uid}:{page-1}"))
    if has_more:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"fds:logs:{db_uid}:{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"fds:user:{db_uid}")])
    return InlineKeyboardMarkup(rows)


# ─── Settings Panel ───────────────────────────────────────────────────────────

_BOOL_KEYS: list[tuple[str, str]] = [
    ("fds_check_dup_txid",       "Dup TXID Check"),
    ("fds_check_dup_wallet",     "Dup Wallet Check"),
    ("fds_check_dup_deposit",    "Dup Deposit Check"),
    ("fds_check_dup_withdrawal", "Dup Withdrawal Check"),
    ("fds_check_referral_abuse", "Referral Abuse Check"),
    ("fds_check_coupon_abuse",   "Coupon Abuse Check"),
    ("fds_auto_freeze",          "Auto Freeze"),
    ("fds_auto_suspend",         "Auto Suspend"),
    ("fds_admin_alerts",         "Admin Alerts"),
]

_INT_KEYS: list[tuple[str, str, int, int]] = [
    # (key, label, min, max)
    ("fds_max_failed_payments",   "Max Failed Payments",   1, 50),
    ("fds_max_daily_withdrawals", "Max Daily Withdrawals", 1, 20),
    ("fds_max_daily_deposits",    "Max Daily Deposits",    1, 100),
    ("fds_max_daily_orders",      "Max Daily Orders",      1, 100),
    ("fds_risk_threshold_medium", "Medium Risk Threshold", 10, 50),
    ("fds_risk_threshold_high",   "High Risk Threshold",   40, 80),
    ("fds_risk_threshold_critical","Critical Threshold",   70, 100),
]


def _build_settings_text() -> str:
    status = cfg.get("fds_status", "enabled")
    semoji = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(status, "⚪")
    lines = [
        "⚙️ <b>Fraud Detection Settings</b>\n",
        f"Feature Status: {semoji} {status.title()}\n",
        "<b>Detection Checks:</b>",
    ]
    for key, label in _BOOL_KEYS:
        val = cfg.get_bool(key, True)
        lines.append(f"  {'✅' if val else '❌'} {label}")
    lines.append("\n<b>Thresholds:</b>")
    for key, label, *_ in _INT_KEYS:
        val = cfg.get(key, "?")
        lines.append(f"  • {label}: <b>{val}</b>")
    return "\n".join(lines)


def _build_settings_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="fds:setstatus:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="fds:setstatus:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="fds:setstatus:disabled")],
    ]
    # Bool toggles — 2 per row
    bool_btns = [
        InlineKeyboardButton(
            f"{'✅' if cfg.get_bool(k, True) else '❌'} {lbl}",
            callback_data=f"fds:toggle:{k}",
        )
        for k, lbl in _BOOL_KEYS
    ]
    for i in range(0, len(bool_btns), 2):
        rows.append(bool_btns[i:i+2])

    # Integer setters — preset values
    rows.append([InlineKeyboardButton("━━ Limit Thresholds ━━", callback_data="fds:noop")])
    rows.append([
        InlineKeyboardButton("Failed≥3",  callback_data="fds:setint:fds_max_failed_payments:3"),
        InlineKeyboardButton("Failed≥5",  callback_data="fds:setint:fds_max_failed_payments:5"),
        InlineKeyboardButton("Failed≥10", callback_data="fds:setint:fds_max_failed_payments:10"),
    ])
    rows.append([
        InlineKeyboardButton("W/D≤2/d",  callback_data="fds:setint:fds_max_daily_withdrawals:2"),
        InlineKeyboardButton("W/D≤3/d",  callback_data="fds:setint:fds_max_daily_withdrawals:3"),
        InlineKeyboardButton("W/D≤5/d",  callback_data="fds:setint:fds_max_daily_withdrawals:5"),
    ])
    rows.append([
        InlineKeyboardButton("Dep≤5/d",  callback_data="fds:setint:fds_max_daily_deposits:5"),
        InlineKeyboardButton("Dep≤10/d", callback_data="fds:setint:fds_max_daily_deposits:10"),
        InlineKeyboardButton("Dep≤20/d", callback_data="fds:setint:fds_max_daily_deposits:20"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="fds:home")])
    return InlineKeyboardMarkup(rows)


# ─── Main Dispatcher ──────────────────────────────────────────────────────────

async def fds_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: C901
    query = update.callback_query
    if query is None:
        return

    if not _check_access(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()
    data  = query.data or ""
    parts = data.split(":")
    # ["fds", action, arg1, arg2, ...]
    action = parts[1] if len(parts) > 1 else "home"
    arg1   = parts[2] if len(parts) > 2 else ""
    arg2   = parts[3] if len(parts) > 3 else ""
    admin_tg_id = update.effective_user.id

    # ── fds:home ─────────────────────────────────────────────────────────────
    if action in ("home", ""):
        stats = get_fraud_stats()
        await _safe_edit(query, _build_home_text(stats), _build_home_kb())
        return

    # ── fds:list[:<level>][:<page>] ───────────────────────────────────────────
    if action == "list":
        level = arg1 if arg1 in ("all", "medium", "high", "critical") else "all"
        try:
            page = int(arg2)
        except ValueError:
            page = 0
        users = get_flagged_users(level, _PAGE_SIZE, page * _PAGE_SIZE)
        text  = _build_list_text(users, level, page, len(users))
        kb    = _build_list_kb(users, level, page)
        await _safe_edit(query, text, kb)
        return

    # ── fds:user:<db_uid> ─────────────────────────────────────────────────────
    if action == "user":
        try:
            db_uid = int(arg1)
        except ValueError:
            await _safe_edit(query, "Invalid user ID.", InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="fds:home")]]))
            return
        risk = get_risk(db_uid)
        # Also inject telegram_id from DB for scan button
        try:
            from database import get_db_session
            from sqlalchemy import text as _txt
            with get_db_session() as s:
                row = s.execute(_txt(
                    "SELECT telegram_id FROM users WHERE id = :uid"
                ), {"uid": db_uid}).fetchone()
                if row:
                    risk["telegram_id"] = int(row[0])
        except Exception:
            risk.setdefault("telegram_id", db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:scan:<tg_uid> ─────────────────────────────────────────────────────
    if action == "scan":
        try:
            tg_uid = int(arg1)
        except ValueError:
            return
        await query.answer("🔄 Scanning…", show_alert=False)
        result = run_checks(tg_uid)
        await query.answer(
            f"Scan complete: {result.emoji} {result.risk_level.upper()} (score {result.risk_score})",
            show_alert=True,
        )
        # Refresh user detail — look up db_uid
        try:
            from database import get_db_session
            from sqlalchemy import text as _txt
            with get_db_session() as s:
                row = s.execute(_txt(
                    "SELECT id FROM users WHERE telegram_id = :uid"
                ), {"uid": tg_uid}).fetchone()
                db_uid = int(row[0]) if row else tg_uid
        except Exception:
            db_uid = tg_uid
        risk = get_risk(db_uid)
        risk["telegram_id"] = tg_uid
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:scan_all ─────────────────────────────────────────────────────────
    if action == "scan_all":
        try:
            from database import get_db_session
            from sqlalchemy import text as _txt
            with get_db_session() as s:
                rows = s.execute(_txt(
                    "SELECT telegram_id FROM users WHERE is_banned = FALSE LIMIT 200"
                )).fetchall()
            tg_ids = [int(r[0]) for r in rows]
        except Exception:
            tg_ids = []

        scanned = 0
        for uid in tg_ids:
            try:
                run_checks(uid)
                scanned += 1
            except Exception:
                pass

        await query.answer(f"✅ Scanned {scanned} users.", show_alert=True)
        stats = get_fraud_stats()
        await _safe_edit(query, _build_home_text(stats), _build_home_kb())
        return

    # ── fds:approve:<db_uid> ─────────────────────────────────────────────────
    if action == "approve":
        try:
            db_uid = int(arg1)
        except ValueError:
            return
        admin_clear_risk(db_uid, admin_tg_id)
        await query.answer("✅ Risk cleared and user approved.", show_alert=True)
        risk = get_risk(db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:freeze / fds:unfreeze ────────────────────────────────────────────
    if action in ("freeze", "unfreeze"):
        try:
            db_uid = int(arg1)
        except ValueError:
            return
        frozen = (action == "freeze")
        admin_set_state(db_uid, admin_tg_id, "is_frozen", frozen,
                        f"Admin {'froze' if frozen else 'unfroze'} wallet")
        await query.answer(
            f"{'🔒 Wallet frozen.' if frozen else '🔓 Wallet unfrozen.'}",
            show_alert=True,
        )
        risk = get_risk(db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:suspend / fds:unsuspend ───────────────────────────────────────────
    if action in ("suspend", "unsuspend"):
        try:
            db_uid = int(arg1)
        except ValueError:
            return
        susp = (action == "suspend")
        admin_set_state(db_uid, admin_tg_id, "is_suspended", susp,
                        f"Admin {'suspended' if susp else 'unsuspended'} account")
        await query.answer(
            f"{'⏸ Account suspended.' if susp else '▶ Account unsuspended.'}",
            show_alert=True,
        )
        risk = get_risk(db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:whitelist / fds:unwhitelist ──────────────────────────────────────
    if action in ("whitelist", "unwhitelist"):
        try:
            db_uid = int(arg1)
        except ValueError:
            return
        wl = (action == "whitelist")
        admin_set_state(db_uid, admin_tg_id, "is_whitelisted", wl,
                        f"Admin {'whitelisted' if wl else 'removed from whitelist'}")
        await query.answer(
            f"{'✅ User whitelisted — fraud checks skipped.' if wl else '❌ Whitelist removed.'}",
            show_alert=True,
        )
        risk = get_risk(db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:blacklist / fds:unblacklist ──────────────────────────────────────
    if action in ("blacklist", "unblacklist"):
        try:
            db_uid = int(arg1)
        except ValueError:
            return
        bl = (action == "blacklist")
        admin_set_state(db_uid, admin_tg_id, "is_blacklisted", bl,
                        f"Admin {'blacklisted' if bl else 'removed from blacklist'}")
        await query.answer(
            f"{'🚫 User blacklisted.' if bl else '🟢 Blacklist removed.'}",
            show_alert=True,
        )
        risk = get_risk(db_uid)
        logs = get_user_logs(db_uid, 5, 0)
        level = context.user_data.get("fds_list_level", "all")
        await _safe_edit(query, _build_user_text(db_uid, risk, logs),
                         _build_user_kb(db_uid, risk, level))
        return

    # ── fds:logs:<db_uid>:<page> ──────────────────────────────────────────────
    if action == "logs":
        try:
            db_uid = int(arg1)
            page   = int(arg2) if arg2 else 0
        except ValueError:
            return
        logs     = get_user_logs(db_uid, _LOG_PAGE + 1, page * _LOG_PAGE)
        has_more = len(logs) > _LOG_PAGE
        logs     = logs[:_LOG_PAGE]
        await _safe_edit(query, _build_logs_text(db_uid, logs, page),
                         _build_logs_kb(db_uid, page, has_more))
        return

    # ── fds:settings ──────────────────────────────────────────────────────────
    if action == "settings":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ manage_settings permission required.", show_alert=True)
            return
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── fds:setstatus:<value> ─────────────────────────────────────────────────
    if action == "setstatus":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        if arg1 in ("enabled", "maintenance", "disabled"):
            cfg.set("fds_status", arg1)
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── fds:toggle:<key> ──────────────────────────────────────────────────────
    if action == "toggle":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        valid_keys = {k for k, _ in _BOOL_KEYS}
        if arg1 in valid_keys:
            cfg.set(arg1, not cfg.get_bool(arg1, True))
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── fds:setint:<key>:<value> ─────────────────────────────────────────────
    if action == "setint":
        if not has_permission(admin_tg_id, "manage_settings"):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        valid_keys = {k for k, *_ in _INT_KEYS}
        key = arg1
        if key in valid_keys:
            try:
                cfg.set(key, int(arg2))
                await query.answer(f"✅ {key} = {arg2}", show_alert=False)
            except ValueError:
                pass
        await _safe_edit(query, _build_settings_text(), _build_settings_kb())
        return

    # ── fds:noop ──────────────────────────────────────────────────────────────
    if action == "noop":
        return

    # Unknown — fall back to home
    stats = get_fraud_stats()
    await _safe_edit(query, _build_home_text(stats), _build_home_kb())


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    application.add_handler(
        CallbackQueryHandler(fds_dispatch, pattern=r"^fds:")
    )
