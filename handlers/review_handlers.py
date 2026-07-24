"""Product reviews — user leaves 1-5★ + comment, others browse reviews."""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import func
from database import get_db_session, User, Product, Order, OrderItem, Review, OrderStatus
from telegram.error import BadRequest

REVIEW_COMMENT = 5201


def product_rating_summary(session, product_id: int):
    """Return (avg_rating_float, count) for a product."""
    from utils.bot_config import cfg
    require_approval = cfg.get_bool("feature_reviews_require_approval", False)
    q = session.query(
        func.avg(Review.rating), func.count(Review.id)
    ).filter(Review.product_id == product_id, Review.is_hidden == False)
    if require_approval:
        q = q.filter(Review.is_approved == True)
    row = q.first()
    avg = float(row[0]) if row and row[0] is not None else 0.0
    cnt = int(row[1] or 0)
    return avg, cnt


def format_stars(avg: float) -> str:
    full = int(round(avg))
    return "⭐" * max(0, min(5, full))


async def product_reviews_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show reviews for a product: callback ^reviews_<product_id>$"""
    query = update.callback_query
    await query.answer()
    try:
        pid = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=pid).first()
        if not product:
            try:
                await query.edit_message_text("❌ Product not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        avg, cnt = product_rating_summary(session, pid)
        from utils.bot_config import cfg
        require_approval = cfg.get_bool("feature_reviews_require_approval", False)
        q = session.query(Review).filter_by(product_id=pid, is_hidden=False)
        if require_approval:
            q = q.filter(Review.is_approved == True)
        # Pinned reviews first, then newest
        reviews = q.order_by(Review.is_pinned.desc(), Review.created_at.desc()).limit(15).all()
        header = (f"⭐ Reviews — {product.name}\n"
                  f"Average: {format_stars(avg)} ({avg:.1f} / 5, {cnt} reviews)\n")
        if not reviews:
            body = "\nNo reviews yet — be the first after your purchase!"
        else:
            body = "\n\n" + "\n\n".join(
                f"{'📌 ' if r.is_pinned else ''}{format_stars(r.rating)}  ({r.rating}/5)\n"
                f"{(r.comment or '_no comment_')[:300]}"
                for r in reviews
            )
        kb = [[InlineKeyboardButton("🔙 Back", callback_data=f"product_{pid}")]]
        try:
            await query.edit_message_text(header + body, reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def review_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: ^review_start_<order_id>_<product_id>$"""
    query = update.callback_query
    await query.answer()
    try:
        _, _, oid, pid = query.data.split("_")
        oid, pid = int(oid), int(pid)
    except (ValueError, IndexError):
        return ConversationHandler.END

    tg_id = update.effective_user.id
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return ConversationHandler.END
        order = session.query(Order).filter_by(id=oid, user_id=user.id).first()
        if not order or order.status != OrderStatus.COMPLETED:
            try:
                await query.edit_message_text("❌ You can only review completed orders.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        owns = session.query(OrderItem).filter_by(order_id=oid, product_id=pid).first()
        if not owns:
            try:
                await query.edit_message_text("❌ That product isn't in this order.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        existing = session.query(Review).filter_by(
            user_id=user.id, product_id=pid, order_id=oid).first()
        if existing:
            try:
                await query.edit_message_text("ℹ️ You've already reviewed this product.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

    context.user_data['review_order_id'] = oid
    context.user_data['review_product_id'] = pid
    kb = [[InlineKeyboardButton(f"{'⭐' * i}", callback_data=f"reviewrate_{i}") for i in (1, 2, 3, 4, 5)]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="review_cancel")])
    try:
        await query.edit_message_text("Rate this product:", reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return REVIEW_COMMENT


async def review_rating_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        rating = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return REVIEW_COMMENT
    if rating < 1 or rating > 5:
        return REVIEW_COMMENT
    context.user_data['review_rating'] = rating
    kb = [[InlineKeyboardButton("⏭ Skip comment", callback_data="review_skip")],
          [InlineKeyboardButton("❌ Cancel", callback_data="review_cancel")]]
    try:
        await query.edit_message_text(
            f"You picked {'⭐' * rating}\n\nNow send a short comment (or Skip):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return REVIEW_COMMENT


async def review_save(update: Update, context: ContextTypes.DEFAULT_TYPE, comment_text: str | None):
    tg_id = update.effective_user.id
    oid = context.user_data.get('review_order_id')
    pid = context.user_data.get('review_product_id')
    rating = context.user_data.get('review_rating')
    if not (oid and pid and rating):
        return ConversationHandler.END
    from utils.bot_config import cfg as _cfg
    require_approval = _cfg.get_bool("feature_reviews_require_approval", False)
    # New reviews are approved=True unless admin has enabled approval requirement
    auto_approved = not require_approval

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return ConversationHandler.END
        session.add(Review(
            user_id=user.id, product_id=pid, order_id=oid,
            rating=int(rating), comment=(comment_text or None),
            is_approved=auto_approved,
        ))
        session.commit()

    # Activity Feed: review submitted (best-effort, non-blocking)
    try:
        import asyncio as _asyncio
        from services.activity_feed import post_event as _af_post, EVENT_REVIEW_SUBMITTED
        _asyncio.create_task(_af_post(context.bot, EVENT_REVIEW_SUBMITTED, {
            "customer_telegram_id": tg_id,
            "product_id": pid,
            "order_id": oid or "—",
            "rating": rating,
        }))
    except Exception:
        pass

    try:
        from services.social_proof import invalidate as _invalidate_social_proof
        _invalidate_social_proof(pid)
    except Exception:
        pass
    for k in ('review_order_id', 'review_product_id', 'review_rating'):
        context.user_data.pop(k, None)
    from utils.bot_config import cfg as _cfg
    pending_msg = ""
    if _cfg.get_bool("feature_reviews_require_approval", False):
        pending_msg = "\n\n🕐 Your review will appear after admin approval."
    msg = f"✅ Thanks — your review was posted!{pending_msg}"
    if update.message:
        await update.message.reply_text(msg)
    else:
        try:
            await update.callback_query.edit_message_text(msg)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return ConversationHandler.END


async def review_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await review_save(update, context, (update.message.text or "").strip()[:500])


async def review_skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await review_save(update, context, None)


# ─── Part 3: User review edit / delete ───────────────────────────────────────

REVIEW_EDIT_COMMENT = 5320
REVIEW_EDIT_RATING  = 5321


async def my_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a user's own submitted reviews: ^my_reviews$"""
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return
        reviews = (session.query(Review)
                   .filter_by(user_id=user.id, is_hidden=False)
                   .order_by(Review.created_at.desc())
                   .limit(20).all())
        rows = []
        for r in reviews:
            product = session.query(Product).filter_by(id=r.product_id).first()
            rows.append({
                "id": r.id, "rating": r.rating,
                "product": product.name[:25] if product else "?",
                "is_approved": r.is_approved,
            })

    if not rows:
        text = "⭐ <b>My Reviews</b>\n\nYou haven't submitted any reviews yet."
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    else:
        text = "⭐ <b>My Reviews</b>\n\nTap a review to edit or delete it."
        kb = []
        for r in rows:
            status = "" if r["is_approved"] else " ⏳"
            kb.append([InlineKeyboardButton(
                f"{'⭐' * r['rating']} {r['product']}{status}",
                callback_data=f"review_manage_{r['id']}"
            )])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def review_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show actions for a user's own review: ^review_manage_<review_id>$"""
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    try:
        review_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return
        review = session.query(Review).filter_by(id=review_id, user_id=user.id, is_hidden=False).first()
        if not review:
            try:
                await query.edit_message_text("❌ Review not found or already removed.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        product = session.query(Product).filter_by(id=review.product_id).first()
        info = {
            "id":        review.id,
            "rating":    review.rating,
            "comment":   review.comment or "— no comment —",
            "product":   product.name if product else "?",
            "is_approved": review.is_approved,
        }

    from datetime import datetime as _dt
    stars = "⭐" * info["rating"]
    status = "✅ Published" if info["is_approved"] else "⏳ Pending Approval"
    text = (
        f"⭐ <b>Your Review</b>\n\n"
        f"Product: {info['product']}\n"
        f"Rating: {stars}\n"
        f"Comment: <i>{info['comment'][:300]}</i>\n"
        f"Status: {status}"
    )
    kb = [
        [InlineKeyboardButton("✏️ Edit Rating",   callback_data=f"review_edit_{review_id}"),
         InlineKeyboardButton("🗑 Delete",         callback_data=f"review_del_{review_id}")],
        [InlineKeyboardButton("🔙 My Reviews",    callback_data="my_reviews")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def review_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start review edit conversation: ^review_edit_<review_id>$"""
    query = update.callback_query
    await query.answer()
    try:
        review_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    tg_id = update.effective_user.id
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if not user:
            return ConversationHandler.END
        review = session.query(Review).filter_by(id=review_id, user_id=user.id, is_hidden=False).first()
        if not review:
            return ConversationHandler.END

    context.user_data["edit_review_id"] = review_id
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{'⭐' * i}", callback_data=f"review_edit_rate_{i}")
        for i in (1, 2, 3, 4, 5)
    ], [InlineKeyboardButton("🚫 Cancel", callback_data="review_edit_cancel")]])
    try:
        await query.edit_message_text(
            "✏️ <b>Edit Review</b>\n\nSelect your new rating:",
            reply_markup=kb, parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return REVIEW_EDIT_RATING


async def review_edit_rating_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new rating pick for edit: ^review_edit_rate_[1-5]$"""
    query = update.callback_query
    await query.answer()
    try:
        rating = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return REVIEW_EDIT_RATING
    context.user_data["edit_review_rating"] = rating
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Keep existing comment", callback_data="review_edit_skip_comment")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="review_edit_cancel")],
    ])
    try:
        await query.edit_message_text(
            f"Rating: {'⭐' * rating}\n\nSend a new comment, or tap <b>Keep existing comment</b>:",
            reply_markup=kb, parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return REVIEW_EDIT_COMMENT


async def review_edit_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new comment text for edit."""
    new_comment = (update.message.text or "").strip()[:500]
    return await _save_review_edit(update, context, new_comment)


async def review_edit_skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep existing comment during edit."""
    await update.callback_query.answer()
    return await _save_review_edit(update, context, None)


async def _save_review_edit(update, context, new_comment):
    """Persist the review edit."""
    from datetime import datetime as _dt
    review_id = context.user_data.pop("edit_review_id", None)
    rating    = context.user_data.pop("edit_review_rating", None)
    if not review_id or not rating:
        return ConversationHandler.END

    from utils.bot_config import cfg as _cfg
    require_approval = _cfg.get_bool("feature_reviews_require_approval", False)
    with get_db_session() as session:
        review = session.query(Review).filter_by(id=review_id).first()
        if review:
            review.rating = int(rating)
            if new_comment is not None:
                review.comment = new_comment
            review.updated_at = _dt.utcnow()
            # If approval required, re-queue for approval on edit
            if require_approval:
                review.is_approved = False
            session.commit()

    try:
        from services.social_proof import invalidate as _invalidate_social_proof
        _invalidate_social_proof(review.product_id)
    except Exception:
        pass

    msg = "✅ Review updated!"
    if require_approval:
        msg += "\n\n🕐 It will reappear after admin approval."

    if update.message:
        await update.message.reply_text(msg)
    else:
        try:
            await update.callback_query.edit_message_text(msg)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return ConversationHandler.END


async def review_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel review edit."""
    for k in ("edit_review_id", "edit_review_rating"):
        context.user_data.pop(k, None)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("✏️ Edit cancelled.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    elif update.message:
        await update.message.reply_text("✏️ Edit cancelled.")
    return ConversationHandler.END


async def review_delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm review deletion: ^review_del_<review_id>$"""
    query = update.callback_query
    await query.answer()
    try:
        review_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete it", callback_data=f"review_del_confirm_{review_id}")],
        [InlineKeyboardButton("🚫 Cancel",         callback_data="my_reviews")],
    ])
    try:
        await query.edit_message_text(
            "🗑 <b>Delete Review</b>\n\nAre you sure you want to delete this review? This cannot be undone.",
            reply_markup=kb, parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def review_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute review deletion: ^review_del_confirm_<review_id>$"""
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    try:
        review_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        return

    pid = None
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if user:
            review = session.query(Review).filter_by(id=review_id, user_id=user.id).first()
            if review:
                pid = review.product_id
                review.is_hidden = True
                session.commit()

    if pid:
        try:
            from services.social_proof import invalidate as _inv
            _inv(pid)
        except Exception:
            pass

    try:
        await query.edit_message_text("🗑 Your review has been deleted.")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def review_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            try:
                await update.callback_query.edit_message_text("Cancelled.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        except Exception:
            pass
    for k in ('review_order_id', 'review_product_id', 'review_rating'):
        context.user_data.pop(k, None)
    return ConversationHandler.END
