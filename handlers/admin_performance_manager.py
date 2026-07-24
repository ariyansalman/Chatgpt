"""V44 — Performance & Cache Manager admin handler.

Callback namespace: pcm:*
All admin actions require the 'admin' permission.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest

from services.performance_cache_service import (
    collect_metrics, compute_health, get_uptime_str,
    get_cache_namespaces, clear_cache, clear_all_caches,
    optimize_database, optimize_logs, optimize_storage, optimize_cache,
    optimize_search_index, optimize_background_jobs, optimize_images,
    optimize_scheduler, run_full_optimization,
    take_snapshot, get_snapshot_history, get_optimization_history,
    generate_report, get_stats,
)
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

PAGE_SIZE = 8

# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _check_perm(update: Update) -> bool:
    uid = update.effective_user.id
    if not has_permission(uid, "admin"):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest:
        pass


def _back(to: str = "pcm:menu") -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=to)


def _fmt_ms(ms: float) -> str:
    if ms < 0:
        return "❌ Error"
    return f"{ms:.0f} ms"


def _bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _health_color(score: int) -> str:
    if score >= 90:
        return "🟢"
    if score >= 70:
        return "🟡"
    if score >= 50:
        return "🟠"
    return "🔴"


def _pct_emoji(pct: float, warn: float = 70, crit: float = 90) -> str:
    if pct >= crit:
        return "🔴"
    if pct >= warn:
        return "🟡"
    return "🟢"


def _enabled() -> bool:
    return cfg.get("pcm_status", "enabled") != "disabled"


def _maintenance() -> bool:
    return cfg.get("pcm_status", "enabled") == "maintenance"


# ─── Main menu ────────────────────────────────────────────────────────────────

async def pcm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    status = cfg.get("pcm_status", "enabled")
    if status == "maintenance":
        await _edit(update, "⚡ <b>Performance Manager</b>\n\n🟡 <b>Under maintenance.</b>",
                    InlineKeyboardMarkup([[_back("acc:root")]]))
        return

    s = get_stats()
    h_emoji = s.get("emoji", "❓")
    score = s.get("score", 0)
    text = (
        "⚡ <b>PERFORMANCE & CACHE MANAGER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Health: {h_emoji} <b>{s.get('label','—')}</b> ({score}/100)\n"
        f"🖥 CPU: {_pct_emoji(s.get('cpu_pct',0))} {s.get('cpu_pct',0)}%   "
        f"💾 RAM: {_pct_emoji(s.get('mem_pct',0))} {s.get('mem_pct',0)}%   "
        f"💿 Disk: {_pct_emoji(s.get('disk_pct',0))} {s.get('disk_pct',0)}%\n"
        f"🗄 DB: {_fmt_ms(s.get('db_ping_ms',-1))}   "
        f"⏱ Uptime: {s.get('uptime_str','—')}\n"
        f"🔧 Optimizations today: {s.get('opt_today',0)}"
    )
    if s.get("issues"):
        text += "\n⚠️ <i>" + " | ".join(s["issues"]) + "</i>"

    kb = [
        [InlineKeyboardButton("📊 Live Stats",        callback_data="pcm:live"),
         InlineKeyboardButton("❤️ Health",            callback_data="pcm:health")],
        [InlineKeyboardButton("🧹 Cache Manager",     callback_data="pcm:cache"),
         InlineKeyboardButton("⚡ Optimize",          callback_data="pcm:optimize")],
        [InlineKeyboardButton("📋 Reports",           callback_data="pcm:reports"),
         InlineKeyboardButton("🕐 History",           callback_data="pcm:history:0")],
        [InlineKeyboardButton("🔬 Quick Scan",        callback_data="pcm:scan:quick"),
         InlineKeyboardButton("🔭 Full Scan",         callback_data="pcm:scan:full")],
        [InlineKeyboardButton("⚙️ Settings",          callback_data="pcm:settings")],
        [_back("acc:root")],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Live stats ───────────────────────────────────────────────────────────────

async def pcm_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🔄 Refreshing…")
    if not await _check_perm(update):
        return

    m = collect_metrics()
    h = compute_health(m)

    cpu_bar  = _bar(m["cpu_pct"])
    mem_bar  = _bar(m["mem_pct"])
    disk_bar = _bar(m["disk_pct"])

    text = (
        "📊 <b>LIVE PERFORMANCE STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥 <b>CPU</b>\n"
        f"  Usage:    {m['cpu_pct']}%  {cpu_bar}\n"
        f"  Load 1m:  {m['cpu_load1']}  "
        f"5m: {m['cpu_load5']}  "
        f"15m: {m['cpu_load15']}\n"
        f"  Cores:    {m['cpu_count']}\n\n"
        f"💾 <b>Memory (RAM)</b>\n"
        f"  Used:     {m['mem_used_mb']} MB / {m['mem_total_mb']} MB\n"
        f"  Free:     {m['mem_avail_mb']} MB ({100-m['mem_pct']}%)\n"
        f"  Usage:    {m['mem_pct']}%  {mem_bar}\n\n"
        f"💿 <b>Disk</b>\n"
        f"  Free:     {m['disk_free_gb']} GB / {m['disk_total_gb']} GB\n"
        f"  Used:     {m['disk_pct']}%  {disk_bar}\n"
        f"  Tmp:      {m['tmp_size_mb']} MB\n\n"
        f"🗄 <b>Database</b>\n"
        f"  Ping:     {_fmt_ms(m['db_ping_ms'])}\n"
        f"  Size:     {m['db_size_mb']} MB\n"
        f"  Conns:    {m['db_conn']}\n\n"
        f"⏱ <b>Uptime:</b> {m['uptime_str']}\n"
        f"🕐 Snapshot: {m['collected_at'][:19]}"
    )
    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="pcm:live"),
         InlineKeyboardButton("📸 Save Snapshot", callback_data="pcm:snapshot")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Health dashboard ─────────────────────────────────────────────────────────

async def pcm_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    m = collect_metrics()
    h = compute_health(m)

    def _check_line(label: str, pct: float, warn: float, crit: float) -> str:
        emoji = _pct_emoji(pct, warn, crit)
        return f"  {emoji} {label}: {pct}%\n"

    db_status = "🟢 Connected" if m["db_ping_ms"] >= 0 else "🔴 Error"
    db_speed = ""
    if m["db_ping_ms"] >= 0:
        db_speed = f" ({_fmt_ms(m['db_ping_ms'])})"
        if m["db_ping_ms"] > 2000:
            db_status = "🔴 Very Slow"
        elif m["db_ping_ms"] > 500:
            db_status = "🟡 Slow"

    text = (
        f"❤️ <b>SYSTEM HEALTH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Overall: {h['emoji']} <b>{h['label']}</b> — Score: {h['score']}/100\n\n"
        "<b>Component Status:</b>\n"
        + _check_line("CPU",    m["cpu_pct"],  70, 90)
        + _check_line("Memory", m["mem_pct"],  80, 92)
        + _check_line("Disk",   m["disk_pct"], 80, 95)
        + f"  {db_status} Database{db_speed}\n"
        + f"  🟢 Uptime: {m['uptime_str']}\n\n"
    )

    if h["issues"]:
        text += "⚠️ <b>Active Issues:</b>\n"
        for issue in h["issues"]:
            text += f"  • {issue}\n"
    else:
        text += "✅ <b>No issues detected.</b>\n"

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="pcm:health"),
         InlineKeyboardButton("⚡ Fix All", callback_data="pcm:full_optim")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Cache manager ────────────────────────────────────────────────────────────

async def pcm_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    namespaces = get_cache_namespaces()
    text = (
        "🧹 <b>CACHE MANAGER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Available cache namespaces: <b>{len(namespaces)}</b>\n\n"
        "Select a cache to clear, or clear all at once:"
    )
    kb = []
    for ns, label in namespaces.items():
        kb.append([InlineKeyboardButton(f"🗑 Clear {label}",
                                         callback_data=f"pcm:cache_clear:{ns}")])
    kb.append([InlineKeyboardButton("🗑 Clear ALL Caches", callback_data="pcm:cache_clear:all")])
    kb.append([InlineKeyboardButton("⚡ Optimize Cache",   callback_data="pcm:optim:cache")])
    kb.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def pcm_cache_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🧹 Clearing…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:cache_clear:{namespace}
    namespace = parts[2]
    admin_id = update.effective_user.id

    if namespace == "all":
        result = clear_all_caches()
        total = result["total_cleared"]
        msg = f"✅ All caches cleared — {total} item(s) removed."
    else:
        result = clear_cache(namespace)
        if result["ok"]:
            msg = f"✅ {result['label']} cleared — {result['cleared']} item(s)."
        else:
            msg = f"❌ Failed to clear {namespace}: {result.get('error','unknown error')}"

    try:
        log_admin_action(admin_id, "pcm_cache_clear", details=f"namespace={namespace}")
    except Exception:
        pass

    kb = [
        [InlineKeyboardButton("🧹 Back to Cache Manager", callback_data="pcm:cache")],
        [_back()],
    ]
    await _edit(update, msg, InlineKeyboardMarkup(kb))


# ─── Optimization tools ───────────────────────────────────────────────────────

async def pcm_optimize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    text = (
        "⚡ <b>OPTIMIZATION TOOLS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select an optimization to run:"
    )
    kb = [
        [InlineKeyboardButton("🗄 Optimize Database",      callback_data="pcm:optim:database"),
         InlineKeyboardButton("🧹 Optimize Cache",         callback_data="pcm:optim:cache")],
        [InlineKeyboardButton("🖼 Optimize Images",        callback_data="pcm:optim:images"),
         InlineKeyboardButton("💿 Optimize Storage",       callback_data="pcm:optim:storage")],
        [InlineKeyboardButton("📋 Optimize Logs",          callback_data="pcm:optim:logs"),
         InlineKeyboardButton("🔍 Optimize Search Index",  callback_data="pcm:optim:search")],
        [InlineKeyboardButton("⚙️ Optimize Background Jobs", callback_data="pcm:optim:jobs"),
         InlineKeyboardButton("🕐 Optimize Scheduler",    callback_data="pcm:optim:scheduler")],
        [InlineKeyboardButton("🚀 Run Full Optimization",  callback_data="pcm:full_optim")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


_OPTIM_DISPATCH = {
    "database":  (optimize_database,          "🗄 Database Optimization"),
    "cache":     (optimize_cache,             "🧹 Cache Optimization"),
    "images":    (optimize_images,            "🖼 Image Optimization"),
    "storage":   (optimize_storage,           "💿 Storage Optimization"),
    "logs":      (optimize_logs,              "📋 Log Cleanup"),
    "search":    (optimize_search_index,      "🔍 Search Index Optimization"),
    "jobs":      (optimize_background_jobs,   "⚙️ Background Job Cleanup"),
    "scheduler": (optimize_scheduler,         "🕐 Scheduler Cleanup"),
}


async def pcm_optim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⚡ Running…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:optim:{tool}
    tool = parts[2]
    admin_id = update.effective_user.id

    if tool not in _OPTIM_DISPATCH:
        await q.answer("❓ Unknown tool.", show_alert=True)
        return

    fn, label = _OPTIM_DISPATCH[tool]
    result = fn()

    ok = result.get("ok", False)
    msg_body = result.get("msg", "Done.")
    elapsed = result.get("elapsed_ms", "")
    elapsed_str = f" ({elapsed}ms)" if elapsed else ""

    text = (
        f"{'✅' if ok else '❌'} <b>{label}</b>{elapsed_str}\n\n"
        f"{msg_body}"
    )
    try:
        log_admin_action(admin_id, f"pcm_optim_{tool}", details=msg_body[:200])
    except Exception:
        pass

    kb = [
        [InlineKeyboardButton("⚡ More Tools",    callback_data="pcm:optimize"),
         InlineKeyboardButton("📊 View Stats",   callback_data="pcm:live")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def pcm_full_optim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("🚀 Running full optimization…")
    if not await _check_perm(update):
        return

    admin_id = update.effective_user.id
    result = run_full_optimization()

    lines = []
    for name, r in result.get("results", {}).items():
        icon = "✅" if r.get("ok") else "❌"
        lines.append(f"  {icon} {name}: {r.get('msg','')[:60]}")

    ok_c = result.get("ok_count", 0)
    total_c = result.get("total", 0)
    text = (
        f"🚀 <b>Full Optimization Complete</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ {ok_c}/{total_c} tasks completed\n\n"
        + "\n".join(lines)
    )
    try:
        log_admin_action(admin_id, "pcm_full_optim",
                         details=f"{ok_c}/{total_c} ok")
    except Exception:
        pass

    await _edit(update, text,
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Live Stats", callback_data="pcm:live")],
                    [_back()],
                ]))


# ─── Scan ─────────────────────────────────────────────────────────────────────

async def pcm_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split(":")          # pcm:scan:{mode}
    mode = parts[2] if len(parts) > 2 else "quick"

    await q.answer(f"🔬 {'Quick' if mode == 'quick' else 'Full'} scan running…")
    if not await _check_perm(update):
        return

    m = collect_metrics()
    h = compute_health(m)
    take_snapshot()

    if mode == "full":
        # Also run full optimization
        await pcm_full_optim(update, context)
        return

    # Quick scan: just show health + metrics
    text = (
        f"🔬 <b>Quick Scan Complete</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Health: {h['emoji']} {h['label']} ({h['score']}/100)\n\n"
        f"🖥 CPU:   {m['cpu_pct']}%\n"
        f"💾 RAM:   {m['mem_pct']}%\n"
        f"💿 Disk:  {m['disk_pct']}%\n"
        f"🗄 DB:    {_fmt_ms(m['db_ping_ms'])}\n"
        f"⏱ Up:    {m['uptime_str']}\n"
    )
    if h["issues"]:
        text += "\n⚠️ Issues: " + " | ".join(h["issues"])
    else:
        text += "\n✅ No issues detected."

    kb = [
        [InlineKeyboardButton("📊 Full Stats",    callback_data="pcm:live"),
         InlineKeyboardButton("🔭 Full Scan",     callback_data="pcm:scan:full")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Snapshot ────────────────────────────────────────────────────────────────

async def pcm_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("📸 Saving snapshot…")
    if not await _check_perm(update):
        return

    take_snapshot()
    await _edit(update, "✅ Performance snapshot saved.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🕐 View History", callback_data="pcm:history:0")],
                    [_back()],
                ]))


# ─── History ──────────────────────────────────────────────────────────────────

async def pcm_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:history:{page}
    page = int(parts[2]) if len(parts) > 2 else 0

    snapshots = get_snapshot_history(limit=200)
    total = len(snapshots)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    slice_ = snapshots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = f"🕐 <b>Performance History</b> — Page {page+1}/{pages}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for snap in slice_:
        ts = snap["created_at"].strftime("%m-%d %H:%M") if snap["created_at"] else "—"
        h_emoji = _health_color(snap["health_score"] or 0)
        text += (f"{h_emoji} {ts} — CPU {snap['cpu_pct']}% | "
                 f"Mem {snap['mem_pct']}% | "
                 f"DB {_fmt_ms(snap['db_ping_ms'] or -1)}\n")

    if not slice_:
        text += "No snapshots yet. Run a scan first."

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pcm:history:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="pcm:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pcm:history:{page+1}"))

    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("📋 Optim. History", callback_data="pcm:optim_hist:0")])
    kb.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def pcm_optim_hist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:optim_hist:{page}
    page = int(parts[2]) if len(parts) > 2 else 0

    history = get_optimization_history(limit=200)
    total = len(history)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    slice_ = history[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = f"📋 <b>Optimization History</b> — Page {page+1}/{pages}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for r in slice_:
        ts = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "—"
        icon = "✅" if r["result"] == "success" else "❌"
        text += (f"{icon} {ts} {r['op_type']} → {r['target']} "
                 f"({r['duration_ms']}ms)\n")
    if not slice_:
        text += "No optimization records yet."

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pcm:optim_hist:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="pcm:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pcm:optim_hist:{page+1}"))

    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("📸 Perf. History", callback_data="pcm:history:0")])
    kb.append([_back()])
    await _edit(update, text, InlineKeyboardMarkup(kb))


# ─── Reports ──────────────────────────────────────────────────────────────────

async def pcm_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    text = (
        "📋 <b>PERFORMANCE REPORTS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Generate a report:"
    )
    kb = [
        [InlineKeyboardButton("📊 Performance",      callback_data="pcm:report:performance"),
         InlineKeyboardButton("🗄 Database",         callback_data="pcm:report:database")],
        [InlineKeyboardButton("🧹 Cache",            callback_data="pcm:report:cache"),
         InlineKeyboardButton("💾 Memory",           callback_data="pcm:report:memory")],
        [InlineKeyboardButton("💿 Storage",          callback_data="pcm:report:storage"),
         InlineKeyboardButton("⏱ Response Time",    callback_data="pcm:report:response_time")],
        [InlineKeyboardButton("🔌 API Performance",  callback_data="pcm:report:api")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def pcm_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("📋 Generating…")
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:report:{type}
    report_type = parts[2]
    report_text = generate_report(report_type)

    # Truncate to Telegram message limit
    if len(report_text) > 3800:
        report_text = report_text[:3800] + "\n…(truncated)"

    kb = [
        [InlineKeyboardButton("📋 More Reports", callback_data="pcm:reports")],
        [_back()],
    ]
    await _edit(update, f"<pre>{report_text}</pre>", InlineKeyboardMarkup(kb))


# ─── Settings ────────────────────────────────────────────────────────────────

async def pcm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    def _b(key: str, default: str = "true") -> str:
        return "✅" if cfg.get(key, default) == "true" else "○"

    status = cfg.get("pcm_status", "enabled")
    interval = cfg.get_int("pcm_snapshot_interval_min", 15)
    log_days = cfg.get_int("pcm_log_retention_days", 90)

    text = (
        "⚙️ <b>Performance Manager Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status:               <b>{status.upper()}</b>\n"
        f"Snapshot interval:    <b>{interval} min</b>\n"
        f"Log retention:        <b>{log_days} days</b>\n\n"
        f"Auto cache cleanup:   {_b('pcm_auto_cache_cleanup')} Auto cache\n"
        f"Auto log cleanup:     {_b('pcm_auto_log_cleanup')} Auto logs\n"
        f"Auto storage cleanup: {_b('pcm_auto_storage_cleanup')} Auto storage\n"
        f"Auto job cleanup:     {_b('pcm_auto_job_cleanup')} Auto jobs\n"
        f"Auto snapshots:       {_b('pcm_auto_snapshot')} Auto snapshots\n"
        f"Performance alerts:   {_b('pcm_alerts_enabled')} Alerts\n"
    )

    def _s(k: str, v: str) -> str:
        return "✅" if status == v else "○"

    kb = [
        [InlineKeyboardButton(f"{_s(status,'enabled')} 🟢 Enable",
                              callback_data="pcm:set:pcm_status:enabled"),
         InlineKeyboardButton(f"{_s(status,'maintenance')} 🟡 Maintenance",
                              callback_data="pcm:set:pcm_status:maintenance"),
         InlineKeyboardButton(f"{_s(status,'disabled')} 🔴 Disable",
                              callback_data="pcm:set:pcm_status:disabled")],
        [InlineKeyboardButton(f"{_b('pcm_auto_cache_cleanup')} Auto Cache",
                              callback_data="pcm:toggle:pcm_auto_cache_cleanup"),
         InlineKeyboardButton(f"{_b('pcm_auto_log_cleanup')} Auto Logs",
                              callback_data="pcm:toggle:pcm_auto_log_cleanup")],
        [InlineKeyboardButton(f"{_b('pcm_auto_storage_cleanup')} Auto Storage",
                              callback_data="pcm:toggle:pcm_auto_storage_cleanup"),
         InlineKeyboardButton(f"{_b('pcm_auto_snapshot')} Snapshots",
                              callback_data="pcm:toggle:pcm_auto_snapshot")],
        [InlineKeyboardButton(f"{_b('pcm_alerts_enabled')} Perf Alerts",
                              callback_data="pcm:toggle:pcm_alerts_enabled"),
         InlineKeyboardButton(f"{_b('pcm_auto_job_cleanup')} Auto Jobs",
                              callback_data="pcm:toggle:pcm_auto_job_cleanup")],
        [_back()],
    ]
    await _edit(update, text, InlineKeyboardMarkup(kb))


async def pcm_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:set:{key}:{value}
    key, value = parts[2], parts[3]
    cfg.set(key, value)
    try:
        log_admin_action(update.effective_user.id, "pcm_settings",
                         details=f"{key}={value}")
    except Exception:
        pass
    await pcm_settings(update, context)


async def pcm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not await _check_perm(update):
        return

    parts = q.data.split(":")          # pcm:toggle:{key}
    key = parts[2]
    current = cfg.get(key, "true") == "true"
    cfg.set(key, "false" if current else "true")
    try:
        log_admin_action(update.effective_user.id, "pcm_settings",
                         details=f"{key}={'false' if current else 'true'}")
    except Exception:
        pass
    await pcm_settings(update, context)


# ─── No-op ───────────────────────────────────────────────────────────────────

async def pcm_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ─── Handler registration ─────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all Performance & Cache Manager handlers. Called from bot.py main()."""
    from services.performance_cache_service import set_start_time
    set_start_time()

    application.add_handler(CallbackQueryHandler(pcm_menu,         pattern=r"^pcm:menu$"))
    application.add_handler(CallbackQueryHandler(pcm_live,         pattern=r"^pcm:live$"))
    application.add_handler(CallbackQueryHandler(pcm_health,       pattern=r"^pcm:health$"))
    application.add_handler(CallbackQueryHandler(pcm_cache,        pattern=r"^pcm:cache$"))
    application.add_handler(CallbackQueryHandler(pcm_cache_clear,  pattern=r"^pcm:cache_clear:"))
    application.add_handler(CallbackQueryHandler(pcm_optimize,     pattern=r"^pcm:optimize$"))
    application.add_handler(CallbackQueryHandler(pcm_optim,        pattern=r"^pcm:optim:"))
    application.add_handler(CallbackQueryHandler(pcm_full_optim,   pattern=r"^pcm:full_optim$"))
    application.add_handler(CallbackQueryHandler(pcm_scan,         pattern=r"^pcm:scan:"))
    application.add_handler(CallbackQueryHandler(pcm_snapshot,     pattern=r"^pcm:snapshot$"))
    application.add_handler(CallbackQueryHandler(pcm_history,      pattern=r"^pcm:history:"))
    application.add_handler(CallbackQueryHandler(pcm_optim_hist,   pattern=r"^pcm:optim_hist:"))
    application.add_handler(CallbackQueryHandler(pcm_reports,      pattern=r"^pcm:reports$"))
    application.add_handler(CallbackQueryHandler(pcm_report,       pattern=r"^pcm:report:"))
    application.add_handler(CallbackQueryHandler(pcm_settings,     pattern=r"^pcm:settings$"))
    application.add_handler(CallbackQueryHandler(pcm_set,          pattern=r"^pcm:set:"))
    application.add_handler(CallbackQueryHandler(pcm_toggle,       pattern=r"^pcm:toggle:"))
    application.add_handler(CallbackQueryHandler(pcm_noop,         pattern=r"^pcm:noop$"))

    logger.info("V44: Performance & Cache Manager handlers registered.")
