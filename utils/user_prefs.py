"""Per-user preference flags for the Settings menu (notifications, privacy).

Follows the same pattern already used by ``handlers/admin_notification_settings.py``:
values live as rows in the existing generic ``bot_config`` key/value table
(``utils.bot_config.cfg``), keyed per Telegram user id. No new tables, no
schema changes, no changes to any existing business logic.

These flags are purely presentational (what the user sees / gets notified
about) -- they never gate payments, delivery, or any other business logic.
"""
from __future__ import annotations

from utils.bot_config import cfg

# Defaults mirror "everything on, nothing hidden" so existing users see no
# behavior change until they actively open Settings and flip something.
_DEFAULTS = {
    "promo": True,
    "order": True,
}


def get_notif_pref(telegram_id: int, kind: str) -> bool:
    """kind: 'promo' or 'order'."""
    return cfg.get_bool(f"uset_notif_{kind}_{telegram_id}", _DEFAULTS.get(kind, True))


def toggle_notif_pref(telegram_id: int, kind: str) -> bool:
    new_val = not get_notif_pref(telegram_id, kind)
    cfg.set(f"uset_notif_{kind}_{telegram_id}", new_val)
    return new_val


def get_hide_balance(telegram_id: int) -> bool:
    return cfg.get_bool(f"uset_privacy_hide_balance_{telegram_id}", False)


def toggle_hide_balance(telegram_id: int) -> bool:
    new_val = not get_hide_balance(telegram_id)
    cfg.set(f"uset_privacy_hide_balance_{telegram_id}", new_val)
    return new_val


def get_hide_referral(telegram_id: int) -> bool:
    return cfg.get_bool(f"uset_privacy_hide_referral_{telegram_id}", False)


def toggle_hide_referral(telegram_id: int) -> bool:
    new_val = not get_hide_referral(telegram_id)
    cfg.set(f"uset_privacy_hide_referral_{telegram_id}", new_val)
    return new_val
