"""Admin: toggle the 'Featured' badge on a product (Section 14)."""
from __future__ import annotations

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import Product
from utils.permissions import has_permission


async def toggle_featured(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_permission(update.effective_user.id, "manage_products"):
        return
    try:
        pid = int(q.data.split("_")[-1])
    except Exception:
        return
    with get_db_session() as s:
        p = s.query(Product).filter_by(id=pid).first()
        if not p:
            await q.answer("Not found", show_alert=True)
            return
        p.is_featured = not bool(p.is_featured)
        s.commit()
        state = "ON" if p.is_featured else "OFF"
    await q.answer(f"⭐ Featured: {state}", show_alert=False)


def register_handlers(app):
    app.add_handler(CallbackQueryHandler(
        toggle_featured, pattern=r"^adm_feature_\d+$"))
