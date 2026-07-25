"""Dispute handling for orders.

V16 (Priority-Based Ticketing): disputes now carry a ``priority``
(low/medium/high/urgent, default HIGH since money is on the line) and
an SLA resolution deadline. Admins can escalate/de-escalate priority
from the dispute detail view, which recomputes the deadline. See
``services/notifications.py`` for the SLA reminder job and
``handlers/admin_quality.py`` for the SLA compliance report.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import get_db_session, User, Order, Dispute, DisputeStatus, TicketPriority
from utils import format_price, format_datetime, notify_admin, create_cancel_keyboard
from utils.audit import log_admin_action
from utils.helpers import is_admin
from services.notifications import compute_sla_deadline
from datetime import datetime
from telegram.error import BadRequest

# Conversation states
DISPUTE_REASON = 0

_PRIORITY_ICON = {
    TicketPriority.LOW: "🔵",
    TicketPriority.MEDIUM: "🟡",
    TicketPriority.HIGH: "🟠",
    TicketPriority.URGENT: "🔴",
}


def _priority_icon(p: TicketPriority) -> str:
    return _PRIORITY_ICON.get(p, "🟠")


def _sla_line(d: Dispute) -> str:
    if d.status == DisputeStatus.RESOLVED:
        if d.resolved_at and d.sla_deadline:
            late = d.resolved_at > d.sla_deadline
            return f"SLA: {'❌ missed' if late else '✅ met'} (resolved {d.resolved_at.strftime('%b %d %H:%M')})"
        return "SLA: resolved"
    if not d.sla_deadline:
        return ""
    now = datetime.utcnow()
    if d.sla_deadline <= now or d.sla_breached:
        return "SLA: 🚨 <b>BREACHED</b>"
    remaining = d.sla_deadline - now
    hrs = int(remaining.total_seconds() // 3600)
    mins = int((remaining.total_seconds() % 3600) // 60)
    return f"SLA: ⏳ {hrs}h {mins}m left"


async def open_dispute_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start dispute opening flow - ask for reason."""
    query = update.callback_query
    await query.answer()

    # Extract order_id from callback data
    order_id = int(query.data.split("_")[2])
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            try:
                await query.edit_message_text("❌ Order not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

        # Check if dispute already exists
        if order.dispute_status != DisputeStatus.NIL:
            try:
                await query.edit_message_text(
                    f"⚠️ This order already has a dispute with status: {order.dispute_status.value}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="order_history")]])
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END

    # Store order_id in context for later use
    context.user_data['dispute_order_id'] = order_id

    try:
        await query.edit_message_text(
            f"🚨 Open Dispute for Order #{order_id}\n\n"
            f"Please describe the issue with your order.\n"
            f"Be as detailed as possible to help us resolve this quickly:",
            reply_markup=create_cancel_keyboard()
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

    return DISPUTE_REASON


async def dispute_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive dispute reason and create dispute."""
    reason = update.message.text
    order_id = context.user_data.get('dispute_order_id')
    user_id = update.effective_user.id

    if not order_id:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ User not found.")
            return ConversationHandler.END

        order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            await update.message.reply_text("❌ Order not found.")
            return ConversationHandler.END

        # Create dispute
        dispute = Dispute(
            order_id=order.id,
            user_id=user.id,
            reason=reason,
            status=DisputeStatus.OPENED,
            created_at=datetime.utcnow(),
            priority=TicketPriority.HIGH,
        )
        dispute.sla_deadline = compute_sla_deadline(TicketPriority.HIGH, dispute.created_at)
        session.add(dispute)

        # Update order dispute status
        order.dispute_status = DisputeStatus.OPENED
        session.commit()

        # Get order details for admin notification (before session closes)
        username = update.effective_user.username or "No username"
        order_total = order.total_amount
        dispute_id = dispute.id

    # Notify admin about new dispute
    admin_message = f"""🚨 NEW DISPUTE OPENED

Order ID: #{order_id}
User: @{username} (ID: {user_id})
Amount: {format_price(order_total)}
Priority: {_priority_icon(TicketPriority.HIGH)} HIGH (default — tap to adjust)

Reason:
{reason}

Use /admin to manage disputes."""

    await notify_admin(context, admin_message)

    # Confirm to user
    keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="order_history")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Dispute opened successfully!\n\n"
        f"Your dispute for Order #{order_id} has been submitted.\n"
        f"Our admin team will review it and contact you soon.\n\n"
        f"Dispute ID: #{dispute_id}",
        reply_markup=reply_markup
    )

    # Clear context
    context.user_data.pop('dispute_order_id', None)

    return ConversationHandler.END


async def dispute_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel dispute creation."""
    query = update.callback_query
    await query.answer()

    # Clear context
    context.user_data.pop('dispute_order_id', None)

    keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="order_history")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            "❌ Dispute opening cancelled.",
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

    return ConversationHandler.END


# ============================================================================
# ADMIN DISPUTE HANDLERS
# ============================================================================

async def admin_view_disputes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all open disputes for admin."""
    query = update.callback_query
    await query.answer()

    with get_db_session() as session:
        # Get all open disputes
        disputes = (session.query(Dispute)
                   .filter_by(status=DisputeStatus.OPENED)
                   .order_by(Dispute.sla_deadline.asc().nullslast())
                   .all())

        if not disputes:
            keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(
                    "✅ No open disputes at the moment.",
                    reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        # Build keyboard with dispute buttons
        keyboard = []
        for dispute in disputes:
            order = session.query(Order).filter_by(id=dispute.order_id).first()
            user = session.query(User).filter_by(id=dispute.user_id).first()

            button_text = (f"{_priority_icon(dispute.priority)} Dispute #{dispute.id} | "
                          f"Order #{order.id} | @{user.username or 'No username'}")
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"admin_dispute_detail_{dispute.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = f"🚨 Open Disputes ({len(disputes)})\n\nClick on a dispute to view details and resolve:"

        try:
            await query.edit_message_text(message, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_dispute_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show dispute details with resolution buttons for admin."""
    query = update.callback_query
    await query.answer()

    # Extract dispute_id from callback data
    dispute_id = int(query.data.split("_")[3])

    with get_db_session() as session:
        dispute = session.query(Dispute).filter_by(id=dispute_id).first()

        if not dispute:
            try:
                await query.edit_message_text("❌ Dispute not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        order = session.query(Order).filter_by(id=dispute.order_id).first()
        user = session.query(User).filter_by(id=dispute.user_id).first()

        # Get order items for display
        from database import OrderItem
        order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

        items_text = ""
        for item in order_items:
            items_text += f"  📦 {item.product.name} (x{item.quantity}) - {format_price(item.price * item.quantity)}\n"

        # Build message
        status_emoji = {
            DisputeStatus.OPENED: "🚨",
            DisputeStatus.RESOLVED: "✔️",
            DisputeStatus.NIL: "❓"
        }.get(dispute.status, "❓")

        message = f"""🚨 Dispute Details

Dispute ID: #{dispute.id}
Status: {status_emoji} {dispute.status.value}
Priority: {_priority_icon(dispute.priority)} {dispute.priority.value.upper()}
{_sla_line(dispute)}

📋 Order Information:
Order ID: #{order.id}
Order Status: {order.status.value}
Total Amount: {format_price(order.total_amount)}
Date: {format_datetime(order.created_at)}

👤 User Information:
Username: @{user.username or 'No username'}
Telegram ID: {user.telegram_id}

📦 Order Items:
{items_text}
📝 Dispute Reason:
{dispute.reason}

Opened: {format_datetime(dispute.created_at)}"""

        if dispute.admin_notes:
            message += f"\n\n📌 Admin Notes:\n{dispute.admin_notes}"

        if dispute.resolved_at:
            message += f"\n\nResolved: {format_datetime(dispute.resolved_at)}"

        # Build keyboard
        keyboard = []

        if dispute.status == DisputeStatus.OPENED:
            keyboard.append([InlineKeyboardButton("✅ Resolve Dispute", callback_data=f"resolve_dispute_{dispute.id}")])
            prio_row = []
            for p in TicketPriority:
                mark = "✅" if p == dispute.priority else _priority_icon(p)
                prio_row.append(InlineKeyboardButton(
                    f"{mark} {p.value[:1].upper()}", callback_data=f"adm_disp_pri_{dispute.id}_{p.value}"))
            keyboard.append(prio_row)

        keyboard.append([InlineKeyboardButton("🔙 Back to Disputes", callback_data="admin_view_disputes")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(message, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def admin_dispute_set_priority_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin escalates/de-escalates a dispute's priority — recomputes the SLA deadline from now."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    # callback data: adm_disp_pri_<dispute_id>_<priority_value>
    rest = query.data.replace("adm_disp_pri_", "")
    dispute_id_str, priority_val = rest.rsplit("_", 1)
    dispute_id = int(dispute_id_str)
    try:
        new_priority = TicketPriority(priority_val)
    except ValueError:
        await query.answer("Invalid priority.", show_alert=True)
        return

    with get_db_session() as session:
        dispute = session.query(Dispute).filter_by(id=dispute_id).first()
        if not dispute:
            await query.answer("Dispute not found.", show_alert=True)
            return
        dispute.priority = new_priority
        if dispute.status == DisputeStatus.OPENED:
            dispute.sla_deadline = compute_sla_deadline(new_priority)
            dispute.sla_reminder_sent = False
            dispute.sla_breached = False
        session.commit()

    log_admin_action(
        update.effective_user.id, "dispute_set_priority",
        target_type="dispute", target_id=dispute_id,
        details=f"priority={new_priority.value}",
    )
    await admin_dispute_detail_callback(update, context)


async def admin_resolve_dispute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve a dispute - mark as resolved and notify user."""
    query = update.callback_query
    await query.answer()

    # Extract dispute_id from callback data
    dispute_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        dispute = session.query(Dispute).filter_by(id=dispute_id).first()

        if not dispute:
            try:
                await query.edit_message_text("❌ Dispute not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return

        order = session.query(Order).filter_by(id=dispute.order_id).first()
        user = session.query(User).filter_by(id=dispute.user_id).first()

        # Update dispute status
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolved_at = datetime.utcnow()

        # Update order dispute status
        order.dispute_status = DisputeStatus.RESOLVED

        session.commit()

        # Get user telegram_id before closing session
        user_telegram_id = user.telegram_id
        order_id = order.id

    # Notify user about dispute resolution
    try:
        user_keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        user_reply_markup = InlineKeyboardMarkup(user_keyboard)

        await context.bot.send_message(
            chat_id=user_telegram_id,
            text=f"✅ Your dispute for Order #{order_id} has been resolved.\n\n"
                 f"Thank you for your patience. If you have any further questions, please contact support.",
            reply_markup=user_reply_markup
        )
    except Exception as e:
        logging.getLogger(__name__).warning("dispute resolution notify user failed: %s", e)

    # Confirm to admin
    keyboard = [[InlineKeyboardButton("🔙 Back to Disputes", callback_data="admin_view_disputes")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            f"✅ Dispute #{dispute_id} has been resolved!\n\n"
            f"User has been notified.",
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
