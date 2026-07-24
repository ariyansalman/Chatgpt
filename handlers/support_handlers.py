"""Support Center handlers — user-side ticket flow + admin replies.

V16 (Priority-Based Ticketing): every ticket now carries a ``priority``
(low/medium/high/urgent) and an SLA response deadline computed from it
(see ``services/notifications.sla_hours_for`` /
``services/notifications.compute_sla_deadline``). Admins can change the
priority from the ticket view, which recomputes the deadline. A
background job (``services/notifications.sla_reminder_job``) nudges the
admin shortly before — and alerts them right after — a ticket misses
its SLA.
"""

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, User, Settings, SupportTicket, TicketMessage,
    TicketStatus, TicketSender, TicketPriority,
)
from utils import (
    create_support_center_keyboard, create_main_menu_keyboard,
    check_user_banned, is_admin, safe_edit_message_text,
)
from utils.audit import log_admin_action
from config.settings import settings as app_settings
from services.notifications import compute_sla_deadline
from utils.update_proxy import with_data

# Conversation states
TICKET_SUBJECT, TICKET_MESSAGE, TICKET_REPLY, ADMIN_TICKET_REPLY = range(4)
# V20: new state for category selection (must not collide with above)
TICKET_CATEGORY = 4

_PRIORITY_ICON = {
    TicketPriority.LOW: "🔵",
    TicketPriority.MEDIUM: "🟡",
    TicketPriority.HIGH: "🟠",
    TicketPriority.URGENT: "🔴",
}

# ── V20: Ticket categories ─────────────────────────────────────────────────────
_TICKET_CATEGORIES = [
    ("general",   "💬 General"),
    ("payment",   "💳 Payment Issue"),
    ("order",     "📦 Order Problem"),
    ("technical", "🔧 Technical Issue"),
    ("refund",    "↩️ Refund Request"),
    ("other",     "📋 Other"),
]


def _build_category_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for ticket category selection."""
    rows = []
    for cat_id, cat_name in _TICKET_CATEGORIES:
        rows.append([InlineKeyboardButton(cat_name, callback_data=f"sc_cat_{cat_id}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="sc_cancel")])
    return InlineKeyboardMarkup(rows)


def _category_label(cat_id: str) -> str:
    for cid, cname in _TICKET_CATEGORIES:
        if cid == cat_id:
            return cname
    return (cat_id or "general").capitalize()


def _get_user(session, telegram_id):
    return session.query(User).filter_by(telegram_id=telegram_id).first()


def _priority_icon(p: TicketPriority) -> str:
    return _PRIORITY_ICON.get(p, "🟡")


def _sla_line(tk: SupportTicket) -> str:
    """Human-readable SLA status line for a ticket (used in list & detail views)."""
    if tk.status == TicketStatus.CLOSED:
        if tk.resolved_at and tk.sla_deadline:
            late = tk.resolved_at > tk.sla_deadline
            return f"SLA: {'❌ missed' if late else '✅ met'} (resolved {tk.resolved_at.strftime('%b %d %H:%M')})"
        return "SLA: closed"
    if not tk.sla_deadline:
        return ""
    now = datetime.utcnow()
    if tk.sla_deadline <= now or tk.sla_breached:
        return "SLA: 🚨 <b>BREACHED</b>"
    remaining = tk.sla_deadline - now
    hrs = int(remaining.total_seconds() // 3600)
    mins = int((remaining.total_seconds() % 3600) // 60)
    return f"SLA: ⏳ {hrs}h {mins}m left"


# ─── User: Support Center home ──────────────────────────────────────────────
async def support_center_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    if check_user_banned(tid):
        return

    with get_db_session() as session:
        _ = _get_user(session, tid)
        s = session.query(Settings).first()
        support_username = (s.support_username or "").lstrip("@") if s else ""

    text = (
        "🔔 <b>Support Center</b>\n\n"
        "Need assistance? Create a support ticket and our team will reply directly in this bot. "
        "You'll receive a notification as soon as we respond."
    )
    await safe_edit_message_text(query, 
        text,
        reply_markup=create_support_center_keyboard("en", support_username),
        parse_mode="HTML",
    )


# ─── User: Info pages (Terms / FAQ / About) ─────────────────────────────────
# Content is 100% admin-managed — see Admin Panel → Store Settings →
# 🛠 Bot Configuration → 📄 Static Pages (page_terms / page_faq / page_about
# in utils/bot_config.py). No text is hardcoded here; editing this screen
# never requires a code change or restart.
_INFO_PAGES = {
    "terms": ("📄", "Terms of Service", "page_terms"),
    "faq":   ("❓", "FAQ", "page_faq"),
    "about": ("ℹ️", "About Us", "page_about"),
}


async def show_info_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.bot_config import cfg

    query = update.callback_query
    await query.answer()
    page_key = query.data.replace("sc_page_", "")
    emoji, title, cfg_key = _INFO_PAGES.get(page_key, ("📄", "Info", None))
    body = cfg.get_str(cfg_key, "") if cfg_key else ""
    if not body.strip():
        body = "This page hasn't been set up yet. Please check back later."

    text = f"{emoji} <b>{title}</b>\n\n{body}"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="support_center")]]
    await safe_edit_message_text(
        query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML",
    )


# ─── User: List tickets ─────────────────────────────────────────────────────
async def my_tickets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    with get_db_session() as session:
        user = _get_user(session, tid)
        tickets = []
        if user:
            tickets = (session.query(SupportTicket)
                       .filter_by(user_id=user.id)
                       .order_by(SupportTicket.updated_at.desc())
                       .limit(20).all())
        rows = []
        if not tickets:
            body = "📭 You have no tickets yet."
        else:
            body = "📋 <b>Your Tickets</b>"
            for tk in tickets:
                icon = "🟢" if tk.status == TicketStatus.OPEN else "🔒"
                rows.append([InlineKeyboardButton(
                    f"{icon}{_priority_icon(tk.priority)} #{tk.id} — {tk.subject[:35]}",
                    callback_data=f"sc_view_{tk.id}",
                )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="support_center")])
    await safe_edit_message_text(query, body, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


# ─── User: View one ticket ──────────────────────────────────────────────────
async def view_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    ticket_id = int(query.data.replace("sc_view_", ""))

    with get_db_session() as session:
        user = _get_user(session, tid)
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if not tk or (not is_admin(tid) and tk.user_id != user.id):
            await safe_edit_message_text(query, "❌ Ticket not found.")
            return

        header = (f"🎫 <b>Ticket #{tk.id}</b> — {tk.subject}\n"
                  f"Status: {tk.status.value}  ·  Priority: {_priority_icon(tk.priority)} {tk.priority.value.upper()}\n"
                  f"{_sla_line(tk)}")
        lines = [header, ""]
        for m in tk.messages:
            who = "👤 You" if m.sender == TicketSender.USER else "🛠 Support"
            lines.append(f"<b>{who}</b> · {m.created_at.strftime('%b %d %H:%M')}")
            lines.append(m.text)
            lines.append("")
        is_open = tk.status == TicketStatus.OPEN
        tk.status

    rows = []
    if is_open:
        rows.append([InlineKeyboardButton("✍️ Reply", callback_data=f"sc_reply_{ticket_id}")])
        rows.append([InlineKeyboardButton("🔒 Close Ticket", callback_data=f"sc_close_{ticket_id}")])
    else:
        rows.append([InlineKeyboardButton("🔓 Reopen", callback_data=f"sc_reopen_{ticket_id}")])
    rows.append([InlineKeyboardButton("📋 My Tickets", callback_data="sc_list")])

    await safe_edit_message_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


# ─── User: Close / Reopen ───────────────────────────────────────────────────
async def close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ticket_id = int(q.data.replace("sc_close_", ""))
    tid = update.effective_user.id
    with get_db_session() as session:
        user = _get_user(session, tid)
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if tk and (is_admin(tid) or tk.user_id == user.id):
            tk.status = TicketStatus.CLOSED
            tk.resolved_at = datetime.utcnow()
            session.commit()
    await safe_edit_message_text(q, f"🔒 Ticket #{ticket_id} closed.",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 My Tickets", callback_data="sc_list")]]))


async def reopen_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ticket_id = int(q.data.replace("sc_reopen_", ""))
    tid = update.effective_user.id
    with get_db_session() as session:
        user = _get_user(session, tid)
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if tk and (is_admin(tid) or tk.user_id == user.id):
            tk.status = TicketStatus.OPEN
            tk.resolved_at = None
            tk.sla_deadline = compute_sla_deadline(tk.priority)
            tk.sla_reminder_sent = False
            tk.sla_breached = False
            session.commit()
    await safe_edit_message_text(q, f"🔓 Ticket #{ticket_id} reopened.",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 My Tickets", callback_data="sc_list")]]))


# ─── User: Open new ticket (conversation) ───────────────────────────────────
async def new_ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = update.effective_user.id
    with get_db_session() as session:
        _ = _get_user(session, tid)
    await safe_edit_message_text(q, 
        "✏️ Please send the <b>subject</b> of your ticket (short summary):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sc_cancel")]]),
        parse_mode="HTML",
    )
    return TICKET_SUBJECT


async def new_ticket_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = (update.message.text or "").strip()[:500]
    if not subject:
        return TICKET_SUBJECT
    context.user_data["_tk_subject"] = subject
    # V20: Show category picker if the feature is enabled
    try:
        from utils.bot_config import cfg as _cfg
        if _cfg.get_bool("feature_support_categories_enabled", True):
            await update.message.reply_text(
                "📂 Select a <b>category</b> for your ticket:",
                reply_markup=_build_category_keyboard(),
                parse_mode="HTML",
            )
            return TICKET_CATEGORY
    except Exception:
        pass
    # Category feature off — skip straight to message
    context.user_data["_tk_category"] = "general"
    await update.message.reply_text(
        "💬 Now send your <b>message</b> describing the issue:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sc_cancel")]]),
        parse_mode="HTML",
    )
    return TICKET_MESSAGE


async def new_ticket_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picks ticket category — advances to TICKET_MESSAGE state (sc_cat_<cat>)."""
    q = update.callback_query
    await q.answer()
    cat_id = q.data.replace("sc_cat_", "") or "general"
    context.user_data["_tk_category"] = cat_id
    await safe_edit_message_text(
        q,
        "💬 Now send your <b>message</b> describing the issue:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sc_cancel")]]),
        parse_mode="HTML",
    )
    return TICKET_MESSAGE


async def new_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # V20: Accept both text messages and photo attachments
    text = ""
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
        text = (update.message.caption or "").strip() or "[Photo attached]"
    else:
        text = (update.message.text or "").strip()
    if not text and not file_id:
        return TICKET_MESSAGE
    tid = update.effective_user.id
    subject = context.user_data.get("_tk_subject", "Support")
    category = context.user_data.get("_tk_category", "general")

    ticket_id = None
    username = update.effective_user.username or ""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tid).first()
        if not user:
            user = User(telegram_id=tid, username=username)
            session.add(user); session.commit(); session.refresh(user)
        tk = SupportTicket(user_id=user.id, subject=subject, status=TicketStatus.OPEN,
                          priority=TicketPriority.MEDIUM,
                          sla_deadline=compute_sla_deadline(TicketPriority.MEDIUM),
                          category=category)
        session.add(tk); session.commit(); session.refresh(tk)
        session.add(TicketMessage(ticket_id=tk.id, sender=TicketSender.USER, text=text,
                                  file_id=file_id, file_type=file_type))
        session.commit()
        ticket_id = tk.id

    await update.message.reply_text(
        f"✅ Ticket <b>#{ticket_id}</b> created.\nWe'll notify you when support replies.",
        reply_markup=create_main_menu_keyboard(user_id=tid),
        parse_mode="HTML",
    )

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=app_settings.ADMIN_TELEGRAM_ID,
            text=(f"🎫 <b>New Support Ticket #{ticket_id}</b>\n"
                  f"From: <a href='tg://user?id={tid}'>{username or tid}</a>\n"
                  f"Priority: {_priority_icon(TicketPriority.MEDIUM)} MEDIUM (default — tap to escalate)\n"
                  f"Subject: {subject}\n\n{text}"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✍️ Reply", callback_data=f"adm_tk_reply_{ticket_id}"),
                InlineKeyboardButton("👁 View", callback_data=f"adm_tk_view_{ticket_id}"),
            ]]),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.getLogger(__name__).warning("[support] notify admin failed: %s", e)

    # Activity Feed: new support ticket (best-effort, non-blocking)
    try:
        import asyncio as _asyncio
        from services.activity_feed import post_event as _af_post, EVENT_SUPPORT_TICKET
        _asyncio.create_task(_af_post(context.bot, EVENT_SUPPORT_TICKET, {
            "customer_telegram_id": tid,
            "ticket_id": ticket_id,
            "subject": subject,
            "category": str(category) if category else "General",
        }))
    except Exception:
        pass

    context.user_data.pop("_tk_subject", None)
    return ConversationHandler.END


async def new_ticket_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        tid = update.effective_user.id
        with get_db_session() as session:
            _ = _get_user(session, tid)
        await safe_edit_message_text(q, 
            "❌ Cancelled.",
            reply_markup=create_main_menu_keyboard(user_id=tid),
        )
    context.user_data.pop("_tk_subject", None)
    context.user_data.pop("_tk_category", None)  # V20
    return ConversationHandler.END


# ─── User: Reply to existing ticket ─────────────────────────────────────────
async def reply_ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ticket_id = int(q.data.replace("sc_reply_", ""))
    tid = update.effective_user.id
    with get_db_session() as session:
        _ = _get_user(session, tid)
    context.user_data["_tk_reply_id"] = ticket_id
    await safe_edit_message_text(q, 
        f"💬 Send your reply for ticket #{ticket_id}:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sc_cancel")]]),
        parse_mode="HTML",
    )
    return TICKET_REPLY


async def reply_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # V20: Accept text or photo attachment
    text = ""
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
        text = (update.message.caption or "").strip() or "[Photo attached]"
    else:
        text = (update.message.text or "").strip()
    if not text and not file_id:
        return TICKET_REPLY
    tid = update.effective_user.id
    ticket_id = context.user_data.get("_tk_reply_id")

    subject = ""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=tid).first()
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if not tk or tk.user_id != user.id:
            await update.message.reply_text("❌ Ticket not found.")
            return ConversationHandler.END
        tk.status = TicketStatus.OPEN
        session.add(TicketMessage(ticket_id=tk.id, sender=TicketSender.USER, text=text,
                                  file_id=file_id, file_type=file_type))
        session.commit()
        subject = tk.subject

    await update.message.reply_text(
        "✅ Reply sent.",
        reply_markup=create_main_menu_keyboard(user_id=tid),
    )

    try:
        await context.bot.send_message(
            chat_id=app_settings.ADMIN_TELEGRAM_ID,
            text=f"🎫 <b>Reply on ticket #{ticket_id}</b> — {subject}\n\n{text}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✍️ Reply", callback_data=f"adm_tk_reply_{ticket_id}"),
                InlineKeyboardButton("👁 View", callback_data=f"adm_tk_view_{ticket_id}"),
            ]]),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.getLogger(__name__).warning("[support] admin notify failed: %s", e)

    context.user_data.pop("_tk_reply_id", None)
    return ConversationHandler.END


# ─── Admin: list all tickets ────────────────────────────────────────────────
async def admin_tickets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    with get_db_session() as session:
        tickets = (session.query(SupportTicket)
                   .order_by(SupportTicket.status.asc(), SupportTicket.sla_deadline.asc().nullslast())
                   .limit(30).all())
        rows = []
        for tk in tickets:
            u = session.query(User).filter_by(id=tk.user_id).first()
            uname = (u.username or str(u.telegram_id)) if u else "?"
            icon = "🟢" if tk.status == TicketStatus.OPEN else "🔒"
            rows.append([InlineKeyboardButton(
                f"{icon}{_priority_icon(tk.priority)} #{tk.id} · @{uname} · {tk.subject[:25]}",
                callback_data=f"adm_tk_view_{tk.id}",
            )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu")])
    body = "🎫 <b>Support Tickets</b>" if tickets else "📭 No tickets."
    await safe_edit_message_text(q, body, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


async def admin_ticket_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    ticket_id = int(q.data.replace("adm_tk_view_", ""))
    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if not tk:
            await safe_edit_message_text(q, "❌ Ticket not found.")
            return
        u = session.query(User).filter_by(id=tk.user_id).first()
        uname = (u.username or str(u.telegram_id)) if u else "?"
        lines = [
            f"🎫 <b>Ticket #{tk.id}</b> — {tk.subject}",
            f"User: @{uname} (<code>{u.telegram_id if u else '?'}</code>)",
            f"Status: {tk.status.value}  ·  Priority: {_priority_icon(tk.priority)} {tk.priority.value.upper()}",
            f"Category: <b>{_category_label(getattr(tk, 'category', None) or 'general')}</b>",  # V20
            _sla_line(tk),
            "",
        ]
        for m in tk.messages:
            who = "👤 User" if m.sender == TicketSender.USER else "🛠 Admin"
            lines.append(f"<b>{who}</b> · {m.created_at.strftime('%b %d %H:%M')}")
            lines.append(m.text)
            # V20: Show attachment indicator
            if getattr(m, 'file_id', None) and getattr(m, 'file_type', None):
                lines.append(f"  📎 <i>[{m.file_type} attachment]</i>")
            lines.append("")
        is_open = tk.status == TicketStatus.OPEN
        cur_priority = tk.priority

    rows = [[InlineKeyboardButton("✍️ Reply", callback_data=f"adm_tk_reply_{ticket_id}")]]
    if is_open:
        rows.append([InlineKeyboardButton("🔒 Close", callback_data=f"adm_tk_close_{ticket_id}")])
    else:
        rows.append([InlineKeyboardButton("🔓 Reopen", callback_data=f"adm_tk_reopen_{ticket_id}")])
    if is_open:
        prio_row = []
        for p in TicketPriority:
            mark = "✅" if p == cur_priority else _priority_icon(p)
            prio_row.append(InlineKeyboardButton(
                f"{mark} {p.value[:1].upper()}", callback_data=f"adm_tk_pri_{ticket_id}_{p.value}"))
        rows.append(prio_row)
    # V20: Assign to self + delete
    rows.append([
        InlineKeyboardButton("👤 Assign to Me", callback_data=f"adm_tk_assign_{ticket_id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"adm_tk_delete_{ticket_id}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_tickets")])
    await safe_edit_message_text(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


async def admin_ticket_set_priority_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin changes a ticket's priority — recomputes the SLA deadline from now."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    # callback data: adm_tk_pri_<ticket_id>_<priority_value>
    rest = q.data.replace("adm_tk_pri_", "")
    ticket_id_str, priority_val = rest.rsplit("_", 1)
    ticket_id = int(ticket_id_str)
    try:
        new_priority = TicketPriority(priority_val)
    except ValueError:
        await q.answer("Invalid priority.", show_alert=True)
        return

    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if not tk:
            await q.answer("Ticket not found.", show_alert=True)
            return
        tk.priority = new_priority
        if tk.status == TicketStatus.OPEN:
            tk.sla_deadline = compute_sla_deadline(new_priority)
            tk.sla_reminder_sent = False
            tk.sla_breached = False
        session.commit()

    log_admin_action(
        update.effective_user.id, "ticket_set_priority",
        target_type="support_ticket", target_id=ticket_id,
        details=f"priority={new_priority.value}",
    )
    await admin_ticket_view_callback(update, context)


async def admin_ticket_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    ticket_id = int(q.data.replace("adm_tk_close_", ""))
    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if tk:
            tk.status = TicketStatus.CLOSED
            tk.resolved_at = datetime.utcnow()
            session.commit()
    await safe_edit_message_text(q, f"🔒 Ticket #{ticket_id} closed.",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_tickets")]]))


async def admin_ticket_reopen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    ticket_id = int(q.data.replace("adm_tk_reopen_", ""))
    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if tk:
            tk.status = TicketStatus.OPEN
            tk.resolved_at = None
            tk.sla_deadline = compute_sla_deadline(tk.priority)
            tk.sla_reminder_sent = False
            tk.sla_breached = False
            session.commit()
    await safe_edit_message_text(q, f"🔓 Ticket #{ticket_id} reopened.",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_tickets")]]))


async def admin_ticket_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ticket_id = int(q.data.replace("adm_tk_reply_", ""))
    context.user_data["_adm_tk_id"] = ticket_id
    await safe_edit_message_text(q, 
        f"✍️ Send your reply for ticket #{ticket_id}:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_tk_view_{ticket_id}")]]),
    )
    return ADMIN_TICKET_REPLY


async def admin_ticket_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    # V20: Accept text or photo attachment
    text = ""
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
        text = (update.message.caption or "").strip() or "[Photo attached]"
    else:
        text = (update.message.text or "").strip()
    ticket_id = context.user_data.get("_adm_tk_id")
    if not (text or file_id) or not ticket_id:
        return ADMIN_TICKET_REPLY

    user_telegram_id = None
    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if not tk:
            await update.message.reply_text("❌ Ticket not found.")
            return ConversationHandler.END
        session.add(TicketMessage(ticket_id=tk.id, sender=TicketSender.ADMIN, text=text,
                                  file_id=file_id, file_type=file_type))
        tk.status = TicketStatus.OPEN
        session.commit()
        u = session.query(User).filter_by(id=tk.user_id).first()
        if u:
            user_telegram_id = u.telegram_id

    await update.message.reply_text(f"✅ Reply sent for ticket #{ticket_id}.")

    if user_telegram_id:
        try:
            await context.bot.send_message(
                chat_id=user_telegram_id,
                text=f"🔔 <b>Support replied on ticket #{ticket_id}</b>\n\n{text}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👁 View Ticket", callback_data=f"sc_view_{ticket_id}"),
                ]]),
                parse_mode="HTML",
            )
        except Exception as e:
            logging.getLogger(__name__).warning("[support] user notify failed: %s", e)

    context.user_data.pop("_adm_tk_id", None)
    return ConversationHandler.END


# ─── V20: Admin delete / assign ticket ──────────────────────────────────────

async def admin_ticket_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin permanently deletes a support ticket — two-step confirm (adm_tk_delete_<id>)."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    ticket_id = int(q.data.replace("adm_tk_delete_", ""))

    # Two-step confirm
    if context.user_data.get("_del_tk_confirm") == ticket_id:
        context.user_data.pop("_del_tk_confirm", None)
        with get_db_session() as session:
            tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
            if tk:
                # Delete child messages first (cascade may handle this but be safe)
                session.query(TicketMessage).filter_by(ticket_id=ticket_id).delete()
                session.delete(tk)
                session.commit()
        await safe_edit_message_text(
            q,
            f"🗑 Ticket #{ticket_id} permanently deleted.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 All Tickets", callback_data="admin_tickets")
            ]]),
            parse_mode="HTML",
        )
    else:
        context.user_data["_del_tk_confirm"] = ticket_id
        await safe_edit_message_text(
            q,
            f"⚠️ <b>Delete Ticket #{ticket_id}?</b>\n\n"
            f"This will permanently remove the ticket and all messages.\n"
            f"<b>This cannot be undone.</b>\n\nTap again to confirm.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🗑 Yes, Delete Forever",
                    callback_data=f"adm_tk_delete_{ticket_id}"
                )],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"adm_tk_view_{ticket_id}")],
            ]),
            parse_mode="HTML",
        )


async def admin_ticket_assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin assigns a ticket to themselves (adm_tk_assign_<id>)."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    ticket_id = int(q.data.replace("adm_tk_assign_", ""))
    admin_tid = update.effective_user.id

    with get_db_session() as session:
        tk = session.query(SupportTicket).filter_by(id=ticket_id).first()
        if tk:
            tk.assigned_admin_id = admin_tid
            session.commit()

    await q.answer(f"✅ Ticket #{ticket_id} assigned to you.", show_alert=False)
    # Refresh the view
    await admin_ticket_view_callback(with_data(update, f"adm_tk_view_{ticket_id}"), context)
