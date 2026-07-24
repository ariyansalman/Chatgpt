"""Database package for models and connection management."""

from .models import (
    Base, User, Category, Subcategory, Product, ProductKey,
    Cart, Order, OrderItem, Transaction, Settings, Broadcast, Dispute,
    ReferralReward, SupportTicket, TicketMessage, ManualPaymentMethod,
    Coupon, CouponRedemption, Review, LoyaltyLedger, BotConfig,
    ProductType, OrderStatus, DisputeStatus, TransactionStatus, PaymentMethod,
    TicketStatus, TicketSender, TicketPriority, DiscountType,
    AdminAuditLog,
    ProductVariant, StockReservation, OrderStatusHistory,
    OrderLifecycleStatus, PaymentLifecycleStatus, DeliveryStatus, ReservationStatus,
    # V9 (Premium Admin Control Center)
    WalletLedger, Promotion, AdminNotificationPref, LowStockAlertState,
    # V11 (Product Types 360)
    SubscriptionPlan, Subscription, BundleItem, Preorder, ServiceOrder,
    ManualDeliveryTask, ExternalIntegration, GeneratedValue, ExternalDeliveryLog,
    # V12 (Multi-Currency)
    Currency, DEFAULT_CURRENCY,
    # V13 (Multi-Admin RBAC + 2FA)
    AdminRole, AdminRoleType, ROLE_DEFAULT_PERMISSIONS,
    # V14 (Marketing Automation)
    MarketingTouch, MarketingCampaignType,
    # V15 (Flash Sales)
    FlashSale,
    # V17 (Telegram Stars)
    PaymentGatewayConfig, HeleketStaticWallet, HeleketDeposit, BinancePayTransaction,
    BybitPayTransaction,
    # ZiniPay duplicate-prevention
    ZiniPayUsedTransaction,
    # Admin-approval flow for failed auto-verifications
    VerificationAttemptLog, PendingManualVerification,
    UserWishlist, PriceDropAlert, RecentlyViewed, QuickBuyConfig, PreferredPayment,
    # V19 (Account & Order Features)
    OrderReceipt, UserDownload, ActivityLog, UserSession,
    # Part 3 — Sales & Marketing
    GiftCard, GiftCardRedemption, GiftCardType,
    GiftPurchase, GiftPurchaseStatus,
    # V20 (Advanced Features)
    ReferralClick, ReferralCommission, ReferralWithdrawal,
    Announcement, AnnouncementRead,
    # V21 (Six New Features)
    ScheduledBroadcast, BroadcastStatus,
    # V26 (Scheduled Broadcast V2)
    BroadcastLog, BroadcastRetryQueue,
    Refund, RefundStatus, RefundTrigger,
    LanguageConfig,
    # V22 (Favorites)
    UserFavorite,
    # V22 (Product Compare)
    ProductCompare,
    ProductCompareLog,
    # V22 (Subscription Reminder)
    SubscriptionReminderLog,
    # V23 (Price History)
    ProductPriceHistory,
    # V24 (Supplier Auto Assignment)
    SupplierProduct,
    Supplier,
    # V25 (Product FAQ)
    ProductFAQ,
    # V27 (Webhook Monitor & API Health)
    ApiHealthLog, WebhookLog, WebhookRetryQueue,
    # V28 (Product Clone & Template System)
    ProductTemplate, ProductCloneLog,
    # V34
    SettingsBackupRecord,
    DiagnosticsRecord,
    # V35
    BulkImportRecord,
    BulkExportRecord,
    BulkActionRecord,
    # V36 — Delivery Management System
    DeliveryRecord,
    # V37 — Notification Center & File/License Key Manager
    AdminNotification, NotificationSeverity, NotificationCategory,
    ManagedFile, ManagedKey, ManagedKeyDelivery, FileDownloadLog,
    ManagedKeyType, ManagedKeyStatus,
    # V38 — Flash Sale Manager (Enhanced)
    FlashSaleEvent, FlashSalePriceSnapshot, FlashSaleBroadcastLog,
    FlashSaleStatus, FlashSaleScopeType,
    # V39 — Multi-Currency Wallet & Exchange Rate Manager
    WalletCurrencyConfig, UserCurrencyWallet, CurrencyTransaction,
    ExchangeRatePair, ExchangeRateHistory, ExchangeRateLog,
    WalletCurrencyStatus, CurrencyTransactionType, CurrencyTxStatus,
    ExchangeRateSource, ExchangeRatePairStatus,
    # V40 — Sales Forecast & Business Insights
    BusinessReport, ForecastSnapshot, DailyAnalyticsSnapshot,
    # V40 — Auto Moderation & Anti-Spam
    UserModerationStatus, SpamLog, ModerationActionLog,
    BlacklistEntry, WhitelistEntry,
    SpamViolationType, ModerationActionType, ModerationStatusType,
    BlacklistEntryType, WhitelistEntryType,
    # V41 — VIP Tier Manager
    VipTier, UserVipTier, VipTierHistory,
    LoyaltyReward, LoyaltyRewardClaim,
    # V41 — API Key & Integration Manager
    ApiIntegration, ApiConnectionLog,
    # V42 — Plugin & Module Manager
    ModuleConfig,
    # V42 — Global Activity Timeline
    GlobalActivityEntry,
    # V43 — Data Export Center
    ExportJob,
    # V43 — Global Search Engine
    SearchRecord,
    # V44 — Performance & Cache Manager
    PerformanceSnapshot, OptimizationLog,
    # V44.4 — Enterprise Broadcast Campaign Manager
    BroadcastTemplate, BroadcastCampaign, CampaignExecution,
    BroadcastAutomationRule, AutomationTriggerLog,
    CampaignType, CampaignStatus, AutomationTrigger,
    # V45 — Inventory Batch / Issue Tracking
    InventoryBatch, InventoryIssue,
    # Reseller Tiers
    ResellerTier, UserReseller,
    # Delivery Job Queue
    DeliveryJob,
    # Backup & Integrity Records
    BackupRecord, IntegrityScan, IntegrityScanResult,
    # Payment Idempotency
    PaymentIdempotency,
    # Login Activity & Device Tracking
    LoginRecord, UserDevice,
    # Customer CRM
    CustomerProfile, CustomerNote, CustomerTag,
    CustomerTagAssignment, CustomerReminder,
    # V45 — Restock Notifications
    RestockSubscription, RestockNotificationLog,
    # Product Recommendation Pins
    ProductRecommendationPin,
)
from .db import init_db, get_db_session

__all__ = [
    'Base', 'User', 'Category', 'Subcategory', 'Product', 'ProductKey',
    'Cart', 'Order', 'OrderItem', 'Transaction', 'Settings', 'Broadcast', 'Dispute',
    'ReferralReward', 'SupportTicket', 'TicketMessage', 'ManualPaymentMethod',
    'Coupon', 'CouponRedemption', 'Review', 'LoyaltyLedger', 'BotConfig',
    'ProductType', 'OrderStatus', 'DisputeStatus', 'TransactionStatus', 'PaymentMethod',
    'TicketStatus', 'TicketSender', 'TicketPriority', 'DiscountType',
    'AdminAuditLog',
    'ProductVariant', 'StockReservation', 'OrderStatusHistory',
    'OrderLifecycleStatus', 'PaymentLifecycleStatus', 'DeliveryStatus', 'ReservationStatus',
    'WalletLedger', 'Promotion', 'AdminNotificationPref', 'LowStockAlertState',
    'SubscriptionPlan', 'Subscription', 'BundleItem', 'Preorder', 'ServiceOrder',
    'ManualDeliveryTask', 'ExternalIntegration', 'GeneratedValue', 'ExternalDeliveryLog',
    'Currency', 'DEFAULT_CURRENCY',
    'AdminRole', 'AdminRoleType', 'ROLE_DEFAULT_PERMISSIONS',
    'MarketingTouch', 'MarketingCampaignType',
    'FlashSale',
    'PaymentGatewayConfig', 'HeleketStaticWallet', 'HeleketDeposit', 'BinancePayTransaction',
    'BybitPayTransaction',
    'ZiniPayUsedTransaction',
    'VerificationAttemptLog', 'PendingManualVerification',
    # V18 — User Features
    'UserWishlist', 'PriceDropAlert', 'RecentlyViewed',
    'QuickBuyConfig', 'PreferredPayment',
    # V19 — Account & Order Features
    'OrderReceipt', 'UserDownload', 'ActivityLog', 'UserSession',
    # Part 3 — Sales & Marketing
    'GiftCard', 'GiftCardRedemption', 'GiftCardType',
    'GiftPurchase', 'GiftPurchaseStatus',
    # V20 — Advanced Features
    'ReferralClick', 'ReferralCommission', 'ReferralWithdrawal',
    'Announcement', 'AnnouncementRead',
    # V21 — Six New Features
    'ScheduledBroadcast', 'BroadcastStatus',
    # V26 — Scheduled Broadcast V2
    'BroadcastLog', 'BroadcastRetryQueue',
    'Refund', 'RefundStatus', 'RefundTrigger',
    'LanguageConfig',
    # V22 — Favorites
    'UserFavorite',
    # V22 — Product Compare
    'ProductCompare',
    'ProductCompareLog',
    # V22 — Subscription Reminder
    'SubscriptionReminderLog',
    # V23 — Price History
    'ProductPriceHistory',
    # V24 — Supplier Auto Assignment
    'SupplierProduct',
    # V25 — Product FAQ
    'ProductFAQ',
    'Supplier',
    # V27 — Webhook Monitor & API Health
    'ApiHealthLog', 'WebhookLog', 'WebhookRetryQueue',
    # V28 — Product Clone & Template System
    'ProductTemplate', 'ProductCloneLog',
    # V34 — Settings Backup & Diagnostics
    'SettingsBackupRecord',
    'DiagnosticsRecord',
    # V35
    'BulkImportRecord',
    'BulkExportRecord',
    'BulkActionRecord',
    # V36 — Delivery Management System
    'DeliveryRecord',
    # V37 — Notification Center & File/License Key Manager
    'AdminNotification', 'NotificationSeverity', 'NotificationCategory',
    'ManagedFile', 'ManagedKey', 'ManagedKeyDelivery', 'FileDownloadLog',
    'ManagedKeyType', 'ManagedKeyStatus',
    # V38 — Flash Sale Manager (Enhanced)
    'FlashSaleEvent', 'FlashSalePriceSnapshot', 'FlashSaleBroadcastLog',
    'FlashSaleStatus', 'FlashSaleScopeType',
    # V39 — Multi-Currency Wallet & Exchange Rate Manager
    'WalletCurrencyConfig', 'UserCurrencyWallet', 'CurrencyTransaction',
    'ExchangeRatePair', 'ExchangeRateHistory', 'ExchangeRateLog',
    'WalletCurrencyStatus', 'CurrencyTransactionType', 'CurrencyTxStatus',
    'ExchangeRateSource', 'ExchangeRatePairStatus',
    # V40 — Sales Forecast & Business Insights
    'BusinessReport', 'ForecastSnapshot', 'DailyAnalyticsSnapshot',
    # V40 — Auto Moderation & Anti-Spam
    'UserModerationStatus', 'SpamLog', 'ModerationActionLog',
    'BlacklistEntry', 'WhitelistEntry',
    'SpamViolationType', 'ModerationActionType', 'ModerationStatusType',
    'BlacklistEntryType', 'WhitelistEntryType',
    # V41 — VIP Tier Manager
    'VipTier', 'UserVipTier', 'VipTierHistory',
    'LoyaltyReward', 'LoyaltyRewardClaim',
    # V41 — API Key & Integration Manager
    'ApiIntegration', 'ApiConnectionLog',
    # V42 — Plugin & Module Manager
    'ModuleConfig',
    # V42 — Global Activity Timeline
    'GlobalActivityEntry',
    # V43 — Data Export Center
    'ExportJob',
    # V43 — Global Search Engine
    'SearchRecord',
    # V44 — Performance & Cache Manager
    'PerformanceSnapshot', 'OptimizationLog',
    # V44.4 — Enterprise Broadcast Campaign Manager
    'BroadcastTemplate', 'BroadcastCampaign', 'CampaignExecution',
    'BroadcastAutomationRule', 'AutomationTriggerLog',
    'CampaignType', 'CampaignStatus', 'AutomationTrigger',
    # V45 — Inventory Batch / Issue Tracking
    'InventoryBatch', 'InventoryIssue',
    # Reseller Tiers
    'ResellerTier', 'UserReseller',
    # Delivery Job Queue
    'DeliveryJob',
    # Backup & Integrity Records
    'BackupRecord', 'IntegrityScan', 'IntegrityScanResult',
    # Payment Idempotency
    'PaymentIdempotency',
    # Login Activity & Device Tracking
    'LoginRecord', 'UserDevice',
    # Customer CRM
    'CustomerProfile', 'CustomerNote', 'CustomerTag',
    'CustomerTagAssignment', 'CustomerReminder',
    # V45 — Restock Notifications
    'RestockSubscription', 'RestockNotificationLog',
    # Product Recommendation Pins
    'ProductRecommendationPin',
    'init_db', 'get_db_session'
]
