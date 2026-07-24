"""Utils package for helper functions and keyboard utilities."""

from .helpers import (
    is_admin, admin_only, get_or_create_user, format_price,
    format_datetime, calculate_expiry_time, paginate_items,
    validate_amount, format_product_display,
    notify_admin, build_availability_text, parse_keys_from_text,
    check_user_banned, clear_ban_cache,
    catalog_stock_emoji, format_product_button_text,
    sanitize_message,
)
from .keyboards import (
    create_main_menu_keyboard, create_back_support_keyboard,
    create_pagination_keyboard, create_product_detail_keyboard,
    create_quantity_keyboard,
    create_cancel_keyboard, create_payment_method_keyboard,
    create_support_keyboard, create_admin_main_menu_keyboard,
    create_admin_product_menu_keyboard, create_admin_category_menu_keyboard,
    create_admin_user_menu_keyboard, create_admin_order_menu_keyboard,
    create_admin_settings_menu_keyboard, create_admin_broadcast_menu_keyboard,
    create_refer_keyboard, create_support_center_keyboard,
    create_admin_payment_methods_menu_keyboard, create_admin_payment_method_detail_keyboard,
    create_admin_gateways_menu_keyboard, create_admin_gateway_detail_keyboard,
    create_language_keyboard,
)
# Full i18n (en/bn) lives in the top-level `i18n` package: see i18n/__init__.py
# (t(), get_user_language(), set_user_language()) and i18n/locales/*.json.
from .currency import (
    format_price_multi, convert_usd, clear_currency_cache,
    get_user_currency, toggle_user_currency, format_amount_in, format_price_for_user,
)
from .receipt import generate_receipt_pdf
from .safe_edit import safe_edit_message_text

__all__ = [
    'is_admin', 'admin_only', 'get_or_create_user', 'format_price',
    'format_datetime', 'calculate_expiry_time', 'paginate_items',
    'validate_amount', 'format_product_display',
    'notify_admin', 'build_availability_text', 'parse_keys_from_text',
    'check_user_banned', 'clear_ban_cache',
    'catalog_stock_emoji', 'format_product_button_text',
    'create_main_menu_keyboard', 'create_back_support_keyboard',
    'create_pagination_keyboard', 'create_product_detail_keyboard',
    'create_quantity_keyboard',
    'create_cancel_keyboard', 'create_payment_method_keyboard',
    'create_support_keyboard', 'create_admin_main_menu_keyboard',
    'create_admin_product_menu_keyboard', 'create_admin_category_menu_keyboard',
    'create_admin_user_menu_keyboard', 'create_admin_order_menu_keyboard',
    'create_admin_settings_menu_keyboard', 'create_admin_broadcast_menu_keyboard',
    'create_refer_keyboard', 'create_support_center_keyboard',
    'create_admin_payment_methods_menu_keyboard', 'create_admin_payment_method_detail_keyboard',
    'create_admin_gateways_menu_keyboard', 'create_admin_gateway_detail_keyboard',
    'create_language_keyboard',
    'format_price_multi', 'convert_usd', 'clear_currency_cache',
    'get_user_currency', 'toggle_user_currency', 'format_amount_in', 'format_price_for_user',
    'generate_receipt_pdf',
    'safe_edit_message_text',
]
