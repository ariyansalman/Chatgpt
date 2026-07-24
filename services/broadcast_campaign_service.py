"""Broadcast Campaign Manager Service — V44.4.

Provides:
  • Template CRUD (create, read, update, delete, duplicate, favorite, search, group)
  • Campaign CRUD (create, read, update, delete, duplicate, run, pause, resume, cancel, archive)
  • Automation Rule CRUD (create, read, update, delete, toggle)
  • Automation trigger dispatcher (called from event hooks across the bot)
  • A/B testing support (split audience, compare CTR, select winner)
  • Variable substitution for templates
  • Campaign scheduler job (runs every 60 seconds via job_queue)
  • Pre-built template seeding
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from database import get_db_session
from database.models import (
    BroadcastCampaign, BroadcastTemplate, BroadcastAutomationRule,
    CampaignExecution, AutomationTriggerLog,
    CampaignStatus, CampaignType, AutomationTrigger,
    User,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ── Variable substitution ─────────────────────────────────────────────────────

SUPPORTED_VARIABLES = [
    "{first_name}", "{last_name}", "{username}", "{telegram_id}",
    "{wallet_balance}", "{product_name}", "{category_name}",
    "{coupon_code}", "{discount}", "{bonus}", "{old_price}", "{new_price}",
    "{order_id}", "{subscription_expiry}", "{referral_reward}",
    "{current_date}", "{current_time}", "{custom_field}",
]


def substitute_variables(text: str, user: Optional[User] = None,
                         extra: Optional[Dict[str, Any]] = None) -> str:
    """Replace {variable} placeholders in *text* with real values.

    Falls back to an empty string for any variable that cannot be resolved.
    """
    if not text:
        return text

    now = datetime.utcnow()
    vals: Dict[str, str] = {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time": now.strftime("%H:%M UTC"),
        "custom_field": "",
    }

    if user:
        vals["first_name"]    = user.first_name or ""
        vals["last_name"]     = user.last_name or ""
        vals["username"]      = f"@{user.username}" if user.username else ""
        vals["telegram_id"]   = str(user.telegram_id)
        wallet_bal = getattr(user, "wallet_balance", 0) or 0
        vals["wallet_balance"] = f"{wallet_bal:.2f}"

    if extra:
        for k, v in extra.items():
            vals[k] = str(v) if v is not None else ""

    result = text
    for key, value in vals.items():
        result = result.replace(f"{{{key}}}", value)

    # Clear any unresolved placeholders
    for var in SUPPORTED_VARIABLES:
        result = result.replace(var, "")

    return result


# ── Template helpers ─────────────────────────────────────────────────────────

def get_all_templates(search: Optional[str] = None, category: Optional[str] = None,
                      favorites_only: bool = False) -> List[BroadcastTemplate]:
    """Return templates matching the given filters, ordered by favorite → name."""
    with get_db_session() as s:
        q = s.query(BroadcastTemplate)
        if search:
            q = q.filter(BroadcastTemplate.name.ilike(f"%{search}%"))
        if category:
            q = q.filter(BroadcastTemplate.category == category)
        if favorites_only:
            q = q.filter(BroadcastTemplate.is_favorite.is_(True))
        results = q.order_by(
            BroadcastTemplate.is_favorite.desc(),
            BroadcastTemplate.usage_count.desc(),
            BroadcastTemplate.name,
        ).all()
        # Detach from session
        s.expunge_all()
        return results


def get_template(template_id: int) -> Optional[BroadcastTemplate]:
    with get_db_session() as s:
        t = s.get(BroadcastTemplate, template_id)
        if t:
            s.expunge(t)
        return t


def create_template(name: str, message_text: str, created_by: Optional[int] = None,
                    category: Optional[str] = None, group_name: Optional[str] = None,
                    media_type: str = "text", button_text: Optional[str] = None,
                    button_url: Optional[str] = None, parse_mode: str = "HTML") -> BroadcastTemplate:
    """Create and persist a new broadcast template."""
    now = datetime.utcnow()
    with get_db_session() as s:
        t = BroadcastTemplate(
            name=name,
            message_text=message_text,
            created_by=created_by,
            category=category,
            group_name=group_name,
            media_type=media_type,
            button_text=button_text,
            button_url=button_url,
            parse_mode=parse_mode,
            is_default=False,
            is_favorite=False,
            usage_count=0,
            created_at=now,
            updated_at=now,
        )
        s.add(t)
        s.flush()
        s.expunge(t)
        return t


def update_template(template_id: int, **kwargs) -> bool:
    """Update mutable fields on a template. Returns True if found."""
    allowed = {"name", "message_text", "category", "group_name",
               "media_type", "button_text", "button_url", "parse_mode"}
    with get_db_session() as s:
        t = s.get(BroadcastTemplate, template_id)
        if not t:
            return False
        for k, v in kwargs.items():
            if k in allowed:
                setattr(t, k, v)
        t.updated_at = datetime.utcnow()
        return True


def delete_template(template_id: int) -> bool:
    """Delete a template. Returns True if found and deleted."""
    with get_db_session() as s:
        t = s.get(BroadcastTemplate, template_id)
        if not t:
            return False
        s.delete(t)
        return True


def duplicate_template(template_id: int, created_by: Optional[int] = None) -> Optional[BroadcastTemplate]:
    """Duplicate an existing template, returning the new copy."""
    with get_db_session() as s:
        src = s.get(BroadcastTemplate, template_id)
        if not src:
            return None
        now = datetime.utcnow()
        copy = BroadcastTemplate(
            name=f"Copy of {src.name}",
            category=src.category,
            group_name=src.group_name,
            message_text=src.message_text,
            media_type=src.media_type,
            button_text=src.button_text,
            button_url=src.button_url,
            parse_mode=src.parse_mode,
            variables_json=src.variables_json,
            is_default=False,
            is_favorite=False,
            usage_count=0,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        s.add(copy)
        s.flush()
        s.expunge(copy)
        return copy


def toggle_template_favorite(template_id: int) -> Optional[bool]:
    """Toggle the is_favorite flag. Returns the new state or None if not found."""
    with get_db_session() as s:
        t = s.get(BroadcastTemplate, template_id)
        if not t:
            return None
        t.is_favorite = not t.is_favorite
        t.updated_at  = datetime.utcnow()
        return t.is_favorite


def get_template_groups() -> Dict[str, int]:
    """Return {group_name: template_count} mapping."""
    with get_db_session() as s:
        rows = (s.query(BroadcastTemplate.group_name,
                        func.count(BroadcastTemplate.id))
                .filter(BroadcastTemplate.group_name.isnot(None))
                .group_by(BroadcastTemplate.group_name)
                .all())
        return {row[0]: row[1] for row in rows}


def increment_template_usage(template_id: int) -> None:
    with get_db_session() as s:
        t = s.get(BroadcastTemplate, template_id)
        if t:
            t.usage_count = (t.usage_count or 0) + 1


# ── Campaign helpers ─────────────────────────────────────────────────────────

def get_all_campaigns(status: Optional[str] = None, include_archived: bool = False,
                      page: int = 0, page_size: int = 10) -> Tuple[List[BroadcastCampaign], int]:
    """Return (campaigns, total_count) for the given filters."""
    with get_db_session() as s:
        q = s.query(BroadcastCampaign)
        if not include_archived:
            q = q.filter(BroadcastCampaign.is_archived.is_(False))
        if status:
            q = q.filter(BroadcastCampaign.status == status)
        total = q.count()
        items = (q.order_by(BroadcastCampaign.created_at.desc())
                  .offset(page * page_size)
                  .limit(page_size)
                  .all())
        s.expunge_all()
        return items, total


def get_campaign(campaign_id: int) -> Optional[BroadcastCampaign]:
    with get_db_session() as s:
        c = s.get(BroadcastCampaign, campaign_id)
        if c:
            s.expunge(c)
        return c


def create_campaign(name: str, campaign_type: str = "single",
                    created_by: Optional[int] = None, **kwargs) -> BroadcastCampaign:
    """Create and persist a new campaign in DRAFT status."""
    now = datetime.utcnow()
    with get_db_session() as s:
        c = BroadcastCampaign(
            name=name,
            campaign_type=campaign_type,
            status=CampaignStatus.DRAFT.value,
            media_type="text",
            parse_mode="HTML",
            target_segment="all",
            ab_split_percent=50,
            total_runs=0, total_sent=0, total_delivered=0, total_failed=0,
            is_archived=False,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            **kwargs,
        )
        s.add(c)
        s.flush()
        s.expunge(c)
        return c


def update_campaign(campaign_id: int, **kwargs) -> bool:
    """Update mutable campaign fields. Returns True if found."""
    allowed = {
        "name", "campaign_type", "status", "message_text", "media_type",
        "file_id", "button_text", "button_url", "parse_mode", "variables_json",
        "target_segment", "audience_filters_json", "start_date", "end_date",
        "timezone", "schedule_type", "schedule_interval_hours", "schedule_days_json",
        "ab_test_enabled", "ab_variant_b_text", "ab_split_percent",
        "steps_json", "template_id", "next_run_at", "is_archived",
    }
    with get_db_session() as s:
        c = s.get(BroadcastCampaign, campaign_id)
        if not c:
            return False
        for k, v in kwargs.items():
            if k in allowed:
                setattr(c, k, v)
        c.updated_at = datetime.utcnow()
        return True


def delete_campaign(campaign_id: int) -> bool:
    with get_db_session() as s:
        c = s.get(BroadcastCampaign, campaign_id)
        if not c:
            return False
        s.delete(c)
        return True


def duplicate_campaign(campaign_id: int, created_by: Optional[int] = None) -> Optional[BroadcastCampaign]:
    """Duplicate a campaign, returning it in DRAFT status."""
    with get_db_session() as s:
        src = s.get(BroadcastCampaign, campaign_id)
        if not src:
            return None
        now = datetime.utcnow()
        copy = BroadcastCampaign(
            name=f"Copy of {src.name}",
            campaign_type=src.campaign_type,
            status=CampaignStatus.DRAFT.value,
            template_id=src.template_id,
            message_text=src.message_text,
            media_type=src.media_type,
            file_id=src.file_id,
            button_text=src.button_text,
            button_url=src.button_url,
            parse_mode=src.parse_mode,
            variables_json=src.variables_json,
            target_segment=src.target_segment,
            audience_filters_json=src.audience_filters_json,
            schedule_type=src.schedule_type,
            schedule_interval_hours=src.schedule_interval_hours,
            timezone=src.timezone or "UTC",
            ab_test_enabled=src.ab_test_enabled,
            ab_variant_b_text=src.ab_variant_b_text,
            ab_split_percent=src.ab_split_percent or 50,
            steps_json=src.steps_json,
            total_runs=0, total_sent=0, total_delivered=0, total_failed=0,
            is_archived=False,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        s.add(copy)
        s.flush()
        s.expunge(copy)
        return copy


def set_campaign_status(campaign_id: int, status: str) -> bool:
    """Transition a campaign to a new status. Returns True if found."""
    with get_db_session() as s:
        c = s.get(BroadcastCampaign, campaign_id)
        if not c:
            return False
        c.status     = status
        c.updated_at = datetime.utcnow()
        return True


def get_campaign_executions(campaign_id: int, limit: int = 20) -> List[CampaignExecution]:
    with get_db_session() as s:
        items = (s.query(CampaignExecution)
                  .filter_by(campaign_id=campaign_id)
                  .order_by(CampaignExecution.created_at.desc())
                  .limit(limit)
                  .all())
        s.expunge_all()
        return items


def get_campaign_dashboard_stats() -> Dict[str, Any]:
    """Aggregate stats for the Campaign Manager dashboard."""
    with get_db_session() as s:
        total    = s.query(BroadcastCampaign).filter_by(is_archived=False).count()
        drafts   = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.DRAFT.value,     is_archived=False).count()
        scheduled = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.SCHEDULED.value, is_archived=False).count()
        running  = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.RUNNING.value,   is_archived=False).count()
        paused   = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.PAUSED.value,    is_archived=False).count()
        completed = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.COMPLETED.value, is_archived=False).count()
        cancelled = s.query(BroadcastCampaign).filter_by(status=CampaignStatus.CANCELLED.value, is_archived=False).count()
        archived = s.query(BroadcastCampaign).filter_by(is_archived=True).count()
        templates_total   = s.query(BroadcastTemplate).count()
        templates_default = s.query(BroadcastTemplate).filter_by(is_default=True).count()
        templates_custom  = templates_total - templates_default
        automation_total  = s.query(BroadcastAutomationRule).count()
        automation_active = s.query(BroadcastAutomationRule).filter_by(is_enabled=True).count()

        agg = s.query(
            func.sum(BroadcastCampaign.total_sent),
            func.sum(BroadcastCampaign.total_delivered),
            func.sum(BroadcastCampaign.total_failed),
        ).first()

    return {
        "total": total,
        "drafts": drafts,
        "scheduled": scheduled,
        "running": running,
        "paused": paused,
        "completed": completed,
        "cancelled": cancelled,
        "archived": archived,
        "templates_total": templates_total,
        "templates_default": templates_default,
        "templates_custom": templates_custom,
        "automation_total": automation_total,
        "automation_active": automation_active,
        "total_sent":      int(agg[0] or 0),
        "total_delivered": int(agg[1] or 0),
        "total_failed":    int(agg[2] or 0),
    }


# ── Automation rule helpers ──────────────────────────────────────────────────

def get_all_automation_rules() -> List[BroadcastAutomationRule]:
    with get_db_session() as s:
        items = (s.query(BroadcastAutomationRule)
                  .order_by(BroadcastAutomationRule.is_enabled.desc(),
                             BroadcastAutomationRule.name)
                  .all())
        s.expunge_all()
        return items


def get_automation_rule(rule_id: int) -> Optional[BroadcastAutomationRule]:
    with get_db_session() as s:
        r = s.get(BroadcastAutomationRule, rule_id)
        if r:
            s.expunge(r)
        return r


def create_automation_rule(name: str, trigger: str, message_text: str,
                           created_by: Optional[int] = None,
                           template_id: Optional[int] = None,
                           delay_minutes: int = 0,
                           target_segment: str = "trigger_user",
                           dedup_window_hours: int = 24,
                           **kwargs) -> BroadcastAutomationRule:
    now = datetime.utcnow()
    with get_db_session() as s:
        r = BroadcastAutomationRule(
            name=name,
            trigger=trigger,
            is_enabled=True,
            template_id=template_id,
            message_text=message_text,
            media_type="text",
            parse_mode="HTML",
            target_segment=target_segment,
            delay_minutes=delay_minutes,
            dedup_window_hours=dedup_window_hours,
            trigger_count=0,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            **kwargs,
        )
        s.add(r)
        s.flush()
        s.expunge(r)
        return r


def update_automation_rule(rule_id: int, **kwargs) -> bool:
    allowed = {
        "name", "trigger", "is_enabled", "template_id", "message_text",
        "media_type", "button_text", "button_url", "parse_mode", "variables_json",
        "conditions_json", "delay_minutes", "target_segment", "dedup_window_hours",
    }
    with get_db_session() as s:
        r = s.get(BroadcastAutomationRule, rule_id)
        if not r:
            return False
        for k, v in kwargs.items():
            if k in allowed:
                setattr(r, k, v)
        r.updated_at = datetime.utcnow()
        return True


def delete_automation_rule(rule_id: int) -> bool:
    with get_db_session() as s:
        r = s.get(BroadcastAutomationRule, rule_id)
        if not r:
            return False
        s.delete(r)
        return True


def toggle_automation_rule(rule_id: int) -> Optional[bool]:
    with get_db_session() as s:
        r = s.get(BroadcastAutomationRule, rule_id)
        if not r:
            return None
        r.is_enabled = not r.is_enabled
        r.updated_at = datetime.utcnow()
        return r.is_enabled


# ── Automation trigger dispatcher ─────────────────────────────────────────────

async def fire_automation_trigger(bot, trigger: str, user_telegram_id: Optional[int] = None,
                                  extra_vars: Optional[Dict[str, Any]] = None) -> int:
    """Dispatch all enabled automation rules for the given trigger event.

    Returns the number of messages sent.

    Deduplication: if the same (rule_id, user_telegram_id) was fired within
    dedup_window_hours, the rule is skipped.
    """
    if not cfg.get_bool("broadcast_automation_enabled", True):
        return 0
    if cfg.get("broadcast_campaign_manager_status", "enabled") != "enabled":
        return 0

    rules: List[BroadcastAutomationRule] = []
    user: Optional[User] = None

    with get_db_session() as s:
        q = (s.query(BroadcastAutomationRule)
               .filter_by(trigger=trigger, is_enabled=True))
        rules_raw = q.all()
        for r in rules_raw:
            s.expunge(r)
        rules = rules_raw

        if user_telegram_id:
            u = s.query(User).filter_by(telegram_id=user_telegram_id).first()
            if u:
                s.expunge(u)
                user = u

    if not rules:
        return 0

    sent_total = 0
    now = datetime.utcnow()

    for rule in rules:
        # Check dedup window
        if user_telegram_id and rule.dedup_window_hours > 0:
            window_start = now - timedelta(hours=rule.dedup_window_hours)
            with get_db_session() as s:
                recent = (s.query(AutomationTriggerLog)
                           .filter(
                               AutomationTriggerLog.rule_id == rule.id,
                               AutomationTriggerLog.user_telegram_id == user_telegram_id,
                               AutomationTriggerLog.triggered_at >= window_start,
                               AutomationTriggerLog.sent.is_(True),
                           )
                           .first())
                if recent:
                    continue  # skip — already triggered for this user in the window

        # Resolve message text
        msg_text = rule.message_text or ""
        if rule.template_id:
            t = get_template(rule.template_id)
            if t:
                msg_text = t.message_text

        if not msg_text:
            continue

        msg_text = substitute_variables(msg_text, user=user, extra=extra_vars)

        # Delay (non-blocking: fire a background task)
        if rule.delay_minutes > 0:
            asyncio.create_task(
                _delayed_send(bot, rule, user_telegram_id, msg_text, rule.delay_minutes, extra_vars)
            )
        else:
            ok = await _send_automation_message(bot, rule, user_telegram_id, msg_text)
            if ok:
                sent_total += 1
                _log_trigger(rule.id, user_telegram_id, sent=True)
                _inc_rule_trigger_count(rule.id)

    return sent_total


async def _delayed_send(bot, rule: BroadcastAutomationRule, user_telegram_id: Optional[int],
                        msg_text: str, delay_minutes: int,
                        extra_vars: Optional[Dict[str, Any]]) -> None:
    await asyncio.sleep(delay_minutes * 60)
    ok = await _send_automation_message(bot, rule, user_telegram_id, msg_text)
    if ok:
        _log_trigger(rule.id, user_telegram_id, sent=True)
        _inc_rule_trigger_count(rule.id)


async def _send_automation_message(bot, rule: BroadcastAutomationRule,
                                   user_telegram_id: Optional[int], msg_text: str) -> bool:
    """Send an automation message to a single user or broadcast audience."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = None
    if rule.button_text and rule.button_url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(rule.button_text, url=rule.button_url)]])

    target_ids: List[int] = []
    if user_telegram_id and rule.target_segment in ("trigger_user", "all"):
        target_ids = [user_telegram_id]
    elif rule.target_segment == "all":
        with get_db_session() as s:
            rows = s.query(User.telegram_id).filter_by(is_banned=False).all()
            target_ids = [r[0] for r in rows]

    sent = 0
    for tid in target_ids:
        try:
            await bot.send_message(
                chat_id=tid,
                text=msg_text,
                parse_mode=rule.parse_mode or "HTML",
                reply_markup=kb,
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.debug("Automation send error for user %s: %s", tid, e)
    return sent > 0


def _log_trigger(rule_id: int, user_telegram_id: Optional[int], sent: bool = True) -> None:
    with get_db_session() as s:
        log = AutomationTriggerLog(
            rule_id=rule_id,
            user_telegram_id=user_telegram_id,
            sent=sent,
            triggered_at=datetime.utcnow(),
        )
        s.add(log)


def _inc_rule_trigger_count(rule_id: int) -> None:
    with get_db_session() as s:
        r = s.get(BroadcastAutomationRule, rule_id)
        if r:
            r.trigger_count     = (r.trigger_count or 0) + 1
            r.last_triggered_at = datetime.utcnow()


# ── Campaign execution ────────────────────────────────────────────────────────

async def execute_campaign(bot, campaign_id: int) -> Dict[str, Any]:
    """Run a campaign immediately. Returns execution summary.

    Handles A/B splitting: variant A gets first ab_split_percent of audience,
    variant B gets the rest.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    campaign = get_campaign(campaign_id)
    if not campaign:
        return {"error": "Campaign not found"}

    # Resolve audience
    with get_db_session() as s:
        rows = s.query(User.telegram_id).filter_by(is_banned=False).all()
        all_ids = [r[0] for r in rows]

    if not all_ids:
        return {"error": "No eligible recipients"}

    now = datetime.utcnow()

    # Create execution record
    with get_db_session() as s:
        exec_rec = CampaignExecution(
            campaign_id=campaign_id,
            step_index=0,
            status="running",
            started_at=now,
            total_recipients=len(all_ids),
            created_at=now,
        )
        s.add(exec_rec)
        s.flush()
        exec_id = exec_rec.id

    # Update campaign status
    set_campaign_status(campaign_id, CampaignStatus.RUNNING.value)

    msg_a = campaign.message_text or ""
    msg_b = campaign.ab_variant_b_text or msg_a if campaign.ab_test_enabled else None

    kb = None
    if campaign.button_text and campaign.button_url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(campaign.button_text, url=campaign.button_url)]])

    # A/B split
    split = (campaign.ab_split_percent or 50) / 100.0
    if campaign.ab_test_enabled and msg_b:
        split_idx = int(len(all_ids) * split)
        ids_a = all_ids[:split_idx]
        ids_b = all_ids[split_idx:]
    else:
        ids_a = all_ids
        ids_b = []

    sent = 0
    delivered = 0
    failed = 0
    sent_a = 0
    sent_b = 0

    for tid in ids_a:
        try:
            await bot.send_message(tid, msg_a, parse_mode=campaign.parse_mode or "HTML",
                                   reply_markup=kb)
            sent += 1
            delivered += 1
            sent_a += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    for tid in ids_b:
        try:
            await bot.send_message(tid, msg_b, parse_mode=campaign.parse_mode or "HTML",
                                   reply_markup=kb)
            sent += 1
            delivered += 1
            sent_b += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    finished = datetime.utcnow()

    # Update execution record
    with get_db_session() as s:
        er = s.get(CampaignExecution, exec_id)
        if er:
            er.status     = "completed"
            er.finished_at = finished
            er.sent       = sent
            er.delivered  = delivered
            er.failed     = failed
            er.ab_sent_a  = sent_a
            er.ab_sent_b  = sent_b

    # Update campaign stats
    with get_db_session() as s:
        c = s.get(BroadcastCampaign, campaign_id)
        if c:
            c.total_runs      = (c.total_runs or 0) + 1
            c.total_sent      = (c.total_sent or 0) + sent
            c.total_delivered = (c.total_delivered or 0) + delivered
            c.total_failed    = (c.total_failed or 0) + failed
            c.last_run_at     = now
            # Compute next run for recurring campaigns
            nxt = _compute_next_run(c)
            c.next_run_at = nxt
            c.status = CampaignStatus.RUNNING.value if nxt else CampaignStatus.COMPLETED.value
            c.updated_at = finished

    if campaign.template_id:
        increment_template_usage(campaign.template_id)

    return {
        "sent": sent,
        "delivered": delivered,
        "failed": failed,
        "sent_a": sent_a,
        "sent_b": sent_b,
        "duration_s": (finished - now).total_seconds(),
    }


def _compute_next_run(campaign: BroadcastCampaign) -> Optional[datetime]:
    """Compute next_run_at for a recurring campaign after a successful run.

    Returns None if the campaign is one-shot or has passed its end_date.
    """
    stype = campaign.schedule_type
    if not stype:
        return None

    now = datetime.utcnow()
    base = campaign.last_run_at or now

    if stype == "daily":
        nxt = base + timedelta(days=1)
    elif stype == "weekly":
        nxt = base + timedelta(weeks=1)
    elif stype == "monthly":
        month = base.month + 1
        year  = base.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        try:
            nxt = base.replace(year=year, month=month)
        except ValueError:
            # Handle Feb 30, etc.
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            nxt = base.replace(year=year, month=month, day=min(base.day, last_day))
    elif stype == "custom" and campaign.schedule_interval_hours:
        nxt = base + timedelta(hours=campaign.schedule_interval_hours)
    else:
        return None

    if campaign.end_date and nxt > campaign.end_date:
        return None
    return nxt


# ── Campaign scheduler job ────────────────────────────────────────────────────

async def campaign_scheduler_job(context) -> None:
    """PTB job that fires due campaigns every 60 seconds."""
    if not cfg.get_bool("broadcast_campaigns_enabled", True):
        return
    if cfg.get("broadcast_campaign_manager_status", "enabled") != "enabled":
        return

    max_running = cfg.get_int("broadcast_campaign_max_running", 3)
    now = datetime.utcnow()

    with get_db_session() as s:
        # Count currently running
        running_cnt = (s.query(BroadcastCampaign)
                        .filter_by(status=CampaignStatus.RUNNING.value)
                        .count())
        if running_cnt >= max_running:
            return

        slots = max_running - running_cnt
        due = (s.query(BroadcastCampaign)
                .filter(
                    BroadcastCampaign.status == CampaignStatus.SCHEDULED.value,
                    BroadcastCampaign.next_run_at <= now,
                    BroadcastCampaign.is_archived.is_(False),
                )
                .order_by(BroadcastCampaign.next_run_at)
                .limit(slots)
                .all())
        ids_to_run = [c.id for c in due]

    for cid in ids_to_run:
        try:
            await execute_campaign(context.bot, cid)
        except Exception:
            logger.exception("Campaign scheduler error for campaign %d", cid)
