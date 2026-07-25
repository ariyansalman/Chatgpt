"""Admin Webhook Monitor & API Health Dashboard — V27.

Complete observability panel: real-time API health status, full webhook
history, per-webhook detail, retry queue, search/filter, error log,
export, and settings — all inside the existing admin panel.

Callback namespace: ``awm:*``
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.error import BadRequest

from database import get_db_session
from database.models import ApiHealthLog, WebhookLog, WebhookRetryQueue
from services.health_monitor import (
    SERVICES, STATUS_ICONS, get_latest_statuses, get_service_history,
)
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 8
PROVIDER_LABELS = {
    "telegram":      "Telegram Bot API",
    "nowpayments":   "NOWPayments",
    "binance":       "Binance Pay",
    "bybit":         "Bybit Pay",
    "trc20":         "USDT TRC20",
    "bep20":         "USDT BEP20",
    "erc20":         "USDT ERC20",
    "mobile_banking":"Mobile Banking",
    "database":      "PostgreSQL",
}
STATUS_ICONS_WH = {
    "received":  "📥",
    "processed": "✅",
    "failed":    "❌",
    "duplicate": "🔁",
    "ignored":   "⏭",
}

# ── Auth helper ────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return (uid == settings.ADMIN_TELEGRAM_ID
            or has_permission(uid, "manage_webhooks"))


# ── Keyboard / text helpers ────────────────────────────────────────────────

async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back(data: str = "awm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


# ── Main dashboard ─────────────────────────────────────────────────────────

async def awm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    monitor_status = cfg.get("webhook_monitor_status", "enabled")
    if monitor_status != "enabled":
        icon = "🟡" if monitor_status == "maintenance" else "🔴"
        await _safe_edit(query,
            f"🔌 <b>Webhook Monitor</b>\n\n"
            f"{icon} Status: <b>{monitor_status.capitalize()}</b>",
            _back("acc:root"))
        return

    # Counts
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    with get_db_session() as s:
        total_wh     = s.query(WebhookLog).count()
        today_wh     = s.query(WebhookLog).filter(WebhookLog.received_at >= today_start).count()
        failed_wh    = s.query(WebhookLog).filter_by(status="failed").count()
        processed_wh = s.query(WebhookLog).filter_by(status="processed").count()
        pending_ret  = s.query(WebhookRetryQueue).filter_by(status="pending").count()
        from sqlalchemy import func
        avg_ms_row = s.query(func.avg(WebhookLog.processing_time_ms)).scalar()
        avg_ms = int(avg_ms_row) if avg_ms_row else 0

    # Service health summary
    statuses = get_latest_statuses()
    online_cnt  = sum(1 for r in statuses if r["status"] == "online")
    offline_cnt = sum(1 for r in statuses if r["status"] == "offline")
    total_svcs  = len(SERVICES)

    text = (
        "🔌 <b>Webhook Monitor & API Health</b>\n\n"
        f"<b>API Services:</b>  {total_svcs} total  |  "
        f"🟢 {online_cnt} online  |  🔴 {offline_cnt} offline\n\n"
        f"<b>Webhooks (all-time):</b>  {total_wh}\n"
        f"<b>Today:</b>  {today_wh}  |  "
        f"<b>✅ Processed:</b> {processed_wh}  |  <b>❌ Failed:</b> {failed_wh}\n"
        f"<b>⏱ Avg Processing:</b> {avg_ms} ms  |  "
        f"<b>🔄 Pending Retries:</b> {pending_ret}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🩺 API Health", callback_data="awm:health"),
            InlineKeyboardButton("📋 Webhook History", callback_data="awm:list:0"),
        ],
        [
            InlineKeyboardButton("🔴 Failed Only", callback_data="awm:filter:status:failed"),
            InlineKeyboardButton("🔄 Retry Queue", callback_data="awm:retries:0"),
        ],
        [
            InlineKeyboardButton("🔍 Search", callback_data="awm:search"),
            InlineKeyboardButton("📊 Error Logs", callback_data="awm:logs"),
        ],
        [
            InlineKeyboardButton("🗑 Clear Old Logs", callback_data="awm:clear"),
            InlineKeyboardButton("📥 Export CSV", callback_data="awm:export"),
        ],
        [
            InlineKeyboardButton("🔁 Refresh Now", callback_data="awm:refresh"),
            InlineKeyboardButton("⚙️ Settings", callback_data="awm:settings"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ])
    await _safe_edit(query, text, kb)


# ── API Health dashboard ───────────────────────────────────────────────────

async def awm_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    statuses = get_latest_statuses()
    lines = ["🩺 <b>API Health Status</b>\n"]
    kb_rows = []
    for r in statuses:
        icon = STATUS_ICONS.get(r["status"], "❓")
        ms_str = f"{r['ms']}ms" if r["ms"] is not None else "—"
        at_str = r["at"].strftime("%m/%d %H:%M") if r["at"] else "never"
        lines.append(
            f"{icon} <b>{r['label']}</b> — {r['status'].upper()}  "
            f"({ms_str}, checked {at_str})"
        )
        if r["error"]:
            lines.append(f"   ⚠️ {r['error'][:80]}")
        kb_rows.append([InlineKeyboardButton(
            f"{icon} {r['label'][:30]}",
            callback_data=f"awm:health:{r['key']}")])

    kb_rows.append([
        InlineKeyboardButton("🔁 Refresh Now", callback_data="awm:refresh"),
        InlineKeyboardButton("🔙 Back", callback_data="awm:menu"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


async def awm_health_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detail view for one service: last 10 check results."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        key = query.data.split(":")[2]
    except IndexError:
        return

    svc = SERVICES.get(key)
    label = svc["label"] if svc else key
    history = get_service_history(key, limit=10)

    lines = [f"🩺 <b>{label}</b> — Last 10 Checks\n"]
    if not history:
        lines.append("No data yet. Run a health check first.")
    else:
        # Rolling stats
        ms_values = [r["ms"] for r in history if r["ms"] is not None]
        avg_ms = int(sum(ms_values) / len(ms_values)) if ms_values else 0
        online_pct = int(sum(1 for r in history if r["status"] == "online") / len(history) * 100)
        lines.append(f"📈 Uptime (last {len(history)}): {online_pct}%  |  Avg: {avg_ms}ms\n")
        for r in history:
            icon = STATUS_ICONS.get(r["status"], "❓")
            at   = r["at"].strftime("%m/%d %H:%M") if r["at"] else "—"
            ms   = f"{r['ms']}ms" if r["ms"] is not None else "—"
            err  = f" ⚠️{r['error'][:60]}" if r["error"] else ""
            lines.append(f"{icon} {at}  {ms}{err}")

    await _safe_edit(query, "\n".join(lines), _back("awm:health"))


# ── Webhook history list ───────────────────────────────────────────────────

async def awm_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        page = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await _render_webhook_list(query, page, provider=None, status_filter=None)


async def _render_webhook_list(query, page: int,
                                provider: Optional[str] = None,
                                status_filter: Optional[str] = None):
    with get_db_session() as s:
        q = s.query(WebhookLog).order_by(WebhookLog.received_at.desc())
        if provider:
            q = q.filter_by(provider=provider)
        if status_filter:
            q = q.filter_by(status=status_filter)
        total = q.count()
        rows  = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [
            (r.id, r.provider, r.status, r.received_at, r.processing_time_ms, r.order_id)
            for r in rows
        ]

    title = "📋 Webhook History"
    if provider:
        title += f" — {PROVIDER_LABELS.get(provider, provider)}"
    if status_filter:
        title += f" [{status_filter}]"

    lines = [f"{title}  (page {page+1}, {total} total)\n"]
    kb = []
    for wid, prov, st, recv, ms, oid in items:
        icon   = STATUS_ICONS_WH.get(st, "❓")
        at_str = recv.strftime("%m/%d %H:%M") if recv else "—"
        ms_str = f"{ms}ms" if ms else "—"
        oid_str = f" ord:{oid}" if oid else ""
        lines.append(f"{icon} #{wid} {PROVIDER_LABELS.get(prov, prov)[:12]} {at_str} {ms_str}{oid_str}")
        kb.append([InlineKeyboardButton(
            f"{icon} #{wid} {prov} {at_str}",
            callback_data=f"awm:wh:{wid}")])

    if not items:
        lines.append("No webhook events found.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"awm:list:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"awm:list:{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([
        InlineKeyboardButton("🔍 Filter Provider", callback_data="awm:filter:provider"),
        InlineKeyboardButton("🔙 Back", callback_data="awm:menu"),
    ])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Webhook detail ─────────────────────────────────────────────────────────

async def awm_webhook_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        wid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    with get_db_session() as s:
        wh = s.get(WebhookLog, wid)
        if not wh:
            await query.answer("❌ Not found.", show_alert=True)
            return
        provider   = wh.provider
        status     = wh.status
        recv       = wh.received_at
        proc_ms    = wh.processing_time_ms
        error      = wh.error_message or "—"
        retry_cnt  = wh.retry_count
        order_id   = wh.order_id
        user_id    = wh.user_id
        payment_id = wh.payment_id or "—"
        tx_id      = wh.transaction_id or "—"
        uuid_str   = wh.webhook_uuid
        pending_retries = (s.query(WebhookRetryQueue)
                           .filter_by(webhook_log_id=wid, status="pending")
                           .count())
        raw_snippet = (wh.raw_payload or "")[:400]

    icon   = STATUS_ICONS_WH.get(status, "❓")
    at_str = recv.strftime("%Y-%m-%d %H:%M:%S UTC") if recv else "—"

    text = (
        f"📋 <b>Webhook #{wid}</b>\n\n"
        f"<b>Provider:</b> {PROVIDER_LABELS.get(provider, provider)}\n"
        f"<b>Status:</b> {icon} {status}\n"
        f"<b>Received:</b> {at_str}\n"
        f"<b>Processing Time:</b> {proc_ms or '—'} ms\n"
        f"<b>Retry Count:</b> {retry_cnt}  |  <b>Pending:</b> {pending_retries}\n\n"
        f"<b>Order ID:</b> {order_id or '—'}\n"
        f"<b>User ID:</b> {user_id or '—'}\n"
        f"<b>Payment ID:</b> {payment_id}\n"
        f"<b>Transaction ID:</b> {tx_id}\n\n"
        f"<b>Error:</b> {error[:200]}\n\n"
        f"<b>UUID:</b> <code>{uuid_str[:32]}…</code>\n\n"
        f"<b>Payload Preview:</b>\n<code>{raw_snippet}</code>"
    )
    kb_rows = []
    if status == "failed":
        kb_rows.append([InlineKeyboardButton("🔄 Retry", callback_data=f"awm:retry:{wid}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="awm:list:0")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ── Retry failed webhook ───────────────────────────────────────────────────

async def awm_retry_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Queue a failed webhook for retry."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        wid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    max_retries = cfg.get_int("webhook_monitor_retry_count", 3)

    with get_db_session() as s:
        wh = s.get(WebhookLog, wid)
        if not wh:
            await query.answer("Not found.", show_alert=True)
            return
        if wh.retry_count >= max_retries:
            await query.answer(
                f"❌ Max retries ({max_retries}) reached. Mark as abandoned?",
                show_alert=True)
            return
        # Check for existing pending retry
        existing = (s.query(WebhookRetryQueue)
                    .filter_by(webhook_log_id=wid, status="pending")
                    .first())
        if existing:
            await query.answer("Already queued for retry.", show_alert=True)
            return
        s.add(WebhookRetryQueue(
            webhook_log_id = wid,
            provider       = wh.provider,
            payload        = wh.raw_payload,
            retry_at       = datetime.utcnow() + timedelta(minutes=2),
            attempts       = 0,
            status         = "pending",
            created_at     = datetime.utcnow(),
        ))
        wh.retry_count = wh.retry_count + 1
        s.commit()

    log_admin_action(update.effective_user.id, "webhook_monitor.retry",
                     "webhook_log", wid, module="webhook_monitor")
    await query.answer("🔄 Queued for retry.", show_alert=True)
    context.user_data["_cb_data_override"] = str(wid)
    await awm_webhook_view(update, context)


# ── Retry queue list ───────────────────────────────────────────────────────

async def awm_retries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        page = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0

    with get_db_session() as s:
        q = (s.query(WebhookRetryQueue)
             .order_by(WebhookRetryQueue.created_at.desc()))
        total = q.count()
        rows  = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        items = [
            (r.id, r.webhook_log_id, r.provider, r.status, r.attempts, r.retry_at)
            for r in rows
        ]

    lines = [f"🔄 <b>Retry Queue</b>  ({total} total)\n"]
    kb = []
    for rid, wlid, prov, st, attempts, retry_at in items:
        at_str = retry_at.strftime("%m/%d %H:%M") if retry_at else "—"
        lines.append(f"#{rid}  {prov}  [{st}]  attempts:{attempts}  next:{at_str}")
        kb.append([InlineKeyboardButton(
            f"#{rid} {prov} [{st}] att:{attempts}",
            callback_data=f"awm:wh:{wlid}")])

    if not items:
        lines.append("No retry items.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"awm:retries:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"awm:retries:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="awm:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Filter by provider ─────────────────────────────────────────────────────

async def awm_filter_provider_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show provider picker for filtering webhook list."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    rows = []
    for key, label in PROVIDER_LABELS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"awm:filter:p:{key}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="awm:list:0")])
    await _safe_edit(query, "🔍 <b>Filter by Provider</b>\n\nSelect a provider:", InlineKeyboardMarkup(rows))


async def awm_filter_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route awm:filter:* callbacks."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    # awm:filter:provider      → show provider picker
    # awm:filter:p:KEY         → filter by provider KEY
    # awm:filter:status:STATUS → filter by status
    if len(parts) == 3 and parts[2] == "provider":
        await awm_filter_provider_menu(update, context)
        return
    if len(parts) >= 4 and parts[2] == "p":
        provider = parts[3]
        await _render_webhook_list(query, 0, provider=provider)
        return
    if len(parts) >= 4 and parts[2] == "status":
        status_filter = parts[3]
        await _render_webhook_list(query, 0, status_filter=status_filter)
        return


# ── Search ─────────────────────────────────────────────────────────────────

async def awm_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt the admin for a search term stored in user_data."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    await _safe_edit(query,
        "🔍 <b>Search Webhooks</b>\n\n"
        "Use the following commands to search webhooks:\n\n"
        "• <code>/whsearch order:123</code> — by Order ID\n"
        "• <code>/whsearch user:456</code> — by User ID\n"
        "• <code>/whsearch pay:abc123</code> — by Payment ID\n"
        "• <code>/whsearch tx:xyz</code> — by Transaction ID\n"
        "• <code>/whsearch provider:binance</code> — by Provider\n\n"
        "Or tap a filter button on the list page.",
        _back("awm:menu"))


# ── Error logs ─────────────────────────────────────────────────────────────

async def awm_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        rows = (s.query(WebhookLog)
                .filter(WebhookLog.error_message != None)  # noqa: E711
                .order_by(WebhookLog.received_at.desc())
                .limit(15)
                .all())
        items = [(r.id, r.provider, r.status, r.received_at, r.error_message) for r in rows]

    lines = ["📊 <b>Recent Error Logs</b>  (last 15)\n"]
    for wid, prov, st, recv, err in items:
        at_str = recv.strftime("%m/%d %H:%M") if recv else "—"
        lines.append(
            f"#{wid}  <b>{PROVIDER_LABELS.get(prov, prov)}</b>  [{st}]  {at_str}\n"
            f"   ⚠️ {(err or '')[:120]}\n"
        )
    if not items:
        lines.append("No errors recorded.")

    await _safe_edit(query, "\n".join(lines), _back("awm:menu"))


# ── Clear old logs ─────────────────────────────────────────────────────────

async def awm_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    # Confirm step
    if query.data == "awm:clear":
        await _safe_edit(query,
            "⚠️ <b>Clear Old Logs?</b>\n\n"
            "This will delete webhook logs and health-check history older than "
            f"{cfg.get_int('webhook_log_retention_days', 30)} days.\n\n"
            "This action cannot be undone.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, clear", callback_data="awm:clear:confirm"),
                 InlineKeyboardButton("🔙 Cancel",     callback_data="awm:menu")],
            ]))
        return
    # Confirmed
    days = cfg.get_int("webhook_log_retention_days", 30)
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_db_session() as s:
        import sqlalchemy as sa
        wh_del  = s.execute(sa.text("DELETE FROM webhook_log WHERE received_at < :c"), {"c": cutoff}).rowcount
        ahl_del = s.execute(sa.text("DELETE FROM api_health_log WHERE checked_at < :c"), {"c": cutoff}).rowcount
        s.commit()
    log_admin_action(update.effective_user.id, "webhook_monitor.clear_logs",
                     module="webhook_monitor")
    await _safe_edit(query,
        f"🗑 <b>Logs cleared.</b>\n\n"
        f"Deleted {wh_del} webhook logs and {ahl_del} health-check records "
        f"older than {days} days.",
        _back("awm:menu"))


# ── Export CSV ─────────────────────────────────────────────────────────────

async def awm_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send a CSV file of the last 1000 webhook events."""
    query = update.callback_query
    await query.answer("Generating CSV…")
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        rows = (s.query(WebhookLog)
                .order_by(WebhookLog.received_at.desc())
                .limit(1000)
                .all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "webhook_uuid", "provider", "received_at", "processing_time_ms",
        "status", "error_message", "retry_count",
        "order_id", "user_id", "payment_id", "transaction_id",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.webhook_uuid, r.provider,
            r.received_at.isoformat() if r.received_at else "",
            r.processing_time_ms, r.status,
            (r.error_message or "").replace("\n", " ")[:200],
            r.retry_count,
            r.order_id or "", r.user_id or "",
            r.payment_id or "", r.transaction_id or "",
        ])

    buf.seek(0)
    filename = f"webhooks_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    log_admin_action(update.effective_user.id, "webhook_monitor.export",
                     module="webhook_monitor")
    await query.message.reply_document(
        document=InputFile(buf.getvalue().encode("utf-8"), filename=filename),
        caption="📥 Webhook export (last 1,000 events)")


# ── Force refresh ──────────────────────────────────────────────────────────

async def awm_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger an immediate health-check run."""
    query = update.callback_query
    await query.answer("🔁 Running health check…")
    if not _is_admin(update.effective_user.id):
        return
    from services.health_monitor import health_check_job
    try:
        await health_check_job(context)
    except Exception as exc:
        logger.exception("awm_refresh: health_check_job failed")
        await query.answer(f"Error: {str(exc)[:80]}", show_alert=True)
        return
    await awm_health(update, context)


# ── Settings panel ─────────────────────────────────────────────────────────

async def awm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    await _render_settings(query)


async def _render_settings(query):
    mon_status   = cfg.get("webhook_monitor_status", "enabled")
    auto_refresh = cfg.get_bool("webhook_monitor_auto_refresh", True)
    refresh_int  = cfg.get_int("webhook_monitor_refresh_interval", 60)
    retry_cnt    = cfg.get_int("webhook_monitor_retry_count", 3)
    timeout      = cfg.get_int("webhook_monitor_timeout", 10)
    alerts       = cfg.get_bool("webhook_monitor_admin_alerts", True)
    retention    = cfg.get_int("webhook_log_retention_days", 30)
    slow_ms      = cfg.get_int("health_slow_threshold_ms", 2000)
    warn_ms      = cfg.get_int("health_warn_threshold_ms", 5000)
    hc_interval  = cfg.get_int("health_check_interval", 300)
    cooldown_min = cfg.get_int("health_alert_cooldown_minutes", 60)

    icon = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(mon_status, "🟢")

    text = (
        "⚙️ <b>Webhook Monitor Settings</b>\n\n"
        f"<b>Status:</b> {icon} {mon_status.capitalize()}\n"
        f"<b>Auto-Refresh:</b> {'✅' if auto_refresh else '❌'}  "
        f"Interval: {refresh_int}s\n"
        f"<b>Retry Count:</b> {retry_cnt}\n"
        f"<b>Probe Timeout:</b> {timeout}s\n"
        f"<b>Admin Alerts:</b> {'✅' if alerts else '❌'}\n"
        f"<b>Alert Cooldown:</b> {cooldown_min} min "
        "(min. gap between repeat alerts for the same status)\n"
        f"<b>Log Retention:</b> {retention} days\n"
        f"<b>Slow Threshold:</b> {slow_ms}ms\n"
        f"<b>Warn Threshold:</b> {warn_ms}ms\n"
        f"<b>Health Check Every:</b> {hc_interval}s"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Enable",       callback_data="awm:set:status:enabled"),
            InlineKeyboardButton("🟡 Maintenance",  callback_data="awm:set:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",      callback_data="awm:set:status:disabled"),
        ],
        [
            InlineKeyboardButton(
                f"🔁 Auto-Refresh: {'ON ✅' if auto_refresh else 'OFF ❌'}",
                callback_data="awm:set:toggle:webhook_monitor_auto_refresh"),
        ],
        [
            InlineKeyboardButton(
                f"🔔 Alerts: {'ON ✅' if alerts else 'OFF ❌'}",
                callback_data="awm:set:toggle:webhook_monitor_admin_alerts"),
        ],
        [
            InlineKeyboardButton(f"🕐 Cooldown: {cooldown_min} min  →  {_next_cooldown(cooldown_min)} min",
                                  callback_data="awm:set:cooldown"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="awm:menu")],
    ])
    await _safe_edit(query, text, kb)


_COOLDOWN_STEPS = [15, 30, 60, 120, 240, 360]


def _next_cooldown(current: int) -> int:
    """Next value in the cooldown preset cycle, wrapping around."""
    if current in _COOLDOWN_STEPS:
        idx = _COOLDOWN_STEPS.index(current)
        return _COOLDOWN_STEPS[(idx + 1) % len(_COOLDOWN_STEPS)]
    return _COOLDOWN_STEPS[0]


async def awm_settings_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle the API-alert cooldown through a preset list of minute values."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    current = cfg.get_int("health_alert_cooldown_minutes", 60)
    new_val = _next_cooldown(current)
    cfg.set("health_alert_cooldown_minutes", new_val)
    log_admin_action(update.effective_user.id, "webhook_monitor.settings",
                     "webhook_monitor", 0,
                     f"health_alert_cooldown_minutes={new_val}",
                     module="webhook_monitor")
    await query.answer(f"Alert cooldown → {new_val} min", show_alert=True)
    await _render_settings(query)


async def awm_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        new_status = query.data.split(":")[3]
    except IndexError:
        return
    cfg.set("webhook_monitor_status", new_status)
    log_admin_action(update.effective_user.id, "webhook_monitor.settings",
                     "webhook_monitor", 0,
                     f"webhook_monitor_status={new_status}",
                     module="webhook_monitor")
    await query.answer(f"Status → {new_status}", show_alert=True)
    await _render_settings(query)


async def awm_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a boolean BotConfig key — awm:set:toggle:<key>"""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    try:
        key = query.data.split(":")[3]
    except IndexError:
        return
    current = cfg.get_bool(key, False)
    cfg.set(key, str(not current))
    log_admin_action(update.effective_user.id, "webhook_monitor.settings",
                     "webhook_monitor", 0,
                     f"{key}={not current}",
                     module="webhook_monitor")
    await _render_settings(query)


# ── Master dispatcher for awm:* callbacks ─────────────────────────────────

async def awm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for all awm:* callbacks not registered individually."""
    query = update.callback_query
    data  = query.data or ""

    if data == "awm:menu":
        return await awm_menu(update, context)
    if data == "awm:health":
        return await awm_health(update, context)
    if data.startswith("awm:health:"):
        return await awm_health_service(update, context)
    if data.startswith("awm:list:"):
        return await awm_list(update, context)
    if data.startswith("awm:wh:"):
        return await awm_webhook_view(update, context)
    if data.startswith("awm:retry:"):
        return await awm_retry_webhook(update, context)
    if data.startswith("awm:retries:"):
        return await awm_retries(update, context)
    if data.startswith("awm:filter:"):
        return await awm_filter_dispatch(update, context)
    if data == "awm:search":
        return await awm_search(update, context)
    if data == "awm:logs":
        return await awm_logs(update, context)
    if data in ("awm:clear", "awm:clear:confirm"):
        return await awm_clear(update, context)
    if data == "awm:export":
        return await awm_export(update, context)
    if data == "awm:refresh":
        return await awm_refresh(update, context)
    if data == "awm:settings":
        return await awm_settings(update, context)
    if data.startswith("awm:set:status:"):
        return await awm_settings_status(update, context)
    if data == "awm:set:cooldown":
        return await awm_settings_cooldown(update, context)
    if data.startswith("awm:set:toggle:"):
        return await awm_settings_toggle(update, context)

    await query.answer("❓ Unknown command.", show_alert=True)
