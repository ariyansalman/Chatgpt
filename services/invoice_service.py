"""Automatic PDF invoice generation + delivery.

Builds a professional PDF invoice (logo, business info, itemized order
lines, payment info) for a completed order using ReportLab, and DMs it to
the buyer over Telegram.

This module is intentionally standalone from ``utils/receipt.py`` (the
existing "Download Receipt" button flow, which produces a plainer manual
receipt). Invoices generated here are richer and are sent automatically —
see the hook in ``services/order_lifecycle.py``.

Design notes
------------
* ``generate_invoice_pdf`` is synchronous (DB + ReportLab, both blocking)
  and mirrors the session-then-build pattern already used by
  ``utils/receipt.py``: snapshot everything out of the DB session first,
  then build the PDF from plain Python values.
* ``send_invoice_pdf`` is async and never raises — a failure to email/DM an
  invoice must never break the order/payment flow that triggered it. It
  accepts an optional already-running ``telegram.Bot`` (reused from a PTB
  handler's ``context.bot``); if none is given it spins up a short-lived
  standalone ``Bot`` so this also works from background jobs that have no
  live PTB context (e.g. the delivery queue).
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from config.settings import settings
from database import get_db_session
from database.models import Order, OrderItem, Settings, User, WalletLedger
from utils.bot_config import cfg
from utils.currency import convert_usd

logger = logging.getLogger(__name__)

BRAND_DARK = colors.HexColor("#111827")
BRAND_MUTED = colors.HexColor("#6B7280")
BRAND_LINE = colors.HexColor("#E5E7EB")
BRAND_ACCENT = colors.HexColor("#2563EB")
BRAND_ROW_ALT = colors.HexColor("#F9FAFB")


def _business_info(session) -> dict:
    """Collect business/branding fields from Settings + BotConfig."""
    s = session.query(Settings).first()

    name = cfg.get_str("business_name", "").strip()
    if not name:
        if s and s.welcome_message:
            name = s.welcome_message.strip().splitlines()[0][:80]
        else:
            name = "Digital Store"

    logo_path = (s.store_logo_path if s and s.store_logo_path else None)
    if not logo_path or not os.path.isfile(logo_path):
        # Fall back to the default logo shipped under assets/logos/.
        default_logo = os.path.join(settings.LOGOS_DIR, "img.png")
        logo_path = default_logo if os.path.isfile(default_logo) else None
        # A 0-byte / corrupt placeholder file should not blow up PDF build.
        if logo_path and os.path.getsize(logo_path) < 16:
            logo_path = None

    return {
        "name": name,
        "logo_path": logo_path,
        "address": cfg.get_str("business_address", "").strip(),
        "email": cfg.get_str("business_email", "").strip(),
        "phone": cfg.get_str("business_phone", "").strip(),
        "support_username": (s.support_username if s and s.support_username else None),
        "channel_username": (s.channel_username if s and s.channel_username else None),
        "footer": cfg.get_str("receipt_footer", "Thank you for shopping with us!"),
    }


def _payment_info(session, order: Order) -> dict:
    """Best-effort payment summary for the order (wallet ledger, if logged)."""
    ledger = (
        session.query(WalletLedger)
        .filter(
            WalletLedger.ref_type == "order",
            WalletLedger.ref_id == str(order.id),
        )
        .order_by(WalletLedger.created_at.desc())
        .first()
    )
    if ledger:
        return {
            "method": "Wallet Balance",
            "amount": abs(ledger.delta),
            "paid_at": ledger.created_at,
            "reference": f"WL-{ledger.id}",
        }
    return {
        "method": "Wallet Balance",
        "amount": order.total_amount,
        "paid_at": order.completed_at or order.created_at,
        "reference": f"ORDER-{order.id}",
    }


def generate_invoice_pdf(order_id: int) -> str:
    """Build a professional PDF invoice for ``order_id``. Returns the temp file path.

    Raises ``ValueError`` if the order doesn't exist.
    """
    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()
        if not order:
            raise ValueError(f"Order #{order_id} not found")

        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        user = session.query(User).filter_by(id=order.user_id).first()

        biz = _business_info(session)
        pay = _payment_info(session, order)

        # Snapshot everything BEFORE the session closes.
        rows = []
        for it in items:
            label = it.product.name if it.product else f"Product #{it.product_id}"
            if it.variant is not None:
                label += f" ({it.variant.name})"
            rows.append((label, it.quantity, it.price, it.price * it.quantity))

        username = (user.username or f"user_{user.telegram_id}") if user else "unknown"
        telegram_id = user.telegram_id if user else None
        total = order.total_amount
        currency = order.currency or "USD"
        created = order.created_at
        completed = order.completed_at
        order_currency_conv = convert_usd(total)

    # ── Build PDF ────────────────────────────────────────────────────────
    fd, path = tempfile.mkstemp(suffix=f"_invoice_order_{order_id}.pdf")
    os.close(fd)

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Invoice #{order_id}",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=20, textColor=BRAND_DARK, leading=24)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, textColor=BRAND_ACCENT,
                        spaceBefore=2, spaceAfter=4)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, textColor=BRAND_MUTED, leading=13)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14)

    story = []

    # ── Header: logo + business info | INVOICE title + meta ────────────
    invoice_no = f"INV-{order_id:06d}"
    biz_block = [Paragraph(f"<b>{biz['name']}</b>", ParagraphStyle(
        "bizname", parent=body, fontSize=13, textColor=BRAND_DARK))]
    contact_lines = [l for l in (biz["address"], biz["email"], biz["phone"]) if l]
    if biz["support_username"]:
        contact_lines.append(f"Support: @{biz['support_username']}")
    for line in contact_lines:
        biz_block.append(Paragraph(line, small))

    meta_block = [
        Paragraph("INVOICE", h1),
        Paragraph(f"№ {invoice_no}", small),
        Paragraph(f"Order #{order_id}", small),
        Paragraph(f"Date: {created.strftime('%Y-%m-%d %H:%M UTC') if created else '—'}", small),
        Paragraph(f"Status: <b>PAID</b>", ParagraphStyle(
            "status", parent=small, textColor=colors.HexColor("#16A34A"))),
    ]

    if biz["logo_path"]:
        try:
            logo = Image(biz["logo_path"], width=28 * mm, height=28 * mm, kind="proportional")
        except Exception:
            logo = Paragraph("", body)
        header_left = Table([[logo, biz_block]], colWidths=[32 * mm, 65 * mm])
        header_left.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
    else:
        header_left = Table([[biz_block]], colWidths=[97 * mm])
        header_left.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    header = Table([[header_left, meta_block]], colWidths=[97 * mm, 63 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header)
    story.append(Spacer(1, 4 * mm))
    story.append(Table([[""]], colWidths=[160 * mm], style=TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 1, BRAND_LINE),
    ])))
    story.append(Spacer(1, 6 * mm))

    # ── Bill To ──────────────────────────────────────────────────────────
    story.append(Paragraph("BILL TO", h2))
    bill_lines = [f"@{username}" if username != "unknown" else "Telegram User"]
    if telegram_id:
        bill_lines.append(f"Telegram ID: {telegram_id}")
    for line in bill_lines:
        story.append(Paragraph(line, body))
    story.append(Spacer(1, 8 * mm))

    # ── Items table ──────────────────────────────────────────────────────
    header_row = ["Item", "Qty", "Unit Price", "Subtotal"]
    data = [header_row]
    for name, qty, unit, sub in rows:
        data.append([Paragraph(name, body), str(qty), f"${unit:.2f}", f"${sub:.2f}"])
    data.append(["", "", "Total", f"${total:.2f}"])
    if order_currency_conv[0] is not None:
        conv_amount, conv_code, conv_symbol = order_currency_conv
        data.append(["", "", f"≈ {conv_code}", f"{conv_symbol}{conv_amount:,.2f}"])

    tbl = Table(data, colWidths=[85 * mm, 20 * mm, 27.5 * mm, 27.5 * mm])
    n_item_rows = len(rows)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, n_item_rows), [colors.white, BRAND_ROW_ALT]),
        ("FONTNAME", (2, n_item_rows + 1), (-1, n_item_rows + 1), "Helvetica-Bold"),
        ("LINEABOVE", (0, n_item_rows + 1), (-1, n_item_rows + 1), 0.5, colors.HexColor("#9CA3AF")),
        ("GRID", (0, 0), (-1, n_item_rows), 0.25, BRAND_LINE),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10 * mm))

    # ── Payment info ─────────────────────────────────────────────────────
    story.append(Paragraph("PAYMENT INFORMATION", h2))
    paid_at = pay["paid_at"] or completed or created
    pay_rows = [
        ["Payment Method:", pay["method"]],
        ["Amount Charged:", f"${pay['amount']:.2f} {currency}"],
        ["Paid At:", paid_at.strftime("%Y-%m-%d %H:%M UTC") if paid_at else "—"],
        ["Reference:", pay["reference"]],
    ]
    pay_tbl = Table(pay_rows, colWidths=[40 * mm, 120 * mm])
    pay_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#374151")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(pay_tbl)
    story.append(Spacer(1, 12 * mm))

    # ── Footer ───────────────────────────────────────────────────────────
    story.append(Table([[""]], colWidths=[160 * mm], style=TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, BRAND_LINE),
    ])))
    story.append(Spacer(1, 3 * mm))
    if biz["footer"]:
        story.append(Paragraph(biz["footer"], small))
    story.append(Paragraph(
        f"Generated on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} — this invoice is a "
        f"system-generated document and does not require a signature.",
        small,
    ))

    doc.build(story)
    return path


async def send_invoice_pdf(order_id: int, bot: Optional[Any] = None,
                            lang: Optional[str] = None) -> bool:
    """Generate the invoice PDF for ``order_id`` and DM it to the buyer.

    Best-effort: any failure is logged and swallowed (returns False) so this
    can safely be fired-and-forgotten from the order lifecycle hook.
    """
    import asyncio

    path: Optional[str] = None
    try:
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()
            if not order:
                logger.warning("send_invoice_pdf: order %s not found", order_id)
                return False
            user = session.query(User).filter_by(id=order.user_id).first()
            if not user:
                logger.warning("send_invoice_pdf: user for order %s not found", order_id)
                return False
            telegram_id = user.telegram_id
            total = order.total_amount

        if lang is None:
            try:
                from i18n import get_user_language
                lang = get_user_language(telegram_id)
            except Exception:
                lang = "en"

        try:
            from i18n import t as _t
            caption = _t(
                "invoice.caption", lang,
                order_id=order_id,
                total=f"${total:.2f}",
                date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
        except Exception:
            caption = f"🧾 Invoice for Order #{order_id} — ${total:.2f}"

        # Building the PDF is blocking (DB + ReportLab); keep it off the
        # event loop so this never stalls other bot updates.
        path = await asyncio.to_thread(generate_invoice_pdf, order_id)

        own_bot = bot is None
        if own_bot:
            from telegram import Bot
            bot = Bot(token=settings.BOT_TOKEN)

        with open(path, "rb") as fh:
            if own_bot:
                async with bot:
                    await bot.send_document(
                        chat_id=telegram_id,
                        document=fh,
                        filename=f"invoice_order_{order_id}.pdf",
                        caption=caption,
                        parse_mode="HTML",
                    )
            else:
                await bot.send_document(
                    chat_id=telegram_id,
                    document=fh,
                    filename=f"invoice_order_{order_id}.pdf",
                    caption=caption,
                    parse_mode="HTML",
                )
        return True
    except Exception:
        logger.exception("Failed to generate/send invoice for order %s", order_id)
        return False
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
