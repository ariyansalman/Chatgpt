"""Admin Gift Card Manager.

Callback namespace: agc:*

Admin can:
  • Generate gift cards (fixed amount, percent, custom value)
  • Set expiry date, max uses, single-use flag
  • View all cards and usage
  • Deactivate / delete cards

Conversation states:
    AGC_CODE     (5400) — entering card code
    AGC_TYPE     (5401) — selecting card type
    AGC_VALUE    (5402) — entering card value
    AGC_EXPIRY   (5403) — entering expiry date (YYYY-MM-DD or 'none')
    AGC_MAXUSES  (5404) — entering max uses (0=unlimited)
    AGC_LABEL    (5405) — entering card label/description
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from database import get_db_session
from database.models import GiftCard, GiftCardRedemption, GiftCardType
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

AGC_CODE     = 5400
AGC_VALUE    = 5402
AGC_EXPIRY   = 5403
AGC_MAXUSES  = 5404
AGC_LABEL    = 5405

_PER_PAGE = 10


def _feature_enabled() -> bool:
    return cfg.get_bool("feature_gift_cards_enabled", True)


def _kb_back(cb: str = "agc:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])


def _random_code(length: int = 12) -> str:
    chars = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(chars) for _ in range(4))
              for _ in range(length // 4)]
    return "-".join(groups)


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

def get_gift_card_stats() -> dict:
    stats = {}
    try:
        with get_db_session() as s:
            stats["total"]       = s.query(GiftCard).count()
            stats["active"]      = s.query(GiftCard).filter_by(is_active=True).count()
            stats["redemptions"] = s.query(GiftCardRedemption).count()
    except Exception:
        logger.exception("get_gift_card_stats failed")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Main menu
# ─────────────────────────────────────────────────────────────────────────────

async def gift_card_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main gift card admin menu: agc:menu"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    enabled = _feature_enabled()
    stats   = get_gift_card_stats()

    toggle_label = "❌ Disable Gift Cards" if enabled else "✅ Enable Gift Cards"
    text = (
        "🎟 <b>Gift Card Manager</b>\n\n"
        f"Feature: {'✅ Enabled' if enabled else '❌ Disabled'}\n\n"
        "<b>Statistics:</b>\n"
        f"  • Total cards: <b>{stats.get('total', 0)}</b>\n"
        f"  • Active: <b>{stats.get('active', 0)}</b>\n"
        f"  • Total redemptions: <b>{stats.get('redemptions', 0)}</b>"
    )
    kb = [
        [InlineKeyboardButton(toggle_label, callback_data="agc:toggle")],
        [InlineKeyboardButton("➕ Create Gift Card", callback_data="agc:create_start")],
        [InlineKeyboardButton("📋 All Gift Cards",   callback_data="agc:list:0")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:gifts:menu")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gift_card_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle gift card feature: agc:toggle"""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return
    new_val = not _feature_enabled()
    cfg.set("feature_gift_cards_enabled", new_val)
    log_admin_action(update.effective_user.id, "gift_card.toggle", details=f"enabled={new_val}")
    await gift_card_menu(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# List cards
# ─────────────────────────────────────────────────────────────────────────────

async def gift_card_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all gift cards with pagination: agc:list:<page>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        page = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    with get_db_session() as s:
        total  = s.query(GiftCard).count()
        cards  = (s.query(GiftCard)
                  .order_by(GiftCard.created_at.desc())
                  .offset(page * _PER_PAGE)
                  .limit(_PER_PAGE)
                  .all())
        rows = []
        for c in cards:
            rdm = s.query(GiftCardRedemption).filter_by(card_id=c.id).count()
            rows.append({
                "id": c.id, "code": c.code, "label": c.label or "",
                "type": c.card_type.value, "value": c.value,
                "is_active": c.is_active, "max_uses": c.max_uses,
                "used_count": c.used_count, "redemptions": rdm,
                "expires_at": c.expires_at,
            })

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    lines = [f"🎟 <b>Gift Cards</b> (page {page + 1}/{total_pages})\n"]
    for r in rows:
        active_tag = "✅" if r["is_active"] else "❌"
        expiry_str = r["expires_at"].strftime("%Y-%m-%d") if r["expires_at"] else "Never"
        if r["type"] == GiftCardType.PERCENT.value:
            val_str = f"{r['value']:.0f}%"
        else:
            val_str = f"${r['value']:.2f}"
        uses_str = f"{r['used_count']}/{r['max_uses']}" if r["max_uses"] > 0 else f"{r['used_count']}/∞"
        lines.append(
            f"{active_tag} <code>{r['code']}</code> — {val_str}\n"
            f"   {r['label']}  |  Uses: {uses_str}  |  Exp: {expiry_str}"
        )

    text = "\n\n".join(lines) if lines else "No gift cards found."

    kb_rows: list = []
    for r in rows:
        kb_rows.append([InlineKeyboardButton(
            f"{'✅' if r['is_active'] else '❌'} {r['code']} ({r['type']}, {r['value']})",
            callback_data=f"agc:view:{r['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"agc:list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"agc:list:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="agc:menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# View / manage single card
# ─────────────────────────────────────────────────────────────────────────────

async def gift_card_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View single gift card: agc:view:<card_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        card_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await gift_card_menu(update, context)
        return

    with get_db_session() as s:
        card = s.query(GiftCard).filter_by(id=card_id).first()
        if not card:
            await query.answer("❌ Card not found.", show_alert=True)
            return
        rdm_count = s.query(GiftCardRedemption).filter_by(card_id=card_id).count()
        info = {
            "id":         card.id,
            "code":       card.code,
            "label":      card.label or "—",
            "type":       card.card_type.value,
            "value":      card.value,
            "is_active":  card.is_active,
            "max_uses":   card.max_uses,
            "used_count": card.used_count,
            "is_single":  card.is_single_use,
            "expires_at": card.expires_at,
            "created_at": card.created_at,
            "redemptions": rdm_count,
        }

    expiry_str = info["expires_at"].strftime("%Y-%m-%d %H:%M") if info["expires_at"] else "No expiry"
    created_str = info["created_at"].strftime("%Y-%m-%d") if info["created_at"] else "?"
    uses_str = f"{info['used_count']}/{info['max_uses']}" if info["max_uses"] > 0 else f"{info['used_count']}/∞"
    if info["type"] == GiftCardType.PERCENT.value:
        val_str = f"{info['value']:.0f}% discount"
    else:
        val_str = f"${info['value']:.2f}"

    text = (
        f"🎟 <b>Gift Card #{info['id']}</b>\n\n"
        f"Code: <code>{info['code']}</code>\n"
        f"Label: {info['label']}\n"
        f"Type: {info['type'].upper()}\n"
        f"Value: {val_str}\n"
        f"Status: {'✅ Active' if info['is_active'] else '❌ Inactive'}\n"
        f"Single Use: {'Yes' if info['is_single'] else 'No'}\n"
        f"Uses: {uses_str}\n"
        f"Redemptions: {info['redemptions']}\n"
        f"Expires: {expiry_str}\n"
        f"Created: {created_str}"
    )

    toggle_lbl = "❌ Deactivate" if info["is_active"] else "✅ Activate"
    kb = [
        [InlineKeyboardButton(toggle_lbl, callback_data=f"agc:deactivate:{card_id}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="agc:list:0")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gift_card_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle card active state: agc:deactivate:<card_id>"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    try:
        _override = context.user_data.pop("_cb_data_override", None)
        card_id = int(_override) if _override else int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return

    with get_db_session() as s:
        card = s.query(GiftCard).filter_by(id=card_id).first()
        if card:
            card.is_active = not card.is_active
            s.commit()
            new_state = card.is_active

    log_admin_action(update.effective_user.id, "gift_card.toggle",
                     target_type="gift_card", target_id=card_id,
                     details=f"active={new_state}")
    context.user_data["_cb_data_override"] = str(card_id)
    await gift_card_view(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Create gift card conversation
# ─────────────────────────────────────────────────────────────────────────────

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start gift card creation: agc:create_start"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Fixed Amount (USD)", callback_data="agc:ctype:fixed")],
        [InlineKeyboardButton("🏷 Percentage Discount", callback_data="agc:ctype:percent")],
        [InlineKeyboardButton("🎨 Custom Value (USD)",  callback_data="agc:ctype:custom")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="agc:cancel_create")],
    ])
    try:
        await query.edit_message_text(
            "🎟 <b>Create Gift Card</b>\n\nSelect the card type:",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AGC_VALUE


async def create_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Card type selected: agc:ctype:<type>"""
    query = update.callback_query
    await query.answer()

    ctype = query.data.split(":")[-1]
    context.user_data["agc_type"] = ctype
    context.user_data["agc_code"] = _random_code()  # auto-generate code

    unit = "%" if ctype == "percent" else "USD"
    try:
        await query.edit_message_text(
            f"🎟 <b>Gift Card Value</b>\n\n"
            f"Type: <b>{ctype.upper()}</b>\n"
            f"Auto-generated code: <code>{context.user_data['agc_code']}</code>\n\n"
            f"Enter the value in <b>{unit}</b> (e.g., 10 for ${10}$):\n"
            f"Send /cancel to abort.",
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return AGC_VALUE


async def create_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive card value."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        return await create_cancel(update, context)

    try:
        value = float(text)
        if value <= 0:
            raise ValueError("non-positive")
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a positive number (e.g., 10 or 5.50).\nSend /cancel to abort."
        )
        return AGC_VALUE

    context.user_data["agc_value"] = value
    await update.message.reply_text(
        f"✅ Value set to <b>{value}</b>.\n\n"
        f"Enter expiry date as <b>YYYY-MM-DD</b> (e.g., 2027-01-01), "
        f"or type <b>none</b> for no expiry.\n"
        f"Send /cancel to abort.",
        parse_mode="HTML",
    )
    return AGC_EXPIRY


async def create_expiry_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive card expiry date."""
    text = (update.message.text or "").strip().lower()
    if text == "/cancel":
        return await create_cancel(update, context)

    if text in ("none", "0", "never", "-"):
        context.user_data["agc_expires"] = None
    else:
        try:
            expires = datetime.strptime(text, "%Y-%m-%d")
            if expires < datetime.utcnow():
                await update.message.reply_text(
                    "❌ Expiry date must be in the future. Try again or type none."
                )
                return AGC_EXPIRY
            context.user_data["agc_expires"] = expires
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid date format. Use YYYY-MM-DD (e.g., 2027-01-01) or 'none'."
            )
            return AGC_EXPIRY

    await update.message.reply_text(
        "Enter <b>max uses</b> (0 = unlimited, 1 = single use, etc.):\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return AGC_MAXUSES


async def create_maxuses_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive max uses."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        return await create_cancel(update, context)

    try:
        max_uses = int(text)
        if max_uses < 0:
            raise ValueError("negative")
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a non-negative integer (0 = unlimited). Send /cancel to abort."
        )
        return AGC_MAXUSES

    context.user_data["agc_max_uses"] = max_uses
    context.user_data["agc_single_use"] = (max_uses == 1)

    await update.message.reply_text(
        "Enter a <b>label</b> / description for this gift card "
        "(shown to users on redemption):\nSend /cancel to abort.",
        parse_mode="HTML",
    )
    return AGC_LABEL


async def create_label_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive label and save the card."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        return await create_cancel(update, context)

    label = text[:120]

    ctype_str   = context.user_data.pop("agc_type", "fixed")
    code        = context.user_data.pop("agc_code", _random_code())
    value       = context.user_data.pop("agc_value", 0.0)
    expires     = context.user_data.pop("agc_expires", None)
    max_uses    = context.user_data.pop("agc_max_uses", 0)
    single_use  = context.user_data.pop("agc_single_use", False)

    type_map = {
        "fixed":   GiftCardType.FIXED,
        "percent": GiftCardType.PERCENT,
        "custom":  GiftCardType.CUSTOM,
    }
    card_type = type_map.get(ctype_str, GiftCardType.FIXED)

    try:
        with get_db_session() as s:
            # Ensure code uniqueness
            existing = s.query(GiftCard).filter_by(code=code).first()
            if existing:
                code = _random_code()

            card = GiftCard(
                code=code,
                label=label,
                card_type=card_type,
                value=value,
                expires_at=expires,
                max_uses=max_uses,
                used_count=0,
                is_single_use=single_use,
                is_active=True,
                created_at=datetime.utcnow(),
                created_by=update.effective_user.id,
            )
            s.add(card)
            s.commit()
            card_id = card.id

        log_admin_action(update.effective_user.id, "gift_card.create",
                         target_type="gift_card", target_id=card_id,
                         details=f"code={code} type={ctype_str} value={value}")

        if card_type == GiftCardType.PERCENT:
            val_str = f"{value:.0f}%"
        else:
            val_str = f"${value:.2f}"

        await update.message.reply_text(
            f"✅ <b>Gift Card Created!</b>\n\n"
            f"Code: <code>{code}</code>\n"
            f"Label: {label}\n"
            f"Type: {ctype_str.upper()}\n"
            f"Value: {val_str}\n"
            f"Max Uses: {'Unlimited' if max_uses == 0 else max_uses}\n"
            f"Expires: {expires.strftime('%Y-%m-%d') if expires else 'Never'}",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to create gift card")
        await update.message.reply_text("❌ Failed to create gift card. Please try again.")

    return ConversationHandler.END


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel gift card creation."""
    for k in ("agc_type", "agc_code", "agc_value", "agc_expires",
              "agc_max_uses", "agc_single_use"):
        context.user_data.pop(k, None)
    msg = "🎟 Gift card creation cancelled."
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(msg)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# acc: route dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route acc:gifts:gc:<action> calls."""
    if action == "menu":
        await gift_card_menu(update, context)
    elif action == "toggle":
        await gift_card_toggle(update, context)
    elif action == "list":
        page = int(rest[0]) if rest else 0
        context.user_data["_cb_data_override"] = str(page)
        await gift_card_list(update, context)
    elif action == "view" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await gift_card_view(update, context)
    elif action == "deactivate" and rest:
        context.user_data["_cb_data_override"] = str(rest[0])
        await gift_card_deactivate(update, context)
    else:
        await gift_card_menu(update, context)
