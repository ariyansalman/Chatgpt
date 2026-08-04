"""Product Information Builder Service — V48

Provides per-product information blocks that display on the product detail page
and in the Product Information step of the purchase flow. Every piece of text
visible to users on the info page is stored in the database and editable from
the Admin Panel — no content is hardcoded.

DO NOT modify delivery formatters, payment logic, or order-creation flow.
This service only provides UI rendering and settings helpers.
"""

from __future__ import annotations

import html as _html
import json
import logging
from typing import Optional

from database import get_db_session
from utils.bot_config import cfg as _cfg

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Block Types
# ─────────────────────────────────────────────────────────────────────────────
BLOCK_TYPES: dict[str, str] = {
    "text":             "📄 Plain Text",
    "bold_text":        "𝐁 Bold Text",
    "italic_text":      "𝐼 Italic Text",
    "underline_text":   "U̲ Underline Text",
    "spoiler":          "🙈 Spoiler",
    "quote":            "💬 Quote",
    "expandable_quote": "📂 Expandable Quote",
    "bullet_list":      "• Bullet List",
    "number_list":      "1. Numbered List",
    "divider":          "─── Divider",
    "html":             "🖥 HTML (Advanced)",
}

BLOCK_TYPE_KEYS = list(BLOCK_TYPES.keys())

# ─────────────────────────────────────────────────────────────────────────────
# Accent Colors
# ─────────────────────────────────────────────────────────────────────────────
ACCENT_COLORS: dict[str, str] = {
    "none":    "⬜ None",
    "blue":    "🔵 Blue",
    "green":   "🟢 Green",
    "red":     "🔴 Red",
    "yellow":  "🟡 Yellow",
    "purple":  "🟣 Purple",
    "orange":  "🟠 Orange",
}

# ─────────────────────────────────────────────────────────────────────────────
# Global Visibility Settings  (stored via bot_config)
# ─────────────────────────────────────────────────────────────────────────────
# key → (label, default_visible)
VISIBILITY_KEYS: dict[str, tuple[str, bool]] = {
    "show_price":         ("💰 Price",            True),
    "show_old_price":     ("❌ Old Price",         True),
    "show_discount":      ("🏷 Discount Badge",    True),
    "show_status":        ("🟢 Status",            True),
    "show_stock":         ("📦 Stock Count",       True),
    "show_duration":      ("⏳ Duration",          True),
    "show_warranty":      ("🛡 Warranty",          True),
    "show_delivery_type": ("⚡ Delivery Type",     True),
    "show_delivery_fmt":  ("🎁 Delivery Format",   True),
    "show_info_blocks":   ("📋 Info Blocks",       True),
    "show_buy_button":    ("🛒 Buy Button",        True),
    "show_back_button":   ("🔙 Back Button",       True),
}

_CFG_PREFIX = "pib_vis_"


def get_visibility(key: str) -> bool:
    """Return current admin visibility toggle for a display field."""
    default = VISIBILITY_KEYS.get(key, ("", True))[1]
    try:
        return _cfg.get_bool(_CFG_PREFIX + key, default)
    except Exception:
        return default


def set_visibility(key: str, value: bool) -> None:
    try:
        _cfg.set(_CFG_PREFIX + key, "true" if value else "false")
    except Exception as exc:
        logger.warning("PIB: could not save visibility %s: %s", key, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Per-product purchase-flow settings  (stored as JSON in Product.pib_settings)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_PURCHASE_SETTINGS: dict = {
    "show_info_before_purchase": True,
    "skip_if_no_blocks":         True,
    "require_scroll":            False,
    "show_confirm_checkbox":     False,
    "show_continue_button":      True,
}


def get_purchase_settings(product_id: int) -> dict:
    """Return per-product purchase-flow settings merged with defaults."""
    try:
        from database.models import Product as _P
        with get_db_session() as session:
            p = session.query(_P).filter_by(id=product_id).first()
            if not p:
                return dict(_DEFAULT_PURCHASE_SETTINGS)
            raw = getattr(p, "pib_settings", None)
            if raw:
                try:
                    return {**_DEFAULT_PURCHASE_SETTINGS, **json.loads(raw)}
                except (ValueError, TypeError):
                    pass
    except Exception as exc:
        logger.warning("PIB: could not load purchase settings for %s: %s", product_id, exc)
    return dict(_DEFAULT_PURCHASE_SETTINGS)


def save_purchase_settings(product_id: int, settings: dict) -> None:
    """Persist per-product purchase-flow settings."""
    try:
        from database.models import Product as _P
        with get_db_session() as session:
            p = session.query(_P).filter_by(id=product_id).first()
            if p:
                current_raw = getattr(p, "pib_settings", None)
                current = {}
                if current_raw:
                    try:
                        current = json.loads(current_raw)
                    except (ValueError, TypeError):
                        pass
                current.update(settings)
                p.pib_settings = json.dumps(current)  # type: ignore[attr-defined]
                session.commit()
    except Exception as exc:
        logger.warning("PIB: could not save purchase settings for %s: %s", product_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Block Rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_block_html(block) -> str:
    """Render a single ProductInfoBlock to Telegram HTML."""
    btype = (block.block_type or "text").lower().strip()
    content = (block.content or "").strip()
    title   = (block.title   or "").strip()
    emoji   = (block.emoji   or "").strip()

    # ── Divider ──────────────────────────────────────────────────────────
    if btype == "divider":
        return "─────────────────────────────────"

    # ── Title line ───────────────────────────────────────────────────────
    title_html = ""
    if title:
        safe_title = _html.escape(title)
        title_html = (f"{emoji} <b>{safe_title}</b>" if emoji
                      else f"<b>{safe_title}</b>")

    # ── Body ─────────────────────────────────────────────────────────────
    if btype == "html":
        body = content          # admin-supplied raw HTML
    elif btype == "bold_text":
        body = f"<b>{_html.escape(content)}</b>"
    elif btype == "italic_text":
        body = f"<i>{_html.escape(content)}</i>"
    elif btype == "underline_text":
        body = f"<u>{_html.escape(content)}</u>"
    elif btype == "spoiler":
        body = f"<tg-spoiler>{_html.escape(content)}</tg-spoiler>"
    elif btype == "quote":
        body = f"<blockquote>{_html.escape(content)}</blockquote>"
    elif btype == "expandable_quote":
        body = f"<blockquote expandable>{_html.escape(content)}</blockquote>"
    elif btype == "bullet_list":
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        body = "\n".join(f"• {_html.escape(l)}" for l in lines)
    elif btype == "number_list":
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        body = "\n".join(f"{i + 1}. {_html.escape(l)}" for i, l in enumerate(lines))
    else:
        # plain text
        body = _html.escape(content)

    if title_html and body:
        return f"{title_html}\n{body}"
    return title_html or body


def render_product_info_page(product_id: int) -> tuple[str, int]:
    """Render all visible info blocks for a product as a single HTML string.

    Returns (html_text, block_count).  block_count is the raw count of
    *visible* blocks, used by callers to decide whether to show the info page
    at all when skip_if_no_blocks is True.
    """
    if not get_visibility("show_info_blocks"):
        return "", 0

    try:
        from database.models import ProductInfoBlock as _PIB
        with get_db_session() as session:
            blocks = (
                session.query(_PIB)
                .filter_by(product_id=product_id, is_visible=True)
                .order_by(_PIB.display_order, _PIB.id)
                .all()
            )
            if not blocks:
                return "", 0
            parts = []
            for b in blocks:
                rendered = render_block_html(b)
                if rendered.strip():
                    parts.append(rendered)
            return "\n\n".join(parts), len(blocks)
    except Exception as exc:
        logger.warning("PIB: render_product_info_page(%s) failed: %s", product_id, exc)
        return "", 0


def has_info_blocks(product_id: int) -> bool:
    """Return True when the product has at least one visible info block."""
    try:
        from database.models import ProductInfoBlock as _PIB
        with get_db_session() as session:
            count = (session.query(_PIB)
                     .filter_by(product_id=product_id, is_visible=True)
                     .count())
            return count > 0
    except Exception:
        return False


def count_all_blocks(product_id: int) -> int:
    """Return total block count (visible + hidden) for admin UI."""
    try:
        from database.models import ProductInfoBlock as _PIB
        with get_db_session() as session:
            return (session.query(_PIB).filter_by(product_id=product_id).count())
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Enhanced Product Card
# ─────────────────────────────────────────────────────────────────────────────

def format_product_detail_card(product, telegram_id: int = None) -> str:
    """Render a rich product-detail card HTML.

    Called from the product detail view when PIB is active. Respects every
    admin visibility toggle. Falls back gracefully if fields are missing.
    Does NOT touch the delivery formatter, payment flow, or order logic.
    """
    import html as _h

    name = _h.escape(product.name or "")
    lines: list[str] = [f"🛍️ <b>{name}</b>"]

    # ── Price ─────────────────────────────────────────────────────────────
    try:
        from utils.helpers import format_price
        if get_visibility("show_price"):
            price = product.price

            # Determine old/sale price
            old_price: Optional[float] = None
            discount_pct: Optional[float] = None
            sale_price = getattr(product, "sale_price", None)
            bundle_discount = getattr(product, "bundle_discount_percent", None)

            if sale_price and float(sale_price) > float(price):
                old_price = float(sale_price)
                discount_pct = round((1.0 - float(price) / float(old_price)) * 100.0)
            elif bundle_discount:
                discount_pct = float(bundle_discount)

            price_line = f"💰 <b>Price:</b> {format_price(price)}"
            if get_visibility("show_old_price") and old_price:
                price_line += f"  <s>{format_price(old_price)}</s>"
            if get_visibility("show_discount") and discount_pct:
                price_line += f"  🏷 <b>{discount_pct:.0f}% OFF</b>"
            lines.append(price_line)
    except Exception:
        pass

    # ── Status & Stock ────────────────────────────────────────────────────
    stock = getattr(product, "stock_count", 0) or 0
    status_parts: list[str] = []
    if get_visibility("show_status"):
        if stock > 0:
            status_parts.append("🟢 <b>Status:</b> Available")
        else:
            status_parts.append("🔴 <b>Status:</b> Out of Stock")
    if get_visibility("show_stock") and stock > 0:
        status_parts.append(f"📦 <b>Stock:</b> {stock}")
    if status_parts:
        lines.append("  │  ".join(status_parts))

    # ── Delivery Type ─────────────────────────────────────────────────────
    if get_visibility("show_delivery_type"):
        try:
            ptype = getattr(product, "product_type", None)
            if ptype:
                _TYPE_LABELS = {
                    "KEY":               "🔑 Software Key",
                    "FILE":              "📁 File (Legacy)",
                    "REDEEM_LINK":       "🔗 Redeem Link",
                    "ACCOUNT_LOGIN":     "📧 Account Login",
                    "DOWNLOADABLE_FILE": "📁 Digital Download",
                    "AUTO_GENERATED":    "🤖 Auto Generated",
                    "MANUAL_DELIVERY":   "👤 Manual Delivery",
                    "PREORDER":          "⏳ Pre-Order",
                    "SUBSCRIPTION":      "♻️ Subscription",
                    "BUNDLE":            "📦 Bundle",
                    "SERVICE":           "🛠️ Service",
                    "VOUCHER":           "🎟️ Voucher",
                    "EXTERNAL_DELIVERY": "🌐 External Delivery",
                }
                pname = ptype.name if hasattr(ptype, "name") else str(ptype)
                label = _TYPE_LABELS.get(pname, pname.replace("_", " ").title())
                lines.append(f"⚡ <b>Delivery:</b> {label}")
        except Exception:
            pass

    # ── Delivery Format ───────────────────────────────────────────────────
    if get_visibility("show_delivery_fmt"):
        try:
            fmt = getattr(product, "delivery_format_template", None)
            if fmt:
                lines.append("🎁 <b>Format:</b> Structured Delivery")
        except Exception:
            pass

    # ── Warranty ──────────────────────────────────────────────────────────
    if get_visibility("show_warranty"):
        try:
            warranty = getattr(product, "warranty_info", None)
            if warranty:
                w = warranty.strip()
                short = (w[:120] + "…") if len(w) > 120 else w
                lines.append(f"🛡 <b>Warranty:</b> {_h.escape(short)}")
        except Exception:
            pass

    # ── Duration (subscription products) ─────────────────────────────────
    if get_visibility("show_duration"):
        try:
            ptype = getattr(product, "product_type", None)
            if ptype and hasattr(ptype, "name") and ptype.name == "SUBSCRIPTION":
                lines.append("⏳ <b>Duration:</b> See plans below")
        except Exception:
            pass

    # ── Description ───────────────────────────────────────────────────────
    try:
        desc = getattr(product, "description", None)
        if desc:
            d = desc.strip()
            short = (d[:300] + "…") if len(d) > 300 else d
            lines.append(f"\n📝 {_h.escape(short)}")
    except Exception:
        pass

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Template Helpers
# ─────────────────────────────────────────────────────────────────────────────

def apply_template_to_product(template_id: int, product_id: int,
                               append: bool = False) -> tuple[bool, str]:
    """Copy all blocks from a template to a product.

    If append=False (default), existing blocks for the product are removed first.
    Returns (success, message).
    """
    try:
        from database.models import (
            ProductInfoTemplate as _TPL,
            ProductInfoTemplateBlock as _TPLB,
            ProductInfoBlock as _PIB,
        )
        from datetime import datetime

        with get_db_session() as session:
            tpl = session.query(_TPL).filter_by(id=template_id).first()
            if not tpl:
                return False, "Template not found."
            tbl_blocks = (session.query(_TPLB)
                          .filter_by(template_id=template_id)
                          .order_by(_TPLB.display_order)
                          .all())

            if not append:
                (session.query(_PIB)
                 .filter_by(product_id=product_id)
                 .delete())

            # Find starting order
            max_order = 0
            if append:
                from sqlalchemy import func
                result = (session.query(func.max(_PIB.display_order))
                          .filter_by(product_id=product_id)
                          .scalar())
                max_order = (result or 0) + 10

            for i, tb in enumerate(tbl_blocks):
                new_block = _PIB(
                    product_id=product_id,
                    title=tb.title,
                    emoji=tb.emoji,
                    content=tb.content,
                    block_type=tb.block_type,
                    accent_color=tb.accent_color,
                    is_bold=tb.is_bold,
                    is_italic=tb.is_italic,
                    has_spoiler=tb.has_spoiler,
                    is_visible=tb.is_visible,
                    display_order=max_order + (i * 10),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                session.add(new_block)
            session.commit()
        return True, f"✅ Template <b>{_html.escape(tpl.name)}</b> applied ({len(tbl_blocks)} blocks)."
    except Exception as exc:
        logger.exception("PIB: apply_template_to_product failed")
        return False, f"❌ Error: {exc}"


def save_product_blocks_as_template(product_id: int, template_name: str) -> tuple[bool, str]:
    """Save a product's current blocks as a new reusable template."""
    try:
        from database.models import (
            ProductInfoBlock as _PIB,
            ProductInfoTemplate as _TPL,
            ProductInfoTemplateBlock as _TPLB,
        )
        from datetime import datetime

        with get_db_session() as session:
            blocks = (session.query(_PIB)
                      .filter_by(product_id=product_id)
                      .order_by(_PIB.display_order, _PIB.id)
                      .all())
            tpl = _TPL(
                name=template_name,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(tpl)
            session.flush()  # get tpl.id

            for i, b in enumerate(blocks):
                tb = _TPLB(
                    template_id=tpl.id,
                    title=b.title,
                    emoji=b.emoji,
                    content=b.content,
                    block_type=b.block_type,
                    accent_color=b.accent_color,
                    is_bold=b.is_bold,
                    is_italic=b.is_italic,
                    has_spoiler=b.has_spoiler,
                    is_visible=b.is_visible,
                    display_order=i * 10,
                )
                session.add(tb)
            session.commit()
            return True, f"✅ Saved as template <b>{_html.escape(template_name)}</b> ({len(blocks)} blocks)."
    except Exception as exc:
        logger.exception("PIB: save_product_blocks_as_template failed")
        return False, f"❌ Error: {exc}"
