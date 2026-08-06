"""Product Tags Service — Premium Product System, Phase 1, Feature 7.

Admin-managed tag catalog (Featured, Best Seller, New, Popular, Premium,
Discount, Limited, Digital, Instant, ...) plus per-product assignment.

Distinct from services/badges.py, which computes badges automatically from
real data (sales_count, created_at, price). These tags are manually
assigned by the admin and fully editable — the admin can rename, disable,
reorder, delete, or add brand-new tags.
"""

from __future__ import annotations

import html as _html
import logging
from datetime import datetime

from database import get_db_session

logger = logging.getLogger(__name__)

# Seed catalog — matches Feature 7's list. Used only as a fallback / for
# `ensure_default_tags`; the live catalog always comes from the DB so the
# admin's edits (renames, new tags, deletions) are authoritative.
DEFAULT_TAGS: list[tuple[str, str, str, str, int]] = [
    ("featured",     "Featured",     "⭐", "yellow", 10),
    ("best_seller",  "Best Seller",  "🔥", "orange", 20),
    ("new",          "New",          "🆕", "green",  30),
    ("popular",      "Popular",      "📈", "blue",   40),
    ("premium",      "Premium",      "💎", "purple", 50),
    ("discount",     "Discount",     "🏷️", "red",    60),
    ("limited",      "Limited",      "⏳", "red",    70),
    ("digital",      "Digital",      "💾", "blue",   80),
    ("instant",      "Instant",      "⚡", "yellow", 90),
]


def ensure_default_tags() -> None:
    """Seed the catalog with defaults if it's completely empty. Safe no-op otherwise."""
    try:
        from database.models import ProductTag as _PT
        with get_db_session() as session:
            if session.query(_PT).count() > 0:
                return
            for key, label, emoji, color, order in DEFAULT_TAGS:
                session.add(_PT(key=key, label=label, emoji=emoji, color=color,
                                is_active=True, display_order=order,
                                created_at=datetime.utcnow()))
            session.commit()
    except Exception as exc:
        logger.warning("ProductTags: ensure_default_tags failed: %s", exc)


def list_tags(active_only: bool = False) -> list[dict]:
    try:
        from database.models import ProductTag as _PT
        with get_db_session() as session:
            q = session.query(_PT)
            if active_only:
                q = q.filter_by(is_active=True)
            rows = q.order_by(_PT.display_order, _PT.id).all()
            return [
                {"id": r.id, "key": r.key, "label": r.label, "emoji": r.emoji,
                 "color": r.color, "is_active": r.is_active,
                 "display_order": r.display_order}
                for r in rows
            ]
    except Exception as exc:
        logger.warning("ProductTags: list_tags failed: %s", exc)
        return []


def create_tag(key: str, label: str, emoji: str = "", color: str = "none") -> tuple[bool, str]:
    try:
        from database.models import ProductTag as _PT
        from sqlalchemy import func
        key = key.strip().lower().replace(" ", "_")[:64]
        if not key or not label.strip():
            return False, "❌ Key and label are required."
        with get_db_session() as session:
            if session.query(_PT).filter_by(key=key).first():
                return False, "❌ A tag with that key already exists."
            max_order = (session.query(func.max(_PT.display_order)).scalar()) or 0
            session.add(_PT(key=key, label=label.strip()[:64], emoji=emoji.strip()[:32],
                            color=color, is_active=True, display_order=max_order + 10,
                            created_at=datetime.utcnow()))
            session.commit()
            return True, f"✅ Tag <b>{_html.escape(label)}</b> created."
    except Exception as exc:
        logger.exception("ProductTags: create_tag failed")
        return False, f"❌ Error: {exc}"


def update_tag(tag_id: int, **fields) -> tuple[bool, str]:
    """Update one or more of: label, emoji, color, is_active."""
    try:
        from database.models import ProductTag as _PT
        with get_db_session() as session:
            tag = session.query(_PT).filter_by(id=tag_id).first()
            if not tag:
                return False, "❌ Tag not found."
            for f in ("label", "emoji", "color", "is_active"):
                if f in fields:
                    setattr(tag, f, fields[f])
            session.commit()
            return True, "✅ Tag updated."
    except Exception as exc:
        logger.exception("ProductTags: update_tag failed")
        return False, f"❌ Error: {exc}"


def delete_tag(tag_id: int) -> bool:
    try:
        from database.models import ProductTag as _PT
        with get_db_session() as session:
            tag = session.query(_PT).filter_by(id=tag_id).first()
            if not tag:
                return False
            session.delete(tag)  # cascades product_tag_links
            session.commit()
            return True
    except Exception as exc:
        logger.warning("ProductTags: delete_tag failed: %s", exc)
        return False


def move_tag(tag_id: int, direction: int) -> bool:
    try:
        from database.models import ProductTag as _PT
        with get_db_session() as session:
            tag = session.query(_PT).filter_by(id=tag_id).first()
            if not tag:
                return False
            q = session.query(_PT)
            if direction < 0:
                neighbor = (q.filter(_PT.display_order < tag.display_order)
                           .order_by(_PT.display_order.desc()).first())
            else:
                neighbor = (q.filter(_PT.display_order > tag.display_order)
                           .order_by(_PT.display_order.asc()).first())
            if not neighbor:
                return False
            tag.display_order, neighbor.display_order = (
                neighbor.display_order, tag.display_order,
            )
            session.commit()
            return True
    except Exception as exc:
        logger.warning("ProductTags: move_tag failed: %s", exc)
        return False


def product_tags(product_id: int, active_only: bool = True) -> list[dict]:
    """Return tags currently assigned to a product."""
    try:
        from database.models import ProductTag as _PT, ProductTagLink as _PTL
        with get_db_session() as session:
            q = (session.query(_PT)
                 .join(_PTL, _PTL.tag_id == _PT.id)
                 .filter(_PTL.product_id == product_id))
            if active_only:
                q = q.filter(_PT.is_active == True)  # noqa: E712
            rows = q.order_by(_PT.display_order, _PT.id).all()
            return [
                {"id": r.id, "key": r.key, "label": r.label, "emoji": r.emoji,
                 "color": r.color}
                for r in rows
            ]
    except Exception as exc:
        logger.warning("ProductTags: product_tags(%s) failed: %s", product_id, exc)
        return []


def assigned_tag_ids(product_id: int) -> set[int]:
    try:
        from database.models import ProductTagLink as _PTL
        with get_db_session() as session:
            rows = session.query(_PTL.tag_id).filter_by(product_id=product_id).all()
            return {r[0] for r in rows}
    except Exception:
        return set()


def toggle_product_tag(product_id: int, tag_id: int) -> bool:
    """Assign the tag if not present, unassign if present. Returns new state (True=assigned)."""
    try:
        from database.models import ProductTagLink as _PTL
        with get_db_session() as session:
            link = (session.query(_PTL)
                    .filter_by(product_id=product_id, tag_id=tag_id).first())
            if link:
                session.delete(link)
                session.commit()
                return False
            session.add(_PTL(product_id=product_id, tag_id=tag_id,
                             created_at=datetime.utcnow()))
            session.commit()
            return True
    except Exception as exc:
        logger.warning("ProductTags: toggle_product_tag failed: %s", exc)
        return False


def render_tag_line(product_id: int) -> str:
    """Return the tags joined for display on the product card, or '' if none."""
    tags = product_tags(product_id, active_only=True)
    if not tags:
        return ""
    parts = []
    for t in tags:
        emoji = (t["emoji"] or "").strip()
        label = _html.escape(t["label"] or "")
        parts.append(f"{emoji} {label}".strip())
    return "  ".join(parts)
