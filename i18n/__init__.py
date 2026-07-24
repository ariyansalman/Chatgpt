"""Full i18n (internationalisation) support for the bot.

Design
------
* Translated strings live in JSON files under ``i18n/locales/<lang>.json``,
  organised as nested namespaces (e.g. ``wallet.title``).
* ``t(key, lang, **kwargs)`` looks up ``key`` (dot-path) in the requested
  language file and falls back to English, then to the raw key itself, so a
  missing translation never crashes a handler — it just shows English (or
  the key) instead of a blank message.
* Per-user language preference is stored on ``User.language`` (already
  present on the model / DB schema since the v2 migration). This module
  owns reading/writing that column, mirroring the existing
  ``utils/currency.py`` pattern for ``preferred_currency``.
* New users default to Bengali or English based on their Telegram client's
  ``language_code`` (``resolve_initial_language``) if it matches one of
  ``SUPPORTED_LANGUAGES`` (en, bn, vi, ru, zh, fr, de, ar, id), else English.
  They can switch anytime via the "🌐 Language" menu (see
  ``handlers/user_handlers.py``).

Usage
-----
    from i18n import t, get_user_language

    lang = get_user_language(telegram_id)
    await update.message.reply_text(t("wallet.title", lang))
    await update.message.reply_text(t("wallet.balance", lang, amount=formatted))
"""

from __future__ import annotations

import json
import os
import threading

SUPPORTED_LANGUAGES = ("en", "bn", "vi", "ru", "zh", "fr", "de", "ar", "id")
DEFAULT_LANGUAGE = "en"

# Languages that read right-to-left. Telegram clients handle RTL rendering
# automatically for these language codes; this set exists purely so other
# code (if it ever needs to know) doesn't have to hardcode Arabic.
RTL_LANGUAGES = ("ar",)

LANGUAGE_NAMES = {
    "en": "English",
    "bn": "বাংলা",
    "vi": "Tiếng Việt",
    "ru": "Русский",
    "zh": "中文",
    "fr": "Français",
    "de": "Deutsch",
    "ar": "العربية",
    "id": "Bahasa Indonesia",
}
LANGUAGE_FLAGS = {
    "en": "🇬🇧",
    "bn": "🇧🇩",
    "vi": "🇻🇳",
    "ru": "🇷🇺",
    "zh": "🇨🇳",
    "fr": "🇫🇷",
    "de": "🇩🇪",
    "ar": "🇸🇦",
    "id": "🇮🇩",
}

_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "locales")
_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _load_locale(lang: str) -> dict:
    """Load (and cache) a locale JSON file. Returns {} if missing/invalid."""
    if lang in _cache:
        return _cache[lang]
    with _lock:
        if lang in _cache:  # re-check inside the lock
            return _cache[lang]
        path = os.path.join(_LOCALES_DIR, f"{lang}.json")
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[i18n] failed to load locale '{lang}' ({path}): {e}")
        _cache[lang] = data
        return data


def reload_locales():
    """Clear the in-memory cache — call after editing locale files at runtime."""
    with _lock:
        _cache.clear()


def _lookup(data: dict, dotted_key: str):
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def normalize_lang(lang: str | None) -> str:
    """Coerce any input into one of SUPPORTED_LANGUAGES, defaulting to English."""
    if not lang:
        return DEFAULT_LANGUAGE
    lang = str(lang).strip().lower()[:2]
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate `key` (dot-path, e.g. 'wallet.title') into `lang`.

    Falls back to English if the key is missing in `lang`, and to the raw
    key string itself if it's missing from English too (so a typo/missing
    translation never raises — it just becomes visibly obvious in the UI).
    Any `**kwargs` are applied with `str.format(**kwargs)`; formatting
    errors (e.g. a missing placeholder) fail safe and return the
    unformatted string rather than raising.
    """
    lang = normalize_lang(lang)
    value = _lookup(_load_locale(lang), key)
    if value is None and lang != DEFAULT_LANGUAGE:
        value = _lookup(_load_locale(DEFAULT_LANGUAGE), key)
    if value is None:
        value = key

    if kwargs:
        try:
            return value.format(**kwargs)
        except Exception:
            return value
    return value


# ─── Per-user language preference (mirrors utils/currency.py) ──────────────

def get_user_language(telegram_id: int) -> str:
    """Return the user's saved language ('en' or 'bn'). Defaults to 'en'."""
    try:
        from database import get_db_session
        from database.models import User
        with get_db_session() as session:
            u = session.query(User).filter_by(telegram_id=telegram_id).first()
            if u:
                return normalize_lang(u.language)
    except Exception as e:
        print(f"[i18n] get_user_language failed: {e}")
    return DEFAULT_LANGUAGE


def set_user_language(telegram_id: int, lang: str) -> str:
    """Persist the user's language preference. Returns the normalized value stored."""
    lang = normalize_lang(lang)
    try:
        from database import get_db_session
        from database.models import User
        with get_db_session() as session:
            u = session.query(User).filter_by(telegram_id=telegram_id).first()
            if u:
                u.language = lang
                session.commit()
    except Exception as e:
        print(f"[i18n] set_user_language failed: {e}")
    return lang


def resolve_initial_language(telegram_user) -> str:
    """Best-effort default language for a brand-new user based on their
    Telegram client locale (``telegram_user.language_code``, e.g. 'bn', 'bn-BD').
    Falls back to English for anything we don't explicitly support yet.
    """
    code = getattr(telegram_user, "language_code", None)
    return normalize_lang(code)


__all__ = [
    "t",
    "get_user_language",
    "set_user_language",
    "resolve_initial_language",
    "normalize_lang",
    "reload_locales",
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "LANGUAGE_NAMES",
    "LANGUAGE_FLAGS",
    "RTL_LANGUAGES",
]
