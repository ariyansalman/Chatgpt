"""Subscription Expiry Reminder service (V22).

Separate from the billing-renewal reminder in ``subscription_service.py``.
This service tracks when a subscription's ``expires_at`` is approaching and
sends one-time reminder messages at configurable intervals.

Supported intervals (days before expiry):
    30, 15, 7, 3, 1 days, and 0 (expired notice).

Each interval is sent **at most once** per subscription, tracked via the
``SubscriptionReminderLog`` table.

Admin-configurable via BotConfig keys:
    sub_expiry_reminder_status        — "enabled" / "maintenance" / "disabled"
    sub_expiry_reminder_days          — comma-sep, e.g. "30,15,7,3,1"
    sub_expiry_reminder_template      — message template key (1/2/3)
    sub_expiry_reminder_send_time     — "any" or UTC hour string "8","12","18",...
    sub_expiry_reminder_retry_failed  — bool: retry previously-failed sends
    sub_expiry_reminder_check_interval_minutes — job cadence

Entry point for the JobQueue:
    ``expiry_reminder_job(context)``

Admin helpers:
    ``get_stats()``        — dashboard counters
    ``manual_remind(bot, sub_id)``  — admin-triggered single-subscription send
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from database import get_db_session, User, Product, Subscription
from database.models import SubscriptionReminderLog
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

# Days-before-expiry reminder thresholds (descending order matters).
DEFAULT_INTERVALS = [30, 15, 7, 3, 1]
EXPIRED_INTERVAL = 0   # special value meaning "subscription just expired"

# Subscription statuses considered "active" (eligible for expiry reminders).
_ACTIVE_STATUSES = ("active", "past_due")


# ─────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────

def _feature_status() -> str:
    """Return "enabled", "maintenance", or "disabled"."""
    return cfg.get_str("sub_expiry_reminder_status", "enabled").lower()


def _reminder_intervals() -> list[int]:
    """Parse the comma-separated config into a sorted list of ints."""
    raw = cfg.get_str("sub_expiry_reminder_days", "30,15,7,3,1")
    try:
        vals = sorted({int(x.strip()) for x in raw.split(",") if x.strip().isdigit()},
                      reverse=True)
        return vals if vals else DEFAULT_INTERVALS
    except Exception:
        return DEFAULT_INTERVALS


def _retry_failed() -> bool:
    return cfg.get_bool("sub_expiry_reminder_retry_failed", True)


def _send_time_ok() -> bool:
    """Return True if it is within the configured send-time window (UTC)."""
    raw = cfg.get_str("sub_expiry_reminder_send_time", "any").lower().strip()
    if raw == "any" or not raw:
        return True
    try:
        target_hour = int(raw)
        return datetime.utcnow().hour == target_hour
    except (ValueError, TypeError):
        return True


def _build_message(product_name: str, expires_at: datetime,
                   days_left: int, interval: int) -> str:
    """Build the reminder message from the configured template."""
    tmpl_key = cfg.get_str("sub_expiry_reminder_template", "1")
    exp_str = expires_at.strftime("%Y-%m-%d")

    if interval == EXPIRED_INTERVAL:
        if tmpl_key == "2":
            return (
                f"😔 <b>Subscription expired</b>\n\n"
                f"Your subscription to <b>{product_name}</b> has expired as of "
                f"<b>{exp_str}</b>.\n\n"
                f"Renew now to continue enjoying uninterrupted access."
            )
        elif tmpl_key == "3":
            return (
                f"⏰ <b>Time's up — {product_name}</b>\n\n"
                f"Your subscription expired on <b>{exp_str}</b>.\n"
                f"Tap below to renew and keep your access going."
            )
        else:
            return (
                f"❌ <b>Subscription Expired</b>\n\n"
                f"Your <b>{product_name}</b> subscription expired on <b>{exp_str}</b>.\n"
                f"Please renew to restore access."
            )

    day_word = "day" if days_left == 1 else "days"
    if tmpl_key == "2":
        return (
            f"⏳ <b>Subscription expiring soon</b>\n\n"
            f"Your <b>{product_name}</b> subscription expires in "
            f"<b>{days_left} {day_word}</b> on <b>{exp_str}</b>.\n\n"
            f"Renew early to avoid interruption."
        )
    elif tmpl_key == "3":
        return (
            f"🔔 <b>{product_name} — {days_left} {day_word} left</b>\n\n"
            f"Don't let your subscription lapse! It expires on <b>{exp_str}</b>.\n"
            f"Renew now to keep uninterrupted access."
        )
    else:
        return (
            f"🔔 <b>Subscription Reminder</b>\n\n"
            f"Your <b>{product_name}</b> subscription expires in "
            f"<b>{days_left} {day_word}</b> (on <b>{exp_str}</b>).\n\n"
            f"Make sure to renew before it runs out."
        )


# ─────────────────────────────────────────────────────────────────────────
# Core send logic
# ─────────────────────────────────────────────────────────────────────────

async def send_expiry_reminders(bot: Bot) -> int:
    """Scan all active subscriptions and send due expiry reminders.

    Returns the number of messages successfully sent.
    """
    status = _feature_status()
    if status == "disabled":
        return 0
    if status == "maintenance":
        logger.info("sub_expiry_reminder: maintenance mode — skipping send.")
        return 0
    if not _send_time_ok():
        logger.debug("sub_expiry_reminder: outside send-time window — skipping.")
        return 0

    intervals = _reminder_intervals()
    retry = _retry_failed()
    now = datetime.utcnow()
    sent = 0

    # Build the scan horizon: furthest interval (e.g. 30 days)
    max_days = max(intervals) if intervals else 30
    horizon = now + timedelta(days=max_days)

    # Fetch all subscriptions whose expires_at is within the next max_days,
    # OR that have already expired (for the expired notice).
    with get_db_session() as s:
        subs = (s.query(Subscription)
                .filter(
                    Subscription.status.in_(_ACTIVE_STATUSES),
                    Subscription.expires_at.isnot(None),
                    Subscription.expires_at <= horizon,
                )
                .all())

        # Build list of (sub_id, user_telegram_id, product_name, expires_at, intervals_needed)
        work = []
        for sub in subs:
            user = s.query(User).filter(User.id == sub.user_id).first()
            product = s.query(Product).filter(Product.id == sub.product_id).first()
            if not user or not user.telegram_id:
                continue
            pname = product.name if product else f"Subscription #{sub.id}"

            # Determine which intervals still need to be sent
            days_remaining = (sub.expires_at - now).total_seconds() / 86400

            due_intervals = []
            # Check expiry notice
            if days_remaining < 0:
                due_intervals.append(EXPIRED_INTERVAL)

            # Check each configured interval
            for interval in intervals:
                if days_remaining <= interval:
                    due_intervals.append(interval)

            if not due_intervals:
                continue

            # Fetch which intervals already have log entries
            existing_logs = (s.query(SubscriptionReminderLog)
                             .filter(SubscriptionReminderLog.subscription_id == sub.id)
                             .all())
            sent_intervals = {
                log.interval_days
                for log in existing_logs
                if log.success or not retry
            }

            intervals_to_send = [i for i in due_intervals if i not in sent_intervals]
            if not intervals_to_send:
                continue

            # Only send the most urgent unsent interval (smallest days_left)
            # to avoid spamming multiple at once.
            target_interval = min(intervals_to_send)
            work.append((
                sub.id,
                int(user.telegram_id),
                pname,
                sub.expires_at,
                target_interval,
                int(max(0, days_remaining)),
            ))

        s.expunge_all()

    # Send messages outside the session to avoid long-held transactions
    for sub_id, tg_id, pname, expires_at, interval, days_left in work:
        text = _build_message(pname, expires_at, days_left, interval)
        success = False
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            success = True
            sent += 1
        except Exception:
            logger.exception(
                "sub_expiry_reminder: failed to send interval=%d for sub #%d (tg:%d)",
                interval, sub_id, tg_id,
            )

        # Record the attempt
        _record_log(sub_id, interval, success)

    return sent


def _record_log(sub_id: int, interval: int, success: bool) -> None:
    """Insert or update the reminder log entry."""
    try:
        with get_db_session() as s:
            existing = (s.query(SubscriptionReminderLog)
                        .filter(
                            SubscriptionReminderLog.subscription_id == sub_id,
                            SubscriptionReminderLog.interval_days == interval,
                        ).first())
            if existing is None:
                log = SubscriptionReminderLog(
                    subscription_id=sub_id,
                    interval_days=interval,
                    success=success,
                    retry_count=0,
                    sent_at=datetime.utcnow(),
                )
                s.add(log)
            else:
                existing.sent_at = datetime.utcnow()
                existing.success = success
                existing.retry_count = (existing.retry_count or 0) + (0 if success else 1)
            s.commit()
    except Exception:
        logger.exception("sub_expiry_reminder: failed to record log for sub #%d interval %d",
                         sub_id, interval)


# ─────────────────────────────────────────────────────────────────────────
# Admin helpers
# ─────────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return counters for the admin dashboard."""
    now = datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59)
    week_end = now + timedelta(days=7)

    stats = {
        "total_active": 0,
        "expiring_today": 0,
        "expiring_this_week": 0,
        "expired": 0,
        "reminders_sent": 0,
    }
    try:
        with get_db_session() as s:
            stats["total_active"] = (
                s.query(Subscription)
                .filter(Subscription.status.in_(_ACTIVE_STATUSES))
                .count()
            )
            stats["expiring_today"] = (
                s.query(Subscription)
                .filter(
                    Subscription.status.in_(_ACTIVE_STATUSES),
                    Subscription.expires_at >= now,
                    Subscription.expires_at <= today_end,
                )
                .count()
            )
            stats["expiring_this_week"] = (
                s.query(Subscription)
                .filter(
                    Subscription.status.in_(_ACTIVE_STATUSES),
                    Subscription.expires_at >= now,
                    Subscription.expires_at <= week_end,
                )
                .count()
            )
            stats["expired"] = (
                s.query(Subscription)
                .filter(
                    Subscription.expires_at < now,
                    Subscription.status.notin_(("cancelled",)),
                )
                .count()
            )
            stats["reminders_sent"] = (
                s.query(SubscriptionReminderLog)
                .filter(SubscriptionReminderLog.success == True)  # noqa: E712
                .count()
            )
    except Exception:
        logger.exception("sub_expiry_reminder: get_stats failed")
    return stats


async def manual_remind(bot: Bot, sub_id: int) -> tuple[bool, str]:
    """Admin action: forcibly send the next pending reminder for sub_id.

    Returns (success, message) for display in the admin panel.
    Ignores send-time window (manual triggers are always immediate).
    Ignores feature status (admin override).
    """
    now = datetime.utcnow()
    try:
        with get_db_session() as s:
            sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
            if not sub:
                return False, "Subscription not found."
            user = s.query(User).filter(User.id == sub.user_id).first()
            product = s.query(Product).filter(Product.id == sub.product_id).first()
            if not user or not user.telegram_id:
                return False, "User has no Telegram ID."
            tg_id = int(user.telegram_id)
            pname = product.name if product else f"Subscription #{sub_id}"
            expires_at = sub.expires_at

        days_remaining = (expires_at - now).total_seconds() / 86400

        if days_remaining < 0:
            interval = EXPIRED_INTERVAL
            days_left = 0
        else:
            # Find the most appropriate interval
            intervals = _reminder_intervals()
            matched = [i for i in intervals if days_remaining <= i]
            interval = min(matched) if matched else (intervals[0] if intervals else 1)
            days_left = int(max(0, days_remaining))

        text = _build_message(pname, expires_at, days_left, interval)
        await bot.send_message(chat_id=tg_id, text=text, parse_mode=ParseMode.HTML)
        _record_log(sub_id, interval, True)
        return True, f"Reminder sent to user tg:{tg_id} (interval={interval}d)."

    except Exception as exc:
        logger.exception("sub_expiry_reminder: manual_remind failed for sub #%d", sub_id)
        return False, f"Send failed: {exc}"


# ─────────────────────────────────────────────────────────────────────────
# JobQueue entry point
# ─────────────────────────────────────────────────────────────────────────

async def expiry_reminder_job(context) -> None:
    """Called periodically by the JobQueue. Sends all due expiry reminders."""
    try:
        n = await send_expiry_reminders(context.bot)
        if n:
            logger.info("sub_expiry_reminder: sent %d expiry reminder(s).", n)
    except Exception:
        logger.exception("sub_expiry_reminder: expiry_reminder_job failed")
