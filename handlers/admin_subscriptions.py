"""Admin Subscriptions panel — browse active/past-due/cancelled subscriptions
and force-cancel a user's subscription.

Wired into the V9 Admin Control Center dispatcher (see
``handlers/admin_control_center.py``):
  acc:sec:subscriptions                    -> subscriptions_menu (status filter)
  acc:subs:list:<status>:<page>            -> paginated list for that status
  acc:subs:view:<id>                       -> subscription detail
  acc:subs:cancel:<id>                     -> confirm force-cancel
  acc:subs:cancel_do:<id>                  -> perform force-cancel
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services import subscription_service as sub_svc
from utils.helpers import format_price
from ._acc_helpers import require_admin, back_root, send

STATUSES = [
    ("active", "🟢 Active"),
    ("past_due", "🟠 Past due"),
    ("cancelled", "⚪ Cancelled"),
    ("expired", "⚫ Expired"),
]
PAGE_SIZE = 6


def _fmt_date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


@require_admin
async def subscriptions_menu(update, context, status: str = "active", page: int = 0):
    counts = sub_svc.counts_by_status()
    rows, page, pages = sub_svc.list_subscriptions(status=status, page=page,
                                                    page_size=PAGE_SIZE)

    lines = ["♻️ <b>SUBSCRIPTIONS</b>", ""]
    lines.append(" · ".join(f"{label}: {counts.get(key, 0)}" for key, label in STATUSES))
    lines.append("")
    if not rows:
        lines.append(f"No {status} subscriptions.")
    else:
        for r in rows:
            uname = f"@{r.username}" if r.username and r.username != "—" else f"id:{r.user_id}"
            renew = "🔁" if r.auto_renew else "⏸"
            lines.append(
                f"<b>#{r.id}</b> {uname} — {r.product_name}\n"
                f"  {renew} next: {_fmt_date(r.next_billing_date)}  "
                f"{format_price(r.billing_amount)}"
                + (f"  ⚠️ fails:{r.failed_attempts}" if r.failed_attempts else "")
            )

    kb = []
    # Status filter row
    filt_row = []
    for key, label in STATUSES:
        prefix = "▶️ " if key == status else ""
        filt_row.append(InlineKeyboardButton(f"{prefix}{label.split(' ',1)[1]}",
                                              callback_data=f"acc:subs:list:{key}:0"))
    kb.append(filt_row[:2])
    kb.append(filt_row[2:])

    # Per-row "view" buttons (compact, id-only labels)
    view_row = []
    for r in rows:
        view_row.append(InlineKeyboardButton(f"🔎 #{r.id}",
                                              callback_data=f"acc:subs:view:{r.id}"))
        if len(view_row) == 4:
            kb.append(view_row); view_row = []
    if view_row:
        kb.append(view_row)

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"acc:subs:list:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="acc:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"acc:subs:list:{status}:{page+1}"))
    kb.append(nav)
    kb.append([back_root()])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


@require_admin
async def subscription_detail(update, context, sub_id: int):
    d = sub_svc.get_detail(sub_id)
    if not d:
        await send(update, "❌ Subscription not found.",
                  InlineKeyboardMarkup([[back_root()]]))
        return

    lines = [
        f"♻️ <b>Subscription #{d['id']}</b>",
        "",
        f"User: @{d['username']} (tg <code>{d['telegram_id']}</code>)",
        f"Product: {d['product_name']}",
        f"Status: <b>{d['status']}</b>",
        f"Auto-renew: {'ON' if d['auto_renew'] else 'OFF'}",
        f"Started: {_fmt_date(d['starts_at'])}",
        f"Expires: {_fmt_date(d['expires_at'])}",
        f"Next billing: {_fmt_date(d['next_billing_date'])}",
        f"Billing cycle: {d['billing_cycle_days'] or '—'} day(s)",
        f"Amount / cycle: {format_price(d['billing_amount'])}",
        f"Failed attempts: {d['failed_attempts']}",
    ]
    if d["last_billed_at"]:
        lines.append(f"Last billed: {_fmt_date(d['last_billed_at'])}")
    if d["status"] == "cancelled":
        lines.append(f"Cancelled: {_fmt_date(d['cancelled_at'])} ({d['cancel_reason'] or '—'})")

    kb = []
    if d["status"] != "cancelled":
        kb.append([InlineKeyboardButton("🚫 Force-cancel",
                                         callback_data=f"acc:subs:cancel:{sub_id}")])
    kb.append([InlineKeyboardButton("🔔 Send Reminder",
                                     callback_data=f"acc:srm:remind:{sub_id}")])
    kb.append([InlineKeyboardButton("🔙 Back to list",
                                     callback_data=f"acc:subs:list:{d['status']}:0")])
    kb.append([back_root()])

    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


@require_admin
async def cancel_confirm(update, context, sub_id: int):
    d = sub_svc.get_detail(sub_id)
    if not d:
        await send(update, "❌ Subscription not found.",
                  InlineKeyboardMarkup([[back_root()]]))
        return
    kb = [
        [InlineKeyboardButton("✅ Yes, force-cancel",
                              callback_data=f"acc:subs:cancel_do:{sub_id}")],
        [InlineKeyboardButton("↩️ No, go back",
                              callback_data=f"acc:subs:view:{sub_id}")],
    ]
    await send(
        update,
        f"⚠️ Force-cancel subscription <b>#{sub_id}</b> "
        f"({d['product_name']} — @{d['username']})?\n\n"
        f"This stops auto-renewal immediately. The user will keep access "
        f"until <b>{_fmt_date(d['expires_at'])}</b> but will not be re-billed.",
        InlineKeyboardMarkup(kb),
    )


@require_admin
async def cancel_do(update, context, sub_id: int):
    admin_id = update.effective_user.id
    ok = sub_svc.force_cancel(sub_id, admin_id, reason="admin_force_cancel")
    if not ok:
        await send(update, "❌ Could not cancel (not found or already cancelled).",
                  InlineKeyboardMarkup([[back_root()]]))
        return
    d = sub_svc.get_detail(sub_id)
    try:
        if d and d.get("telegram_id"):
            await context.bot.send_message(
                chat_id=d["telegram_id"],
                text=(f"ℹ️ Your subscription to <b>{d['product_name']}</b> has been "
                      f"cancelled by an admin. Auto-renewal is stopped."),
                parse_mode="HTML",
            )
    except Exception:
        pass
    await send(
        update, f"✅ Subscription #{sub_id} force-cancelled.",
        InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔙 Back to list", callback_data="acc:subs:list:cancelled:0")], [back_root()]]),
    )


async def route(action, rest, update, context):
    """Entry point from admin_control_center._route_section_action."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
    if action == "list":
        status = rest[0] if len(rest) > 0 else "active"
        page = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        await subscriptions_menu(update, context, status=status, page=page)
        return
    if action == "view" and rest:
        await subscription_detail(update, context, int(rest[0]))
        return
    if action == "cancel" and rest:
        await cancel_confirm(update, context, int(rest[0]))
        return
    if action == "cancel_do" and rest:
        await cancel_do(update, context, int(rest[0]))
        return
