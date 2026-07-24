"""Database-backed runtime configuration.

Every tunable value the admin used to have to change in Python code lives here
as a row in the ``bot_config`` table. Values are cached in-process for a short
TTL so hot paths (payment checks, message handlers) don't hit the DB per call.

Usage:
    from utils.bot_config import cfg
    threshold = cfg.get_int("bulk_delivery_threshold", 10)
    cfg.set("bulk_delivery_threshold", 20)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

from database import get_db_session
from database.models import BotConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default catalogue — the single source of truth for every admin-tunable knob.
# Each entry: (key, value_type, default_value, category, label, description)
# ---------------------------------------------------------------------------

DEFAULTS: List[Tuple[str, str, Any, str, str, str]] = [
    # ── Payments ────────────────────────────────────────────────────────────
    ("payment_expiry_minutes", "int", 30, "payments",
     "Payment Expiry (minutes)",
     "How long a pending payment stays valid before being auto-expired."),
    ("payment_check_interval_seconds", "int", 30, "payments",
     "Payment Check Interval (seconds)",
     "How often the bot polls for on-chain / manual payments. Takes effect on restart."),
    ("auto_refund_enabled", "bool", True, "payments",
     "Auto-Refund on Delivery Failure",
     "If ON, failed deliveries automatically refund the user's wallet."),
    ("auto_refund_after_minutes", "int", 5, "payments",
     "Auto-Refund Delay (minutes)",
     "Wait this many minutes after a failure before refunding (0 = immediate)."),

    # ── Payments (v2) ──────────────────────────────────────────────────────
    ("minimum_deposit_enabled", "bool", False, "payments",
     "Global Minimum Deposit: Enabled",
     "If ON, users cannot deposit less than the configured minimum amount. "
     "If OFF, any positive amount is accepted across all payment gateways."),
    ("topup_min_amount", "float", 1.0, "payments",
     "Global Minimum Deposit Amount (USD)",
     "The minimum deposit amount enforced when 'Minimum Deposit: Enabled' is ON. "
     "Ignored when the toggle is OFF. Applies to all payment gateways."),
    ("topup_max_amount", "float", 0.0, "payments",
     "Global Maximum Top-up (USD)",
     "Ceiling for a single top-up. 0 = no ceiling."),
    ("manual_require_txid_default", "bool", True, "payments",
     "Manual Payments: Require TXID by default",
     "Applied to newly created manual payment methods."),
    ("manual_require_proof_default", "bool", True, "payments",
     "Manual Payments: Require Screenshot by default",
     "Applied to newly created manual payment methods."),

    # ── Delivery ────────────────────────────────────────────────────────────
    ("bulk_delivery_threshold", "int", 10, "inventory",
     "Bulk Delivery Threshold",
     "Orders with MORE than this many keys are delivered as a .txt file. "
     "10 = up to 10 keys inline, 11+ as file."),
    ("bulk_delivery_caption", "text", "📎 Your {qty} keys for {product} are attached above.",
     "inventory",
     "Bulk File Caption",
     "Sent with the .txt file. Placeholders: {qty}, {product}, {order_id}."),

    # ── Inventory ───────────────────────────────────────────────────────────
    ("low_stock_threshold", "int", 5, "inventory",
     "Low-Stock Threshold",
     "Products with stock at or below this number show up in the low-stock alert."),
    ("inventory_reservation_ttl_minutes", "int", 15, "inventory",
     "Reservation TTL (minutes)",
     "How long a stock reservation is held during checkout before it auto-expires "
     "and the reserved keys/units are returned to inventory. Must be > 0."),

    # ── Templates ───────────────────────────────────────────────────────────
    ("delivery_message_header", "text",
     "✅ *Payment Successful!*\n\n🎉 Thank you for your purchase!",
     "ops",
     "Delivery Message Header",
     "Header shown above every successful delivery."),
    ("receipt_footer", "text",
     "Thank you for shopping with us!",
     "ops",
     "Receipt Footer",
     "Printed at the bottom of every PDF receipt."),

    # ── Invoicing (auto PDF invoice on order completion) ───────────────────
    ("business_name", "str", "",
     "invoicing",
     "Business Name",
     "Shown on the PDF invoice header. Empty = use the first line of the Welcome Message."),
    ("business_address", "text", "",
     "invoicing",
     "Business Address",
     "Optional postal / registered address printed on the invoice."),
    ("business_email", "str", "",
     "invoicing",
     "Business Email",
     "Optional contact email printed on the invoice."),
    ("business_phone", "str", "",
     "invoicing",
     "Business Phone",
     "Optional contact phone number printed on the invoice."),
    ("invoice_auto_send", "bool", True,
     "invoicing",
     "Auto-Send Invoice on Completion",
     "When ON, a PDF invoice is generated and DM'd to the buyer automatically "
     "the moment their order completes."),

    # ── Static pages ────────────────────────────────────────────────────────
    ("page_terms", "text", "By using this bot you agree to our terms of service.",
     "ops", "Terms of Service", "Shown when the user opens Terms."),
    ("page_faq", "text", "Q: How do I top up?\nA: Tap Top Up in the main menu.",
     "ops", "FAQ", "Shown when the user opens FAQ."),
    ("page_about", "text", "We sell digital keys and files. Fast, automated, 24/7.",
     "ops", "About", "Shown when the user opens About."),

    # ── Home Dashboard (/start & Main Menu card) ────────────────────────────
    ("home_title", "str", "🛍️ Premium Digital Store",
     "home_dashboard", "Home: Title",
     "Bold headline shown at the top of the /start & Main Menu dashboard card. "
     "Leave empty to use the built-in localized default."),
    ("home_subtitle", "text", "✨ Premium AI subscriptions, software licenses, and digital products.",
     "home_dashboard", "Home: Subtitle",
     "One-line tagline shown under the title. "
     "Leave empty to use the built-in localized default."),
    ("home_wallet_label", "str", "💳 Wallet Balance",
     "home_dashboard", "Home: Wallet Label",
     "Label shown above the user's wallet balance (rendered in monospace). "
     "Leave empty to use the built-in localized default."),
    ("home_footer", "str", "👇 Select an option below.",
     "home_dashboard", "Home: Footer",
     "Short call-to-action shown at the bottom of the dashboard card, above the menu buttons. "
     "Leave empty to use the built-in localized default."),

    # ── Operations ──────────────────────────────────────────────────────────
    ("webhook_base_url", "str", "", "ops",
     "Webhook Base URL",
     "Public HTTPS base URL of this bot's webhook server, e.g. "
     "https://yourdomain.com (no trailing slash). Required for Heleket "
     "Static Wallet and ZiniPay so they know where to send payment "
     "notifications. Falls back to the WEBHOOK_URL environment variable "
     "if left empty — set here if you don't have access to edit env vars."),
    ("maintenance_mode", "bool", False, "ops",
     "Maintenance Mode",
     "When ON, non-admin users receive the maintenance message and no other "
     "handler runs. Admins are unaffected."),
    ("maintenance_message", "text",
     "🔧 The bot is under maintenance. Please try again in a few minutes.",
     "ops",
     "Maintenance Message",
      "Shown to users while maintenance mode is on."),

    # ── V9 (Premium Admin Control Center) ──────────────────────────────────
    # Notifications
    ("notif_new_order", "bool", True, "notifications",
     "Notify: New Order", "Send admin a message when a new order is placed."),
    ("notif_manual_payment", "bool", True, "notifications",
     "Notify: Manual Payment", "Send admin a message on new manual payment submissions."),
    ("notif_dispute", "bool", True, "notifications",
     "Notify: New Dispute", "Send admin a message when a user opens a dispute."),
    ("notif_low_stock", "bool", True, "notifications",
     "Notify: Low Stock", "Send admin an alert when a product falls at or below the low-stock threshold."),
    ("notif_refund", "bool", True, "notifications",
     "Notify: Refund", "Send admin a message when a refund is issued."),
    ("notif_ticket_reply", "bool", True, "notifications",
     "Notify: Ticket Reply", "Send admin a message when a user replies on a support ticket."),
    ("low_stock_check_interval_minutes", "int", 30, "notifications",
     "Low-Stock Job Interval (minutes)",
     "How often the background job scans inventory for low-stock alerts."),

    # ── Notification Settings module (destination routing) ─────────────────
    ("notif_settings_mode", "str", "admin", "notifications",
     "Notification Delivery Mode",
     "Where admin notifications are delivered: admin | log_channel | both."),
    ("notif_settings_log_channel_id", "str", "", "notifications",
     "Notification Log Channel ID",
     "Chat ID of the channel used for log-channel notification delivery."),
    ("notif_settings_log_channel_title", "str", "", "notifications",
     "Notification Log Channel Title",
     "Cached display title of the configured log channel (set on validation)."),
    ("notif_settings_log_channel_verified", "bool", False, "notifications",
     "Notification Log Channel Verified",
     "Whether the configured log channel passed the bot-access validation check."),

    # Wallets
    ("wallet_max_manual_adjust", "float", 1000.0, "wallets",
     "Wallet: Max Manual Adjust (USD)",
     "Ceiling on a single admin credit/debit. 0 = no ceiling."),
    ("wallet_require_reason", "bool", True, "wallets",
     "Wallet: Require Reason",
     "Force admin to type a reason for every manual balance adjustment."),

    # Promotions
    ("promotions_enabled", "bool", True, "promotions",
     "Promotions: Enabled",
     "Master switch for the Promotions section of the admin panel."),

    # System / Ops (extends existing ops category)
    ("admin_2step_confirm_destructive", "bool", True, "system",
     "Two-Step Confirm on Destructive Actions",
     "Require a confirmation tap for refund / ban / delete actions."),
    ("dashboard_default_range_days", "int", 7, "system",
     "Dashboard Default Range (days)",
     "Default window for revenue/KPI charts on the dashboard."),
     ("audit_retention_days", "int", 180, "system",
      "Audit Retention (days)",
      "Informational only — audit rows are append-only; rotate manually if needed."),

    # ── V45: Admin Panel UI Settings ────────────────────────────────────────
    ("admin_panel_status", "str", "enabled", "admin_ui",
     "Admin Panel Status",
     "Controls the admin panel UI state: enabled / maintenance / disabled."),
    ("admin_panel_categories", "bool", True, "admin_ui",
     "Admin Panel: Enable Categories",
     "Show the 8-category navigation layout on the admin root panel."),
    ("admin_panel_search", "bool", True, "admin_ui",
     "Admin Panel: Enable Global Search",
     "Show the 🔍 Admin Search shortcut on the root panel."),
    ("admin_panel_favorites", "bool", True, "admin_ui",
     "Admin Panel: Enable Favorites",
     "Allow admins to pin frequently used menus."),
    ("admin_panel_recent", "bool", True, "admin_ui",
     "Admin Panel: Enable Recent Menus",
     "Track and display recently visited admin menus."),
    ("admin_panel_compact", "bool", False, "admin_ui",
     "Admin Panel: Compact Mode",
     "Show two feature buttons per row in category submenus (saves scrolling)."),
    ("admin_panel_icons", "bool", True, "admin_ui",
     "Admin Panel: Show Icons",
     "Prefix category labels with emoji icons on the root panel."),
    ("admin_panel_breadcrumb", "bool", True, "admin_ui",
     "Admin Panel: Breadcrumb Navigation",
     "Show the navigation path (e.g. Admin › Products › Page 1) above category submenus."),

    # ── V10: Business Scale & Operations ────────────────────────────────────
    ("reseller_system_enabled", "bool", True, "system",
     "Reseller System Enabled",
     "When OFF, pricing quotes skip reseller tier discounts."),
    ("delivery_max_attempts", "int", 5, "inventory",
     "Delivery Max Attempts",
     "Cap on retries for a single delivery job before it is marked FAILED."),
    ("backup_enabled", "bool", False, "backups",
     "Scheduled Backups Enabled",
     "When ON, the pg_dump backup job runs on the configured interval."),
    ("backup_interval_hours", "int", 24, "backups",
     "Backup Interval (hours)",
     "How often the scheduled backup runs."),
    ("backup_retention_count", "int", 14, "backups",
     "Backup Retention Count",
     "Number of most-recent SUCCESS backups to keep. Newest is always preserved."),
    ("integrity_scan_interval_hours", "int", 24, "diagnostics",
     "Integrity Scan Interval (hours)",
     "Automatic read-only integrity scan cadence. Set 0 to disable."),

    # ── V34: Backup Manager settings ─────────────────────────────────────────
    ("backup_manager_status", "str", "enabled", "backups",
     "Backup Manager Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("backup_auto_settings_enabled", "bool", False, "backups",
     "Auto Settings Backup Enabled",
     "When ON, settings are backed up automatically on the configured interval."),
    ("backup_settings_interval_hours", "int", 24, "backups",
     "Settings Backup Interval (hours)",
     "How often the automatic settings backup job runs."),
    ("backup_max_count", "int", 30, "backups",
     "Maximum Backup Count",
     "Total number of settings backups to keep. Oldest are pruned first."),
    ("backup_restore_confirm", "bool", True, "backups",
     "Restore Confirmation Required",
     "When ON, admin must confirm before any restore operation is applied."),
    ("backup_compression", "bool", True, "backups",
     "Backup Compression",
     "When ON, settings backup files are gzip-compressed."),

    # ── V34: Diagnostics Center settings ─────────────────────────────────────
    ("diagnostics_status", "str", "enabled", "diagnostics",
     "Diagnostics Center Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("diagnostics_auto_scan", "bool", False, "diagnostics",
     "Auto Diagnostics Scan Enabled",
     "When ON, a diagnostics scan runs automatically at the configured interval."),
    ("diagnostics_scan_interval_hours", "int", 6, "diagnostics",
     "Diagnostics Scan Interval (hours)",
     "How often the automatic diagnostics scan runs."),
    ("diagnostics_admin_alerts", "bool", True, "diagnostics",
     "Diagnostics Admin Alerts",
     "When ON, admin receives a message when Critical issues are detected."),

    # ── Order History & Order Details ────────────────────────────────────────
    ("order_history_status", "str", "enabled", "orders",
     "📋 Order History Status",
     "Feature status for Order History & Details: enabled | maintenance | disabled. "
     "Maintenance shows a notice to users; disabled hides history entirely."),
    ("orders_per_page", "int", 10, "orders",
     "📋 Orders Per Page",
     "Maximum orders shown per page (3–20). Default: 10. "
     "Changes take effect immediately without restart."),
    ("order_history_enable_timeline", "bool", True, "orders",
     "📋 Enable Order Timeline",
     "Show the 📜 Order Timeline section inside Order Details."),
    ("order_history_enable_receipt", "bool", True, "orders",
     "📋 Enable Receipt Download",
     "Show the 🧾 Download Receipt button for completed orders."),
    ("order_history_enable_buy_again", "bool", True, "orders",
     "📋 Enable Buy Again",
     "Show a 🔄 Buy Again shortcut in completed Order Details, "
     "taking the user directly back to the product page."),
    ("order_history_enable_copy_buttons", "bool", True, "orders",
     "📋 Enable Copy Buttons",
     "Show 📋 Copy buttons for delivered fields (key, link, username, email, password)."),
    ("order_history_enable_security_masking", "bool", True, "orders",
     "📋 Enable Security Masking",
     "Mask password fields by default in Order Details. "
     "User must tap 👁 Show to reveal the plaintext value."),
    ("order_history_enable_review", "bool", False, "orders",
     "📋 Enable Review Button",
     "Show the ⭐ Review button in Order Details for eligible products. "
     "OFF by default to keep the Order Details view compact."),
    ("order_history_enable_dispute", "bool", False, "orders",
     "📋 Enable Dispute Button",
     "Show the 🚨 Open Dispute button in Order Details. "
     "OFF by default to keep the Order Details view compact."),

    # ── Product Pagination (paginated flat catalog) ──────────────────────────
    ("product_pagination_status", "str", "enabled", "catalog",
     "🛍 Product List Status",
     "Feature status: enabled | maintenance | disabled. "
     "Maintenance shows a notice to users; disabled hides the list entirely."),
    ("products_per_page", "int", 20, "catalog",
     "🛍 Products Per Page",
     "Maximum products shown per page (1–50). Default: 20. "
     "Changes take effect immediately without restart."),
    ("product_list_allow_pagination", "bool", True, "catalog",
     "🛍 Allow Pagination",
     "Show ⬅ Previous / ➡ Next navigation when total products exceed one page. "
     "When OFF, all products are shown on a single (potentially long) screen."),
    ("product_list_refresh_button", "bool", True, "catalog",
     "🛍 Show Refresh Button",
     "Show the 🔄 Refresh button at the bottom of the product list."),
    ("product_list_show_stock", "bool", True, "catalog",
     "🛍 Show Stock Count",
     "Append remaining stock to each product button, e.g. '(15 left)'."),
    ("product_list_show_counter", "bool", True, "catalog",
     "🛍 Show Product Counter",
     "Display the total available product count in the header, "
     "e.g. '🛍 Products (42 Available)'."),

    # -- Catalog Badges -------------------------------------------------------
    ("new_product_days", "int", 7, "catalog",
     "New Badge Window (days)",
     "Products created within this many days show a New badge "
     "in the catalog list and product detail view."),
    ("flat_product_catalog", "bool", False, "catalog",
     "Flat Product List (Skip Categories)",
     "If ON, tapping 'Products' shows every active product directly in one "
     "paginated list, skipping the category/subcategory picker. Categories "
     "are NOT deleted — they're just bypassed in this shopping flow, so you "
     "can turn this back OFF anytime to restore the category-first browsing."),
    ("social_proof_cache_enabled", "bool", True, "catalog",
     "Social Proof: Cache Enabled",
     "Cache the ⭐ rating / sold-count numbers shown on product pages instead "
     "of recomputing them from Reviews/Orders on every view. Turn OFF only "
     "for small catalogs where you want always-live numbers."),
    ("social_proof_cache_seconds", "int", 300, "catalog",
     "Social Proof: Cache TTL (seconds)",
     "How long the ⭐ rating / sold-count numbers stay cached before being "
     "recomputed. Default 300 = 5 minutes. Only applies when caching is enabled."),

    # ── Payment Gateways: bKash / Nagad ─────────────────────────────────────
    ("bkash_enabled", "bool", False, "gateways",
     "bKash: Enabled",
     "Show bKash as a top-up option to users. Requires app key/secret + username/password below."),
    ("bkash_mode", "str", "sandbox", "gateways",
     "bKash: Mode",
     "'sandbox' for testing or 'live' for real payments."),
    ("bkash_app_key", "str", "", "gateways",
     "bKash: App Key",
     "From the bKash Merchant/PGW portal."),
    ("bkash_app_secret", "str", "", "gateways",
     "bKash: App Secret",
     "From the bKash Merchant/PGW portal. Stored in the database — see security note in services/bkash_payment.py."),
    ("bkash_username", "str", "", "gateways",
     "bKash: API Username",
     "From the bKash Merchant/PGW portal."),
    ("bkash_password", "str", "", "gateways",
     "bKash: API Password",
     "From the bKash Merchant/PGW portal."),
    ("bkash_min_amount", "float", 10.0, "gateways",
     "bKash: Minimum Top-up (BDT-equivalent USD)",
     "Lowest top-up amount that offers bKash as a payment option."),
    ("bkash_max_amount", "float", 0.0, "gateways",
     "bKash: Maximum Top-up (USD)",
     "Ceiling for a single bKash top-up. 0 = no ceiling."),

    ("nagad_enabled", "bool", False, "gateways",
     "Nagad: Enabled",
     "Show Nagad as a top-up option to users. Requires merchant ID/number + RSA keys below."),
    ("nagad_mode", "str", "sandbox", "gateways",
     "Nagad: Mode",
     "'sandbox' for testing or 'live' for real payments."),
    ("nagad_merchant_id", "str", "", "gateways",
     "Nagad: Merchant ID",
     "From the Nagad Merchant Onboarding portal."),
    ("nagad_merchant_number", "str", "", "gateways",
     "Nagad: Merchant Number",
     "The Nagad account number registered for this merchant."),
    ("nagad_public_key", "text", "", "gateways",
     "Nagad: Gateway Public Key",
     "Nagad's public key (PEM, or just the base64 body) — used to encrypt requests."),
    ("nagad_private_key", "text", "", "gateways",
     "Nagad: Merchant Private Key",
     "Your merchant private key (PEM, or just the base64 body) — used to sign requests. "
     "Stored in the database — see security note in services/nagad_payment.py."),
    ("nagad_min_amount", "float", 10.0, "gateways",
     "Nagad: Minimum Top-up (USD)",
     "Lowest top-up amount that offers Nagad as a payment option."),
    ("nagad_max_amount", "float", 0.0, "gateways",
     "Nagad: Maximum Top-up (USD)",
     "Ceiling for a single Nagad top-up. 0 = no ceiling."),

    # ── V12: Broadcast Center ───────────────────────────────────────────────
    ("restock_broadcast_enabled", "bool", False, "broadcast",
     "Automatic Restock Broadcast",
     "When ON, an automatic broadcast is sent to eligible users the moment a "
     "product's available stock changes from 0 to more than 0. Manual Product "
     "Broadcast from the admin panel always works regardless of this setting."),

    # ── V18: Channel Auto-Post ──────────────────────────────────────────────
    ("channel_autopost_enabled", "bool", False, "broadcast",
     "Channel Auto-Post: Enabled",
     "When ON, the bot automatically posts to the Channel ID below whenever "
     "a new product is created, or whenever a product's stock goes from 0 "
     "to more than 0 (restock). The bot must be an admin of that channel."),
    ("channel_autopost_channel_id", "str", "", "broadcast",
     "Channel Auto-Post: Channel ID",
     "The channel to post to — either its numeric ID (e.g. -1001234567890) "
     "or its public @username (e.g. @mystorechannel). Leave empty to disable "
     "posting even if the toggle above is ON."),

    # ── Marketing Automation (V14): abandoned cart + win-back ────────────────
    ("marketing_cart_reminders_enabled", "bool", True, "marketing",
     "Abandoned Cart Reminders",
     "When ON, users with items sitting in their cart get an automatic reminder "
     "with a discount code."),
    ("marketing_cart_reminder_30m_minutes", "int", 30, "marketing",
     "Cart Reminder #1 Delay (minutes)",
     "How long a cart must sit untouched before the first reminder is sent."),
    ("marketing_cart_reminder_24h_hours", "int", 24, "marketing",
     "Cart Reminder #2 Delay (hours)",
     "How long a cart must sit untouched before the escalation reminder (bigger "
     "discount) is sent."),
    ("marketing_cart_reminder_30m_discount_percent", "int", 10, "marketing",
     "Cart Reminder #1 Discount (%)",
     "Percent-off coupon auto-generated for the first abandoned-cart reminder."),
    ("marketing_cart_reminder_24h_discount_percent", "int", 15, "marketing",
     "Cart Reminder #2 Discount (%)",
     "Percent-off coupon auto-generated for the escalation reminder (usually "
     "higher than reminder #1 to close the sale)."),
    ("marketing_cart_coupon_validity_hours", "int", 48, "marketing",
     "Cart Coupon Validity (hours)",
     "How long an auto-generated abandoned-cart coupon stays valid after "
     "being issued."),
    ("marketing_winback_enabled", "bool", True, "marketing",
     "Win-Back Offers",
     "When ON, users who go quiet get an automatic win-back offer with a "
     "discount code."),
    ("marketing_winback_7d_days", "int", 7, "marketing",
     "Win-Back Tier 1 (days inactive)",
     "Days of inactivity before the first win-back offer is sent."),
    ("marketing_winback_30d_days", "int", 30, "marketing",
     "Win-Back Tier 2 (days inactive)",
     "Days of inactivity before the bigger, second win-back offer is sent."),
    ("marketing_winback_7d_discount_percent", "int", 10, "marketing",
     "Win-Back Tier 1 Discount (%)",
     "Percent-off coupon for the 7-day inactivity win-back offer."),
    ("marketing_winback_30d_discount_percent", "int", 20, "marketing",
     "Win-Back Tier 2 Discount (%)",
     "Percent-off coupon for the 30-day inactivity win-back offer."),
    ("marketing_winback_coupon_validity_days", "int", 7, "marketing",
     "Win-Back Coupon Validity (days)",
     "How long an auto-generated win-back coupon stays valid after being "
     "issued."),
    ("marketing_check_interval_minutes", "int", 15, "marketing",
     "Marketing Job Check Interval (minutes)",
     "How often the bot scans for abandoned carts and inactive users. Takes "
     "effect on restart."),

    # ── Main Menu (UI) ──────────────────────────────────────────────────────
    ("show_currency_toggle_button", "bool", False, "system",
     "Show Currency Toggle Button",
     "When ON, the \"🌐 Currency: USD (tap for BDT)\" row is shown on the "
     "user-facing main menu. When OFF (default), the row is hidden and the "
     "rest of the main menu keyboard is unaffected."),

    # ── V16: Customer Segmentation (Broadcast targeting) ────────────────────
    ("seg_vip_spend_threshold", "float", 100.0, "crm",
     "VIP Spend Threshold (USD)",
     "Users with total completed-order spend at or above this amount are "
     "included in the VIP broadcast segment."),
    ("seg_high_freq_order_count", "int", 3, "crm",
     "High-Frequency Order Count",
     "Users with this many completed orders (or more) are included in the "
     "High-Frequency Buyers broadcast segment."),
    ("seg_inactive_days", "int", 30, "crm",
     "Inactive Threshold (days)",
     "Users whose most recent completed order is older than this many days "
     "are included in the Inactive (Lapsed) broadcast segment."),

    # ── V18: User Features ──────────────────────────────────────────────────
    ("feature_wishlist_enabled", "bool", True, "features",
     "❤️ Wishlist: Enabled",
     "Show the Wishlist feature to users — lets them save products for later."),
    ("feature_wishlist_max", "int", 50, "features",
     "❤️ Wishlist: Max Items per User (0 = unlimited)",
     "Maximum products a user can save in their wishlist. 0 = no limit."),
    ("feature_wishlist_counter", "bool", True, "features",
     "❤️ Wishlist: Show Item Count on Button",
     "Show the number of saved items next to the Wishlist button label."),

    ("feature_price_alerts_enabled", "bool", True, "features",
     "🔔 Price Drop Alerts: Enabled",
     "Allow users to subscribe to price-drop notifications on products."),
    ("feature_price_alerts_auto_notify", "bool", True, "features",
     "🔔 Price Drop Alerts: Auto-Notify on Price Change",
     "Automatically message subscribed users when an admin reduces a product price."),

    ("feature_recently_viewed_enabled", "bool", True, "features",
     "🕒 Recently Viewed: Enabled",
     "Track and display the last N products each user viewed."),
    ("feature_recently_viewed_max", "int", 20, "features",
     "🕒 Recently Viewed: History Size per User",
     "Maximum recently-viewed products to store per user (10 / 20 / 50 / 100)."),
    ("feature_recently_viewed_clean_deleted", "bool", True, "features",
     "🕒 Recently Viewed: Auto-Remove Deleted / Inactive Products",
     "When ON, deleted or deactivated products are hidden from the history list."),
    ("recently_viewed_status", "str", "enabled", "features",
     "🕒 Recently Viewed: Status",
     "3-state status toggle: enabled / maintenance / disabled. "
     "Admins can always access the list regardless of status."),
    ("recently_viewed_allow_clear_all", "bool", True, "features",
     "🕒 Recently Viewed: Allow Clear All",
     "When ON, users can clear their entire recently-viewed history with one tap."),

    # ── Price History (V23) ─────────────────────────────────────────────────
    ("price_history_enabled", "bool", True, "features",
     "📈 Price History: Enabled",
     "Master toggle. When OFF, no history is recorded and the button is hidden."),
    ("price_history_status", "str", "enabled", "features",
     "📈 Price History: Status",
     "3-state status toggle: enabled / maintenance / disabled."),
    ("price_history_max_records", "int", 50, "features",
     "📈 Price History: Max Records per Product",
     "Maximum price-history records stored per product (10/20/50/100/0=unlimited). "
     "Oldest records are evicted when the cap is reached."),
    ("price_history_allow_users", "bool", True, "features",
     "📈 Price History: Allow Users to View",
     "When ON, the 📈 Price History button appears on product pages for all users."),
    ("price_history_show_difference", "bool", True, "features",
     "📈 Price History: Show Price Difference",
     "When ON, the Δ price change line is shown in the timeline."),
    ("price_history_show_pct_change", "bool", True, "features",
     "📈 Price History: Show Percentage Change",
     "When ON, the % change line is shown in the timeline."),
    ("price_history_record_admin_name", "bool", True, "features",
     "📈 Price History: Record Admin Name",
     "When ON, the admin's display name is saved with each price change record."),

    # ── Inventory Reservation System (V23) ──────────────────────────────────
    ("irs_enabled", "bool", True, "inventory",
     "⏳ Inventory Reservation: Enabled",
     "Master toggle for the user-facing stock reservation system."),
    ("irs_status", "str", "enabled", "inventory",
     "⏳ Inventory Reservation: Status",
     "3-state status toggle: enabled / maintenance / disabled."),
    ("irs_allow_manual_release", "bool", True, "inventory",
     "⏳ Inventory Reservation: Allow Manual Release",
     "When ON, users can cancel their own active reservation before it expires."),
    ("irs_max_per_user", "int", 1, "inventory",
     "⏳ Inventory Reservation: Max Reservations per User",
     "Maximum simultaneous active reservations a single user can hold (0 = unlimited)."),
    ("irs_auto_release", "bool", True, "inventory",
     "⏳ Inventory Reservation: Auto Release on Expiry",
     "When ON, expired reservations are automatically released by the background job."),

    # ── Supplier Auto Assignment (V24) ───────────────────────────────────────
    ("sas_enabled", "bool", True, "inventory",
     "🤖 Supplier Auto Assignment: Enabled",
     "Master toggle for the Supplier Auto Assignment engine. "
     "When ON, key reservations prefer the highest-priority supplier's inventory. "
     "When OFF, any available key is picked (pre-V24 behaviour)."),
    ("sas_fallback_to_any", "bool", True, "inventory",
     "🤖 Supplier Auto Assignment: Fallback to Any Supplier",
     "When ON, if the preferred supplier has insufficient stock, the engine "
     "picks from any remaining available keys. "
     "When OFF, the order fails if the preferred supplier is out of stock."),

    # ── Order Timeline System (V25) ──────────────────────────────────────────
    ("ots_status", "str", "enabled", "ops",
     "📋 Order Timeline: Status",
     "3-state status for the Order Timeline feature: enabled / maintenance / disabled."),
    ("ots_show_to_users", "bool", True, "ops",
     "📋 Order Timeline: Show to Users",
     "When ON, users can view the full order timeline from their order detail page."),
    ("ots_show_processing_time", "bool", True, "ops",
     "📋 Order Timeline: Show Processing Time",
     "When ON, the timeline shows elapsed time from order creation to current status."),
    ("ots_show_estimated_delivery", "bool", False, "ops",
     "📋 Order Timeline: Show Estimated Delivery",
     "When ON, an estimated delivery time is shown on the timeline (if configured)."),
    ("ots_allow_manual_status", "bool", True, "ops",
     "📋 Order Timeline: Allow Admin Manual Status Update",
     "When ON, admins can manually change an order's lifecycle status from the timeline panel."),
    ("ots_notify_users", "bool", True, "ops",
     "📋 Order Timeline: Notify Users on Status Change",
     "When ON, users receive a Telegram DM whenever their order status changes."),

    # ── Product FAQ System (V25) ──────────────────────────────────────────────
    ("pfaq_status", "str", "enabled", "ops",
     "❓ Product FAQ: Status",
     "3-state status: enabled / maintenance / disabled."),
    ("pfaq_max_per_product", "str", "20", "ops",
     "❓ Product FAQ: Max FAQs Per Product",
     "Maximum number of FAQs allowed per product. Set to 0 for unlimited."),
    ("pfaq_show_counter", "bool", True, "ops",
     "❓ Product FAQ: Show Counter",
     "When ON, the FAQ count is shown on the FAQ button (e.g. ❓ FAQ (5))."),
    ("pfaq_allow_search", "bool", True, "ops",
     "❓ Product FAQ: Allow Search",
     "When ON, users can search FAQs by keyword from the product FAQ page."),
    ("pfaq_expand_first", "bool", False, "ops",
     "❓ Product FAQ: Expand First Question",
     "When ON, the first FAQ's answer is highlighted/shown first in the list."),

    ("feature_quick_buy_enabled", "bool", True, "features",
     "⚡ Quick Buy: Enabled",
     "Remember the last payment method + quantity per product for one-click checkout."),
    ("feature_quick_buy_max", "int", 10, "features",
     "⚡ Quick Buy: Max Remembered Products per User",
     "How many products to remember quick-buy settings for (10 / 20 / 50)."),

    ("feature_preferred_payment_enabled", "bool", True, "features",
     "⭐ Preferred Payment: Enabled",
     "Let users choose and store a preferred payment method, highlighted at checkout."),

    ("feature_buy_again_enabled", "bool", True, "features",
     "🔁 Buy Again: Enabled",
     "Show users a list of their previously purchased (available) products."),
    ("feature_buy_again_max", "int", 20, "features",
     "🔁 Buy Again: Max History Items Shown",
     "Maximum past purchases to display in the Buy Again list (10 / 20 / 50 / 100)."),

    # ── V19: Account & Order Features ──────────────────────────────────────
    ("feature_receipt_enabled", "bool", True, "account_features",
     "🧾 Auto Receipt: Enabled",
     "Automatically generate a receipt after every successful purchase or wallet deposit."),
    ("feature_receipt_header", "str", "", "account_features",
     "🧾 Receipt Header Text",
     "Custom text shown at the top of every receipt. Leave empty to skip."),
    ("feature_receipt_footer", "str", "Thank you for your purchase!", "account_features",
     "🧾 Receipt Footer Text",
     "Custom text shown at the bottom of every receipt."),
    ("feature_order_status_enabled", "bool", True, "account_features",
     "📦 Order Status System: Enabled",
     "Let users view their order status timeline and full history."),
    ("feature_order_expiry_hours", "int", 0, "account_features",
     "📦 Order Expiry (hours)",
     "Hours after which an order is considered expired. 0 = never."),
    ("feature_download_center_enabled", "bool", True, "account_features",
     "📁 Download Center: Enabled",
     "Give users access to all previously delivered assets (keys, files, codes) for re-download."),
    ("feature_download_max", "int", 0, "account_features",
     "📁 Max Downloads per Item",
     "Maximum times a user can access/download a single item. 0 = unlimited."),
    ("feature_download_expiry_days", "int", 0, "account_features",
     "📁 Download Expiry (days)",
     "Days after which a download expires and is no longer accessible. 0 = never."),
    ("feature_activity_history_enabled", "bool", True, "account_features",
     "📜 Activity History: Enabled",
     "Record and display a log of user actions (purchases, deposits, logins, etc.)."),
    ("feature_activity_max", "int", 100, "account_features",
     "📜 Max Activity History Entries",
     "Maximum activity log entries kept per user. 0 = unlimited. Oldest removed when exceeded."),
    ("feature_security_center_enabled", "bool", True, "account_features",
     "🔒 Security Center: Enabled",
     "Show users their last login, last purchase, active sessions, and session management."),
    ("feature_session_timeout_hours", "int", 0, "account_features",
     "🔒 Session Timeout (hours)",
     "Automatically expire user sessions after this many hours of inactivity. 0 = never."),
    ("feature_new_login_notification", "bool", False, "account_features",
     "🔒 New Login Notification",
     "Notify users when a new session is created for their account."),
    ("feature_security_alerts", "bool", True, "account_features",
     "🔒 Security Alerts",
     "Show security-related status in the Security Center."),

    # ── Part 3: Sales & Marketing ───────────────────────────────────────────
    ("feature_gift_purchase_enabled", "bool", True, "marketing",
     "🎁 Gift Purchase: Enabled",
     "Allow users to buy products as gifts for other Telegram users."),
    ("feature_gift_allow_anonymous", "bool", True, "marketing",
     "🎁 Gift Purchase: Allow Anonymous Gifts",
     "Let senders hide their identity when sending a gift."),

    ("feature_gift_cards_enabled", "bool", True, "marketing",
     "🎟 Gift Cards: Enabled",
     "Allow admins to create redeemable gift card codes for users."),

    ("feature_reviews_enabled", "bool", True, "marketing",
     "⭐ Product Reviews: Enabled",
     "Let users leave star ratings and comments on products they've purchased."),
    ("feature_reviews_require_approval", "bool", False, "marketing",
     "⭐ Product Reviews: Require Admin Approval",
     "When ON, new reviews are hidden until an admin approves them. Existing reviews stay visible."),

    ("feature_bundles_show_savings", "bool", True, "marketing",
     "📦 Bundles: Show Savings Badge",
     "Display the customer's savings amount on bundle product pages."),
    ("feature_bundles_show_contents", "bool", True, "marketing",
     "📦 Bundles: Show Included Products",
     "List the products included in a bundle on the bundle detail page."),

    # ── V20: Advanced Referral Dashboard ────────────────────────────────────────
    ("feature_referral_dashboard_enabled", "bool", True, "referral_advanced",
     "👥 Advanced Referral Dashboard: Enabled",
     "Show users a rich referral dashboard with stats, commissions, and withdrawal."),
    ("referral_commission_pct", "float", 5.0, "referral_advanced",
     "👥 Referral Commission % on Purchase",
     "Percentage of each referred user's purchase credited as commission (0 = disabled)."),
    ("referral_min_withdrawal", "float", 5.0, "referral_advanced",
     "👥 Referral Min Withdrawal Amount",
     "Minimum available commission balance required to request a withdrawal."),
    ("referral_max_withdrawal", "float", 0.0, "referral_advanced",
     "👥 Referral Max Withdrawal Amount",
     "Maximum single withdrawal amount (0 = unlimited)."),
    ("referral_bonus", "float", 0.0, "referral_advanced",
     "👥 Referral Signup Bonus",
     "Bonus credited to the referred user on first /start (0 = disabled)."),
    ("referral_first_purchase_bonus", "float", 0.0, "referral_advanced",
     "👥 Referral First-Purchase Bonus",
     "Extra bonus to referrer when their referred user makes their first purchase (0 = disabled)."),
    ("referral_lifetime_enabled", "bool", True, "referral_advanced",
     "👥 Referral Lifetime Tracking",
     "When ON, commission is tracked for all future purchases by referred users, not just the first."),
    ("referral_max_levels", "int", 1, "referral_advanced",
     "👥 Referral Max Levels",
     "How many levels deep referral commissions apply (1 = direct only, 2 = also referred's referrals, etc.)."),

    # ── V20: Announcement System ────────────────────────────────────────────────
    ("feature_announcements_enabled", "bool", True, "broadcast",
     "📢 Announcements: Enabled",
     "Enable the announcement system — admins can create, schedule, and broadcast messages."),
    ("announcement_popup_enabled", "bool", True, "broadcast",
     "📢 Announcements: Popup on Main Menu",
     "When ON, unread pinned popup-type announcements are shown as a DM when user opens main menu."),
    ("announcement_homepage_banner", "bool", False, "broadcast",
     "📢 Announcements: Homepage Banner Text",
     "When ON, the latest active banner-type announcement title is shown in the main menu header."),

    # ── V20: Enhanced Maintenance Mode ─────────────────────────────────────────
    ("feature_maintenance_advanced_enabled", "bool", True, "ops",
     "🔧 Maintenance Advanced Controls: Enabled",
     "Enable the extended maintenance panel with whitelist, return time, and enhanced message."),
    ("maintenance_estimated_return", "str", "", "ops",
     "🔧 Maintenance Estimated Return Time",
     "Human-readable estimated return time shown in the maintenance message (e.g. '~2 hours')."),
    ("maintenance_whitelist", "str", "", "ops",
     "🔧 Maintenance Whitelist (comma-separated Telegram IDs)",
     "Users whose Telegram IDs appear here can access the bot even during maintenance mode."),

    # ── V20: Enhanced Support Tickets ──────────────────────────────────────────
    ("feature_support_categories_enabled", "bool", True, "features",
     "🎫 Support Ticket Categories: Enabled",
     "When ON, users pick a category (General, Payment, Order, etc.) when opening a ticket."),
    ("feature_support_file_uploads", "bool", True, "features",
     "🎫 Support Ticket File Uploads: Enabled",
     "When ON, users and admins can attach photos to ticket messages."),
    ("feature_support_assign_enabled", "bool", True, "features",
     "🎫 Support Ticket Assignment: Enabled",
     "When ON, admins can assign tickets to specific admin accounts."),

    # ── V20: Advanced Low-Stock Monitoring ─────────────────────────────────────
    ("feature_low_stock_advanced_enabled", "bool", True, "inventory",
     "📉 Advanced Low-Stock Monitoring: Enabled",
     "Enables per-product thresholds, silent mode, and fast-selling detection."),
    ("low_stock_auto_notify", "bool", True, "inventory",
     "📉 Low-Stock Auto Notify",
     "Automatically notify admins when stock falls below threshold (same as existing notif_low_stock)."),
    ("low_stock_silent_notify", "bool", False, "inventory",
     "📉 Low-Stock Silent Mode (No User Notification)",
     "When ON, low-stock alerts are recorded but NOT sent as messages (log only)."),
    ("low_stock_fast_seller_days", "int", 7, "inventory",
     "📉 Fast-Seller Detection Window (days)",
     "Products that sell more than the threshold units in this many days trigger a fast-seller alert."),
    ("low_stock_fast_seller_threshold", "int", 10, "inventory",
     "📉 Fast-Seller Sales Threshold",
     "Units sold within the detection window to be flagged as a fast seller."),

    # ── V21: Six New Feature Flags ─────────────────────────────────────────
    ("feature_scheduled_broadcast_enabled", "bool", True, "broadcast",
     "📨 Scheduled Broadcast: Enabled",
     "Enable the scheduled broadcast system — CRUD, multi-media, targeting, recurring."),
    ("broadcast_default_segment", "str", "all", "broadcast",
     "📨 Broadcast Default Segment",
     "Default audience segment: all|buyers|non_buyers|wallet_users."),
    ("broadcast_max_daily", "int", 0, "broadcast",
     "📨 Max Broadcasts Per Day",
     "Maximum number of broadcasts per day (0 = unlimited)."),

    # ── V26: Scheduled Broadcast V2 settings ───────────────────────────────
    ("scheduled_broadcast_status", "str", "enabled", "broadcast",
     "📨 Scheduled Broadcast Status",
     "Feature status: enabled | maintenance | disabled. "
     "Maintenance pauses all sending but preserves scheduled items."),
    ("broadcast_max_speed", "int", 20, "broadcast",
     "📨 Max Broadcast Speed (msg/s)",
     "Maximum messages per second sent during a broadcast. Lower = safer, higher = faster."),
    ("broadcast_delay_ms", "int", 50, "broadcast",
     "📨 Delay Between Messages (ms)",
     "Milliseconds to wait between individual message sends. Minimum 30 ms recommended."),
    ("broadcast_retry_failed", "bool", True, "broadcast",
     "📨 Retry Failed Deliveries",
     "Queue failed recipients for automatic retry 5 minutes after the broadcast completes."),
    ("broadcast_retry_count", "int", 3, "broadcast",
     "📨 Retry Attempt Limit",
     "Maximum number of retry attempts per failed recipient before giving up."),
    ("broadcast_silent", "bool", False, "broadcast",
     "📨 Silent Broadcast Mode",
     "When ON, no notifications are sent to recipients (messages arrive silently)."),
    ("broadcast_disable_notifications", "bool", False, "broadcast",
     "📨 Disable Notifications",
     "Sends messages with disable_notification=True — recipients see no push notification."),

    # ── V27: Webhook Monitor & API Health settings ─────────────────────────
    ("webhook_monitor_status", "str", "enabled", "monitoring",
     "🔌 Webhook Monitor Status",
     "Feature status: enabled | maintenance | disabled."),
    ("webhook_monitor_auto_refresh", "bool", True, "monitoring",
     "🔌 Webhook Monitor Auto-Refresh",
     "Automatically refresh health status on a fixed interval."),
    ("webhook_monitor_refresh_interval", "int", 60, "monitoring",
     "🔌 Webhook Monitor Refresh Interval (s)",
     "Seconds between automatic health refreshes shown in the admin panel."),
    ("webhook_monitor_retry_count", "int", 3, "monitoring",
     "🔌 Webhook Retry Count",
     "Maximum number of retry attempts for a failed webhook before it is abandoned."),
    ("webhook_monitor_timeout", "int", 10, "monitoring",
     "🔌 Webhook / API Probe Timeout (s)",
     "HTTP timeout in seconds used when probing external API endpoints."),
    ("webhook_monitor_admin_alerts", "bool", True, "monitoring",
     "🔌 Webhook Monitor Admin Alerts",
     "Send Telegram alert to admin when a service goes offline or a webhook repeatedly fails."),
    ("webhook_log_retention_days", "int", 30, "monitoring",
     "🔌 Webhook Log Retention (days)",
     "Webhook and health-check records older than this are pruned automatically."),
    ("health_slow_threshold_ms", "int", 2000, "monitoring",
     "🔌 Slow Response Threshold (ms)",
     "Responses exceeding this latency are flagged 🟡 Slow."),
    ("health_warn_threshold_ms", "int", 5000, "monitoring",
     "🔌 Warning Response Threshold (ms)",
     "Responses exceeding this latency are flagged 🟠 Warning (still up)."),
    ("health_check_interval", "int", 300, "monitoring",
     "🔌 API Health Check Interval (s)",
     "How often (seconds) the background job probes all external APIs."),
    ("health_alert_cooldown_minutes", "int", 60, "monitoring",
     "🔌 API Alert Cooldown (minutes)",
     "Minimum time between repeated alerts for the same service + status. "
     "Prevents notification spam from scheduler restarts, manual refreshes, "
     "or overlapping checks while nothing has actually changed."),

    # ── V28: Product Clone & Template System ───────────────────────────────
    ("product_clone_status", "str", "enabled", "products",
     "📄 Product Clone Status",
     "Feature status: enabled | maintenance | disabled."),
    ("product_clone_images", "bool", True, "products",
     "📄 Clone Images",
     "Include image_path, download_link, and telegram_file_id when cloning."),
    ("product_clone_faq", "bool", True, "products",
     "📄 Clone FAQ",
     "Copy all active ProductFAQ rows to the cloned product."),
    ("product_clone_coupons", "bool", False, "products",
     "📄 Clone Coupons",
     "Copy product-specific coupons to the clone (codes get _copy suffix)."),
    ("product_clone_stock", "bool", False, "products",
     "📄 Clone Stock Count",
     "Copy the stock_count counter to the clone (never copies actual key inventory)."),
    ("product_clone_settings", "bool", True, "products",
     "📄 Clone Product Settings",
     "Copy type_config and delivery_format_template to the clone."),
    ("product_clone_custom_fields", "bool", True, "products",
     "📄 Clone Custom Fields",
     "Copy type_config JSON blob (which holds custom fields) to the clone."),
    ("product_template_max", "int", 50, "products",
     "📄 Max Templates",
     "Maximum number of product templates that can be saved (0 = unlimited)."),

    ("feature_advanced_analytics_enabled", "bool", True, "features",
     "📊 Advanced Analytics Dashboard: Enabled",
     "Enable the advanced analytics dashboard with revenue/order/wallet/coupon/referral reports."),
    ("analytics_default_period", "str", "30d", "features",
     "📊 Analytics Default Period",
     "Default report period: 7d|30d|90d|all."),
    ("analytics_export_enabled", "bool", True, "features",
     "📊 Analytics CSV Export: Enabled",
     "Allow admins to export analytics reports as CSV files."),

    ("feature_multilang_enabled", "bool", True, "features",
     "🌍 Multi-Language System: Enabled",
     "Enable per-user language selection and the language management dashboard."),
    ("default_language", "str", "en", "features",
     "🌍 Default Bot Language",
     "Default language code for new users: en|bn|ar|ru|vi|zh."),
    ("multilang_user_switch", "bool", True, "features",
     "🌍 Users Can Switch Language",
     "Allow users to switch the bot language from their profile."),

    ("feature_advanced_coupons_enabled", "bool", True, "promotions",
     "🏷 Advanced Coupon System: Enabled",
     "Enable advanced coupon features: percentage/fixed/free-product, per-user limits, targeting."),
    ("coupon_auto_apply", "bool", False, "promotions",
     "🏷 Auto-Apply Best Coupon",
     "Automatically apply the best available coupon at checkout."),
    ("coupon_birthday_enabled", "bool", False, "promotions",
     "🏷 Auto Birthday Coupons",
     "Automatically issue birthday discount coupons to users on their birthday."),
    ("coupon_birthday_discount", "float", 10.0, "promotions",
     "🏷 Birthday Coupon Discount %",
     "Discount percentage for auto-issued birthday coupons."),
    ("coupon_referral_enabled", "bool", False, "promotions",
     "🏷 Auto Referral Coupons",
     "Automatically issue referral coupons when a new user is referred."),

    ("feature_auto_refund_enabled", "bool", True, "features",
     "💰 Automatic Refund System: Enabled",
     "Enable the automatic refund system for failed/cancelled/timed-out/duplicate orders."),
    ("refund_auto_failed_orders", "bool", True, "features",
     "💰 Auto-Refund Failed Orders",
     "Automatically trigger refunds for orders with FAILED status."),
    ("refund_auto_cancelled_orders", "bool", False, "features",
     "💰 Auto-Refund Cancelled Orders",
     "Automatically trigger refunds for orders with CANCELLED status."),
    ("refund_auto_timed_out", "bool", True, "features",
     "💰 Auto-Refund Timed-Out Orders",
     "Automatically trigger refunds for orders that timed out without delivery."),
    ("refund_auto_duplicate", "bool", True, "features",
     "💰 Auto-Refund Duplicate Payments",
     "Automatically trigger refunds for duplicate payment detections."),
    ("refund_notify_admin", "bool", True, "notifications",
     "💰 Notify Admin on New Refund",
     "Send the bot admin a notification whenever a new refund is queued."),

    ("feature_audit_enhanced_enabled", "bool", True, "features",
     "📝 Enhanced Admin Audit Log: Enabled",
     "Enable enhanced audit log with old/new values, IP address, module filter, CSV export."),
    ("audit_log_ip", "bool", True, "features",
     "📝 Audit Log IP Addresses",
     "Record IP addresses in the admin audit log when available."),
    ("audit_log_old_new_vals", "bool", True, "features",
     "📝 Audit Log Old/New Values",
     "Record before/after values on all configuration and data changes."),
    ("audit_max_retention_days", "int", 0, "features",
     "📝 Audit Log Retention (days)",
     "Auto-delete audit entries older than this many days (0 = keep forever)."),

    # ── V22: Favorites (Bookmarks) ──────────────────────────────────────────
    ("feature_favorites_enabled", "bool", True, "features",
     "❤️ Favorites: Enabled",
     "Master toggle for the Favorites (bookmark) feature. "
     "Use 'favorites_status' for finer-grained control."),
    ("favorites_status", "str", "enabled", "catalog",
     "❤️ Favorites: Status",
     "Controls feature state: 'enabled', 'maintenance', or 'disabled'."),
    ("favorites_max", "int", 50, "catalog",
     "❤️ Favorites: Max per User",
     "Maximum favorites per user (10/20/50/100 or 0 = unlimited)."),
    ("favorites_counter", "bool", True, "catalog",
     "❤️ Favorites: Show Counter",
     "Show the total favorites count on the Add to Favorites button."),
    ("favorites_allow_clear_all", "bool", True, "catalog",
     "❤️ Favorites: Allow Clear All",
     "Show the 'Clear All Favorites' button to users on the favorites page."),

    # ── V22: Product Compare ────────────────────────────────────────────────
    ("feature_product_compare_enabled", "bool", True, "features",
     "⚖️ Product Compare: Enabled",
     "Master toggle for the Product Compare feature. "
     "Use 'product_compare_status' for finer-grained control."),
    ("product_compare_status", "str", "enabled", "catalog",
     "⚖️ Product Compare: Status",
     "Controls feature state: 'enabled', 'maintenance', or 'disabled'."),
    ("product_compare_max", "int", 4, "catalog",
     "⚖️ Product Compare: Max Products",
     "Maximum number of products a user can add to their compare list (2, 3, or 4)."),
    ("product_compare_counter", "bool", True, "catalog",
     "⚖️ Product Compare: Show Counter",
     "Show the [N/Max] compare count on the Add/Remove buttons on product pages."),
    ("product_compare_best_value", "bool", True, "catalog",
     "⚖️ Product Compare: Highlight Best Value",
     "Mark the best value for each metric (lowest price, highest stock, etc.) "
     "with a 🏆 trophy in the comparison table."),
    ("product_compare_show_unavailable", "bool", True, "catalog",
     "⚖️ Product Compare: Show Unavailable Products",
     "Allow users to add out-of-stock or inactive products to their compare list."),

    # ── V22: Subscription Expiry Reminders ──────────────────────────────────
    ("feature_subscription_reminder_enabled", "bool", True, "features",
     "🔔 Subscription Expiry Reminder: Enabled",
     "Master toggle for the subscription expiry reminder system. "
     "Use 'sub_expiry_reminder_status' for finer-grained control (enabled/maintenance/disabled)."),
    ("sub_expiry_reminder_status", "str", "enabled", "subscriptions",
     "🔔 Subscription Reminder: Status",
     "Controls whether expiry reminders are sent: 'enabled', 'maintenance', or 'disabled'."),
    ("sub_expiry_reminder_days", "str", "30,15,7,3,1", "subscriptions",
     "🔔 Subscription Reminder: Days",
     "Comma-separated list of days-before-expiry intervals to send reminders. "
     "e.g. '30,15,7,3,1'. Also sends an expired notice (0) on the day of expiry."),
    ("sub_expiry_reminder_template", "str", "1", "subscriptions",
     "🔔 Subscription Reminder: Template",
     "Which notification template to use: 1=Standard, 2=Detailed, 3=Friendly."),
    ("sub_expiry_reminder_send_time", "str", "any", "subscriptions",
     "🔔 Subscription Reminder: Send Time (UTC hour)",
     "UTC hour (0-23) during which reminders are sent, e.g. '8' for 08:00. "
     "'any' = send any time the job runs."),
    ("sub_expiry_reminder_retry_failed", "bool", True, "subscriptions",
     "🔔 Subscription Reminder: Retry Failed",
     "If ON, previously-failed reminder sends are retried on the next job cycle."),
    ("sub_expiry_reminder_check_interval_minutes", "int", 60, "subscriptions",
     "🔔 Subscription Reminder: Check Interval (minutes)",
     "How often the expiry reminder job runs (in minutes). Takes effect on restart."),

    # ── V29: Withdrawal Approval System ────────────────────────────────────
    ("withdrawal_approval_status", "str", "enabled", "wallets",
     "💸 Withdrawal Approval: Status",
     "Controls withdrawal approval feature: 'enabled', 'maintenance', or 'disabled'."),
    ("withdrawal_approval_auto_approval", "bool", False, "wallets",
     "💸 Withdrawal Approval: Auto Approval",
     "If ON, withdrawals at or below the auto-approval max are approved automatically."),
    ("withdrawal_approval_auto_max", "float", 10.0, "wallets",
     "💸 Withdrawal Approval: Auto Approval Max Amount",
     "Withdrawals at or below this amount are auto-approved when auto-approval is ON."),
    ("withdrawal_approval_min_amount", "float", 5.0, "wallets",
     "💸 Withdrawal Approval: Minimum Amount",
     "Minimum withdrawal amount allowed."),
    ("withdrawal_approval_max_amount", "float", 0.0, "wallets",
     "💸 Withdrawal Approval: Maximum Amount",
     "Maximum withdrawal amount (0 = unlimited)."),
    ("withdrawal_approval_max_daily", "int", 0, "wallets",
     "💸 Withdrawal Approval: Max Daily Withdrawals Per User",
     "Maximum withdrawal requests per user per day (0 = unlimited)."),
    ("withdrawal_approval_processing_time", "str", "1-3 business days", "wallets",
     "💸 Withdrawal Approval: Processing Time",
     "Estimated processing time shown to users, e.g. '1-3 business days'."),
    ("withdrawal_approval_retry_failed", "bool", True, "wallets",
     "💸 Withdrawal Approval: Retry Failed",
     "If ON, failed withdrawal processing can be retried by admins."),

    # ── V30: Admin Dashboard Widget System ──────────────────────────────────
    ("adw_status", "str", "enabled", "admin",
     "📊 Dashboard Widgets: Status",
     "Controls dashboard widget feature: 'enabled', 'maintenance', or 'disabled'."),
    ("adw_auto_refresh", "bool", False, "admin",
     "📊 Dashboard Widgets: Auto Refresh",
     "If ON, admins can activate per-session auto-refresh of the dashboard message."),
    ("adw_refresh_interval", "int", 60, "admin",
     "📊 Dashboard Widgets: Refresh Interval (seconds)",
     "How often the dashboard auto-refreshes when enabled (30, 60, 300, or 600)."),
    ("adw_charts_enabled", "bool", True, "admin",
     "📊 Dashboard Widgets: Charts / Sparklines",
     "If ON, 7-day revenue sparklines are shown on revenue widgets."),
    ("adw_quick_actions", "bool", True, "admin",
     "📊 Dashboard Widgets: Quick Actions",
     "If ON, the Quick Actions panel is available from the dashboard."),
    ("adw_statistics", "bool", True, "admin",
     "📊 Dashboard Widgets: Statistics View",
     "If ON, the detailed Statistics view is available from the dashboard."),

    # ── V31: Smart Fraud Detection System ───────────────────────────────────
    ("fds_status",                   "str",   "enabled", "security",
     "🔍 Fraud Detection: Status",
     "Controls the fraud detection feature: 'enabled', 'maintenance', or 'disabled'."),
    ("fds_check_dup_txid",           "bool",  True,      "security",
     "🔍 Fraud Detection: Duplicate TXID Check",
     "If ON, detects when the same transaction ID is used by multiple accounts."),
    ("fds_check_dup_wallet",         "bool",  True,      "security",
     "🔍 Fraud Detection: Duplicate Wallet Check",
     "If ON, flags wallet addresses shared across multiple user accounts."),
    ("fds_check_dup_deposit",        "bool",  True,      "security",
     "🔍 Fraud Detection: Duplicate Deposit Check",
     "If ON, detects the same amount deposited twice within 10 minutes."),
    ("fds_check_dup_withdrawal",     "bool",  True,      "security",
     "🔍 Fraud Detection: Duplicate Withdrawal Check",
     "If ON, detects the same withdrawal amount submitted twice within 1 hour."),
    ("fds_check_referral_abuse",     "bool",  True,      "security",
     "🔍 Fraud Detection: Referral Abuse Check",
     "If ON, detects circular referral chains and referral farming."),
    ("fds_check_coupon_abuse",       "bool",  True,      "security",
     "🔍 Fraud Detection: Coupon Abuse Check",
     "If ON, flags users who redeem 5+ coupons within 24 hours."),
    ("fds_max_failed_payments",      "int",   5,         "security",
     "🔍 Fraud Detection: Max Failed Payments (24h)",
     "Number of failed payments in 24h that triggers a fraud flag."),
    ("fds_max_daily_withdrawals",    "int",   3,         "security",
     "🔍 Fraud Detection: Max Daily Withdrawals",
     "Maximum withdrawal requests per user per day before flagging."),
    ("fds_max_daily_deposits",       "int",   10,        "security",
     "🔍 Fraud Detection: Max Daily Deposits",
     "Maximum deposit attempts per user per day before flagging."),
    ("fds_max_daily_orders",         "int",   20,        "security",
     "🔍 Fraud Detection: Max Daily Orders",
     "Maximum orders per user per day before flagging unusual activity."),
    ("fds_risk_threshold_medium",    "int",   30,        "security",
     "🔍 Fraud Detection: Medium Risk Threshold",
     "Risk score at or above which a user is classified as Medium risk."),
    ("fds_risk_threshold_high",      "int",   60,        "security",
     "🔍 Fraud Detection: High Risk Threshold",
     "Risk score at or above which a user is classified as High risk."),
    ("fds_risk_threshold_critical",  "int",   90,        "security",
     "🔍 Fraud Detection: Critical Risk Threshold",
     "Risk score at or above which a user is classified as Critical risk."),
    ("fds_auto_freeze",              "bool",  True,      "security",
     "🔍 Fraud Detection: Auto Freeze Wallet",
     "If ON, Critical-risk users have their wallet automatically frozen."),
    ("fds_auto_suspend",             "bool",  False,     "security",
     "🔍 Fraud Detection: Auto Suspend Account",
     "If ON, Critical-risk users have their account automatically suspended."),
    ("fds_admin_alerts",             "bool",  True,      "security",
     "🔍 Fraud Detection: Admin Alerts",
     "If ON, admins receive notifications when High or Critical risk is detected."),

    # ── V32: Login Activity & Device Management ───────────────────────────────
    ("lam_status",              "str",   "enabled", "security",
     "🔐 Login Activity: Status",
     "Master switch: 'enabled' = fully active, 'maintenance' = tracking only "
     "(no user-facing UI), 'disabled' = completely off."),
    ("lam_track_history",       "bool",  True,      "security",
     "🔐 Login Activity: Track Login History",
     "If ON, every new session start is recorded in login_records."),
    ("lam_track_devices",       "bool",  True,      "security",
     "🔐 Login Activity: Track Devices",
     "If ON, each unique device fingerprint (based on Telegram language/locale) "
     "is stored in user_devices and presented in Security Center."),
    ("lam_track_ip",            "bool",  True,      "security",
     "🔐 Login Activity: Track IP Address",
     "If ON, the IP field is stored when available. Standard Telegram Bot API "
     "does not expose user IPs; this takes effect if a webhook proxy forwards the "
     "X-Forwarded-For header."),
    ("lam_track_location",      "bool",  True,      "security",
     "🔐 Login Activity: Track Location",
     "If ON, country/city fields are stored when a geo-IP resolver is configured."),
    ("lam_max_history",         "int",   50,        "security",
     "🔐 Login Activity: Max Login History per User",
     "Maximum number of login records kept per user. "
     "Older rows are pruned on write. 0 = unlimited."),
    ("lam_session_expiry_days", "int",   30,        "security",
     "🔐 Login Activity: Session Expiration (days)",
     "Sessions not active for this many days are automatically closed by the "
     "background cleanup job. 0 = sessions never expire automatically."),
    ("lam_max_sessions",        "int",   0,         "security",
     "🔐 Login Activity: Max Active Sessions per User",
     "Maximum number of simultaneous active sessions per user. "
     "0 = unlimited. When the limit is exceeded, the oldest session is closed."),
    ("lam_notify_new_login",    "bool",  True,      "security",
     "🔐 Login Activity: Notify on New Login",
     "If ON, users receive a Telegram message every time a new session starts."),
    ("lam_notify_new_device",   "bool",  True,      "security",
     "🔐 Login Activity: Notify on New Device",
     "If ON, users receive a Telegram message when a login is detected from a "
     "previously-unseen device fingerprint."),

    # ── V33: Customer Notes & CRM System ─────────────────────────────────────
    ("crm_status",                "str",   "enabled", "crm",
     "📝 Customer CRM: Status",
     "Master switch: 'enabled' = fully active, 'maintenance' = admin-only, "
     "'disabled' = completely off."),
    ("crm_allow_multiple_notes",  "bool",  True,      "crm",
     "📝 Customer CRM: Allow Multiple Notes per User",
     "If OFF, each user may have at most one active (non-archived) note at a time."),
    ("crm_allow_tags",            "bool",  True,      "crm",
     "📝 Customer CRM: Allow Customer Tags",
     "If ON, admins can assign and create tags on user profiles."),
    ("crm_allow_priority",        "bool",  True,      "crm",
     "📝 Customer CRM: Allow Priority Levels",
     "If ON, admins can set Low / Medium / High / Critical priority on users."),
    ("crm_allow_reminders",       "bool",  True,      "crm",
     "📝 Customer CRM: Allow Follow-up Reminders",
     "If ON, admins can create timed follow-up reminders for users."),
    ("crm_allow_internal_status", "bool",  True,      "crm",
     "📝 Customer CRM: Allow Internal Status",
     "If ON, admins can assign internal CRM statuses such as VIP, Wholesale, etc."),
    ("crm_max_notes",             "int",   0,         "crm",
     "📝 Customer CRM: Max Notes per User",
     "Maximum number of active notes per user. 0 = unlimited."),
    # ── V39: Multi-Currency Wallet ────────────────────────────────────────────
    ("multicurrency_wallet_status",       "str",   "enabled",  "wallets",
     "🌍 Multi-Currency Wallet Status",
     "enabled = operational; maintenance = read-only; disabled = hidden."),
    ("mcw_default_deposit_fee_pct",       "float", 0.0,        "wallets",
     "🌍 MCW: Default Deposit Fee (%)",
     "Default deposit fee percentage applied to new currencies."),
    ("mcw_default_withdrawal_fee_pct",    "float", 0.0,        "wallets",
     "🌍 MCW: Default Withdrawal Fee (%)",
     "Default withdrawal fee percentage applied to new currencies."),
    ("mcw_portfolio_display_currency",    "str",   "USD",      "wallets",
     "🌍 MCW: Portfolio Display Currency",
     "Currency used to show total portfolio value (e.g. USD)."),
    ("mcw_transfer_enabled",              "bool",  True,       "wallets",
     "🌍 MCW: Enable Wallet-to-Wallet Transfer",
     "Allow users to transfer between their own currency wallets."),
    ("mcw_show_zero_balances",            "bool",  True,       "wallets",
     "🌍 MCW: Show Zero-Balance Wallets",
     "Show all enabled currencies in the wallet even if balance is 0."),
    # ── V39: Exchange Rate Manager ────────────────────────────────────────────
    ("exchange_rate_manager_status",      "str",   "enabled",  "exchange_rates",
     "🔄 Exchange Rate Manager Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    ("erm_auto_update_enabled",           "bool",  True,       "exchange_rates",
     "🔄 ERM: Auto-Update Rates",
     "When ON, the bot automatically refreshes exchange rates on their configured interval."),
    ("erm_scheduler_interval_seconds",    "int",   60,         "exchange_rates",
     "🔄 ERM: Scheduler Tick (seconds)",
     "How often the auto-update job runs to check for pairs due for a refresh."),
    ("erm_default_auto_interval_minutes", "int",   60,         "exchange_rates",
     "🔄 ERM: Default Auto-Update Interval (minutes)",
     "Default auto-update frequency for newly-added pairs."),
    ("erm_default_margin_pct",            "float", 0.0,        "exchange_rates",
     "🔄 ERM: Default Margin (%)",
     "Default buy/sell spread applied to newly-added pairs."),
    ("erm_reset_daily_counters",          "bool",  True,       "exchange_rates",
     "🔄 ERM: Reset Daily Counters at Midnight",
     "Reset updates_today / failed_updates_today counters each day."),
    ("erm_history_retention_days",        "int",   30,         "exchange_rates",
     "🔄 ERM: History Retention (days)",
     "How many days of rate history to keep per pair. 0 = keep forever."),
    # ── V40: Business Analytics & Sales Forecast ──────────────────────────────
    ("biz_analytics_status",             "str",   "enabled",  "admin",
     "📊 Business Analytics Status",
     "enabled = operational; maintenance = read-only; disabled = hidden."),
    ("biz_forecast_period_days",         "int",   30,         "admin",
     "📊 Forecast Period (days)",
     "Number of past days used as baseline for sales forecasting (SMA window)."),
    ("biz_report_retention_days",        "int",   90,         "admin",
     "📊 Report Retention (days)",
     "How many days to keep generated reports. 0 = keep forever."),
    ("biz_auto_daily_report",            "bool",  False,      "admin",
     "📊 Auto Daily Report",
     "Automatically generate a daily business report at midnight."),
    ("biz_auto_weekly_report",           "bool",  False,      "admin",
     "📊 Auto Weekly Report",
     "Automatically generate a weekly business report on Mondays."),
    ("biz_auto_monthly_report",          "bool",  False,      "admin",
     "📊 Auto Monthly Report",
     "Automatically generate a monthly business report on the 1st of each month."),
    # ── V40: Anti-Spam & Auto-Moderation ─────────────────────────────────────
    ("antispam_status",                  "str",   "enabled",  "antispam",
     "🛡 Anti-Spam Status",
     "enabled = active; maintenance = log-only (no auto-actions); disabled = off."),
    ("antispam_max_cmds_per_min",        "int",   10,         "antispam",
     "🛡 Max Commands per Minute",
     "How many /commands a user may send per minute before rate-limit trigger."),
    ("antispam_max_clicks_per_min",      "int",   20,         "antispam",
     "🛡 Max Button Clicks per Minute",
     "How many inline button presses a user may make per minute."),
    ("antispam_max_msgs_per_min",        "int",   15,         "antispam",
     "🛡 Max Messages per Minute",
     "How many text messages a user may send per minute."),
    ("antispam_flood_window_secs",       "int",   10,         "antispam",
     "🛡 Flood Window (seconds)",
     "Short window in seconds for flood detection."),
    ("antispam_flood_threshold",         "int",   8,          "antispam",
     "🛡 Flood Threshold (events)",
     "How many events in the flood window constitute a flood."),
    ("antispam_cooldown_secs",           "int",   60,         "antispam",
     "🛡 Cooldown Duration (seconds)",
     "How long a user is put on cooldown after reaching max warnings."),
    ("antispam_max_warnings",            "int",   3,          "antispam",
     "🛡 Max Warnings Before Action",
     "Number of violations before auto-mute or cooldown is applied."),
    ("antispam_auto_mute",               "bool",  True,       "antispam",
     "🛡 Auto-Mute on Max Warnings",
     "Automatically mute users who exceed the warning threshold."),
    ("antispam_auto_ban",                "bool",  False,      "antispam",
     "🛡 Auto-Ban on Repeated Mutes",
     "Automatically temp-ban users who are muted multiple times."),
    ("antispam_mute_secs",               "int",   300,        "antispam",
     "🛡 Mute Duration (seconds)",
     "Duration of an automatic mute. Default: 300 (5 minutes)."),
    ("antispam_ban_secs",                "int",   86400,      "antispam",
     "🛡 Temp-Ban Duration (seconds)",
     "Duration of an automatic temporary ban. Default: 86400 (24h)."),
    ("antispam_captcha_on_new",          "bool",  False,      "antispam",
     "🛡 Captcha for New Users",
     "Require captcha verification for brand-new users on first interaction."),

    # ── V41: VIP Tier Manager ───────────────────────────────────────────────
    ("vip_status",               "str",   "enabled", "vip",
     "🏆 VIP System Status",
     "enabled | maintenance | disabled"),
    ("vip_auto_upgrade",         "bool",  True,       "vip",
     "🏆 Auto Upgrade",
     "Automatically upgrade users when they meet tier requirements."),
    ("vip_auto_downgrade",       "bool",  False,      "vip",
     "🏆 Auto Downgrade",
     "Automatically downgrade users when they no longer meet tier requirements."),
    ("vip_points_expiration_days","int",  0,          "vip",
     "🏆 Points Expiration (days)",
     "0 = never expire. >0 = expire points older than N days."),
    ("vip_cashback_enabled",     "bool",  True,       "vip",
     "🏆 Cashback Enabled",
     "Apply cashback from VIP tier on completed orders."),
    ("vip_referral_bonus_enabled","bool", True,       "vip",
     "🏆 Referral Bonus Enabled",
     "Apply extra referral bonus from VIP tier."),
    ("vip_reward_limit_per_day", "int",   0,          "vip",
     "🏆 Reward Claim Limit/Day",
     "0 = unlimited. >0 = max reward claims per user per day."),

    # ── V41: API Key & Integration Manager ─────────────────────────────────
    ("aim_status",               "str",   "enabled", "api_manager",
     "🔑 API Manager Status",
     "enabled | maintenance | disabled"),
    ("aim_auto_health_check",    "bool",  True,       "api_manager",
     "🔑 Auto Health Check",
     "Automatically check API health at the configured interval."),
    ("aim_auto_retry",           "bool",  True,       "api_manager",
     "🔑 Auto Retry on Failure",
     "Automatically retry failed connections."),
    ("aim_retry_count",          "int",   3,          "api_manager",
     "🔑 Retry Count",
     "Number of retries before marking an integration offline."),
    ("aim_timeout_seconds",      "int",   10,         "api_manager",
     "🔑 Request Timeout (s)",
     "Timeout in seconds for health check HTTP requests."),
    ("aim_health_check_interval_minutes","int",15,    "api_manager",
     "🔑 Health Check Interval (min)",
     "How often to run the background health check job."),
    ("aim_log_retention_days",   "int",   30,         "api_manager",
     "🔑 Log Retention (days)",
     "How many days to keep connection log rows."),

    # ── V20: Main Menu Manager ────────────────────────────────────────────────
    ("main_menu_status", "str", "enabled", "main_menu",
     "📋 Main Menu Status",
     "Controls the user-facing main menu: enabled | maintenance | disabled."),
    ("main_menu_colors_enabled", "bool", True, "main_menu",
     "🎨 Main Menu Button Colors",
     "Master switch for colored buttons (Bot API 9.4). OFF shows every "
     "main menu button in the app's default color, regardless of each "
     "item's individual color setting. Doesn't affect emoji icons."),
    ("global_button_colors_enabled", "bool", True, "main_menu",
     "🌈 All Bot Buttons Colored",
     "Master switch for colored buttons (Bot API 9.4) EVERYWHERE in the "
     "bot -- products, cart, orders, admin panels, etc. -- not just the "
     "main menu. OFF shows plain, uncolored buttons in every keyboard "
     "this switch covers."),
    ("menu_item_products_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Products",
     "Show the 🛒 Products button on the main menu."),
    ("menu_item_topup_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Top Up",
     "Show the 💰 Top Up button on the main menu."),
    ("menu_item_orders_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Orders",
     "Show the 📜 Orders button on the main menu."),
    ("menu_item_support_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Support",
     "Show the 💬 Support button on the main menu."),
    ("menu_item_refer_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Refer & Earn",
     "Show the 🎁 Refer & Earn button on the main menu."),
    ("menu_item_account_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Account",
     "Show the 👤 Account button on the main menu."),
    ("menu_item_language_enabled", "bool", True, "main_menu",
     "📋 Menu Item: Language",
     "Show the 🌐 Language button on the main menu."),
    # Per-item color overrides -- must be registered here or cfg.set()
    # silently refuses to write them (see _BotConfigCache.set: "unknown
    # key"), which is why Reset to Default / the color-cycle button used
    # to appear to do nothing.
    ("menu_item_products_style", "str", "success", "main_menu",
     "🎨 Menu Item: Products Color",
     "Button color for 🛒 Products (none/success/primary/danger)."),
    ("menu_item_topup_style", "str", "success", "main_menu",
     "🎨 Menu Item: Top Up Color",
     "Button color for 💰 Top Up (none/success/primary/danger)."),
    ("menu_item_orders_style", "str", "primary", "main_menu",
     "🎨 Menu Item: Orders Color",
     "Button color for 📜 Orders (none/success/primary/danger)."),
    ("menu_item_support_style", "str", "primary", "main_menu",
     "🎨 Menu Item: Support Color",
     "Button color for 💬 Support (none/success/primary/danger)."),
    ("menu_item_refer_style", "str", "success", "main_menu",
     "🎨 Menu Item: Refer & Earn Color",
     "Button color for 🎁 Refer & Earn (none/success/primary/danger)."),
    ("menu_item_account_style", "str", "primary", "main_menu",
     "🎨 Menu Item: Account Color",
     "Button color for 👤 Account (none/success/primary/danger)."),
    ("menu_item_language_style", "str", "primary", "main_menu",
     "🎨 Menu Item: Language Color",
     "Button color for 🌐 Language (none/success/primary/danger)."),
    ("menu_item_admin_style", "str", "danger", "main_menu",
     "🎨 Menu Item: Admin Panel Color",
     "Button color for 🛠 Admin Panel (none/success/primary/danger)."),
    ("main_menu_maintenance_msg", "text",
     "🔧 The main menu is temporarily under maintenance. Please check back shortly.",
     "main_menu",
     "📋 Maintenance Message",
     "Message shown to users when main menu status is set to maintenance."),
    ("main_menu_disabled_msg", "text",
     "🔴 The store is currently closed. Please check back later.",
     "main_menu",
     "📋 Disabled Message",
     "Message shown to users when main menu status is set to disabled."),
    ("main_menu_custom_buttons", "text", "[]", "main_menu",
     "📋 Custom Buttons (JSON)",
     "JSON array of custom buttons to append below the standard menu rows. "
     'Format: [{"label": "🛍 Shop", "callback": "products", "style": "success"}, '
     '{"label": "📢 Channel", "url": "https://t.me/channel"}]. '
     '"style" (success/primary/danger) and "emoji_id" are optional per entry.'),

    # ── V20.1: Main Menu Premium Emoji Icons ──────────────────────────────────
    # Paste a Telegram custom_emoji_id here to show it as an icon on that
    # button (Bot API 9.4). Get the ID by forwarding the emoji to
    # @RawDataBot. Requires the bot owner to have Telegram Premium.
    # Leave empty to show no icon. Colors are set from Admin → Menu Manager
    # → 🎨 Color, not here.
    ("menu_item_products_emoji_id", "str", "", "main_menu",
     "✨ Products: Premium Emoji ID",
     "Custom emoji ID shown on the Products button. Leave empty for none."),
    ("menu_item_topup_emoji_id", "str", "", "main_menu",
     "✨ Top Up: Premium Emoji ID",
     "Custom emoji ID shown on the Top Up button. Leave empty for none."),
    ("menu_item_orders_emoji_id", "str", "", "main_menu",
     "✨ Orders: Premium Emoji ID",
     "Custom emoji ID shown on the Orders button. Leave empty for none."),
    ("menu_item_support_emoji_id", "str", "", "main_menu",
     "✨ Support: Premium Emoji ID",
     "Custom emoji ID shown on the Support button. Leave empty for none."),
    ("menu_item_refer_emoji_id", "str", "", "main_menu",
     "✨ Refer & Earn: Premium Emoji ID",
     "Custom emoji ID shown on the Refer & Earn button. Leave empty for none."),
    ("menu_item_account_emoji_id", "str", "", "main_menu",
     "✨ Account: Premium Emoji ID",
     "Custom emoji ID shown on the Account button. Leave empty for none."),
    ("menu_item_language_emoji_id", "str", "", "main_menu",
     "✨ Language: Premium Emoji ID",
     "Custom emoji ID shown on the Language button. Leave empty for none."),
    ("menu_item_admin_emoji_id", "str", "", "main_menu",
     "✨ Admin Panel: Premium Emoji ID",
     "Custom emoji ID shown on the Admin Panel button. Leave empty for none."),

    # ── V21: Activity Feed System ─────────────────────────────────────────────
    # Master control
    ("af_status",                 "str",  "enabled", "activity_feed",
     "📡 Activity Feed Status",
     "Master switch: enabled | maintenance | disabled."),
    # Private feed
    ("af_private_enabled",        "bool", False,     "activity_feed",
     "🔒 Private Feed Enabled",
     "Post detailed admin logs to a private channel or group."),
    ("af_private_channel_id",     "str",  "",        "activity_feed",
     "🔒 Private Feed Channel ID",
     "Telegram channel/group ID for the private admin feed (e.g. -1001234567890)."),
    ("af_private_extra_channels", "str",  "",        "activity_feed",
     "🔒 Private Feed Extra Channels",
     "Comma-separated additional channel IDs to mirror the private feed."),
    # Public feed
    ("af_public_enabled",         "bool", False,     "activity_feed",
     "🌍 Public Feed Enabled",
     "Post privacy-safe purchase announcements to a public channel."),
    ("af_public_channel_id",      "str",  "",        "activity_feed",
     "🌍 Public Feed Channel ID",
     "Telegram channel ID for the public purchase feed."),
    ("af_public_extra_channels",  "str",  "",        "activity_feed",
     "🌍 Public Feed Extra Channels",
     "Comma-separated additional channel IDs to mirror the public feed."),
    # Per-event toggles (default True = post by default when feed is active)
    ("af_event_new_order",          "bool", True,  "activity_feed",
     "📡 Event: New Order",          "Post when a new order is placed."),
    ("af_event_wallet_topup",       "bool", True,  "activity_feed",
     "📡 Event: Wallet Top-Up",      "Post when a wallet top-up is approved."),
    ("af_event_refund",             "bool", True,  "activity_feed",
     "📡 Event: Refund",             "Post when a refund is issued."),
    ("af_event_delivery_completed", "bool", True,  "activity_feed",
     "📡 Event: Delivery Completed", "Post when a delivery is confirmed."),
    ("af_event_order_cancelled",    "bool", True,  "activity_feed",
     "📡 Event: Order Cancelled",    "Post when an order is cancelled."),
    ("af_event_coupon_used",        "bool", True,  "activity_feed",
     "📡 Event: Coupon Used",        "Post when a coupon is redeemed."),
    ("af_event_referral_reward",    "bool", True,  "activity_feed",
     "📡 Event: Referral Reward",    "Post when a referral reward is awarded."),
    ("af_event_review_submitted",   "bool", True,  "activity_feed",
     "📡 Event: Review Submitted",   "Post when a product review is posted."),
    ("af_event_product_restocked",  "bool", True,  "activity_feed",
     "📡 Event: Product Restocked",  "Post when stock is added to a product."),
    ("af_event_product_out_of_stock","bool",True,  "activity_feed",
     "📡 Event: Out of Stock",       "Post when a product runs out of stock."),
    ("af_event_invoice_generated",  "bool", True,  "activity_feed",
     "📡 Event: Invoice Generated",  "Post when an invoice is generated."),
    ("af_event_user_registered",    "bool", True,  "activity_feed",
     "📡 Event: User Registered",    "Post when a new user starts the bot."),
    ("af_event_login_alert",        "bool", False, "activity_feed",
     "📡 Event: Login Alert",        "Post on each user session start (high volume — off by default)."),
    ("af_event_failed_payment",     "bool", True,  "activity_feed",
     "📡 Event: Failed Payment",     "Post when a payment is rejected."),
    ("af_event_fraud_detected",     "bool", True,  "activity_feed",
     "📡 Event: Fraud Detected",     "Post when a fraud flag is triggered."),
    ("af_event_support_ticket",     "bool", True,  "activity_feed",
     "📡 Event: Support Ticket",     "Post when a new support ticket is opened."),
    ("af_event_admin_action",       "bool", False, "activity_feed",
     "📡 Event: Admin Action",       "Post admin panel actions (off by default — high volume)."),
    # Display options
    ("af_anonymous_names",          "bool", False, "activity_feed",
     "👤 Anonymous Customer Names",
     "Replace customer names with 'Someone' in public feed posts."),
    ("af_hide_prices",              "bool", False, "activity_feed",
     "💰 Hide Prices",
     "Omit prices from public feed posts."),
    ("af_hide_quantity",            "bool", False, "activity_feed",
     "📦 Hide Quantity",
     "Omit quantity from public feed posts."),
    ("af_hide_product_name",        "bool", False, "activity_feed",
     "📦 Hide Product Name",
     "Replace product name with 'a product' in public feed posts."),
    ("af_hide_payment_method",      "bool", False, "activity_feed",
     "💳 Hide Payment Method",
     "Omit payment method and gateway from private feed posts."),
    ("af_hide_time",                "bool", False, "activity_feed",
     "🕒 Hide Timestamps",
     "Omit timestamp block from both feed post types."),
    ("af_enable_emojis",            "bool", True,  "activity_feed",
     "😀 Enable Emojis",
     "Include emojis in feed messages (cosmetic, always-on for now)."),
    ("af_pin_important",            "bool", False, "activity_feed",
     "📌 Pin Important Messages",
     "Pin new-order and fraud-alert messages in the channel (requires pin permission)."),
    ("af_auto_delete_seconds",      "int",  0,     "activity_feed",
     "⏳ Auto-Delete (seconds)",
     "Automatically delete feed messages after N seconds. 0 = never delete."),
]


# ---------------------------------------------------------------------------
# Category registry — every entry in DEFAULTS must have its category listed
# here. Groups match the 8 admin-panel sections for consistent navigation.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Category registry — 28 categories (10 small ones merged).
# Groups align with the 8 admin-panel sections.
# ---------------------------------------------------------------------------
CATEGORIES = [
    # ── 💳 Payments & Billing  (53 settings) ─────────────────────────────
    ("payments",          "💳 Payments"),
    ("gateways",          "🏦 Payment Gateways"),
    ("wallets",           "💰 Wallets"),
    ("exchange_rates",    "🔄 Exchange Rates"),
    ("invoicing",         "🧾 Invoicing"),
    # ── 📋 Orders  ────────────────────────────────────────────────────────
    ("orders",            "📋 Order History & Details"),
    # ── 📦 Products & Inventory  (38 settings) ───────────────────────────
    ("products",          "📦 Products"),
    ("inventory",         "📊 Inventory & Delivery"),
    ("catalog",           "🏷 Catalog & Display"),
    # ── 🔧 Operations  (40 settings) ─────────────────────────────────────
    ("ops",               "🔧 Operations & Pages"),
    ("home_dashboard",    "🏠 Home Dashboard"),
    ("monitoring",        "🔌 Webhook Monitor"),
    ("notifications",     "🔔 Notifications"),
    # ── 👥 Customers & Loyalty  (38 settings) ────────────────────────────
    ("crm",               "📝 CRM & Segments"),
    ("vip",               "🏆 VIP System"),
    ("referral_advanced", "👥 Advanced Referrals"),
    ("subscriptions",     "🔔 Subscriptions"),
    # ── 📢 Marketing  (42 settings) ──────────────────────────────────────
    ("broadcast",         "📢 Broadcast & Announcements"),
    ("marketing",         "🛒 Marketing & Sales"),
    ("promotions",        "🎁 Promotions"),
    # ── 🛡 Security  (47 settings) ───────────────────────────────────────
    ("security",          "🛡 Security & Fraud"),
    ("antispam",          "🚫 Anti-Spam"),
    ("api_manager",       "🔑 API & Integrations"),
    # ── ⚙️ Features  (57 settings) ───────────────────────────────────────
    ("features",          "⚙️ Feature Management"),
    ("account_features",  "📱 Account Features"),
    # ── 🛠 System  (39 settings) ─────────────────────────────────────────
    ("system",            "🛠 System & Store UI"),
    ("backups",           "💾 Backups"),
    ("diagnostics",       "🩺 Diagnostics"),
    ("admin",             "📊 Analytics & Dashboard"),
    ("admin_ui",          "🔧 Admin Panel UI"),
    ("main_menu",         "📋 Main Menu Manager"),
    ("activity_feed",     "📡 Activity Feed"),
]


# ---------------------------------------------------------------------------
# Cached, typed accessor
# ---------------------------------------------------------------------------


class _BotConfigCache:
    _CACHE_TTL = 30.0  # seconds

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}
        self._loaded_at: float = 0.0

    # ---- internal ---------------------------------------------------------

    def _load(self) -> None:
        try:
            with get_db_session() as s:
                rows = s.query(BotConfig).all()
                self._cache = {r.key: r.value for r in rows}
                self._loaded_at = time.time()
        except Exception:
            logger.exception("Failed to load bot_config cache")

    def _ensure_fresh(self) -> None:
        if not self._cache or (time.time() - self._loaded_at) > self._CACHE_TTL:
            self._load()

    # ---- public typed getters --------------------------------------------

    def get(self, key: str, default: str = "") -> str:
        """Alias for get_str() — dict-like access for backwards-compat."""
        return self.get_str(key, default)

    def get_str(self, key: str, default: str = "") -> str:
        self._ensure_fresh()
        v = self._cache.get(key)
        return default if v is None else str(v)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_str(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_str(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_str(key, "true" if default else "false").strip().lower()
        return v in ("1", "true", "yes", "on", "y", "t")

    # ---- setter ----------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        str_val = "true" if value is True else "false" if value is False else str(value)
        with get_db_session() as s:
            row = s.query(BotConfig).filter_by(key=key).first()
            if row is None:
                # unknown key — refuse silently (only defaults are editable)
                logger.warning("Attempt to set unknown config key: %s", key)
                return
            row.value = str_val
            s.commit()
        # invalidate cache
        self._cache = {}
        self._loaded_at = 0.0

    def reset(self, key: str) -> None:
        for k, _t, default, *_ in DEFAULTS:
            if k == key:
                self.set(key, default)
                return


cfg = _BotConfigCache()


# ---------------------------------------------------------------------------
# Seeding — call once at startup
# ---------------------------------------------------------------------------


def seed_defaults() -> None:
    """Insert any missing default rows. Existing values are left untouched."""
    try:
        with get_db_session() as s:
            existing = {r.key for r in s.query(BotConfig.key).all()}
            added = 0
            for key, vtype, default, category, label, description in DEFAULTS:
                if key in existing:
                    continue
                str_val = ("true" if default is True
                           else "false" if default is False
                           else str(default))
                s.add(BotConfig(
                    key=key, value=str_val, value_type=vtype,
                    category=category, label=label, description=description,
                ))
                added += 1
            if added:
                s.commit()
                logger.info("Seeded %d bot_config defaults", added)
    except Exception:
        logger.exception("bot_config seed_defaults failed")


def get_meta(key: str) -> Tuple[str, str, str, str]:
    """Return (value_type, category, label, description) for a key."""
    for k, vtype, _default, category, label, description in DEFAULTS:
        if k == key:
            return vtype, category, label, description
    return "str", "general", key, ""


def list_by_category(category: str) -> List[Tuple[str, str, Any, str, str, str]]:
    return [d for d in DEFAULTS if d[3] == category]
