"""
Enterprise Purchase Success Experience.

Builds the unified post-purchase success message used by all payment gateway
callbacks so customers always receive the same professional confirmation
regardless of how they paid.

Public API
----------
  generate_receipt_number(order_id)          → str  (ORD-YYYYMMDD-NNNNNN)
  get_or_create_receipt(order_id, user_id)   → str  (idempotent)
  build_success_text(...)                    → str
  build_success_keyboard(...)                → InlineKeyboardMarkup
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from database import get_db_session
from database.models import OrderItem, OrderReceipt, Product
from utils import format_price

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# URL detection
# ──────────────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s\)\]\}\"\'<>]+", re.IGNORECASE)

# Matches legacy pipe-delimited ACCOUNT_LOGIN  e.g. "user@example.com|password"
_PIPE_ACCOUNT_RE = re.compile(r"^[^\|\n]{3,}@[^\|\n]+\|.+", re.MULTILINE)


def extract_urls(text: str) -> List[str]:
    """Return all distinct http/https URLs found in *text*, trailing punctuation stripped."""
    if not text:
        return []
    seen: List[str] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?)")
        if url and url not in seen:
            seen.append(url)
    return seen


# ──────────────────────────────────────────────────────────────────────────────
# Receipt number generation
# ──────────────────────────────────────────────────────────────────────────────

def generate_receipt_number(order_id: int) -> str:
    """Return the canonical order display ID ``ORD-YYYYMMDD-NNNNNN`` for *order_id*.

    This is stored as the receipt_number in OrderReceipt and is the single
    display format used everywhere in the UI.  The database primary key
    (orders.id) is never exposed directly to customers.
    """
    from utils.helpers import format_order_id
    return format_order_id(order_id)


def get_or_create_receipt(order_id: int, user_id: int) -> str:
    """Store an :class:`OrderReceipt` row and return its ``receipt_number`` (idempotent).

    If a receipt row already exists for *order_id* the existing number is
    returned unchanged so repeated calls never create duplicates.
    """
    receipt_number = generate_receipt_number(order_id)
    try:
        with get_db_session() as s:
            existing = s.query(OrderReceipt).filter_by(order_id=order_id).first()
            if existing:
                return existing.receipt_number
            s.add(OrderReceipt(
                receipt_number=receipt_number,
                order_id=order_id,
                user_id=user_id,
                receipt_type="purchase",
            ))
            s.commit()
    except Exception:
        logger.exception("Failed to store receipt for order %s", order_id)
    return receipt_number


def get_receipt_number(order_id: int) -> Optional[str]:
    """Return the stored receipt number for *order_id*, or ``None`` if not found."""
    try:
        with get_db_session() as s:
            row = s.query(OrderReceipt).filter_by(order_id=order_id).first()
            return row.receipt_number if row else None
    except Exception:
        logger.debug("receipt lookup failed for order %s", order_id)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Delivery section helpers
# ──────────────────────────────────────────────────────────────────────────────

def _delivery_label(delivered_asset: Optional[str], product_type: Optional[str]) -> str:
    """Choose the appropriate section header based on product type and content."""
    pt = (product_type or "").upper()
    if "REDEEM" in pt:
        return "🔗 Redeem Link"
    if "ACCOUNT" in pt or "LOGIN" in pt:
        return "🔑 Account Details"
    if "FILE" in pt or "DOWNLOAD" in pt:
        return "📥 Download Link"
    if "VOUCHER" in pt:
        return "🎁 Voucher Code"
    if "SUBSCRIPTION" in pt:
        return "📋 Subscription Details"
    if "SERVICE" in pt:
        return "⚙️ Service Details"
    if "BUNDLE" in pt:
        return "📦 Bundle Contents"
    # Auto-detect from content
    if delivered_asset:
        if extract_urls(delivered_asset):
            return "🔗 Activation Link"
        if _PIPE_ACCOUNT_RE.search(delivered_asset):
            return "🔑 Account Details"
    return "🔑 License Key"


def _warranty_text(product_id: Optional[int]) -> str:
    """Return a formatted warranty line if the product has ``warranty_info`` set."""
    if not product_id:
        return ""
    try:
        with get_db_session() as s:
            p = s.query(Product).filter_by(id=product_id).first()
            if p and p.warranty_info and p.warranty_info.strip():
                return f"\n🛡 Warranty:  {p.warranty_info.strip()}"
    except Exception:
        pass
    return ""


def _activation_instructions(delivered_asset: Optional[str]) -> str:
    """Return a short activation guide when the delivery contains a URL."""
    if not delivered_asset:
        return ""
    if not extract_urls(delivered_asset):
        return ""
    return (
        "\n\n📋 Activation Steps\n"
        "  1. Open the link above.\n"
        "  2. Sign in or create an account.\n"
        "  3. Complete activation."
    )


def _format_delivery_block(
    delivered_asset: Optional[str],
    product_type: Optional[str] = None,
    product_id: Optional[int] = None,
) -> str:
    """Build the delivery section: label + content only."""
    if not delivered_asset or not delivered_asset.strip():
        return ""
    label = _delivery_label(delivered_asset, product_type)
    return f"\n🔑 Delivery\n\n{label}\n{delivered_asset.strip()}"


# ──────────────────────────────────────────────────────────────────────────────
# Universal oversized-delivery → .txt file fallback
# ──────────────────────────────────────────────────────────────────────────────
# Legacy KEY-type bulk purchases have long offloaded oversized deliveries to a
# .txt file (see handlers/payment_handlers.py / cart_handlers.py bulk_keys /
# bulk_payloads). The 11 newer dispatcher-backed product types (REDEEM_LINK,
# ACCOUNT_LOGIN, VOUCHER, AUTO_GENERATED, DOWNLOADABLE_FILE, etc. — see
# services/delivery_service.py) had no equivalent safety net: a large-quantity
# purchase could produce a user_message that exceeds Telegram's ~4096 char
# message limit and fail to send. These two helpers are shared by both
# purchase call sites so every product type gets the same protection without
# duplicating the file-writing logic.

# Conservative vs Telegram's 4096 hard cap — leaves headroom for the order
# header/receipt/footer text that gets prepended around the delivered content.
DELIVERY_INLINE_CHAR_LIMIT = 3000


def is_delivery_oversized(content: Optional[str], limit: int = DELIVERY_INLINE_CHAR_LIMIT) -> bool:
    """True when delivered content is too large to safely inline in a single
    Telegram message alongside the rest of the order summary."""
    return bool(content) and len(content) > limit


async def send_delivery_as_file(
    bot,
    chat_id: int,
    order_id: int,
    product_name: str,
    content: str,
    caption: Optional[str] = None,
    admin_chat_id: Optional[int] = None,
) -> bool:
    """Write ``content`` to a temp .txt file and send it as a Telegram
    document — the same fallback legacy bulk KEY delivery already uses,
    generalized so any delivery type can use it. Returns True once the buyer
    has received the file (a best-effort admin copy failing does not flip
    this back to False).
    """
    import os
    import tempfile
    from telegram import InputFile

    safe_name = "".join(
        c for c in (product_name or "product") if c.isalnum() or c in ("-", "_")
    )[:40] or "product"
    filename = f"order_{order_id}_{safe_name}.txt"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)
    delivered_ok = False
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        with open(tmp_path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(f, filename=filename),
                caption=caption or f"📎 Delivery for order #{order_id}",
            )
        delivered_ok = True
        if admin_chat_id:
            try:
                with open(tmp_path, "rb") as f:
                    await bot.send_document(
                        chat_id=admin_chat_id,
                        document=InputFile(f, filename=filename),
                        caption=f"📎 Bulk delivery copy — order #{order_id}",
                    )
            except Exception:
                logger.exception(
                    "send_delivery_as_file: admin copy failed for order %s", order_id
                )
    except Exception:
        logger.exception("send_delivery_as_file failed for order %s", order_id)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return delivered_ok


# ──────────────────────────────────────────────────────────────────────────────
# Success message builders
# ──────────────────────────────────────────────────────────────────────────────

def build_success_text(
    *,
    order_id: int,
    product_name: str,
    quantity: int,
    total: float,
    receipt_number: str,
    delivered_asset: Optional[str] = None,
    product_type: Optional[str] = None,
    product_id: Optional[int] = None,
    purchase_date: Optional[datetime] = None,
) -> str:
    """Return the single unified purchase-success message text."""
    qty_suffix = f" ×{quantity}" if quantity > 1 else ""
    date_str = (purchase_date or datetime.utcnow()).strftime("%d %b %Y • %H:%M UTC")

    lines: List[str] = [
        "✅ Payment Successful",
        "",
        f"🧾 Order ID\n{receipt_number}",
        f"📅 {date_str}",
        "",
        f"📦 Product\n{product_name}{qty_suffix}",
        "",
        f"💰 Amount Paid\n{format_price(total)}",
    ]

    delivery_block = _format_delivery_block(delivered_asset, product_type, product_id)
    if delivery_block:
        lines.append(delivery_block)

    lines.append("")
    lines.append("Thank you for your purchase!")

    return "\n".join(lines)


def build_success_keyboard(
    *,
    order_id: int,
    product_id: Optional[int] = None,
    delivered_asset: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the purchase success message.

    URL-count rules:
      • 0 URLs        → no link buttons
      • Exactly 1 URL → 🌐 Open Link  +  📋 Copy Link  (native clipboard, no new message)
      • 2+ URLs       → 📥 Download Links  (sends a TXT file — no Message_too_long risk)
                        Open Link is hidden when there are multiple links.

    Other buttons (always present):
      • 🏠 Home | 📦 My Orders
      • 🛒 Buy Again   (only when product_id is known)
      • ⭐ Leave Review (only when product_id is known)
      • 🆘 Support
    """
    urls = extract_urls(delivered_asset or "")
    url_count = len(urls)
    rows: list = []

    if url_count == 1:
        # Single URL: Open Link (url button) + Copy Link (CopyTextButton — native clipboard, no new message)
        rows.append([
            InlineKeyboardButton("🌐 Open Link", url=urls[0]),
            InlineKeyboardButton("📋 Copy Link", copy_text=CopyTextButton(text=urls[0])),
        ])
    elif url_count > 1:
        # Multiple URLs: Download as TXT file — never put links in a callback alert
        # (answerCallbackQuery text limit is ~200 chars; long URLs cause Message_too_long)
        rows.append([
            InlineKeyboardButton("📥 Download Links", callback_data=f"download_links_txt_{order_id}"),
        ])
    # url_count == 0 → no link buttons

    # Home + My Orders
    rows.append([
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu"),
        InlineKeyboardButton("📦 My Orders", callback_data=f"user_order_detail_{order_id}"),
    ])

    # Buy Again
    if product_id:
        rows.append([InlineKeyboardButton(
            "🛒 Buy Again", callback_data=f"product_{product_id}",
        )])

    # Review
    if product_id:
        rows.append([InlineKeyboardButton(
            "⭐ Leave Review",
            callback_data=f"review_start_{order_id}_{product_id}",
        )])

    # Support
    rows.append([InlineKeyboardButton("🆘 Support", callback_data="support_center")])

    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Copy-link callback handlers
# ──────────────────────────────────────────────────────────────────────────────

def _get_delivered_asset(order_id: int) -> Optional[str]:
    """Fetch the first ``OrderItem.delivered_asset`` for *order_id* from the DB."""
    try:
        with get_db_session() as s:
            item = (
                s.query(OrderItem)
                .filter(OrderItem.order_id == order_id)
                .order_by(OrderItem.id.asc())
                .first()
            )
            return item.delivered_asset if item else None
    except Exception:
        logger.exception("_get_delivered_asset: DB error for order %s", order_id)
        return None


async def copy_link_callback(update, context) -> None:
    """Handle 📋 Copy Link (legacy callback fallback) — show the URL in a popup alert.

    New buttons use CopyTextButton for native clipboard copy without any message.
    This handler remains for backward compatibility with older in-chat buttons that
    still carry a ``copy_link_*`` callback_data.

    callback_data pattern: ``copy_link_{order_id}``
    """
    query = update.callback_query
    try:
        order_id = int(query.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    asset = _get_delivered_asset(order_id)
    urls = extract_urls(asset or "")
    if not urls:
        await query.answer("No link found for this order.", show_alert=True)
        return

    # Show the single link in a native Telegram popup — no new message is sent.
    # Truncate to Telegram's 200-char answerCallbackQuery limit to be safe.
    await query.answer(urls[0][:200], show_alert=True)


async def download_links_txt_callback(update, context) -> None:
    """Handle 📥 Download Links — send all delivery URLs as a TXT file.

    This avoids the ``Message_too_long`` error that occurs when putting multiple
    long URLs into ``answerCallbackQuery`` (Telegram hard limit: ~200 chars).
    A plain ``.txt`` file is sent so the user can open / copy all links easily.

    callback_data pattern: ``download_links_txt_{order_id}``
    """
    import io
    query = update.callback_query
    await query.answer()

    try:
        order_id = int(query.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    asset = _get_delivered_asset(order_id)
    urls = extract_urls(asset or "")
    if not urls:
        await query.answer("No links found for this order.", show_alert=True)
        return

    content = "\n".join(urls)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"links_order_{order_id}.txt"

    try:
        await query.message.reply_document(
            document=buf,
            filename=f"links_order_{order_id}.txt",
            caption=f"🔗 {len(urls)} link(s) for Order #{order_id}",
        )
    except Exception:
        logger.exception("download_links_txt_callback: failed to send TXT for order %s", order_id)
        await query.answer("❌ Could not generate the links file.", show_alert=True)


async def copy_all_links_callback(update, context) -> None:
    """Handle 📋 Copy All Links (legacy backward-compat) — redirect to TXT file download.

    Old in-chat buttons may still carry ``copy_all_links_{order_id}`` callback_data.
    Putting multiple links into ``answerCallbackQuery`` exceeds Telegram's ~200-char
    text limit and causes ``Message_too_long``.  We redirect to the TXT file approach
    so existing buttons continue to work safely without sending an extra text message.

    callback_data pattern: ``copy_all_links_{order_id}``
    """
    import io
    query = update.callback_query
    await query.answer()

    try:
        order_id = int(query.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    asset = _get_delivered_asset(order_id)
    urls = extract_urls(asset or "")
    if not urls:
        await query.answer("No links found for this order.", show_alert=True)
        return

    # Send a TXT file — never put multiple URLs in an alert to avoid Message_too_long
    content = "\n".join(urls)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"links_order_{order_id}.txt"

    try:
        await query.message.reply_document(
            document=buf,
            filename=f"links_order_{order_id}.txt",
            caption=f"🔗 {len(urls)} link(s) for Order #{order_id}",
        )
    except Exception:
        logger.exception("copy_all_links_callback: failed to send TXT for order %s", order_id)
        await query.answer("❌ Could not generate the links file.", show_alert=True)
