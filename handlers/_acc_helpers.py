"""Shared helpers for the V10 admin sub-panels (Suppliers, Batches,
Profit, Quality, Resellers, Delivery Queue, Backups, Integrity).

Keeps each sub-handler tiny and consistent.
"""
from __future__ import annotations

from typing import List, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.helpers import is_admin
from telegram.error import BadRequest

PAGE = 8


def require_admin(func):
    async def _wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            q = getattr(update, "callback_query", None)
            if q:
                await q.answer("⛔ Access denied.", show_alert=True)
            return
        return await func(update, context, *a, **kw)
    return _wrap


def back_root() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Control Center", callback_data="acc:root")


def paginate(items: List, page: int, page_size: int = PAGE) -> Tuple[List, int, int]:
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    return items[page * page_size:(page + 1) * page_size], page, pages


def nav_row(section: str, page: int, pages: int) -> List[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"acc:{section}:list:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="acc:noop"))
    if page < pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"acc:{section}:list:{page + 1}"))
    return row


async def send(update: Update, text: str, kb: InlineKeyboardMarkup):
    q = getattr(update, "callback_query", None)
    if q:
        try:
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                          disable_web_page_preview=True)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return
        except Exception:
            pass
        await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


def fmt_money(v) -> str:
    try:
        return f"${float(v or 0):,.2f}"
    except Exception:
        return "$0.00"
