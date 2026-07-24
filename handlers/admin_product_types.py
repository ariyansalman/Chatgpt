"""V11 — Admin UI for the 12 product-type picker.

Renders a paginated inline keyboard so the admin can pick one of the 12
product types when creating a product. Callback data uses the format
``ptype:<enum_name>`` (stable identifier) and ``ptype_page:<n>`` for
pagination. Legacy ``type_key`` / ``type_file`` callbacks are still
accepted so existing conversations that were mid-flight don't break.
"""
from __future__ import annotations

from typing import List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import ProductType


PAGE_SIZE = 6


def _pages() -> List[List[Tuple[ProductType, str, str]]]:
    catalog = ProductType.catalog()
    return [catalog[i:i + PAGE_SIZE] for i in range(0, len(catalog), PAGE_SIZE)]


def build_type_picker(page: int = 0) -> InlineKeyboardMarkup:
    """Build the paginated 12-option product-type picker."""
    pages = _pages()
    page = max(0, min(page, len(pages) - 1))
    rows: List[List[InlineKeyboardButton]] = []
    for pt, emoji, label in pages[page]:
        rows.append([InlineKeyboardButton(
            f"{emoji} {label}", callback_data=f"ptype:{pt.name}"
        )])
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous",
                                        callback_data=f"ptype_page:{page - 1}"))
    if page < len(pages) - 1:
        nav.append(InlineKeyboardButton("➡️ Next",
                                        callback_data=f"ptype_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")])
    return InlineKeyboardMarkup(rows)


def parse_type_callback(data: str) -> ProductType | None:
    """Parse ``ptype:<NAME>`` (or legacy ``type_key`` / ``type_file``)."""
    if data == "type_key":
        return ProductType.KEY
    if data == "type_file":
        return ProductType.FILE
    if data.startswith("ptype:"):
        name = data.split(":", 1)[1]
        try:
            return ProductType[name]
        except KeyError:
            return None
    return None
