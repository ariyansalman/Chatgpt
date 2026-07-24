"""Admin Gift Purchase Settings panel.

Callback namespace: agp:*

Provides admin control over the Gift Purchase feature:
  • Enable / Disable feature
  • Allow / Disallow anonymous gifts
  • View gift purchase history and statistics
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import get_db_session, User, Product, Order
from database.models import GiftPurchase, GiftPurchaseStatus
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)


def _kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="acc:gifts:menu")]])


# ─────────────────────────────────────────────────────────────────────────────
# Stats helper
# ─────────────────────────────────────────────────────────────────────────────

def get_gift_purchase_stats() -> dict:
    stats = {}
    try:
        with get_db_session() as s:
            stats["total"] = s.query(GiftPurchase).count()
            stats["pending"] = s.query(GiftPurchase).filter_by(
                status=GiftPurchaseStatus.PENDING).count()
            stats["notified"] = s.query(GiftPurchase).filter_by(
                status=GiftPurchaseStatus.NOTIFIED).count()
            stats["undeliverable"] = s.query(GiftPurchase).filter_by(
                status=GiftPurchaseStatus.UNDELIVERABLE).count()
            cutoff = datetime.utcnow() - timedelta(days=30)
            stats["last_30d"] = s.query(GiftPurchase).filter(
                GiftPurchase.created_at >= cutoff).count()
    except Exception:
        logger.exception("get_gift_purchase_stats failed")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Main panel
# ─────────────────────────────────────────────────────────────────────────────

async def gift_purchase_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main Gift Purchase admin panel — callback: agp:menu or acc:gifts:gp"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    enabled     = cfg.get_bool("feature_gift_purchase_enabled", True)
    allow_anon  = cfg.get_bool("feature_gift_allow_anonymous", True)
    stats       = get_gift_purchase_stats()

    status_str  = "✅ Enabled" if enabled else "❌ Disabled"
    anon_str    = "✅ ON" if allow_anon else "🚫 OFF"

    text = (
        "🎁 <b>Gift Purchase Settings</b>\n\n"
        f"Status: {status_str}\n"
        f"Anonymous Gifts: {anon_str}\n\n"
        "<b>Statistics (all-time):</b>\n"
        f"  • Total gifts: <b>{stats.get('total', 0)}</b>\n"
        f"  • Delivered: <b>{stats.get('notified', 0)}</b>\n"
        f"  • Pending: <b>{stats.get('pending', 0)}</b>\n"
        f"  • Undeliverable: <b>{stats.get('undeliverable', 0)}</b>\n"
        f"  • Last 30 days: <b>{stats.get('last_30d', 0)}</b>"
    )

    toggle_label = "❌ Disable Feature" if enabled else "✅ Enable Feature"
    anon_toggle  = "🚫 Disallow Anonymous" if allow_anon else "✅ Allow Anonymous"

    kb = [
        [InlineKeyboardButton(toggle_label, callback_data="agp:toggle")],
        [InlineKeyboardButton(anon_toggle,  callback_data="agp:toggle_anon")],
        [InlineKeyboardButton("📋 Recent Gifts", callback_data="agp:list")],
        [InlineKeyboardButton("🔙 Back", callback_data="acc:gifts:menu")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def gift_purchase_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Gift Purchase feature on/off: agp:toggle"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    new_val = not cfg.get_bool("feature_gift_purchase_enabled", True)
    cfg.set("feature_gift_purchase_enabled", new_val)
    log_admin_action(update.effective_user.id, "gift_purchase.toggle",
                     details=f"enabled={new_val}")
    await gift_purchase_menu(update, context)


async def gift_purchase_toggle_anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle anonymous gift setting: agp:toggle_anon"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    new_val = not cfg.get_bool("feature_gift_allow_anonymous", True)
    cfg.set("feature_gift_allow_anonymous", new_val)
    log_admin_action(update.effective_user.id, "gift_purchase.toggle_anon",
                     details=f"anonymous={new_val}")
    await gift_purchase_menu(update, context)


async def gift_purchase_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent gift purchases: agp:list"""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True)
        return

    with get_db_session() as s:
        gifts = (
            s.query(GiftPurchase)
            .order_by(GiftPurchase.created_at.desc())
            .limit(15)
            .all()
        )
        rows = []
        for gp in gifts:
            sender = s.query(User).filter_by(id=gp.sender_user_id).first()
            product = s.query(Product).filter_by(id=gp.product_id).first()
            rows.append({
                "id":        gp.id,
                "order_id":  gp.order_id,
                "status":    gp.status.value if gp.status else "?",
                "sender":    sender.username or str(sender.telegram_id) if sender else "?",
                "product":   product.name[:20] if product else "?",
                "recipient": gp.recipient_username or str(gp.recipient_telegram_id or "?"),
                "anon":      gp.is_anonymous,
                "when":      gp.created_at.strftime("%m-%d %H:%M") if gp.created_at else "?",
            })

    if not rows:
        text = "🎁 <b>Recent Gift Purchases</b>\n\nNo gift purchases yet."
    else:
        lines = ["🎁 <b>Recent Gift Purchases</b>\n"]
        for r in rows:
            anon_tag = " 🎭" if r["anon"] else ""
            lines.append(
                f"#{r['id']} — {r['product']}\n"
                f"  From: @{r['sender']}{anon_tag} → To: {r['recipient']}\n"
                f"  Status: {r['status']} | {r['when']}"
            )
        text = "\n\n".join(lines)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="agp:menu")]])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Route dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def route(action: str, rest: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route acc:gifts:gp:<action> calls."""
    if action == "menu":
        await gift_purchase_menu(update, context)
    elif action == "toggle":
        await gift_purchase_toggle(update, context)
    elif action == "toggle_anon":
        await gift_purchase_toggle_anon(update, context)
    elif action == "list":
        await gift_purchase_list(update, context)
    else:
        await gift_purchase_menu(update, context)
