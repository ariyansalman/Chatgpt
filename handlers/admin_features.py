"""Admin Feature Management panel — V18.

Callback namespace: af:*

Provides a dedicated admin section where every user feature can be:
  • Enabled / Disabled
  • Configured (limits, toggles, etc.)

Statistics are shown for each feature.
"""
from __future__ import annotations

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session, User, Product
from database.models import (
    UserWishlist, PriceDropAlert, RecentlyViewed,
    QuickBuyConfig, PreferredPayment,
    Order, OrderItem, OrderStatus,
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


def _count_unique_users(model) -> int:
    """Count distinct users that have at least one row."""
    from sqlalchemy import func
    try:
        with get_db_session() as s:
            return s.query(func.count(func.distinct(model.user_id))).scalar() or 0
    except Exception:
        return 0


def get_feature_stats() -> dict:
    """Return stats dict for all features (used by admin dashboard too)."""
    from sqlalchemy import func
    stats: dict = {}
    try:
        stats["wishlist_items"] = _count(UserWishlist)
        stats["wishlist_users"] = _count_unique_users(UserWishlist)
        stats["price_alert_subs"] = _count(PriceDropAlert)
        stats["price_alert_users"] = _count_unique_users(PriceDropAlert)
        stats["recently_viewed_entries"] = _count(RecentlyViewed)
        stats["recently_viewed_users"] = _count_unique_users(RecentlyViewed)
        stats["quick_buy_configs"] = _count(QuickBuyConfig)
        stats["quick_buy_users"] = _count_unique_users(QuickBuyConfig)
        stats["preferred_payment_users"] = _count(PreferredPayment)
        # Buy Again uses existing orders — count distinct users with ≥1 completed order
        try:
            with get_db_session() as s:
                stats["buy_again_users"] = (
                    s.query(func.count(func.distinct(Order.user_id)))
                    .filter(Order.status == OrderStatus.COMPLETED)
                    .scalar() or 0
                )
        except Exception:
            stats["buy_again_users"] = 0
    except Exception:
        logger.exception("get_feature_stats failed")

    # ── V22: Favorites stats ──────────────────────────────────────────────
    try:
        from database.models import UserFavorite as _UF
        from datetime import datetime as _dt, timedelta as _td
        _now = _dt.utcnow()
        with get_db_session() as s:
            stats["favorites_total"] = s.query(_UF).count()
            stats["favorites_today"] = s.query(_UF).filter(
                _UF.created_at >= _now - _td(days=1)
            ).count()
    except Exception:
        stats.setdefault("favorites_total", 0)
        stats.setdefault("favorites_today", 0)

    # ── V22: Product Compare stats ────────────────────────────────────────
    try:
        from database.models import ProductCompareLog as _PCL
        with get_db_session() as s:
            stats["compare_total_sessions"] = s.query(_PCL).count()
            stats["compare_purchased_after"] = (
                s.query(_PCL).filter_by(purchased_from_compare=True).count()
            )
    except Exception:
        stats.setdefault("compare_total_sessions", 0)
        stats.setdefault("compare_purchased_after", 0)

    # ── V22: Subscription Reminder stats ─────────────────────────────────
    try:
        from database.models import SubscriptionReminderLog, Subscription as _Sub
        with get_db_session() as s:
            stats["sub_reminder_active"] = (
                s.query(_Sub).filter(
                    _Sub.status.in_(("active", "past_due"))
                ).count()
            )
            try:
                stats["sub_reminder_sent"] = (
                    s.query(SubscriptionReminderLog).filter(
                        SubscriptionReminderLog.success == True  # noqa: E712
                    ).count()
                )
            except Exception:
                stats["sub_reminder_sent"] = 0
    except Exception:
        stats.setdefault("sub_reminder_active", 0)
        stats.setdefault("sub_reminder_sent", 0)

    # ── V21: stats for new features ──────────────────────────────────────
    try:
        from database.models import ScheduledBroadcast, Refund, AdminAuditLog
        from database import User as _User
        with get_db_session() as s:
            # Scheduled broadcasts
            try:
                stats["scheduled_broadcasts_total"] = s.query(func.count(ScheduledBroadcast.id)).scalar() or 0
                stats["scheduled_broadcasts_sent"] = s.query(func.count(ScheduledBroadcast.id)).filter(
                    ScheduledBroadcast.status == "sent").scalar() or 0
            except Exception:
                stats.setdefault("scheduled_broadcasts_total", 0)
                stats.setdefault("scheduled_broadcasts_sent", 0)
            # Refunds
            try:
                stats["refunds_pending"] = s.query(func.count(Refund.id)).filter(
                    Refund.status == "pending").scalar() or 0
                stats["refunds_processed"] = s.query(func.count(Refund.id)).filter(
                    Refund.status == "processed").scalar() or 0
            except Exception:
                stats.setdefault("refunds_pending", 0)
                stats.setdefault("refunds_processed", 0)
            # Audit log
            try:
                stats["audit_log_total"] = s.query(func.count(AdminAuditLog.id)).scalar() or 0
            except Exception:
                stats.setdefault("audit_log_total", 0)
            # Multi-language: users with non-English language
            try:
                stats["multilang_total_users"] = s.query(func.count(_User.id)).filter(
                    _User.language != "en", _User.language.isnot(None)).scalar() or 0
            except Exception:
                stats.setdefault("multilang_total_users", 0)
            # Advanced coupons
            try:
                from database import Coupon, CouponRedemption
                stats["advanced_coupons_active"] = s.query(func.count(Coupon.id)).filter(
                    Coupon.is_active == True).scalar() or 0  # noqa: E712
                stats["advanced_coupons_redeemed"] = s.query(func.count(CouponRedemption.id)).scalar() or 0
            except Exception:
                stats.setdefault("advanced_coupons_active", 0)
                stats.setdefault("advanced_coupons_redeemed", 0)
    except Exception:
        logger.debug("get_feature_stats V21 section failed", exc_info=True)

    return stats


# ─────────────────────────────────────────────────────────────────────────
# Feature definitions for the admin panel
# ─────────────────────────────────────────────────────────────────────────

_FEATURES = [
    {
        "id": "wishlist",
        "emoji": "❤️",
        "name": "Wishlist",
        "enable_key": "feature_wishlist_enabled",
        "options": [
            {
                "key": "feature_wishlist_max",
                "label": "Max items per user",
                "type": "choice",
                "choices": [("10", "10"), ("20", "20"), ("50", "50"), ("100", "100"), ("0", "Unlimited")],
            },
            {
                "key": "feature_wishlist_counter",
                "label": "Show item counter on button",
                "type": "bool",
            },
        ],
        "stat_keys": ["wishlist_items", "wishlist_users"],
        "stat_labels": ["Total saved items", "Users with wishlists"],
    },
    {
        "id": "price_alerts",
        "emoji": "🔔",
        "name": "Price Drop Alerts",
        "enable_key": "feature_price_alerts_enabled",
        "options": [
            {
                "key": "feature_price_alerts_auto_notify",
                "label": "Auto-notify on price change",
                "type": "bool",
            },
        ],
        "stat_keys": ["price_alert_subs", "price_alert_users"],
        "stat_labels": ["Active subscriptions", "Subscribed users"],
    },
    {
        "id": "recently_viewed",
        "emoji": "🕒",
        "name": "Recently Viewed",
        "enable_key": "feature_recently_viewed_enabled",
        "options": [
            {
                "key": "feature_recently_viewed_max",
                "label": "History size per user",
                "type": "choice",
                "choices": [("10", "10"), ("20", "20"), ("50", "50"), ("100", "100")],
            },
            {
                "key": "feature_recently_viewed_clean_deleted",
                "label": "Auto-remove deleted products",
                "type": "bool",
            },
        ],
        "stat_keys": ["recently_viewed_entries", "recently_viewed_users"],
        "stat_labels": ["Total view records", "Users with history"],
    },
    {
        "id": "quick_buy",
        "emoji": "⚡",
        "name": "Quick Buy",
        "enable_key": "feature_quick_buy_enabled",
        "options": [
            {
                "key": "feature_quick_buy_max",
                "label": "Max remembered products per user",
                "type": "choice",
                "choices": [("10", "10"), ("20", "20"), ("50", "50")],
            },
        ],
        "stat_keys": ["quick_buy_configs", "quick_buy_users"],
        "stat_labels": ["Saved quick-buy configs", "Users with quick-buy"],
    },
    {
        "id": "preferred_payment",
        "emoji": "⭐",
        "name": "Preferred Payment",
        "enable_key": "feature_preferred_payment_enabled",
        "options": [],
        "stat_keys": ["preferred_payment_users"],
        "stat_labels": ["Users with a preference set"],
    },
    {
        "id": "buy_again",
        "emoji": "🔁",
        "name": "Buy Again",
        "enable_key": "feature_buy_again_enabled",
        "options": [
            {
                "key": "feature_buy_again_max",
                "label": "Max history shown",
                "type": "choice",
                "choices": [("10", "10"), ("20", "20"), ("50", "50"), ("100", "100")],
            },
        ],
        "stat_keys": ["buy_again_users"],
        "stat_labels": ["Users with completed orders"],
    },
    # ── Part 3: Sales & Marketing ──────────────────────────────────────────
    {
        "id": "gift_purchase",
        "emoji": "🎁",
        "name": "Gift Purchase",
        "enable_key": "feature_gift_purchase_enabled",
        "options": [
            {
                "key": "feature_gift_allow_anonymous",
                "label": "Allow anonymous gifts",
                "type": "bool",
            },
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    {
        "id": "gift_cards",
        "emoji": "🎟",
        "name": "Gift Cards",
        "enable_key": "feature_gift_cards_enabled",
        "options": [],
        "stat_keys": [],
        "stat_labels": [],
    },
    {
        "id": "product_reviews",
        "emoji": "⭐",
        "name": "Product Reviews",
        "enable_key": "feature_reviews_enabled",
        "options": [
            {
                "key": "feature_reviews_require_approval",
                "label": "Require admin approval before publishing",
                "type": "bool",
            },
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    {
        "id": "bundles",
        "emoji": "📦",
        "name": "Product Bundles",
        "enable_key": "feature_bundles_show_savings",
        "options": [
            {
                "key": "feature_bundles_show_savings",
                "label": "Show savings badge on bundle pages",
                "type": "bool",
            },
            {
                "key": "feature_bundles_show_contents",
                "label": "List included products on bundle page",
                "type": "bool",
            },
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V20: Advanced Feature Group 1 — Referral Dashboard ───────────────────
    {
        "id": "referral_dashboard",
        "emoji": "👥",
        "name": "Advanced Referral Dashboard",
        "enable_key": "feature_referral_dashboard_enabled",
        "options": [
            {"key": "referral_commission_pct",         "label": "Commission % on referred purchases", "type": "float"},
            {"key": "referral_min_withdrawal",         "label": "Minimum withdrawal amount ($)",       "type": "float"},
            {"key": "referral_max_withdrawal",         "label": "Maximum withdrawal ($, 0=unlimited)", "type": "float"},
            {"key": "referral_bonus",                  "label": "Signup bonus credited to referrals",  "type": "float"},
            {"key": "referral_first_purchase_bonus",   "label": "First-purchase bonus to referrer",    "type": "float"},
            {"key": "referral_lifetime_enabled",       "label": "Lifetime referral tracking",          "type": "bool"},
        ],
        "stat_keys": ["referral_clicks_total", "referral_withdrawals_pending"],
        "stat_labels": ["Total link clicks", "Pending withdrawals"],
    },
    # ── V20: Advanced Feature Group 2 — Enhanced Support Tickets ─────────────
    {
        "id": "support_enhanced",
        "emoji": "🎫",
        "name": "Enhanced Support Tickets",
        "enable_key": "feature_support_categories_enabled",
        "options": [
            {"key": "feature_support_categories_enabled", "label": "Category picker on ticket create", "type": "bool"},
            {"key": "feature_support_file_uploads",       "label": "Allow photo attachments",          "type": "bool"},
            {"key": "feature_support_assign_enabled",     "label": "Admin ticket assignment",           "type": "bool"},
        ],
        "stat_keys": ["open_tickets", "total_tickets"],
        "stat_labels": ["Open tickets", "Total tickets"],
    },
    # ── V20: Advanced Feature Group 3 — Maintenance Advanced ─────────────────
    {
        "id": "maintenance_advanced",
        "emoji": "🔧",
        "name": "Maintenance Mode Advanced",
        "enable_key": "feature_maintenance_advanced_enabled",
        "options": [
            {"key": "maintenance_estimated_return", "label": "Estimated return time (free text)", "type": "str"},
            {"key": "maintenance_whitelist",        "label": "Bypass whitelist (comma-sep IDs)",  "type": "str"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V20: Advanced Feature Group 4 — Announcement System ──────────────────
    {
        "id": "announcements",
        "emoji": "📢",
        "name": "Announcement System",
        "enable_key": "feature_announcements_enabled",
        "options": [
            {"key": "announcement_popup_enabled",    "label": "Show popup on main menu (unread)",   "type": "bool"},
            {"key": "announcement_homepage_banner",  "label": "Show banner text in menu header",    "type": "bool"},
        ],
        "stat_keys": ["active_announcements"],
        "stat_labels": ["Active announcements"],
    },
    # ── V20: Advanced Feature Group 5 — Low Stock Monitoring Advanced ─────────
    {
        "id": "low_stock_advanced",
        "emoji": "📉",
        "name": "Low-Stock Monitoring Advanced",
        "enable_key": "feature_low_stock_advanced_enabled",
        "options": [
            {"key": "low_stock_auto_notify",          "label": "Auto-notify admins on low stock",  "type": "bool"},
            {"key": "low_stock_silent_notify",        "label": "Silent mode (log only, no DM)",    "type": "bool"},
            {"key": "low_stock_fast_seller_days",     "label": "Fast-seller detection window (days)", "type": "int"},
            {"key": "low_stock_fast_seller_threshold","label": "Fast-seller unit threshold",        "type": "int"},
        ],
        "stat_keys": ["low_stock"],
        "stat_labels": ["Products at low-stock threshold"],
    },
    # ── V21: Six New Major Features ───────────────────────────────────────────
    {
        "id": "scheduled_broadcast",
        "emoji": "📨",
        "name": "Scheduled Broadcast",
        "enable_key": "feature_scheduled_broadcast_enabled",
        "options": [
            {"key": "broadcast_default_segment", "label": "Default audience segment",
             "type": "choice",
             "choices": [("all", "All Users"), ("buyers", "Buyers Only"),
                         ("non_buyers", "Non-Buyers"), ("wallet_users", "Wallet Users")]},
            {"key": "broadcast_max_daily",        "label": "Max broadcasts per day (0=unlimited)", "type": "int"},
        ],
        "stat_keys": ["scheduled_broadcasts_total", "scheduled_broadcasts_sent"],
        "stat_labels": ["Total broadcasts", "Successfully sent"],
    },
    {
        "id": "advanced_analytics",
        "emoji": "📊",
        "name": "Advanced Analytics Dashboard",
        "enable_key": "feature_advanced_analytics_enabled",
        "options": [
            {"key": "analytics_default_period", "label": "Default report period",
             "type": "choice",
             "choices": [("7d", "7 days"), ("30d", "30 days"), ("90d", "90 days"), ("all", "All time")]},
            {"key": "analytics_export_enabled", "label": "Enable CSV export", "type": "bool"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    {
        "id": "multilang",
        "emoji": "🌍",
        "name": "Multi-Language System",
        "enable_key": "feature_multilang_enabled",
        "options": [
            {"key": "default_language", "label": "Default bot language",
             "type": "choice",
             "choices": [("en", "English"), ("bn", "বাংলা"), ("ar", "العربية"),
                         ("ru", "Русский"), ("vi", "Tiếng Việt"), ("zh", "中文")]},
            {"key": "multilang_user_switch", "label": "Allow users to switch language", "type": "bool"},
        ],
        "stat_keys": ["multilang_total_users"],
        "stat_labels": ["Users with non-English language"],
    },
    {
        "id": "advanced_coupons",
        "emoji": "🏷",
        "name": "Advanced Coupon System",
        "enable_key": "feature_advanced_coupons_enabled",
        "options": [
            {"key": "coupon_auto_apply",        "label": "Auto-apply best available coupon", "type": "bool"},
            {"key": "coupon_birthday_enabled",  "label": "Auto-issue birthday coupons",      "type": "bool"},
            {"key": "coupon_birthday_discount", "label": "Birthday coupon discount %",       "type": "float"},
            {"key": "coupon_referral_enabled",  "label": "Auto-issue referral coupons",      "type": "bool"},
        ],
        "stat_keys": ["advanced_coupons_active", "advanced_coupons_redeemed"],
        "stat_labels": ["Active advanced coupons", "Total redemptions"],
    },
    {
        "id": "auto_refund",
        "emoji": "💰",
        "name": "Automatic Refund System",
        "enable_key": "feature_auto_refund_enabled",
        "options": [
            {"key": "refund_auto_failed_orders",    "label": "Auto-refund failed orders",      "type": "bool"},
            {"key": "refund_auto_cancelled_orders", "label": "Auto-refund cancelled orders",   "type": "bool"},
            {"key": "refund_auto_timed_out",        "label": "Auto-refund timed-out orders",   "type": "bool"},
            {"key": "refund_auto_duplicate",        "label": "Auto-refund duplicate payments", "type": "bool"},
            {"key": "refund_notify_admin",          "label": "Notify admin on new refund",     "type": "bool"},
        ],
        "stat_keys": ["refunds_pending", "refunds_processed"],
        "stat_labels": ["Pending refunds", "Processed refunds"],
    },
    {
        "id": "audit_enhanced",
        "emoji": "📝",
        "name": "Enhanced Admin Audit Log",
        "enable_key": "feature_audit_enhanced_enabled",
        "options": [
            {"key": "audit_log_ip",          "label": "Log IP addresses in audit",         "type": "bool"},
            {"key": "audit_log_old_new_vals", "label": "Log old/new values on changes",     "type": "bool"},
            {"key": "audit_max_retention_days", "label": "Retention (days, 0=forever)",    "type": "int"},
        ],
        "stat_keys": ["audit_log_total"],
        "stat_labels": ["Total audit entries"],
    },
    # ── V22: Favorites (Bookmarks) ───────────────────────────────────────────
    {
        "id": "favorites",
        "emoji": "❤️",
        "name": "Favorites (Bookmarks)",
        "enable_key": "feature_favorites_enabled",
        "options": [
            {"key": "favorites_max",           "label": "Max favorites per user (0=unlimited)", "type": "int"},
            {"key": "favorites_counter",        "label": "Show count on Add button",             "type": "bool"},
            {"key": "favorites_allow_clear_all","label": "Allow users to clear all favorites",   "type": "bool"},
        ],
        "stat_keys": ["favorites_total", "favorites_today"],
        "stat_labels": ["Total saved favorites", "Favorites added today"],
    },
    # ── V22: Product Compare ─────────────────────────────────────────────────
    {
        "id": "product_compare",
        "emoji": "⚖️",
        "name": "Product Compare",
        "enable_key": "feature_product_compare_enabled",
        "options": [
            {"key": "product_compare_max",      "label": "Max products to compare (2-4)", "type": "int"},
            {"key": "product_compare_counter",  "label": "Show compare counter on button", "type": "bool"},
            {"key": "product_compare_best_value", "label": "Highlight best value in table", "type": "bool"},
            {"key": "product_compare_show_unavailable", "label": "Allow unavailable products", "type": "bool"},
        ],
        "stat_keys": ["compare_total_sessions", "compare_purchased_after"],
        "stat_labels": ["Total comparison sessions", "Purchases after comparison"],
    },
    # ── V22: Subscription Expiry Reminder ────────────────────────────────────
    {
        "id": "subscription_reminder",
        "emoji": "🔔",
        "name": "Subscription Expiry Reminder",
        "enable_key": "feature_subscription_reminder_enabled",
        "options": [
            {
                "key": "sub_expiry_reminder_retry_failed",
                "label": "Retry failed reminder sends",
                "type": "bool",
            },
        ],
        "stat_keys": ["sub_reminder_active", "sub_reminder_sent"],
        "stat_labels": ["Active subscriptions", "Reminders sent (all time)"],
    },
    # ── V34: Backup Manager ───────────────────────────────────────────────────
    {
        "id": "backup_manager",
        "emoji": "💾",
        "name": "Backup Manager",
        "enable_key": "backup_manager_status",
        "options": [
            {"key": "backup_auto_settings_enabled",   "label": "Auto Settings Backup",      "type": "bool"},
            {"key": "backup_compression",             "label": "Backup Compression",         "type": "bool"},
            {"key": "backup_restore_confirm",         "label": "Restore Confirmation",       "type": "bool"},
            {"key": "backup_settings_interval_hours", "label": "Settings Backup Interval (h)","type": "int"},
            {"key": "backup_max_count",               "label": "Max Backups to Keep",        "type": "int"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V34: System Diagnostics Center ────────────────────────────────────────
    {
        "id": "diagnostics",
        "emoji": "🩺",
        "name": "System Diagnostics Center",
        "enable_key": "diagnostics_status",
        "options": [
            {"key": "diagnostics_auto_scan",            "label": "Auto Diagnostics Scan",    "type": "bool"},
            {"key": "diagnostics_admin_alerts",         "label": "Admin Alerts on Critical", "type": "bool"},
            {"key": "diagnostics_scan_interval_hours",  "label": "Scan Interval (hours)",    "type": "int"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V37: Admin Notification Center ────────────────────────────────────────
    {
        "id": "notification_center",
        "emoji": "🔔",
        "name": "Admin Notification Center",
        "enable_key": "notification_center_status",
        "options": [
            {"key": "notification_center_sound",           "label": "Enable Sound Notifications",    "type": "bool"},
            {"key": "notification_center_silent_mode",     "label": "Silent Mode",                   "type": "bool"},
            {"key": "notification_center_auto_delete",     "label": "Auto Delete Old Notifications", "type": "bool"},
            {"key": "notification_center_max",             "label": "Max Stored Notifications",      "type": "int"},
            {"key": "notification_center_retention_days",  "label": "Retention Period (days)",       "type": "int"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V37: File & License Key Manager ───────────────────────────────────────
    {
        "id": "file_license_manager",
        "emoji": "📂",
        "name": "File & License Key Manager",
        "enable_key": "file_license_manager_status",
        "options": [
            {"key": "flm_max_upload_size_mb",        "label": "Max Upload Size (MB)",         "type": "int"},
            {"key": "flm_allowed_types",             "label": "Allowed File Types (CSV)",     "type": "str"},
            {"key": "flm_auto_delete_expired",       "label": "Auto Delete Expired Files",    "type": "bool"},
            {"key": "flm_auto_archive_used_keys",    "label": "Auto Archive Used Keys",       "type": "bool"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V38: Flash Sale Manager ────────────────────────────────────────────────
    {
        "id": "flash_sale_manager",
        "emoji": "⚡",
        "name": "Flash Sale Manager",
        "enable_key": "flash_sale_manager_status",
        "options": [
            {"key": "fsm_auto_price_update",    "label": "Auto Price Update",                "type": "bool"},
            {"key": "fsm_auto_broadcast",       "label": "Auto Broadcast",                   "type": "bool"},
            {"key": "fsm_countdown_timer",      "label": "Show Countdown Timer",             "type": "bool"},
            {"key": "fsm_homepage_banner",      "label": "Homepage Banner",                  "type": "bool"},
            {"key": "fsm_product_badge",        "label": "Product Page Badge",               "type": "bool"},
            {"key": "fsm_stack_discounts",      "label": "Stack Discounts with Coupons",     "type": "bool"},
            {"key": "fsm_allow_multiple_sales", "label": "Allow Multiple Sales per Product", "type": "bool"},
            {"key": "fsm_broadcast_24h",        "label": "Broadcast: 24h Before End",        "type": "bool"},
            {"key": "fsm_broadcast_12h",        "label": "Broadcast: 12h Before End",        "type": "bool"},
            {"key": "fsm_broadcast_6h",         "label": "Broadcast: 6h Before End",         "type": "bool"},
            {"key": "fsm_broadcast_3h",         "label": "Broadcast: 3h Before End",         "type": "bool"},
            {"key": "fsm_broadcast_1h",         "label": "Broadcast: 1h Before End",         "type": "bool"},
            {"key": "fsm_broadcast_30m",        "label": "Broadcast: 30m Before End",        "type": "bool"},
            {"key": "fsm_broadcast_10m",        "label": "Broadcast: 10m Before End",        "type": "bool"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
    # ── V44.4: Enterprise Broadcast Campaign Manager ────────────────────────
    {
        "id": "broadcast_campaign_manager",
        "emoji": "📢",
        "name": "Broadcast Campaign Manager",
        "enable_key": "broadcast_campaigns_enabled",
        "options": [
            {"key": "broadcast_templates_enabled",          "label": "Template Library",         "type": "bool"},
            {"key": "broadcast_automation_enabled",         "label": "Automation Rules",          "type": "bool"},
            {"key": "broadcast_ab_testing_enabled",         "label": "A/B Testing",              "type": "bool"},
            {"key": "broadcast_recurring_campaigns_enabled","label": "Recurring Campaigns",       "type": "bool"},
            {"key": "broadcast_campaign_max_running",       "label": "Max running campaigns",     "type": "int"},
            {"key": "broadcast_template_max",               "label": "Max templates (0=unlimited)","type": "int"},
            {"key": "broadcast_automation_max_rules",       "label": "Max automation rules",      "type": "int"},
        ],
        "stat_keys": [],
        "stat_labels": [],
    },
]

_FEAT_BY_ID = {f["id"]: f for f in _FEATURES}


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _is_on(feature_id: str) -> bool:
    feat = _FEAT_BY_ID.get(feature_id)
    if not feat:
        return False
    return cfg.get_bool(feat["enable_key"], True)


# ─────────────────────────────────────────────────────────────────────────
# Main Feature Management Menu
# ─────────────────────────────────────────────────────────────────────────

async def features_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin Feature Management main menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    stats = get_feature_stats()

    lines = ["⚙ <b>Feature Management</b>\n"]
    lines.append("Enable or disable user features below.\n")
    lines.append("<b>Feature         Status   Stats</b>")

    kb: List[List[InlineKeyboardButton]] = []
    for feat in _FEATURES:
        on = cfg.get_bool(feat["enable_key"], True)
        status = "✅ ON" if on else "❌ OFF"
        stat_vals = " · ".join(
            str(stats.get(k, 0)) for k in feat["stat_keys"]
        )
        lines.append(
            f"{feat['emoji']} <b>{feat['name']}</b> — {status}  ({stat_vals})"
        )
        kb.append([InlineKeyboardButton(
            f"{feat['emoji']} {feat['name']} ({status})",
            callback_data=f"af:f:{feat['id']}",
        )])

    kb.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Feature Detail & Settings
# ─────────────────────────────────────────────────────────────────────────

async def feature_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings for a single feature (callback: af:f:<feature_id>)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        feature_id = _override if _override else query.data.split(":")[2]
    except IndexError:
        await query.answer("❌ Invalid feature.", show_alert=True)
        return

    feat = _FEAT_BY_ID.get(feature_id)
    if not feat:
        await query.answer("❌ Unknown feature.", show_alert=True)
        return

    stats = get_feature_stats()
    on = cfg.get_bool(feat["enable_key"], True)
    status_str = "✅ Enabled" if on else "❌ Disabled"

    lines = [f"{feat['emoji']} <b>{feat['name']}</b>\n", f"Status: {status_str}\n"]

    # Stats block
    if feat["stat_keys"]:
        lines.append("<b>Statistics:</b>")
        for k, label in zip(feat["stat_keys"], feat["stat_labels"]):
            lines.append(f"  • {label}: <b>{stats.get(k, 0)}</b>")
        lines.append("")

    # Options block
    if feat["options"]:
        lines.append("<b>Settings:</b>")
        for opt in feat["options"]:
            val = cfg.get_str(opt["key"], "")
            if opt["type"] == "bool":
                display = "✅ ON" if cfg.get_bool(opt["key"], True) else "🚫 OFF"
            else:
                display = val or "—"
            lines.append(f"  • {opt['label']}: <b>{display}</b>")

    kb: List[List[InlineKeyboardButton]] = []

    # Enable / Disable toggle
    if on:
        kb.append([InlineKeyboardButton(
            f"❌ Disable {feat['name']}",
            callback_data=f"af:off:{feature_id}",
        )])
    else:
        kb.append([InlineKeyboardButton(
            f"✅ Enable {feat['name']}",
            callback_data=f"af:on:{feature_id}",
        )])

    # Option buttons
    for opt in feat["options"]:
        if opt["type"] == "bool":
            current = cfg.get_bool(opt["key"], True)
            new_val = "false" if current else "true"
            toggle_label = f"{'🚫 Disable' if current else '✅ Enable'} {opt['label']}"
            kb.append([InlineKeyboardButton(
                toggle_label,
                callback_data=f"af:set:{opt['key']}:{new_val}",
            )])
        elif opt["type"] == "choice":
            # Show inline choices
            row = []
            for cval, clabel in opt["choices"]:
                current_val = cfg.get_str(opt["key"], "")
                mark = "✅ " if current_val == cval else ""
                row.append(InlineKeyboardButton(
                    f"{mark}{clabel}",
                    callback_data=f"af:set:{opt['key']}:{cval}",
                ))
                if len(row) >= 4:
                    kb.append(row)
                    row = []
            if row:
                kb.append(row)

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="af:menu")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────────────────────────────────
# Enable / Disable toggle
# ─────────────────────────────────────────────────────────────────────────

async def feature_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable a feature (callback: af:on:<feature_id>)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        feature_id = query.data.split(":")[2]
    except IndexError:
        return

    feat = _FEAT_BY_ID.get(feature_id)
    if not feat:
        return

    cfg.set(feat["enable_key"], True)
    await query.answer(f"✅ {feat['name']} enabled.", show_alert=False)
    # Refresh detail view
    context.user_data["_cb_data_override"] = feature_id
    await feature_detail(update, context)


async def feature_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable a feature (callback: af:off:<feature_id>)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        feature_id = query.data.split(":")[2]
    except IndexError:
        return

    feat = _FEAT_BY_ID.get(feature_id)
    if not feat:
        return

    cfg.set(feat["enable_key"], False)
    await query.answer(f"❌ {feat['name']} disabled.", show_alert=False)
    context.user_data["_cb_data_override"] = feature_id
    await feature_detail(update, context)


async def feature_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a feature option value (callback: af:set:<key>:<value>)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    # Format: af:set:<config_key>:<value>
    # config_key may contain underscores; value is last segment
    parts = query.data.split(":")
    if len(parts) < 4:
        return

    # parts: ["af", "set", key_part1_part2_..., value]
    # but key may have underscores — rebuild
    # callback_data format: af:set:FULLKEY:VALUE
    # We stored it as af:set:{key}:{val} where key uses underscores, val uses : separator
    # To be safe, split only on first 3 ":"s
    _, _, config_key, value = query.data.split(":", 3)

    cfg.set(config_key, value)

    # Find the feature that owns this key and navigate back to its detail
    feature_id = None
    for feat in _FEATURES:
        if feat["enable_key"] == config_key:
            feature_id = feat["id"]
            break
        for opt in feat["options"]:
            if opt["key"] == config_key:
                feature_id = feat["id"]
                break
        if feature_id:
            break

    if feature_id:
        context.user_data["_cb_data_override"] = feature_id
        await feature_detail(update, context)
    else:
        await features_menu(update, context)
