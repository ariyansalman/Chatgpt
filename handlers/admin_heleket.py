"""Admin configuration for Heleket Static Wallet payments."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import get_db_session
from database.models import PaymentGatewayConfig
from utils.permissions import has_permission
from telegram.error import BadRequest

HELEKET_EDIT_MERCHANT_ID, HELEKET_EDIT_API_KEY = range(740, 742)

def _row(session):
    r=session.query(PaymentGatewayConfig).filter_by(gateway="heleket").first()
    if not r:
        r=PaymentGatewayConfig(gateway="heleket", is_enabled=False); session.add(r); session.commit(); session.refresh(r)
    return r

def _mask(v):
    if not v: return "(not set)"
    return "•"*len(v) if len(v)<=6 else f"{v[:3]}…{v[-3:]} ({len(v)} chars)"

def _cfg():
    with get_db_session() as s:
        r=_row(s); return {"enabled":bool(r.is_enabled),"merchant_id":r.merchant_uuid or "","api_key":r.api_key or ""}

def _kb(c):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🆔 Merchant ID",callback_data="admin_heleket_edit_merchant")],
        [InlineKeyboardButton("🔑 Payment API Key",callback_data="admin_heleket_edit_key")],
        [InlineKeyboardButton("🚫 Disable" if c["enabled"] else "✅ Enable",callback_data="admin_heleket_toggle")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin_settings")]])

def _text(c):
    return ("🟣 <b>Heleket Static Wallet</b>\n\n"+f"Status: {'✅ Enabled' if c['enabled'] else '🚫 Disabled'}\n"+
        f"Merchant ID: <code>{_mask(c['merchant_id'])}</code>\nPayment API Key: <code>{_mask(c['api_key'])}</code>\n\n"+
        "Reusable crypto deposit addresses with automatic webhook balance credit.\n\n⚠️ Merchant ID, Payment API Key and WEBHOOK_URL are required.")

async def admin_heleket_view(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not has_permission(update.effective_user.id,"manage_payments"): return
    try:
        c=_cfg(); await q.edit_message_text(_text(c),reply_markup=_kb(c),parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def admin_heleket_toggle(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not has_permission(update.effective_user.id,"manage_payments"): return
    c=_cfg()
    if not c["enabled"] and not(c["merchant_id"] and c["api_key"]):
        await q.answer("Set Merchant ID and Payment API Key first.",show_alert=True); return
    with get_db_session() as s:
        r=_row(s); r.is_enabled=not r.is_enabled
    await admin_heleket_view(update,context)

async def edit_merchant_start(update,context):
    try:
        q=update.callback_query; await q.answer(); await q.edit_message_text("Send Heleket <b>Merchant ID</b>.",parse_mode="HTML"); return HELEKET_EDIT_MERCHANT_ID
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
async def edit_key_start(update,context):
    try:
        q=update.callback_query; await q.answer(); await q.edit_message_text("Send Heleket <b>Payment API Key</b>. It will be masked after saving.",parse_mode="HTML"); return HELEKET_EDIT_API_KEY
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
async def edit_merchant_value(update,context):
    v=(update.message.text or "").strip()
    if not v: return HELEKET_EDIT_MERCHANT_ID
    with get_db_session() as s: _row(s).merchant_uuid=v[:120]
    c=_cfg(); await update.message.reply_text(_text(c),reply_markup=_kb(c),parse_mode="HTML"); return ConversationHandler.END
async def edit_key_value(update,context):
    v=(update.message.text or "").strip()
    if not v: return HELEKET_EDIT_API_KEY
    with get_db_session() as s: _row(s).api_key=v[:255]
    c=_cfg(); await update.message.reply_text(_text(c),reply_markup=_kb(c),parse_mode="HTML"); return ConversationHandler.END

def build_heleket_edit_conv():
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters
    return ConversationHandler(entry_points=[CallbackQueryHandler(edit_merchant_start,pattern="^admin_heleket_edit_merchant$"),CallbackQueryHandler(edit_key_start,pattern="^admin_heleket_edit_key$")],
        states={HELEKET_EDIT_MERCHANT_ID:[MessageHandler(filters.TEXT & ~filters.COMMAND,edit_merchant_value)],HELEKET_EDIT_API_KEY:[MessageHandler(filters.TEXT & ~filters.COMMAND,edit_key_value)]},fallbacks=[],allow_reentry=True)
