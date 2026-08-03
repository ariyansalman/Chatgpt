"""Settings menu (⚙ Settings on the Main Menu).

Callback namespace: ``uset:*`` (new -- does not collide with any existing
namespace in this project).

    uset:menu                  — Settings root (Language / Notifications /
                                  Currency / Privacy / Terms / About / Back)
    uset:notif                 — Notifications sub-menu
    uset:notif:tgl:<promo|order>
    uset:currency               — Currency sub-menu (reuses the existing
                                  USD/BDT toggle from utils.currency)
    uset:privacy                — Privacy sub-menu
    uset:privacy:tgl:<balance|referral>
    uset:terms                  — Terms of Service & Refund Policy
    uset:about                  — Bot Version / Developer / Uptime / Support

This module does not change any existing business logic, database schema,
callback_data, or handler -- it only adds a new navigational layer that the
Main Menu's ⚙ Settings button opens into. The existing 🌐 Language button
(``language_menu`` callback, handlers/user_handlers.py) and the existing
USD/BDT currency toggle (``utils.currency.toggle_user_currency``) are reused
as-is from inside this menu.
"""
from __future__ import annotations

from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database import get_db_session
from database.models import Settings
from i18n import t, get_user_language
from utils.bot_config import cfg
from utils.currency import get_user_currency
from utils.helpers import check_user_banned
from utils.safe_edit import safe_edit_message_text
from utils.user_prefs import (
    get_notif_pref, toggle_notif_pref,
    get_hide_balance, toggle_hide_balance,
    get_hide_referral, toggle_hide_referral,
)

# Recorded once, at import time (bot startup) -- used for the About screen's
# "Uptime" line. Best-effort / display-only, never used for any business logic.
_PROCESS_STARTED_AT = datetime.utcnow()


def _on(flag: bool) -> str:
    return "✅ On" if flag else "⛔ Off"


def _back_row(callback: str, label: str = "⬅ Back") -> list:
    return [InlineKeyboardButton(label, callback_data=callback)]


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """⚙ Settings — root menu."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)
    if check_user_banned(tid):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(t("language.menu_button", lang), callback_data="language_menu")],
        [InlineKeyboardButton(t("settings.notifications", lang), callback_data="uset:notif")],
        [InlineKeyboardButton(t("settings.currency", lang), callback_data="uset:currency")],
        [InlineKeyboardButton(t("settings.privacy", lang), callback_data="uset:privacy")],
        [InlineKeyboardButton(t("settings.terms", lang), callback_data="uset:terms")],
        [InlineKeyboardButton(t("settings.about", lang), callback_data="uset:about")],
        _back_row("main_menu", t("settings.back", lang)),
    ])
    await safe_edit_message_text(
        query,
        f"{t('settings.title', lang)}\n\n{t('settings.subtitle', lang)}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def notifications_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)

    promo = get_notif_pref(tid, "promo")
    order = get_notif_pref(tid, "order")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 {t('settings.promo_notifications', lang)}: {_on(promo)}",
                               callback_data="uset:notif:tgl:promo")],
        [InlineKeyboardButton(f"📦 {t('settings.order_notifications', lang)}: {_on(order)}",
                               callback_data="uset:notif:tgl:order")],
        _back_row("uset:menu", t("settings.back", lang)),
    ])
    await safe_edit_message_text(
        query,
        f"{t('settings.notifications', lang)}\n\n{t('settings.notifications_hint', lang)}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def notifications_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    kind = query.data.rsplit(":", 1)[-1]  # 'promo' | 'order'
    new_val = toggle_notif_pref(tid, kind)
    lang = get_user_language(tid)
    await query.answer(t("settings.saved", lang))
    await notifications_menu_callback(update, context)


async def currency_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)
    current = get_user_currency(tid)
    other = "BDT" if current == "USD" else "USD"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💱 {t('settings.switch_to', lang)} {other}", callback_data="currency_toggle")],
        _back_row("uset:menu", t("settings.back", lang)),
    ])
    await safe_edit_message_text(
        query,
        f"{t('settings.currency', lang)}\n\n{t('settings.currency_current', lang, currency=current)}\n"
        f"{t('settings.currency_hint', lang)}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def privacy_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)

    hide_balance = get_hide_balance(tid)
    hide_referral = get_hide_referral(tid)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👛 {t('settings.hide_balance', lang)}: {_on(hide_balance)}",
                               callback_data="uset:privacy:tgl:balance")],
        [InlineKeyboardButton(f"👥 {t('settings.hide_referral', lang)}: {_on(hide_referral)}",
                               callback_data="uset:privacy:tgl:referral")],
        _back_row("uset:menu", t("settings.back", lang)),
    ])
    await safe_edit_message_text(
        query,
        f"{t('settings.privacy', lang)}\n\n{t('settings.privacy_hint', lang)}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def privacy_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    which = query.data.rsplit(":", 1)[-1]  # 'balance' | 'referral'
    lang = get_user_language(tid)
    if which == "balance":
        toggle_hide_balance(tid)
    else:
        toggle_hide_referral(tid)
    await query.answer(t("settings.saved", lang))
    await privacy_menu_callback(update, context)


async def terms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)

    body = cfg.get_str("terms_of_service_text", "").strip() or t("settings.terms_default", lang)

    keyboard = InlineKeyboardMarkup([_back_row("uset:menu", t("settings.back", lang))])
    await safe_edit_message_text(
        query,
        f"{t('settings.terms', lang)}\n\n{body}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def about_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    lang = get_user_language(tid)

    version = cfg.get_str("about_bot_version", "1.0.0")
    developer = cfg.get_str("about_developer", "Store Team")

    with get_db_session() as session:
        s = session.query(Settings).first()
        support_username = (s.support_username or "").strip().lstrip("@") if s else ""
    support_contact = f"@{support_username}" if support_username else t("settings.about_no_contact", lang)

    delta = datetime.utcnow() - _PROCESS_STARTED_AT
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        uptime = f"{days}d {hours}h {minutes}m"
    elif hours:
        uptime = f"{hours}h {minutes}m"
    else:
        uptime = f"{minutes}m"

    text = (
        f"{t('settings.about', lang)}\n\n"
        f"🏷 {t('settings.about_version', lang)}: {version}\n"
        f"👨‍💻 {t('settings.about_developer', lang)}: {developer}\n"
        f"⏱ {t('settings.about_uptime', lang)}: {uptime}\n"
        f"📞 {t('settings.about_support', lang)}: {support_contact}"
    )
    keyboard = InlineKeyboardMarkup([_back_row("uset:menu", t("settings.back", lang))])
    await safe_edit_message_text(query, text, reply_markup=keyboard, parse_mode="HTML")


def register_handlers(app):
    app.add_handler(CallbackQueryHandler(settings_menu_callback, pattern=r"^uset:menu$"))
    app.add_handler(CallbackQueryHandler(notifications_menu_callback, pattern=r"^uset:notif$"))
    app.add_handler(CallbackQueryHandler(notifications_toggle_callback, pattern=r"^uset:notif:tgl:(promo|order)$"))
    app.add_handler(CallbackQueryHandler(currency_menu_callback, pattern=r"^uset:currency$"))
    app.add_handler(CallbackQueryHandler(privacy_menu_callback, pattern=r"^uset:privacy$"))
    app.add_handler(CallbackQueryHandler(privacy_toggle_callback, pattern=r"^uset:privacy:tgl:(balance|referral)$"))
    app.add_handler(CallbackQueryHandler(terms_callback, pattern=r"^uset:terms$"))
    app.add_handler(CallbackQueryHandler(about_callback, pattern=r"^uset:about$"))
