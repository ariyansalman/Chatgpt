"""Database models for the Telegram digital products store bot."""

from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, DateTime, ForeignKey, Text, Enum, UniqueConstraint, Numeric
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

Base = declarative_base()


class ProductType(enum.Enum):
    """Enum for product types.

    V11 (Product Types 360) — 10 new values added to the original KEY/FILE.
    All new values map to a deliverer in ``services/delivery_service.py``.
    Enum *names* (KEY, FILE, REDEEM_LINK, ...) are what SQLAlchemy stores.
    """
    # Legacy — untouched, existing rows keep working.
    KEY = "key"                     # 🔑 Software Key
    FILE = "file"                   # 📁 Downloadable File (legacy — uses download_link)

    # V11 — new product types
    REDEEM_LINK = "redeem_link"         # 🔗 Redeem / activation link
    ACCOUNT_LOGIN = "account_login"     # 📧 Email|password (+ optional recovery)
    DOWNLOADABLE_FILE = "downloadable_file"  # 📁 Telegram file_id delivery
    AUTO_GENERATED = "auto_generated"   # 🤖 Server-generated code/token/uuid
    MANUAL_DELIVERY = "manual_delivery" # 👤 Admin fulfils via queue
    PREORDER = "preorder"               # ⏳ Pre-order — admin fulfils later
    SUBSCRIPTION = "subscription"       # ♻️ Duration-based plans
    BUNDLE = "bundle"                   # 📦 Multiple child products
    SERVICE = "service"                 # 🛠️ Service — collects customer info
    VOUCHER = "voucher"                 # 🎟️ Voucher/gift code inventory
    EXTERNAL_DELIVERY = "external_delivery"  # 🌐 External API / webhook

    @classmethod
    def catalog(cls):
        """Ordered list of (enum, emoji, label) for admin pickers."""
        return [
            (cls.KEY,               "🔑", "Software Key"),
            (cls.REDEEM_LINK,       "🔗", "Redeem Link"),
            (cls.ACCOUNT_LOGIN,     "📧", "Account / Login"),
            (cls.DOWNLOADABLE_FILE, "📁", "Downloadable File"),
            (cls.AUTO_GENERATED,    "🤖", "Auto Generated"),
            (cls.MANUAL_DELIVERY,   "👤", "Manual Delivery"),
            (cls.PREORDER,          "⏳", "Pre-Order"),
            (cls.SUBSCRIPTION,      "♻️", "Subscription"),
            (cls.BUNDLE,            "📦", "Bundle / Package"),
            (cls.SERVICE,           "🛠️", "Service Product"),
            (cls.VOUCHER,           "🎟️", "Voucher / Gift Code"),
            (cls.EXTERNAL_DELIVERY, "🌐", "External Delivery"),
        ]


class OrderStatus(enum.Enum):
    """Enum for order status."""
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    FAILED = "Failed"
    REFUNDED = "Refunded"


class OrderLifecycleStatus(enum.Enum):
    """Extended order lifecycle status (V8 Premium Core).

    Kept parallel to :class:`OrderStatus` so legacy code paths that read/write
    ``Order.status`` keep working unchanged. Enriched status is stored on
    ``Order.lifecycle_status`` (nullable) and is authoritative when set.
    """
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentLifecycleStatus(enum.Enum):
    """Payment sub-status displayed in admin order detail."""
    UNPAID = "unpaid"
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    FAILED = "failed"


class DeliveryStatus(enum.Enum):
    """Delivery sub-status displayed in admin order detail."""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    REDELIVERED = "redelivered"


class ReservationStatus(enum.Enum):
    """Inventory reservation lifecycle."""
    ACTIVE = "active"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class DisputeStatus(enum.Enum):
    """Enum for dispute status."""
    NIL = "NIL"
    OPENED = "Opened"
    RESOLVED = "Resolved"


class TransactionStatus(enum.Enum):
    """Enum for transaction/payment status."""
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # Manual payment: waiting for admin approval
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"  # Order expired, was cancelled by the user, or was cancelled/deleted by an admin.
    FAILED = "failed"
    REJECTED = "rejected"  # Manual payment rejected by admin

    @classmethod
    def terminal_non_blocking(cls):
        """Statuses that must NEVER block creation of a new pending order.

        Anything other than PENDING/AWAITING_CONFIRMATION is a dead-end for
        that transaction, so a user (or admin) must always be free to start
        a fresh payment order once a prior one lands in one of these.
        """
        return (cls.COMPLETED, cls.EXPIRED, cls.CANCELLED, cls.FAILED, cls.REJECTED)


class PaymentMethod(enum.Enum):
    """Enum for payment methods."""
    CRYPTO_WALLET = "crypto_wallet"
    CARD = "card"
    MANUAL = "manual"  # Admin-managed manual payment methods
    BKASH = "bkash"    # bKash Tokenized Checkout (Personal/Merchant API)
    NAGAD = "nagad"    # Nagad Merchant Checkout
    STARS = "stars"    # Telegram Stars (native XTR in-app currency)
    CRYPTOMUS = "cryptomus"  # Cryptomus (USDT / crypto) — see services/cryptomus_payment.py.
    NOWPAYMENTS = "nowpayments"  # NOWPayments (https://nowpayments.io) — see services/nowpayments_payment.py.
    ZINIPAY = "zinipay"  # ZiniPay (https://zinipay.com) — see services/zinipay_payment.py.
    BINANCE_PAY = "binance_pay"  # Binance Pay, verified via GET /sapi/v1/pay/transactions — see services/binance_pay.py.
    BYBIT_PAY = "bybit_pay"  # Bybit Pay (UID Transfer + on-chain deposit) — see services/bybit_pay.py.
    HELEKET = "heleket"  # Heleket Static Wallet automatic crypto deposits — see services/heleket_payment.py.
    # NOTE: on PostgreSQL this enum backs a native TYPE (created by
    # SQLAlchemy as "paymentmethod"). Adding a member here updates the
    # Python enum immediately, but an EXISTING Postgres database needs the
    # matching `ALTER TYPE paymentmethod ADD VALUE 'cryptomus'` run once —
    # see migrations/v12_cryptomus_gateway.py. SQLite has no native enum
    # type, so no migration step is needed there.


class TicketStatus(enum.Enum):
    """Enum for support ticket status."""
    OPEN = "Open"
    CLOSED = "Closed"


class TicketSender(enum.Enum):
    """Who sent a ticket message."""
    USER = "user"
    ADMIN = "admin"


class TicketPriority(enum.Enum):
    """Priority level for support tickets & disputes (V16 — SLA Ticketing).

    Drives how long support has to respond before an SLA reminder /
    breach alert fires (see ``services/notifications.py``).
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

    @classmethod
    def choices(cls):
        return [c.value for c in cls]


class Currency(enum.Enum):
    """Supported store currencies (V12 — Multi-Currency).

    Stored as plain VARCHAR on the ORM models (not a SQL Enum type) so that
    adding a currency later is a painless data-only change on every backend,
    matching the convention already used for V11's enum-like status columns.
    """
    USD = "USD"
    BDT = "BDT"

    @classmethod
    def choices(cls):
        return [c.value for c in cls]

    @classmethod
    def is_valid(cls, code) -> bool:
        return bool(code) and str(code).upper() in cls.choices()


DEFAULT_CURRENCY = Currency.USD.value


class User(Base):
    """User model for storing customer information."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255))
    wallet_balance = Column(Float, default=0.0)
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # V2 features
    language = Column(String(8), default='en')
    referred_by_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    referral_earnings = Column(Float, default=0.0)
    has_purchased = Column(Boolean, default=False)

    # V5 (Phase 3): Loyalty
    loyalty_points = Column(Integer, default=0)

    # V12 (Multi-Currency): which currency this user wants prices displayed
    # in ("USD" or "BDT"). Purely a display preference — wallet_balance and
    # all internal accounting stay in USD regardless of this setting.
    preferred_currency = Column(String(3), nullable=False, default="USD")

    # V14 (Marketing Automation): last time this user was seen interacting
    # with the bot (any update). Nullable/best-effort — used only to detect
    # "inactive" users for win-back campaigns (services/marketing_automation.py).
    # Falls back to created_at / last order date when null (older accounts).
    last_seen_at = Column(DateTime, nullable=True, index=True)

    # Relationships
    orders = relationship("Order", back_populates="user")
    cart_items = relationship("Cart", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user")
    tickets = relationship("SupportTicket", back_populates="user", cascade="all, delete-orphan")


class Category(Base):
    """Category model for product organization."""
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    products = relationship("Product", back_populates="category")
    subcategories = relationship("Subcategory", back_populates="category")


class Subcategory(Base):
    """Subcategory model for additional product organization."""
    __tablename__ = 'subcategories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    category = relationship("Category", back_populates="subcategories")
    products = relationship("Product", back_populates="subcategory")


class Product(Base):
    """Product model for items available for purchase."""
    __tablename__ = 'products'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    price = Column(Float, nullable=False)
    stock_count = Column(Integer, default=0)
    product_type = Column(Enum(ProductType), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    subcategory_id = Column(Integer, ForeignKey('subcategories.id'), nullable=True)
    image_path = Column(String(500), nullable=True)
    download_link = Column(String(500), nullable=True)  # For file-type products
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── V11 (Product Types 360) ─────────────────────────────────────────
    # JSON blob (stored as TEXT) with per-product-type settings. Keys are
    # documented in ``services/delivery_service.py``.
    type_config = Column(Text, nullable=True)
    delivery_note = Column(Text, nullable=True)
    warranty_info = Column(Text, nullable=True)
    min_quantity = Column(Integer, nullable=True)
    max_quantity = Column(Integer, nullable=True)
    bulk_purchase_enabled = Column(Boolean, default=True, nullable=False)
    # Telegram file_id for downloadable_file type — avoids re-uploading blobs.
    telegram_file_id = Column(String(256), nullable=True)
    telegram_file_type = Column(String(24), nullable=True)  # document|photo|video|audio
    reusable = Column(Boolean, default=False, nullable=False)

    # ── Section 14 — Product Badges ─────────────────────────────────────
    # Admin-managed flags. Best Seller / New / Sale are computed dynamically
    # (see services/badges.py) so we don't persist derived state.
    is_featured = Column(Boolean, default=False, nullable=False, index=True)
    sale_price = Column(Float, nullable=True)   # optional product-level sale
    sales_count = Column(Integer, default=0, nullable=False)  # denorm counter

    # V12 (Multi-Currency): currency that `price` / `sale_price` are entered
    # and stored in for this product ("USD" or "BDT"). Defaults to "USD" so
    # every existing product keeps working unchanged. All internal pricing
    # math in services/pricing.py is currency-relative (it never mixes
    # currencies within one product), so this only needs to be read when
    # converting for display or snapshotting an order.
    currency = Column(String(3), nullable=False, default="USD", index=True)

    # V17 (Formatted Account Delivery): optional admin-defined template used
    # to render structured account/key data (email, password, recovery,
    # expiry, ...) into a nicely formatted delivery message. Uses
    # ``{placeholder}`` tokens — see ``services/structured_delivery.py``.
    # NULL means "no template configured", in which case delivery falls back
    # to the exact legacy raw-text behaviour (fully backward compatible).
    delivery_format_template = Column(Text, nullable=True)

    # ── Flat Product Catalog (no-pagination customer view) ─────────────
    # Optional admin-chosen emoji shown in front of the product name on the
    # single-screen "🛍 Products" catalog and in product detail/search rows.
    # NULL falls back to a generic 📦 (or ❌ while out of stock — see
    # utils.helpers.catalog_display_emoji). Picked up automatically by the
    # schema auto-fixer in database/db.py for existing databases.
    product_emoji = Column(String(32), nullable=True)

    # Stable manual ordering for the flat catalog (lower shows first within
    # the same stock-availability group). NULL/duplicate values fall back to
    # Product.id ASC as a deterministic secondary sort.
    sort_order = Column(Integer, nullable=True)

    # ── Part 3: Bundle pricing ─────────────────────────────────────────────
    # Override price displayed for bundle products; None = use base price.
    bundle_price            = Column(Float, nullable=True)
    # Display-only discount percentage; informational for the customer.
    bundle_discount_percent = Column(Float, nullable=True)

    # ── Soft delete ──────────────────────────────────────────────────────
    # Admin "delete" never removes the row: order_items.product_id is
    # NOT NULL, so physically deleting a Product would force SQLAlchemy to
    # null out the FK on every associated OrderItem and raise
    # IntegrityError. Instead we mark the product hidden/soft-deleted and
    # keep the row (and its id) alive forever so existing OrderItem rows
    # keep a valid, non-null product_id. is_active is also set to False so
    # every existing "is_active == True" storefront/listing query already
    # in the codebase hides it automatically.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    category = relationship("Category", back_populates="products")
    subcategory = relationship("Subcategory", back_populates="products")
    product_keys = relationship("ProductKey", back_populates="product", cascade="all, delete-orphan")
    cart_items = relationship("Cart", back_populates="product")
    order_items = relationship("OrderItem", back_populates="product")
    variants = relationship("ProductVariant", back_populates="product",
                            cascade="all, delete-orphan",
                            order_by="ProductVariant.display_order")


class ProductVariant(Base):
    """A purchasable option of a product (e.g. 1 Month / 3 Months / Family).

    Products without variants keep working exactly as before — checkout paths
    check ``product.variants`` and only branch when at least one active
    variant exists.
    """
    __tablename__ = 'product_variants'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    name = Column(String(120), nullable=False)               # "1 Month" / "Family"
    price = Column(Float, nullable=False)
    sale_price = Column(Float, nullable=True)                # NULL when not on sale
    stock_count = Column(Integer, default=0)                 # for FILE-type variants
    is_active = Column(Boolean, default=True, index=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="variants")
    keys = relationship("ProductKey", back_populates="variant")

    @property
    def effective_price(self) -> float:
        return self.sale_price if (self.sale_price and self.sale_price > 0) else self.price


class ProductKey(Base):
    """SEPARATE TABLE for storing product keys inventory."""
    __tablename__ = 'product_keys'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    key_value = Column(Text, nullable=False)
    # Section 15 — deterministic fingerprint (sha256 of normalized key_value)
    # for duplicate detection without exposing full values in logs.
    key_fingerprint = Column(String(64), nullable=True, index=True)
    is_sold = Column(Boolean, default=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    reservation_id = Column(Integer, ForeignKey('stock_reservations.id'), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sold_at = Column(DateTime, nullable=True)
    batch_id = Column(Integer, ForeignKey('inventory_batches.id'), nullable=True, index=True)
    cost_per_unit_snapshot = Column(Float, nullable=True)

    # Relationships
    product = relationship("Product", back_populates="product_keys")
    order = relationship("Order", back_populates="assigned_keys")
    variant = relationship("ProductVariant", back_populates="keys")


class Cart(Base):
    """Shopping cart model for temporary product storage."""
    __tablename__ = 'cart'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="cart_items")
    product = relationship("Product", back_populates="cart_items")
    variant = relationship("ProductVariant")


class Order(Base):
    """Order model for purchase records."""
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    total_amount = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PROCESSING)
    dispute_status = Column(Enum(DisputeStatus), default=DisputeStatus.NIL)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # V8 Premium Core — parallel to `status`, nullable so old rows stay valid.
    lifecycle_status = Column(Enum(OrderLifecycleStatus), nullable=True, index=True)
    payment_status = Column(Enum(PaymentLifecycleStatus), nullable=True)
    delivery_status = Column(Enum(DeliveryStatus), nullable=True)

    # V12 (Multi-Currency): the currency `total_amount` is denominated in.
    # `total_amount` itself is ALWAYS the real USD amount debited from the
    # wallet (wallet_balance is USD-only) — this column only records which
    # currency the buyer was viewing/checking out in, so receipts and order
    # history can show "you paid ৳X (~$Y)" instead of always USD.
    currency = Column(String(3), nullable=False, default="USD")

    # Relationships
    user = relationship("User", back_populates="orders")
    order_items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    assigned_keys = relationship("ProductKey", back_populates="order")
    disputes = relationship("Dispute", back_populates="order", cascade="all, delete-orphan")
    status_history = relationship("OrderStatusHistory", back_populates="order",
                                  cascade="all, delete-orphan",
                                  order_by="OrderStatusHistory.created_at")


class OrderItem(Base):
    """Order items model for individual line items in orders."""
    __tablename__ = 'order_items'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    delivered_asset = Column(Text, nullable=True)  # Keys or download link
    created_at = Column(DateTime, default=datetime.utcnow)
    # V10 pricing / profit snapshots
    base_price = Column(Float, nullable=True)
    unit_cost_snapshot = Column(Float, nullable=True)
    total_cost_snapshot = Column(Float, nullable=True)
    reseller_tier_id = Column(Integer, ForeignKey('reseller_tiers.id'), nullable=True)
    pricing_meta = Column(Text, nullable=True)  # JSON breakdown

    # Relationships
    order = relationship("Order", back_populates="order_items")
    product = relationship("Product", back_populates="order_items")
    variant = relationship("ProductVariant")


class Transaction(Base):
    """Transaction model for wallet funding history."""
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    crypto_address = Column(String(500), nullable=True)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # V3: Manual payment support
    manual_method_id = Column(Integer, ForeignKey('manual_payment_methods.id'), nullable=True)
    proof = Column(Text, nullable=True)  # User submitted TXID / note / file_id
    admin_note = Column(Text, nullable=True)

    # V6 (Payment v2): explicit TXID + screenshot fields (proof/crypto_address kept
    # for backward-compat with legacy rows that packed both into a single column).
    txid = Column(String(128), nullable=True, index=True)
    proof_file_id = Column(String(256), nullable=True)

    # Telegram Stars: exact number of ⭐ Stars quoted/charged for this
    # top-up, frozen at invoice-creation time. ``amount`` still holds the
    # USD value credited to the wallet — ``stars_amount`` is only used to
    # cross-check what Telegram actually charged in precheckout/successful
    # payment (see services/telegram_stars.py).
    stars_amount = Column(Integer, nullable=True)

    # Locked exchange rate for non-stablecoin networks (e.g. LTC).
    # NULL for USDT-denominated orders.  Both fields are set atomically at
    # order-creation time and never mutated — the rate is frozen for the
    # lifetime of the order.
    locked_crypto_rate   = Column(Float, nullable=True)   # USD per 1 unit of crypto at order time
    locked_crypto_amount = Column(Float, nullable=True)   # exact crypto units the user must send

    # Notification dedup flags — flipped exactly once, atomically, at the
    # moment the corresponding notification is actually sent. Scheduler jobs
    # and retry-driven handlers gate on these (not just `status`) before
    # sending, so a job re-run, an overlapping execution, or a bot restart
    # can never re-send "Payment Expired" / "Payment Review" for the same
    # order.
    expiry_notified = Column(Boolean, default=False, nullable=False)
    review_notified = Column(Boolean, default=False, nullable=False)

    # Relationships
    user = relationship("User", back_populates="transactions")
    manual_method = relationship("ManualPaymentMethod")


class ManualPaymentMethod(Base):
    """Admin-managed manual payment methods (USDT TRC20, Binance Pay, bKash, etc.)."""
    __tablename__ = 'manual_payment_methods'

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)                # "USDT TRC20"
    emoji = Column(String(12), default="💳")                  # "🪙"
    instructions = Column(Text, nullable=False)               # payment address / how-to
    min_amount = Column(Float, default=1.0)
    is_active = Column(Boolean, default=True, index=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # V6 (Payment v2): richer, admin-editable metadata rendered on the user
    # instruction card. All nullable so v3 rows keep working unchanged.
    account_label = Column(String(120), nullable=True)   # e.g. "bKash Personal"
    account_number = Column(String(255), nullable=True)  # phone / address / IBAN
    max_amount = Column(Float, nullable=True)            # 0/NULL = no ceiling
    require_txid = Column(Boolean, default=True, nullable=False)
    require_proof = Column(Boolean, default=True, nullable=False)


class PaymentGatewayConfig(Base):
    """Admin-configurable settings for native/in-house payment gateways.

    Unlike bKash/Nagad (which store credentials as loose key/value pairs in
    ``bot_config``, see services/bkash_payment.py), gateways that need
    structured, typed settings — e.g. Telegram Stars' Stars→USD conversion
    rate — get one row here, keyed by ``gateway``.
    """
    __tablename__ = 'payment_gateway_configs'

    id = Column(Integer, primary_key=True)
    gateway = Column(String(50), unique=True, nullable=False, index=True)  # e.g. "telegram_stars"
    is_enabled = Column(Boolean, default=False, nullable=False)

    # Telegram Stars: USD credited to the user's wallet per 1 ⭐ Star paid.
    rate_usd_per_star = Column(Float, nullable=True)
    min_stars = Column(Integer, nullable=True)
    max_stars = Column(Integer, nullable=True)

    # bKash / Nagad: Manual vs Auto mode toggle.
    #   "auto"   (default) — existing Tokenized Checkout / Merchant Checkout
    #            API flow (services/bkash_payment.py, services/nagad_payment.py).
    #   "manual" — hides the API credential fields; the user instead sends
    #            money directly to `manual_merchant_number` and reports a
    #            TrxID/screenshot through the same manual verification flow
    #            used by ManualPaymentMethod (see handlers/payment_handlers.py).
    mode = Column(String(10), nullable=False, default="auto")
    manual_merchant_number = Column(String(120), nullable=True)  # bKash/Nagad number shown to users
    manual_instructions = Column(Text, nullable=True)            # instructions shown to users in manual mode

    # Cryptomus (USDT/crypto): credentials for the merchant API — see
    # services/cryptomus_payment.py. Stored here (like Telegram Stars)
    # rather than in bot_config, since it's just these two fields.
    merchant_uuid = Column(String(120), nullable=True)
    api_key = Column(String(255), nullable=True)

    # Generic second secret slot, reused by any gateway that needs one beyond
    # merchant_uuid/api_key — e.g. NOWPayments' IPN secret (services/nowpayments_payment.py).
    secondary_key = Column(String(255), nullable=True)

    # ZiniPay Transaction Verification (gateway="zinipay").
    # Each field is NULL until the admin sets it — the bot hides any number
    # that is NULL so users never see an empty field.
    zinipay_bkash_number   = Column(String(120), nullable=True)   # e.g. "01712345678"
    zinipay_nagad_number   = Column(String(120), nullable=True)
    zinipay_rocket_number  = Column(String(120), nullable=True)
    zinipay_upay_number    = Column(String(120), nullable=True)
    # Which provider to highlight as "default" in the payment screen.
    zinipay_default_provider = Column(String(10), nullable=True, default="bkash")
    # USD → BDT exchange rate for ZiniPay.  NULL = use the global Settings rate.
    zinipay_usd_to_bdt_rate  = Column(Float, nullable=True)
    # When True the bot refreshes the rate automatically from the global API
    # configured in Settings (exchange_rate_api_url).
    zinipay_auto_rate        = Column(Boolean, nullable=False, default=False)
    # Free-form payment instructions shown below the wallet numbers.
    zinipay_instructions     = Column(Text, nullable=True)

    # Binance Pay (gateway="binance_pay" — see services/binance_pay.py).
    # NOTE: the actual BINANCE_API_KEY / BINANCE_API_SECRET are READ from
    # environment variables ONLY (config/settings.py) — never stored here,
    # never entered via Telegram. Only display/limit settings live in this
    # row; see handlers/admin_binance.py.
    binance_pay_id = Column(String(64), nullable=True)               # Binance Pay ID shown to users
    binance_allowed_currencies = Column(String(120), nullable=True, default="USDT,USDC")
    binance_min_amount = Column(Float, nullable=True)
    binance_max_amount = Column(Float, nullable=True)
    binance_order_expiry_minutes = Column(Integer, nullable=True, default=30)
    binance_bonus_percent = Column(Float, nullable=True, default=0.0)
    binance_instructions = Column(Text, nullable=True)
    # Admin-configurable API credentials (DB storage — fallback to env vars).
    # When set via the admin panel, these take priority over environment variables.
    # Plain-text storage follows the same pattern as Cryptomus / NOWPayments credentials
    # in this project. Access to the DB is equivalent to access to the env vars.
    binance_api_key = Column(Text, nullable=True)
    binance_api_secret = Column(Text, nullable=True)

    # Bybit Pay (gateway="bybit_pay" — see services/bybit_pay.py).
    bybit_uid = Column(String(64), nullable=True)                        # Bybit UID shown to users for UID Transfer
    bybit_wallet_trc20 = Column(String(255), nullable=True)              # USDT TRC20 deposit address
    bybit_wallet_bep20 = Column(String(255), nullable=True)              # USDT BEP20 deposit address
    bybit_wallet_erc20 = Column(String(255), nullable=True)              # USDT ERC20 deposit address
    bybit_wallet_ltc = Column(String(255), nullable=True)               # LTC (Litecoin) deposit address
    bybit_wallet_avaxc = Column(String(255), nullable=True)             # USDT Avalanche C-Chain deposit address
    bybit_wallet_ton = Column(String(255), nullable=True)               # USDT TON deposit address
    bybit_wallet_base = Column(String(255), nullable=True)              # USDT Base (Coinbase Base L2) deposit address
    bybit_wallet_arb = Column(String(255), nullable=True)               # USDT Arbitrum One deposit address
    bybit_wallet_op = Column(String(255), nullable=True)                # USDT Optimism deposit address
    bybit_wallet_matic = Column(String(255), nullable=True)             # USDT Polygon (MATIC) deposit address
    bybit_wallet_sol = Column(String(255), nullable=True)               # USDT Solana deposit address
    bybit_allowed_networks = Column(String(64), nullable=True, default="TRC20,BEP20,ERC20,LTC,AVAXC,TON,BASE,ARBONE,OP,MATIC,SOL")
    bybit_min_amount = Column(Float, nullable=True)
    bybit_max_amount = Column(Float, nullable=True)
    bybit_order_expiry_minutes = Column(Integer, nullable=True, default=30)
    bybit_bonus_percent = Column(Float, nullable=True, default=0.0)
    bybit_instructions = Column(Text, nullable=True)
    # Admin-configurable API credentials (same pattern as binance above).
    bybit_api_key = Column(Text, nullable=True)
    bybit_api_secret = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HeleketStaticWallet(Base):
    """One reusable Heleket static deposit address per user/currency/network."""
    __tablename__ = 'heleket_static_wallets'
    __table_args__ = (
        UniqueConstraint('order_id', name='uq_heleket_wallet_order_id'),
        UniqueConstraint('wallet_address_uuid', name='uq_heleket_wallet_address_uuid'),
        UniqueConstraint('user_id', 'currency', 'network', name='uq_heleket_wallet_user_pair'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    order_id = Column(String(100), nullable=False)
    heleket_wallet_uuid = Column(String(64), nullable=True)
    wallet_address_uuid = Column(String(64), nullable=False)
    address = Column(String(255), nullable=False)
    currency = Column(String(16), nullable=False)
    network = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HeleketDeposit(Base):
    """Persistent Heleket webhook/payment record and credit state."""
    __tablename__ = 'heleket_deposits'
    __table_args__ = (
        UniqueConstraint('heleket_payment_uuid', name='uq_heleket_deposit_payment_uuid'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    heleket_payment_uuid = Column(String(64), nullable=False, index=True)
    order_id = Column(String(100), nullable=False, index=True)
    wallet_address_uuid = Column(String(64), nullable=False, index=True)
    currency = Column(String(16), nullable=False)
    network = Column(String(32), nullable=False)
    payment_amount = Column(Float, nullable=False)
    payment_amount_usd = Column(Float, nullable=False)
    merchant_amount = Column(Float, nullable=True)
    status = Column(String(32), nullable=False)
    credited_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class BinancePayTransaction(Base):
    """A verified Binance Pay transaction credited to a user's wallet.

    ``transaction_id`` (the Binance Pay transaction/order ID the user pastes
    into the bot) carries a UNIQUE constraint — this is the core of the
    duplicate-payment protection required for Binance Pay: the same Binance
    transaction can never be credited twice, and can never be claimed by a
    different user/order than the one that first verified it. Rows are only
    ever inserted AFTER a successful GET /sapi/v1/pay/transactions lookup
    (see services/binance_pay.py) — the Binance API response is always the
    source of truth, never the user-submitted text alone.
    """
    __tablename__ = 'binance_pay_transactions'
    __table_args__ = (
        UniqueConstraint('transaction_id', name='uq_binance_pay_transaction_id'),
    )

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(128), nullable=False, index=True)
    binance_order_id = Column(String(128), nullable=True, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    internal_order_id = Column(Integer, ForeignKey('transactions.id'), nullable=False, index=True)
    currency = Column(String(16), nullable=False)
    expected_amount = Column(Numeric(20, 8), nullable=False)
    received_amount = Column(Numeric(20, 8), nullable=False)
    transaction_time = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, default=datetime.utcnow)
    raw_transaction_data = Column(Text, nullable=True)  # JSON dump of the matched Binance record


class BybitPayTransaction(Base):
    """A verified Bybit Pay transaction (UID Transfer or on-chain deposit)
    credited to a user's wallet.

    ``transaction_id`` (the Bybit internal transfer Transaction ID, or the
    blockchain TXID, that the user pastes into the bot) carries a UNIQUE
    constraint — this is the core of the duplicate-payment protection
    required for Bybit Pay: the same transaction can never be credited
    twice, and can never be claimed by a different user/order than the one
    that first verified it. Rows are only ever inserted AFTER a successful
    lookup via GET /v5/asset/deposit/query-internal-record (UID transfer) or
    GET /v5/asset/deposit/query-record (on-chain) — see
    services/bybit_pay.py — the Bybit API response is always the source of
    truth, never the user-submitted text alone.
    """
    __tablename__ = 'bybit_pay_transactions'
    __table_args__ = (
        UniqueConstraint('transaction_id', name='uq_bybit_pay_transaction_id'),
    )

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(128), nullable=False, index=True)
    bybit_record_id = Column(String(128), nullable=True, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    internal_order_id = Column(Integer, ForeignKey('transactions.id'), nullable=False, index=True)
    payment_type = Column(String(16), nullable=False)   # "uid_transfer" | "onchain"
    network = Column(String(16), nullable=True)          # TRC20 / BEP20 / ERC20 (onchain only)
    currency = Column(String(16), nullable=False)
    expected_amount = Column(Numeric(20, 8), nullable=False)
    received_amount = Column(Numeric(20, 8), nullable=False)
    transaction_time = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, default=datetime.utcnow)
    raw_transaction_data = Column(Text, nullable=True)  # JSON dump of the matched Bybit record


class ZiniPayUsedTransaction(Base):
    """A confirmed ZiniPay transaction credited to a user's wallet.

    ``trx_id`` (ZiniPay's trxID returned by POST /v1/trx/verify) carries a
    UNIQUE constraint — the core of the replay-attack prevention for ZiniPay.
    The same trxID can never be credited twice and can never be claimed by a
    different user/order than the one that first verified + confirmed it.
    A row is only inserted AFTER a successful POST /v1/trx/confirm call —
    the ZiniPay API response is always the source of truth.
    See services/zinipay_payment.py and handlers/payment_handlers.py.
    """
    __tablename__ = 'zinipay_used_transactions'
    __table_args__ = (
        UniqueConstraint('trx_id', name='uq_zinipay_trx_id'),
    )

    id = Column(Integer, primary_key=True)
    trx_id = Column(String(128), nullable=False, index=True)      # ZiniPay trxID (unique)
    verify_id = Column(Integer, nullable=True)                     # data.id from /verify
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    internal_order_id = Column(Integer, ForeignKey('transactions.id'), nullable=False, index=True)
    provider = Column(String(64), nullable=True)                   # "bkash" / "nagad" / "rocket"
    sender = Column(String(128), nullable=True)                    # sender mobile / account
    amount = Column(Numeric(20, 2), nullable=False)                # amount verified+confirmed
    verified_at = Column(DateTime, default=datetime.utcnow)


class Settings(Base):
    """Settings model for store configuration (single row table)."""
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    welcome_message = Column(Text, default="Welcome to our digital store!")
    store_logo_path = Column(String(500), nullable=True)
    support_username = Column(String(255), nullable=True)
    channel_username = Column(String(255), nullable=True)
    # V2: Referral configuration
    referral_reward_amount = Column(Float, default=0.10)
    referral_required_channel = Column(String(255), nullable=True)
    referral_enabled = Column(Boolean, default=True)
    # V4 (Phase 2): Multi-currency display. DB values always stay in USD.
    secondary_currency_code = Column(String(8), nullable=True)   # "EUR", "BDT", ...
    secondary_currency_symbol = Column(String(8), nullable=True) # "€", "৳", ...
    secondary_currency_rate = Column(Float, default=0.0)         # 1 USD = rate * secondary
    # V5 (Phase 3): Loyalty program
    loyalty_enabled = Column(Boolean, default=True)
    loyalty_earn_rate = Column(Float, default=1.0)        # points per $1 spent
    loyalty_redeem_rate = Column(Float, default=100.0)    # points per $1 credit
    loyalty_min_redeem = Column(Integer, default=100)     # min points to redeem

    # V12 (Multi-Currency): USD <-> BDT exchange rate configuration.
    # exchange_rate_mode: "fixed" (use usd_to_bdt_rate as-is) or
    #                     "api"   (fetch from exchange_rate_api_url, falling
    #                              back to the last good value / fixed rate
    #                              on any failure).
    exchange_rate_mode = Column(String(8), nullable=False, default="fixed")
    usd_to_bdt_rate = Column(Float, nullable=False, default=110.0)   # 1 USD = N BDT (fixed/fallback)
    exchange_rate_api_url = Column(String(500), nullable=True)
    exchange_rate_last_value = Column(Float, nullable=True)     # last good rate fetched from the API
    exchange_rate_last_synced = Column(DateTime, nullable=True)  # when it was last fetched

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Broadcast(Base):
    """Broadcast model for tracking broadcast messages."""
    __tablename__ = 'broadcasts'

    id = Column(Integer, primary_key=True)
    message_text = Column(Text, nullable=False)
    image_path = Column(String(500), nullable=True)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Dispute(Base):
    """Dispute model for order disputes."""
    __tablename__ = 'disputes'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    status = Column(Enum(DisputeStatus), default=DisputeStatus.OPENED)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    admin_notes = Column(Text, nullable=True)

    # V16 (Priority-Based Ticketing / SLA)
    priority = Column(Enum(TicketPriority), default=TicketPriority.HIGH,
                      nullable=False, index=True)
    sla_deadline = Column(DateTime, nullable=True, index=True)
    sla_reminder_sent = Column(Boolean, default=False, nullable=False)
    sla_breached = Column(Boolean, default=False, nullable=False)

    # Relationships
    order = relationship("Order", back_populates="disputes")
    user = relationship("User")


class ReferralReward(Base):
    """Referral reward log — one entry per successful first-purchase reward."""
    __tablename__ = 'referral_rewards'

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    referred_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    amount = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SupportTicket(Base):
    """Support ticket opened by a user."""
    __tablename__ = 'support_tickets'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    subject = Column(String(500), nullable=False)
    status = Column(Enum(TicketStatus), default=TicketStatus.OPEN, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # V16 (Priority-Based Ticketing / SLA)
    priority = Column(Enum(TicketPriority), default=TicketPriority.MEDIUM,
                      nullable=False, index=True)
    sla_deadline = Column(DateTime, nullable=True, index=True)
    sla_reminder_sent = Column(Boolean, default=False, nullable=False)
    sla_breached = Column(Boolean, default=False, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    # ── V20: Enhanced support ticket fields ──────────────────────────────────
    category = Column(String(32), nullable=True, default='general')
    assigned_admin_id = Column(BigInteger, nullable=True)
    ticket_number = Column(String(20), nullable=True, index=True)

    user = relationship("User", back_populates="tickets")
    messages = relationship("TicketMessage", back_populates="ticket", cascade="all, delete-orphan", order_by="TicketMessage.created_at")


class TicketMessage(Base):
    """A single message inside a support ticket thread."""
    __tablename__ = 'ticket_messages'

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey('support_tickets.id'), nullable=False, index=True)
    sender = Column(Enum(TicketSender), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── V20: File/image attachment support ────────────────────────────────────
    file_id = Column(String(256), nullable=True)
    file_type = Column(String(16), nullable=True)  # 'photo', 'document', 'video'

    ticket = relationship("SupportTicket", back_populates="messages")


# ─── V4 (Phase 2): Coupons / Promo codes ─────────────────────────────
class DiscountType(enum.Enum):
    PERCENT = "percent"   # 10 = 10%
    AMOUNT = "amount"     # 5 = flat $5


class Coupon(Base):
    """Promo / discount coupon codes managed by admin."""
    __tablename__ = 'coupons'

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    discount_type = Column(Enum(DiscountType), default=DiscountType.PERCENT, nullable=False)
    discount_value = Column(Float, nullable=False)
    min_order_amount = Column(Float, default=0.0)
    max_uses = Column(Integer, default=0)   # 0 = unlimited
    used_count = Column(Integer, default=0)
    per_user_limit = Column(Integer, default=1)  # 0 = unlimited
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CouponRedemption(Base):
    """Log of coupon uses — one entry per successful redemption."""
    __tablename__ = 'coupon_redemptions'

    id = Column(Integer, primary_key=True)
    coupon_id = Column(Integer, ForeignKey('coupons.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    discount_applied = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── V5 (Phase 3): Product Reviews ───────────────────────────────────
class Review(Base):
    """User review + rating for a purchased product."""
    __tablename__ = 'reviews'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    rating = Column(Integer, nullable=False)   # 1..5
    comment = Column(Text, nullable=True)
    is_hidden   = Column(Boolean, default=False)
    # ── Part 3 additions (is_approved defaults True for all existing rows) ──
    is_approved = Column(Boolean, nullable=False, default=True, server_default='true')
    is_pinned   = Column(Boolean, nullable=False, default=False, server_default='false')
    updated_at  = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ─── V5 (Phase 3): Loyalty Points Ledger ─────────────────────────────
class LoyaltyLedger(Base):
    """Audit log of every loyalty-point movement (+earn / -redeem)."""
    __tablename__ = 'loyalty_ledger'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    change = Column(Integer, nullable=False)         # positive earn, negative redeem
    balance_after = Column(Integer, nullable=False)
    reason = Column(String(64), nullable=False)      # "purchase" | "redeem" | "admin"
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BotConfig(Base):
    """Generic key/value config editable from the admin panel — no code edits."""
    __tablename__ = 'bot_config'

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    value_type = Column(String(16), default='str')   # str | int | float | bool | text
    category = Column(String(32), default='general', index=True)
    label = Column(String(128), default='')
    description = Column(Text, default='')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── V7 (Admin v2): Admin Audit Log ─────────────────────────────────
class AdminAuditLog(Base):
    """Append-only log of privileged admin actions.

    Never store secrets, tokens, passwords, or payment proofs — only enough
    context to reconstruct *what* an admin did to *which* entity.
    """
    __tablename__ = 'admin_audit_logs'

    id = Column(Integer, primary_key=True)
    admin_telegram_id = Column(BigInteger, nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)     # e.g. "payment.approve"
    target_type = Column(String(32), nullable=True)             # "user" | "order" | "product" | ...
    target_id = Column(String(64), nullable=True)               # stringified PK / TG id
    details = Column(Text, nullable=True)                       # short human description
    # V21 enhanced audit columns
    old_value = Column(Text, nullable=True)                     # serialized previous value
    new_value = Column(Text, nullable=True)                     # serialized new value
    ip_address = Column(String(45), nullable=True)              # IPv4/IPv6 if available
    module = Column(String(64), nullable=True, index=True)      # module/feature area
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── V8 (Premium Core): Inventory Reservations ──────────────────────
class StockReservation(Base):
    """Temporary hold on inventory during checkout / payment.

    A reservation locks specific ``ProductKey`` rows (for KEY products) or a
    quantity slot (for FILE products) so two users cannot buy the same stock
    concurrently. It is created at the payment step, expires automatically
    after ``inventory_reservation_ttl_minutes``, is CONSUMED on successful
    delivery, and RELEASED on cancel / rejection / expiry.
    """
    __tablename__ = 'stock_reservations'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    status = Column(Enum(ReservationStatus), default=ReservationStatus.ACTIVE,
                    nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    released_at = Column(DateTime, nullable=True)

    user = relationship("User")
    product = relationship("Product")
    variant = relationship("ProductVariant")
    order = relationship("Order")


# ─── V8 (Premium Core): Order Status History ────────────────────────
class OrderStatusHistory(Base):
    """Append-only timeline of order status transitions."""
    __tablename__ = 'order_status_history'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    from_status = Column(String(32), nullable=True)     # OrderLifecycleStatus.name or None
    to_status = Column(String(32), nullable=False)
    actor_type = Column(String(16), nullable=False, default='system')  # system|user|admin
    admin_id = Column(BigInteger, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    order = relationship("Order", back_populates="status_history")


# ═════════════════════════════════════════════════════════════════════
# V9 (Premium Admin Control Center) — additive, non-destructive
# ═════════════════════════════════════════════════════════════════════

class WalletLedger(Base):
    """Append-only ledger for every wallet balance mutation.

    Written in the same transaction as ``User.wallet_balance`` by
    ``services/wallet.py``. Legacy paths that still write the balance
    directly are unaffected; those movements simply aren't ledgered yet.
    """
    __tablename__ = 'wallet_ledger'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    delta = Column(Float, nullable=False)              # +credit / -debit
    balance_after = Column(Float, nullable=False)
    reason = Column(String(255), nullable=True)
    actor_type = Column(String(16), nullable=False, default='system')  # system|user|admin
    actor_id = Column(BigInteger, nullable=True)       # admin telegram id or user pk
    ref_type = Column(String(32), nullable=True)       # order|topup|refund|admin_adjust|promo
    ref_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Promotion(Base):
    """Scheduled promotion — optional wrapper on top of a coupon or product.

    This does NOT duplicate Coupons; a Promotion references an existing
    Coupon (or a product/category price rule) plus a schedule window.
    """
    __tablename__ = 'promotions'

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    coupon_id = Column(Integer, ForeignKey('coupons.id'), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=True, index=True)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True, index=True)
    discount_pct = Column(Float, nullable=True)        # 0..100
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminNotificationPref(Base):
    """Per-admin toggles for the notification fan-out."""
    __tablename__ = 'admin_notification_prefs'

    id = Column(Integer, primary_key=True)
    admin_telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    new_order = Column(Boolean, default=True, nullable=False)
    manual_payment = Column(Boolean, default=True, nullable=False)
    dispute = Column(Boolean, default=True, nullable=False)
    low_stock = Column(Boolean, default=True, nullable=False)
    refund = Column(Boolean, default=True, nullable=False)
    ticket_reply = Column(Boolean, default=True, nullable=False)
    subscription = Column(Boolean, default=True, nullable=False)  # V13 recurring billing
    sla_warning = Column(Boolean, default=True, nullable=False)  # V16 SLA ticketing
    sla_breach = Column(Boolean, default=True, nullable=False)   # V16 SLA ticketing
    # ── Enterprise Admin Notification System (migration 20260917) ──────────
    new_user         = Column(Boolean, default=True, nullable=False)
    deposit          = Column(Boolean, default=True, nullable=False)
    payment_failed   = Column(Boolean, default=True, nullable=False)
    payment_expired  = Column(Boolean, default=True, nullable=False)
    payment_reversed = Column(Boolean, default=True, nullable=False)
    order_delivered  = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LowStockAlertState(Base):
    """Debounce state for the low-stock notifier job.

    A row is upserted the first time a product crosses the threshold; the
    job won't re-notify until stock rises above the threshold and drops
    below again (edge-triggered).
    """
    __tablename__ = 'low_stock_alert_state'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    last_alert_at = Column(DateTime, default=datetime.utcnow)
    last_stock_seen = Column(Integer, default=0)

    # ── V20: Enhanced low-stock monitoring fields ─────────────────────────────
    silent_mode = Column(Boolean, default=False, nullable=False)
    custom_threshold = Column(Integer, nullable=True)   # per-product override
    fast_sell_alert_sent = Column(Boolean, default=False, nullable=False)
    fast_sell_sales_count = Column(Integer, default=0)
    fast_sell_window_start = Column(DateTime, nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# V10: Business Scale & Operations
# Suppliers, batches, quality, resellers, delivery queue, backups, integrity
# All additive. All FKs nullable so historical rows keep working.
# Enums intentionally stored as VARCHAR (validated in Python) to avoid the
# PostgreSQL native-enum migration pain seen with OrderStatus previously.
# ══════════════════════════════════════════════════════════════════════════

class Supplier(Base):
    __tablename__ = 'suppliers'
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, index=True)
    contact = Column(String(255), nullable=True)
    telegram_username = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    # V24 — Auto Assignment
    priority = Column(Integer, nullable=False, default=10, index=True)
    total_delivered = Column(Integer, nullable=False, default=0)
    total_failed = Column(Integer, nullable=False, default=0)
    last_activity = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches = relationship("InventoryBatch", back_populates="supplier")
    product_assignments = relationship("SupplierProduct", back_populates="supplier", cascade="all, delete-orphan")


class InventoryBatch(Base):
    __tablename__ = 'inventory_batches'
    id = Column(Integer, primary_key=True)
    reference = Column(String(80), unique=True, nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=True, index=True)
    quantity_imported = Column(Integer, nullable=False, default=0)
    cost_per_unit = Column(Float, nullable=False, default=0.0)
    total_cost = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=True)
    import_source = Column(String(32), nullable=True)   # 'manual', 'bulk', 'api'
    notes = Column(Text, nullable=True)
    created_by = Column(BigInteger, nullable=True)      # admin telegram id
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    supplier = relationship("Supplier", back_populates="batches")


class InventoryIssue(Base):
    __tablename__ = 'inventory_issues'
    id = Column(Integer, primary_key=True)
    product_key_id = Column(Integer, ForeignKey('product_keys.id'), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    dispute_id = Column(Integer, ForeignKey('disputes.id'), nullable=True, index=True)
    batch_id = Column(Integer, ForeignKey('inventory_batches.id'), nullable=True, index=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=True, index=True)
    issue_type = Column(String(32), nullable=False, index=True)  # INVALID|DUPLICATE|EXPIRED|DELIVERY_FAILED|REPLACED|UNDER_REVIEW
    description = Column(Text, nullable=True)
    reporter_type = Column(String(16), nullable=False, default='system')  # user|admin|system
    reporter_id = Column(BigInteger, nullable=True)
    admin_id = Column(BigInteger, nullable=True)
    resolution = Column(Text, nullable=True)
    replacement_key_id = Column(Integer, ForeignKey('product_keys.id'), nullable=True)
    replacement_cost = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)


# ─── V24 (Supplier Auto Assignment) ──────────────────────────────────────────

class SupplierProduct(Base):
    """Maps a supplier to a product/variant with auto-assignment configuration.

    The auto-assignment engine consults this table when selecting which
    supplier's keys to fulfil an order from. Lower ``priority`` = selected
    first. When no assignment exists for a product the engine falls back to
    any available key (the pre-V24 behaviour).
    """
    __tablename__ = 'supplier_products'

    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True, index=True)

    # Lower value = higher priority (1 is selected before 10)
    priority = Column(Integer, nullable=False, default=10, index=True)

    # When False this assignment is skipped during auto-selection
    is_auto_assign = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)

    # Optional daily cap (None = unlimited)
    max_daily_qty = Column(Integer, nullable=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = relationship("Supplier", back_populates="product_assignments")
    product = relationship("Product")
    variant = relationship("ProductVariant")

    __table_args__ = (
        UniqueConstraint(
            'supplier_id', 'product_id', 'variant_id',
            name='uq_supplier_product_variant',
        ),
    )


class ResellerTier(Base):
    __tablename__ = 'reseller_tiers'
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    display_order = Column(Integer, default=0)
    min_qualification_spend = Column(Float, nullable=True)
    discount_pct = Column(Float, nullable=False, default=0.0)      # 0-100
    min_quantity = Column(Integer, nullable=False, default=1)
    points_multiplier = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserReseller(Base):
    """Assignment link — a user's current reseller tier."""
    __tablename__ = 'user_reseller'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True, index=True)
    tier_id = Column(Integer, ForeignKey('reseller_tiers.id'), nullable=False, index=True)
    assigned_by = Column(BigInteger, nullable=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)


class DeliveryJob(Base):
    __tablename__ = 'delivery_jobs'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    status = Column(String(24), nullable=False, default='PENDING', index=True)  # PENDING|PROCESSING|DELIVERED|RETRY_SCHEDULED|FAILED|CANCELLED
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    last_error_category = Column(String(48), nullable=True)
    last_error_summary = Column(String(500), nullable=True)
    inventory_assigned = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BackupRecord(Base):
    __tablename__ = 'backup_records'
    id = Column(Integer, primary_key=True)
    filename = Column(String(255), nullable=False)
    method = Column(String(32), nullable=False, default='pg_dump')
    status = Column(String(16), nullable=False, default='RUNNING', index=True)  # RUNNING|SUCCESS|FAILED
    size_bytes = Column(BigInteger, nullable=True)
    error_summary = Column(String(500), nullable=True)
    triggered_by = Column(String(16), nullable=False, default='schedule')  # schedule|manual
    admin_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)


class IntegrityScan(Base):
    __tablename__ = 'integrity_scans'
    id = Column(Integer, primary_key=True)
    triggered_by = Column(String(16), nullable=False, default='manual')
    admin_id = Column(BigInteger, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    total_checks = Column(Integer, default=0)
    total_issues = Column(Integer, default=0)
    critical_count = Column(Integer, default=0)
    warning_count = Column(Integer, default=0)
    info_count = Column(Integer, default=0)

    results = relationship("IntegrityScanResult", back_populates="scan",
                           cascade="all, delete-orphan")


class IntegrityScanResult(Base):
    __tablename__ = 'integrity_scan_results'
    id = Column(Integer, primary_key=True)
    scan_id = Column(Integer, ForeignKey('integrity_scans.id'), nullable=False, index=True)
    check_name = Column(String(80), nullable=False, index=True)
    severity = Column(String(16), nullable=False)  # INFO|WARNING|CRITICAL
    count = Column(Integer, default=0)
    explanation = Column(Text, nullable=True)
    sample_ids = Column(Text, nullable=True)  # JSON list of up to N ids

    scan = relationship("IntegrityScan", back_populates="results")


# ══════════════════════════════════════════════════════════════════════════
# V11: Product Types 360 — supporting tables for the 10 new product types.
# Fully additive. Every FK is nullable so old rows stay valid.
# Enum-like columns use VARCHAR (validated in Python) for painless migration.
# ══════════════════════════════════════════════════════════════════════════

class SubscriptionPlan(Base):
    """A named plan of a SUBSCRIPTION-type product (e.g. '1 Month')."""
    __tablename__ = 'subscription_plans'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    duration_days = Column(Integer, nullable=False, default=30)
    price = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    delivery_type = Column(String(24), nullable=True)   # key|link|manual|auto
    renewal_instructions = Column(Text, nullable=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Subscription(Base):
    """Concrete subscription assigned to a user after purchase.

    V13 (Recurring Billing): a subscription now also tracks its own
    auto-renewal cycle so ``services/subscription_service.py`` can bill the
    user's wallet automatically and send renewal reminders, independent of
    the one-shot ``starts_at``/``expires_at`` window used by the original
    V11 delivery flow. All new columns are nullable / have safe defaults so
    existing rows keep working unchanged.
    """
    __tablename__ = 'subscriptions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    starts_at = Column(DateTime, default=datetime.utcnow, nullable=False)  # = start_date
    expires_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(16), default='active', nullable=False, index=True)
    # active|past_due|cancelled|expired
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── V13: Recurring billing ───────────────────────────────────────────
    next_billing_date = Column(DateTime, nullable=True, index=True)
    billing_cycle_days = Column(Integer, nullable=True)       # e.g. 30 for monthly
    billing_amount = Column(Float, nullable=True)             # USD charged per cycle
    auto_renew = Column(Boolean, default=True, nullable=False)
    failed_attempts = Column(Integer, default=0, nullable=False)
    last_billed_at = Column(DateTime, nullable=True)
    last_reminder_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_by = Column(Integer, nullable=True)  # admin telegram_id, if force-cancelled
    cancel_reason = Column(String(255), nullable=True)


class UserFavorite(Base):
    """Products a user has bookmarked / saved for later — V22 Favorites.

    Separate from UserWishlist (which drives price-drop alerts).
    A UniqueConstraint on (user_id, product_id) prevents duplicates.
    ON DELETE CASCADE ensures rows are removed if the user or product is deleted.
    """
    __tablename__ = 'user_favorites'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_user_favorite'),
    )

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    note       = Column(String(255), nullable=True)   # reserved for future use

    user    = relationship("User")
    product = relationship("Product")


class ProductCompare(Base):
    """Per-user product compare list (V22).

    Stores up to N products a user wants to compare side-by-side.
    A UniqueConstraint prevents adding the same product twice.
    """
    __tablename__ = 'product_comparisons'
    __table_args__ = (
        UniqueConstraint('user_telegram_id', 'product_id',
                         name='uq_compare_user_product'),
    )

    id               = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    product_id       = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                              nullable=False)
    added_at         = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProductCompareLog(Base):
    """Records each comparison-page view for admin statistics (V22).

    ``purchased_from_compare`` is set to True when the user taps a Buy button
    directly from the comparison page.
    """
    __tablename__ = 'product_compare_logs'

    id                     = Column(Integer, primary_key=True)
    user_telegram_id       = Column(BigInteger, nullable=False, index=True)
    product_ids_json       = Column(Text, nullable=False)   # JSON list of product IDs
    product_count          = Column(Integer, nullable=False, default=0)
    purchased_from_compare = Column(Boolean, default=False, nullable=False)
    purchased_product_id   = Column(Integer, nullable=True)
    viewed_at              = Column(DateTime, default=datetime.utcnow, nullable=False)


class SubscriptionReminderLog(Base):
    """Tracks which expiry-reminder intervals have been sent for each subscription.

    V22 (Subscription Reminder): one row per (subscription_id, interval_days).
    interval_days values: 30, 15, 7, 3, 1 = days before expiry; 0 = expired notice.
    A UniqueConstraint prevents sending the same interval twice per subscription.
    """
    __tablename__ = 'subscription_reminder_logs'
    __table_args__ = (
        UniqueConstraint('subscription_id', 'interval_days',
                         name='uq_sub_reminder_interval'),
    )

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'),
                             nullable=False, index=True)
    interval_days = Column(Integer, nullable=False)  # 30/15/7/3/1/0
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    success = Column(Boolean, default=True, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)


class BundleItem(Base):
    """Child inventory contained inside a BUNDLE-type parent product."""
    __tablename__ = 'bundle_items'

    id = Column(Integer, primary_key=True)
    parent_product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    child_product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Preorder(Base):
    """Pending PREORDER-type order awaiting admin fulfilment."""
    __tablename__ = 'preorders'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    status = Column(String(24), default='pending', nullable=False, index=True)  # pending|processing|delivered|cancelled
    estimated_delivery = Column(String(255), nullable=True)
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ServiceOrder(Base):
    """SERVICE-type order — carries the customer-collected info."""
    __tablename__ = 'service_orders'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    submitted_fields = Column(Text, nullable=True)   # JSON dict
    status = Column(String(24), default='pending', nullable=False, index=True)  # pending|processing|completed|cancelled
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ManualDeliveryTask(Base):
    """Row in the MANUAL_DELIVERY queue — admin picks it up, sends assets."""
    __tablename__ = 'manual_delivery_tasks'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    status = Column(String(24), default='pending', nullable=False, index=True)  # pending|processing|delivered|cancelled
    admin_note = Column(Text, nullable=True)
    delivery_payload = Column(Text, nullable=True)  # what admin sent
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExternalIntegration(Base):
    """Reusable external delivery integration (referenced by EXTERNAL_DELIVERY products)."""
    __tablename__ = 'external_integrations'

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    endpoint_url = Column(String(500), nullable=False)
    http_method = Column(String(8), default='POST', nullable=False)
    auth_type = Column(String(24), default='none', nullable=False)  # none|bearer|header|basic
    # SECURITY: never store raw secrets — this is the *env-var name* holding the secret.
    credential_env_name = Column(String(80), nullable=True)
    timeout_seconds = Column(Integer, default=30, nullable=False)
    max_retries = Column(Integer, default=2, nullable=False)
    request_template = Column(Text, nullable=True)   # JSON — request body template
    response_mapping = Column(Text, nullable=True)   # JSON — how to extract delivery
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GeneratedValue(Base):
    """Unique server-generated value for AUTO_GENERATED products (audit + duplicate-prevention)."""
    __tablename__ = 'generated_values'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    value = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExternalDeliveryLog(Base):
    """Idempotent log of external-delivery attempts (prevents double-charge)."""
    __tablename__ = 'external_delivery_logs'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    integration_id = Column(Integer, ForeignKey('external_integrations.id'), nullable=True)
    idempotency_key = Column(String(120), nullable=False, unique=True, index=True)
    attempt = Column(Integer, default=1, nullable=False)
    status = Column(String(24), default='pending', nullable=False, index=True)  # pending|success|failed
    http_status = Column(Integer, nullable=True)
    response_summary = Column(Text, nullable=True)  # truncated
    delivered_value = Column(Text, nullable=True)
    error_summary = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)


class PaymentIdempotency(Base):
    """Section 16 — one row per processed payment reference.

    A UNIQUE(source, external_ref) index makes duplicate processing
    a hard integrity error the caller can safely catch.
    """
    __tablename__ = 'payment_idempotency'
    __table_args__ = (
        UniqueConstraint('source', 'external_ref', name='uq_payment_idem_src_ref'),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False, index=True)  # wallet|manual|tg|crypto|...
    external_ref = Column(String(180), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── V13 (Multi-Admin RBAC + 2FA) ────────────────────────────────────
class AdminRoleType(str, enum.Enum):
    """The three admin tiers. Stored as plain strings so raw SQL/CSV stays readable."""
    SUPER_ADMIN = "super_admin"
    MODERATOR = "moderator"
    SUPPORT_STAFF = "support_staff"


# Default permission grants per role. Applied when an AdminRole row is
# created without explicit overrides. super_admin always resolves to "all
# permissions, always" in code (see utils/permissions.py) regardless of
# these flags, so its row's booleans are cosmetic/display-only.
ROLE_DEFAULT_PERMISSIONS = {
    AdminRoleType.SUPER_ADMIN: dict(
        manage_products=True, manage_orders=True, manage_users=True,
        manage_broadcasts=True, manage_payments=True, view_analytics=True,
        manage_settings=True, manage_admins=True,
    ),
    AdminRoleType.MODERATOR: dict(
        manage_products=True, manage_orders=True, manage_users=True,
        manage_broadcasts=True, manage_payments=False, view_analytics=True,
        manage_settings=False, manage_admins=False,
    ),
    AdminRoleType.SUPPORT_STAFF: dict(
        manage_products=False, manage_orders=True, manage_users=False,
        manage_broadcasts=False, manage_payments=False, view_analytics=False,
        manage_settings=False, manage_admins=False,
    ),
}


class AdminRole(Base):
    """One row per admin staff member: their tier + granular permission flags.

    The bootstrap owner (``settings.ADMIN_TELEGRAM_ID``) is always treated as
    an implicit, unremovable SUPER_ADMIN even before any row exists for them
    (see ``utils/permissions.py:get_admin``) — this prevents ever locking the
    store owner out of their own bot.

    2FA fields implement a Telegram-native OTP: the bot itself DMs the admin
    a short-lived numeric code (nothing is ever emailed/SMS'd), the admin
    types it back, and a verified session is remembered for
    ``SESSION_TTL_HOURS`` so they aren't re-prompted on every tap.
    """
    __tablename__ = 'admin_roles'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(64), nullable=True)
    role = Column(Enum(AdminRoleType), nullable=False, default=AdminRoleType.SUPPORT_STAFF)

    # ── Granular permission flags ──
    manage_products = Column(Boolean, default=False, nullable=False)
    manage_orders = Column(Boolean, default=False, nullable=False)
    manage_users = Column(Boolean, default=False, nullable=False)
    manage_broadcasts = Column(Boolean, default=False, nullable=False)
    manage_payments = Column(Boolean, default=False, nullable=False)
    view_analytics = Column(Boolean, default=False, nullable=False)
    manage_settings = Column(Boolean, default=False, nullable=False)
    manage_admins = Column(Boolean, default=False, nullable=False)  # add/remove/promote other admins

    is_active = Column(Boolean, default=True, nullable=False)  # soft-disable without deleting history
    added_by = Column(BigInteger, nullable=True)  # telegram_id of the super_admin who added them
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── OTP / 2FA state (single active code per admin) ──
    otp_code_hash = Column(String(128), nullable=True)   # sha256 hex, never plaintext
    otp_expires_at = Column(DateTime, nullable=True)
    otp_attempts = Column(Integer, default=0, nullable=False)
    otp_last_sent_at = Column(DateTime, nullable=True)    # for resend cooldown

    session_verified_until = Column(DateTime, nullable=True)  # 2FA session valid until
    last_login_at = Column(DateTime, nullable=True)


# ─── V14: Marketing Automation (abandoned cart + win-back) ────────────────
class MarketingCampaignType(enum.Enum):
    """The four automated touches ``services/marketing_automation.py`` sends."""
    CART_30M = "cart_30m"        # cart untouched for 30 minutes
    CART_24H = "cart_24h"        # cart still untouched 24 hours later (escalation)
    WINBACK_7D = "winback_7d"    # no activity for 7 days
    WINBACK_30D = "winback_30d"  # no activity for 30 days (bigger offer)


class MarketingTouch(Base):
    """Dedup ledger for every automated marketing message actually sent.

    One row per (user, campaign_type, reference_at). ``reference_at`` anchors
    the row to the specific moment the campaign was computed from — the
    cart's last-updated timestamp for cart reminders, or the user's
    last-activity timestamp for win-back offers. Because it's part of the
    unique key, a user is only ever touched once per distinct moment: if
    they add to cart again (new "reference_at") or come back and go quiet
    again later, they naturally become eligible again without any extra
    bookkeeping, and a periodic job re-run can never double-send for the
    same moment.
    """
    __tablename__ = 'marketing_touches'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    campaign_type = Column(Enum(MarketingCampaignType), nullable=False, index=True)
    reference_at = Column(DateTime, nullable=False)
    coupon_code = Column(String(64), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint('user_id', 'campaign_type', 'reference_at',
                         name='uq_marketing_touch_dedup'),
    )


# ─── V15: Flash Sales ──────────────────────────────────────────────────────
class FlashSale(Base):
    """Time-boxed % discount on a single product OR an entire category.

    Exactly one of ``product_id`` / ``category_id`` should be set by the
    admin panel (``handlers/admin_promotions.py``). When both a product-level
    and a category-level sale could apply to the same product,
    ``services/pricing.py:get_active_flash_sale`` prefers the product-level
    one. ``is_active`` is the admin "cancel" switch — cancelling a sale never
    deletes the row, so past sales stay visible in the admin history.
    """
    __tablename__ = 'flash_sales'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=True, index=True)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True, index=True)
    discount_percent = Column(Float, nullable=False)   # 0..100
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)  # admin cancel switch
    label = Column(String(120), nullable=True)   # optional banner text, e.g. "🔥 Weekend Flash Sale"
    created_by = Column(BigInteger, nullable=True)   # admin telegram_id
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product")
    category = relationship("Category")

    def is_live(self, now: datetime = None) -> bool:
        """True only while the sale is cancelled-free, started, and not yet ended."""
        now = now or datetime.utcnow()
        return bool(self.is_active and self.start_time <= now < self.end_time)


# ─── Admin-approval queue for failed auto-verifications ───────────────────────


class VerificationAttemptLog(Base):
    """Persistent audit log for every TXID verification attempt.

    Written for every call into binance_txid_received / bybit_txid_received,
    regardless of outcome — used by the admin panel security view and for
    post-incident analysis.  Never contains secret credentials.
    """
    __tablename__ = "verification_attempt_log"

    id = Column(Integer, primary_key=True)
    gateway = Column(String(32), nullable=False, index=True)         # "binance_pay" | "bybit_pay"
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    internal_order_id = Column(Integer, nullable=False, index=True)
    submitted_txid = Column(String(256), nullable=False)
    outcome = Column(String(64), nullable=False)                      # VerificationOutcome.*
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PendingManualVerification(Base):
    """A failed auto-verification queued for admin review.

    When the Binance / Bybit / ZiniPay (bKash, Nagad, Rocket) API cannot
    confirm a TXID automatically (e.g. API error, transaction not yet
    visible, amount mismatch), a row is created here and the admin receives
    a notification with Approve / Reject buttons.

    On admin Approve  → wallet is credited and the internal order COMPLETED.
    On admin Reject   → user is notified, order stays pending (user can retry).

    A UNIQUE constraint on (gateway, internal_order_id, submitted_txid) ensures
    the same failed submission is never queued twice for the same order.
    """
    __tablename__ = "pending_manual_verifications"
    __table_args__ = (
        UniqueConstraint(
            "gateway", "internal_order_id", "submitted_txid",
            name="uq_pmv_gateway_order_txid",
        ),
    )

    id = Column(Integer, primary_key=True)
    gateway = Column(String(32), nullable=False, index=True)         # "binance_pay" | "bybit_pay"
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    internal_order_id = Column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True
    )
    submitted_txid = Column(String(256), nullable=False)
    amount = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(16), nullable=False)
    payment_type = Column(String(32), nullable=True)   # uid_transfer | onchain (Bybit only)
    network = Column(String(16), nullable=True)         # TRC20/BEP20/ERC20 (Bybit onchain)
    auto_outcome = Column(String(64), nullable=True)    # what the API returned
    auto_detail = Column(Text, nullable=True)           # human-readable reason
    # "pending" | "approved" | "rejected"
    status = Column(String(16), nullable=False, default="pending", index=True)
    admin_note = Column(Text, nullable=True)         # full resolution note including admin identity
    admin_telegram_id = Column(BigInteger, nullable=True, index=True)  # Telegram ID of resolving admin
    reject_reason = Column(Text, nullable=True)      # user-visible rejection reason (if rejected)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# V18 — User Features (Wishlist, Price Alerts, Recently Viewed,
#        Quick Buy, Preferred Payment, Buy Again)
# ═══════════════════════════════════════════════════════════════════════

class UserWishlist(Base):
    """Products a user has saved for later purchase."""
    __tablename__ = 'user_wishlists'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_wishlist_user_product'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    product = relationship("Product")


class PriceDropAlert(Base):
    """User subscription for price-drop notifications on a product."""
    __tablename__ = 'price_drop_alerts'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_pda_user_product'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    subscribed_at = Column(DateTime, default=datetime.utcnow)
    last_notified_price = Column(Float, nullable=True)

    user = relationship("User")
    product = relationship("Product")


class RecentlyViewed(Base):
    """Recently viewed products per user (upserted on each view)."""
    __tablename__ = 'recently_viewed'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_rv_user_product'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    viewed_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    product = relationship("Product")


class QuickBuyConfig(Base):
    """Remembered payment method + quantity per (user, product) for one-click checkout."""
    __tablename__ = 'quick_buy_configs'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_qbc_user_product'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    payment_method = Column(String(64), nullable=True)
    quantity = Column(Integer, default=1)
    last_used_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    product = relationship("Product")


class PreferredPayment(Base):
    """User's explicitly-chosen preferred payment method (one row per user)."""
    __tablename__ = 'preferred_payments'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True, index=True)
    payment_method = Column(String(64), nullable=False)
    set_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


# ═══════════════════════════════════════════════════════════════════════
# V19 — Account & Order Features (Receipt, Downloads, Activity, Security)
# ═══════════════════════════════════════════════════════════════════════

class OrderReceipt(Base):
    """Auto-generated receipt record for completed orders and wallet deposits.

    One receipt per order (UNIQUE on order_id) prevents duplicates.
    Deposit receipts reference a Transaction row instead.
    """
    __tablename__ = 'order_receipts'
    __table_args__ = (
        UniqueConstraint('order_id', name='uq_receipt_order_id'),
    )

    id = Column(Integer, primary_key=True)
    receipt_number = Column(String(32), unique=True, nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    # 'purchase' | 'deposit'
    receipt_type = Column(String(16), nullable=False, default='purchase')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")
    order = relationship("Order")
    transaction = relationship("Transaction")


class UserDownload(Base):
    """Download Center — tracks every delivered asset so users can re-access it.

    One row per (user_id, order_item_id).  The actual content lives in
    OrderItem.delivered_asset — this record stores metadata + counters.
    """
    __tablename__ = 'user_downloads'
    __table_args__ = (
        UniqueConstraint('user_id', 'order_item_id', name='uq_download_user_item'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    order_item_id = Column(Integer, ForeignKey('order_items.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    product_name = Column(String(255), nullable=False)
    # key | file | account | redeem_link | code | subscription | voucher | other
    asset_type = Column(String(32), nullable=False, default='key')
    download_count = Column(Integer, default=0)
    last_downloaded_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")
    order = relationship("Order")
    order_item = relationship("OrderItem")
    product = relationship("Product")


class ActivityLog(Base):
    """User activity audit log for Activity History feature.

    Actions: login | deposit | purchase | refund | coupon_used |
             referral_bonus | wallet_credit | wallet_debit |
             profile_changed | ticket_opened | ticket_closed |
             ticket_replied | download | order_viewed
    """
    __tablename__ = 'activity_logs'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    # success | failed | pending
    status = Column(String(16), nullable=False, default='success')
    details = Column(Text, nullable=True)
    ref_type = Column(String(32), nullable=True)   # order | transaction | ticket | coupon
    ref_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")


# ─── Part 3: Gift Cards ──────────────────────────────────────────────────────

class GiftCardType(str, enum.Enum):
    FIXED   = "fixed"    # fixed USD amount credited to wallet
    PERCENT = "percent"  # percentage discount converted to one-time coupon
    CUSTOM  = "custom"   # custom USD amount (same as FIXED but different UX label)


class GiftCard(Base):
    """Admin-issued redeemable gift card."""
    __tablename__ = "gift_cards"

    id            = Column(Integer, primary_key=True)
    code          = Column(String(64), nullable=False, unique=True, index=True)
    label         = Column(String(120), nullable=True)
    card_type     = Column(Enum(GiftCardType),
                           nullable=False, default=GiftCardType.FIXED,
                           server_default=GiftCardType.FIXED.value)
    value         = Column(Float, nullable=False)
    expires_at    = Column(DateTime, nullable=True, index=True)
    max_uses      = Column(Integer, nullable=False, default=0)       # 0 = unlimited
    used_count    = Column(Integer, nullable=False, default=0)
    is_single_use = Column(Boolean, nullable=False, default=False)
    is_active     = Column(Boolean, nullable=False, default=True, index=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    created_by    = Column(BigInteger, nullable=True)  # admin Telegram ID

    redemptions = relationship("GiftCardRedemption", back_populates="card",
                               cascade="all, delete-orphan")


class GiftCardRedemption(Base):
    """Records each user's redemption of a gift card (enforces one-per-user)."""
    __tablename__ = "gift_card_redemptions"

    id          = Column(Integer, primary_key=True)
    card_id     = Column(Integer, ForeignKey("gift_cards.id"),  nullable=False, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"),        nullable=False, index=True)
    redeemed_at = Column(DateTime, default=datetime.utcnow)

    card = relationship("GiftCard", back_populates="redemptions")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("card_id", "user_id", name="uq_gcr_card_user"),
    )


# ─── Part 3: Gift Purchases ───────────────────────────────────────────────────

class GiftPurchaseStatus(str, enum.Enum):
    PENDING       = "pending"        # order exists, notification not sent yet
    NOTIFIED      = "notified"       # recipient successfully notified
    UNDELIVERABLE = "undeliverable"  # recipient could not be reached


class GiftPurchase(Base):
    """Tracks gift purchases — buyer sends a product to a recipient."""
    __tablename__ = "gift_purchases"

    id                    = Column(Integer, primary_key=True)
    order_id              = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    sender_user_id        = Column(Integer, ForeignKey("users.id"),  nullable=False, index=True)
    recipient_telegram_id = Column(BigInteger, nullable=True)
    recipient_username    = Column(String(120), nullable=True)
    product_id            = Column(Integer, ForeignKey("products.id"), nullable=True)
    gift_message          = Column(Text, nullable=True)
    is_anonymous          = Column(Boolean, nullable=False, default=False)
    status      = Column(String(20), nullable=False, default=GiftPurchaseStatus.PENDING.value,
                         server_default=GiftPurchaseStatus.PENDING.value, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)
    notified_at = Column(DateTime, nullable=True)

    sender  = relationship("User",    foreign_keys=[sender_user_id])
    order   = relationship("Order",   foreign_keys=[order_id])
    product = relationship("Product", foreign_keys=[product_id])


class UserSession(Base):
    """User session tracking for Security Center.

    A new session is created on /start (or first-ever interaction after a
    long gap). Only one active session per user at a time in this model —
    terminating marks is_active=False so it shows up in history.
    """
    __tablename__ = 'user_sessions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    session_token = Column(String(64), unique=True, nullable=False, index=True)
    device_info = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_active_at = Column(DateTime, default=datetime.utcnow, index=True)
    terminated_at = Column(DateTime, nullable=True)

    user = relationship("User")


# ─── V20: Advanced Referral Dashboard ────────────────────────────────────────

class ReferralClick(Base):
    """Track referral link clicks for analytics (V20)."""
    __tablename__ = 'referral_clicks'

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    clicked_at = Column(DateTime, default=datetime.utcnow, index=True)
    ip_hash = Column(String(64), nullable=True)

    referrer = relationship("User", foreign_keys=[referrer_id])


class ReferralCommission(Base):
    """Per-purchase commission earned by referrers (V20).

    Status flow: pending → available → withdrawn
    """
    __tablename__ = 'referral_commissions'

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    referred_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    order_amount = Column(Float, nullable=False, default=0.0)
    commission_rate = Column(Float, nullable=False, default=0.0)
    commission_amount = Column(Float, nullable=False, default=0.0)
    status = Column(String(16), nullable=False, default='pending', index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    cleared_at = Column(DateTime, nullable=True)

    referrer = relationship("User", foreign_keys=[referrer_id])
    referred = relationship("User", foreign_keys=[referred_id])
    order = relationship("Order", foreign_keys=[order_id])


class ReferralWithdrawal(Base):
    """Referral commission withdrawal requests (V20 + V29 Approval System).

    Statuses (V29 extended):
      pending → under_review → approved → processing → completed
                                        ↘ rejected
                ↘ cancelled / expired  (any active status)
    """
    __tablename__ = 'referral_withdrawals'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount = Column(Float, nullable=False, default=0.0)
    status = Column(String(16), nullable=False, default='pending', index=True)
    admin_note = Column(Text, nullable=True)          # legacy; use notes for V29
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)

    # ── V29: Withdrawal Approval System ──────────────────────────────────────
    payment_method  = Column(String(32), nullable=True)   # usdt_trc20, binance_pay, …
    wallet_address  = Column(Text, nullable=True)          # wallet addr / Pay ID / bank info
    currency        = Column(String(16), nullable=True, default='USD')
    admin_tg_id     = Column(BigInteger, nullable=True)    # Telegram ID of acting admin
    approval_time   = Column(DateTime, nullable=True)
    completion_time = Column(DateTime, nullable=True)
    reason          = Column(Text, nullable=True)          # rejection / cancellation reason
    notes           = Column(Text, nullable=True)          # internal admin notes
    logs_json       = Column(Text, nullable=True)          # JSON array of status-change log entries

    user = relationship("User", foreign_keys=[user_id])


# ─── V20: Announcement System ─────────────────────────────────────────────────

class Announcement(Base):
    """Admin-created announcements broadcast to users (V20)."""
    __tablename__ = 'announcements'

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    target = Column(String(32), nullable=False, default='all')
    target_user_ids = Column(Text, nullable=True)   # JSON list of Telegram IDs

    is_active = Column(Boolean, default=True, nullable=False, index=True)
    is_pinned = Column(Boolean, default=False, nullable=False, index=True)
    is_scheduled = Column(Boolean, default=False, nullable=False)
    scheduled_at = Column(DateTime, nullable=True, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)

    sent_count = Column(Integer, default=0)
    is_sent = Column(Boolean, default=False, nullable=False)
    sent_at = Column(DateTime, nullable=True)

    # popup (DM) | banner (homepage notice) | silent (internal only)
    announcement_type = Column(String(16), nullable=False, default='popup')

    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reads = relationship("AnnouncementRead", back_populates="announcement",
                         cascade="all, delete-orphan")


class AnnouncementRead(Base):
    """Track which users have acknowledged which announcements (V20)."""
    __tablename__ = 'announcement_reads'
    __table_args__ = (
        UniqueConstraint('announcement_id', 'user_id', name='uq_annread_ann_user'),
    )

    id = Column(Integer, primary_key=True)
    announcement_id = Column(Integer, ForeignKey('announcements.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    read_at = Column(DateTime, default=datetime.utcnow)

    announcement = relationship("Announcement", back_populates="reads")
    user = relationship("User", foreign_keys=[user_id])


# ─── V21 / V26: Scheduled Broadcast ──────────────────────────────────────

class BroadcastStatus(str, enum.Enum):
    DRAFT      = "draft"
    SCHEDULED  = "scheduled"
    SENDING    = "sending"
    SENT       = "sent"
    CANCELLED  = "cancelled"
    # V26 additions
    PAUSED     = "paused"
    FAILED     = "failed"


class ScheduledBroadcast(Base):
    """Full-featured scheduled/recurring broadcast with delivery stats (V21/V26)."""
    __tablename__ = 'scheduled_broadcasts'

    id              = Column(Integer, primary_key=True)
    title           = Column(String(100), nullable=False)
    message_text    = Column(Text, nullable=True)
    # V26: extended media types — text|photo|video|document|animation|voice|audio|sticker|poll
    media_type      = Column(String(16), nullable=False, default='text')
    file_id         = Column(String(256), nullable=True)
    # V26: extended targets — all|buyers|non_buyers|wallet_users|premium|no_balance|
    #                         no_orders|new_users|inactive|referred|specific_ids|specific_language
    target_segment  = Column(String(32), nullable=False, default='all')
    # V26: extra targeting data
    target_user_ids = Column(Text, nullable=True)    # JSON list of telegram IDs
    target_language = Column(String(8), nullable=True)  # language code
    # draft|scheduled|sending|sent|cancelled|paused|failed
    status          = Column(String(16), nullable=False, default='draft', index=True)
    scheduled_at    = Column(DateTime, nullable=True, index=True)
    sent_at         = Column(DateTime, nullable=True)
    started_at      = Column(DateTime, nullable=True)
    finished_at     = Column(DateTime, nullable=True)
    is_recurring    = Column(Boolean, default=False)
    recurrence_type = Column(String(16), nullable=True)   # daily|weekly|monthly
    # V26: pause support
    is_paused       = Column(Boolean, default=False)
    next_run_at     = Column(DateTime, nullable=True, index=True)
    # V26: message formatting
    parse_mode      = Column(String(16), nullable=False, default='HTML')
    disable_notification = Column(Boolean, default=False)
    # V26: timezone
    timezone        = Column(String(64), nullable=False, default='UTC')
    button_text     = Column(String(64), nullable=True)
    button_url      = Column(String(512), nullable=True)
    # Delivery statistics
    total_recipients = Column(Integer, default=0)
    sent_count      = Column(Integer, default=0)
    delivered_count = Column(Integer, default=0)
    failed_count    = Column(Integer, default=0)
    blocked_count   = Column(Integer, default=0)
    skipped_count   = Column(Integer, default=0)
    # V26: retry
    retry_count     = Column(Integer, default=0)
    max_retries     = Column(Integer, default=3)
    error_log       = Column(Text, nullable=True)
    created_by      = Column(BigInteger, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Enterprise Broadcast Center (V44)
    custom_interval_hours = Column(Integer, nullable=True)   # hours between runs when recurrence_type="custom"
    media_group_ids       = Column(Text, nullable=True)      # JSON array of file_ids for media-group broadcasts

    # Relationships
    logs        = relationship("BroadcastLog", back_populates="broadcast",
                               cascade="all, delete-orphan")
    retry_queue = relationship("BroadcastRetryQueue", back_populates="broadcast",
                               cascade="all, delete-orphan")


class BroadcastLog(Base):
    """Per-send delivery log for a scheduled broadcast (V26)."""
    __tablename__ = 'broadcast_logs'

    id               = Column(Integer, primary_key=True)
    broadcast_id     = Column(Integer, ForeignKey('scheduled_broadcasts.id', ondelete='CASCADE'),
                              nullable=False, index=True)
    started_at       = Column(DateTime, nullable=True)
    finished_at      = Column(DateTime, nullable=True)
    total_recipients = Column(Integer, nullable=False, default=0)
    sent             = Column(Integer, nullable=False, default=0)
    delivered        = Column(Integer, nullable=False, default=0)
    failed           = Column(Integer, nullable=False, default=0)
    blocked          = Column(Integer, nullable=False, default=0)
    skipped          = Column(Integer, nullable=False, default=0)
    error_log        = Column(Text, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    broadcast = relationship("ScheduledBroadcast", back_populates="logs")


class BroadcastRetryQueue(Base):
    """Queue of failed recipients for a broadcast retry (V26)."""
    __tablename__ = 'broadcast_retry_queue'

    id           = Column(Integer, primary_key=True)
    broadcast_id = Column(Integer, ForeignKey('scheduled_broadcasts.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    telegram_id  = Column(BigInteger, nullable=False, index=True)
    error_msg    = Column(String(512), nullable=True)
    retry_at     = Column(DateTime, nullable=True)
    attempts     = Column(Integer, nullable=False, default=0)
    # pending|sent|failed
    status       = Column(String(16), nullable=False, default='pending', index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    broadcast = relationship("ScheduledBroadcast", back_populates="retry_queue")


# ─── V21: Advanced Coupon extensions ──────────────────────────────────────
# New columns are added via ALTER TABLE in _apply_pending_migrations().
# Model already has: code, discount_type, discount_value, min_order_amount,
# max_uses, used_count, per_user_limit, expires_at, is_active, created_at.
# New: max_discount_amount, activation_date, target_user_id, product_ids,
#      category_ids, coupon_type, free_product_id


# ─── V21: Refund System ───────────────────────────────────────────────────

class RefundStatus(str, enum.Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    PROCESSED = "processed"
    FAILED    = "failed"


class RefundTrigger(str, enum.Enum):
    FAILED_ORDER  = "failed_order"
    CANCELLED     = "cancelled"
    TIMEOUT       = "timeout"
    DUPLICATE     = "duplicate"
    OVERPAYMENT   = "overpayment"
    MANUAL        = "manual"


class Refund(Base):
    """Automatic and manual refund records (V21)."""
    __tablename__ = 'refunds'

    id                  = Column(Integer, primary_key=True)
    order_id            = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id             = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount              = Column(Float, nullable=False)
    reason              = Column(Text, nullable=True)
    refund_type         = Column(String(32), nullable=False, default='wallet')  # wallet|original_method
    status              = Column(String(16), nullable=False, default='pending', index=True)
    trigger             = Column(String(32), nullable=False, default='manual')
    admin_telegram_id   = Column(BigInteger, nullable=True)
    admin_note          = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, index=True)
    processed_at        = Column(DateTime, nullable=True)

    order = relationship("Order", foreign_keys=[order_id])
    user  = relationship("User",  foreign_keys=[user_id])


# ─── V21: Language Configuration ─────────────────────────────────────────

class LanguageConfig(Base):
    """Per-language enable/disable/default settings managed by admin (V21)."""
    __tablename__ = 'language_configs'

    id         = Column(Integer, primary_key=True)
    code       = Column(String(8), unique=True, nullable=False, index=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── V23: Product Price History ───────────────────────────────────────────

class ProductPriceHistory(Base):
    """Records every price change for a product — V23 Price History.

    A record is written when:
    - A product is first created   (old_price=0, reason='initial')
    - An admin edits the price     (old/new tracked, reason optional)

    Duplicate prevention (price unchanged) is enforced at the service layer.
    ON DELETE CASCADE removes records when the product is deleted.
    """
    __tablename__ = 'product_price_history'

    id                     = Column(Integer, primary_key=True)
    product_id             = Column(Integer,
                                    ForeignKey('products.id', ondelete='CASCADE'),
                                    nullable=False, index=True)
    old_price              = Column(Float, nullable=False, default=0.0)
    new_price              = Column(Float, nullable=False)
    difference             = Column(Float, nullable=False, default=0.0)
    pct_change             = Column(Float, nullable=True)   # NULL when old_price is 0
    changed_by_telegram_id = Column(BigInteger, nullable=True, index=True)
    changed_by_name        = Column(String(128), nullable=True)
    reason                 = Column(String(255), nullable=True)
    changed_at             = Column(DateTime, default=datetime.utcnow,
                                   nullable=False, index=True)

    product = relationship("Product")


# ─── V25: Product FAQ System ──────────────────────────────────────────────

class ProductFAQ(Base):
    """Per-product Frequently Asked Questions — V25.

    Each FAQ belongs to exactly one product and carries a category label,
    sort order, and active flag. Duplicate-question prevention is enforced
    at the service layer (``services/product_faq.py``).

    ON DELETE CASCADE removes all FAQs when the parent product is deleted.
    """
    __tablename__ = "product_faqs"

    id         = Column(Integer, primary_key=True)
    product_id = Column(Integer,
                        ForeignKey("products.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    question   = Column(Text, nullable=False)
    answer     = Column(Text, nullable=False)
    # general | payment | delivery | account | warranty | troubleshooting | custom
    category   = Column(String(32), nullable=False, default="general")
    sort_order = Column(Integer, nullable=False, default=0, index=True)
    is_active  = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product")


# ─── V27: Webhook Monitor & API Health ────────────────────────────────────

class ApiHealthLog(Base):
    """Per-check API health snapshot (V27).

    Each background health-check run writes one row per service.
    status: 'online' | 'slow' | 'warning' | 'offline'
    """
    __tablename__ = 'api_health_log'

    id               = Column(Integer, primary_key=True)
    service_name     = Column(String(64), nullable=False, index=True)
    # online | slow | warning | offline
    status           = Column(String(16), nullable=False, index=True)
    response_time_ms = Column(Integer, nullable=True)
    error_message    = Column(String(512), nullable=True)
    http_status      = Column(Integer, nullable=True)
    checked_at       = Column(DateTime, nullable=False, index=True)


class WebhookLog(Base):
    """Every inbound webhook event from any payment provider (V27).

    ``webhook_uuid`` is provider-supplied or deterministically derived so the
    same event is never recorded twice (duplicate-suppression guard).
    status: 'received' | 'processed' | 'failed' | 'duplicate' | 'ignored'
    """
    __tablename__ = 'webhook_log'

    id                 = Column(Integer, primary_key=True)
    webhook_uuid       = Column(String(128), nullable=False, unique=True, index=True)
    provider           = Column(String(32),  nullable=False, index=True)
    # nowpayments | binance | bybit | heleket | trc20 | bep20 | erc20 | mobile | telegram
    received_at        = Column(DateTime, nullable=False, index=True)
    processing_time_ms = Column(Integer, nullable=True)
    # received | processed | failed | duplicate | ignored
    status             = Column(String(16), nullable=False, index=True)
    error_message      = Column(Text, nullable=True)
    retry_count        = Column(Integer, nullable=False, default=0)
    order_id           = Column(Integer, nullable=True, index=True)
    user_id            = Column(Integer, nullable=True, index=True)
    payment_id         = Column(String(128), nullable=True)
    transaction_id     = Column(String(128), nullable=True)
    raw_payload        = Column(Text, nullable=True)

    retry_queue = relationship("WebhookRetryQueue", back_populates="webhook_log",
                               cascade="all, delete-orphan")


class WebhookRetryQueue(Base):
    """Retry queue for failed webhook events (V27).

    status: 'pending' | 'processing' | 'success' | 'failed' | 'abandoned'
    """
    __tablename__ = 'webhook_retry_queue'

    id             = Column(Integer, primary_key=True)
    webhook_log_id = Column(Integer, ForeignKey('webhook_log.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    provider       = Column(String(32),  nullable=False)
    payload        = Column(Text, nullable=True)
    retry_at       = Column(DateTime, nullable=True, index=True)
    attempts       = Column(Integer, nullable=False, default=0)
    # pending | processing | success | failed | abandoned
    status         = Column(String(16), nullable=False, default='pending', index=True)
    last_error     = Column(String(512), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    webhook_log = relationship("WebhookLog", back_populates="retry_queue")


# ─── V28: Product Clone & Template System ─────────────────────────────────

class ProductTemplate(Base):
    """A reusable product blueprint created by admins (V28 + V46).

    ``template_data`` is a JSON blob containing all cloneable product fields,
    variant structures, and FAQ entries — see handlers/admin_product_clone.py
    ``_snapshot_product`` for the exact schema.  Keys / inventory rows are
    never stored here.

    V46 adds per-template metadata columns for the new Enterprise Product
    Template System (apt:* handler) so templates can be filtered, sorted,
    defaulted, archived, and queried without parsing the full JSON blob.
    All V46 columns are nullable / have defaults so the V28 pct:* system
    continues to work without any changes.
    """
    __tablename__ = "product_templates"

    # ── V28 original columns (unchanged) ──────────────────────────────────────
    id            = Column(Integer, primary_key=True)
    name          = Column(String(120), nullable=False, index=True)
    description   = Column(String(512), nullable=True)
    template_data = Column(Text, nullable=False)    # JSON blob — pct:* schema
    use_count     = Column(Integer, nullable=False, default=0)
    created_by    = Column(BigInteger, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── V46 Enterprise Product Template System columns ─────────────────────────
    # ProductType enum *name* (KEY, REDEEM_LINK, …) — VARCHAR so no SQL enum sync
    template_type      = Column(String(32), nullable=True, index=True)
    delivery_method    = Column(String(32), nullable=True)
    is_default         = Column(Boolean, nullable=False, default=False)
    is_archived        = Column(Boolean, nullable=False, default=False)
    tags_json          = Column(Text, nullable=True)          # JSON list of strings
    last_used_at       = Column(DateTime, nullable=True)
    products_created   = Column(Integer, nullable=False, default=0)
    default_price      = Column(Float, nullable=True)
    currency_code      = Column(String(10), nullable=False, default="USD")
    visibility         = Column(String(16), nullable=False, default="public")  # public|hidden
    auto_delivery      = Column(Boolean, nullable=False, default=True)
    manual_review      = Column(Boolean, nullable=False, default=False)
    refund_policy      = Column(Text, nullable=True)
    replacement_policy = Column(Text, nullable=True)
    warranty_info      = Column(Text, nullable=True)
    product_image      = Column(String(256), nullable=True)   # Telegram file_id or URL
    custom_fields_json = Column(Text, nullable=True)          # JSON object of type-specific defaults


class ProductCloneLog(Base):
    """Audit row written for every product clone or template-materialisation (V28).

    ``source_product_id`` is NULL when a product was created purely from a
    template without an existing product as a reference.
    ``template_id`` is NULL for direct product-to-product clones.
    """
    __tablename__ = "product_clone_log"

    id                = Column(Integer, primary_key=True)
    source_product_id = Column(Integer,
                               ForeignKey("products.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    cloned_product_id = Column(Integer,
                               ForeignKey("products.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    template_id       = Column(Integer,
                               ForeignKey("product_templates.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    created_by        = Column(BigInteger, nullable=True)
    # single | bulk_category | from_template
    clone_type        = Column(String(32), nullable=False, default="single")
    options_json      = Column(Text, nullable=True)   # JSON of overrides applied
    created_at        = Column(DateTime, default=datetime.utcnow, index=True)

    source_product = relationship("Product", foreign_keys=[source_product_id])
    cloned_product = relationship("Product", foreign_keys=[cloned_product_id])
    template       = relationship("ProductTemplate")


# ─── V32: Login Activity & Device Management ─────────────────────────────────

class LoginRecord(Base):
    """Detailed login / session-start record — V32 Login Activity & Device Management.

    A row is written every time a new session begins (user inactive ≥12 hours
    then becomes active again, matching the heuristic in bot.py:_track_activity).

    Telegram bot updates do not expose device hardware, OS, or user IP, so
    device_name, os_name, app_version, ip_address, country, and city default
    to NULL and are stored only if the calling context can supply them (e.g.
    a future webhook integration).  language_code is available from
    ``telegram.User`` and serves as a lightweight locale/region signal.
    """
    __tablename__ = 'login_records'

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    telegram_id     = Column(BigInteger, nullable=False, index=True)
    username        = Column(String(255), nullable=True)
    session_id      = Column(Integer,
                             ForeignKey('user_sessions.id', ondelete='SET NULL'),
                             nullable=True, index=True)
    login_method    = Column(String(64), nullable=False, default='telegram')
    device_name     = Column(String(255), nullable=True)
    os_name         = Column(String(128), nullable=True)
    app_version     = Column(String(64), nullable=True)
    language_code   = Column(String(16), nullable=True)
    ip_address      = Column(String(64), nullable=True)
    country         = Column(String(128), nullable=True)
    city            = Column(String(128), nullable=True)
    is_suspicious   = Column(Boolean, default=False, nullable=False, index=True)
    is_new_device   = Column(Boolean, default=False, nullable=False)
    is_new_location = Column(Boolean, default=False, nullable=False)
    alert_sent      = Column(Boolean, default=False, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow,
                             nullable=False, index=True)

    user = relationship("User")


class UserDevice(Base):
    """Per-user device fingerprint registry — V32 Login Activity & Device Management.

    A row is created the first time a new fingerprint appears for a user.
    login_count increments on every subsequent visit from that fingerprint.

    device_hash is a sha1 of (user_id + language_code), a stable, privacy-safe
    proxy available from every Telegram update.  When richer device data
    becomes available (webhook headers, future API fields), device_name /
    os_name / app_version can be backfilled.
    """
    __tablename__ = 'user_devices'
    __table_args__ = (
        UniqueConstraint('user_id', 'device_hash', name='uq_device_user_hash'),
    )

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    device_hash   = Column(String(64), nullable=False, index=True)
    device_name   = Column(String(255), nullable=True)
    os_name       = Column(String(128), nullable=True)
    app_version   = Column(String(64), nullable=True)
    language_code = Column(String(16), nullable=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at  = Column(DateTime, default=datetime.utcnow,
                           nullable=False, index=True)
    is_trusted    = Column(Boolean, default=False, nullable=False)
    login_count   = Column(Integer, default=1, nullable=False)

    user = relationship("User")


# ─── V33: Customer Notes & CRM System ────────────────────────────────────────

class CustomerProfile(Base):
    """Per-user CRM profile — V33 Customer Notes & CRM System.

    Created on first admin interaction. Stores the admin-facing priority level,
    internal status, and a denormalised notes_count for fast dashboard queries.
    NEVER exposed to end-users.
    """
    __tablename__ = 'customer_profiles'

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                           nullable=False, unique=True, index=True)
    # low | medium | high | critical
    priority      = Column(String(16), nullable=False, default='low', index=True)
    # new_customer | returning | vip | reseller | wholesale |
    # blocked | suspended | verified | custom
    crm_status    = Column(String(64), nullable=False, default='new_customer', index=True)
    custom_status = Column(String(128), nullable=True)
    notes_count   = Column(Integer, nullable=False, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at    = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by    = Column(BigInteger, nullable=True)   # admin telegram_id

    user = relationship("User")


class CustomerNote(Base):
    """Admin-private note about a user — V33 Customer Notes & CRM System.

    These notes must NEVER be returned to any user-facing handler.
    ``admin_id`` is the Telegram ID of the admin who wrote the note.
    """
    __tablename__ = 'customer_notes'

    id          = Column(Integer, primary_key=True)
    user_id     = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    admin_id    = Column(BigInteger, nullable=False, index=True)
    admin_name  = Column(String(255), nullable=True)
    content     = Column(Text, nullable=False)
    is_pinned   = Column(Boolean, nullable=False, default=False, index=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at  = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")


class CustomerTag(Base):
    """Global admin-defined tag — V33 Customer Notes & CRM System.

    Shared across all users; admins may create unlimited custom tags.
    """
    __tablename__ = 'customer_tags'
    __table_args__ = (
        UniqueConstraint('name', name='uq_tag_name'),
    )

    id         = Column(Integer, primary_key=True)
    name       = Column(String(64), nullable=False)
    color      = Column(String(16), nullable=True)
    created_by = Column(BigInteger, nullable=True)   # admin telegram_id
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    assignments = relationship("CustomerTagAssignment", back_populates="tag",
                               cascade="all, delete-orphan")


class CustomerTagAssignment(Base):
    """Many-to-many between users and tags — V33 Customer Notes & CRM System."""
    __tablename__ = 'customer_tag_assignments'
    __table_args__ = (
        UniqueConstraint('user_id', 'tag_id', name='uq_tag_assignment'),
    )

    id          = Column(Integer, primary_key=True)
    user_id     = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    tag_id      = Column(Integer, ForeignKey('customer_tags.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    assigned_by = Column(BigInteger, nullable=True)
    assigned_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")
    tag  = relationship("CustomerTag", back_populates="assignments")


class CustomerReminder(Base):
    """Follow-up reminder set by an admin for a user — V33 Customer Notes & CRM System."""
    __tablename__ = 'customer_reminders'

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    admin_id     = Column(BigInteger, nullable=False, index=True)
    reason       = Column(Text, nullable=False)
    remind_at    = Column(DateTime, nullable=False, index=True)
    is_completed = Column(Boolean, nullable=False, default=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")


# ══════════════════════════════════════════════════════════════════════════
# V34: Global Settings Backup & Restore
# ══════════════════════════════════════════════════════════════════════════

class SettingsBackupRecord(Base):
    """JSON settings backup record — V34 Global Settings Backup & Restore."""
    __tablename__ = 'settings_backup_records'

    id               = Column(Integer, primary_key=True)
    backup_type      = Column(String(32), nullable=False, default='settings')
    filename         = Column(String(255), nullable=False)
    size_bytes       = Column(BigInteger, nullable=True)
    status           = Column(String(16), nullable=False, default='RUNNING', index=True)
    checksum         = Column(String(64), nullable=True)
    note             = Column(String(255), nullable=True)
    created_by       = Column(BigInteger, nullable=True)
    triggered_by     = Column(String(16), nullable=False, default='manual')
    created_at       = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at     = Column(DateTime, nullable=True)
    restore_count    = Column(Integer, nullable=False, default=0)
    last_restored_at = Column(DateTime, nullable=True)
    last_restored_by = Column(BigInteger, nullable=True)
    error_summary    = Column(String(500), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# V34: System Diagnostics Center
# ══════════════════════════════════════════════════════════════════════════

class DiagnosticsRecord(Base):
    """System diagnostics scan record — V34 System Diagnostics Center."""
    __tablename__ = 'diagnostics_records'

    id             = Column(Integer, primary_key=True)
    scan_type      = Column(String(16), nullable=False, default='full')
    triggered_by   = Column(String(16), nullable=False, default='manual')
    admin_id       = Column(BigInteger, nullable=True)
    started_at     = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at   = Column(DateTime, nullable=True)
    status         = Column(String(16), nullable=False, default='RUNNING', index=True)
    overall_health = Column(String(16), nullable=True)   # healthy|warning|critical
    summary        = Column(Text, nullable=True)          # JSON array of CheckResult dicts
    total_checks   = Column(Integer, nullable=False, default=0)
    healthy_count  = Column(Integer, nullable=False, default=0)
    warning_count  = Column(Integer, nullable=False, default=0)
    critical_count = Column(Integer, nullable=False, default=0)


# ══════════════════════════════════════════════════════════════════════════
# V35: Bulk Product Import/Export & Bulk User Management
# ══════════════════════════════════════════════════════════════════════════

class BulkImportRecord(Base):
    """Tracks a single bulk product import operation — V35."""
    __tablename__ = 'bulk_import_records'

    id            = Column(Integer, primary_key=True)
    admin_id      = Column(BigInteger, nullable=False, index=True)
    file_format   = Column(String(8), nullable=False)           # csv | xlsx | json
    status        = Column(String(16), nullable=False, default='RUNNING', index=True)
    total_rows    = Column(Integer, nullable=False, default=0)
    imported      = Column(Integer, nullable=False, default=0)
    failed        = Column(Integer, nullable=False, default=0)
    duplicates    = Column(Integer, nullable=False, default=0)
    report        = Column(Text, nullable=True)                  # JSON list of error strings
    started_at    = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at  = Column(DateTime, nullable=True)
    error_summary = Column(String(500), nullable=True)


class BulkExportRecord(Base):
    """Tracks a bulk product or user export operation — V35."""
    __tablename__ = 'bulk_export_records'

    id            = Column(Integer, primary_key=True)
    admin_id      = Column(BigInteger, nullable=False, index=True)
    export_type   = Column(String(16), nullable=False)           # products | users
    file_format   = Column(String(8), nullable=False)            # csv | xlsx | json
    scope         = Column(String(32), nullable=False)           # all|category|selected|filtered
    status        = Column(String(16), nullable=False, default='RUNNING', index=True)
    row_count     = Column(Integer, nullable=False, default=0)
    size_bytes    = Column(BigInteger, nullable=True)
    started_at    = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at  = Column(DateTime, nullable=True)
    error_summary = Column(String(500), nullable=True)


class BulkActionRecord(Base):
    """Tracks a bulk action (on products or users) — V35."""
    __tablename__ = 'bulk_action_records'

    id            = Column(Integer, primary_key=True)
    admin_id      = Column(BigInteger, nullable=False, index=True)
    action_type   = Column(String(32), nullable=False, index=True)  # bulk_ban, bulk_price_edit ...
    entity_type   = Column(String(16), nullable=False)               # product | user
    scope         = Column(String(32), nullable=True)                # all|selected|filtered
    target_count  = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failed_count  = Column(Integer, nullable=False, default=0)
    details       = Column(Text, nullable=True)                      # JSON params (no PII)
    status        = Column(String(16), nullable=False, default='COMPLETED', index=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at  = Column(DateTime, nullable=True)


# ═══════════════════════════════════════════════════════════════════════════════
# V36 — Delivery Management System
# ═══════════════════════════════════════════════════════════════════════════════

import uuid as _uuid_mod


def _new_uuid() -> str:
    return str(_uuid_mod.uuid4())


class DeliveryRecord(Base):
    """Comprehensive delivery record for the V36 Delivery Management System.

    Every delivery attempt—automatic or manual—creates one row here.
    Supports all 9 delivery types and 6 delivery methods defined in the
    Delivery Management System spec.
    """
    __tablename__ = "delivery_records"

    id              = Column(Integer, primary_key=True)
    secure_id       = Column(String(36), unique=True, nullable=False,
                             index=True, default=_new_uuid)

    # ── Relations ────────────────────────────────────────────────────────────
    order_id        = Column(Integer, ForeignKey("orders.id"),
                             nullable=True, index=True)
    order_item_id   = Column(Integer, ForeignKey("order_items.id"),
                             nullable=True, index=True)
    user_id         = Column(BigInteger, nullable=False, index=True)
    product_id      = Column(Integer, ForeignKey("products.id"),
                             nullable=True, index=True)

    # ── Delivery type / method ────────────────────────────────────────────────
    # delivery_type: product_key | account | gift_card | license_key |
    #                digital_file | download_link | custom_text | api | manual
    delivery_type   = Column(String(32), nullable=False, index=True)
    # delivery_method: automatic | manual | scheduled | bulk | random | api
    delivery_method = Column(String(16), nullable=False,
                             default="automatic", index=True)

    # ── Delivered content ─────────────────────────────────────────────────────
    delivered_content   = Column(Text, nullable=True)    # JSON / plain text
    template_snapshot   = Column(Text, nullable=True)    # Template at delivery time

    # ── Status ────────────────────────────────────────────────────────────────
    # pending | preparing | processing | delivered | completed |
    # failed | cancelled | expired | refunded
    status          = Column(String(16), nullable=False,
                             default="pending", index=True)

    # ── Admin ─────────────────────────────────────────────────────────────────
    admin_id        = Column(BigInteger, nullable=True)
    admin_note      = Column(String(500), nullable=True)

    # ── Retry tracking ────────────────────────────────────────────────────────
    retry_count     = Column(Integer, nullable=False, default=0)
    max_retries     = Column(Integer, nullable=False, default=3)
    last_error      = Column(String(1000), nullable=True)

    # ── Secure download link support ──────────────────────────────────────────
    download_token  = Column(String(64), nullable=True,
                             unique=True, index=True)
    download_limit  = Column(Integer, nullable=True)       # None = unlimited
    download_count  = Column(Integer, nullable=False, default=0)
    is_one_time     = Column(Boolean, nullable=False, default=False)
    link_expires_at = Column(DateTime, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at      = Column(DateTime, default=datetime.utcnow,
                             nullable=False, index=True)
    prepared_at     = Column(DateTime, nullable=True)
    processed_at    = Column(DateTime, nullable=True)
    delivered_at    = Column(DateTime, nullable=True)
    completed_at    = Column(DateTime, nullable=True)
    expires_at      = Column(DateTime, nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# V37: Real-time Admin Notification Center
# ══════════════════════════════════════════════════════════════════════════

class NotificationSeverity(str, enum.Enum):
    PUSH     = "push"      # Real-time Telegram message + stored
    IN_BOT   = "in_bot"    # Stored in notification center only
    SILENT   = "silent"    # Stored silently, no Telegram message
    CRITICAL = "critical"  # Critical alert, sent immediately


class NotificationCategory(str, enum.Enum):
    ORDERS      = "orders"
    PAYMENTS    = "payments"
    WITHDRAWALS = "withdrawals"
    PRODUCTS    = "products"
    USERS       = "users"
    SYSTEM      = "system"
    SECURITY    = "security"
    API         = "api"
    FRAUD       = "fraud"
    SUPPORT     = "support"


class AdminNotification(Base):
    """Centralized notification record for the Admin Notification Center — V37.

    One row per event. Admins can view, filter, mark-read, pin, archive,
    and delete notifications from the Notification Center panel.
    """
    __tablename__ = "admin_notifications"

    id                = Column(Integer, primary_key=True)
    event_type        = Column(String(64),  nullable=False, index=True)   # e.g. new_order
    category          = Column(String(32),  nullable=False, index=True)   # orders|payments|...
    severity          = Column(String(16),  nullable=False, default="push", index=True)
    title             = Column(String(255), nullable=False)
    body              = Column(Text,        nullable=False)
    source_type       = Column(String(32),  nullable=True)   # user|order|product|payment|system
    source_id         = Column(String(64),  nullable=True)   # stringified PK / TG id
    is_read           = Column(Boolean, nullable=False, default=False, index=True)
    is_pinned         = Column(Boolean, nullable=False, default=False, index=True)
    is_archived       = Column(Boolean, nullable=False, default=False, index=True)
    admin_telegram_id = Column(BigInteger, nullable=True, index=True)  # null = broadcast to all admins
    created_at        = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    read_at           = Column(DateTime, nullable=True)
    archived_at       = Column(DateTime, nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# V37: File & License Key Manager
# ══════════════════════════════════════════════════════════════════════════

class ManagedKeyType(str, enum.Enum):
    PRODUCT_KEY     = "product_key"
    LICENSE_KEY     = "license_key"
    ACCOUNT         = "account"
    SERIAL_NUMBER   = "serial_number"
    GIFT_CODE       = "gift_code"
    ACTIVATION_CODE = "activation_code"


class ManagedKeyStatus(str, enum.Enum):
    UNUSED   = "unused"
    RESERVED = "reserved"
    USED     = "used"
    EXPIRED  = "expired"
    RECYCLED = "recycled"


class ManagedFile(Base):
    """Digital file record managed by the File Manager — V37.

    Files are uploaded by the admin and optionally linked to a product.
    They can be delivered automatically on order completion.
    """
    __tablename__ = "managed_files"

    id                = Column(Integer, primary_key=True)
    filename          = Column(String(255), nullable=False)
    description       = Column(Text, nullable=True)
    # pdf|zip|rar|txt|docx|image|video|software|other
    file_type         = Column(String(16),  nullable=False, default="other", index=True)
    telegram_file_id  = Column(String(256), nullable=True)
    file_size         = Column(BigInteger, nullable=True)
    product_id        = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    # active|archived|expired
    status            = Column(String(16), nullable=False, default="active", index=True)
    max_downloads     = Column(Integer, nullable=True)      # null = unlimited
    download_count    = Column(Integer, nullable=False, default=0)
    auto_delete_days  = Column(Integer, nullable=True)      # auto-archive after N days
    expires_at        = Column(DateTime, nullable=True)
    created_by        = Column(BigInteger, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product           = relationship("Product", foreign_keys=[product_id])
    download_logs     = relationship("FileDownloadLog", back_populates="managed_file",
                                     cascade="all, delete-orphan")


class ManagedKey(Base):
    """Generic license/product/activation key — V37 File & License Key Manager.

    Decoupled from ProductKey (which is per-product inventory) to allow
    bulk import, generate, reserve, export, recycle operations.
    """
    __tablename__ = "managed_keys"
    __table_args__ = (
        UniqueConstraint("key_fingerprint", name="uq_managed_key_fingerprint"),
    )

    id                = Column(Integer, primary_key=True)
    key_type          = Column(String(32), nullable=False, index=True)    # ManagedKeyType value
    key_value         = Column(Text,       nullable=False)
    key_fingerprint   = Column(String(64), nullable=True,  index=True)   # sha256 for dedup
    product_id        = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    # unused|reserved|used|expired|recycled
    status            = Column(String(16), nullable=False, default="unused", index=True)
    reserved_by       = Column(BigInteger, nullable=True)               # admin telegram id
    reserved_at       = Column(DateTime,   nullable=True)
    used_by_user_id   = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    used_at           = Column(DateTime,   nullable=True)
    order_id          = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    notes             = Column(Text,       nullable=True)
    created_by        = Column(BigInteger, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    expires_at        = Column(DateTime,   nullable=True)

    product           = relationship("Product", foreign_keys=[product_id])
    used_by_user      = relationship("User",    foreign_keys=[used_by_user_id])
    order             = relationship("Order",   foreign_keys=[order_id])
    deliveries        = relationship("ManagedKeyDelivery", back_populates="key",
                                     cascade="all, delete-orphan")


class ManagedKeyDelivery(Base):
    """Delivery log for a ManagedKey — V37."""
    __tablename__ = "managed_key_deliveries"

    id              = Column(Integer, primary_key=True)
    key_id          = Column(Integer, ForeignKey("managed_keys.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id        = Column(Integer, ForeignKey("orders.id"), nullable=True,  index=True)
    delivered_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    delivery_method = Column(String(16), nullable=False, default="automatic")
    admin_id        = Column(BigInteger, nullable=True)

    key             = relationship("ManagedKey", back_populates="deliveries")
    user            = relationship("User",       foreign_keys=[user_id])
    order           = relationship("Order",      foreign_keys=[order_id])


class FileDownloadLog(Base):
    """Download event log for a ManagedFile — V37."""
    __tablename__ = "file_download_logs"

    id              = Column(Integer, primary_key=True)
    file_id         = Column(Integer, ForeignKey("managed_files.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True,  index=True)
    order_id        = Column(Integer, ForeignKey("orders.id"), nullable=True,  index=True)
    downloaded_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    managed_file    = relationship("ManagedFile", back_populates="download_logs")
    user            = relationship("User",  foreign_keys=[user_id])
    order           = relationship("Order", foreign_keys=[order_id])


# ══════════════════════════════════════════════════════════════════════════
# V38: Flash Sale Manager (Enhanced)
# ══════════════════════════════════════════════════════════════════════════

class FlashSaleStatus(str, enum.Enum):
    DRAFT     = "draft"
    SCHEDULED = "scheduled"
    ACTIVE    = "active"
    PAUSED    = "paused"
    ENDED     = "ended"
    CANCELLED = "cancelled"


class FlashSaleScopeType(str, enum.Enum):
    SINGLE_PRODUCT   = "single_product"
    MULTI_PRODUCT    = "multi_product"
    CATEGORY         = "category"
    MULTI_CATEGORY   = "multi_category"


class FlashSaleEvent(Base):
    """Enhanced Flash Sale with multi-product/category scope, broadcasts, stats — V38.

    Relationship to legacy FlashSale (V15):
    When activated, the service creates corresponding FlashSale rows so that
    services/pricing.py continues to price correctly without modification.
    """
    __tablename__ = "flash_sale_events"

    id              = Column(Integer, primary_key=True)
    name            = Column(String(255), nullable=False)
    description     = Column(Text, nullable=True)
    banner_file_id  = Column(String(256), nullable=True)   # Telegram file_id for banner
    badge_text      = Column(String(64),  nullable=True)   # e.g. "⚡ FLASH SALE"

    # Scope
    scope_type         = Column(String(32), nullable=False, default="single_product", index=True)
    product_ids_json   = Column(Text, nullable=True)   # JSON list of int product IDs
    category_ids_json  = Column(Text, nullable=True)   # JSON list of int category IDs

    # Pricing
    discount_percent = Column(Float, nullable=True)   # 0..100 — null when fixed price used
    fixed_sale_price = Column(Float, nullable=True)   # null when percent used

    # Schedule
    start_time  = Column(DateTime, nullable=False, index=True)
    end_time    = Column(DateTime, nullable=False, index=True)
    timezone    = Column(String(64), nullable=False, default="UTC")
    priority    = Column(Integer,  nullable=False, default=0)   # higher overrides lower

    # Status
    status    = Column(String(16), nullable=False, default="draft", index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    # Broadcast flags
    broadcast_on_start = Column(Boolean, nullable=False, default=True)
    broadcast_on_end   = Column(Boolean, nullable=False, default=False)
    broadcast_24h      = Column(Boolean, nullable=False, default=True)
    broadcast_12h      = Column(Boolean, nullable=False, default=False)
    broadcast_6h       = Column(Boolean, nullable=False, default=False)
    broadcast_3h       = Column(Boolean, nullable=False, default=False)
    broadcast_1h       = Column(Boolean, nullable=False, default=True)
    broadcast_30m      = Column(Boolean, nullable=False, default=False)
    broadcast_10m      = Column(Boolean, nullable=False, default=False)

    # Customizable message template (supports {product_name}, {old_price}, {sale_price},
    # {discount_percent}, {countdown}, {badge})
    message_template = Column(Text, nullable=True)

    # Homepage display
    show_on_homepage   = Column(Boolean, nullable=False, default=True)
    homepage_priority  = Column(Integer, nullable=False, default=0)

    # Statistics (denormalised for fast reads)
    view_count  = Column(Integer, nullable=False, default=0)
    click_count = Column(Integer, nullable=False, default=0)
    order_count = Column(Integer, nullable=False, default=0)
    revenue     = Column(Float,   nullable=False, default=0.0)

    # Meta
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    price_snapshots = relationship(
        "FlashSalePriceSnapshot", back_populates="flash_sale_event",
        cascade="all, delete-orphan"
    )
    broadcast_logs = relationship(
        "FlashSaleBroadcastLog", back_populates="flash_sale_event",
        cascade="all, delete-orphan"
    )

    def is_live(self, now: datetime = None) -> bool:
        now = now or datetime.utcnow()
        return self.is_active and self.status == "active" and self.start_time <= now < self.end_time

    def countdown(self, now: datetime = None) -> str:
        """Human-readable countdown string, e.g. '4h 32m' or '2 Days'."""
        now = now or datetime.utcnow()
        delta = self.end_time - now
        total = int(delta.total_seconds())
        if total <= 0:
            return "Ended"
        days  = total // 86400
        hours = (total % 86400) // 3600
        mins  = (total % 3600) // 60
        if days >= 2:
            return f"{days} Days"
        if days == 1:
            return f"1 Day {hours}h" if hours else "1 Day"
        if hours >= 1:
            return f"{hours}h {mins}m"
        return f"{mins} Minutes" if mins > 0 else "< 1 Minute"


class FlashSalePriceSnapshot(Base):
    """Original product prices saved before a sale starts — restored on sale end — V38."""
    __tablename__ = "flash_sale_price_snapshots"
    __table_args__ = (
        UniqueConstraint("flash_sale_event_id", "product_id",
                         name="uq_fsps_sale_product"),
    )

    id                  = Column(Integer, primary_key=True)
    flash_sale_event_id = Column(Integer, ForeignKey("flash_sale_events.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    product_id          = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    original_price      = Column(Float, nullable=False)
    original_sale_price = Column(Float, nullable=True)    # product.sale_price before sale
    applied_sale_price  = Column(Float, nullable=False)   # price we actually set
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False)

    flash_sale_event = relationship("FlashSaleEvent", back_populates="price_snapshots")
    product          = relationship("Product")


class FlashSaleBroadcastLog(Base):
    """Tracks which timed broadcasts have been sent for a FlashSaleEvent — V38."""
    __tablename__ = "flash_sale_broadcast_logs"
    __table_args__ = (
        UniqueConstraint("flash_sale_event_id", "broadcast_type",
                         name="uq_fsbl_sale_type"),
    )

    id                  = Column(Integer, primary_key=True)
    flash_sale_event_id = Column(Integer, ForeignKey("flash_sale_events.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    # "start"|"end"|"24h"|"12h"|"6h"|"3h"|"1h"|"30m"|"10m"
    broadcast_type      = Column(String(8), nullable=False)
    sent_at             = Column(DateTime, default=datetime.utcnow, nullable=False)
    recipients          = Column(Integer, nullable=False, default=0)
    error_message       = Column(Text, nullable=True)

    flash_sale_event = relationship("FlashSaleEvent", back_populates="broadcast_logs")


# ══════════════════════════════════════════════════════════════════════════════
# V39: Multi-Currency Wallet System
# ══════════════════════════════════════════════════════════════════════════════

class WalletCurrencyStatus(str, enum.Enum):
    """Operational status for a currency in the multi-currency wallet."""
    ENABLED     = "enabled"
    DISABLED    = "disabled"
    MAINTENANCE = "maintenance"
    FROZEN      = "frozen"


class CurrencyTransactionType(str, enum.Enum):
    """Types of multi-currency wallet transactions — V39."""
    DEPOSIT          = "deposit"
    WITHDRAWAL       = "withdrawal"
    PURCHASE         = "purchase"
    REFUND           = "refund"
    REFERRAL_REWARD  = "referral_reward"
    BONUS            = "bonus"
    ADMIN_ADJUSTMENT = "admin_adjustment"
    MANUAL_CREDIT    = "manual_credit"
    MANUAL_DEBIT     = "manual_debit"
    TRANSFER_IN      = "transfer_in"
    TRANSFER_OUT     = "transfer_out"
    EXCHANGE_IN      = "exchange_in"
    EXCHANGE_OUT     = "exchange_out"
    FEE              = "fee"


class CurrencyTxStatus(str, enum.Enum):
    """Lifecycle status of a multi-currency wallet transaction — V39."""
    PENDING   = "pending"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class WalletCurrencyConfig(Base):
    """Admin-managed currency registry for the multi-currency wallet — V39.

    Each row represents one supported currency (fiat or crypto).
    The existing User.wallet_balance (USD) stays unchanged; this table
    powers ADDITIONAL per-currency balances via UserCurrencyWallet.
    """
    __tablename__ = "wallet_currency_configs"
    __table_args__ = (
        UniqueConstraint("code", name="uq_wcc_code"),
    )

    id                  = Column(Integer, primary_key=True)
    code                = Column(String(16), nullable=False, index=True)   # "USD", "BTC", "USDT" …
    name                = Column(String(64), nullable=False)                # "US Dollar" …
    symbol              = Column(String(8),  nullable=False, default="$")  # "$", "₿", "Ξ" …
    is_crypto           = Column(Boolean, nullable=False, default=False)
    is_enabled          = Column(Boolean, nullable=False, default=True, index=True)
    status              = Column(String(16), nullable=False, default="enabled")  # WalletCurrencyStatus
    is_frozen           = Column(Boolean, nullable=False, default=False)

    # Balance constraints (0 = no limit)
    min_balance         = Column(Float, nullable=False, default=0.0)
    max_balance         = Column(Float, nullable=False, default=0.0)

    # Deposit settings
    min_deposit         = Column(Float, nullable=False, default=0.0)
    max_deposit         = Column(Float, nullable=False, default=0.0)
    deposit_fee_pct     = Column(Float, nullable=False, default=0.0)

    # Withdrawal settings
    min_withdrawal      = Column(Float, nullable=False, default=0.0)
    max_withdrawal      = Column(Float, nullable=False, default=0.0)
    withdrawal_fee_pct  = Column(Float, nullable=False, default=0.0)
    withdrawal_fee_flat = Column(Float, nullable=False, default=0.0)

    sort_order          = Column(Integer, nullable=False, default=0)
    notes               = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                                 nullable=False)

    wallets             = relationship("UserCurrencyWallet", back_populates="currency_config",
                                       cascade="all, delete-orphan",
                                       foreign_keys="UserCurrencyWallet.currency_code",
                                       primaryjoin="WalletCurrencyConfig.code == UserCurrencyWallet.currency_code")


class UserCurrencyWallet(Base):
    """Per-user per-currency balance record — V39.

    Distinct from User.wallet_balance (primary USD wallet, unchanged).
    One row per (user_id, currency_code) pair; created on-demand.
    Negative balances are prevented at the service layer.
    """
    __tablename__ = "user_currency_wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "currency_code", name="uq_ucw_user_currency"),
    )

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    currency_code   = Column(String(16), nullable=False, index=True)
    balance         = Column(Float, nullable=False, default=0.0)
    is_frozen       = Column(Boolean, nullable=False, default=False)
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                             nullable=False)

    user            = relationship("User", foreign_keys=[user_id])
    currency_config = relationship("WalletCurrencyConfig",
                                   back_populates="wallets",
                                   foreign_keys=[currency_code],
                                   primaryjoin="UserCurrencyWallet.currency_code == WalletCurrencyConfig.code")
    transactions    = relationship("CurrencyTransaction", back_populates="wallet",
                                   order_by="CurrencyTransaction.created_at.desc()")


class CurrencyTransaction(Base):
    """Append-only ledger for every multi-currency wallet movement — V39.

    amount  is always positive; tx_type encodes direction
    (DEPOSIT / MANUAL_CREDIT = credit; WITHDRAWAL / MANUAL_DEBIT = debit).
    """
    __tablename__ = "currency_transactions"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    wallet_id       = Column(Integer, ForeignKey("user_currency_wallets.id"),
                             nullable=False, index=True)
    currency_code   = Column(String(16), nullable=False, index=True)
    tx_type         = Column(String(32), nullable=False)   # CurrencyTransactionType.value
    amount          = Column(Float, nullable=False)         # always positive
    fee             = Column(Float, nullable=False, default=0.0)
    net_amount      = Column(Float, nullable=False)         # amount − fee
    balance_before  = Column(Float, nullable=False)
    balance_after   = Column(Float, nullable=False)
    status          = Column(String(16), nullable=False, default="completed")  # CurrencyTxStatus
    ref_type        = Column(String(32), nullable=True)    # order|topup|refund|transfer|exchange
    ref_id          = Column(String(64), nullable=True)
    actor_type      = Column(String(16), nullable=False, default="system")  # system|user|admin
    actor_id        = Column(BigInteger, nullable=True)
    notes           = Column(String(255), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user            = relationship("User", foreign_keys=[user_id])
    wallet          = relationship("UserCurrencyWallet", back_populates="transactions")


# ══════════════════════════════════════════════════════════════════════════════
# V39: Exchange Rate Manager
# ══════════════════════════════════════════════════════════════════════════════

class ExchangeRateSource(str, enum.Enum):
    """Source of an exchange rate value — V39."""
    MANUAL      = "manual"      # Admin-entered rate
    AUTO_API    = "auto_api"    # Fetched from external API automatically
    FIXED       = "fixed"       # Locked/frozen — never auto-updated
    CUSTOM      = "custom"      # Margin-adjusted from API base


class ExchangeRatePairStatus(str, enum.Enum):
    """Operational status of an exchange rate pair — V39."""
    ENABLED     = "enabled"
    DISABLED    = "disabled"
    MAINTENANCE = "maintenance"


class ExchangeRatePair(Base):
    """Configured exchange rate pair — V39.

    Tracks how many `to_currency` units equal 1 `from_currency`.
    Rates are kept separately for buy (user buys to_currency) and
    sell (user sells/converts to_currency), with an optional margin.
    """
    __tablename__ = "exchange_rate_pairs"
    __table_args__ = (
        UniqueConstraint("from_currency", "to_currency", name="uq_erp_pair"),
    )

    id                   = Column(Integer, primary_key=True)
    from_currency        = Column(String(16), nullable=False, index=True)
    to_currency          = Column(String(16), nullable=False, index=True)
    display_name         = Column(String(64), nullable=True)   # e.g. "USD / BDT"

    # Current rates
    mid_rate             = Column(Float, nullable=True)         # raw market midpoint
    buy_rate             = Column(Float, nullable=True)         # admin-to-user direction
    sell_rate            = Column(Float, nullable=True)         # user-to-admin direction
    margin_pct           = Column(Float, nullable=False, default=0.0)  # spread %

    # Source & update settings
    rate_source          = Column(String(16), nullable=False, default="manual")   # ExchangeRateSource
    auto_update_interval = Column(Integer, nullable=False, default=60)            # minutes
    api_url              = Column(String(512), nullable=True)   # custom endpoint, if any
    api_response_path    = Column(String(128), nullable=True)   # dot-separated JSON path

    # Manual override (admin-set; NULL = use fetched/computed)
    manual_override_rate = Column(Float, nullable=True)
    is_locked            = Column(Boolean, nullable=False, default=False)  # no auto updates

    status               = Column(String(16), nullable=False, default="enabled")  # ExchangeRatePairStatus
    is_active            = Column(Boolean, nullable=False, default=True, index=True)

    # Tracking
    previous_mid_rate    = Column(Float, nullable=True)
    last_updated         = Column(DateTime, nullable=True)
    last_auto_update     = Column(DateTime, nullable=True)
    last_update_source   = Column(String(16), nullable=True)
    last_update_error    = Column(Text, nullable=True)
    updates_today        = Column(Integer, nullable=False, default=0)
    failed_updates_today = Column(Integer, nullable=False, default=0)

    created_at           = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at           = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                                  nullable=False)

    history              = relationship("ExchangeRateHistory", back_populates="pair",
                                        order_by="ExchangeRateHistory.recorded_at.desc()",
                                        cascade="all, delete-orphan")
    logs                 = relationship("ExchangeRateLog", back_populates="pair",
                                        cascade="all, delete-orphan")


class ExchangeRateHistory(Base):
    """Point-in-time snapshot of an exchange rate pair — V39."""
    __tablename__ = "exchange_rate_history"

    id            = Column(Integer, primary_key=True)
    pair_id       = Column(Integer, ForeignKey("exchange_rate_pairs.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    from_currency = Column(String(16), nullable=False)
    to_currency   = Column(String(16), nullable=False)
    mid_rate      = Column(Float, nullable=True)
    buy_rate      = Column(Float, nullable=True)
    sell_rate     = Column(Float, nullable=True)
    margin_pct    = Column(Float, nullable=True)
    source        = Column(String(16), nullable=False, default="manual")
    recorded_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    pair          = relationship("ExchangeRatePair", back_populates="history")


class ExchangeRateLog(Base):
    """Audit trail for admin actions on exchange rate pairs — V39."""
    __tablename__ = "exchange_rate_logs"

    id            = Column(Integer, primary_key=True)
    pair_id       = Column(Integer, ForeignKey("exchange_rate_pairs.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    action        = Column(String(64), nullable=False)    # update|lock|unlock|manual_override|enable|disable
    old_rate      = Column(Float, nullable=True)
    new_rate      = Column(Float, nullable=True)
    actor_type    = Column(String(16), nullable=False, default="system")
    actor_id      = Column(BigInteger, nullable=True)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    pair          = relationship("ExchangeRatePair", back_populates="logs")


# ══════════════════════════════════════════════════════════════════════════════
# V40: Sales Forecast & Business Insights
# ══════════════════════════════════════════════════════════════════════════════

class BusinessReport(Base):
    """Stored generated reports — V40."""
    __tablename__ = "business_reports"

    id           = Column(Integer, primary_key=True)
    report_type  = Column(String(32), nullable=False, index=True)  # daily|weekly|monthly|yearly|revenue|orders|customer|referral|payment
    period_start = Column(DateTime, nullable=False)
    period_end   = Column(DateTime, nullable=False)
    title        = Column(String(128), nullable=False)
    summary_json = Column(Text, nullable=True)   # JSON-encoded key metrics
    notes        = Column(Text, nullable=True)
    generated_by = Column(BigInteger, nullable=True)  # telegram_id (NULL = auto)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ForecastSnapshot(Base):
    """Point-in-time sales forecast — V40."""
    __tablename__ = "forecast_snapshots"

    id                    = Column(Integer, primary_key=True)
    period                = Column(String(16), nullable=False, index=True)   # day|week|month
    forecast_date         = Column(DateTime, nullable=False, index=True)
    predicted_revenue     = Column(Float, nullable=False, default=0.0)
    predicted_orders      = Column(Integer, nullable=False, default=0)
    predicted_growth_pct  = Column(Float, nullable=True)
    baseline_revenue      = Column(Float, nullable=True)    # average used as baseline
    trend_direction       = Column(String(16), nullable=True)   # up|down|flat
    confidence_pct        = Column(Float, nullable=True)
    actual_revenue        = Column(Float, nullable=True)    # filled in after the period
    actual_orders         = Column(Integer, nullable=True)
    model_version         = Column(String(16), nullable=False, default="v1_sma")
    created_at            = Column(DateTime, default=datetime.utcnow, nullable=False)


class DailyAnalyticsSnapshot(Base):
    """Cached daily analytics rolled up for fast dashboard queries — V40."""
    __tablename__ = "daily_analytics_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_das_date"),
    )

    id                  = Column(Integer, primary_key=True)
    snapshot_date       = Column(DateTime, nullable=False, index=True)
    revenue             = Column(Float, nullable=False, default=0.0)
    orders              = Column(Integer, nullable=False, default=0)
    new_users           = Column(Integer, nullable=False, default=0)
    active_users        = Column(Integer, nullable=False, default=0)
    avg_order_value     = Column(Float, nullable=True)
    top_product_id      = Column(Integer, nullable=True)
    top_category_id     = Column(Integer, nullable=True)
    refund_amount       = Column(Float, nullable=False, default=0.0)
    gross_profit        = Column(Float, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at          = Column(DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# V40: Auto Moderation & Anti-Spam
# ══════════════════════════════════════════════════════════════════════════════

class SpamViolationType(str, enum.Enum):
    FLOOD              = "flood"
    REPEATED_COMMAND   = "repeated_command"
    REPEATED_MESSAGE   = "repeated_message"
    RAPID_CLICKS       = "rapid_clicks"
    FAKE_REFERRAL      = "fake_referral"
    DUPLICATE_PAYMENT  = "duplicate_payment"
    FAILED_PAYMENTS    = "failed_payments"
    REFERRAL_ABUSE     = "referral_abuse"
    COUPON_ABUSE       = "coupon_abuse"
    BOT_ABUSE          = "bot_abuse"
    BLACKLISTED_WORD   = "blacklisted_word"
    MANUAL             = "manual"


class ModerationActionType(str, enum.Enum):
    WARNING       = "warning"
    MUTE          = "mute"
    UNMUTE        = "unmute"
    RESTRICT      = "restrict"
    TEMP_BAN      = "temp_ban"
    PERM_BAN      = "perm_ban"
    UNBAN         = "unban"
    COOLDOWN      = "cooldown"
    CAPTCHA       = "captcha"
    ADMIN_REVIEW  = "admin_review"
    WHITELIST_ADD = "whitelist_add"
    BLACKLIST_ADD = "blacklist_add"
    CLEAR_WARNINGS= "clear_warnings"


class ModerationStatusType(str, enum.Enum):
    ACTIVE    = "active"
    MUTED     = "muted"
    BANNED    = "banned"
    COOLDOWN  = "cooldown"
    CAPTCHA   = "captcha"
    REVIEW    = "review"


class BlacklistEntryType(str, enum.Enum):
    USER     = "user"
    WORD     = "word"
    REFERRAL = "referral"
    WALLET   = "wallet"


class WhitelistEntryType(str, enum.Enum):
    TRUSTED = "trusted"
    VIP     = "vip"
    ADMIN   = "admin"


class UserModerationStatus(Base):
    """Current moderation state for a user — V40.

    One row per user_telegram_id; upserted on each action.
    """
    __tablename__ = "user_moderation_status"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_ums_tgid"),
    )

    id                = Column(Integer, primary_key=True)
    telegram_id       = Column(BigInteger, nullable=False, index=True)
    username          = Column(String(255), nullable=True)
    status            = Column(String(16), nullable=False,
                               default=ModerationStatusType.ACTIVE.value, index=True)
    is_muted          = Column(Boolean, nullable=False, default=False, index=True)
    mute_expires_at   = Column(DateTime, nullable=True)
    is_banned         = Column(Boolean, nullable=False, default=False, index=True)
    ban_type          = Column(String(16), nullable=True)   # temp|perm
    ban_expires_at    = Column(DateTime, nullable=True)
    is_in_cooldown    = Column(Boolean, nullable=False, default=False)
    cooldown_expires  = Column(DateTime, nullable=True)
    needs_captcha     = Column(Boolean, nullable=False, default=False)
    warning_count     = Column(Integer, nullable=False, default=0)
    total_violations  = Column(Integer, nullable=False, default=0)
    last_violation_at = Column(DateTime, nullable=True)
    under_review      = Column(Boolean, nullable=False, default=False)
    notes             = Column(Text, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at        = Column(DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow, nullable=False)


class SpamLog(Base):
    """Raw spam event log — V40."""
    __tablename__ = "spam_logs"

    id            = Column(Integer, primary_key=True)
    telegram_id   = Column(BigInteger, nullable=False, index=True)
    username      = Column(String(255), nullable=True)
    violation_type= Column(String(32), nullable=False, index=True)   # SpamViolationType
    action_taken  = Column(String(32), nullable=False)               # ModerationActionType
    detail        = Column(Text, nullable=True)
    raw_data      = Column(Text, nullable=True)   # JSON snapshot of the triggering event
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ModerationActionLog(Base):
    """Admin moderation actions audit trail — V40."""
    __tablename__ = "moderation_action_logs"

    id            = Column(Integer, primary_key=True)
    target_tg_id  = Column(BigInteger, nullable=False, index=True)
    action_type   = Column(String(32), nullable=False)    # ModerationActionType
    duration_secs = Column(Integer, nullable=True)        # for mute / temp_ban / cooldown
    expires_at    = Column(DateTime, nullable=True)
    reason        = Column(String(255), nullable=True)
    actor_type    = Column(String(16), nullable=False, default="system")
    actor_id      = Column(BigInteger, nullable=True)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class BlacklistEntry(Base):
    """User / word / referral / wallet blacklist — V40."""
    __tablename__ = "blacklist_entries"
    __table_args__ = (
        UniqueConstraint("entry_type", "value", name="uq_bl_type_value"),
    )

    id          = Column(Integer, primary_key=True)
    entry_type  = Column(String(16), nullable=False, index=True)  # BlacklistEntryType
    value       = Column(String(512), nullable=False)             # tg_id / word / ref_code / wallet
    reason      = Column(String(255), nullable=True)
    added_by    = Column(BigInteger, nullable=True)
    is_active   = Column(Boolean, nullable=False, default=True, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow, nullable=False)


class WhitelistEntry(Base):
    """Trusted / VIP / admin whitelist — V40."""
    __tablename__ = "whitelist_entries"
    __table_args__ = (
        UniqueConstraint("entry_type", "telegram_id", name="uq_wl_type_tgid"),
    )

    id          = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    entry_type  = Column(String(16), nullable=False, index=True)  # WhitelistEntryType
    reason      = Column(String(255), nullable=True)
    added_by    = Column(BigInteger, nullable=True)
    is_active   = Column(Boolean, nullable=False, default=True, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# V41: Customer Loyalty & VIP Tier Manager
# ══════════════════════════════════════════════════════════════════════════════

class VipTier(Base):
    """Admin-defined VIP tier with upgrade requirements and benefits — V41."""
    __tablename__ = 'vip_tiers'
    __table_args__ = (
        UniqueConstraint('level', name='uq_vt_level'),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    emoji = Column(String(8), nullable=False, default='⭐')
    level = Column(Integer, nullable=False, index=True)  # 0=lowest, ascending

    # ── Upgrade requirements (any satisfied → eligible) ─────────────────
    min_orders = Column(Integer, nullable=False, default=0)
    min_spending = Column(Float, nullable=False, default=0.0)
    min_referral_earnings = Column(Float, nullable=False, default=0.0)
    min_account_age_days = Column(Integer, nullable=False, default=0)

    # ── Benefits ─────────────────────────────────────────────────────────
    discount_pct = Column(Float, nullable=False, default=0.0)
    cashback_pct = Column(Float, nullable=False, default=0.0)
    referral_bonus_pct = Column(Float, nullable=False, default=0.0)
    extra_coupon_discount_pct = Column(Float, nullable=False, default=0.0)
    priority_support = Column(Boolean, nullable=False, default=False)
    priority_delivery = Column(Boolean, nullable=False, default=False)
    exclusive_products = Column(Boolean, nullable=False, default=False)
    exclusive_flash_sales = Column(Boolean, nullable=False, default=False)
    withdrawal_limit_multiplier = Column(Float, nullable=False, default=1.0)
    wallet_limit_multiplier = Column(Float, nullable=False, default=1.0)
    custom_benefits = Column(Text, nullable=True)  # JSON list of strings

    is_active = Column(Boolean, nullable=False, default=True, index=True)
    is_default = Column(Boolean, nullable=False, default=False)  # lowest tier for new users
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    assignments = relationship("UserVipTier", back_populates="tier")
    history_new = relationship(
        "VipTierHistory", foreign_keys="VipTierHistory.new_tier_id",
        back_populates="new_tier",
    )
    history_old = relationship(
        "VipTierHistory", foreign_keys="VipTierHistory.old_tier_id",
        back_populates="old_tier",
    )


class UserVipTier(Base):
    """Current VIP tier assignment for a user — one row per user — V41."""
    __tablename__ = 'user_vip_tiers'
    __table_args__ = (
        UniqueConstraint('user_id', name='uq_uvt_user'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    tier_id = Column(Integer, ForeignKey('vip_tiers.id'), nullable=False, index=True)
    assigned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    assigned_by = Column(BigInteger, nullable=True)   # admin telegram_id or NULL for auto
    reason = Column(String(255), nullable=True)

    user = relationship("User")
    tier = relationship("VipTier", back_populates="assignments")


class VipTierHistory(Base):
    """Audit trail of every tier change per user — V41."""
    __tablename__ = 'vip_tier_history'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    old_tier_id = Column(Integer, ForeignKey('vip_tiers.id'), nullable=True)
    new_tier_id = Column(Integer, ForeignKey('vip_tiers.id'), nullable=False)
    reason = Column(String(255), nullable=True)
    changed_by = Column(BigInteger, nullable=True)   # admin tg_id or NULL for auto
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User")
    old_tier = relationship("VipTier", foreign_keys=[old_tier_id], back_populates="history_old")
    new_tier = relationship("VipTier", foreign_keys=[new_tier_id], back_populates="history_new")


class LoyaltyReward(Base):
    """Redemption reward catalog — users spend points to claim — V41."""
    __tablename__ = 'loyalty_rewards'

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    # wallet | coupon | discount | product
    reward_type = Column(String(32), nullable=False, default='wallet')
    points_cost = Column(Integer, nullable=False, default=100)
    value = Column(Float, nullable=False, default=1.0)   # USD amount or % depending on type
    min_tier_level = Column(Integer, nullable=False, default=0)   # 0 = any tier
    max_claims_per_user = Column(Integer, nullable=False, default=0)  # 0 = unlimited
    max_total_claims = Column(Integer, nullable=False, default=0)    # 0 = unlimited
    total_claims = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    claims = relationship("LoyaltyRewardClaim", back_populates="reward",
                          cascade="all, delete-orphan")


class LoyaltyRewardClaim(Base):
    """Records every reward claim per user — V41."""
    __tablename__ = 'loyalty_reward_claims'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    reward_id = Column(Integer, ForeignKey('loyalty_rewards.id'), nullable=False, index=True)
    points_spent = Column(Integer, nullable=False)
    value_received = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User")
    reward = relationship("LoyaltyReward", back_populates="claims")


# ══════════════════════════════════════════════════════════════════════════════
# V41: API Key & Integration Manager
# ══════════════════════════════════════════════════════════════════════════════

class ApiIntegration(Base):
    """Centralised API / integration registry — V41.

    Credentials are stored masked: the full key is kept in ``api_key_masked``
    but only the 4-char hint is ever shown in the UI.
    status: 'enabled' | 'maintenance' | 'disabled'
    connection_status: 'connected' | 'slow' | 'warning' | 'offline' | 'unknown'
    api_type: 'telegram' | 'payment' | 'database' | 'smtp' | 'webhook' | 'custom'
    """
    __tablename__ = 'api_integrations'

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    provider = Column(String(64), nullable=False)
    api_type = Column(String(32), nullable=False, default='custom', index=True)

    # Credentials — raw values NEVER rendered in any UI message
    api_key_masked = Column(String(512), nullable=True)
    api_key_hint = Column(String(8), nullable=True)       # last 4 chars for display
    api_secret_masked = Column(String(512), nullable=True)
    api_secret_hint = Column(String(8), nullable=True)

    webhook_url = Column(String(512), nullable=True)
    base_url = Column(String(512), nullable=True)
    extra_config = Column(Text, nullable=True)             # JSON blob for extra fields

    status = Column(String(16), nullable=False, default='enabled', index=True)
    connection_status = Column(String(16), nullable=False, default='unknown', index=True)
    response_time_ms = Column(Integer, nullable=True)
    last_check_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error_message = Column(Text, nullable=True)
    version = Column(String(32), nullable=True)

    is_built_in = Column(Boolean, nullable=False, default=False)  # pre-seeded system entries
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    connection_logs = relationship(
        "ApiConnectionLog", back_populates="integration",
        cascade="all, delete-orphan",
    )


class ApiConnectionLog(Base):
    """Per-check connection log for an ApiIntegration — V41."""
    __tablename__ = 'api_connection_logs'

    id = Column(Integer, primary_key=True)
    integration_id = Column(
        Integer, ForeignKey('api_integrations.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    status = Column(String(16), nullable=False, index=True)
    response_time_ms = Column(Integer, nullable=True)
    http_status = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    checked_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    integration = relationship("ApiIntegration", back_populates="connection_logs")


# ══════════════════════════════════════════════════════════════════════════════
# V42: Plugin & Module Manager
# ══════════════════════════════════════════════════════════════════════════════

class ModuleConfig(Base):
    """Built-in module registry for the Plugin & Module Manager — V42.

    status: 'enabled' | 'maintenance' | 'disabled'
    Core modules (is_core=True) cannot be set to 'disabled'.
    dependencies: JSON array of sibling module slugs.
    """
    __tablename__ = 'module_configs'

    id              = Column(Integer, primary_key=True)
    slug            = Column(String(64), nullable=False, unique=True, index=True)
    name            = Column(String(128), nullable=False)
    version         = Column(String(32), nullable=True)
    description     = Column(Text, nullable=True)
    author          = Column(String(64), nullable=True)
    dependencies    = Column(Text, nullable=True)   # JSON array of slugs
    category        = Column(String(64), nullable=True, index=True)
    is_core         = Column(Boolean, nullable=False, default=False)
    # enabled | maintenance | disabled
    status          = Column(String(16), nullable=False, default='enabled', index=True)
    last_updated_at = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# V42: Global Activity Timeline
# ══════════════════════════════════════════════════════════════════════════════

class GlobalActivityEntry(Base):
    """Centralised system-wide audit trail — V42.

    Records every important action: user events, admin events, system events.
    Separate from activity_logs (user account history) and admin_audit_logs
    (admin-only actions).

    category examples: user | wallet | order | product | coupon | broadcast |
                       flash_sale | referral | admin | api | settings |
                       module | system
    status: 'success' | 'failed' | 'pending'
    """
    __tablename__ = 'global_activity_entries'

    id                  = Column(Integer, primary_key=True)
    user_id             = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                                 nullable=True, index=True)
    username            = Column(String(64), nullable=True)
    admin_telegram_id   = Column(BigInteger, nullable=True, index=True)
    action              = Column(String(64), nullable=False, index=True)
    category            = Column(String(32), nullable=False, index=True)
    description         = Column(Text, nullable=True)
    ip_address          = Column(String(45), nullable=True)
    status              = Column(String(16), nullable=False, default='success', index=True)
    ref_type            = Column(String(32), nullable=True)   # order | product | coupon | …
    ref_id              = Column(String(64), nullable=True)
    extra               = Column(Text, nullable=True)          # JSON blob
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User", foreign_keys=[user_id])


# ══════════════════════════════════════════════════════════════════════════════
# V43: Data Export Center
# ══════════════════════════════════════════════════════════════════════════════

class ExportJob(Base):
    """Export job record — tracks every export request lifecycle.

    status: 'pending' | 'running' | 'done' | 'failed' | 'scheduled'
    format: 'csv' | 'xlsx' | 'pdf' | 'json' | 'txt' | 'zip'
    export_type: see EXPORT_TYPES in services/data_export_service.py
    """
    __tablename__ = 'export_jobs'

    id                  = Column(Integer, primary_key=True)
    admin_telegram_id   = Column(BigInteger, nullable=False, index=True)
    export_type         = Column(String(32),  nullable=False)
    format              = Column(String(8),   nullable=False)
    status              = Column(String(16),  nullable=False, default='pending', index=True)
    filters             = Column(Text, nullable=True)          # JSON
    file_path           = Column(String(512), nullable=True)
    file_size           = Column(Integer, nullable=True)       # bytes
    row_count           = Column(Integer, nullable=True)
    error_message       = Column(Text, nullable=True)
    label               = Column(String(128), nullable=True)
    scheduled_at        = Column(DateTime, nullable=True)
    started_at          = Column(DateTime, nullable=True)
    completed_at        = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════════════
# V43: Global Search Engine
# ══════════════════════════════════════════════════════════════════════════════

class SearchRecord(Base):
    """Search history + saved searches for the Global Search Engine — V43.

    is_saved=True marks admin-pinned saved searches.
    label is the human-readable name for a saved search.
    """
    __tablename__ = 'search_records'

    id                  = Column(Integer, primary_key=True)
    admin_telegram_id   = Column(BigInteger, nullable=False, index=True)
    query               = Column(String(256), nullable=False, index=True)
    modules             = Column(Text, nullable=True)           # JSON array of slugs
    result_count        = Column(Integer, nullable=True, default=0)
    search_time_ms      = Column(Integer, nullable=True)
    is_saved            = Column(Boolean, nullable=False, default=False)
    label               = Column(String(128), nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════════════
# V44: Performance & Cache Manager
# ══════════════════════════════════════════════════════════════════════════════

class PerformanceSnapshot(Base):
    """Periodic snapshot of key system metrics — V44.

    Stored every N minutes by the PCM background job.
    health_label: 'Excellent' | 'Good' | 'Warning' | 'Critical'
    extra: JSON blob with supplementary fields (load avg, disk_free_gb, etc.)
    """
    __tablename__ = 'performance_snapshots'

    id           = Column(Integer, primary_key=True)
    cpu_pct      = Column(Float, nullable=True)
    mem_pct      = Column(Float, nullable=True)
    disk_pct     = Column(Float, nullable=True)
    db_ping_ms   = Column(Float, nullable=True)
    db_size_mb   = Column(Float, nullable=True)
    db_conn      = Column(Integer, nullable=True)
    uptime_s     = Column(Integer, nullable=True)
    health_score = Column(Integer, nullable=True)
    health_label = Column(String(16), nullable=True)
    extra        = Column(Text, nullable=True)          # JSON
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class OptimizationLog(Base):
    """Audit trail for every optimization action — V44.

    op_type examples: db_optimize | cache_clear | log_cleanup |
                      storage_cleanup | search_index | job_cleanup |
                      snapshot_cleanup | cache_optimize
    result: 'success' | 'failed'
    """
    __tablename__ = 'optimization_logs'

    id            = Column(Integer, primary_key=True)
    op_type       = Column(String(32), nullable=False, index=True)
    target        = Column(String(64), nullable=True)
    result        = Column(String(16), nullable=False)
    details       = Column(String(500), nullable=True)
    duration_ms   = Column(Integer, nullable=True)
    rows_affected = Column(Integer, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════════════
# V44.4: Enterprise Broadcast Campaign Manager
# ══════════════════════════════════════════════════════════════════════════════

class CampaignType(str, enum.Enum):
    SINGLE     = "single"
    MULTI_STEP = "multi_step"
    SCHEDULED  = "scheduled"
    RECURRING  = "recurring"
    DRIP       = "drip"
    SEASONAL   = "seasonal"


class CampaignStatus(str, enum.Enum):
    DRAFT     = "draft"
    SCHEDULED = "scheduled"
    RUNNING   = "running"
    PAUSED    = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED  = "archived"


class AutomationTrigger(str, enum.Enum):
    NEW_USER              = "new_user"
    FIRST_PURCHASE        = "first_purchase"
    USER_VIP              = "user_vip"
    WALLET_DEPOSIT        = "wallet_deposit"
    WALLET_LOW            = "wallet_low"
    PRODUCT_RESTOCKED     = "product_restocked"
    PRODUCT_PRICE_DROP    = "product_price_drop"
    COUPON_CREATED        = "coupon_created"
    COUPON_EXPIRING       = "coupon_expiring"
    SUBSCRIPTION_EXPIRING = "subscription_expiring"
    SUBSCRIPTION_EXPIRED  = "subscription_expired"
    REFERRAL_REWARD       = "referral_reward"
    FLASH_SALE_STARTED    = "flash_sale_started"
    FLASH_SALE_ENDING     = "flash_sale_ending"


class BroadcastTemplate(Base):
    """Reusable broadcast message template — V44.4."""
    __tablename__ = 'broadcast_templates'

    id             = Column(Integer, primary_key=True)
    name           = Column(String(100), nullable=False)
    category       = Column(String(64),  nullable=True, index=True)
    group_name     = Column(String(64),  nullable=True)
    message_text   = Column(Text, nullable=False)
    media_type     = Column(String(16),  nullable=False, default='text')
    button_text    = Column(String(64),  nullable=True)
    button_url     = Column(String(512), nullable=True)
    parse_mode     = Column(String(16),  nullable=False, default='HTML')
    variables_json = Column(Text, nullable=True)
    is_default     = Column(Boolean, default=False)
    is_favorite    = Column(Boolean, default=False)
    usage_count    = Column(Integer, default=0)
    created_by     = Column(BigInteger, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BroadcastCampaign(Base):
    """Campaign manager — single/multi-step/scheduled/recurring/drip/seasonal — V44.4."""
    __tablename__ = 'broadcast_campaigns'

    id                      = Column(Integer, primary_key=True)
    name                    = Column(String(100), nullable=False)
    campaign_type           = Column(String(32),  nullable=False, default='single')
    status                  = Column(String(32),  nullable=False, default='draft', index=True)
    template_id             = Column(Integer, ForeignKey('broadcast_templates.id', ondelete='SET NULL'), nullable=True)
    # scheduling
    start_date              = Column(DateTime, nullable=True)
    end_date                = Column(DateTime, nullable=True)
    timezone                = Column(String(64),  nullable=False, default='UTC')
    schedule_type           = Column(String(16),  nullable=True)
    schedule_interval_hours = Column(Integer, nullable=True)
    schedule_days_json      = Column(Text, nullable=True)
    # targeting
    target_segment        = Column(String(32),  nullable=False, default='all')
    audience_filters_json = Column(Text, nullable=True)
    # message
    message_text   = Column(Text, nullable=True)
    media_type     = Column(String(16),  nullable=False, default='text')
    file_id        = Column(String(256), nullable=True)
    button_text    = Column(String(64),  nullable=True)
    button_url     = Column(String(512), nullable=True)
    parse_mode     = Column(String(16),  nullable=False, default='HTML')
    variables_json = Column(Text, nullable=True)
    # A/B testing
    ab_test_enabled   = Column(Boolean, default=False)
    ab_variant_b_text = Column(Text,    nullable=True)
    ab_winner         = Column(String(1), nullable=True)
    ab_split_percent  = Column(Integer, default=50)
    ab_ctr_a          = Column(Integer, default=0)
    ab_ctr_b          = Column(Integer, default=0)
    # multi-step / drip
    steps_json = Column(Text, nullable=True)
    # stats
    total_runs      = Column(Integer, default=0)
    total_sent      = Column(Integer, default=0)
    total_delivered = Column(Integer, default=0)
    total_failed    = Column(Integer, default=0)
    last_run_at     = Column(DateTime, nullable=True)
    next_run_at     = Column(DateTime, nullable=True, index=True)
    is_archived     = Column(Boolean, default=False)
    created_by      = Column(BigInteger, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    executions = relationship("CampaignExecution", back_populates="campaign",
                              cascade="all, delete-orphan")


class CampaignExecution(Base):
    """Per-run execution history for a campaign — V44.4."""
    __tablename__ = 'campaign_executions'

    id               = Column(Integer, primary_key=True)
    campaign_id      = Column(Integer, ForeignKey('broadcast_campaigns.id', ondelete='CASCADE'),
                              nullable=False, index=True)
    step_index       = Column(Integer, nullable=False, default=0)
    status           = Column(String(16), nullable=False, default='running')
    started_at       = Column(DateTime, nullable=True)
    finished_at      = Column(DateTime, nullable=True)
    total_recipients = Column(Integer, default=0)
    sent             = Column(Integer, default=0)
    delivered        = Column(Integer, default=0)
    failed           = Column(Integer, default=0)
    ab_variant       = Column(String(1), nullable=True)
    ab_sent_a        = Column(Integer, default=0)
    ab_sent_b        = Column(Integer, default=0)
    error_log        = Column(Text, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("BroadcastCampaign", back_populates="executions")


class BroadcastAutomationRule(Base):
    """Event-driven automation rule — triggers a broadcast on a system event — V44.4."""
    __tablename__ = 'broadcast_automation_rules'

    id                 = Column(Integer, primary_key=True)
    name               = Column(String(100), nullable=False)
    trigger            = Column(String(64),  nullable=False, index=True)
    is_enabled         = Column(Boolean, default=True)
    template_id        = Column(Integer, ForeignKey('broadcast_templates.id', ondelete='SET NULL'), nullable=True)
    campaign_id        = Column(Integer, ForeignKey('broadcast_campaigns.id',  ondelete='SET NULL'), nullable=True)
    message_text       = Column(Text, nullable=True)
    media_type         = Column(String(16),  nullable=False, default='text')
    button_text        = Column(String(64),  nullable=True)
    button_url         = Column(String(512), nullable=True)
    parse_mode         = Column(String(16),  nullable=False, default='HTML')
    variables_json     = Column(Text, nullable=True)
    conditions_json    = Column(Text, nullable=True)
    delay_minutes      = Column(Integer, default=0)
    target_segment     = Column(String(32), nullable=False, default='trigger_user')
    dedup_window_hours = Column(Integer, default=24)
    trigger_count      = Column(Integer, default=0)
    last_triggered_at  = Column(DateTime, nullable=True)
    created_by         = Column(BigInteger, nullable=True)
    created_at         = Column(DateTime, default=datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AutomationTriggerLog(Base):
    """Deduplication log for automation trigger events — V44.4."""
    __tablename__ = 'automation_trigger_logs'

    id               = Column(Integer, primary_key=True)
    rule_id          = Column(Integer, ForeignKey('broadcast_automation_rules.id', ondelete='CASCADE'),
                              nullable=False, index=True)
    user_telegram_id = Column(BigInteger, nullable=True, index=True)
    trigger_key      = Column(String(128), nullable=True)
    sent             = Column(Boolean, default=False)
    triggered_at     = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# V45: Enterprise Features — Restock Notifications, Product Scheduler,
#      Recommendation Pins
# ══════════════════════════════════════════════════════════════════════════════

class RestockSubscription(Base):
    """User subscription to out-of-stock product restock alerts — V45."""
    __tablename__ = 'restock_subscriptions'
    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_restock_user_product'),
    )

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    product_id    = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    subscribed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    notified      = Column(Boolean, nullable=False, default=False, index=True)
    notified_at   = Column(DateTime, nullable=True)

    user    = relationship("User")
    product = relationship("Product")


class RestockNotificationLog(Base):
    """Log of every restock notification attempt — V45."""
    __tablename__ = 'restock_notification_logs'

    id                    = Column(Integer, primary_key=True)
    product_id            = Column(Integer, ForeignKey('products.id', ondelete='SET NULL'),
                                   nullable=True, index=True)
    product_name_snapshot = Column(String(255), nullable=True)
    telegram_id           = Column(BigInteger, nullable=False, index=True)
    # sent | failed
    status                = Column(String(16), nullable=False, default='sent', index=True)
    error_message         = Column(String(512), nullable=True)
    sent_at               = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ProductSchedule(Base):
    """Scheduled product change — V45.

    schedule_type: publish | unpublish | price_change | discount | stock_change
    status: pending | executed | failed | cancelled
    payload_json: JSON dict with type-specific fields (price, stock_count, etc.)
    """
    __tablename__ = 'product_schedules'

    id                    = Column(Integer, primary_key=True)
    product_id            = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                                   nullable=False, index=True)
    product_name_snapshot = Column(String(255), nullable=True)
    admin_id              = Column(BigInteger, nullable=False, index=True)
    schedule_type         = Column(String(32),  nullable=False, index=True)
    execute_at            = Column(DateTime, nullable=False, index=True)
    payload_json          = Column(Text, nullable=True)
    timezone_name         = Column(String(64), nullable=False, default='UTC')
    notes                 = Column(Text, nullable=True)
    # pending | executed | failed | cancelled
    status                = Column(String(16), nullable=False, default='pending', index=True)
    executed_at           = Column(DateTime, nullable=True)
    cancelled_at          = Column(DateTime, nullable=True)
    result_message        = Column(String(512), nullable=True)
    created_at            = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    product = relationship("Product")


class ProductRecommendationPin(Base):
    """Admin-pinned recommendation — V45.

    section: 'home' | 'trending' | 'fbt' | 'related' | custom string
    product_id: the source product (NULL means global / no source product)
    recommended_product_id: the product to recommend
    """
    __tablename__ = 'product_recommendation_pins'
    __table_args__ = (
        UniqueConstraint('section', 'product_id', 'recommended_product_id',
                         name='uq_rec_pin_section_prod'),
    )

    id                      = Column(Integer, primary_key=True)
    admin_id                = Column(BigInteger, nullable=False, index=True)
    section                 = Column(String(64),  nullable=False, index=True)
    product_id              = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                                     nullable=True, index=True)
    recommended_product_id  = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'),
                                     nullable=False, index=True)
    display_order           = Column(Integer, nullable=False, default=0)
    created_at              = Column(DateTime, default=datetime.utcnow, nullable=False)

    product             = relationship("Product", foreign_keys=[product_id])
    recommended_product = relationship("Product", foreign_keys=[recommended_product_id])
