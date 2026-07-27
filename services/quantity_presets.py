"""Section 9 — dynamic quantity preset builder.

Given the real available stock plus product/variant min/max constraints,
compute the effective [lo, hi] quantity range, then present it through the
standardized "Intelligent Quantity Buttons" UI:

    Stock 1–4     1 2 / 3 4
    Stock 5–9     1 2 3 / 5 Max
    Stock 10–30   1 2 3 5 / 10 15 20 Max
    Stock 30+     1 2 3 5 / 10 25 50 Max

Rules:
* Never present a quantity above real availability (or below the minimum).
* Respect Product.min_quantity / Product.max_quantity when set.
* Reusable File / Manual / Service / Pre-Order / Subscription products
  are not stock-limited; fall back to a sensible max.
* If a tier's preset would exceed the effective max, it is dropped and a
  single "Max" button (representing the true upper bound) is shown instead
  of an impossible quantity.
* Include a "Custom" entry as a caller-controlled string sentinel.

This module is UI-presentation only — the constraint math (min/max
quantity, unlimited product types, availability cap) is unchanged from the
original implementation; only how those bounds are turned into buttons has
been standardized.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from database.models import Product

# Legacy flat ladder — still exported/used by build_presets() for any
# caller that wants the raw list of valid preset quantities (not the
# tiered button UI). Unchanged.
_LADDER = [1, 2, 3, 4, 5, 10, 15, 20, 25, 50, 100]

_UNLIMITED_TYPES = {
    "manual_delivery", "service", "pre_order", "subscription",
    "auto_generated", "external_delivery",
}


def _effective_bounds(product: Product,
                      available: Optional[int] = None,
                      cap: int = 100) -> Tuple[int, int]:
    """Compute the (lo, hi) quantity range allowed for this product.

    This is the same constraint logic used previously — only extracted
    into a helper so both ``build_presets`` (legacy flat list) and the
    tiered button UI in ``build_keyboard`` derive their bounds from a
    single, unchanged source of truth.
    """
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
    return lo, hi


def build_presets(product: Product,
                  available: Optional[int] = None,
                  cap: int = 100) -> List[int]:
    """Return the flat preset quantity ladder for this product (unchanged)."""
    lo, hi = _effective_bounds(product, available=available, cap=cap)
    if hi < lo:
        return []
    out = sorted({q for q in _LADDER if lo <= q <= hi})
    if lo not in out and lo <= hi:
        out = sorted({lo, *out})
    return out


def _tiered_display(lo: int, hi: int) -> Tuple[List[int], bool]:
    """Return (numeric_presets_to_show, show_max) for the standardized
    quantity-selector tiers, given the effective [lo, hi] bounds.

    Purely a presentation concern — never changes what quantities are
    *allowed*, only how the picker offers them. Every numeric preset
    returned is guaranteed to satisfy ``lo <= preset <= hi``; anything
    that would exceed ``hi`` is dropped and represented by "Max" instead.
    """
    if hi < lo:
        return [], False

    if hi <= 4:
        # Stock 1–4 — every quantity is directly selectable, no Max needed.
        return [q for q in range(max(lo, 1), hi + 1)], False

    if hi <= 9:
        ladder = [1, 2, 3, 5]
    elif hi <= 30:
        ladder = [1, 2, 3, 5, 10, 15, 20]
    else:
        ladder = [1, 2, 3, 5, 10, 25, 50]

    kept = [q for q in ladder if lo <= q <= hi]
    if kept and kept[-1] == hi:
        # Avoid a redundant button duplicating the exact Max value.
        kept = kept[:-1]
    return kept, True


def build_keyboard(product: Product,
                   available: Optional[int] = None,
                   product_id: Optional[int] = None) -> "InlineKeyboardMarkup":
    """Return the standardized quantity-selector keyboard.

    Numeric presets fire ``qty_preset_<product_id>_<qty>``; the "Max"
    button fires the same callback with the true upper bound as the
    quantity, so the purchase handler needs no changes. A
    ``✏️ Custom Quantity`` button (``qty_custom_<product_id>``) always
    follows, then a final row with ``⬅ Back to Product`` and
    ``❌ Cancel``.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # lazy import

    pid = product_id or product.id
    lo, hi = _effective_bounds(product, available=available)
    kept, show_max = _tiered_display(lo, hi)

    buttons = [
        InlineKeyboardButton(str(q), callback_data=f"qty_preset_{pid}_{q}")
        for q in kept
    ]
    if show_max:
        buttons.append(InlineKeyboardButton("Max", callback_data=f"qty_preset_{pid}_{hi}"))

    # Row width matches the standardized tier layout:
    #   Stock 1–4   -> 2 per row
    #   Stock 5–9   -> 3 per row (yields "1 2 3" / "5 Max")
    #   Stock 10+   -> 4 per row (yields "1 2 3 5" / "10 15 20 Max", etc.)
    if hi <= 4:
        row_size = 2
    elif hi <= 9:
        row_size = 3
    else:
        row_size = 4

    kb: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), row_size):
        kb.append(buttons[i:i + row_size])

    kb.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data=f"qty_custom_{pid}")])
    kb.append([
        InlineKeyboardButton("⬅ Back to Product", callback_data=f"product_{pid}"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase"),
    ])
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
