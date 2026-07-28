"""Mobile Banking Number Manager — bKash / Nagad / Rocket / Upay.

Callback namespace: ``mb:``

Numbers are stored in ``bot_config`` as JSON under the key
``mb_{provider}_numbers`` — zero database schema changes required.
Payment logic in payment_handlers.py / services/ is NOT modified.

Each number entry:
    {
        "id":        int,    # auto-incrementing per provider
        "number":    str,    # e.g. "01712345678"
        "label":     str,    # optional label e.g. "Personal", "Agent"
        "is_active": bool,   # 🟢 Enabled / 🔴 Disabled
        "is_default":bool,   # only one per provider
    }

Global:
    mb_default_provider   str   # "bkash" | "nagad" | "rocket" | "upay"

Callback patterns (all ≤ 64 bytes):
    mb:menu               — provider selection menu
    mb:list:{p}           — list numbers for provider
    mb:add:{p}            — start add-number conversation
    mb:view:{p}:{id}      — detail view for one number
    mb:tog:{p}:{id}       — toggle enable/disable
    mb:def:{p}:{id}       — set as default number
    mb:defprov:{p}        — set as default provider
    mb:copy:{p}:{id}      — copy number (CopyTextButton)
    mb:del:{p}:{id}       — confirm delete
    mb:delok:{p}:{id}     — execute delete
    mb:edit:{p}:{id}      — edit number/label (start conv)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from utils.bot_config import cfg
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────
MB_ADD_NUMBER  = 9400
MB_ADD_LABEL   = 9401
MB_EDIT_VAL    = 9402

# ── Provider metadata ──────────────────────────────────────────────────────
_PROVIDERS: dict[str, dict] = {
    "bkash":  {"label": "📱 bKash",  "short": "bKash",  "flag": "🇧🇩"},
    "nagad":  {"label": "🟠 Nagad",  "short": "Nagad",  "flag": "🇧🇩"},
    "rocket": {"label": "🚀 Rocket", "short": "Rocket", "flag": "🇧🇩"},
    "upay":   {"label": "💜 Upay",   "short": "Upay",   "flag": "🇧🇩"},
}

_ALL_PROVIDERS = list(_PROVIDERS.keys())


# ── Storage helpers ────────────────────────────────────────────────────────

def _cfg_key(provider: str) -> str:
    return f"mb_{provider}_numbers"


def _load(provider: str) -> list[dict]:
    raw = cfg.get_str(_cfg_key(provider), "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(provider: str, numbers: list[dict]) -> None:
    cfg.set(_cfg_key(provider), json.dumps(numbers))


def _next_id(numbers: list[dict]) -> int:
    if not numbers:
        return 1
    return max((n.get("id") or 0) for n in numbers) + 1


def _find(numbers: list[dict], nid: int) -> Optional[dict]:
    for n in numbers:
        if n.get("id") == nid:
            return n
    return None


def _prov_label(provider: str) -> str:
    return _PROVIDERS.get(provider, {}).get("label", provider.title())


def _default_provider() -> str:
    return cfg.get_str("mb_default_provider", "bkash")


# ── Text helpers ───────────────────────────────────────────────────────────

def _status_icon(n: dict) -> str:
    return "🟢" if n.get("is_active", True) else "🔴"


def _detail_text(provider: str, n: dict) -> str:
    status = "🟢 Enabled" if n.get("is_active", True) else "🔴 Disabled"
    dflt   = "Yes ★" if n.get("is_default") else "No"
    label  = n.get("label") or "(no label)"
    return (
        f"{_prov_label(provider)} — <b>Number Detail</b>\n\n"
        f"<b>Number:</b>  <code>{n.get('number', '—')}</code>\n"
        f"<b>Label:</b>   {label}\n"
        f"<b>Status:</b>  {status}\n"
        f"<b>Default:</b> {dflt}"
    )


def _list_text(provider: str, numbers: list[dict]) -> str:
    active  = sum(1 for n in numbers if n.get("is_active", True))
    total   = len(numbers)
    def_prov = _default_provider()
    is_dp = " ★ Default Provider" if def_prov == provider else ""
    header = (
        f"{_prov_label(provider)}{is_dp} — <b>Number Manager</b>\n\n"
        f"Total: <b>{total}</b>  |  🟢 <b>{active}</b> Enabled  🔴 <b>{total - active}</b> Disabled\n\n"
    )
    if not numbers:
        return header + "No numbers configured yet.\nTap ➕ to add the first one."
    return header + "Tap a number to manage it, or ➕ to add a new one."


# ── Keyboards ──────────────────────────────────────────────────────────────

def _menu_keyboard() -> InlineKeyboardMarkup:
    def_prov = _default_provider()
    rows = []
    for pid, meta in _PROVIDERS.items():
        numbers    = _load(pid)
        active     = sum(1 for n in numbers if n.get("is_active", True))
        total      = len(numbers)
        dflt_mark  = " ★" if pid == def_prov else ""
        btn_label  = f"{meta['label']}{dflt_mark}  [{active}/{total}]"
        rows.append([InlineKeyboardButton(btn_label, callback_data=f"mb:list:{pid}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_gateways")])
    return InlineKeyboardMarkup(rows)


def _menu_text() -> str:
    def_prov  = _default_provider()
    def_label = _prov_label(def_prov)
    return (
        "📱 <b>Mobile Banking Manager</b>\n\n"
        f"Default Provider: <b>{def_label}</b>\n\n"
        "Select a provider to manage its numbers:"
    )


def _list_keyboard(provider: str, numbers: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for n in numbers:
        icon  = _status_icon(n)
        dflt  = " ★" if n.get("is_default") else ""
        lbl   = n.get("label", "")
        lbl_s = f" ({lbl})" if lbl else ""
        row_lbl = f"{icon} {n.get('number', '—')}{dflt}{lbl_s}"
        rows.append([InlineKeyboardButton(row_lbl, callback_data=f"mb:view:{provider}:{n['id']}")])
    rows.append([InlineKeyboardButton("➕ Add Number", callback_data=f"mb:add:{provider}")])
    def_prov = _default_provider()
    if def_prov != provider:
        rows.append([InlineKeyboardButton(
            f"★ Set {_prov_label(provider)} as Default Provider",
            callback_data=f"mb:defprov:{provider}",
        )])
    rows.append([InlineKeyboardButton("🔙 Back to Providers", callback_data="mb:menu")])
    return InlineKeyboardMarkup(rows)


def _detail_keyboard(provider: str, n: dict) -> InlineKeyboardMarkup:
    nid         = n["id"]
    toggle_lbl  = "🔴 Disable" if n.get("is_active", True) else "🟢 Enable"
    dflt_lbl    = "★ Default Number ✓" if n.get("is_default") else "☆ Set as Default"
    number      = n.get("number", "")
    copy_btn    = (
        InlineKeyboardButton("📋 Copy Number", copy_text=CopyTextButton(text=number))
        if number else
        InlineKeyboardButton("📋 Copy Number", callback_data=f"mb:copy:{provider}:{nid}")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit Number", callback_data=f"mb:edit:{provider}:{nid}:number"),
            InlineKeyboardButton("🏷 Edit Label",  callback_data=f"mb:edit:{provider}:{nid}:label"),
        ],
        [copy_btn],
        [InlineKeyboardButton(dflt_lbl,  callback_data=f"mb:def:{provider}:{nid}")],
        [InlineKeyboardButton(toggle_lbl, callback_data=f"mb:tog:{provider}:{nid}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"mb:del:{provider}:{nid}")],
        [InlineKeyboardButton("🔙 Back",  callback_data=f"mb:list:{provider}")],
    ])


# ── Provider menu ──────────────────────────────────────────────────────────

async def mb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📱 Mobile Banking provider selection menu  (mb:menu)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            _menu_text(), reply_markup=_menu_keyboard(), parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Number list ────────────────────────────────────────────────────────────

async def mb_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List numbers for one provider  (mb:list:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    provider = query.data.split(":", 2)[2]
    if provider not in _ALL_PROVIDERS:
        await query.answer("⚠️ Unknown provider.", show_alert=True)
        return

    numbers = _load(provider)
    try:
        await query.edit_message_text(
            _list_text(provider, numbers),
            reply_markup=_list_keyboard(provider, numbers),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Number detail ──────────────────────────────────────────────────────────

async def mb_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show one number's detail + actions  (mb:view:{p}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    numbers  = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await query.answer("⚠️ Number not found.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            _detail_text(provider, n),
            reply_markup=_detail_keyboard(provider, n),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── Toggle enable/disable ──────────────────────────────────────────────────

async def mb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable / Disable a number  (mb:tog:{p}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    numbers  = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await query.answer("⚠️ Number not found.", show_alert=True)
        return

    n["is_active"] = not n.get("is_active", True)
    _save(provider, numbers)
    state = "🟢 Enabled" if n["is_active"] else "🔴 Disabled"
    await query.answer(f"Number is now {state}.", show_alert=False)
    try:
        await query.edit_message_text(
            _detail_text(provider, n),
            reply_markup=_detail_keyboard(provider, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Set default number ─────────────────────────────────────────────────────

async def mb_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a number as the default for this provider  (mb:def:{p}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    numbers  = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await query.answer("⚠️ Number not found.", show_alert=True)
        return
    if n.get("is_default"):
        await query.answer("Already the default.", show_alert=False)
        return

    for x in numbers:
        x["is_default"] = x.get("id") == nid
    _save(provider, numbers)
    await query.answer(f"✅ {n.get('number')} set as default.", show_alert=False)
    try:
        await query.edit_message_text(
            _detail_text(provider, n),
            reply_markup=_detail_keyboard(provider, n),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Set default provider ───────────────────────────────────────────────────

async def mb_set_default_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a provider as the global default  (mb:defprov:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    provider = query.data.split(":", 2)[2]
    if provider not in _ALL_PROVIDERS:
        await query.answer("⚠️ Unknown provider.", show_alert=True)
        return

    cfg.set("mb_default_provider", provider)
    await query.answer(f"✅ {_prov_label(provider)} set as default provider.", show_alert=False)
    numbers = _load(provider)
    try:
        await query.edit_message_text(
            _list_text(provider, numbers),
            reply_markup=_list_keyboard(provider, numbers),
            parse_mode="HTML",
        )
    except BadRequest:
        pass


# ── Copy number fallback ───────────────────────────────────────────────────

async def mb_copy_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for clients that don't support CopyTextButton  (mb:copy:{p}:{id})."""
    query  = update.callback_query
    parts  = query.data.split(":")
    provider, nid = parts[2], int(parts[3])
    numbers = _load(provider)
    n = _find(numbers, nid)
    number = (n or {}).get("number", "")
    if number:
        await query.answer(f"Number: {number}", show_alert=True)
    else:
        await query.answer("No number configured.", show_alert=True)


# ── Delete with confirmation ───────────────────────────────────────────────

async def mb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm before deleting  (mb:del:{p}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    numbers  = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await query.answer("⚠️ Number not found.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            f"🗑 <b>Delete Number</b>\n\n"
            f"Delete <code>{n.get('number', 'this number')}</code> from "
            f"{_prov_label(provider)}?\n\nThis cannot be undone.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"mb:delok:{provider}:{nid}"),
                InlineKeyboardButton("❌ Cancel",      callback_data=f"mb:view:{provider}:{nid}"),
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def mb_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute deletion  (mb:delok:{p}:{id})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    numbers  = _load(provider)
    before   = len(numbers)
    numbers  = [x for x in numbers if x.get("id") != nid]

    if len(numbers) < before:
        if not any(x.get("is_default") for x in numbers) and numbers:
            active = [x for x in numbers if x.get("is_active", True)]
            (active or numbers)[0]["is_default"] = True
        _save(provider, numbers)
        await query.answer("🗑 Number deleted.", show_alert=False)
    else:
        await query.answer("Already removed.", show_alert=False)

    try:
        await query.edit_message_text(
            _list_text(provider, numbers),
            reply_markup=_list_keyboard(provider, numbers),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ──────────────────────────────────────────────────────────────────────────
# Conversation: Add Number  (number → label)
# ──────────────────────────────────────────────────────────────────────────

async def mb_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: ask for the phone number  (mb:add:{p})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    provider = query.data.split(":", 2)[2]
    if provider not in _ALL_PROVIDERS:
        return ConversationHandler.END

    context.user_data["mb_add"] = {"provider": provider}
    try:
        await query.edit_message_text(
            f"{_prov_label(provider)} — <b>➕ Add Number</b>\n\n"
            "Step 1 of 2\n\n"
            "Send the <b>mobile number</b>:\n"
            "Example: <code>01712345678</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"mb:list:{provider}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return MB_ADD_NUMBER


async def mb_add_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive number, ask for optional label."""
    number   = (update.message.text or "").strip()
    add_data = context.user_data.get("mb_add", {})
    provider = add_data.get("provider", "")

    if not number:
        await update.message.reply_text("❌ Number cannot be empty. Send the mobile number:")
        return MB_ADD_NUMBER

    add_data["number"] = number[:30]
    context.user_data["mb_add"] = add_data
    await update.message.reply_text(
        f"{_prov_label(provider)} — <b>➕ Add Number</b>\n\n"
        "Step 2 of 2\n\n"
        f"Number: <code>{number}</code>\n\n"
        "Send an optional <b>label</b> for this number\n"
        "Examples: <code>Personal</code>, <code>Agent</code>, <code>Shop</code>\n"
        "(or send <code>skip</code> to leave blank):",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"mb:list:{provider}")
        ]]),
        parse_mode="HTML",
    )
    return MB_ADD_LABEL


async def mb_add_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive label, save the new number."""
    label_raw = (update.message.text or "").strip()
    add_data  = context.user_data.pop("mb_add", {})
    provider  = add_data.get("provider", "")
    number    = add_data.get("number", "")
    label     = "" if label_raw.lower() == "skip" else label_raw[:60]

    numbers  = _load(provider)
    is_first = len(numbers) == 0
    new_entry: dict = {
        "id":        _next_id(numbers),
        "number":    number,
        "label":     label,
        "is_active": True,
        "is_default": is_first,
    }
    numbers.append(new_entry)
    _save(provider, numbers)

    await update.message.reply_text(
        f"✅ <b>Number added!</b>\n\n" + _detail_text(provider, new_entry),
        reply_markup=_detail_keyboard(provider, new_entry),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def mb_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the add-number conversation."""
    context.user_data.pop("mb_add", None)
    if update.callback_query:
        await update.callback_query.answer()
        parts    = update.callback_query.data.split(":", 2)
        provider = parts[2] if len(parts) > 2 else ""
        if provider in _ALL_PROVIDERS:
            numbers = _load(provider)
            try:
                await update.callback_query.edit_message_text(
                    _list_text(provider, numbers),
                    reply_markup=_list_keyboard(provider, numbers),
                    parse_mode="HTML",
                )
            except BadRequest:
                pass
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────
# Conversation: Edit number or label  (mb:edit:{p}:{id}:{field})
# ──────────────────────────────────────────────────────────────────────────

async def mb_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: ask for new value  (mb:edit:{p}:{id}:{field})."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_payments"):
        return ConversationHandler.END

    parts    = query.data.split(":")
    provider = parts[2]
    nid      = int(parts[3])
    field    = parts[4] if len(parts) > 4 else "number"  # "number" | "label"

    numbers = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await query.answer("⚠️ Number not found.", show_alert=True)
        return ConversationHandler.END

    context.user_data["mb_edit"] = {"provider": provider, "id": nid, "field": field}
    field_lbl  = "mobile number" if field == "number" else "label"
    current    = n.get(field) or "(not set)"
    hint       = (
        "e.g. <code>01712345678</code>"
        if field == "number"
        else "e.g. <code>Personal</code>, <code>Agent</code> — or <code>clear</code> to remove"
    )
    try:
        await query.edit_message_text(
            f"{_prov_label(provider)} — <b>✏️ Edit {field_lbl.title()}</b>\n\n"
            f"Current: <code>{current}</code>\n\n"
            f"Send the new <b>{field_lbl}</b>\n{hint}\n\n/cancel to abort",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"mb:view:{provider}:{nid}")
            ]]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return MB_EDIT_VAL


async def mb_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new value, save, return to detail view."""
    edit     = context.user_data.pop("mb_edit", None)
    if not edit:
        return ConversationHandler.END

    raw      = (update.message.text or "").strip()
    provider = edit["provider"]
    nid      = edit["id"]
    field    = edit["field"]

    if not raw:
        await update.message.reply_text("❌ Cannot be empty. Try again:")
        context.user_data["mb_edit"] = edit
        return MB_EDIT_VAL

    numbers = _load(provider)
    n = _find(numbers, nid)
    if not n:
        await update.message.reply_text("❌ Number no longer exists.")
        return ConversationHandler.END

    if field == "number":
        if not raw:
            await update.message.reply_text("❌ Number cannot be empty. Try again:")
            context.user_data["mb_edit"] = edit
            return MB_EDIT_VAL
        n["number"] = raw[:30]
    elif field == "label":
        n["label"] = "" if raw.lower() == "clear" else raw[:60]

    _save(provider, numbers)
    await update.message.reply_text(
        f"✅ Updated!\n\n" + _detail_text(provider, n),
        reply_markup=_detail_keyboard(provider, n),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def mb_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel edit conversation and return to number detail."""
    edit = context.user_data.pop("mb_edit", None)
    if update.callback_query:
        await update.callback_query.answer()
        if edit:
            provider = edit["provider"]
            nid      = edit["id"]
            numbers  = _load(provider)
            n = _find(numbers, nid)
            if n:
                try:
                    await update.callback_query.edit_message_text(
                        _detail_text(provider, n),
                        reply_markup=_detail_keyboard(provider, n),
                        parse_mode="HTML",
                    )
                except BadRequest:
                    pass
    return ConversationHandler.END


async def mb_edit_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mb_edit", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── ConversationHandler factories ──────────────────────────────────────────

def build_mb_add_conv() -> ConversationHandler:
    """ConversationHandler for adding a new number to any provider."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(mb_add_start, pattern=r"^mb:add:[a-z]+$"),
        ],
        states={
            MB_ADD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, mb_add_number)],
            MB_ADD_LABEL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, mb_add_label)],
        },
        fallbacks=[
            CallbackQueryHandler(mb_add_cancel, pattern=r"^mb:list:[a-z]+$"),
            CommandHandler("cancel", mb_add_cancel),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


def build_mb_edit_conv() -> ConversationHandler:
    """ConversationHandler for editing a number or label."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(mb_edit_start, pattern=r"^mb:edit:[a-z]+:\d+:(number|label)$"),
        ],
        states={
            MB_EDIT_VAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, mb_edit_value),
                CallbackQueryHandler(mb_edit_cancel, pattern=r"^mb:view:[a-z]+:\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", mb_edit_cancel_cmd),
            CallbackQueryHandler(mb_edit_cancel, pattern=r"^mb:view:[a-z]+:\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )


# ── Registration helper ────────────────────────────────────────────────────

def register_handlers(app) -> None:
    """Register all Mobile Banking handlers with the Application."""
    # Conversations first (higher priority)
    app.add_handler(build_mb_add_conv())
    app.add_handler(build_mb_edit_conv())

    # Simple callbacks
    app.add_handler(CallbackQueryHandler(mb_menu,                 pattern=r"^mb:menu$"))
    app.add_handler(CallbackQueryHandler(mb_list,                 pattern=r"^mb:list:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(mb_view,                 pattern=r"^mb:view:[a-z]+:\d+$"))
    app.add_handler(CallbackQueryHandler(mb_toggle,               pattern=r"^mb:tog:[a-z]+:\d+$"))
    app.add_handler(CallbackQueryHandler(mb_set_default,          pattern=r"^mb:def:[a-z]+:\d+$"))
    app.add_handler(CallbackQueryHandler(mb_set_default_provider, pattern=r"^mb:defprov:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(mb_copy_fallback,        pattern=r"^mb:copy:[a-z]+:\d+$"))
    app.add_handler(CallbackQueryHandler(mb_delete_confirm,       pattern=r"^mb:del:[a-z]+:\d+$"))
    app.add_handler(CallbackQueryHandler(mb_delete_execute,       pattern=r"^mb:delok:[a-z]+:\d+$"))
