"""System Diagnostics Center admin panel — V34.

Callback namespace: acc:diag:*

Features:
  • Full Scan / Quick Scan
  • View Scan Results (🟢 Healthy / 🟡 Warning / 🔴 Critical)
  • Reconnect Database
  • Restart Scheduler
  • Clear Cache
  • Export Logs (send as document)
  • View Error Logs
  • Diagnostics Settings (Enable / Maintenance / Disable, auto scan, alerts)
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

# ── Feature guard ─────────────────────────────────────────────────────────

_STATUS_EMOJI = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}


def _status() -> str:
    return cfg.get("diagnostics_status", "enabled")


def _is_enabled() -> bool:
    return _status() == "enabled"


def _back_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Control Center", callback_data="acc:root")


def _back_diag_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Diagnostics", callback_data="acc:diag:menu")


async def _send(update: Update, text: str, kb: InlineKeyboardMarkup):
    q = getattr(update, "callback_query", None)
    if q:
        try:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                          disable_web_page_preview=True)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        except Exception:
            pass
        await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


def _guard_access(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return has_permission(uid, "manage_settings")


# ──────────────────────────────────────────────────────────────────────────
# Menu
# ──────────────────────────────────────────────────────────────────────────

async def diag_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main Diagnostics Center menu (acc:diag:menu)."""
    if not _guard_access(update):
        q = update.callback_query
        if q:
            await q.answer("⛔ Access denied.", show_alert=True)
        return

    status = _status()
    status_emoji = _STATUS_EMOJI.get(status, "⚪")

    from services.diagnostics import get_latest_record
    latest = get_latest_record()

    lines = [
        f"🩺 <b>SYSTEM DIAGNOSTICS CENTER</b>",
        f"Status: {status_emoji} {status.title()}",
        "",
    ]
    if latest:
        health_emoji = {"healthy": "🟢", "warning": "🟡", "critical": "🔴"}.get(
            latest.overall_health or "", "⚪")
        lines += [
            f"<b>Last Scan #{latest.id}</b>  ({latest.scan_type})",
            f"When: {(latest.completed_at or latest.started_at).strftime('%Y-%m-%d %H:%M') if (latest.completed_at or latest.started_at) else '—'}",
            f"Result: {health_emoji} {(latest.overall_health or '—').title()}",
            f"Checks: {latest.total_checks}  ·  🟢 {latest.healthy_count}  🟡 {latest.warning_count}  🔴 {latest.critical_count}",
        ]
    else:
        lines.append("No scans run yet.")

    auto = cfg.get_bool("diagnostics_auto_scan", False)
    interval = cfg.get_int("diagnostics_scan_interval_hours", 6)
    alerts = cfg.get_bool("diagnostics_admin_alerts", True)
    lines += [
        "",
        f"Auto Scan: {'✅ on' if auto else '⚪ off'}  ·  Interval: {interval}h",
        f"Admin Alerts: {'✅ on' if alerts else '⚪ off'}",
    ]

    kb = []
    if status in ("enabled", "maintenance"):
        kb.append([
            InlineKeyboardButton("🔍 Full Scan", callback_data="acc:diag:full"),
            InlineKeyboardButton("⚡ Quick Scan", callback_data="acc:diag:quick"),
        ])
        if latest:
            kb.append([
                InlineKeyboardButton("📋 View Last Results",
                                     callback_data=f"acc:diag:view:{latest.id}"),
            ])
        kb.append([
            InlineKeyboardButton("🔄 Reconnect DB", callback_data="acc:diag:reconnect_db"),
            InlineKeyboardButton("♻️ Restart Sched", callback_data="acc:diag:restart_sched"),
        ])
        kb.append([
            InlineKeyboardButton("🧹 Clear Cache", callback_data="acc:diag:clear_cache"),
            InlineKeyboardButton("📤 Export Logs", callback_data="acc:diag:export_logs"),
        ])
        kb.append([
            InlineKeyboardButton("🔴 Error Logs", callback_data="acc:diag:error_logs"),
        ])
    kb.append([InlineKeyboardButton("⚙️ Settings", callback_data="acc:diag:settings")])
    kb.append([_back_btn()])

    await _send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# Scan runners
# ──────────────────────────────────────────────────────────────────────────

def _fmt_results(results: list) -> list:
    """Format a list of result dicts or CheckResult objects into display lines."""
    lines = []
    for r in results:
        if isinstance(r, dict):
            emoji = {"healthy": "🟢", "warning": "🟡", "critical": "🔴"}.get(r.get("status", ""), "⚪")
            name = r.get("name", "?")
            detail = r.get("detail", "")
            value = r.get("value", "")
            ms = r.get("duration_ms", 0)
        else:
            emoji = r.emoji
            name = r.name
            detail = r.detail
            value = r.value
            ms = r.duration_ms
        val_part = f"  <code>{value}</code>" if value else ""
        ms_part = f"  <i>{ms:.0f}ms</i>" if ms else ""
        lines.append(f"{emoji} <b>{name}</b>{val_part}{ms_part}")
        if detail:
            lines.append(f"    {detail[:120]}")
    return lines


async def _run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    scan_type: str = "full"):
    if not _guard_access(update):
        return
    if not _is_enabled():
        await _send(update, "🔴 Diagnostics is currently disabled.",
                    InlineKeyboardMarkup([[_back_btn()]]))
        return

    q = update.callback_query
    admin_id = update.effective_user.id

    # Show progress
    try:
        await q.edit_message_text(
            f"⏳ Running <b>{'Full' if scan_type == 'full' else 'Quick'} Scan</b>…\n"
            "This may take a moment.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    from services.diagnostics import run_full_scan, run_quick_scan

    jq = getattr(context, "job_queue", None)
    if scan_type == "full":
        rec, check_results = run_full_scan(admin_id=admin_id,
                                           triggered_by="manual",
                                           job_queue=jq)
    else:
        rec, check_results = run_quick_scan(admin_id=admin_id,
                                            triggered_by="manual",
                                            job_queue=jq)

    try:
        log_admin_action(admin_id, f"diagnostics_{scan_type}_scan",
                         f"record_id={rec.id if rec else '?'}")
    except Exception:
        pass

    # Alert admin if critical issues and alerts enabled
    if rec and rec.critical_count > 0 and cfg.get_bool("diagnostics_admin_alerts", True):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🔴 <b>Diagnostics Alert</b>\n"
                     f"Critical issues detected: {rec.critical_count}\n"
                     f"Scan #{rec.id} — run /admin to review.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    if rec:
        await _show_scan_result(update, rec.id, check_results)
    else:
        await _send(update, "❌ Scan failed — no record created.",
                    InlineKeyboardMarkup([[_back_diag_btn()]]))


async def _show_scan_result(update: Update, record_id: int, results=None):
    """Display results of a given scan record."""
    if results is None:
        from services.diagnostics import load_scan_results
        rec, results = load_scan_results(record_id)
    else:
        from services.diagnostics import load_scan_results
        rec, _ = load_scan_results(record_id)

    if not rec:
        from database.models import DiagnosticsRecord
        from database import get_db_session
        try:
            with get_db_session() as s:
                rec = s.get(DiagnosticsRecord, record_id)
        except Exception:
            pass

    if not rec:
        await _send(update, "⚠️ Scan record not found.",
                    InlineKeyboardMarkup([[_back_diag_btn()]]))
        return

    health_emoji = {"healthy": "🟢", "warning": "🟡", "critical": "🔴"}.get(
        rec.overall_health or "", "⚪")

    lines = [
        f"🩺 <b>Diagnostics Scan #{rec.id}</b>  ({rec.scan_type})",
        f"Health: {health_emoji} <b>{(rec.overall_health or '—').title()}</b>",
        f"Checks: {rec.total_checks}  ·  🟢 {rec.healthy_count}  🟡 {rec.warning_count}  🔴 {rec.critical_count}",
        f"When: {(rec.completed_at or rec.started_at).strftime('%Y-%m-%d %H:%M') if (rec.completed_at or rec.started_at) else '—'}",
        "",
    ]
    lines += _fmt_results(results)

    kb = [
        [InlineKeyboardButton("🔍 Run Full Scan", callback_data="acc:diag:full"),
         InlineKeyboardButton("⚡ Quick Scan", callback_data="acc:diag:quick")],
        [_back_diag_btn()],
    ]

    # Telegram message limit is 4096 chars; truncate gracefully
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n… (truncated)"

    await _send(update, text, InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────

async def _reconnect_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    try:
        from database.db import engine
        from sqlalchemy import text
        engine.dispose()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        msg = "✅ Database reconnected successfully."
    except Exception as e:
        msg = f"❌ Reconnect failed: {str(e)[:100]}"
    try:
        log_admin_action(update.effective_user.id, "diagnostics_reconnect_db", msg[:100])
    except Exception:
        pass
    await _send(update, msg, InlineKeyboardMarkup([[_back_diag_btn()]]))


async def _restart_scheduler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    try:
        jq = getattr(context, "job_queue", None)
        if jq is None:
            msg = "⚠️ No job queue accessible — cannot restart scheduler."
        else:
            jobs = list(jq.jobs()) if hasattr(jq, "jobs") else []
            msg = f"♻️ Scheduler info: {len(jobs)} job(s) registered. (Python-telegram-bot job queue cannot be fully restarted without process restart.)"
    except Exception as e:
        msg = f"❌ Error: {str(e)[:80]}"
    try:
        log_admin_action(update.effective_user.id, "diagnostics_restart_scheduler")
    except Exception:
        pass
    await _send(update, msg, InlineKeyboardMarkup([[_back_diag_btn()]]))


async def _clear_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    from services.diagnostics import clear_all_caches
    cleared = clear_all_caches()
    msg = f"🧹 Caches cleared: {', '.join(cleared) or '(none found)'}"
    try:
        log_admin_action(update.effective_user.id, "diagnostics_clear_cache",
                         f"cleared={cleared}")
    except Exception:
        pass
    await _send(update, msg, InlineKeyboardMarkup([[_back_diag_btn()]]))


async def _export_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    from services.diagnostics import collect_recent_logs
    q = update.callback_query
    admin_id = update.effective_user.id

    try:
        await q.edit_message_text("⏳ Collecting logs…", parse_mode="HTML")
    except Exception:
        pass

    log_text = collect_recent_logs(lines=300)
    fname = f"logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    doc = InputFile(io.BytesIO(log_text.encode("utf-8", errors="replace")), filename=fname)

    try:
        await context.bot.send_document(
            chat_id=admin_id,
            document=doc,
            caption="📋 Recent application logs (last 300 lines)",
        )
        result_msg = "✅ Logs sent as file."
    except Exception as e:
        result_msg = f"❌ Failed to send logs: {str(e)[:80]}"

    try:
        log_admin_action(admin_id, "diagnostics_export_logs")
    except Exception:
        pass

    await _send(update, result_msg, InlineKeyboardMarkup([[_back_diag_btn()]]))


async def _error_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    from services.diagnostics import collect_error_logs
    error_text = collect_error_logs(lines=50)
    # Truncate to fit in message
    if len(error_text) > 3800:
        error_text = error_text[-3800:]
    text = f"🔴 <b>Recent Error Logs</b>\n\n<code>{error_text[:3700]}</code>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Export Full Logs", callback_data="acc:diag:export_logs")],
        [_back_diag_btn()],
    ])
    await _send(update, text, kb)


# ──────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────

async def _diag_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _guard_access(update):
        return
    status = _status()
    auto = cfg.get_bool("diagnostics_auto_scan", False)
    interval = cfg.get_int("diagnostics_scan_interval_hours", 6)
    alerts = cfg.get_bool("diagnostics_admin_alerts", True)

    lines = [
        "⚙️ <b>Diagnostics Settings</b>",
        "",
        f"Status: {_STATUS_EMOJI.get(status, '⚪')} {status.title()}",
        f"Auto Scan: {'✅' if auto else '⚪'}",
        f"Scan Interval: {interval}h",
        f"Admin Alerts: {'✅' if alerts else '⚪'}",
    ]

    kb = [
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="acc:diag:set_status:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="acc:diag:set_status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="acc:diag:set_status:disabled"),
        ],
        [
            InlineKeyboardButton(
                "🔄 Auto Scan: " + ("ON ✅" if auto else "OFF ⚪"),
                callback_data="acc:diag:toggle_auto",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔔 Alerts: " + ("ON ✅" if alerts else "OFF ⚪"),
                callback_data="acc:diag:toggle_alerts",
            ),
        ],
    ]
    # Interval presets
    kb.append([
        InlineKeyboardButton(f"{'✔ ' if interval == 1 else ''}1h",
                             callback_data="acc:diag:set_interval:1"),
        InlineKeyboardButton(f"{'✔ ' if interval == 3 else ''}3h",
                             callback_data="acc:diag:set_interval:3"),
        InlineKeyboardButton(f"{'✔ ' if interval == 6 else ''}6h",
                             callback_data="acc:diag:set_interval:6"),
        InlineKeyboardButton(f"{'✔ ' if interval == 12 else ''}12h",
                             callback_data="acc:diag:set_interval:12"),
        InlineKeyboardButton(f"{'✔ ' if interval == 24 else ''}24h",
                             callback_data="acc:diag:set_interval:24"),
    ])
    kb.append([_back_diag_btn()])
    await _send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ──────────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────────

async def diag_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all acc:diag:* callbacks."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if not _guard_access(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data or ""
    # acc:diag:<action>[:<arg>]
    parts = data.split(":", 3)
    # parts[0] = acc, parts[1] = diag, parts[2] = action, parts[3] = arg (optional)
    action = parts[2] if len(parts) > 2 else "menu"
    arg = parts[3] if len(parts) > 3 else ""

    if action == "menu" or action == "":
        await diag_menu(update, context)

    elif action == "full":
        await _run_scan(update, context, scan_type="full")

    elif action == "quick":
        await _run_scan(update, context, scan_type="quick")

    elif action == "view":
        try:
            rec_id = int(arg)
        except (ValueError, TypeError):
            await diag_menu(update, context)
            return
        await _show_scan_result(update, rec_id)

    elif action == "reconnect_db":
        await _reconnect_db(update, context)

    elif action == "restart_sched":
        await _restart_scheduler(update, context)

    elif action == "clear_cache":
        await _clear_cache(update, context)

    elif action == "export_logs":
        await _export_logs(update, context)

    elif action == "error_logs":
        await _error_logs(update, context)

    elif action == "settings":
        await _diag_settings(update, context)

    elif action == "set_status":
        new_status = arg if arg in ("enabled", "maintenance", "disabled") else "enabled"
        cfg.set("diagnostics_status", new_status)
        try:
            log_admin_action(update.effective_user.id, "diagnostics_status_changed",
                             f"status={new_status}")
        except Exception:
            pass
        await _diag_settings(update, context)

    elif action == "toggle_auto":
        new_val = not cfg.get_bool("diagnostics_auto_scan", False)
        cfg.set("diagnostics_auto_scan", "true" if new_val else "false")
        await _diag_settings(update, context)

    elif action == "toggle_alerts":
        new_val = not cfg.get_bool("diagnostics_admin_alerts", True)
        cfg.set("diagnostics_admin_alerts", "true" if new_val else "false")
        await _diag_settings(update, context)

    elif action == "set_interval":
        try:
            hours = max(1, min(168, int(arg)))
        except (ValueError, TypeError):
            hours = 6
        cfg.set("diagnostics_scan_interval_hours", str(hours))
        await _diag_settings(update, context)

    else:
        await diag_menu(update, context)


# ──────────────────────────────────────────────────────────────────────────
# Auto-scan job (called by scheduler)
# ──────────────────────────────────────────────────────────────────────────

async def diagnostics_auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduler job: run quick diagnostics scan if auto scan is enabled."""
    try:
        if not cfg.get_bool("diagnostics_auto_scan", False):
            return
        if _status() != "enabled":
            return

        from services.diagnostics import run_quick_scan
        jq = getattr(context, "job_queue", None)
        rec, results = run_quick_scan(triggered_by="auto", job_queue=jq)

        # Alert admin if critical issues found
        if rec and rec.critical_count > 0 and cfg.get_bool("diagnostics_admin_alerts", True):
            from config.settings import settings
            admin_id = settings.ADMIN_TELEGRAM_ID
            if admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🔴 <b>Auto Diagnostics Alert</b>\n"
                            f"Critical issues detected: {rec.critical_count}\n"
                            f"Warning: {rec.warning_count}\n"
                            f"Scan #{rec.id} — open Admin Panel › Diagnostics to review."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception:
        logger.exception("diagnostics_auto_scan_job failed")
