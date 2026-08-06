"""Section 9 — quantity preset builder for the Quantity Selection screen.

``build_keyboard()`` renders the standardized, marketplace-wide Quantity
Selection UI: a fixed preset ladder — 1, 2, 3, 5, 10, 15, 20, 25 — with any
preset above the effective stock upper-bound hidden, followed by
"✏️ Custom Quantity" and "⬅️ Back". Given the real available stock plus
product/variant min/max constraints, the effective [lo, hi] quantity range
is computed first (unchanged constraint math), then only presets that fall
within it are shown.

Rules:
* Never present a quantity above real availability (or below the minimum).
* Respect Product.min_quantity / Product.max_quantity when set.
* Reusable File / Manual / Service / Pre-Order / Subscription products
  are not stock-limited; fall back to a sensible max.

The older tiered "Max"-button scheme below (``_tiered_display``) and the
flat legacy ladder (``build_presets``) are kept only for callers that still
use them directly; ``build_keyboard`` — the one function the live Quantity
Selection screen uses — does not call either.

    Stock 1–4     1 2 / 3 4
    Stock 5–9     1 2 3 / 5 Max
    Stock 10–30   1 2 3 5 / 10 15 20 Max
    Stock 30+     1 2 3 5 / 10 25 50 Max

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

# Presentation-only singular unit word shown on the Quantity Selection
# screen's price line (e.g. "$25.00 / Code"). Purely a display label
# derived from the product's existing, unchanged ``product_type`` — it
# never affects delivery, stock counting, or any other business logic.
_UNIT_LABELS: dict = {
    "key":               "Key",
    "file":              "File",
    "redeem_link":       "Code",
    "account_login":     "Account",
    "downloadable_file": "File",
    "auto_generated":    "Code",
    "manual_delivery":   "Item",
    "preorder":          "Item",
    "subscription":      "License",
    "bundle":            "Bundle",
    "service":           "Service",
    "voucher":           "Code",
    "external_delivery": "Item",
}


def unit_label(product_type) -> str:
    """Return the display unit word for a product type (e.g. "Key",
    "Account", "Code"), falling back to "Unit" for anything unmapped."""
    key = getattr(product_type, "value", str(product_type or "")).lower()
    return _UNIT_LABELS.get(key, "Unit")


def build_message(product_name: str, price, product_type) -> str:
    """The one standardized Quantity Selection screen text, identical in
    shape for every product in the store:

        ⚡ {Product Name}

        💶 Price
        ${price} / {unit}

        📦 Select Quantity

    Presentation-only — ``price`` is rendered exactly as given (already
    validated/priced elsewhere); this never recomputes or alters it.
    """
    unit = unit_label(product_type)
    return (
        f"⚡ {product_name}\n"
        f"\n"
        f"💶 Price\n"
        f"${float(price):.2f} / {unit}\n"
        f"\n"
        f"📦 Select Quantity"
    )


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

    Numeric presets fire ``qty_preset_<product_id>_<qty>``; the purchase
    handler needs no changes. A ``✏️ Custom Quantity`` button
    (``qty_custom_<product_id>``) always follows, then a final row with
    ``⬅️ Back``. No Cancel button here — no order or payment exists yet
    at this screen.

    Preset ladder: [1, 2, 3, 5, 10, 15, 20, 25] — any entry exceeding the
    effective stock upper-bound is hidden (unchanged constraint logic, see
    ``_effective_bounds``). Displayed 4 per row.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # lazy import

    pid = product_id or product.id
    _lo, hi = _effective_bounds(product, available=available)

    _PRESETS = [1, 2, 3, 5, 10, 15, 20, 25]
    kept = [q for q in _PRESETS if q <= hi]

    buttons = [
        InlineKeyboardButton(str(q), callback_data=f"qty_preset_{pid}_{q}")
        for q in kept
    ]

    row_size = 4
    kb: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), row_size):
        kb.append(buttons[i:i + row_size])

    kb.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data=f"qty_custom_{pid}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_products")])
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
