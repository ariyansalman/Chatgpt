"""V25 — Product FAQ service.

Central module for all FAQ data access: create, read, update, delete,
reorder, duplicate, copy, and search. All write operations enforce
duplicate-prevention and the per-product maximum limit.

Public API
----------
get_faqs(product_id, *, active_only, category, limit)
get_faq(faq_id)
add_faq(product_id, question, answer, category, admin_id) -> ProductFAQ
edit_faq(faq_id, **, admin_id)
delete_faq(faq_id)
move_faq(faq_id, direction)          direction: "up" | "down"
duplicate_faq(faq_id)
copy_faq_to_product(faq_id, target_product_id)
search_faqs(product_id, query)       → list[ProductFAQ]
faq_count(product_id)
is_enabled()
max_per_product()
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func

from database import get_db_session
from database.models import Product, ProductFAQ
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ─── Category catalogue ───────────────────────────────────────────────────

CATEGORIES = {
    "general":         "📋 General",
    "payment":         "💳 Payment",
    "delivery":        "📦 Delivery",
    "account":         "👤 Account",
    "warranty":        "🛡️ Warranty",
    "troubleshooting": "🔧 Troubleshooting",
    "custom":          "✏️ Custom",
}

VALID_CATEGORIES = set(CATEGORIES.keys())


# ─── Config helpers ───────────────────────────────────────────────────────

def is_enabled() -> bool:
    return cfg.get_str("pfaq_status", "enabled") == "enabled"


def is_maintenance() -> bool:
    return cfg.get_str("pfaq_status", "enabled") == "maintenance"


def max_per_product() -> int:
    raw = cfg.get_str("pfaq_max_per_product", "20")
    try:
        v = int(raw)
        return v if v > 0 else 0   # 0 = unlimited
    except (ValueError, TypeError):
        return 20


def show_counter() -> bool:
    return cfg.get_bool("pfaq_show_counter", True)


def allow_search() -> bool:
    return cfg.get_bool("pfaq_allow_search", True)


def expand_first() -> bool:
    return cfg.get_bool("pfaq_expand_first", False)


# ─── Data helpers ─────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    return " ".join(text.strip().split()).lower()


def _duplicate_exists(s, product_id: int, question: str,
                      exclude_id: Optional[int] = None) -> bool:
    norm = _normalise(question)
    q = s.query(ProductFAQ).filter(
        ProductFAQ.product_id == product_id,
        func.lower(ProductFAQ.question) == norm,
    )
    if exclude_id is not None:
        q = q.filter(ProductFAQ.id != exclude_id)
    return q.first() is not None


def _next_sort_order(s, product_id: int) -> int:
    max_row = (s.query(func.max(ProductFAQ.sort_order))
               .filter(ProductFAQ.product_id == product_id)
               .scalar())
    return (max_row or 0) + 10


# ─── Read ─────────────────────────────────────────────────────────────────

def get_faqs(product_id: int, *,
             active_only: bool = True,
             category: Optional[str] = None,
             limit: int = 200) -> List[ProductFAQ]:
    """Return FAQs for a product ordered by sort_order."""
    with get_db_session() as s:
        q = s.query(ProductFAQ).filter(ProductFAQ.product_id == product_id)
        if active_only:
            q = q.filter(ProductFAQ.is_active == True)  # noqa: E712
        if category:
            q = q.filter(ProductFAQ.category == category)
        rows = q.order_by(ProductFAQ.sort_order.asc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "product_id": r.product_id,
                "question": r.question,
                "answer": r.answer,
                "category": r.category,
                "sort_order": r.sort_order,
                "is_active": r.is_active,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]


def get_faq(faq_id: int) -> Optional[dict]:
    with get_db_session() as s:
        r = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not r:
            return None
        return {
            "id": r.id,
            "product_id": r.product_id,
            "question": r.question,
            "answer": r.answer,
            "category": r.category,
            "sort_order": r.sort_order,
            "is_active": r.is_active,
            "created_at": r.created_at,
        }


def faq_count(product_id: int, active_only: bool = False) -> int:
    with get_db_session() as s:
        q = s.query(func.count(ProductFAQ.id)).filter(
            ProductFAQ.product_id == product_id
        )
        if active_only:
            q = q.filter(ProductFAQ.is_active == True)  # noqa: E712
        return q.scalar() or 0


def search_faqs(product_id: int, query: str) -> List[dict]:
    """Case-insensitive substring search across question and answer."""
    term = f"%{query.strip().lower()}%"
    with get_db_session() as s:
        rows = (
            s.query(ProductFAQ)
            .filter(
                ProductFAQ.product_id == product_id,
                ProductFAQ.is_active == True,  # noqa: E712
                (
                    func.lower(ProductFAQ.question).like(term)
                    | func.lower(ProductFAQ.answer).like(term)
                ),
            )
            .order_by(ProductFAQ.sort_order.asc())
            .limit(50)
            .all()
        )
        return [
            {
                "id": r.id, "product_id": r.product_id,
                "question": r.question, "answer": r.answer,
                "category": r.category,
            }
            for r in rows
        ]


# ─── Write ────────────────────────────────────────────────────────────────

def add_faq(product_id: int, question: str, answer: str,
            category: str = "general") -> dict:
    """Create a new FAQ. Raises ValueError on duplicate or limit exceeded."""
    if not question.strip():
        raise ValueError("Question cannot be empty.")
    if not answer.strip():
        raise ValueError("Answer cannot be empty.")
    cat = category if category in VALID_CATEGORIES else "general"

    with get_db_session() as s:
        # Validate product exists
        if not s.query(Product).filter(Product.id == product_id).first():
            raise ValueError(f"Product {product_id} not found.")
        # Duplicate check
        if _duplicate_exists(s, product_id, question):
            raise ValueError("A FAQ with the same question already exists for this product.")
        # Limit check
        mx = max_per_product()
        if mx > 0:
            count = (s.query(func.count(ProductFAQ.id))
                     .filter(ProductFAQ.product_id == product_id)
                     .scalar() or 0)
            if count >= mx:
                raise ValueError(
                    f"Maximum FAQ limit ({mx}) reached for this product."
                )
        sort = _next_sort_order(s, product_id)
        faq = ProductFAQ(
            product_id=product_id,
            question=question.strip()[:1000],
            answer=answer.strip()[:3000],
            category=cat,
            sort_order=sort,
            is_active=True,
        )
        s.add(faq)
        s.commit()
        s.refresh(faq)
        return get_faq(faq.id)


def edit_faq(faq_id: int, *,
             question: Optional[str] = None,
             answer: Optional[str] = None,
             category: Optional[str] = None,
             is_active: Optional[bool] = None) -> bool:
    with get_db_session() as s:
        faq = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not faq:
            return False
        if question is not None:
            q = question.strip()
            if not q:
                raise ValueError("Question cannot be empty.")
            if _duplicate_exists(s, faq.product_id, q, exclude_id=faq_id):
                raise ValueError("Another FAQ with the same question already exists.")
            faq.question = q[:1000]
        if answer is not None:
            a = answer.strip()
            if not a:
                raise ValueError("Answer cannot be empty.")
            faq.answer = a[:3000]
        if category is not None and category in VALID_CATEGORIES:
            faq.category = category
        if is_active is not None:
            faq.is_active = is_active
        faq.updated_at = datetime.utcnow()
        s.commit()
        return True


def delete_faq(faq_id: int) -> bool:
    with get_db_session() as s:
        faq = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not faq:
            return False
        s.delete(faq)
        s.commit()
        return True


def move_faq(faq_id: int, direction: str) -> bool:
    """Swap sort_order with the adjacent FAQ (direction: 'up' or 'down')."""
    with get_db_session() as s:
        faq = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not faq:
            return False
        siblings = (
            s.query(ProductFAQ)
            .filter(ProductFAQ.product_id == faq.product_id)
            .order_by(ProductFAQ.sort_order.asc())
            .all()
        )
        ids = [r.id for r in siblings]
        try:
            idx = ids.index(faq_id)
        except ValueError:
            return False
        if direction == "up" and idx > 0:
            neighbor = siblings[idx - 1]
        elif direction == "down" and idx < len(siblings) - 1:
            neighbor = siblings[idx + 1]
        else:
            return False   # already at boundary
        faq.sort_order, neighbor.sort_order = neighbor.sort_order, faq.sort_order
        s.commit()
        return True


def duplicate_faq(faq_id: int) -> Optional[dict]:
    """Duplicate a FAQ on the same product. Returns the new FAQ or None."""
    with get_db_session() as s:
        orig = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not orig:
            return None
        new_q = f"[Copy] {orig.question}"[:1000]
        sort = _next_sort_order(s, orig.product_id)
        dup = ProductFAQ(
            product_id=orig.product_id,
            question=new_q,
            answer=orig.answer,
            category=orig.category,
            sort_order=sort,
            is_active=False,   # disabled by default so admin can review
        )
        s.add(dup)
        s.commit()
        s.refresh(dup)
        return get_faq(dup.id)


def copy_faq_to_product(faq_id: int, target_product_id: int) -> Optional[dict]:
    """Copy a FAQ to another product. Returns the new FAQ or None."""
    with get_db_session() as s:
        orig = s.query(ProductFAQ).filter(ProductFAQ.id == faq_id).first()
        if not orig:
            return None
        if not s.query(Product).filter(Product.id == target_product_id).first():
            raise ValueError(f"Target product {target_product_id} not found.")
        if _duplicate_exists(s, target_product_id, orig.question):
            raise ValueError("Identical question already exists on the target product.")
        mx = max_per_product()
        if mx > 0:
            count = (s.query(func.count(ProductFAQ.id))
                     .filter(ProductFAQ.product_id == target_product_id).scalar() or 0)
            if count >= mx:
                raise ValueError(f"Target product has reached the FAQ limit ({mx}).")
        sort = _next_sort_order(s, target_product_id)
        copied = ProductFAQ(
            product_id=target_product_id,
            question=orig.question,
            answer=orig.answer,
            category=orig.category,
            sort_order=sort,
            is_active=False,
        )
        s.add(copied)
        s.commit()
        s.refresh(copied)
        return get_faq(copied.id)


# ─── Rendering helpers ────────────────────────────────────────────────────

def render_user_faqs(product_id: int) -> str:
    """Render FAQs as a user-friendly message."""
    if is_maintenance():
        return "⚠️ Product FAQ is currently under maintenance. Please check back soon."

    if not is_enabled():
        return ""

    faqs = get_faqs(product_id, active_only=True)
    if not faqs:
        return "❓ <b>FAQ</b>\n\nNo FAQ available for this product."

    counter = f" ({len(faqs)})" if show_counter() else ""
    lines = [f"❓ <b>FAQ{counter}</b>", ""]

    for i, faq in enumerate(faqs):
        cat_label = CATEGORIES.get(faq["category"], "")
        cat_str = f"  <i>{cat_label}</i>" if cat_label else ""
        # First question expanded when setting ON
        if i == 0 and expand_first():
            lines.append(f"<b>Q{i+1}: {faq['question']}</b>{cat_str}")
            lines.append(f"💬 {faq['answer']}")
        else:
            lines.append(f"<b>Q{i+1}: {faq['question']}</b>{cat_str}")
            lines.append(f"💬 {faq['answer']}")
        lines.append("")

    return "\n".join(lines).rstrip()
