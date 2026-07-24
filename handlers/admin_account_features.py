"""Admin Account Feature Management panel — V19.

Callback namespace: aaf:*

Provides a dedicated admin section where every Account & Order feature can be:
  • Enabled / Disabled
  • Configured (limits, toggles, etc.)

Statistics are shown for each feature.
"""
from __future__ import annotations

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database import get_db_session
from database.models import (
    OrderReceipt, UserDownload, ActivityLog, UserSession,
    Order, OrderStatus,
)
from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Stats helpers
# ─────────────────────────────────────────────────────────────────────────

def _count(model) -> int:
    try:
        with get_db_session() as s:
            return s.query(model).count()
    except Exception:
        return 0


def _count_filter(model, **kwargs) -> int:
    try:
        with get_db_session() as s:
            return s.query(model).filter_by(**kwargs).count()
    except Exception:
        return 0


def get_account_feature_stats() -> dict:
    """Return stats dict for all 5 account features (used by admin dashboard)."""
    from sqlalchemy import func
    stats: dict = {}
    try:
        stats["receipt_count"] = _count(OrderReceipt)
        stats["receipt_purchase"] = _count_filter(OrderReceipt, receipt_type="purchase")
        stats["receipt_deposit"] = _count_filter(OrderReceipt, receipt_type="deposit")
        stats["download_count"] = _count(UserDownload)
        try:
            with get_db_session() as s:
                stats["download_total_accesses"] = (
                    s.query(func.sum(UserDownload.download_count)).scalar() or 0
                )
        except Exception:
            stats["download_total_accesses"] = 0
        stats["activity_events"] = _count(ActivityLog)
        stats["session_active"] = _count_filter(UserSession, is_active=True)
        stats["session_total"] = _count(UserSession)
        try:
            with get_db_session() as s:
                stats["order_status_total"] = s.query(Order).count() or 0
                stats["order_completed"] = s.query(Order).filter(
                    Order.status == OrderStatus.COMPLETED
                ).count() or 0
        except Exception:
            stats["order_status_total"] = 0
            stats["order_completed"] = 0
    except Exception:
        logger.exception("get_account_feature_stats failed")
    return stats


# ─────────────────────────────────────────────────────────────────────────
# Feature definitions for the admin panel
# ─────────────────────────────────────────────────────────────────────────

_ACCOUNT_FEATURES = [
    {
        "id": "receipt",
        "emoji": "🧾",
        "name": "Auto Receipt",
        "enable_key": "feature_receipt_enabled",
        "options": [
            {
                "key": "feature_receipt_header",
                "label": "Receipt Header",
                "type": "text_hint",
                "hint": "Custom text shown at top of every receipt. Leave empty to skip.",
            },
            {
                "key": "feature_receipt_footer",
                "label": "Receipt Footer",
                "type": "text_hint",
                "hint": "Custom text shown at bottom. Default: 'Thank you for your purchase!'",
            },
        ],
        "stat_keys": ["receipt_count", "receipt_purchase", "receipt_deposit"],
        "stat_labels": ["Total receipts", "Purchase receipts", "Deposit receipts"],
    },
    {
        "id": "order_status",
        "emoji": "📦",
        "name": "Order Status System",
        "enable_key": "feature_order_status_enabled",
        "options": [
            {
                "key": "feature_order_expiry_hours",
                "label": "Order Expiry (hours)",
                "type": "choice",
                "choices": [
                    ("0", "Never"),
                    ("24", "24 hours"),
                    ("48", "48 hours"),
                    ("72", "72 hours"),
                    ("168", "7 days"),
                ],
            },
        ],
        "stat_keys": ["order_status_total", "order_completed"],
        "stat_labels": ["Total orders", "Completed orders"],
    },
    {
        "id": "download_center",
        "emoji": "📁",
        "name": "Download Center",
        "enable_key": "feature_download_center_enabled",
        "options": [
            {
                "key": "feature_download_max",
                "label": "Max Downloads per item",
                "type": "choice",
                "choices": [
                    ("0", "Unlimited"),
                    ("1", "1"),
                    ("3", "3"),
                    ("5", "5"),
                    ("10", "10"),
                ],
            },
            {
                "key": "feature_download_expiry_days",
                "label": "Download Expiry (days)",
                "type": "choice",
                "choices": [
                    ("0", "Never"),
                    ("7", "7 days"),
                    ("30", "30 days"),
                    ("90", "90 days"),
                    ("365", "1 year"),
                ],
            },
        ],
        "stat_keys": ["download_count", "download_total_accesses"],
        "stat_labels": ["Total download items", "Total access count"],
    },
    {
        "id": "activity_history",
        "emoji": "📜",
        "name": "Activity History",
        "enable_key": "feature_activity_history_enabled",
        "options": [
            {
                "key": "feature_activity_max",
                "label": "Max history entries per user",
                "type": "choice",
                "choices": [
                    ("30", "30"),
                    ("50", "50"),
                    ("100", "100"),
                    ("0", "Unlimited"),
                ],
            },
        ],
        "stat_keys": ["activity_events"],
        "stat_labels": ["Total activity events"],
    },
    {
        "id": "security_center",
        "emoji": "🔒",
        "name": "Security Center",
        "enable_key": "feature_security_center_enabled",
        "options": [
            {
                "key": "feature_session_timeout_hours",
                "label": "Session Timeout (hours)",
                "type": "choice",
                "choices": [
                    ("0", "Never"),
                    ("1", "1 hour"),
                    ("6", "6 hours"),
                    ("24", "24 hours"),
                    ("72", "72 hours"),
                ],
            },
            {
                "key": "feature_new_login_notification",
                "label": "New Login Notification",
                "type": "bool",
            },
            {
                "key": "feature_security_alerts",
                "label": "Security Alerts",
                "type": "bool",
            },
        ],
        "stat_keys": ["session_active", "session_total"],
        "stat_labels": ["Active sessions", "Total sessions"],
    },
]


def _find_feature(feature_id: str):
    for f in _ACCOUNT_FEATURES:
        if f["id"] == feature_id:
            return f
    return None


# ─────────────────────────────────────────────────────────────────────────
# Admin panel handlers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML",
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def account_features_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """aaf:menu — Account Feature Management main menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    stats = get_account_feature_stats()

    text = "📱 <b>Account Feature Management</b>\n\n"
    kb: List[List[InlineKeyboardButton]] = []

    for feat in _ACCOUNT_FEATURES:
        enabled = cfg.get_bool(feat["enable_key"], True)
        status_icon = "✅" if enabled else "❌"
        stat_lines = []
        for sk, sl in zip(feat["stat_keys"], feat["stat_labels"]):
            val = stats.get(sk, 0)
            stat_lines.append(f"{sl}: {val}")
        stat_str = " | ".join(stat_lines)
        text += (
            f"{feat['emoji']} <b>{feat['name']}</b>  {status_icon}\n"
            f"<i>{stat_str}</i>\n\n"
        )
        kb.append([InlineKeyboardButton(
            f"{feat['emoji']} {feat['name']}",
            callback_data=f"aaf:f:{feat['id']}",
        )])

    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="af:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def account_feature_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """aaf:f:<id> — Account feature detail page."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    _override = context.user_data.pop("_cb_data_override", None)
    feature_id = _override if _override else query.data.split(":", 2)[-1]  # aaf:f:<feature_id>
    feat = _find_feature(feature_id)
    if not feat:
        await query.answer("❌ Unknown feature.", show_alert=True)
        return

    enabled = cfg.get_bool(feat["enable_key"], True)
    status_label = "✅ Enabled" if enabled else "❌ Disabled"
    toggle_label = "❌ Disable" if enabled else "✅ Enable"
    toggle_cb = f"aaf:off:{feature_id}" if enabled else f"aaf:on:{feature_id}"

    stats = get_account_feature_stats()
    stat_lines = []
    for sk, sl in zip(feat["stat_keys"], feat["stat_labels"]):
        stat_lines.append(f"  • {sl}: <b>{stats.get(sk, 0)}</b>")
    stat_str = "\n".join(stat_lines)

    text = (
        f"{feat['emoji']} <b>{feat['name']}</b>\n"
        f"Status: {status_label}\n\n"
        f"<b>📊 Statistics:</b>\n{stat_str}\n\n"
        f"<b>⚙️ Options:</b>\n"
    )

    kb: List[List[InlineKeyboardButton]] = []
    kb.append([InlineKeyboardButton(toggle_label, callback_data=toggle_cb)])

    for opt in feat.get("options", []):
        opt_key = opt["key"]
        opt_label = opt["label"]
        opt_type = opt.get("type", "str")

        if opt_type == "bool":
            cur = cfg.get_bool(opt_key, True)
            cur_label = "ON" if cur else "OFF"
            text += f"\n🔘 {opt_label}: <b>{cur_label}</b>"
            flip_val = "false" if cur else "true"
            kb.append([InlineKeyboardButton(
                f"{'🟢' if cur else '⚫'} {opt_label}: {cur_label}",
                callback_data=f"aaf:set:{opt_key}:{flip_val}",
            )])

        elif opt_type == "choice":
            cur_val = cfg.get_str(opt_key, "")
            cur_label = next(
                (lbl for val, lbl in opt["choices"] if val == cur_val),
                cur_val or "Default"
            )
            text += f"\n🔘 {opt_label}: <b>{cur_label}</b>"
            choice_row = []
            for val, lbl in opt["choices"]:
                marker = "●" if val == cur_val else "○"
                choice_row.append(InlineKeyboardButton(
                    f"{marker} {lbl}",
                    callback_data=f"aaf:set:{opt_key}:{val}",
                ))
            # Break into rows of 3
            for i in range(0, len(choice_row), 3):
                kb.append(choice_row[i:i + 3])

        elif opt_type == "text_hint":
            hint = opt.get("hint", "")
            text += f"\n🔘 {opt_label}\n   <i>{hint}</i>"

    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="aaf:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


async def account_feature_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """aaf:on:<id> — Enable an account feature."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    feature_id = query.data.split(":", 2)[-1]
    feat = _find_feature(feature_id)
    if not feat:
        return

    cfg.set(feat["enable_key"], True)
    await query.answer(f"✅ {feat['name']} enabled.", show_alert=False)
    context.user_data["_cb_data_override"] = feature_id
    await account_feature_detail(update, context)


async def account_feature_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """aaf:off:<id> — Disable an account feature."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    feature_id = query.data.split(":", 2)[-1]
    feat = _find_feature(feature_id)
    if not feat:
        return

    cfg.set(feat["enable_key"], False)
    await query.answer(f"❌ {feat['name']} disabled.", show_alert=False)
    context.user_data["_cb_data_override"] = feature_id
    await account_feature_detail(update, context)


async def account_feature_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """aaf:set:<key>:<value> — Set an account feature option value."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    parts = query.data.split(":", 3)
    if len(parts) < 4:
        return

    _, _, config_key, value = parts
    cfg.set(config_key, value)

    # Find the feature that owns this key and navigate back to its detail
    feature_id = None
    for feat in _ACCOUNT_FEATURES:
        if feat["enable_key"] == config_key:
            feature_id = feat["id"]
            break
        for opt in feat.get("options", []):
            if opt["key"] == config_key:
                feature_id = feat["id"]
                break
        if feature_id:
            break

    if feature_id:
        context.user_data["_cb_data_override"] = feature_id
        await account_feature_detail(update, context)
    else:
        await account_features_menu(update, context)
