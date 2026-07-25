"""Admin Multi-Language System — V21.

Admin can enable/disable languages, set the default, edit translations,
view missing-translation reports, and import/export locale JSON files.

Callback namespace: ``alng:*``
"""
from __future__ import annotations

import json
import io
import logging
import os
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters, CommandHandler,
)
from telegram.error import BadRequest

from database import get_db_session
from database.models import LanguageConfig
from utils.bot_config import cfg
from utils.permissions import has_permission
from utils.audit import log_admin_action
from config.settings import settings

logger = logging.getLogger(__name__)

# ── i18n info ──────────────────────────────────────────────────────────────
try:
    from i18n import SUPPORTED_LANGUAGES, LANGUAGE_NAMES, LANGUAGE_FLAGS, _LOCALES_DIR
except ImportError:
    SUPPORTED_LANGUAGES = ("en", "bn", "ar", "ru", "vi", "zh", "fr", "de", "id")
    LANGUAGE_NAMES = {"en": "English", "bn": "বাংলা", "ar": "العربية",
                      "ru": "Русский", "vi": "Tiếng Việt", "zh": "中文",
                      "fr": "Français", "de": "Deutsch", "id": "Bahasa Indonesia"}
    LANGUAGE_FLAGS = {"en": "🇬🇧", "bn": "🇧🇩", "ar": "🇸🇦", "ru": "🇷🇺",
                      "vi": "🇻🇳", "zh": "🇨🇳", "fr": "🇫🇷", "de": "🇩🇪", "id": "🇮🇩"}
    _LOCALES_DIR = os.path.join(os.path.dirname(__file__), "..", "i18n", "locales")

# Conversation states
(ALNG_EDIT_KEY, ALNG_EDIT_VALUE, ALNG_IMPORT_FILE) = range(3)


def _is_admin(uid: int) -> bool:
    return uid == settings.ADMIN_TELEGRAM_ID or has_permission(uid, "manage_settings")


def _enabled() -> bool:
    return cfg.get_bool("feature_multilang_enabled", True)


async def _safe_edit(query, text: str, kb=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _back_kb(data="alng:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=data)]])


# ── Main menu ─────────────────────────────────────────────────────────────

async def alng_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    if not _enabled():
        await _safe_edit(query, "🌍 <b>Language System</b>\n\n❌ Feature disabled.", _back_kb("acc:root"))
        return

    default_lang = cfg.get_str("default_language", "en")
    enabled_langs = _get_enabled_langs()

    lines = [
        "🌍 <b>Multi-Language System</b>\n",
        f"<b>Default Language:</b> {LANGUAGE_FLAGS.get(default_lang, '')} {LANGUAGE_NAMES.get(default_lang, default_lang)}\n",
        "<b>Languages:</b>",
    ]
    kb = []
    for lang in SUPPORTED_LANGUAGES:
        is_on = lang in enabled_langs
        flag = LANGUAGE_FLAGS.get(lang, "🏳")
        name = LANGUAGE_NAMES.get(lang, lang)
        icon = "✅" if is_on else "❌"
        default_mark = " (Default)" if lang == default_lang else ""
        lines.append(f"{icon} {flag} {name}{default_mark}")
        kb.append([
            InlineKeyboardButton(f"{icon} {flag} {name}", callback_data=f"alng:lang:{lang}"),
        ])

    kb.append([InlineKeyboardButton("📥 Import Translation", callback_data="alng:import")])
    kb.append([InlineKeyboardButton("📉 Missing Translations", callback_data="alng:missing")])
    kb.append([InlineKeyboardButton("📊 Stats", callback_data="alng:stats")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


def _get_enabled_langs() -> set:
    try:
        with get_db_session() as s:
            rows = s.query(LanguageConfig).filter(LanguageConfig.is_enabled == True).all()  # noqa: E712
            return {r.code for r in rows}
    except Exception:
        # Fallback to all supported
        return set(SUPPORTED_LANGUAGES)


# ── Language detail view ──────────────────────────────────────────────────

async def alng_lang_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    _override = context.user_data.pop("_cb_data_override", None)
    if _override:
        lang = _override
    else:
        parts = query.data.split(":")
        lang = parts[2] if len(parts) > 2 else "en"
    if lang not in SUPPORTED_LANGUAGES:
        return await alng_menu(update, context)

    flag = LANGUAGE_FLAGS.get(lang, "🏳")
    name = LANGUAGE_NAMES.get(lang, lang)
    enabled_langs = _get_enabled_langs()
    is_enabled = lang in enabled_langs
    default_lang = cfg.get_str("default_language", "en")
    is_default = lang == default_lang

    # Count translations
    total_keys, missing_keys = _count_translations(lang)

    text = (
        f"🌍 <b>{flag} {name}</b>\n\n"
        f"<b>Status:</b> {'✅ Enabled' if is_enabled else '❌ Disabled'}\n"
        f"<b>Default:</b> {'✅ Yes' if is_default else '❌ No'}\n\n"
        f"<b>Translation Coverage:</b>\n"
        f"  Total keys: {total_keys}\n"
        f"  Missing:    {missing_keys}\n"
        f"  Coverage:   {((total_keys - missing_keys) / max(total_keys, 1) * 100):.0f}%\n"
    )
    kb = []
    if not is_default:
        if is_enabled:
            kb.append([InlineKeyboardButton("❌ Disable", callback_data=f"alng:disable:{lang}")])
        else:
            kb.append([InlineKeyboardButton("✅ Enable", callback_data=f"alng:enable:{lang}")])
    if not is_default:
        kb.append([InlineKeyboardButton("⭐ Set as Default", callback_data=f"alng:setdefault:{lang}")])
    kb.append([InlineKeyboardButton("📥 Export locale JSON", callback_data=f"alng:export:{lang}")])
    kb.append([InlineKeyboardButton("📉 View Missing Keys", callback_data=f"alng:missing:{lang}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="alng:menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb))


def _count_translations(lang: str) -> tuple[int, int]:
    """Return (total_keys, missing_in_lang) by comparing to English locale."""
    en_data = _load_locale_flat("en")
    lang_data = _load_locale_flat(lang)
    total = len(en_data)
    missing = sum(1 for k in en_data if k not in lang_data or not lang_data[k])
    return total, missing


def _load_locale_flat(lang: str) -> dict:
    """Load locale JSON and flatten to dot-notation keys."""
    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return _flatten(data)
    except Exception:
        return {}


def _flatten(obj: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


# ── Enable / Disable / Set default ────────────────────────────────────────

async def alng_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    lang = query.data.split(":")[2]
    _set_lang_enabled(lang, True)
    log_admin_action(update.effective_user.id, "language.enable", "language", lang,
                     module="multilang")
    await query.answer(f"✅ {LANGUAGE_NAMES.get(lang, lang)} enabled.")
    context.user_data["_cb_data_override"] = lang
    await alng_lang_view(update, context)


async def alng_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    lang = query.data.split(":")[2]
    default_lang = cfg.get_str("default_language", "en")
    if lang == default_lang:
        await query.answer("❌ Cannot disable the default language.", show_alert=True)
        return
    _set_lang_enabled(lang, False)
    log_admin_action(update.effective_user.id, "language.disable", "language", lang,
                     module="multilang")
    await query.answer(f"❌ {LANGUAGE_NAMES.get(lang, lang)} disabled.")
    context.user_data["_cb_data_override"] = lang
    await alng_lang_view(update, context)


async def alng_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    lang = query.data.split(":")[2]
    old_default = cfg.get_str("default_language", "en")
    cfg.set("default_language", lang)
    _set_lang_enabled(lang, True)  # ensure it's enabled
    log_admin_action(update.effective_user.id, "language.set_default", "language", lang,
                     old_value=old_default, new_value=lang, module="multilang")
    await query.answer(f"⭐ Default set to {LANGUAGE_NAMES.get(lang, lang)}.")
    context.user_data["_cb_data_override"] = lang
    await alng_lang_view(update, context)


def _set_lang_enabled(lang: str, enabled: bool):
    try:
        with get_db_session() as s:
            row = s.query(LanguageConfig).filter(LanguageConfig.code == lang).first()
            if row:
                row.is_enabled = enabled
            else:
                row = LanguageConfig(code=lang, is_enabled=enabled, is_default=False)
                s.add(row)
            s.commit()
    except Exception:
        logger.exception("_set_lang_enabled failed for %s", lang)


# ── Export locale JSON ─────────────────────────────────────────────────────

async def alng_export_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Exporting…")
    if not _is_admin(update.effective_user.id):
        return
    lang = query.data.split(":")[2]
    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    if not os.path.exists(path):
        await query.answer(f"❌ No locale file for {lang}.", show_alert=True)
        return
    fname = f"locale_{lang}_{datetime.utcnow().strftime('%Y%m%d')}.json"
    try:
        with open(path, "rb") as f:
            await query.message.reply_document(
                InputFile(f, filename=fname),
                caption=f"📥 Locale export: <b>{LANGUAGE_NAMES.get(lang, lang)}</b>",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning("alng_export_lang failed: %s", e)


# ── Missing translation report ────────────────────────────────────────────

async def alng_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return
    parts = query.data.split(":")
    lang = parts[2] if len(parts) > 2 else None

    en_data = _load_locale_flat("en")
    lines = []

    if lang:
        lang_data = _load_locale_flat(lang)
        missing = [k for k in en_data if k not in lang_data or not lang_data[k]]
        flag = LANGUAGE_FLAGS.get(lang, "🏳")
        name = LANGUAGE_NAMES.get(lang, lang)
        lines = [f"📉 <b>Missing Keys: {flag} {name}</b>  ({len(missing)} missing)\n"]
        for k in missing[:50]:
            lines.append(f"• <code>{k}</code>")
        if len(missing) > 50:
            lines.append(f"…and {len(missing) - 50} more")
    else:
        lines = ["📉 <b>Missing Translation Report (all languages)</b>\n"]
        for lcode in SUPPORTED_LANGUAGES:
            lang_data = _load_locale_flat(lcode)
            total = len(en_data)
            miss = sum(1 for k in en_data if k not in lang_data or not lang_data[k])
            pct = int((total - miss) / max(total, 1) * 100)
            flag = LANGUAGE_FLAGS.get(lcode, "🏳")
            name = LANGUAGE_NAMES.get(lcode, lcode)
            lines.append(f"{flag} {name}: {pct}% ({miss} missing)")

    kb = [
        [InlineKeyboardButton("🔙 Back", callback_data="alng:menu")],
    ]
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


# ── Stats ─────────────────────────────────────────────────────────────────

async def alng_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return

    with get_db_session() as s:
        try:
            from database import User as _User
            lang_rows = (s.query(
                _User.language,
                __import__('sqlalchemy').func.count(_User.id).label("cnt")
            ).group_by(_User.language).order_by(
                __import__('sqlalchemy').func.count(_User.id).desc()
            ).all())
        except Exception:
            lang_rows = []

    total_users = sum(cnt for _, cnt in lang_rows)
    lines = [f"📊 <b>Language Usage Stats</b>\n{'─' * 28}"]
    for lang, cnt in lang_rows:
        flag = LANGUAGE_FLAGS.get(lang or "en", "🏳")
        name = LANGUAGE_NAMES.get(lang or "en", lang or "en")
        pct = f"{cnt / total_users * 100:.0f}%" if total_users else "0%"
        lines.append(f"{flag} {name}: <b>{cnt:,}</b> ({pct})")
    if not lang_rows:
        lines.append("No user language data.")

    await _safe_edit(query, "\n".join(lines), _back_kb("alng:menu"))


# ── Import locale JSON (conversation) ────────────────────────────────────

async def alng_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await _safe_edit(query,
        "📥 <b>Import Translation File</b>\n\n"
        "Send a JSON locale file (same format as <code>i18n/locales/en.json</code>).\n"
        "The filename must be <code>XX.json</code> (language code).\n\n"
        "Supported: en, bn, ar, ru, vi, zh, fr, de, id",
        _back_kb(), parse_mode="HTML")
    return ALNG_IMPORT_FILE


async def alng_import_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ Please send a .json file.")
        return ALNG_IMPORT_FILE
    lang = doc.file_name.replace(".json", "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        await update.message.reply_text(f"❌ Unsupported language code: {lang}")
        return ALNG_IMPORT_FILE
    try:
        file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        data = json.load(buf)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse JSON: {e}")
        return ALNG_IMPORT_FILE

    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    try:
        os.makedirs(_LOCALES_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save: {e}")
        return ConversationHandler.END

    log_admin_action(update.effective_user.id, "language.import", "language", lang,
                     module="multilang")
    await update.message.reply_text(
        f"✅ <b>Locale imported!</b>\n"
        f"{LANGUAGE_FLAGS.get(lang, '🏳')} {LANGUAGE_NAMES.get(lang, lang)} updated with {len(_flatten(data))} keys.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def alng_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Cancelled.", _back_kb("alng:menu"))
    return ConversationHandler.END


def build_alng_import_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(alng_import_start, pattern=r"^alng:import$")],
        states={
            ALNG_IMPORT_FILE: [
                MessageHandler(filters.Document.ALL, alng_import_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(alng_cancel, pattern=r"^alng:menu$"),
            CommandHandler("cancel", alng_cancel),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ── Dispatcher ────────────────────────────────────────────────────────────

async def alng_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    dispatch_map = {
        "menu": alng_menu,
        "lang": alng_lang_view,
        "enable": alng_enable,
        "disable": alng_disable,
        "setdefault": alng_set_default,
        "export": alng_export_lang,
        "missing": alng_missing,
        "stats": alng_stats,
    }
    fn = dispatch_map.get(action, alng_menu)
    await fn(update, context)
