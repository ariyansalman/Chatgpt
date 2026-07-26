"""Inline keyboard utilities for the Telegram bot (i18n-aware: en, bn, vi,
ru, zh, fr, de, ar, id — driven entirely by i18n.SUPPORTED_LANGUAGES /
LANGUAGE_NAMES / LANGUAGE_FLAGS, so no per-language branching lives here)."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from i18n import t, SUPPORTED_LANGUAGES, LANGUAGE_NAMES, LANGUAGE_FLAGS
from .helpers import is_admin
from .button_colors import (
    DEFAULT_MENU_ITEM_COLORS,
    get_button_color,
    normalize_color,
    telegram_style_for_color,
)

# ─────────────────────────────────────────────────────────────────────────────
# Styled button helper (Bot API 9.4 — colored buttons + custom emoji icons)
# ─────────────────────────────────────────────────────────────────────────────
#
# Every top-level main-menu item gets a default color so the menu reads well
# out of the box. Admins can override both the color and the custom emoji
# icon per item from Admin -> Menu Manager (color) / Bot Configuration
# (emoji id), stored as menu_item_<key>_style / menu_item_<key>_emoji_id.
#
# NOTE: style / icon_custom_emoji_id require python-telegram-bot >= 22.7
# (see requirements.txt). On older versions InlineKeyboardButton doesn't
# accept these kwargs -- _styled_button() falls back to a plain button
# instead of crashing so the bot keeps working either way.

# Derived from the single canonical table in utils.button_colors so this
# never drifts from the admin Menu Manager UI or the bot_config seed
# defaults, which read the same DEFAULT_MENU_ITEM_COLORS dict.
_DEFAULT_MENU_STYLES = {
    key: telegram_style_for_color(color)
    for key, color in DEFAULT_MENU_ITEM_COLORS.items()
}


def _styled_button(text: str, item_key: str = None, callback_data: str = None, url: str = None,
                    style: str = None, emoji_id: str = None):
    """Build an InlineKeyboardButton with per-item color + custom emoji icon.

    `item_key` looks up menu_item_<item_key>_style / _emoji_id in
    bot_config, falling back to _DEFAULT_MENU_STYLES. Pass item_key=None
    and set `style`/`emoji_id` directly instead (used for admin-defined
    custom buttons, which carry their own style/emoji_id in their JSON).
    """
    # A profile may supply its own style/icon.  Only consult the legacy
    # per-item config when the caller did not provide a profile override.
    profile_override = style is not None or emoji_id is not None
    if item_key and not profile_override:
        try:
            from utils.bot_config import cfg as _cfg
            style = _cfg.get_str(f"menu_item_{item_key}_style", _DEFAULT_MENU_STYLES.get(item_key, ""))
            emoji_id = _cfg.get_str(f"menu_item_{item_key}_emoji_id", "")
            # NOTE: The "colors enabled" toggle is stored in the audience-profile
            # JSON (colors_enabled field written by save_menu_layout) and applied
            # by the callers before they decide which style= value to pass here.
            # There is no "main_menu_colors_enabled" bot_config key; any check
            # against it would always return the default (True) and is dead code.
        except Exception:
            style = _DEFAULT_MENU_STYLES.get(item_key)
    if style is None:
        # Main-menu colors can be explicitly disabled. For custom buttons
        # without a stored color, use the shared automatic default instead.
        if item_key is None:
            style = telegram_style_for_color(
                get_button_color(callback_data or text, text=text)
            )
    else:
        style = telegram_style_for_color(normalize_color(style, fallback="blue"))
    if not emoji_id:
        emoji_id = None

    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url

    try:
        return InlineKeyboardButton(text, style=style, icon_custom_emoji_id=emoji_id, **kwargs)
    except TypeError:
        # Older python-telegram-bot (<22.7) doesn't know these params yet --
        # degrade gracefully to a plain, uncolored button.
        return InlineKeyboardButton(text, **kwargs)


def create_main_menu_keyboard(
    lang: str = "en",
    user_id: int = None,
    audience: str = None,
    simulate: bool = False,
):
    """Create the main menu keyboard for users, localized per `lang`.

    Fully dynamic menu system -- no button, row, or item is hardcoded
    here. The set of items, their labels, callbacks, row placement, and
    admin-only status all come from `utils.menu_registry.get_menu_items()`,
    which itself can be overridden at runtime via bot_config (no code
    change / deploy needed to add a future item). This function's only
    job is to *render* whatever the registry returns, plus append any
    admin-defined custom buttons.

    Each item stays individually togglable from Admin > Menu Manager, and
    its color / custom emoji icon can still be overridden the same way
    (see _styled_button above). callback_data values are unchanged for
    backward compatibility with existing bookmarks and inline keyboards.

    This is also the single renderer used for the admin Live Preview (see
    ``handlers.admin_menu_manager``) so preview and the real menu can never
    drift apart:

    audience:
        Explicit profile to render (``"users"`` | ``"premium"`` | ``"admins"``).
        Normal callers omit this and let it resolve from ``user_id`` as
        before. The admin preview passes this to simulate any profile
        regardless of the viewing admin's own audience.
    simulate:
        When True, every button's callback_data (and any url) is replaced
        with the harmless ``"mm:noop"`` callback so the preview is
        interactive but performs no real action. Real callers never set
        this, so real-menu behavior is unchanged.
    """
    from .menu_registry import (
        get_custom_buttons_for_audience,
        get_item_label,
        get_menu_audience,
        get_menu_items_for_audience,
        is_item_enabled,
        is_item_visible,
        menu_colors_enabled,
        normalize_menu_audience,
        sorted_rows,
    )

    try:
        from utils.bot_config import cfg as _cfg
        def _enabled(item):
            return is_item_enabled(item)
    except Exception:
        _cfg = None
        def _enabled(item):
            return item.get("default_enabled", True)

    audience = normalize_menu_audience(audience) if audience else get_menu_audience(user_id)

    # The per-profile "Colors" switch (Users/Premium/Admins) only ever
    # toggled the *active* profile being edited in Menu Manager, so turning
    # colors off for "Users" left "Admins" unchanged -- confusing for an
    # admin testing their own /start. The "All Bot Buttons" switch is meant
    # to be the bot-wide kill switch, so it now also gates the Main Menu
    # here, on top of (not instead of) the per-profile switch: colors show
    # only when BOTH are on.
    try:
        from utils.bot_config import cfg as _colors_cfg
        _global_colors_on = _colors_cfg.get_bool("global_button_colors_enabled", True)
    except Exception:
        _global_colors_on = True
    _colors_on = _global_colors_on and menu_colors_enabled(audience)

    def _render(item):
        label = get_item_label(item, lang)
        callback = item.get("callback")
        url = item.get("url")
        if simulate:
            callback, url = "mm:noop", None
        elif not _enabled(item):
            callback = f"menu_disabled:{item.get('key')}"
        return _styled_button(
            label,
            item_key=item.get("key"),
            callback_data=callback,
            url=url,
            emoji_id=item.get("emoji_id", ""),
            style=(
                item.get("style") or _DEFAULT_MENU_STYLES.get(item.get("key"), "none")
                if _colors_on
                else "none"
            ),
        )

    keyboard = []
    for row_items in sorted_rows(get_menu_items_for_audience(audience)):
        packed = []
        for item in row_items:
            if not is_item_visible(item):
                continue
            btn = _render(item)
            if item.get("full_width"):
                if packed:
                    keyboard.append(packed)
                    packed = []
                keyboard.append([btn])
            else:
                packed.append(btn)
        if packed:
            keyboard.append(packed)

    # -- Custom buttons (admin-configurable via Menu Manager) ---------------
    # Sourced entirely from admin-supplied JSON. The manager keeps the list
    # ordered, so array order is the visual position and remains unlimited.
    try:
        _custom = get_custom_buttons_for_audience(audience)
        for _btn in _custom:
            if not isinstance(_btn, dict) or "label" not in _btn:
                continue
            _visible = _btn.get("visible", True)
            if isinstance(_visible, str):
                _visible = _visible.strip().lower() in ("1", "true", "yes", "on")
            if not _visible:
                continue
            _emoji = _btn.get("emoji", "")
            _lbl = f"{_emoji} {_btn['label']}".strip() if _emoji else _btn["label"]
            _style = _btn.get("color", _btn.get("style")) if _colors_on else "none"
            _emoji_id = _btn.get("emoji_id")
            _url = _btn.get("url")
            _cb = _btn.get("callback")
            if simulate:
                _cb, _url = "mm:noop", None
            if _url is not None:
                keyboard.append([_styled_button(_lbl, url=_url, style=_style, emoji_id=_emoji_id)])
            elif _cb is not None:
                keyboard.append([_styled_button(_lbl, callback_data=_cb, style=_style, emoji_id=_emoji_id)])
    except Exception:
        # A malformed optional custom-button profile must not affect the
        # standard menu.
        pass

    return InlineKeyboardMarkup(keyboard)


def _get_display_languages() -> tuple:
    """Return the tuple of language codes to show in the picker.

    Tries to honour admin-enabled languages from LanguageConfig.  Falls back
    to all SUPPORTED_LANGUAGES when the table is empty (fresh install) or
    on any database error, so the picker is always functional.
    """
    try:
        from database import get_db_session
        from database.models import LanguageConfig
        with get_db_session() as s:
            rows = s.query(LanguageConfig).filter(LanguageConfig.is_enabled == True).all()  # noqa: E712
            enabled = tuple(r.code for r in rows if r.code in SUPPORTED_LANGUAGES)
            if enabled:
                return enabled
    except Exception:
        pass
    return SUPPORTED_LANGUAGES


def create_language_keyboard(lang: str = "en"):
    """Language picker keyboard — one button per admin-enabled language.

    The caller's current language gets a ✅ prefix so the active choice is
    visible at a glance. Falls back to all SUPPORTED_LANGUAGES when no
    LanguageConfig rows exist (e.g. fresh install) so the picker always
    shows something.
    """
    display_langs = _get_display_languages()
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅ ' if code == lang else ''}{LANGUAGE_FLAGS.get(code, '')} {LANGUAGE_NAMES.get(code, code)}",
            callback_data=f"set_lang_{code}",
        )]
        for code in display_langs
    ]
    keyboard.append([InlineKeyboardButton(t("common.main_menu", lang), callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_refer_keyboard(lang: str):
    """Refer & Earn keyboard — clean, minimal marketplace layout."""
    keyboard = [
        [InlineKeyboardButton("📜 Referral History", callback_data="rd:comm")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def create_support_center_keyboard(lang: str, support_username: str = ""):
    """Support Center main keyboard — compact, ticket-focused (English-only)."""
    keyboard = [
        [InlineKeyboardButton("🎫 Open Ticket", callback_data="sc_new")],
        [InlineKeyboardButton("📂 My Tickets", callback_data="sc_list")],
        [InlineKeyboardButton("❓ FAQ", callback_data="sc_page_faq")],
    ]
    if support_username:
        keyboard.append([InlineKeyboardButton(
            "📞 Chat with Support",
            url=f"https://t.me/{support_username}",
        )])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)



def create_back_support_keyboard():
    """Create standard back and support buttons."""
    keyboard = [
        [
            InlineKeyboardButton("🔙 Back", callback_data="back"),
            InlineKeyboardButton("☎️ Support", callback_data="support_center")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_pagination_keyboard(items, page, total_pages, callback_prefix, back_button=True):
    """Create a paginated keyboard with items."""
    keyboard = []

    # Add item buttons - items should already be a list of button rows
    keyboard.extend(items)

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"{callback_prefix}_page_{page-1}"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{callback_prefix}_page_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    # Add back and support buttons
    if back_button:
        keyboard.append([
            InlineKeyboardButton("🔙 Back", callback_data="back"),
            InlineKeyboardButton("☎️ Support", callback_data="support_center")
        ])

    return InlineKeyboardMarkup(keyboard)


def create_product_detail_keyboard(
    product_id,
    back_callback="back",
    telegram_id: int = None,
    stock_count: int = None,
):
    """Create keyboard for product details view.

    When ``telegram_id`` is provided and user-facing features are enabled,
    Wishlist and Price Drop Alert toggle buttons are injected automatically.
    When ``stock_count`` is 0 the Buy Now button is replaced with a
    Restock Notification toggle (urns:sub / urns:unsub).
    """
    # ── Primary action button ──────────────────────────────────────────────
    if stock_count == 0:
        # Out of stock — show notify-me button, subscription-state aware
        _notify_label = "🔔 Notify Me When Available"
        _notify_cb    = f"urns:sub:{product_id}"
        if telegram_id is not None:
            try:
                from services.restock_service import is_subscribed as _is_sub
                from database import get_db_session as _gds
                from database.models import User as _User
                with _gds() as _s:
                    _u = _s.query(_User).filter_by(telegram_id=telegram_id).first()
                    if _u and _is_sub(_u.id, product_id):
                        _notify_label = "🔕 Cancel Alert"
                        _notify_cb    = f"urns:unsub:{product_id}"
            except Exception:
                pass
        keyboard = [
            [InlineKeyboardButton(_notify_label, callback_data=_notify_cb)],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy_{product_id}")],
        ]

    # Inject feature buttons (Wishlist, Price Drop Alert) when caller provides telegram_id
    if telegram_id is not None:
        try:
            from handlers.feature_handlers import build_product_feature_buttons
            feature_rows = build_product_feature_buttons(telegram_id, product_id)
            keyboard.extend(feature_rows)
        except Exception:
            pass

        # V22: Favorites button
        try:
            from utils.bot_config import cfg as _cfg2
            if _cfg2.get_bool("feature_favorites_enabled", True):
                _fstatus = _cfg2.get_str("favorites_status", "enabled").lower()
                if _fstatus != "disabled":
                    from services.favorites_service import (
                        is_favorited as _is_fav,
                        get_count as _fav_cnt,
                        show_counter as _fav_show_cnt,
                    )
                    in_fav = _is_fav(telegram_id, product_id)
                    if in_fav:
                        fav_label = "💔 Remove from Favorites"
                        fav_cb    = f"fav:rm:{product_id}"
                    else:
                        if _fav_show_cnt():
                            cnt = _fav_cnt(telegram_id)
                            fav_label = f"❤️ Add to Favorites [{cnt}]"
                        else:
                            fav_label = "❤️ Add to Favorites"
                        fav_cb = f"fav:add:{product_id}"
                    keyboard.append([InlineKeyboardButton(fav_label, callback_data=fav_cb)])
        except Exception:
            pass

        # V22: Product Compare button
        try:
            from utils.bot_config import cfg as _cfg
            if _cfg.get_bool("feature_product_compare_enabled", True):
                _status = _cfg.get_str("product_compare_status", "enabled").lower()
                if _status != "disabled":
                    from services.product_compare import (
                        is_in_compare as _in_cmp,
                        get_compare_count as _cmp_cnt,
                        max_products as _cmp_max,
                        show_counter as _show_cnt,
                    )
                    in_cmp = _in_cmp(telegram_id, product_id)
                    if in_cmp:
                        btn_label = "❌ Remove from Compare"
                        btn_cb    = f"cmp:rm:{product_id}"
                    else:
                        if _show_cnt():
                            cnt = _cmp_cnt(telegram_id)
                            mx  = _cmp_max()
                            btn_label = f"⚖️ Add to Compare [{cnt}/{mx}]"
                        else:
                            btn_label = "⚖️ Add to Compare"
                        btn_cb = f"cmp:add:{product_id}"
                    keyboard.append([InlineKeyboardButton(btn_label, callback_data=btn_cb)])
        except Exception:
            pass

        # V23: Price History button
        try:
            from utils.bot_config import cfg as _cfg3
            if _cfg3.get_bool("price_history_enabled", True):
                _ph_status = _cfg3.get_str("price_history_status", "enabled").lower()
                if _ph_status != "disabled" and _cfg3.get_bool("price_history_allow_users", True):
                    keyboard.append([
                        InlineKeyboardButton("📈 Price History",
                                             callback_data=f"ph:view:{product_id}:0")
                    ])
        except Exception:
            pass

        # V23: Inventory Reservation button
        try:
            from utils.bot_config import cfg as _cfg4
            if _cfg4.get_bool("irs_enabled", True):
                _irs_status = _cfg4.get_str("irs_status", "enabled").lower()
                if _irs_status != "disabled":
                    # Check if user already has an active reservation for this product
                    _res_label = "⏳ Reserve Stock"
                    try:
                        from services.inventory_reservation_ui import (
                            get_user_pk as _get_pk,
                            get_user_active_reservation as _get_res,
                            format_countdown as _countdown,
                        )
                        _user_pk = _get_pk(telegram_id)
                        if _user_pk:
                            _active = _get_res(_user_pk, product_id)
                            if _active:
                                _cd = _countdown(_active.expires_at)
                                if _cd != "Expired":
                                    _res_label = f"⏳ Reserved: {_cd}"
                    except Exception:
                        pass
                    keyboard.append([
                        InlineKeyboardButton(_res_label,
                                             callback_data=f"irs:view:{product_id}")
                    ])
        except Exception:
            pass

    # V25: Product FAQ button
    try:
        from utils.bot_config import cfg as _cfg5
        _pfaq_status = _cfg5.get_str("pfaq_status", "enabled")
        if _pfaq_status != "disabled":
            from services.product_faq import faq_count as _faq_cnt, show_counter as _faq_show_cnt
            _cnt = _faq_cnt(product_id, active_only=True)
            if _cnt > 0 or _pfaq_status == "maintenance":
                if _faq_show_cnt() and _cnt > 0:
                    _faq_lbl = f"❓ FAQ ({_cnt})"
                else:
                    _faq_lbl = "❓ FAQ"
                keyboard.append([
                    InlineKeyboardButton(_faq_lbl,
                                         callback_data=f"pfaq:view:{product_id}")
                ])
    except Exception:
        pass

    keyboard.append([
        InlineKeyboardButton("🔙 Back", callback_data=back_callback),
        InlineKeyboardButton("☎️ Support", callback_data="support_center")
    ])
    return InlineKeyboardMarkup(keyboard)


def create_quantity_keyboard(product_id):
    """Create keyboard for the quantity input step."""
    keyboard = [
        [
            InlineKeyboardButton("🔙 Back", callback_data=f"product_{product_id}"),
            InlineKeyboardButton("❌ Close", callback_data="cancel_purchase"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_cancel_keyboard():
    """Create a simple cancel button keyboard."""
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)


def create_payment_method_keyboard(methods=None, gateways=None):
    """Create payment method selection keyboard.

    Renders gateways and manual methods grouped by category:
      1. Payment Providers (Bybit Pay, Binance Pay)
      2. USDT Networks (TRC20, BEP20, ERC20, TON, Solana, Avalanche …)
      3. Other Crypto (LTC, Cryptomus, NOWPayments, Heleket)
      4. Local Payment (bKash, Nagad, ZiniPay, Telegram Stars)
      5. Admin manual methods
      6. Back to Menu

    ``gateways``: list of dicts like
        [{"key": "bybit_pay", "label": "Bybit Pay", "emoji": "⭐"}, ...]
    """
    # Canonical label overrides for consistent naming across the UI.
    _LABEL_OVERRIDE = {
        "bybit_trc20":  ("💵", "USDT (TRC20)"),
        "bybit_bep20":  ("🟢", "USDT (BEP20)"),
        "bybit_erc20":  ("🔵", "USDT (ERC20)"),
        "bybit_ton":    ("⚫", "USDT (TON)"),
        "bybit_sol":    ("🟣", "USDT (Solana)"),
        "bybit_avaxc":  ("🔺", "USDT (Avalanche C-Chain)"),
        "bybit_base":   ("🔷", "USDT (Base)"),
        "bybit_arb":    ("🔵", "USDT (Arbitrum)"),
        "bybit_op":     ("🔴", "USDT (Optimism)"),
        "bybit_matic":  ("🟣", "USDT (Polygon)"),
        "bybit_ltc":    ("🪙", "Litecoin (LTC)"),
        "bybit_pay":    ("⭐", "Bybit Pay"),
        "binance_pay":  ("🟡", "Binance Pay"),
        "zinipay":      ("🇧🇩", "BKash • Nagad • Rocket"),
        "bkash":        ("📱", "bKash"),
        "nagad":        ("🟠", "Nagad"),
        "stars":        ("⭐", "Telegram Stars"),
        "cryptomus":    ("💠", "Cryptomus (USDT/Crypto)"),
        "nowpayments":  ("🌐", "NOWPayments (Crypto)"),
        "heleket":      ("🪙", "Crypto Deposit (Address)"),
    }

    # Ordered group buckets.
    _PROVIDERS   = {"bybit_pay", "binance_pay"}
    _USDT_NETS   = {"bybit_trc20", "bybit_bep20", "bybit_erc20",
                    "bybit_ton", "bybit_sol", "bybit_avaxc",
                    "bybit_base", "bybit_arb", "bybit_op", "bybit_matic",
                    "cryptomus", "nowpayments", "heleket"}
    _CRYPTO_ALT  = {"bybit_ltc"}
    _LOCAL       = {"bkash", "nagad", "zinipay", "stars"}

    buckets = {
        "providers":  [],
        "usdt_nets":  [],
        "crypto_alt": [],
        "local":      [],
        "other":      [],
    }

    for gw in (gateways or []):
        key = gw["key"]
        emoji_l, label_l = _LABEL_OVERRIDE.get(key, (gw.get("emoji", "🏦"), gw.get("label", key)))
        btn = InlineKeyboardButton(f"{emoji_l} {label_l}", callback_data=f"pay_{key}")
        if key in _PROVIDERS:
            buckets["providers"].append([btn])
        elif key in _USDT_NETS:
            buckets["usdt_nets"].append([btn])
        elif key in _CRYPTO_ALT:
            buckets["crypto_alt"].append([btn])
        elif key in _LOCAL:
            buckets["local"].append([btn])
        else:
            buckets["other"].append([btn])

    keyboard = []
    for group in ("providers", "usdt_nets", "crypto_alt", "local", "other"):
        keyboard.extend(buckets[group])

    # Admin-managed manual payment methods.
    for m in (methods or []):
        label = f"{m.emoji or '💳'} {m.name}"
        # Unified pattern (pay_pm_<id>); legacy pay_manual_<id> still routed in bot.py for BC.
        keyboard.append([InlineKeyboardButton(label, callback_data=f"pay_pm_{m.id}")])

    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)



def create_support_keyboard(support_username, channel_username):
    """Create support page keyboard with contact and community links."""
    keyboard = []

    if support_username:
        keyboard.append([InlineKeyboardButton("📞 Contact support", url=f"https://t.me/{support_username}")])

    if channel_username:
        keyboard.append([InlineKeyboardButton("🫂 Join My Community", url=f"https://t.me/{channel_username}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])

    return InlineKeyboardMarkup(keyboard)


def create_admin_main_menu_keyboard():
    """Create admin panel main menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_menu", audience="admin")


def create_admin_product_menu_keyboard():
    """Create admin product management menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_products_menu", audience="admin")


def create_admin_category_menu_keyboard():
    """Create admin category management menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_category_menu", audience="admin")


def create_admin_user_menu_keyboard():
    """Create admin user management menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_user_menu", audience="admin")


def create_admin_order_menu_keyboard():
    """Create admin order management menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_order_menu", audience="admin")


def create_admin_settings_menu_keyboard():
    """Create admin store settings menu keyboard (fully dynamic via menu_builder).

    The ``currency_toggle`` button label is computed at render time via
    ``runtime_overrides`` so it correctly reflects the live config value
    without being stored in bot_config.
    """
    from utils.bot_config import cfg
    from utils.menu_builder import get_menu_keyboard
    currency_btn_label = (
        "🌐 Currency Toggle Button: ✅ Show"
        if cfg.get_bool("show_currency_toggle_button", False)
        else "🌐 Currency Toggle Button: 🚫 Hide"
    )
    return get_menu_keyboard(
        "admin_settings_menu",
        audience="admin",
        runtime_overrides={"currency_toggle": {"label": currency_btn_label}},
    )


def create_admin_payment_methods_menu_keyboard(methods):
    """List existing manual payment methods with an add-new button."""
    keyboard = []
    for m in methods:
        status = "✅" if m.is_active else "🚫"
        keyboard.append([InlineKeyboardButton(
            f"{status} {m.emoji or '💳'} {m.name}",
            callback_data=f"admin_pm_view_{m.id}"
        )])
    keyboard.append([InlineKeyboardButton("➕ Add Payment Method", callback_data="admin_pm_add")])
    if methods:
        keyboard.append([InlineKeyboardButton("🗑 Delete All", callback_data="admin_pm_delete_all_confirm")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    return InlineKeyboardMarkup(keyboard)


def create_admin_payment_method_detail_keyboard(method):
    """View / manage a single manual payment method."""
    toggle_label = "🚫 Disable" if method.is_active else "✅ Enable"
    txid_label = "🧾 TXID: ON" if getattr(method, "require_txid", True) else "🧾 TXID: OFF"
    proof_label = "📸 Proof: ON" if getattr(method, "require_proof", True) else "📸 Proof: OFF"
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"admin_pm_edit_name_{method.id}"),
         InlineKeyboardButton("✏️ Edit Emoji", callback_data=f"admin_pm_edit_emoji_{method.id}")],
        [InlineKeyboardButton("🏷 Account Label", callback_data=f"admin_pm_edit_label_{method.id}"),
         InlineKeyboardButton("💳 Account Number", callback_data=f"admin_pm_edit_acct_{method.id}")],
        [InlineKeyboardButton("✏️ Edit Instructions", callback_data=f"admin_pm_edit_instr_{method.id}")],
        [InlineKeyboardButton("💵 Min Amount", callback_data=f"admin_pm_edit_min_{method.id}"),
         InlineKeyboardButton("🔝 Max Amount", callback_data=f"admin_pm_edit_max_{method.id}")],
        [InlineKeyboardButton("↕️ Display Order", callback_data=f"admin_pm_edit_order_{method.id}")],
        [InlineKeyboardButton(txid_label, callback_data=f"admin_pm_tgl_txid_{method.id}"),
         InlineKeyboardButton(proof_label, callback_data=f"admin_pm_tgl_proof_{method.id}")],
        [InlineKeyboardButton(toggle_label, callback_data=f"admin_pm_toggle_{method.id}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"admin_pm_delete_{method.id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_payment_methods")],
    ]
    return InlineKeyboardMarkup(keyboard)


GATEWAY_LABELS = {
    "bkash": ("📱", "bKash"),
    "nagad": ("🟠", "Nagad"),
}


def create_admin_gateways_menu_keyboard(status: dict):
    """List bKash / Nagad / Telegram Stars / Cryptomus gateways with their on/off status.

    ``status``: dict like {"bkash": True, "nagad": False, "stars": True, "cryptomus": False}
    Telegram Stars routes to ``admin_stars_view`` (handlers/admin_stars.py) and
    Cryptomus routes to ``admin_cryptomus_view`` (handlers/admin_cryptomus.py)
    since both are backed by ``PaymentGatewayConfig``, not bot_config.
    """
    keyboard = []
    for key, (emoji, name) in GATEWAY_LABELS.items():
        on = "✅" if status.get(key) else "🚫"
        keyboard.append([InlineKeyboardButton(
            f"{on} {emoji} {name}", callback_data=f"admin_gw_view_{key}"
        )])
    stars_on = "✅" if status.get("stars") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{stars_on} ⭐ Telegram Stars", callback_data="admin_stars_view"
    )])
    heleket_on = "✅" if status.get("heleket") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{heleket_on} 🟣 Heleket Static Wallet", callback_data="admin_heleket_view"
    )])
    cryptomus_on = "✅" if status.get("cryptomus") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{cryptomus_on} 💠 Cryptomus", callback_data="admin_cryptomus_view"
    )])
    nowpayments_on = "✅" if status.get("nowpayments") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{nowpayments_on} 🌐 NOWPayments", callback_data="admin_nowpayments_view"
    )])
    zinipay_on = "✅" if status.get("zinipay") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{zinipay_on} 🇧🇩 ZiniPay", callback_data="admin_zinipay_view"
    )])
    binance_on = "✅" if status.get("binance_pay") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{binance_on} 🟡 Binance Pay", callback_data="admin_binance_view"
    )])
    bybit_on = "✅" if status.get("bybit_pay") else "🚫"
    keyboard.append([InlineKeyboardButton(
        f"{bybit_on} 💙 Bybit Pay", callback_data="admin_bybit_view"
    )])
    keyboard.append([InlineKeyboardButton("💰 Deposit Settings", callback_data="admin_deposit_view")])
    keyboard.append([InlineKeyboardButton("🗑 Delete/Disable All", callback_data="admin_gw_disable_all_confirm")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="acc:root")])
    return InlineKeyboardMarkup(keyboard)


def create_admin_gateway_detail_keyboard(gateway_key: str, is_enabled: bool, mode: str = "auto"):
    """View / manage a single payment gateway (bKash or Nagad).

    ``mode``: "auto" (API credential fields) or "manual" (merchant number +
    instructions fields instead — see services/gateway_manual_mode.py).
    """
    toggle_label = "🚫 Disable" if is_enabled else "✅ Enable"
    mode_toggle_label = "🔄 Mode: Auto" if mode == "auto" else "🔄 Mode: Manual"

    if mode == "manual":
        # Manual mode: hide API credentials, show merchant number + instructions.
        field_rows = [
            [InlineKeyboardButton("📞 Merchant Number", callback_data=f"admin_gw_edit_manualnumber_{gateway_key}")],
            [InlineKeyboardButton("📝 Instructions", callback_data=f"admin_gw_edit_manualinstr_{gateway_key}")],
            [InlineKeyboardButton("💵 Min Amount", callback_data=f"admin_gw_edit_min_{gateway_key}"),
             InlineKeyboardButton("🔝 Max Amount", callback_data=f"admin_gw_edit_max_{gateway_key}")],
        ]
    elif gateway_key == "bkash":
        field_rows = [
            [InlineKeyboardButton("🌐 Mode (sandbox/live)", callback_data="admin_gw_edit_mode_bkash")],
            [InlineKeyboardButton("🔑 App Key", callback_data="admin_gw_edit_appkey_bkash"),
             InlineKeyboardButton("🔒 App Secret", callback_data="admin_gw_edit_appsecret_bkash")],
            [InlineKeyboardButton("👤 Username", callback_data="admin_gw_edit_username_bkash"),
             InlineKeyboardButton("🔒 Password", callback_data="admin_gw_edit_password_bkash")],
            [InlineKeyboardButton("💵 Min Amount", callback_data="admin_gw_edit_min_bkash"),
             InlineKeyboardButton("🔝 Max Amount", callback_data="admin_gw_edit_max_bkash")],
        ]
    else:
        field_rows = [
            [InlineKeyboardButton("🌐 Mode (sandbox/live)", callback_data="admin_gw_edit_mode_nagad")],
            [InlineKeyboardButton("🏢 Merchant ID", callback_data="admin_gw_edit_merchantid_nagad"),
             InlineKeyboardButton("📞 Merchant Number", callback_data="admin_gw_edit_merchantnumber_nagad")],
            [InlineKeyboardButton("🔓 Public Key", callback_data="admin_gw_edit_pubkey_nagad"),
             InlineKeyboardButton("🔒 Private Key", callback_data="admin_gw_edit_privkey_nagad")],
            [InlineKeyboardButton("💵 Min Amount", callback_data="admin_gw_edit_min_nagad"),
             InlineKeyboardButton("🔝 Max Amount", callback_data="admin_gw_edit_max_nagad")],
        ]
    keyboard = (
        [[InlineKeyboardButton(mode_toggle_label, callback_data=f"admin_gw_mode_toggle_{gateway_key}")]]
        + field_rows
        + [
            [InlineKeyboardButton(toggle_label, callback_data=f"admin_gw_toggle_{gateway_key}")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")],
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def create_admin_broadcast_menu_keyboard():
    """Create admin broadcast menu keyboard (fully dynamic via menu_builder)."""
    from utils.menu_builder import get_menu_keyboard
    return get_menu_keyboard("admin_broadcast_menu", audience="admin")
