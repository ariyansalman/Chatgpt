"""Helper utility functions for the Telegram bot."""

from datetime import datetime, timedelta
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import settings
from database import get_db_session, User

# In-memory cache for ban status (telegram_id: (is_banned, timestamp))
_ban_cache = {}
_BAN_CACHE_TTL = 30  # Cache ban status for 30 seconds


def is_admin(user_id: int) -> bool:
    """Check if a user is an admin (any tier: super_admin, moderator, or
    support_staff). Delegates to the multi-admin RBAC system in
    utils/permissions.py — kept here, under the original name and import
    path, so the ~135 existing call sites across handlers/admin_*.py keep
    working unchanged. The bootstrap owner (ADMIN_TELEGRAM_ID) always
    resolves to an implicit super_admin even with no DB rows yet.
    """
    from utils.permissions import is_admin as _rbac_is_admin
    return _rbac_is_admin(user_id)


def admin_only(func):
    """Decorator to restrict handler access to any admin tier.

    This is the *coarse* gate (no permission/2FA check) kept for backward
    compatibility. New code should prefer
    ``utils.permissions.require_permission("...")`` for granular,
    role-aware, 2FA-enforced access control.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            await update.message.reply_text("⛔ You don't have permission to access this command.")
            return
        return await func(update, context)
    return wrapper


def get_or_create_user(telegram_id: int, username: str = None, referrer_telegram_id: int = None):
    """Get existing user or create a new one in the database.

    If `referrer_telegram_id` is given AND the user is new AND the referrer exists
    AND it's not the same user, the referral link is stored.
    """
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()

        if not user:
            referrer_id = None
            if referrer_telegram_id and referrer_telegram_id != telegram_id:
                referrer = session.query(User).filter_by(telegram_id=referrer_telegram_id).first()
                if referrer:
                    referrer_id = referrer.id
            user = User(telegram_id=telegram_id, username=username, referred_by_id=referrer_id,
                       last_seen_at=datetime.utcnow())
            session.add(user)
            session.commit()
            session.refresh(user)

        return user


def format_order_id(order_id: int, created_at=None) -> str:
    """Return the canonical ORD-YYYYMMDD-NNNNNN display format for an order.

    This is for display purposes only — the database continues to use
    ``orders.id`` as the primary key.  The date component is taken from
    ``created_at`` (the order's creation timestamp) when available, or
    falls back to today's date so that the format always looks correct.

    Examples:
        format_order_id(166, datetime(2026, 7, 22)) → "ORD-20260722-000166"
        format_order_id(1)                          → "ORD-20260722-000001"
    """
    from datetime import datetime as _dt
    dt = created_at if created_at is not None else _dt.utcnow()
    return f"ORD-{dt.strftime('%Y%m%d')}-{order_id:06d}"


def format_deposit_id(deposit_id: int, created_at=None) -> str:
    """Return the canonical DEP-YYYYMMDD-NNNNNN display format for a deposit.

    Display purposes only — the database continues to use the deposit
    table's ``id`` as the primary key. Mirrors :func:`format_order_id`.
    """
    from datetime import datetime as _dt
    dt = created_at if created_at is not None else _dt.utcnow()
    return f"DEP-{dt.strftime('%Y%m%d')}-{deposit_id:06d}"


def format_withdrawal_id(withdrawal_id: int, created_at=None) -> str:
    """Return the canonical WDL-YYYYMMDD-NNNNNN display format for a withdrawal.

    Display purposes only — the database continues to use the withdrawal
    table's ``id`` as the primary key. Mirrors :func:`format_order_id`.
    """
    from datetime import datetime as _dt
    dt = created_at if created_at is not None else _dt.utcnow()
    return f"WDL-{dt.strftime('%Y%m%d')}-{withdrawal_id:06d}"


def parse_display_order_id(text: str):
    """Parse an ORD-YYYYMMDD-NNNNNN string and return the numeric order ID.

    Returns the integer order ID extracted from the formatted string, or
    ``None`` if the text does not match the ORD-YYYYMMDD-NNNNNN pattern.

    Example:
        parse_display_order_id("ORD-20260722-000166") → 166
    """
    import re as _re
    if not text or not isinstance(text, str):
        return None
    m = _re.match(r'^ORD-\d{8}-0*(\d+)$', text.strip(), _re.IGNORECASE)
    return int(m.group(1)) if m else None


def format_price(price: float) -> str:
    """Format price. If the admin configured a secondary display currency,
    append the converted amount, e.g.  `$12.50 (~৳1,375.00)`.
    Base value is always stored/priced in USD."""
    try:
        from utils.currency import format_price_multi
        return format_price_multi(price)
    except Exception:
        return f"${price:.2f}"


def format_datetime(dt: datetime) -> str:
    """Format datetime to readable string."""
    return dt.strftime("%b %d, %Y")


def calculate_expiry_time(hours: int = 1) -> datetime:
    """Calculate expiry datetime from now."""
    return datetime.utcnow() + timedelta(hours=hours)


def paginate_items(items, page: int, page_size: int = 5):
    """Paginate a list of items."""
    start = page * page_size
    end = start + page_size
    total_pages = (len(items) + page_size - 1) // page_size

    return {
        'items': items[start:end],
        'page': page,
        'total_pages': total_pages,
        'has_next': page < total_pages - 1,
        'has_prev': page > 0
    }


def validate_amount(amount_str: str) -> tuple[bool, float, str]:
    """Validate user input for payment amount."""
    try:
        amount = float(amount_str.strip())
        if amount <= 0:
            return False, 0, "Amount must be greater than zero."
        if amount > 100000:
            return False, 0, "Amount is too large. Maximum is $100,000."
        return True, amount, ""
    except ValueError:
        return False, 0, "Invalid amount. Please enter a valid number."


def catalog_stock_emoji(configured_emoji, available_stock: int) -> str:
    """Emoji shown on the flat "🛍 Products" catalog / search rows.

    ``configured_emoji`` is the raw ``Product.product_emoji`` value (string
    or None) — pass ``product.product_emoji`` directly. Out-of-stock
    (``available_stock <= 0``) always shows ❌, overriding whatever emoji is
    configured. In-stock uses the configured emoji if set, else a generic
    📦. The configured emoji is never modified in the database — only the
    displayed catalog glyph changes while stock is at zero.
    """
    if available_stock is None or available_stock <= 0:
        return "❌"
    configured = (configured_emoji or "").strip()
    return configured or "📦"


def format_product_button_text(emoji: str, name: str, price_display: str,
                                stock: int, max_len: int = 64) -> str:
    """Build one catalog/search row's inline button label:

        "{emoji} {name} | {price} | 📦 {stock}"

    Only ``name`` is ever shortened (with a trailing "…"), so emoji, price,
    and stock always stay fully visible — Telegram inline button labels are
    capped around 64 characters. Slicing happens on Python `str` code
    points, which is safe for multi-byte/multilingual names (Bengali, etc.)
    and won't corrupt characters, though very long combined-emoji sequences
    in a name could in principle be split — an acceptable, rare trade-off.
    """
    suffix = f" | {price_display} | 📦 {stock}"
    prefix = f"{emoji} " if emoji else ""
    budget = max_len - len(prefix) - len(suffix)
    if budget < 4:
        budget = 4
    clean_name = (name or "").strip()
    if len(clean_name) > budget:
        clean_name = clean_name[:max(1, budget - 1)].rstrip() + "…"
    return f"{prefix}{clean_name}{suffix}"


def format_product_display(product, include_description=False) -> str:
    """Format product information for display."""
    import html as _html

    name = _html.escape(product.name)
    if product.stock_count > 0:
        stock_line = f"🟢 {product.stock_count} In Stock"
    else:
        stock_line = "🔴 Out of Stock"

    lines = [
        f"🛒 <b>{name}</b>",
        "",
        f"💰 <b>Price:</b> {format_price(product.price)}",
        stock_line,
    ]

    if include_description and product.description:
        desc = product.description
        if len(desc) > 300:
            desc = desc[:297] + "…"
        lines.append(f"\n📝 {_html.escape(desc)}")

    return "\n".join(lines)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str, parse_mode: str = None):
    """Send notification message to admin."""
    try:
        await context.bot.send_message(
            chat_id=settings.ADMIN_TELEGRAM_ID,
            text=message,
            parse_mode=parse_mode,
        )
    except Exception as e:
        print(f"Error notifying admin: {e}")


def build_availability_text(products_by_category) -> str:
    """Build availability page text with products grouped by category."""
    text = "💬 Our available Products\n\n"

    for category_name, products in products_by_category.items():
        text += f"📦━━━━━{category_name}━━━━━📦\n"
        for product in products:
            text += f"{product.name} | {format_price(product.price)} | Available: {product.stock_count}\n"
        text += "\n"

    return text


def parse_keys_from_text(text: str) -> list:
    """Parse keys from text input (one key per line)."""
    keys = [line.strip() for line in text.split('\n') if line.strip()]
    return keys


def sanitize_message(text) -> str:
    """Normalize any notification payload to clean plain-text before sending.

    Handles the known failure modes:
    • 1-element tuple produced by a trailing comma in text=(...,)
    • list / multi-element tuple  → joined with newlines
    • JSON-encoded string         → decoded and extracted
    • Escaped unicode sequences   → decoded automatically by Python
    • Nested objects              → str() fallback
    """
    import json as _json

    # ── Unwrap list / tuple ──────────────────────────────────────────────
    if isinstance(text, (list, tuple)):
        if len(text) == 1:
            text = text[0]
        else:
            text = "\n".join(str(item) for item in text)

    # ── At this point text should be a string ────────────────────────────
    if not isinstance(text, str):
        text = str(text)

    # ── Unwrap JSON-encoded string (starts with [ or {) ──────────────────
    stripped = text.strip()
    if stripped and stripped[0] in ('[', '{', '"'):
        try:
            decoded = _json.loads(stripped)
            if isinstance(decoded, (list, tuple)):
                text = "\n".join(str(item) for item in decoded)
            elif isinstance(decoded, str):
                text = decoded
            elif isinstance(decoded, dict):
                # e.g. {"text": "..."} – try common keys first
                text = decoded.get("text") or decoded.get("message") or str(decoded)
        except (_json.JSONDecodeError, ValueError):
            pass  # not JSON – keep as-is

    # ── Unicode is already decoded by Python's str ────────────────────────
    # Ensure the result is always a non-None string
    return text if text else ""


def check_user_banned(telegram_id: int) -> bool:
    """Check if a user is banned (with caching for performance)."""
    global _ban_cache

    # Check cache first
    if telegram_id in _ban_cache:
        cached_value, cached_time = _ban_cache[telegram_id]
        # If cache is still valid (within TTL), return cached value
        if (datetime.utcnow() - cached_time).total_seconds() < _BAN_CACHE_TTL:
            return cached_value

    # Cache miss or expired - query database
    with get_db_session() as session:
        # Use .scalar() for better performance - only fetch is_banned column
        is_banned = session.query(User.is_banned).filter_by(telegram_id=telegram_id).scalar()
        result = bool(is_banned) if is_banned is not None else False

        # Update cache
        _ban_cache[telegram_id] = (result, datetime.utcnow())

        return result


def clear_ban_cache(telegram_id: int = None):
    """Clear ban cache for a specific user or all users (called when ban status changes)."""
    global _ban_cache
    if telegram_id is None:
        _ban_cache.clear()
    elif telegram_id in _ban_cache:
        del _ban_cache[telegram_id]
