"""Admin Broadcast Campaign Manager — V44.4.

Callback namespace: ``bcm:*``

Provides the full admin UI for:
  • 📢 Campaign Manager  — list, create, view, run, pause, resume, cancel, archive, delete, duplicate
  • 📄 Template Library  — list, create, view, edit, duplicate, delete, favorite, search, groups
  • 🤖 Automation Rules  — list, create, view, toggle, delete
  • 📅 Campaign Scheduler — due/running campaign overview
  • ⚙️ Settings          — feature toggles, limits

All handlers guard against non-admin callers via ``_is_admin()``.
The module never modifies existing Broadcast Center functionality — it only
adds new capabilities on top.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from config.settings import settings
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from utils.update_proxy import with_data

from services.broadcast_campaign_service import (
    # Templates
    get_all_templates, get_template, create_template, update_template,
    delete_template, duplicate_template, toggle_template_favorite,
    get_template_groups, increment_template_usage, SUPPORTED_VARIABLES,
    # Campaigns
    get_all_campaigns, get_campaign, create_campaign, update_campaign,
    delete_campaign, duplicate_campaign, set_campaign_status,
    get_campaign_executions, get_campaign_dashboard_stats, execute_campaign,
    # Automation rules
    get_all_automation_rules, get_automation_rule, create_automation_rule,
    update_automation_rule, delete_automation_rule, toggle_automation_rule,
)

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
(
    BCM_CAMPAIGN_NAME,      # 0
    BCM_CAMPAIGN_TYPE,      # 1
    BCM_CAMPAIGN_MSG,       # 2
    BCM_CAMPAIGN_SCHED,     # 3
    BCM_CAMPAIGN_CONFIRM,   # 4
    BCM_TEMPLATE_NAME,      # 5
    BCM_TEMPLATE_MSG,       # 6
    BCM_TEMPLATE_CONFIRM,   # 7
    BCM_RULE_NAME,          # 8
    BCM_RULE_TRIGGER,       # 9
    BCM_RULE_MSG,           # 10
    BCM_RULE_CONFIRM,       # 11
    BCM_SEARCH_INPUT,       # 12
    BCM_AB_VARIANT_B,       # 13
    BCM_EDIT_FIELD,         # 14
) = range(15)

_NS = "bcm"
PAGE_SIZE = 8

# ── Campaign type definitions ──────────────────────────────────────────────────

CAMPAIGN_TYPES: List[Tuple[str, str, str]] = [
    ("single",     "📨 Single",     "One-time message to audience"),
    ("multi_step", "📋 Multi-Step", "Series of messages sent in sequence"),
    ("scheduled",  "📅 Scheduled",  "Sent at a specific date and time"),
    ("recurring",  "🔄 Recurring",  "Repeats daily / weekly / monthly / custom"),
    ("drip",       "💧 Drip",       "Timed sequence triggered by user action"),
    ("seasonal",   "🌸 Seasonal",   "Runs between start and end dates"),
]

CAMPAIGN_TYPE_LABELS = {k: v for k, v, _ in CAMPAIGN_TYPES}

SCHEDULE_TYPES: List[Tuple[str, str]] = [
    ("daily",   "📅 Daily"),
    ("weekly",  "📅 Weekly"),
    ("monthly", "📅 Monthly"),
    ("custom",  "⚙️ Custom Interval"),
]

# ── Automation trigger definitions ─────────────────────────────────────────────

AUTOMATION_TRIGGERS: List[Tuple[str, str]] = [
    ("new_user",             "👤 New User Registers"),
    ("first_purchase",       "🛍 First Purchase"),
    ("user_vip",             "⭐ User Becomes VIP"),
    ("wallet_deposit",       "💰 Wallet Deposit Completed"),
    ("wallet_low",           "🪙 Wallet Balance Below Limit"),
    ("product_restocked",    "📦 Product Restocked"),
    ("product_price_drop",   "📉 Product Price Reduced"),
    ("coupon_created",       "🎟 Coupon Created"),
    ("coupon_expiring",      "⏰ Coupon Expiring"),
    ("subscription_expiring","🔔 Subscription Expiring"),
    ("subscription_expired", "❌ Subscription Expired"),
    ("referral_reward",      "🤝 Referral Reward Earned"),
    ("flash_sale_started",   "🔥 Flash Sale Started"),
    ("flash_sale_ending",    "⏳ Flash Sale Ending"),
]

TRIGGER_LABELS = {k: v for k, v in AUTOMATION_TRIGGERS}

STATUS_ICONS: Dict[str, str] = {
    "draft":     "📝",
    "scheduled": "⏰",
    "running":   "📤",
    "paused":    "⏸",
    "completed": "✅",
    "cancelled": "❌",
    "archived":  "🗄",
}

# ── Auth / feature guards ──────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return (uid == settings.ADMIN_TELEGRAM_ID
            or has_permission(uid, "manage_broadcasts"))


def _feature_enabled() -> bool:
    status = cfg.get("broadcast_campaign_manager_status", "enabled")
    return status == "enabled"


def _feature_maintenance() -> bool:
    return cfg.get("broadcast_campaign_manager_status", "enabled") == "maintenance"


# ── Shared UI helpers ──────────────────────────────────────────────────────────

def _back_kb(cb: str = "bcm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML") -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug("_safe_edit error: %s", e)


def _pager_row(base_cb: str, page: int, total: int, page_size: int) -> List[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{base_cb}:{page - 1}"))
    if (page + 1) * page_size < total:
        row.append(InlineKeyboardButton("➡️ Next", callback_data=f"{base_cb}:{page + 1}"))
    return row


# ── Root menu ──────────────────────────────────────────────────────────────────

async def bcm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main Campaign Manager menu (callback: bcm:menu)."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    if _feature_maintenance():
        await _safe_edit(query,
            "🟡 <b>Broadcast Campaign Manager</b>\n\nCurrently under maintenance.",
            _back_kb("acc:sec:broadcast"))
        return

    if not _feature_enabled():
        await _safe_edit(query,
            "🔴 <b>Broadcast Campaign Manager</b>\n\nThis feature is disabled.",
            _back_kb("acc:sec:broadcast"))
        return

    stats = get_campaign_dashboard_stats()

    text = (
        "📢 <b>Broadcast Campaign Manager</b>\n\n"
        f"Campaigns: <b>{stats['total']}</b>  "
        f"(Running: {stats['running']} | Scheduled: {stats['scheduled']} | Drafts: {stats['drafts']})\n"
        f"Templates: <b>{stats['templates_total']}</b>  "
        f"(Custom: {stats['templates_custom']} | Default: {stats['templates_default']})\n"
        f"Automation Rules: <b>{stats['automation_total']}</b>  "
        f"(Active: {stats['automation_active']})\n\n"
        f"Total Sent: <b>{stats['total_sent']:,}</b>  "
        f"Delivered: <b>{stats['total_delivered']:,}</b>  "
        f"Failed: <b>{stats['total_failed']:,}</b>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Campaign Manager",   callback_data="bcm:campaigns:0")],
        [InlineKeyboardButton("➕ New Campaign",       callback_data="bcm:campaign:new")],
        [InlineKeyboardButton("📄 Template Library",  callback_data="bcm:templates:0")],
        [InlineKeyboardButton("🤖 Automation Rules",  callback_data="bcm:automation")],
        [InlineKeyboardButton("📅 Scheduler View",    callback_data="bcm:scheduler")],
        [InlineKeyboardButton("⚙️ Settings",          callback_data="bcm:settings")],
        [InlineKeyboardButton("🔙 Back",               callback_data="acc:sec:broadcast")],
    ])
    await _safe_edit(query, text, kb)


# ═══════════════════════════════════════════════════════════════════════════════
# Campaign Manager
# ═══════════════════════════════════════════════════════════════════════════════

async def bcm_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List campaigns (callback: bcm:campaigns:<page>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    page = int(query.data.split(":")[-1])
    items, total = get_all_campaigns(page=page, page_size=PAGE_SIZE)

    text_lines = [f"📢 <b>Campaigns</b> — {total} total\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for c in items:
        icon = STATUS_ICONS.get(c.status, "•")
        ctype = CAMPAIGN_TYPE_LABELS.get(c.campaign_type, c.campaign_type)
        text_lines.append(f"{icon} <b>{c.name}</b> [{ctype}]")
        rows.append([InlineKeyboardButton(
            f"{icon} {c.name}", callback_data=f"bcm:campaign:view:{c.id}"
        )])

    if not items:
        text_lines.append("No campaigns yet. Create one with ➕ New Campaign.")

    pager = _pager_row("bcm:campaigns", page, total, PAGE_SIZE)
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton("➕ New Campaign", callback_data="bcm:campaign:new")])
    rows.append([InlineKeyboardButton("🔙 Back",         callback_data="bcm:menu")])

    await _safe_edit(query, "\n".join(text_lines), InlineKeyboardMarkup(rows))


async def bcm_campaign_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View a single campaign (callback: bcm:campaign:view:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    campaign_id = int(query.data.split(":")[-1])
    c = get_campaign(campaign_id)
    if not c:
        await query.answer("❌ Campaign not found.", show_alert=True)
        return

    icon  = STATUS_ICONS.get(c.status, "•")
    ctype = CAMPAIGN_TYPE_LABELS.get(c.campaign_type, c.campaign_type)
    sched_info = ""
    if c.schedule_type:
        sched_info = f"\n📅 Schedule: <b>{c.schedule_type}</b>"
        if c.start_date:
            sched_info += f"\n▶️ Start: <b>{c.start_date.strftime('%Y-%m-%d %H:%M')}</b>"
        if c.end_date:
            sched_info += f"\n⏹ End: <b>{c.end_date.strftime('%Y-%m-%d %H:%M')}</b>"
        if c.next_run_at:
            sched_info += f"\n⏰ Next run: <b>{c.next_run_at.strftime('%Y-%m-%d %H:%M')}</b>"

    ab_info = ""
    if c.ab_test_enabled:
        winner = f" (Winner: <b>{c.ab_winner}</b>)" if c.ab_winner else ""
        ab_info = f"\n🔬 A/B Test: <b>Enabled</b> — {c.ab_split_percent}%/{100 - c.ab_split_percent}%{winner}"

    text = (
        f"{icon} <b>{c.name}</b>\n"
        f"Type: <b>{ctype}</b>  |  Status: <b>{c.status}</b>\n"
        f"Target: <b>{c.target_segment}</b>\n"
        f"Runs: <b>{c.total_runs}</b>  Sent: <b>{c.total_sent:,}</b>  "
        f"Delivered: <b>{c.total_delivered:,}</b>  Failed: <b>{c.total_failed:,}</b>"
        f"{sched_info}{ab_info}\n\n"
        f"Created: {c.created_at.strftime('%Y-%m-%d') if c.created_at else '—'}"
    )

    rows: List[List[InlineKeyboardButton]] = []

    # Action buttons based on status
    if c.status == "draft":
        rows.append([
            InlineKeyboardButton("🚀 Run Now",   callback_data=f"bcm:campaign:run:{c.id}"),
            InlineKeyboardButton("📅 Schedule",  callback_data=f"bcm:campaign:schedule:{c.id}"),
        ])
    elif c.status == "scheduled":
        rows.append([
            InlineKeyboardButton("🚀 Run Now",   callback_data=f"bcm:campaign:run:{c.id}"),
            InlineKeyboardButton("❌ Cancel",    callback_data=f"bcm:campaign:cancel:{c.id}"),
        ])
    elif c.status == "running":
        rows.append([
            InlineKeyboardButton("⏸ Pause",    callback_data=f"bcm:campaign:pause:{c.id}"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"bcm:campaign:cancel:{c.id}"),
        ])
    elif c.status == "paused":
        rows.append([
            InlineKeyboardButton("▶️ Resume",   callback_data=f"bcm:campaign:resume:{c.id}"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"bcm:campaign:cancel:{c.id}"),
        ])
    elif c.status in ("completed", "cancelled", "failed"):
        rows.append([
            InlineKeyboardButton("🔄 Run Again", callback_data=f"bcm:campaign:run:{c.id}"),
        ])

    # Common actions
    rows.append([
        InlineKeyboardButton("📋 Duplicate", callback_data=f"bcm:campaign:dup:{c.id}"),
        InlineKeyboardButton("📊 History",   callback_data=f"bcm:campaign:history:{c.id}"),
    ])
    if not c.is_archived:
        rows.append([
            InlineKeyboardButton("🗄 Archive",   callback_data=f"bcm:campaign:archive:{c.id}"),
            InlineKeyboardButton("🗑 Delete",    callback_data=f"bcm:campaign:del_ask:{c.id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("📤 Unarchive", callback_data=f"bcm:campaign:unarchive:{c.id}"),
            InlineKeyboardButton("🗑 Delete",    callback_data=f"bcm:campaign:del_ask:{c.id}"),
        ])

    rows.append([InlineKeyboardButton("🔙 Back", callback_data="bcm:campaigns:0")])
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def bcm_campaign_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a campaign immediately (callback: bcm:campaign:run:<id>)."""
    query = update.callback_query
    await query.answer("⏳ Starting campaign…")
    if not _is_admin(update.effective_user.id):
        return

    campaign_id = int(query.data.split(":")[-1])
    c = get_campaign(campaign_id)
    if not c:
        await query.answer("❌ Campaign not found.", show_alert=True)
        return

    max_running = cfg.get_int("broadcast_campaign_max_running", 3)
    from database import get_db_session
    from database.models import BroadcastCampaign as _BC, CampaignStatus as _CS
    with get_db_session() as s:
        running_cnt = s.query(_BC).filter_by(status=_CS.RUNNING.value).count()
    if running_cnt >= max_running:
        await query.answer(
            f"⚠️ Maximum running campaigns reached ({max_running}). "
            "Wait for one to complete before starting another.",
            show_alert=True,
        )
        return

    # Execute in background so the callback returns quickly
    async def _run_in_bg():
        try:
            result = await execute_campaign(query.bot, campaign_id)
            logger.info("Campaign %d finished: %s", campaign_id, result)
        except Exception:
            logger.exception("Campaign %d execution error", campaign_id)

    asyncio.create_task(_run_in_bg())
    log_admin_action(update.effective_user.id, "campaign_run", f"Campaign ID {campaign_id}")
    await query.answer("✅ Campaign started in background.", show_alert=True)
    # Refresh view
    context.user_data["_bcm_view_id"] = campaign_id
    await bcm_campaign_view(update, context)


async def bcm_campaign_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a running campaign (callback: bcm:campaign:pause:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    set_campaign_status(campaign_id, "paused")
    log_admin_action(update.effective_user.id, "campaign_pause", f"ID {campaign_id}")
    await query.answer("⏸ Campaign paused.", show_alert=True)
    await bcm_campaign_view(update, context)


async def bcm_campaign_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume a paused campaign (callback: bcm:campaign:resume:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    set_campaign_status(campaign_id, "scheduled")
    log_admin_action(update.effective_user.id, "campaign_resume", f"ID {campaign_id}")
    await query.answer("▶️ Campaign resumed.", show_alert=True)
    await bcm_campaign_view(update, context)


async def bcm_campaign_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a campaign (callback: bcm:campaign:cancel:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    set_campaign_status(campaign_id, "cancelled")
    log_admin_action(update.effective_user.id, "campaign_cancel", f"ID {campaign_id}")
    await query.answer("❌ Campaign cancelled.", show_alert=True)
    await bcm_campaign_view(update, context)


async def bcm_campaign_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Archive a campaign (callback: bcm:campaign:archive:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    update_campaign(campaign_id, is_archived=True, status="archived")
    log_admin_action(update.effective_user.id, "campaign_archive", f"ID {campaign_id}")
    await query.answer("🗄 Campaign archived.", show_alert=True)
    await bcm_campaigns(update, context)


async def bcm_campaign_unarchive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unarchive a campaign (callback: bcm:campaign:unarchive:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    update_campaign(campaign_id, is_archived=False, status="draft")
    await query.answer("📤 Campaign unarchived.", show_alert=True)
    await bcm_campaign_view(update, context)


async def bcm_campaign_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for delete confirmation (callback: bcm:campaign:del_ask:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    c = get_campaign(campaign_id)
    if not c:
        await query.answer("❌ Campaign not found.", show_alert=True)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"bcm:campaign:del_ok:{campaign_id}")],
        [InlineKeyboardButton("❌ Cancel",       callback_data=f"bcm:campaign:view:{campaign_id}")],
    ])
    await _safe_edit(query, f"⚠️ Delete campaign <b>{c.name}</b>?\n\nThis cannot be undone.", kb)


async def bcm_campaign_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm delete (callback: bcm:campaign:del_ok:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    delete_campaign(campaign_id)
    log_admin_action(update.effective_user.id, "campaign_delete", f"ID {campaign_id}")
    await query.answer("🗑 Campaign deleted.", show_alert=True)
    await bcm_campaigns(update, context)


async def bcm_campaign_dup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Duplicate a campaign (callback: bcm:campaign:dup:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    campaign_id = int(query.data.split(":")[-1])
    copy = duplicate_campaign(campaign_id, created_by=update.effective_user.id)
    if not copy:
        await query.answer("❌ Campaign not found.", show_alert=True)
        return
    log_admin_action(update.effective_user.id, "campaign_duplicate", f"ID {campaign_id} → {copy.id}")
    await query.answer("📋 Campaign duplicated!", show_alert=True)
    # Redirect to copy
    await bcm_campaign_view(with_data(update, f"bcm:campaign:view:{copy.id}"), context)


async def bcm_campaign_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show execution history for a campaign (callback: bcm:campaign:history:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    campaign_id = int(query.data.split(":")[-1])
    c = get_campaign(campaign_id)
    if not c:
        await query.answer("❌ Not found.", show_alert=True)
        return

    execs = get_campaign_executions(campaign_id, limit=10)
    lines = [f"📊 <b>Execution History — {c.name}</b>\n"]
    if not execs:
        lines.append("No execution records yet.")
    else:
        for e in execs:
            ts   = e.started_at.strftime("%Y-%m-%d %H:%M") if e.started_at else "—"
            dur  = ""
            if e.started_at and e.finished_at:
                s = (e.finished_at - e.started_at).total_seconds()
                dur = f" ({int(s)}s)"
            ab_note = f" [A:{e.ab_sent_a} B:{e.ab_sent_b}]" if (e.ab_sent_a or e.ab_sent_b) else ""
            lines.append(
                f"• {ts}{dur} — Sent: {e.sent} | Del: {e.delivered} | Fail: {e.failed}{ab_note}"
            )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=f"bcm:campaign:view:{campaign_id}")],
    ])
    await _safe_edit(query, "\n".join(lines), kb)


# ── New Campaign conversation ──────────────────────────────────────────────────

async def bcm_campaign_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for new-campaign flow (callback: bcm:campaign:new)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    if not _feature_enabled():
        await query.answer("🔴 Feature disabled.", show_alert=True)
        return ConversationHandler.END

    max_total = cfg.get_int("broadcast_campaign_max_total", 100)
    if max_total > 0:
        _, total = get_all_campaigns()
        if total >= max_total:
            await query.answer(
                f"⚠️ Maximum campaigns ({max_total}) reached. Archive some to create new ones.",
                show_alert=True,
            )
            return ConversationHandler.END

    context.user_data["_bcm_new"] = {}
    await _safe_edit(query,
        "📢 <b>New Campaign — Step 1/4</b>\n\n"
        "Enter a name for this campaign:\n"
        "<i>e.g. July Flash Sale, Welcome Drip, VIP Rewards</i>",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_CAMPAIGN_NAME


async def bcm_campaign_recv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name or len(name) > 100:
        await update.message.reply_text("⚠️ Name must be 1-100 characters. Try again:")
        return BCM_CAMPAIGN_NAME

    context.user_data["_bcm_new"]["name"] = name

    type_rows = []
    for key, label, desc in CAMPAIGN_TYPES:
        type_rows.append([InlineKeyboardButton(
            f"{label} — {desc}", callback_data=f"bcm:campaign:type:{key}"
        )])
    type_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")])

    await update.message.reply_text(
        "📢 <b>New Campaign — Step 2/4</b>\n\nSelect campaign type:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(type_rows),
    )
    return BCM_CAMPAIGN_TYPE


async def bcm_campaign_recv_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctype = query.data.split(":")[-1]
    context.user_data["_bcm_new"]["campaign_type"] = ctype
    await _safe_edit(query,
        "📢 <b>New Campaign — Step 3/4</b>\n\n"
        "Enter the campaign message text.\n"
        "Supported variables:\n"
        + "  ".join(SUPPORTED_VARIABLES[:8]) + "\n"
        + "  ".join(SUPPORTED_VARIABLES[8:]) + "\n\n"
        "<i>HTML formatting supported.</i>",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_CAMPAIGN_MSG


async def bcm_campaign_recv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Message cannot be empty. Try again:")
        return BCM_CAMPAIGN_MSG

    context.user_data["_bcm_new"]["message_text"] = text

    # A/B option if enabled
    ab_kb_rows = [
        [
            InlineKeyboardButton("✅ Enable A/B Test", callback_data="bcm:campaign:ab:yes"),
            InlineKeyboardButton("⏭ Skip",            callback_data="bcm:campaign:ab:no"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")],
    ]
    if cfg.get_bool("broadcast_ab_testing_enabled", True):
        await update.message.reply_text(
            "📢 <b>New Campaign — A/B Testing</b>\n\n"
            "Would you like to enable A/B testing for this campaign?\n"
            "<i>You will provide a Variant B message next.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(ab_kb_rows),
        )
        return BCM_AB_VARIANT_B
    else:
        return await _save_campaign(update, context, ab=False)


async def bcm_campaign_ab_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[-1]
    if choice == "yes":
        await _safe_edit(query,
            "📢 <b>A/B Test — Variant B Message</b>\n\n"
            "Enter the alternate (Variant B) message text:",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
        )
        context.user_data["_bcm_new"]["ab_test_enabled"] = True
        return BCM_AB_VARIANT_B
    else:
        return await _save_campaign_from_cb(update, context)


async def bcm_campaign_recv_variant_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text:
        context.user_data["_bcm_new"]["ab_variant_b_text"] = text
    return await _save_campaign(update, context, ab=bool(text))


async def _save_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE, ab: bool = False) -> int:
    data = context.user_data.pop("_bcm_new", {})
    if not ab:
        data.pop("ab_variant_b_text", None)
        data["ab_test_enabled"] = False
    else:
        data["ab_test_enabled"] = True

    c = create_campaign(
        name=data.get("name", "Untitled"),
        campaign_type=data.get("campaign_type", "single"),
        created_by=update.effective_user.id,
        message_text=data.get("message_text"),
        ab_test_enabled=data.get("ab_test_enabled", False),
        ab_variant_b_text=data.get("ab_variant_b_text"),
    )
    log_admin_action(update.effective_user.id, "campaign_create", f"ID {c.id} — {c.name}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Campaign",  callback_data=f"bcm:campaign:view:{c.id}")],
        [InlineKeyboardButton("🚀 Run Now",        callback_data=f"bcm:campaign:run:{c.id}")],
        [InlineKeyboardButton("🔙 Campaigns",      callback_data="bcm:campaigns:0")],
    ])
    await update.message.reply_text(
        f"✅ <b>Campaign created!</b>\n\n"
        f"Name: <b>{c.name}</b>\n"
        f"Type: <b>{CAMPAIGN_TYPE_LABELS.get(c.campaign_type, c.campaign_type)}</b>\n"
        f"A/B Test: <b>{'Yes' if c.ab_test_enabled else 'No'}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def _save_campaign_from_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Helper for saving campaign from a callback query context."""
    data = context.user_data.pop("_bcm_new", {})
    data["ab_test_enabled"] = False
    c = create_campaign(
        name=data.get("name", "Untitled"),
        campaign_type=data.get("campaign_type", "single"),
        created_by=update.effective_user.id,
        message_text=data.get("message_text"),
        ab_test_enabled=False,
    )
    log_admin_action(update.effective_user.id, "campaign_create", f"ID {c.id} — {c.name}")
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Campaign", callback_data=f"bcm:campaign:view:{c.id}")],
        [InlineKeyboardButton("🔙 Campaigns",    callback_data="bcm:campaigns:0")],
    ])
    await _safe_edit(query,
        f"✅ <b>Campaign created!</b>\n\nName: <b>{c.name}</b>",
        kb)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Template Library
# ═══════════════════════════════════════════════════════════════════════════════

async def bcm_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Template library list (callback: bcm:templates:<page>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    if not cfg.get_bool("broadcast_templates_enabled", True):
        await _safe_edit(query, "🔴 Template Library is disabled.", _back_kb("bcm:menu"))
        return

    page = int(query.data.split(":")[-1])
    all_t = get_all_templates()
    total = len(all_t)
    items = all_t[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"📄 <b>Template Library</b> — {total} templates\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for t in items:
        fav  = "⭐" if t.is_favorite else ""
        dflt = "🔒" if t.is_default else ""
        cat  = f"[{t.category}]" if t.category else ""
        lines.append(f"{fav}{dflt} <b>{t.name}</b> {cat}")
        rows.append([InlineKeyboardButton(
            f"{fav}{dflt} {t.name}", callback_data=f"bcm:template:view:{t.id}"
        )])

    if not items:
        lines.append("No templates yet. Create one with ➕ New Template.")

    pager = _pager_row("bcm:templates", page, total, PAGE_SIZE)
    if pager:
        rows.append(pager)
    rows.append([
        InlineKeyboardButton("➕ New Template",  callback_data="bcm:template:new"),
        InlineKeyboardButton("⭐ Favorites",     callback_data="bcm:templates:fav"),
    ])
    rows.append([
        InlineKeyboardButton("🔍 Search",        callback_data="bcm:template:search"),
        InlineKeyboardButton("📂 Groups",        callback_data="bcm:templates:groups"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="bcm:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def bcm_templates_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show favorited templates (callback: bcm:templates:fav)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    favs = get_all_templates(favorites_only=True)
    lines = [f"⭐ <b>Favorite Templates</b> — {len(favs)}\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for t in favs:
        lines.append(f"⭐ <b>{t.name}</b>")
        rows.append([InlineKeyboardButton(f"⭐ {t.name}", callback_data=f"bcm:template:view:{t.id}")])

    if not favs:
        lines.append("No favorites yet. Open a template and press ⭐ to favorite it.")

    rows.append([InlineKeyboardButton("🔙 Back", callback_data="bcm:templates:0")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def bcm_templates_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show template groups (callback: bcm:templates:groups)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    groups = get_template_groups()
    lines  = ["📂 <b>Template Groups</b>\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for grp, cnt in sorted(groups.items()):
        lines.append(f"• <b>{grp}</b> — {cnt} templates")
        rows.append([InlineKeyboardButton(
            f"📂 {grp} ({cnt})", callback_data=f"bcm:templates:group:{grp}"
        )])

    if not groups:
        lines.append("No groups yet. Assign a Group when creating or editing templates.")

    rows.append([InlineKeyboardButton("🔙 Back", callback_data="bcm:templates:0")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def bcm_template_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View a single template (callback: bcm:template:view:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    template_id = int(query.data.split(":")[-1])
    t = get_template(template_id)
    if not t:
        await query.answer("❌ Template not found.", show_alert=True)
        return

    fav_label = "⭐ Unfavorite" if t.is_favorite else "☆ Favorite"
    fav_icon  = "⭐" if t.is_favorite else ""
    dflt      = " 🔒 Built-in" if t.is_default else ""
    text = (
        f"{fav_icon} <b>{t.name}</b>{dflt}\n"
        f"Category: <b>{t.category or '—'}</b>  |  Group: <b>{t.group_name or '—'}</b>\n"
        f"Used: <b>{t.usage_count}</b> times\n\n"
        f"<b>Message:</b>\n{t.message_text[:800]}{'…' if len(t.message_text) > 800 else ''}"
    )
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(fav_label,          callback_data=f"bcm:template:fav:{t.id}"),
            InlineKeyboardButton("📋 Duplicate",     callback_data=f"bcm:template:dup:{t.id}"),
        ],
        [
            InlineKeyboardButton("🚀 Use in Campaign", callback_data=f"bcm:template:use:{t.id}"),
        ],
    ]
    if not t.is_default:
        rows.append([
            InlineKeyboardButton("✏️ Edit",           callback_data=f"bcm:template:edit:{t.id}"),
            InlineKeyboardButton("🗑 Delete",          callback_data=f"bcm:template:del_ask:{t.id}"),
        ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="bcm:templates:0")])
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def bcm_template_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle template favorite (callback: bcm:template:fav:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    template_id = int(query.data.split(":")[-1])
    new_state = toggle_template_favorite(template_id)
    if new_state is None:
        await query.answer("❌ Template not found.", show_alert=True)
        return
    icon = "⭐" if new_state else "☆"
    await query.answer(f"{icon} {'Added to' if new_state else 'Removed from'} favorites.")
    await bcm_template_view(with_data(update, f"bcm:template:view:{template_id}"), context)


async def bcm_template_dup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Duplicate a template (callback: bcm:template:dup:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    template_id = int(query.data.split(":")[-1])
    copy = duplicate_template(template_id, created_by=update.effective_user.id)
    if not copy:
        await query.answer("❌ Template not found.", show_alert=True)
        return
    log_admin_action(update.effective_user.id, "template_duplicate", f"ID {template_id} → {copy.id}")
    await query.answer("📋 Template duplicated!", show_alert=True)
    await bcm_template_view(with_data(update, f"bcm:template:view:{copy.id}"), context)


async def bcm_template_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm template deletion (callback: bcm:template:del_ask:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    template_id = int(query.data.split(":")[-1])
    t = get_template(template_id)
    if not t:
        await query.answer("❌ Not found.", show_alert=True)
        return
    if t.is_default:
        await query.answer("🔒 Built-in templates cannot be deleted.", show_alert=True)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"bcm:template:del_ok:{template_id}")],
        [InlineKeyboardButton("❌ Cancel",       callback_data=f"bcm:template:view:{template_id}")],
    ])
    await _safe_edit(query, f"⚠️ Delete template <b>{t.name}</b>?\n\nThis cannot be undone.", kb)


async def bcm_template_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm template deletion (callback: bcm:template:del_ok:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    template_id = int(query.data.split(":")[-1])
    delete_template(template_id)
    log_admin_action(update.effective_user.id, "template_delete", f"ID {template_id}")
    await query.answer("🗑 Template deleted.", show_alert=True)
    await bcm_templates(with_data(update, "bcm:templates:0"), context)


async def bcm_template_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Use a template in a new campaign (callback: bcm:template:use:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    template_id = int(query.data.split(":")[-1])
    t = get_template(template_id)
    if not t:
        await query.answer("❌ Template not found.", show_alert=True)
        return
    increment_template_usage(template_id)
    # Pre-fill campaign creation with template content
    context.user_data["_bcm_new"] = {
        "message_text": t.message_text,
        "template_id":  t.id,
    }
    await _safe_edit(query,
        f"📢 <b>New Campaign from Template</b>\n\n"
        f"Template: <b>{t.name}</b>\n\n"
        "Enter a name for this campaign:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    # Transition into creation flow
    return BCM_CAMPAIGN_NAME


# ── New Template conversation ──────────────────────────────────────────────────

async def bcm_template_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry for new-template flow (callback: bcm:template:new)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    if not cfg.get_bool("broadcast_templates_enabled", True):
        await query.answer("🔴 Template Library is disabled.", show_alert=True)
        return ConversationHandler.END

    max_t = cfg.get_int("broadcast_template_max", 200)
    if max_t > 0:
        total = len(get_all_templates())
        if total >= max_t:
            await query.answer(f"⚠️ Template limit reached ({max_t}).", show_alert=True)
            return ConversationHandler.END

    context.user_data["_bcm_tmpl"] = {}
    await _safe_edit(query,
        "📄 <b>New Template — Step 1/2</b>\n\nEnter a name for this template:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_TEMPLATE_NAME


async def bcm_template_recv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name or len(name) > 100:
        await update.message.reply_text("⚠️ Name must be 1-100 characters. Try again:")
        return BCM_TEMPLATE_NAME

    context.user_data["_bcm_tmpl"]["name"] = name
    await update.message.reply_text(
        "📄 <b>New Template — Step 2/2</b>\n\n"
        "Enter the message text for this template.\n"
        "Supported variables:\n"
        + "  ".join(SUPPORTED_VARIABLES[:8]) + "\n"
        + "  ".join(SUPPORTED_VARIABLES[8:]) + "\n\n"
        "<i>HTML formatting supported.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_TEMPLATE_MSG


async def bcm_template_recv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Message cannot be empty. Try again:")
        return BCM_TEMPLATE_MSG

    data = context.user_data.pop("_bcm_tmpl", {})
    t = create_template(
        name=data.get("name", "Untitled"),
        message_text=text,
        created_by=update.effective_user.id,
    )
    log_admin_action(update.effective_user.id, "template_create", f"ID {t.id} — {t.name}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Template", callback_data=f"bcm:template:view:{t.id}")],
        [InlineKeyboardButton("🔙 Templates",    callback_data="bcm:templates:0")],
    ])
    await update.message.reply_text(
        f"✅ <b>Template created!</b>\n\nName: <b>{t.name}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ── Template search conversation ───────────────────────────────────────────────

async def bcm_template_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start template search (callback: bcm:template:search)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    await _safe_edit(query,
        "🔍 <b>Search Templates</b>\n\nType keywords to search by name:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_SEARCH_INPUT


async def bcm_template_search_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kw = (update.message.text or "").strip()
    results = get_all_templates(search=kw) if kw else get_all_templates()

    lines = [f"🔍 <b>Search: '{kw}'</b> — {len(results)} results\n"]
    rows: List[List[InlineKeyboardButton]] = []
    for t in results[:PAGE_SIZE]:
        fav = "⭐" if t.is_favorite else ""
        lines.append(f"{fav} <b>{t.name}</b>")
        rows.append([InlineKeyboardButton(f"{fav} {t.name}", callback_data=f"bcm:template:view:{t.id}")])

    if not results:
        lines.append("No templates matched.")

    rows.append([InlineKeyboardButton("🔙 All Templates", callback_data="bcm:templates:0")])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(rows))
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Automation Rules
# ═══════════════════════════════════════════════════════════════════════════════

async def bcm_automation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List automation rules (callback: bcm:automation)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    if not cfg.get_bool("broadcast_automation_enabled", True):
        await _safe_edit(query, "🔴 Automation is disabled.", _back_kb("bcm:menu"))
        return

    rules = get_all_automation_rules()
    lines = [f"🤖 <b>Automation Rules</b> — {len(rules)} total\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for r in rules:
        status = "✅" if r.is_enabled else "🚫"
        trigger_label = TRIGGER_LABELS.get(r.trigger, r.trigger)
        lines.append(f"{status} <b>{r.name}</b> — {trigger_label}")
        rows.append([InlineKeyboardButton(
            f"{status} {r.name}", callback_data=f"bcm:rule:view:{r.id}"
        )])

    if not rules:
        lines.append("No automation rules yet. Create one with ➕ New Rule.")

    rows.append([InlineKeyboardButton("➕ New Rule", callback_data="bcm:rule:new")])
    rows.append([InlineKeyboardButton("🔙 Back",     callback_data="bcm:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def bcm_rule_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View an automation rule (callback: bcm:rule:view:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    rule_id = int(query.data.split(":")[-1])
    r = get_automation_rule(rule_id)
    if not r:
        await query.answer("❌ Rule not found.", show_alert=True)
        return

    status = "✅ Enabled" if r.is_enabled else "🚫 Disabled"
    trigger_label = TRIGGER_LABELS.get(r.trigger, r.trigger)
    text = (
        f"{'✅' if r.is_enabled else '🚫'} <b>{r.name}</b>\n\n"
        f"Trigger: <b>{trigger_label}</b>\n"
        f"Status: <b>{status}</b>\n"
        f"Delay: <b>{r.delay_minutes} min</b>\n"
        f"Target: <b>{r.target_segment}</b>\n"
        f"Dedup window: <b>{r.dedup_window_hours}h</b>\n"
        f"Triggered: <b>{r.trigger_count}</b> times\n"
        f"Last: <b>{r.last_triggered_at.strftime('%Y-%m-%d %H:%M') if r.last_triggered_at else '—'}</b>\n\n"
        f"<b>Message:</b>\n{(r.message_text or '')[:500]}{'…' if r.message_text and len(r.message_text) > 500 else ''}"
    )

    toggle_label = "🚫 Disable" if r.is_enabled else "✅ Enable"
    rows = [
        [
            InlineKeyboardButton(toggle_label,     callback_data=f"bcm:rule:toggle:{r.id}"),
            InlineKeyboardButton("🗑 Delete",       callback_data=f"bcm:rule:del_ask:{r.id}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="bcm:automation")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def bcm_rule_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle automation rule enabled/disabled (callback: bcm:rule:toggle:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    rule_id  = int(query.data.split(":")[-1])
    new_state = toggle_automation_rule(rule_id)
    if new_state is None:
        await query.answer("❌ Rule not found.", show_alert=True)
        return
    icon = "✅" if new_state else "🚫"
    await query.answer(f"{icon} Rule {'enabled' if new_state else 'disabled'}.")
    await bcm_rule_view(with_data(update, f"bcm:rule:view:{rule_id}"), context)


async def bcm_rule_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm rule deletion (callback: bcm:rule:del_ask:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    rule_id = int(query.data.split(":")[-1])
    r = get_automation_rule(rule_id)
    if not r:
        await query.answer("❌ Not found.", show_alert=True)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"bcm:rule:del_ok:{rule_id}")],
        [InlineKeyboardButton("❌ Cancel",       callback_data=f"bcm:rule:view:{rule_id}")],
    ])
    await _safe_edit(query, f"⚠️ Delete rule <b>{r.name}</b>?\n\nThis cannot be undone.", kb)


async def bcm_rule_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete automation rule (callback: bcm:rule:del_ok:<id>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    rule_id = int(query.data.split(":")[-1])
    delete_automation_rule(rule_id)
    log_admin_action(update.effective_user.id, "automation_rule_delete", f"ID {rule_id}")
    await query.answer("🗑 Rule deleted.", show_alert=True)
    await bcm_automation(update, context)


# ── New Rule conversation ──────────────────────────────────────────────────────

async def bcm_rule_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry for new-rule flow (callback: bcm:rule:new)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    if not cfg.get_bool("broadcast_automation_enabled", True):
        await query.answer("🔴 Automation is disabled.", show_alert=True)
        return ConversationHandler.END

    max_rules = cfg.get_int("broadcast_automation_max_rules", 50)
    if max_rules > 0 and len(get_all_automation_rules()) >= max_rules:
        await query.answer(f"⚠️ Rule limit reached ({max_rules}).", show_alert=True)
        return ConversationHandler.END

    context.user_data["_bcm_rule"] = {}
    await _safe_edit(query,
        "🤖 <b>New Automation Rule — Step 1/3</b>\n\nEnter a name for this rule:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_RULE_NAME


async def bcm_rule_recv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name or len(name) > 100:
        await update.message.reply_text("⚠️ Name must be 1-100 characters. Try again:")
        return BCM_RULE_NAME

    context.user_data["_bcm_rule"]["name"] = name

    trigger_rows = []
    for key, label in AUTOMATION_TRIGGERS:
        trigger_rows.append([InlineKeyboardButton(label, callback_data=f"bcm:rule:trigger:{key}")])
    trigger_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")])

    await update.message.reply_text(
        "🤖 <b>New Automation Rule — Step 2/3</b>\n\nSelect trigger event:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(trigger_rows),
    )
    return BCM_RULE_TRIGGER


async def bcm_rule_recv_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trigger = query.data.split(":")[-1]
    context.user_data["_bcm_rule"]["trigger"] = trigger
    trigger_label = TRIGGER_LABELS.get(trigger, trigger)
    await _safe_edit(query,
        f"🤖 <b>New Automation Rule — Step 3/3</b>\n\n"
        f"Trigger: <b>{trigger_label}</b>\n\n"
        "Enter the message to send when this trigger fires.\n"
        "Supported variables:\n"
        + "  ".join(SUPPORTED_VARIABLES[:8]) + "\n"
        + "  ".join(SUPPORTED_VARIABLES[8:]),
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcm:cancel")]])
    )
    return BCM_RULE_MSG


async def bcm_rule_recv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Message cannot be empty. Try again:")
        return BCM_RULE_MSG

    data = context.user_data.pop("_bcm_rule", {})
    r = create_automation_rule(
        name=data.get("name", "Untitled"),
        trigger=data.get("trigger", "new_user"),
        message_text=text,
        created_by=update.effective_user.id,
    )
    log_admin_action(update.effective_user.id, "automation_rule_create", f"ID {r.id} — {r.name}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Rule",     callback_data=f"bcm:rule:view:{r.id}")],
        [InlineKeyboardButton("🔙 Automation",   callback_data="bcm:automation")],
    ])
    await update.message.reply_text(
        f"✅ <b>Automation rule created!</b>\n\n"
        f"Name: <b>{r.name}</b>\n"
        f"Trigger: <b>{TRIGGER_LABELS.get(r.trigger, r.trigger)}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler View
# ═══════════════════════════════════════════════════════════════════════════════

async def bcm_scheduler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Campaign scheduler overview (callback: bcm:scheduler)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    from database import get_db_session
    from database.models import BroadcastCampaign as _BC, CampaignStatus as _CS

    now = datetime.utcnow()
    with get_db_session() as s:
        running  = s.query(_BC).filter_by(status=_CS.RUNNING.value,   is_archived=False).all()
        due_soon = (s.query(_BC)
                     .filter(
                         _BC.status == _CS.SCHEDULED.value,
                         _BC.next_run_at <= now + timedelta(hours=24),
                         _BC.is_archived.is_(False),
                     )
                     .order_by(_BC.next_run_at)
                     .limit(10)
                     .all())
        s.expunge_all()

    lines = ["📅 <b>Campaign Scheduler</b>\n"]

    if running:
        lines.append(f"<b>▶️ Running ({len(running)})</b>")
        for c in running:
            lines.append(f"  • {c.name} — sent {c.total_sent:,}")
    else:
        lines.append("▶️ No campaigns running now.")

    lines.append("")

    if due_soon:
        lines.append(f"<b>⏰ Due in next 24 hours ({len(due_soon)})</b>")
        for c in due_soon:
            ts = c.next_run_at.strftime("%Y-%m-%d %H:%M") if c.next_run_at else "—"
            lines.append(f"  • {c.name} — next: {ts}")
    else:
        lines.append("⏰ No campaigns due in the next 24 hours.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 All Campaigns", callback_data="bcm:campaigns:0")],
        [InlineKeyboardButton("🔙 Back",           callback_data="bcm:menu")],
    ])
    await _safe_edit(query, "\n".join(lines), kb)


# ═══════════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════════

async def bcm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Campaign Manager settings (callback: bcm:settings)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    status = cfg.get("broadcast_campaign_manager_status", "enabled")

    def _tf(key: str, default: bool = True) -> str:
        return "✅ ON" if cfg.get_bool(key, default) else "🚫 OFF"

    text = (
        "⚙️ <b>Campaign Manager Settings</b>\n\n"
        f"Feature Status: <b>{status.title()}</b>\n\n"
        f"• Campaigns: {_tf('broadcast_campaigns_enabled')}\n"
        f"• Templates: {_tf('broadcast_templates_enabled')}\n"
        f"• Automation: {_tf('broadcast_automation_enabled')}\n"
        f"• A/B Testing: {_tf('broadcast_ab_testing_enabled')}\n"
        f"• Recurring Campaigns: {_tf('broadcast_recurring_campaigns_enabled')}\n\n"
        f"Max Running Campaigns: <b>{cfg.get_int('broadcast_campaign_max_running', 3)}</b>\n"
        f"Max Templates: <b>{cfg.get_int('broadcast_template_max', 200)}</b>\n"
        f"Max Automation Rules: <b>{cfg.get_int('broadcast_automation_max_rules', 50)}</b>"
    )

    current_status = cfg.get("broadcast_campaign_manager_status", "enabled")
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("🟢 Enable",      callback_data="bcm:settings:status:enabled"),
            InlineKeyboardButton("🟡 Maintenance", callback_data="bcm:settings:status:maintenance"),
            InlineKeyboardButton("🔴 Disable",     callback_data="bcm:settings:status:disabled"),
        ],
        [
            InlineKeyboardButton(
                f"{'🚫' if cfg.get_bool('broadcast_campaigns_enabled') else '✅'} Campaigns",
                callback_data="bcm:settings:toggle:broadcast_campaigns_enabled",
            ),
            InlineKeyboardButton(
                f"{'🚫' if cfg.get_bool('broadcast_templates_enabled') else '✅'} Templates",
                callback_data="bcm:settings:toggle:broadcast_templates_enabled",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'🚫' if cfg.get_bool('broadcast_automation_enabled') else '✅'} Automation",
                callback_data="bcm:settings:toggle:broadcast_automation_enabled",
            ),
            InlineKeyboardButton(
                f"{'🚫' if cfg.get_bool('broadcast_ab_testing_enabled') else '✅'} A/B Test",
                callback_data="bcm:settings:toggle:broadcast_ab_testing_enabled",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'🚫' if cfg.get_bool('broadcast_recurring_campaigns_enabled') else '✅'} Recurring",
                callback_data="bcm:settings:toggle:broadcast_recurring_campaigns_enabled",
            ),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="bcm:menu")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def bcm_settings_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set feature status (callback: bcm:settings:status:<val>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    val = query.data.split(":")[-1]
    cfg.set("broadcast_campaign_manager_status", val)
    log_admin_action(update.effective_user.id, "bcm_status", val)
    await query.answer(f"Status set to {val}.", show_alert=False)
    await bcm_settings(update, context)


async def bcm_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a boolean setting (callback: bcm:settings:toggle:<key>)."""
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    key = ":".join(query.data.split(":")[3:])
    current = cfg.get_bool(key, True)
    cfg.set(key, not current)
    log_admin_action(update.effective_user.id, "bcm_toggle", f"{key}={'off' if current else 'on'}")
    await bcm_settings(update, context)


# ── Cancel helper ──────────────────────────────────────────────────────────────

async def bcm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any active BCM conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        context.user_data.pop("_bcm_new", None)
        context.user_data.pop("_bcm_tmpl", None)
        context.user_data.pop("_bcm_rule", None)
        await _safe_edit(query, "❌ Cancelled.", _back_kb("bcm:menu"))
    return ConversationHandler.END


# ── Conversation handler builder ───────────────────────────────────────────────

def build_bcm_conv() -> ConversationHandler:
    """Build the ConversationHandler for all BCM multi-step flows."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bcm_campaign_new_start,    pattern=r"^bcm:campaign:new$"),
            CallbackQueryHandler(bcm_template_new_start,    pattern=r"^bcm:template:new$"),
            CallbackQueryHandler(bcm_template_search_start, pattern=r"^bcm:template:search$"),
            CallbackQueryHandler(bcm_rule_new_start,        pattern=r"^bcm:rule:new$"),
        ],
        states={
            BCM_CAMPAIGN_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_campaign_recv_name)],
            BCM_CAMPAIGN_TYPE:   [CallbackQueryHandler(bcm_campaign_recv_type,   pattern=r"^bcm:campaign:type:.+$")],
            BCM_CAMPAIGN_MSG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_campaign_recv_msg)],
            BCM_AB_VARIANT_B:    [
                CallbackQueryHandler(bcm_campaign_ab_choice,    pattern=r"^bcm:campaign:ab:(yes|no)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_campaign_recv_variant_b),
            ],
            BCM_TEMPLATE_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_template_recv_name)],
            BCM_TEMPLATE_MSG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_template_recv_msg)],
            BCM_SEARCH_INPUT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_template_search_recv)],
            BCM_RULE_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_rule_recv_name)],
            BCM_RULE_TRIGGER:    [CallbackQueryHandler(bcm_rule_recv_trigger,    pattern=r"^bcm:rule:trigger:.+$")],
            BCM_RULE_MSG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, bcm_rule_recv_msg)],
        },
        fallbacks=[
            CallbackQueryHandler(bcm_cancel, pattern=r"^bcm:cancel$"),
        ],
        per_message=False,
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    """Register all BCM handlers with the PTB Application instance."""
    # Conversation handler (multi-step flows)
    application.add_handler(build_bcm_conv())

    # ── Root navigation ──────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_menu,         pattern=r"^bcm:menu$"))

    # ── Campaign Manager ─────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_campaigns,         pattern=r"^bcm:campaigns:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_view,     pattern=r"^bcm:campaign:view:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_run,      pattern=r"^bcm:campaign:run:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_pause,    pattern=r"^bcm:campaign:pause:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_resume,   pattern=r"^bcm:campaign:resume:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_cancel,   pattern=r"^bcm:campaign:cancel:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_archive,  pattern=r"^bcm:campaign:archive:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_unarchive,pattern=r"^bcm:campaign:unarchive:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_del_ask,  pattern=r"^bcm:campaign:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_del_ok,   pattern=r"^bcm:campaign:del_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_dup,      pattern=r"^bcm:campaign:dup:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_campaign_history,  pattern=r"^bcm:campaign:history:\d+$"))

    # ── Template Library ─────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_templates,          pattern=r"^bcm:templates:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_templates_fav,      pattern=r"^bcm:templates:fav$"))
    application.add_handler(CallbackQueryHandler(bcm_templates_groups,   pattern=r"^bcm:templates:groups$"))
    application.add_handler(CallbackQueryHandler(bcm_template_view,      pattern=r"^bcm:template:view:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_template_fav,       pattern=r"^bcm:template:fav:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_template_dup,       pattern=r"^bcm:template:dup:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_template_del_ask,   pattern=r"^bcm:template:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_template_del_ok,    pattern=r"^bcm:template:del_ok:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_template_use,       pattern=r"^bcm:template:use:\d+$"))

    # ── Automation Rules ─────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_automation,  pattern=r"^bcm:automation$"))
    application.add_handler(CallbackQueryHandler(bcm_rule_view,   pattern=r"^bcm:rule:view:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_rule_toggle, pattern=r"^bcm:rule:toggle:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_rule_del_ask,pattern=r"^bcm:rule:del_ask:\d+$"))
    application.add_handler(CallbackQueryHandler(bcm_rule_del_ok, pattern=r"^bcm:rule:del_ok:\d+$"))

    # ── Scheduler ────────────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_scheduler, pattern=r"^bcm:scheduler$"))

    # ── Settings ─────────────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(bcm_settings,        pattern=r"^bcm:settings$"))
    application.add_handler(CallbackQueryHandler(bcm_settings_status,  pattern=r"^bcm:settings:status:.+$"))
    application.add_handler(CallbackQueryHandler(bcm_settings_toggle,  pattern=r"^bcm:settings:toggle:.+$"))
