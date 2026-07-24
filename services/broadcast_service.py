"""Enterprise Broadcast Service — centralized utilities for the Broadcast Center.

Provides:
  • test_broadcast_to_admin  — send a test message to the admin only
  • generate_broadcast_report — delivery / failure / blocked / skipped report
  • export_report_csv        — CSV export of broadcast report
  • export_report_json       — JSON export of broadcast report
  • find_interrupted_broadcasts — broadcasts stuck in "sending" state
  • get_broadcast_dashboard_stats — aggregate dashboard statistics
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from database import get_db_session
from database.models import ScheduledBroadcast, BroadcastLog, BroadcastRetryQueue

logger = logging.getLogger(__name__)


# ── Test Broadcast ──────────────────────────────────────────────────────────

async def test_broadcast_to_admin(bot, admin_telegram_id: int, broadcast_id: int) -> bool:
    """Send broadcast message to admin only (test mode). Returns True on success."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputPollOption

    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, broadcast_id)
        if not br:
            return False
        mtype      = br.media_type
        msg_text   = br.message_text or ""
        file_id    = br.file_id
        btn_text   = br.button_text
        btn_url    = br.button_url
        parse_mode = br.parse_mode or "HTML"
        silent     = br.disable_notification or False

    msg_kb = None
    if btn_text and btn_url:
        msg_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])

    header = f"🧪 <b>TEST BROADCAST (ID #{broadcast_id})</b>\n\n"

    try:
        if mtype == "text":
            await bot.send_message(
                admin_telegram_id, header + msg_text,
                parse_mode="HTML", reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "photo" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_photo(
                admin_telegram_id, file_id, caption=msg_text[:1024],
                parse_mode=parse_mode, reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "video" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_video(
                admin_telegram_id, file_id, caption=msg_text[:1024],
                parse_mode=parse_mode, reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "animation" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_animation(
                admin_telegram_id, file_id, caption=msg_text[:1024],
                parse_mode=parse_mode, reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "document" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_document(
                admin_telegram_id, file_id, caption=msg_text[:1024],
                parse_mode=parse_mode, reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "voice" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_voice(admin_telegram_id, file_id, caption=msg_text[:1024])
        elif mtype == "audio" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_audio(
                admin_telegram_id, file_id, caption=msg_text[:1024],
                reply_markup=msg_kb, disable_notification=silent)
        elif mtype == "sticker" and file_id:
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            await bot.send_sticker(admin_telegram_id, file_id, disable_notification=silent)
        elif mtype == "poll":
            await bot.send_message(admin_telegram_id, header, parse_mode="HTML")
            try:
                poll_data   = json.loads(msg_text)
                question    = poll_data.get("question", "Poll")
                raw_options = poll_data.get("options", ["Option 1", "Option 2"])
                options     = [InputPollOption(text=str(o)) for o in raw_options[:10]]
            except Exception:
                question = msg_text[:255]
                options  = [InputPollOption(text="Yes"), InputPollOption(text="No")]
            await bot.send_poll(
                admin_telegram_id, question, options,
                is_anonymous=True, disable_notification=silent)
        else:
            await bot.send_message(
                admin_telegram_id, header + msg_text,
                parse_mode="HTML", disable_notification=silent)
        return True
    except Exception:
        logger.exception("test_broadcast_to_admin: failed for broadcast #%d", broadcast_id)
        return False


# ── Report Generation ───────────────────────────────────────────────────────

def generate_broadcast_report(broadcast_id: int) -> Dict[str, Any]:
    """Generate a comprehensive report dict for a broadcast."""
    with get_db_session() as s:
        br = s.get(ScheduledBroadcast, broadcast_id)
        if not br:
            return {}

        logs = (s.query(BroadcastLog)
                .filter_by(broadcast_id=broadcast_id)
                .order_by(BroadcastLog.created_at.desc())
                .all())

        retry_pending = (s.query(BroadcastRetryQueue)
                         .filter_by(broadcast_id=broadcast_id, status="pending")
                         .count())
        retry_failed  = (s.query(BroadcastRetryQueue)
                         .filter_by(broadcast_id=broadcast_id, status="failed")
                         .count())
        retry_sent    = (s.query(BroadcastRetryQueue)
                         .filter_by(broadcast_id=broadcast_id, status="sent")
                         .count())

        sent      = br.sent_count      or 0
        delivered = br.delivered_count or 0
        failed    = br.failed_count    or 0
        blocked   = br.blocked_count   or 0
        skipped   = br.skipped_count   or 0
        total     = br.total_recipients or 0

        delivery_rate = (delivered / sent * 100) if sent else 0
        failure_rate  = (failed    / sent * 100) if sent else 0
        block_rate    = (blocked   / sent * 100) if sent else 0

        # Average delivery time from logs
        avg_delivery_ms: Optional[float] = None
        if br.started_at and br.finished_at and delivered > 0:
            duration = (br.finished_at - br.started_at).total_seconds()
            avg_delivery_ms = (duration / delivered) * 1000

        log_rows = []
        for l in logs:
            duration_s = None
            if l.started_at and l.finished_at:
                duration_s = (l.finished_at - l.started_at).total_seconds()
            log_rows.append({
                "run_at":    l.created_at.isoformat() if l.created_at else None,
                "total":     l.total_recipients,
                "sent":      l.sent,
                "delivered": l.delivered,
                "failed":    l.failed,
                "blocked":   l.blocked,
                "skipped":   l.skipped,
                "duration_s": round(duration_s, 2) if duration_s is not None else None,
            })

        return {
            "broadcast_id":     broadcast_id,
            "title":            br.title,
            "status":           br.status,
            "media_type":       br.media_type,
            "target_segment":   br.target_segment,
            "created_at":       br.created_at.isoformat()   if br.created_at   else None,
            "scheduled_at":     br.scheduled_at.isoformat() if br.scheduled_at else None,
            "started_at":       br.started_at.isoformat()   if br.started_at   else None,
            "finished_at":      br.finished_at.isoformat()  if br.finished_at  else None,
            "is_recurring":     br.is_recurring,
            "recurrence_type":  br.recurrence_type,
            "total_recipients": total,
            "sent":             sent,
            "delivered":        delivered,
            "failed":           failed,
            "blocked":          blocked,
            "skipped":          skipped,
            "delivery_rate_pct": round(delivery_rate, 2),
            "failure_rate_pct":  round(failure_rate,  2),
            "block_rate_pct":    round(block_rate,    2),
            "retry_pending":    retry_pending,
            "retry_failed":     retry_failed,
            "retry_sent":       retry_sent,
            "avg_delivery_ms":  round(avg_delivery_ms, 1) if avg_delivery_ms else None,
            "run_logs":         log_rows,
        }


def export_report_csv(broadcast_id: int) -> str:
    """Export broadcast report as a CSV-formatted string."""
    report = generate_broadcast_report(broadcast_id)
    if not report:
        return "No data found for broadcast ID."

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Broadcast Report"])
    writer.writerow(["Field", "Value"])
    writer.writerow(["Broadcast ID",   report.get("broadcast_id")])
    writer.writerow(["Title",          report.get("title")])
    writer.writerow(["Status",         report.get("status")])
    writer.writerow(["Media Type",     report.get("media_type")])
    writer.writerow(["Target Segment", report.get("target_segment")])
    writer.writerow(["Created At",     report.get("created_at")])
    writer.writerow(["Scheduled At",   report.get("scheduled_at")])
    writer.writerow(["Started At",     report.get("started_at")])
    writer.writerow(["Finished At",    report.get("finished_at")])
    writer.writerow(["Is Recurring",   report.get("is_recurring")])
    writer.writerow(["Recurrence",     report.get("recurrence_type")])
    writer.writerow([])

    writer.writerow(["Delivery Statistics"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Total Recipients",  report.get("total_recipients")])
    writer.writerow(["Sent",              report.get("sent")])
    writer.writerow(["Delivered",         report.get("delivered")])
    writer.writerow(["Failed",            report.get("failed")])
    writer.writerow(["Blocked",           report.get("blocked")])
    writer.writerow(["Skipped",           report.get("skipped")])
    writer.writerow(["Delivery Rate (%)", report.get("delivery_rate_pct")])
    writer.writerow(["Failure Rate (%)",  report.get("failure_rate_pct")])
    writer.writerow(["Block Rate (%)",    report.get("block_rate_pct")])
    writer.writerow(["Avg Delivery (ms)", report.get("avg_delivery_ms")])
    writer.writerow([])

    writer.writerow(["Retry Queue"])
    writer.writerow(["Pending", report.get("retry_pending")])
    writer.writerow(["Sent",    report.get("retry_sent")])
    writer.writerow(["Failed",  report.get("retry_failed")])
    writer.writerow([])

    if report.get("run_logs"):
        writer.writerow(["Run Logs"])
        writer.writerow(["Run At", "Total", "Sent", "Delivered",
                          "Failed", "Blocked", "Skipped", "Duration (s)"])
        for row in report["run_logs"]:
            writer.writerow([
                row.get("run_at"), row.get("total"), row.get("sent"),
                row.get("delivered"), row.get("failed"), row.get("blocked"),
                row.get("skipped"), row.get("duration_s"),
            ])

    return output.getvalue()


def export_report_json(broadcast_id: int) -> str:
    """Export broadcast report as a JSON-formatted string."""
    report = generate_broadcast_report(broadcast_id)
    return json.dumps(report, indent=2, ensure_ascii=False)


# ── Interrupted Broadcast Detection ─────────────────────────────────────────

def find_interrupted_broadcasts(stale_minutes: int = 30) -> List[Tuple]:
    """Return broadcasts stuck in 'sending' state longer than stale_minutes.

    Returns list of (id, title, started_at, sent_count, total_recipients).
    """
    cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
    with get_db_session() as s:
        stuck = (s.query(ScheduledBroadcast)
                 .filter(
                     ScheduledBroadcast.status == "sending",
                     ScheduledBroadcast.started_at <= cutoff,
                 )
                 .order_by(ScheduledBroadcast.started_at.asc())
                 .all())
        return [
            (b.id, b.title, b.started_at, b.sent_count or 0, b.total_recipients or 0)
            for b in stuck
        ]


# ── Dashboard Aggregate Stats ────────────────────────────────────────────────

def get_broadcast_dashboard_stats() -> Dict[str, Any]:
    """Return aggregate statistics for the Enterprise Broadcast Center dashboard."""
    now         = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    with get_db_session() as s:
        from sqlalchemy import func

        total     = s.query(ScheduledBroadcast).count()
        running   = s.query(ScheduledBroadcast).filter_by(status="sending").count()
        scheduled = s.query(ScheduledBroadcast).filter_by(status="scheduled").count()
        paused    = s.query(ScheduledBroadcast).filter_by(status="paused").count()
        completed = s.query(ScheduledBroadcast).filter_by(status="sent").count()
        failed    = s.query(ScheduledBroadcast).filter_by(status="failed").count()
        cancelled = s.query(ScheduledBroadcast).filter_by(status="cancelled").count()
        drafts    = s.query(ScheduledBroadcast).filter_by(status="draft").count()

        today_bc  = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= today_start).count()
        week_bc   = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= week_start).count()
        month_bc  = s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.created_at >= month_start).count()

        agg = s.query(
            func.sum(BroadcastLog.delivered),
            func.sum(BroadcastLog.failed),
            func.sum(BroadcastLog.sent),
        ).first()
        total_delivered = int(agg[0] or 0)
        total_failed    = int(agg[1] or 0)
        total_sent      = int(agg[2] or 0)

        # Average delivery time from completed log entries
        avg_delivery_ms: Optional[float] = None
        logs_with_time = (s.query(BroadcastLog)
                           .filter(
                               BroadcastLog.started_at.isnot(None),
                               BroadcastLog.finished_at.isnot(None),
                               BroadcastLog.sent > 0,
                           )
                           .order_by(BroadcastLog.created_at.desc())
                           .limit(100)
                           .all())
        if logs_with_time:
            durations = []
            for l in logs_with_time:
                d = (l.finished_at - l.started_at).total_seconds()
                if d > 0 and l.sent > 0:
                    durations.append(d / l.sent)
            if durations:
                avg_s = sum(durations) / len(durations)
                avg_delivery_ms = round(avg_s * 1000, 1)

        retry_pending = s.query(BroadcastRetryQueue).filter_by(status="pending").count()

    delivery_rate = (total_delivered / total_sent * 100) if total_sent else 0

    return {
        "total":           total,
        "running":         running,
        "scheduled":       scheduled,
        "paused":          paused,
        "completed":       completed,
        "failed":          failed,
        "cancelled":       cancelled,
        "drafts":          drafts,
        "today":           today_bc,
        "week":            week_bc,
        "month":           month_bc,
        "total_sent":      total_sent,
        "total_delivered": total_delivered,
        "total_failed":    total_failed,
        "delivery_rate":   round(delivery_rate, 1),
        "avg_delivery_ms": avg_delivery_ms,
        "retry_pending":   retry_pending,
    }
