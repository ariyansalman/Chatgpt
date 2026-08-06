"""Button Builder Service — Premium Product System, Phase 1, Feature 5.

Lets the admin control the label, emoji, visibility, and order of the
shared product-page buttons (Buy Now, Back, Support, View Plans, Refresh,
Favorite, Home) from one place.

IMPORTANT: this only changes how a button is *drawn*. It never changes
callback_data, so every existing handler keeps working untouched. A button
whose ``is_visible`` is turned off simply isn't added to the keyboard —
callers should always check ``is_button_visible`` before appending it.
"""

from __future__ import annotations

import logging
from datetime import datetime

from database import get_db_session

logger = logging.getLogger(__name__)

# key -> (default label, default emoji, default display_order)
DEFAULT_BUTTONS: dict[str, tuple[str, str, int]] = {
    "buy_now":    ("Buy Now",    "🛒", 10),
    "back":       ("Back",       "🔙", 20),
    "support":    ("Support",    "☎️", 30),
    "view_plans": ("View Plans", "📋", 40),
    "refresh":    ("Refresh",    "🔄", 50),
    "favorite":   ("Favorite",   "❤️", 60),
    "home":       ("Home",       "🏠", 70),
}

_CACHE: dict[str, dict] | None = None


def _load_all(force: bool = False) -> dict[str, dict]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    out: dict[str, dict] = {}
    try:
        from database.models import ProductButtonSetting as _PBS
        with get_db_session() as session:
            rows = session.query(_PBS).all()
            existing_keys = {r.button_key for r in rows}
            for r in rows:
                out[r.button_key] = {
                    "label": r.label,
                    "emoji": r.emoji or "",
                    "is_visible": r.is_visible,
                    "display_order": r.display_order,
                }
            # Backfill any button key that doesn't have a row yet (e.g. a
            # fresh DB where the migration's seed rows never ran).
            for key, (label, emoji, order) in DEFAULT_BUTTONS.items():
                if key not in existing_keys:
                    row = _PBS(button_key=key, label=label, emoji=emoji,
                              is_visible=True, display_order=order,
                              updated_at=datetime.utcnow())
                    session.add(row)
                    out[key] = {"label": label, "emoji": emoji,
                               "is_visible": True, "display_order": order}
            session.commit()
    except Exception as exc:
        logger.warning("ButtonBuilder: _load_all failed, using defaults: %s", exc)
        for key, (label, emoji, order) in DEFAULT_BUTTONS.items():
            out.setdefault(key, {"label": label, "emoji": emoji,
                                 "is_visible": True, "display_order": order})
    _CACHE = out
    return out


def get_button(key: str) -> dict:
    """Return {'label', 'emoji', 'is_visible', 'display_order'} for a button key."""
    all_buttons = _load_all()
    if key in all_buttons:
        return all_buttons[key]
    label, emoji, order = DEFAULT_BUTTONS.get(key, (key.title(), "", 999))
    return {"label": label, "emoji": emoji, "is_visible": True, "display_order": order}


def is_button_visible(key: str) -> bool:
    return get_button(key).get("is_visible", True)


def button_text(key: str) -> str:
    """Return the ready-to-display 'emoji Label' text for a button."""
    b = get_button(key)
    emoji = (b.get("emoji") or "").strip()
    label = (b.get("label") or key.title()).strip()
    return f"{emoji} {label}".strip()


def list_all() -> list[dict]:
    all_buttons = _load_all()
    rows = []
    for key, data in all_buttons.items():
        row = dict(data)
        row["key"] = key
        rows.append(row)
    rows.sort(key=lambda r: (r["display_order"], r["key"]))
    return rows


def update_button(key: str, **fields) -> tuple[bool, str]:
    """Update one or more of: label, emoji, is_visible, display_order."""
    try:
        from database.models import ProductButtonSetting as _PBS
        with get_db_session() as session:
            row = session.query(_PBS).filter_by(button_key=key).first()
            if not row:
                label, emoji, order = DEFAULT_BUTTONS.get(key, (key.title(), "", 999))
                row = _PBS(button_key=key, label=label, emoji=emoji,
                          is_visible=True, display_order=order)
                session.add(row)
            for f in ("label", "emoji", "is_visible", "display_order"):
                if f in fields:
                    setattr(row, f, fields[f])
            row.updated_at = datetime.utcnow()
            session.commit()
        global _CACHE
        _CACHE = None
        return True, "✅ Button updated."
    except Exception as exc:
        logger.exception("ButtonBuilder: update_button failed")
        return False, f"❌ Error: {exc}"


def toggle_visibility(key: str) -> bool:
    b = get_button(key)
    new_val = not b.get("is_visible", True)
    update_button(key, is_visible=new_val)
    return new_val


def move_button(key: str, direction: int) -> bool:
    """Swap display_order with the neighboring button. direction: -1 up, +1 down."""
    rows = list_all()
    idx = next((i for i, r in enumerate(rows) if r["key"] == key), None)
    if idx is None:
        return False
    swap_idx = idx + (-1 if direction < 0 else 1)
    if swap_idx < 0 or swap_idx >= len(rows):
        return False
    a, b = rows[idx], rows[swap_idx]
    update_button(a["key"], display_order=b["display_order"])
    update_button(b["key"], display_order=a["display_order"])
    return True
