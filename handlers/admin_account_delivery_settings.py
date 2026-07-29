"""V41 — Account / Login Delivery Settings admin handler.

Namespace: ``accdel:*``

Provides a dedicated, toggle-driven settings page for controlling how
📧 Account / Login products are delivered to customers.

Navigation
──────────
  accdel:menu          — Main settings menu
  accdel:toggle:<key>  — Flip a boolean setting ON/OFF
  accdel:edit:<key>    — Edit a text/int setting (ConversationHandler)
  accdel:restore       — Confirm restore-to-defaults prompt
  accdel:restore_confirm — Execute restore
  accdel:back          — Return to Store Settings

Entry point: callback_data ``accdel:menu``
Registered in menu_builder via the admin_settings_menu.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── ConversationHandler state ─────────────────────────────────────────────────
_WAITING_VALUE = 55

# ── Setting definitions ───────────────────────────────────────────────────────
# (key, label, type)  — type: "bool" | "str" | "int"
_BOOL_SETTINGS: list[tuple[str, str]] = [
    ("accdel_compact_layout",       "Compact Layout"),
    ("accdel_show_order_summary",   "Order Summary"),
    ("accdel_show_product_info",    "Product Information"),
    ("accdel_show_purchase_time",   "Purchase Time"),
    ("accdel_show_quantity",        "Quantity"),
    ("accdel_show_2fa",             "2FA Display"),
    ("accdel_auto_txt_enabled",     "Auto TXT Delivery"),
    ("accdel_txt_include_summary",  "TXT: Order Summary"),
    ("accdel_txt_include_product_name", "TXT: Product Name"),
]

_STR_SETTINGS: list[tuple[str, str, str]] = [
    # (key, label, hint)
    ("accdel_txt_filename_format", "TXT Filename",
     "Placeholders: {order_id}, {product}. Example: {order_id}.txt"),
    ("accdel_txt_divider",         "TXT Divider Style",
     "Section separator between accounts. Example: ━━━━━━━━━━━━━━ or --------------"),
    ("accdel_txt_numbering",       "TXT Numbering Style",
     "Type 'circle' for ①②③ or 'plain' for 1, 2, 3"),
]

_INT_SETTINGS: list[tuple[str, str, str]] = [
    ("account_delivery_inline_limit", "Inline Account Limit",
     "Max accounts sent inline. Orders above this are sent as a .txt file."),
]

# Defaults — must match bot_config.py DEFAULTS
_DEFAULTS: dict[str, object] = {
    "accdel_compact_layout":           False,
    "accdel_show_order_summary":       True,
    "accdel_show_product_info":        True,
    "accdel_show_purchase_time":       True,
    "accdel_show_quantity":            True,
    "accdel_show_2fa":                 True,
    "accdel_auto_txt_enabled":         True,
    "accdel_txt_include_summary":      True,
    "accdel_txt_include_product_name": True,
    "accdel_txt_filename_format":      "{order_id}.txt",
    "accdel_txt_divider":              "━━━━━━━━━━━━━━",
    "accdel_txt_numbering":            "circle",
    "account_delivery_inline_limit":   5,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_cfg():
    from utils.bot_config import cfg
    return cfg


def _bool_icon(val: bool) -> str:
    return "✅" if val else "🔴"


def _build_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Build the settings menu text and keyboard."""
    _cfg = _get_cfg()

    # Read current values
    bools: dict[str, bool] = {}
    for key, _ in _BOOL_SETTINGS:
        default = _DEFAULTS.get(key, False)
        bools[key] = _cfg.get_bool(key, bool(default))

    strs: dict[str, str] = {}
    for key, _, _ in _STR_SETTINGS:
        default = str(_DEFAULTS.get(key, ""))
        strs[key] = _cfg.get_str(key, default) or default

    ints: dict[str, int] = {}
    for key, _, _ in _INT_SETTINGS:
        default = int(_DEFAULTS.get(key, 0))
        ints[key] = _cfg.get_int(key, default)

    # Build text
    lines = [
        "📧 *Account / Login Delivery Settings*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "*Delivery Display*",
        f"  {_bool_icon(bools['accdel_compact_layout'])} Compact Layout",
        f"  {_bool_icon(bools['accdel_show_order_summary'])} Order Summary",
        f"  {_bool_icon(bools['accdel_show_product_info'])} Product Information",
        f"  {_bool_icon(bools['accdel_show_purchase_time'])} Purchase Time",
        f"  {_bool_icon(bools['accdel_show_quantity'])} Quantity",
        f"  {_bool_icon(bools['accdel_show_2fa'])} 2FA Display",
        "",
        "*TXT File Delivery*",
        f"  {_bool_icon(bools['accdel_auto_txt_enabled'])} Auto TXT Delivery",
        f"  Inline Limit: *{ints['account_delivery_inline_limit']}* accounts",
        f"  Filename: `{strs['accdel_txt_filename_format']}`",
        f"  Divider: `{strs['accdel_txt_divider']}`",
        f"  Numbering: *{strs['accdel_txt_numbering']}* "
        f"({'①②③' if strs['accdel_txt_numbering'] == 'circle' else '1,2,3'})",
        f"  {_bool_icon(bools['accdel_txt_include_summary'])} TXT: Order Summary",
        f"  {_bool_icon(bools['accdel_txt_include_product_name'])} TXT: Product Name",
    ]
    text = "\n".join(lines)

    # Build keyboard — toggles for booleans
    rows: list = []

    # Display toggles
    rows.append([InlineKeyboardButton(
        f"{_bool_icon(bools['accdel_compact_layout'])} Compact Layout",
        callback_data="accdel:toggle:accdel_compact_layout",
    )])
    rows.append([
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_show_order_summary'])} Order Summary",
            callback_data="accdel:toggle:accdel_show_order_summary",
        ),
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_show_product_info'])} Product Info",
            callback_data="accdel:toggle:accdel_show_product_info",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_show_purchase_time'])} Purchase Time",
            callback_data="accdel:toggle:accdel_show_purchase_time",
        ),
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_show_quantity'])} Quantity",
            callback_data="accdel:toggle:accdel_show_quantity",
        ),
    ])
    rows.append([InlineKeyboardButton(
        f"{_bool_icon(bools['accdel_show_2fa'])} 2FA Display",
        callback_data="accdel:toggle:accdel_show_2fa",
    )])

    # TXT section separator
    rows.append([InlineKeyboardButton("── TXT File Settings ──", callback_data="accdel:noop")])

    rows.append([InlineKeyboardButton(
        f"{_bool_icon(bools['accdel_auto_txt_enabled'])} Auto TXT Delivery",
        callback_data="accdel:toggle:accdel_auto_txt_enabled",
    )])
    rows.append([InlineKeyboardButton(
        f"📊 Inline Limit: {ints['account_delivery_inline_limit']}",
        callback_data="accdel:edit:account_delivery_inline_limit",
    )])
    rows.append([InlineKeyboardButton(
        f"📄 Filename: {strs['accdel_txt_filename_format']}",
        callback_data="accdel:edit:accdel_txt_filename_format",
    )])
    rows.append([InlineKeyboardButton(
        f"🔲 Divider Style",
        callback_data="accdel:edit:accdel_txt_divider",
    )])
    rows.append([InlineKeyboardButton(
        f"🔢 Numbering: {strs['accdel_txt_numbering']}",
        callback_data="accdel:edit:accdel_txt_numbering",
    )])
    rows.append([
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_txt_include_summary'])} TXT Summary",
            callback_data="accdel:toggle:accdel_txt_include_summary",
        ),
        InlineKeyboardButton(
            f"{_bool_icon(bools['accdel_txt_include_product_name'])} TXT Product",
            callback_data="accdel:toggle:accdel_txt_include_product_name",
        ),
    ])

    rows.append([InlineKeyboardButton("🔄 Restore Defaults", callback_data="accdel:restore")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_settings")])

    return text, InlineKeyboardMarkup(rows)


# ── Menu handler ──────────────────────────────────────────────────────────────

async def accdel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Account Delivery Settings menu."""
    query = update.callback_query
    if query:
        await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    text, kb = _build_menu()
    try:
        if query:
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        logger.exception("accdel_menu: render failed")


# ── Toggle handler ────────────────────────────────────────────────────────────

async def accdel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip a boolean setting and refresh the menu."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract key from callback_data "accdel:toggle:<key>"
    parts = query.data.split(":", 2)
    if len(parts) < 3:
        return
    key = parts[2]

    _cfg = _get_cfg()
    default = bool(_DEFAULTS.get(key, False))
    current = _cfg.get_bool(key, default)
    _cfg.set(key, not current)

    await accdel_menu(update, context)


# ── Noop handler (section label buttons) ─────────────────────────────────────

async def accdel_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()


# ── Edit (ConversationHandler) ────────────────────────────────────────────────

async def accdel_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start edit conversation for a text/int setting."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    parts = query.data.split(":", 2)
    if len(parts) < 3:
        return ConversationHandler.END
    key = parts[2]

    # Find label and hint
    label = key
    hint = ""
    for k, lbl, h in _STR_SETTINGS + _INT_SETTINGS:
        if k == key:
            label = lbl
            hint = h
            break

    _cfg = _get_cfg()
    default = _DEFAULTS.get(key, "")
    if key in [k for k, _, _ in _INT_SETTINGS]:
        current = str(_cfg.get_int(key, int(default)))
    else:
        current = _cfg.get_str(key, str(default)) or str(default)

    context.user_data["accdel_edit_key"] = key
    context.user_data["accdel_edit_label"] = label

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="accdel:menu")]
    ])

    text = (
        f"✏️ *Edit: {label}*\n\n"
        f"Current value: `{current}`\n\n"
        f"{hint}\n\n"
        "Type the new value or send *cancel* to abort."
    )
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=cancel_kb)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return _WAITING_VALUE


async def accdel_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new value and save it."""
    raw = (update.message.text or "").strip()

    if raw.lower() in ("cancel", "/cancel"):
        await update.message.reply_text("❌ Edit cancelled.")
        return ConversationHandler.END

    key = context.user_data.get("accdel_edit_key", "")
    label = context.user_data.get("accdel_edit_label", key)

    if not key:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    if not has_permission(update.effective_user.id, "manage_settings"):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    _cfg = _get_cfg()

    # Validate and save
    if key in [k for k, _, _ in _INT_SETTINGS]:
        try:
            val = int(raw)
            if val < 1:
                raise ValueError("must be >= 1")
        except ValueError as e:
            await update.message.reply_text(
                f"⚠️ Invalid value: {e}\nPlease enter a positive integer or type *cancel*.",
                parse_mode="Markdown",
            )
            return _WAITING_VALUE
        _cfg.set(key, val)
    else:
        if not raw:
            await update.message.reply_text(
                "⚠️ Value cannot be empty. Enter a value or type *cancel*.",
                parse_mode="Markdown",
            )
            return _WAITING_VALUE
        _cfg.set(key, raw)

    await update.message.reply_text(
        f"✅ *{label}* updated to: `{raw}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def accdel_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the edit via button."""
    query = update.callback_query
    if query:
        await query.answer()
    await accdel_menu(update, context)
    return ConversationHandler.END


# ── Restore defaults ──────────────────────────────────────────────────────────

async def accdel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before restoring defaults."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Restore Defaults", callback_data="accdel:restore_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="accdel:menu")],
    ])
    try:
        await query.edit_message_text(
            "🔄 *Restore Default Settings?*\n\n"
            "This will reset all Account / Login Delivery settings to their "
            "built-in defaults. Existing products and orders are not affected.\n\n"
            "Are you sure?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def accdel_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute restore — delete all accdel_* keys from the DB (falls back to defaults)."""
    query = update.callback_query
    await query.answer()

    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    _cfg = _get_cfg()
    restored = 0
    # Restore bool settings
    for key, _ in _BOOL_SETTINGS:
        try:
            default = _DEFAULTS.get(key, False)
            _cfg.set(key, bool(default))
            restored += 1
        except Exception:
            logger.exception("accdel_restore: failed to reset %s", key)

    # Restore str settings
    for key, _, _ in _STR_SETTINGS:
        try:
            default = str(_DEFAULTS.get(key, ""))
            _cfg.set(key, default)
            restored += 1
        except Exception:
            logger.exception("accdel_restore: failed to reset %s", key)

    # Restore int settings
    for key, _, _ in _INT_SETTINGS:
        try:
            default = int(_DEFAULTS.get(key, 0))
            _cfg.set(key, default)
            restored += 1
        except Exception:
            logger.exception("accdel_restore: failed to reset %s", key)

    await query.answer(f"✅ {restored} settings restored to defaults.", show_alert=True)
    await accdel_menu(update, context)


# ── Handler registration ──────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all accdel:* handlers."""
    from telegram.ext import ConversationHandler as CH

    # Edit conversation
    edit_conv = CH(
        entry_points=[
            CallbackQueryHandler(accdel_edit_start, pattern=r"^accdel:edit:.+$"),
        ],
        states={
            _WAITING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, accdel_edit_receive),
                CallbackQueryHandler(accdel_edit_cancel, pattern=r"^accdel:menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(accdel_edit_cancel, pattern=r"^accdel:menu$"),
        ],
        per_message=False,
    )
    application.add_handler(edit_conv)

    # Simple callbacks
    application.add_handler(CallbackQueryHandler(accdel_menu,           pattern=r"^accdel:menu$"))
    application.add_handler(CallbackQueryHandler(accdel_toggle,         pattern=r"^accdel:toggle:.+$"))
    application.add_handler(CallbackQueryHandler(accdel_noop,           pattern=r"^accdel:noop$"))
    application.add_handler(CallbackQueryHandler(accdel_restore,        pattern=r"^accdel:restore$"))
    application.add_handler(CallbackQueryHandler(accdel_restore_confirm,pattern=r"^accdel:restore_confirm$"))
