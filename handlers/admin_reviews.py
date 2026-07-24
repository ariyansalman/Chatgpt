"""Admin Review Manager.

Callback namespace: arv:*

Admin can:
  • View all reviews (pending approval, approved, hidden)
  • Approve / Reject / Hide reviews
  • Pin / Unpin reviews
  • Delete reviews
  • View review statistics

All operations preserve the database row — delete only soft-deletes via is_hidden.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session, User, Product
from database.models import Review
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

_PER_PAGE = 8


def get_review_stats() -> dict:
    """Return review stats for admin dashboard."""
    stats = {}
    try:
        with get_db_session() as s:
            stats["total"]     = s.query(Review).count()
            stats["approved"]  = s.query(Review).filter_by(is_approved=True, is_hidden=False).count()
            stats["pending"]   = s.query(Review).filter_by(is_approved=False, is_hidden=False).count()
            stats["hidden"]    = s.query(Review).filter_by(is_hidden=True).count()
            stats["pinned"]    = s.query(Review).filter_by(is_pinned=True).count()

            # Most reviewed product
            row = (
                s.query(Review.product_id, func.count(Review.id).label("cnt"))
                .filter_by(is_hidden=False)
                .group_by(Review.product_id)
                .order_by(func.count(Review.id).desc())
                .first()
            )
            if row:
                p = s.query(Product).filter_by(id=row.product_id).first()
                stats["top_product"] = p.name if p else str(row.product_id)
                stats["top_product_count"] = row.cnt
            else:
                stats["top_product"] = "—"
                stats["top_product_count"] = 0

            # Average rating
            avg_row = s.query(func.avg(Review.rating)).filter_by(is_hidden=False).first()
            stats["avg_rating"] = float(avg_row[0]) if avg_row and avg_row[0] else 0.0
    except Exception:
        logger.exception("get_review_stats failed")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Main review management menu
# ─────────────────────────────────────────────────────────────────────────────

async def review_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin review management menu: arv:menu"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    enabled = cfg.get_bool("feature_reviews_enabled", True)
    require_approval = cfg.get_bool("feature_reviews_require_approval", False)
    stats = get_review_stats()

    text = (
        "⭐ <b>Review Manager</b>\n\n"
        f"Feature: {'✅ Enabled' if enabled else '❌ Disabled'}\n"
        f"Require Approval: {'✅ Yes' if require_approval else '🚫 No (auto-approved)'}\n\n"
        "<b>Statistics:</b>\n"
        f"  • Total reviews: <b>{stats.get('total', 0)}</b>\n"
        f"  • Approved/Visible: <b>{stats.get('approved', 0)}</b>\n"
        f"  • Pending Approval: <b>{stats.get('pending', 0)}</b>\n"
        f"  • Hidden: <b>{stats.get('hidden', 0)}</b>\n"
        f"  • Pinned: <b>{stats.get('pinned', 0)}</b>\n"
        f"  • Avg Rating: <b>{stats.get('avg_rating', 0):.1f}★</b>\n"
        f"  • Most Reviewed: <b>{stats.get('top_product', '—')}</b>"
        f" ({stats.get('top_product_count', 0)} reviews)"
    )

    toggle_lbl     = "❌ Disable Reviews" if enabled else "✅ Enable Reviews"
    approval_lbl   = "🚫 Disable Approval" if require_approval else "✅ Require Approval"

    kb = [
        [InlineKeyboardButton(toggle_lbl,   callback_data="arv:toggle")],
        [InlineKeyboardButton(approval_lbl, callback_data="arv:toggle_approval")],
        [InlineKeyboardButton("⏳ Pending Approval", callback_data="arv:list:pending:0")],
        [InlineKeyboardButton("✅ Approved Reviews",  callback_data="arv:list:approved:0")],
        [InlineKeyboardButton("🙈 Hidden Reviews",   callback_data="arv:list:hidden:0")],
        [InlineKeyboardButton("📌 Pinned Reviews",   callback_data="arv:list:pinned:0")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:root")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def review_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle review feature on/off: arv:toggle"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    new_val = not cfg.get_bool("feature_reviews_enabled", True)
    cfg.set("feature_reviews_enabled", new_val)
    log_admin_action(update.effective_user.id, "reviews.toggle", details=f"enabled={new_val}")
    await review_admin_menu(update, context)


async def review_toggle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle review approval requirement: arv:toggle_approval"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    new_val = not cfg.get_bool("feature_reviews_require_approval", False)
    cfg.set("feature_reviews_require_approval", new_val)
    log_admin_action(update.effective_user.id, "reviews.toggle_approval", details=f"require={new_val}")
    await review_admin_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Review list views
# ─────────────────────────────────────────────────────────────────────────────

async def review_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated review list: arv:list:<filter>:<page>
    filter: pending | approved | hidden | pinned
    """
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    _override = context.user_data.pop("_cb_data_override", None)
    _effective_data = _override if _override else query.data
    parts = _effective_data.split(":")
    # arv:list:<filter>:<page>
    review_filter = parts[2] if len(parts) > 2 else "pending"
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0

    with get_db_session() as s:
        q = s.query(Review)
        if review_filter == "pending":
            q = q.filter_by(is_approved=False, is_hidden=False)
            header = "⏳ Pending Approval"
        elif review_filter == "approved":
            q = q.filter_by(is_approved=True, is_hidden=False)
            header = "✅ Approved Reviews"
        elif review_filter == "hidden":
            q = q.filter_by(is_hidden=True)
            header = "🙈 Hidden Reviews"
        elif review_filter == "pinned":
            q = q.filter_by(is_pinned=True, is_hidden=False)
            header = "📌 Pinned Reviews"
        else:
            q = q.filter_by(is_hidden=False)
            header = "All Reviews"

        total = q.count()
        reviews = (q.order_by(Review.created_at.desc())
                   .offset(page * _PER_PAGE)
                   .limit(_PER_PAGE)
                   .all())
        rows = []
        for r in reviews:
            user    = s.query(User).filter_by(id=r.user_id).first()
            product = s.query(Product).filter_by(id=r.product_id).first()
            rows.append({
                "id":      r.id,
                "rating":  r.rating,
                "comment": (r.comment or "—")[:80],
                "user":    user.username or str(user.telegram_id) if user else "?",
                "product": product.name[:20] if product else "?",
                "pinned":  r.is_pinned,
                "when":    r.created_at.strftime("%m-%d") if r.created_at else "?",
            })

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    text = f"<b>{header}</b> (page {page + 1}/{total_pages}, {total} total)\n"
    kb = []
    for r in rows:
        pin_tag = "📌 " if r["pinned"] else ""
        kb.append([InlineKeyboardButton(
            f"{'⭐' * r['rating']} {pin_tag}@{r['user']} — {r['product']} ({r['when']})",
            callback_data=f"arv:view:{r['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"arv:list:{review_filter}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"arv:list:{review_filter}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="arv:menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# View single review with admin actions
# ─────────────────────────────────────────────────────────────────────────────

async def review_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View a single review: arv:view:<review_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await review_admin_menu(update, context)
        return

    with get_db_session() as s:
        review  = s.query(Review).filter_by(id=review_id).first()
        if not review:
            await query.answer("❌ Review not found.", show_alert=True)
            return
        user    = s.query(User).filter_by(id=review.user_id).first()
        product = s.query(Product).filter_by(id=review.product_id).first()
        info = {
            "id":        review.id,
            "rating":    review.rating,
            "comment":   review.comment or "— no comment —",
            "is_hidden": review.is_hidden,
            "is_pinned": review.is_pinned,
            "is_approved": review.is_approved,
            "user":      user.username or str(user.telegram_id) if user else "?",
            "product":   product.name if product else "?",
            "product_id": review.product_id,
            "created_at": review.created_at,
            "updated_at": review.updated_at,
        }

    stars = "⭐" * min(5, max(0, info["rating"]))
    when  = info["created_at"].strftime("%Y-%m-%d %H:%M") if info["created_at"] else "?"
    upd   = info["updated_at"].strftime("%Y-%m-%d %H:%M") if info["updated_at"] else "—"

    text = (
        f"⭐ <b>Review #{info['id']}</b>\n\n"
        f"Product: {info['product']}\n"
        f"User: @{info['user']}\n"
        f"Rating: {stars} ({info['rating']}/5)\n"
        f"Comment:\n<i>{info['comment'][:500]}</i>\n\n"
        f"Approved: {'✅' if info['is_approved'] else '❌'}\n"
        f"Hidden: {'🙈 Yes' if info['is_hidden'] else '👁 No'}\n"
        f"Pinned: {'📌 Yes' if info['is_pinned'] else '—'}\n"
        f"Posted: {when}\n"
        f"Edited: {upd}"
    )

    kb = []
    if not info["is_approved"] and not info["is_hidden"]:
        kb.append([InlineKeyboardButton("✅ Approve", callback_data=f"arv:approve:{review_id}")])
    if info["is_approved"] and not info["is_hidden"]:
        kb.append([InlineKeyboardButton("❌ Reject (Hide)", callback_data=f"arv:reject:{review_id}")])
    if info["is_hidden"]:
        kb.append([InlineKeyboardButton("👁 Unhide", callback_data=f"arv:unhide:{review_id}")])
    if not info["is_hidden"]:
        kb.append([InlineKeyboardButton("🙈 Hide", callback_data=f"arv:hide:{review_id}")])

    pin_lbl = "📌 Unpin" if info["is_pinned"] else "📌 Pin"
    kb.append([InlineKeyboardButton(pin_lbl, callback_data=f"arv:pin:{review_id}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"arv:delete:{review_id}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="arv:menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Admin actions on reviews
# ─────────────────────────────────────────────────────────────────────────────

async def _update_review(review_id: int, **kwargs):
    with get_db_session() as s:
        review = s.query(Review).filter_by(id=review_id).first()
        if review:
            for key, val in kwargs.items():
                setattr(review, key, val)
            review.updated_at = datetime.utcnow()
            s.commit()
            return True
    return False


async def review_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a review: arv:approve:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _update_review(review_id, is_approved=True, is_hidden=False)
    log_admin_action(update.effective_user.id, "review.approve",
                     target_type="review", target_id=review_id)
    await query.answer("✅ Review approved.", show_alert=False)
    context.user_data["_cb_data_override"] = str(review_id)
    await review_view(update, context)


async def review_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject (hide) a review: arv:reject:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _update_review(review_id, is_approved=False, is_hidden=True)
    log_admin_action(update.effective_user.id, "review.reject",
                     target_type="review", target_id=review_id)
    await query.answer("❌ Review rejected/hidden.", show_alert=False)
    context.user_data["_cb_data_override"] = str(review_id)
    await review_view(update, context)


async def review_hide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hide a review: arv:hide:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _update_review(review_id, is_hidden=True)
    log_admin_action(update.effective_user.id, "review.hide",
                     target_type="review", target_id=review_id)
    await query.answer("🙈 Review hidden.", show_alert=False)
    context.user_data["_cb_data_override"] = str(review_id)
    await review_view(update, context)


async def review_unhide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unhide a review: arv:unhide:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _update_review(review_id, is_hidden=False)
    log_admin_action(update.effective_user.id, "review.unhide",
                     target_type="review", target_id=review_id)
    await query.answer("👁 Review unhidden.", show_alert=False)
    context.user_data["_cb_data_override"] = str(review_id)
    await review_view(update, context)


async def review_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle pin on a review: arv:pin:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    with get_db_session() as s:
        review = s.query(Review).filter_by(id=review_id).first()
        if review:
            new_pin = not review.is_pinned
            review.is_pinned = new_pin
            review.updated_at = datetime.utcnow()
            s.commit()
    log_admin_action(update.effective_user.id, "review.pin_toggle",
                     target_type="review", target_id=review_id)
    await query.answer("📌 Pinned." if new_pin else "📌 Unpinned.", show_alert=False)
    context.user_data["_cb_data_override"] = str(review_id)
    await review_view(update, context)


async def review_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Soft-delete (permanently hide) a review: arv:delete:<review_id>"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    try:
        _override = context.user_data.pop("_cb_data_override", None)
        review_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return
    await _update_review(review_id, is_hidden=True, is_approved=False, is_pinned=False)
    log_admin_action(update.effective_user.id, "review.delete",
                     target_type="review", target_id=review_id)
    await query.answer("🗑 Review deleted (hidden).", show_alert=False)
    await review_admin_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Route dispatcher (for acc: namespace)
# ─────────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route acc:reviews:<action> calls."""
    if action == "menu":
        await review_admin_menu(update, context)
    elif action == "toggle":
        await review_toggle(update, context)
    elif action == "toggle_approval":
        await review_toggle_approval(update, context)
    elif action == "list" and rest:
        context.user_data["_cb_data_override"] = f"arv:list:{rest[0]}:{rest[1] if len(rest) > 1 else 0}"
        await review_list(update, context)
    elif action == "view" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_view(update, context)
    elif action == "approve" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_approve(update, context)
    elif action == "reject" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_reject(update, context)
    elif action == "hide" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_hide(update, context)
    elif action == "unhide" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_unhide(update, context)
    elif action == "pin" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_pin(update, context)
    elif action == "delete" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await review_delete(update, context)
    else:
        await review_admin_menu(update, context)
