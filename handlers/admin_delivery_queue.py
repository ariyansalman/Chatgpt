"""Delivery Queue admin panel — list / view / manual retry / cancel."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_db_session
from database.models import DeliveryJob
from services import delivery_queue
from utils.audit import log_admin_action
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send

STATUSES = ("PENDING", "PROCESSING", "RETRY_SCHEDULED", "FAILED", "DELIVERED", "CANCELLED")


@require_admin
async def delivery_menu(update, context, status: str = "PENDING", page: int = 0):
    if status not in STATUSES:
        status = "PENDING"
    with get_db_session() as s:
        rows = (s.query(DeliveryJob)
                 .filter(DeliveryJob.status == status)
                 .order_by(DeliveryJob.created_at.desc())
                 .limit(200).all())
    slice_, page, pages = paginate(rows, page)
    lines = [f"🚚 <b>DELIVERY QUEUE — {status}</b>",
             f"Total: {len(rows)}", ""]
    for j in slice_:
        lines.append(f"  #{j.id} · order {j.order_id} · attempts {j.attempts}/{j.max_attempts}")

    kb = []
    tabs = []
    for st in STATUSES:
        tabs.append(InlineKeyboardButton(("• " + st) if st == status else st,
                    callback_data=f"acc:dlv:tab:{st}"))
    # 2-col tabs
    for i in range(0, len(tabs), 2):
        kb.append(tabs[i:i + 2])
    for j in slice_:
        kb.append([InlineKeyboardButton(f"View #{j.id}",
                    callback_data=f"acc:dlv:view:{j.id}")])
    if pages > 1:
        kb.append(nav_row(f"dlv:list:{status}", page, pages))
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _view(update, jid: int):
    with get_db_session() as s:
        j = s.get(DeliveryJob, jid)
        if not j:
            await send(update, "Not found.", InlineKeyboardMarkup([[back_root()]]))
            return
    t = [
        f"🚚 <b>Delivery job #{j.id}</b>",
        f"Order: {j.order_id}",
        f"Status: {j.status}",
        f"Attempts: {j.attempts}/{j.max_attempts}",
        f"Inventory assigned: {'yes' if j.inventory_assigned else 'no'}",
        f"Next retry: {j.next_retry_at or '—'}",
        f"Last error: {j.last_error_category or '—'}",
        f"Summary: {j.last_error_summary or '—'}",
    ]
    kb = []
    if j.status in ("FAILED", "RETRY_SCHEDULED", "PENDING"):
        kb.append([InlineKeyboardButton("🔁 Retry now",
                    callback_data=f"acc:dlv:retry:{j.id}")])
    if j.status in ("PENDING", "RETRY_SCHEDULED"):
        kb.append([InlineKeyboardButton("🛑 Cancel job",
                    callback_data=f"acc:dlv:cancel:{j.id}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:dlv:list:PENDING:0"),
               back_root()])
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


async def _retry(update, jid: int):
    """Manual retry — resets to PENDING for the runner (or the ops team)."""
    with get_db_session() as s:
        j = s.get(DeliveryJob, jid)
        if not j:
            return
        j.status = "PENDING"
        j.next_retry_at = None
        s.commit()
    try:
        log_admin_action(update.effective_user.id, "delivery_manual_retry",
                         f"job_id={jid}")
    except Exception:
        pass
    await _view(update, jid)


async def _cancel(update, jid: int):
    delivery_queue.cancel(jid, update.effective_user.id)
    try:
        log_admin_action(update.effective_user.id, "delivery_cancelled",
                         f"job_id={jid}")
    except Exception:
        pass
    await _view(update, jid)


async def route(action, rest, update, context):
    if action == "tab" and rest:
        await delivery_menu(update, context, status=rest[0], page=0)
    elif action == "list":
        status = rest[0] if rest else "PENDING"
        page = int(rest[1]) if len(rest) > 1 else 0
        await delivery_menu(update, context, status=status, page=page)
    elif action == "view" and rest:
        await _view(update, int(rest[0]))
    elif action == "retry" and rest:
        await _retry(update, int(rest[0]))
    elif action == "cancel" and rest:
        await _cancel(update, int(rest[0]))
