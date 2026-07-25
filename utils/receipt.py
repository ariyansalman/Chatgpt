"""PDF order receipt generation using reportlab."""

import os
import tempfile
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)

from database import get_db_session, Order, OrderItem, Settings, User
from utils.currency import convert_usd


def generate_receipt_pdf(order_id: int) -> str:
    """Build a receipt PDF for `order_id`. Returns the temp file path."""
    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()
        if not order:
            raise ValueError(f"Order #{order_id} not found")

        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        user = session.query(User).filter_by(id=order.user_id).first()
        s = session.query(Settings).first()

        shop_name = "Digital Store"
        if s and s.welcome_message:
            # Use first line of welcome message as shop name.
            shop_name = s.welcome_message.strip().splitlines()[0][:80]

        # Snapshot data BEFORE session closes
        rows = [(it.product.name, it.quantity, it.price, it.price * it.quantity) for it in items]
        username = (user.username or f"user_{user.telegram_id}") if user else "unknown"
        total = order.total_amount
        created = order.created_at
        status = order.status.value if hasattr(order.status, "value") else str(order.status)

    # Build PDF
    fd, path = tempfile.mkstemp(suffix=f"_receipt_order_{order_id}.pdf")
    os.close(fd)

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=22, textColor=colors.HexColor("#111827"))
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#6B7280"))

    story = []
    story.append(Paragraph(shop_name, h1))
    story.append(Paragraph("Order Receipt", small))
    story.append(Spacer(1, 6 * mm))

    meta = [
        ["Order #", str(order_id)],
        ["Date", created.strftime("%Y-%m-%d %H:%M UTC")],
        ["Customer", f"@{username}"],
        ["Status", status],
    ]
    t = Table(meta, colWidths=[35 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#374151")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    # Items table
    header = ["Product", "Qty", "Unit Price", "Subtotal"]
    data = [header]
    for name, qty, unit, sub in rows:
        data.append([name, str(qty), f"${unit:.2f}", f"${sub:.2f}"])
    data.append(["", "", "Total", f"${total:.2f}"])

    conv, code, symbol = convert_usd(total)
    if conv is not None:
        data.append(["", "", f"~ in {code}", f"{symbol}{conv:,.2f}"])

    tbl = Table(data, colWidths=[85 * mm, 20 * mm, 30 * mm, 30 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -3), [colors.white, colors.HexColor("#F9FAFB")]),
        ("FONTNAME", (2, -2), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -2), (-1, -2), 0.5, colors.HexColor("#9CA3AF")),
        ("GRID", (0, 0), (-1, -3), 0.25, colors.HexColor("#E5E7EB")),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph(
        f"Generated on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} — Thank you for your purchase!",
        small,
    ))

    doc.build(story)
    return path
