"""Inventory quality dashboard — read-only counts + list of recent issues.

Also hosts the V16 SLA compliance report (priority-based ticketing),
which reads the same ``priority`` / ``sla_deadline`` / ``sla_breached``
columns that ``handlers/support_handlers.py`` and
``handlers/dispute_handlers.py`` maintain.
"""
from __future__ import annotations


from sqlalchemy import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_db_session
from database.models import (
    InventoryIssue, ProductKey, Supplier,
    SupportTicket, TicketStatus, Dispute, DisputeStatus, TicketPriority,
)
from ._acc_helpers import require_admin, back_root, paginate, nav_row, send


@require_admin
async def quality_menu(update, context, page: int = 0):
    with get_db_session() as s:
        totals = dict(
            (t, s.query(func.count(InventoryIssue.id))
                  .filter(InventoryIssue.issue_type == t).scalar() or 0)
            for t in ("INVALID", "DUPLICATE", "EXPIRED",
                      "DELIVERY_FAILED", "REPLACED", "UNDER_REVIEW"))
        total_keys = s.query(func.count(ProductKey.id)).scalar() or 0
        total_issues = sum(totals.values())
        fail_rate = (total_issues / total_keys * 100.0) if total_keys else 0.0
        recent = (s.query(InventoryIssue)
                    .order_by(InventoryIssue.created_at.desc())
                    .limit(60).all())
        slice_, page, pages = paginate(recent, page)
        # Prefetch supplier names
        sup_ids = {i.supplier_id for i in slice_ if i.supplier_id}
        sup_map = {sup.id: sup.name for sup in
                   s.query(Supplier).filter(Supplier.id.in_(sup_ids)).all()} if sup_ids else {}

    t = [
        "🧪 <b>INVENTORY QUALITY</b>",
        f"Total keys: {total_keys}  ·  Reported issues: {total_issues}",
        f"Overall failure rate: <b>{fail_rate:.2f}%</b>",
        "",
        f"  Invalid: {totals['INVALID']}",
        f"  Duplicate: {totals['DUPLICATE']}",
        f"  Expired: {totals['EXPIRED']}",
        f"  Delivery failed: {totals['DELIVERY_FAILED']}",
        f"  Replaced: {totals['REPLACED']}",
        f"  Under review: {totals['UNDER_REVIEW']}",
        "",
        "<b>Recent issues:</b>",
    ]
    if not slice_:
        t.append("  (none)")
    for iss in slice_:
        who = sup_map.get(iss.supplier_id, "—")
        t.append(f"  #{iss.id} {iss.issue_type} · order {iss.order_id or '—'} · supplier {who}")

    kb: list = []
    if pages > 1:
        kb.append(nav_row("qual", page, pages))
    kb.append([InlineKeyboardButton("⏱ SLA Compliance Report", callback_data="acc:qual:sla:0")])
    kb.append([back_root()])
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


# ─── V16: Priority-Based Ticketing — SLA compliance report ────────────────
def _priority_breakdown(session, model, status_open_value, status_field="status"):
    """Return {priority_value: {open, breached, resolved, resolved_late}} for a model."""
    out = {p.value: {"open": 0, "breached_open": 0, "resolved": 0, "resolved_late": 0} for p in TicketPriority}
    rows = session.query(model).all()
    for row in rows:
        prio = row.priority.value if row.priority else TicketPriority.MEDIUM.value
        status = getattr(row, status_field)
        is_open = (status == status_open_value)
        if is_open:
            out[prio]["open"] += 1
            if row.sla_breached:
                out[prio]["breached_open"] += 1
        else:
            resolved_at = getattr(row, "resolved_at", None)
            if resolved_at is not None:
                out[prio]["resolved"] += 1
                if row.sla_deadline and resolved_at > row.sla_deadline:
                    out[prio]["resolved_late"] += 1
    return out


@require_admin
async def sla_report(update, context, page: int = 0):
    with get_db_session() as s:
        tk_stats = _priority_breakdown(s, SupportTicket, TicketStatus.OPEN)
        dp_stats = _priority_breakdown(s, Dispute, DisputeStatus.OPENED)

        open_tickets_with_sla = s.query(func.count(SupportTicket.id)).filter(
            SupportTicket.status == TicketStatus.OPEN,
            SupportTicket.sla_deadline.isnot(None)).scalar() or 0
        breached_open_tickets = s.query(func.count(SupportTicket.id)).filter(
            SupportTicket.status == TicketStatus.OPEN,
            SupportTicket.sla_breached == True).scalar() or 0  # noqa: E712

        open_disputes_with_sla = s.query(func.count(Dispute.id)).filter(
            Dispute.status == DisputeStatus.OPENED,
            Dispute.sla_deadline.isnot(None)).scalar() or 0
        breached_open_disputes = s.query(func.count(Dispute.id)).filter(
            Dispute.status == DisputeStatus.OPENED,
            Dispute.sla_breached == True).scalar() or 0  # noqa: E712

        resolved_tickets = s.query(SupportTicket).filter(
            SupportTicket.status == TicketStatus.CLOSED,
            SupportTicket.resolved_at.isnot(None),
            SupportTicket.sla_deadline.isnot(None)).all()
        resolved_disputes = s.query(Dispute).filter(
            Dispute.status == DisputeStatus.RESOLVED,
            Dispute.resolved_at.isnot(None),
            Dispute.sla_deadline.isnot(None)).all()

    def _compliance(resolved_rows):
        total = len(resolved_rows)
        if not total:
            return 0, 0, 100.0
        late = sum(1 for r in resolved_rows if r.resolved_at > r.sla_deadline)
        met = total - late
        return total, late, round(met / total * 100.0, 1)

    tk_total, tk_late, tk_rate = _compliance(resolved_tickets)
    dp_total, dp_late, dp_rate = _compliance(resolved_disputes)

    t = [
        "⏱ <b>SLA COMPLIANCE REPORT</b>",
        "",
        "<b>🎫 Support Tickets</b>",
        f"  Open (SLA-tracked): {open_tickets_with_sla}  ·  Currently breached: {breached_open_tickets}",
        f"  Resolved (with SLA): {tk_total}  ·  Missed: {tk_late}  ·  <b>Compliance: {tk_rate}%</b>",
        "",
        "<b>🚨 Disputes</b>",
        f"  Open (SLA-tracked): {open_disputes_with_sla}  ·  Currently breached: {breached_open_disputes}",
        f"  Resolved (with SLA): {dp_total}  ·  Missed: {dp_late}  ·  <b>Compliance: {dp_rate}%</b>",
        "",
        "<b>By priority — tickets</b>",
    ]
    for p in TicketPriority:
        st = tk_stats[p.value]
        t.append(f"  {p.value.upper():<7} open:{st['open']:>3} breached:{st['breached_open']:>3} "
                 f"resolved:{st['resolved']:>3} late:{st['resolved_late']:>3}")
    t.append("")
    t.append("<b>By priority — disputes</b>")
    for p in TicketPriority:
        st = dp_stats[p.value]
        t.append(f"  {p.value.upper():<7} open:{st['open']:>3} breached:{st['breached_open']:>3} "
                 f"resolved:{st['resolved']:>3} late:{st['resolved_late']:>3}")

    kb = [[InlineKeyboardButton("🔙 Back to Quality", callback_data="acc:qual:list:0")], [back_root()]]
    await send(update, "\n".join(t), InlineKeyboardMarkup(kb))


async def route(action, rest, update, context):
    if action == "list":
        await quality_menu(update, context, page=int(rest[0]) if rest else 0)
    elif action == "sla":
        await sla_report(update, context)
