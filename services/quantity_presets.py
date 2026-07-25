"""Section 9 — dynamic quantity preset builder.

Given the real available stock plus product/variant min/max constraints,
return a list of preset quantities suitable for building keyboard buttons.

Rules:
* Never present a quantity above real availability.
* Respect Product.min_quantity / Product.max_quantity when set.
* Reusable File / Manual / Service / Pre-Order / Subscription products
  are not stock-limited; fall back to a sensible max.
* Include a "Custom" entry as a caller-controlled string sentinel.
"""
from __future__ import annotations

from typing import List, Optional

from database.models import Product

# Preset ladder used before availability filtering.
_LADDER = [1, 2, 3, 4, 5, 10, 15, 20, 25, 50, 100]

_UNLIMITED_TYPES = {
    "manual_delivery", "service", "pre_order", "subscription",
    "auto_generated", "external_delivery",
}


def build_presets(product: Product,
                  available: Optional[int] = None,
                  cap: int = 100) -> List[int]:
    """Return the preset quantity ladder for this product."""
    ptype_val = getattr(product.product_type, "value",
                        str(product.product_type or "")).lower()
    lo = int(product.min_quantity or 1)
    hi_raw = product.max_quantity
    if ptype_val in _UNLIMITED_TYPES or getattr(product, "reusable", False):
        # No unique-inventory pressure — use configured cap.
        hi = int(hi_raw or cap)
    else:
        hi_candidates = [cap]
        if available is not None:
            hi_candidates.append(int(available))
        if hi_raw:
            hi_candidates.append(int(hi_raw))
        hi = max(0, min(hi_candidates))
    if hi < lo:
        return []
    out = sorted({q for q in _LADDER if lo <= q <= hi})
    if lo not in out and lo <= hi:
        out = sorted({lo, *out})
    return out


def build_keyboard(product: Product,
                   available: Optional[int] = None,
                   product_id: Optional[int] = None) -> "InlineKeyboardMarkup":
    """Return an InlineKeyboardMarkup with preset quantity buttons.

    Each preset fires a callback ``qty_preset_<product_id>_<qty>`` so the
    purchase handler can pick it up without requiring manual text input.
    A Cancel button is always appended as the last row.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # lazy import

    pid = product_id or product.id
    presets = build_presets(product, available=available)

    kb: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for qty in presets:
        row.append(InlineKeyboardButton(str(qty), callback_data=f"qty_preset_{pid}_{qty}"))
        if len(row) == 4:          # max 4 per row for readability
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")])
    return InlineKeyboardMarkup(kb)


def validate_custom(product: Product, requested: int,
                    available: Optional[int] = None) -> tuple[bool, str]:
    """Validate a user-entered custom quantity. Returns (ok, error_message)."""
    if requested <= 0:
        return False, "Quantity must be a positive number."
    lo = int(product.min_quantity or 1)
    if requested < lo:
        return False, f"Minimum quantity for this product is {lo}."
    ptype_val = getattr(product.product_type, "value",
                        str(product.product_type or "")).lower()
    if product.max_quantity and requested > int(product.max_quantity):
        return False, f"Maximum quantity for this product is {product.max_quantity}."
    if (available is not None
            and ptype_val not in _UNLIMITED_TYPES
            and not getattr(product, "reusable", False)
            and requested > available):
        return False, f"Only {available} unit(s) available."
    return True, ""
