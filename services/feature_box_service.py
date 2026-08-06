"""Feature Box Service — Premium Product System, Phase 1, Feature 2.

Each product can have an unlimited number of admin-defined feature rows
(emoji + title + description), shown as a compact block on the product
detail card, e.g.:

    ✅ Instant Delivery
    🛡 30 Days Warranty
    ⚡ Premium Account
    🎁 Ready To Use

Everything is stored in ``ProductFeatureItem`` and editable from the Admin
Panel — nothing here is hardcoded per product. Does not touch payment,
delivery, or order logic.
"""

from __future__ import annotations

import html as _html
import logging
from datetime import datetime
from typing import Optional

from database import get_db_session

logger = logging.getLogger(__name__)


def list_items(product_id: int, visible_only: bool = False) -> list:
    """Return feature-box rows for a product, ordered for display."""
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            q = session.query(_PFI).filter_by(product_id=product_id)
            if visible_only:
                q = q.filter_by(is_visible=True)
            rows = q.order_by(_PFI.display_order, _PFI.id).all()
            return [
                {
                    "id": r.id,
                    "emoji": r.emoji,
                    "title": r.title,
                    "description": r.description,
                    "is_visible": r.is_visible,
                    "display_order": r.display_order,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.warning("FeatureBox: list_items(%s) failed: %s", product_id, exc)
        return []


def add_item(product_id: int, emoji: str = "", title: str = "",
             description: Optional[str] = None) -> tuple[bool, str]:
    try:
        from database.models import ProductFeatureItem as _PFI
        from sqlalchemy import func
        with get_db_session() as session:
            max_order = (session.query(func.max(_PFI.display_order))
                        .filter_by(product_id=product_id).scalar()) or 0
            item = _PFI(
                product_id=product_id,
                emoji=(emoji or "").strip()[:32],
                title=(title or "New Feature").strip()[:200],
                description=(description or None),
                is_visible=True,
                display_order=max_order + 10,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(item)
            session.commit()
            return True, "✅ Feature added."
    except Exception as exc:
        logger.exception("FeatureBox: add_item failed")
        return False, f"❌ Error: {exc}"


def update_item(item_id: int, **fields) -> tuple[bool, str]:
    """Update one or more of: emoji, title, description, is_visible."""
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            item = session.query(_PFI).filter_by(id=item_id).first()
            if not item:
                return False, "❌ Feature not found."
            for key in ("emoji", "title", "description", "is_visible"):
                if key in fields:
                    setattr(item, key, fields[key])
            item.updated_at = datetime.utcnow()
            session.commit()
            return True, "✅ Feature updated."
    except Exception as exc:
        logger.exception("FeatureBox: update_item failed")
        return False, f"❌ Error: {exc}"


def toggle_visibility(item_id: int) -> bool:
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            item = session.query(_PFI).filter_by(id=item_id).first()
            if not item:
                return False
            item.is_visible = not item.is_visible
            item.updated_at = datetime.utcnow()
            session.commit()
            return item.is_visible
    except Exception as exc:
        logger.warning("FeatureBox: toggle_visibility failed: %s", exc)
        return False


def move_item(item_id: int, direction: int) -> bool:
    """Swap display_order with the neighboring item. direction: -1 up, +1 down."""
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            item = session.query(_PFI).filter_by(id=item_id).first()
            if not item:
                return False
            q = session.query(_PFI).filter_by(product_id=item.product_id)
            if direction < 0:
                neighbor = (q.filter(_PFI.display_order < item.display_order)
                            .order_by(_PFI.display_order.desc()).first())
            else:
                neighbor = (q.filter(_PFI.display_order > item.display_order)
                            .order_by(_PFI.display_order.asc()).first())
            if not neighbor:
                return False
            item.display_order, neighbor.display_order = (
                neighbor.display_order, item.display_order,
            )
            session.commit()
            return True
    except Exception as exc:
        logger.warning("FeatureBox: move_item failed: %s", exc)
        return False


def delete_item(item_id: int) -> bool:
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            item = session.query(_PFI).filter_by(id=item_id).first()
            if not item:
                return False
            session.delete(item)
            session.commit()
            return True
    except Exception as exc:
        logger.warning("FeatureBox: delete_item failed: %s", exc)
        return False


def count_items(product_id: int) -> int:
    try:
        from database.models import ProductFeatureItem as _PFI
        with get_db_session() as session:
            return session.query(_PFI).filter_by(product_id=product_id).count()
    except Exception:
        return 0


def render_feature_box_html(product_id: int) -> str:
    """Render the visible feature rows as an HTML block, or '' if none."""
    items = list_items(product_id, visible_only=True)
    if not items:
        return ""
    lines = []
    for it in items:
        emoji = (it["emoji"] or "").strip()
        title = _html.escape((it["title"] or "").strip())
        desc = (it["description"] or "").strip()
        line = f"{emoji} <b>{title}</b>".strip() if title else emoji
        if desc:
            line += f" — {_html.escape(desc)}"
        lines.append(line)
    return "\n".join(lines)
