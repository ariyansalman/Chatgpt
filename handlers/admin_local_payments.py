"""
Admin Panel — Dynamic LOCAL Payment Provider Configuration.
═══════════════════════════════════════════════════════════

Every local payment provider shown on this screen (bKash, Nagad, Rocket,
Upay, SureCash, Tap, CellFin, … unlimited) is generated from the
``local_payment_providers`` table (services/local_payments.py). Nothing is
hardcoded, so a provider added here appears instantly in the customer
🇧🇩 LOCAL PAYMENT menu without any code change.

Strictly configuration + UI. This module never verifies a deposit, never
credits a wallet, never calls a gateway API and never introduces a new
user-facing callback: a provider routes through the EXISTING
``pay_<gateway_key>`` handler when bound to a code gateway, or the EXISTING
``pay_pm_<manual_method_id>`` manual flow when the admin created it here.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from services import local_payments as lp
from utils.permissions import has_permission
from utils.safe_conversation import safe_conversation

# Conversation states
(
    ALP_ADD_NAME,
    ALP_ADD_DISPLAY,
    ALP_ADD_EMOJI,
    ALP_ADD_WALLET,
    ALP_ADD_TYPE,
    ALP_ADD_HOLDER,
    ALP_ADD_INSTR,
    ALP_EDIT_VALUE,
) = range(8)

_DRAFT = "alp_draft"

# field key → (prompt, label, parser)
EDITABLE = {
    "name":    ("Send the new <b>provider name</b>.", "Provider Name", str),
    "display": ("Send the new <b>display name</b> (shown to customers).", "Display Name", str),
    "emoji":   ("Send the new <b>emoji / icon</b>.", "Emoji", str),
    "wallet":  ("Send the <b>wallet number</b> customers should send money to.", "Wallet Number", str),
    "holder":  ("Send the <b>account holder name</b>.", "Account Holder", str),
    "instr":   ("Send the <b>instructions</b> shown to customers.", "Instructions", str),
    "min":     ("Send the <b>minimum deposit</b> in USD.", "Minimum Deposit", float),
    "max":     ("Send the <b>maximum deposit</b> in USD (<code>0</code> = no limit).", "Maximum Deposit", float),
    "bonus":   ("Send the <b>bonus %</b> for this provider (e.g. <code>5</code>).", "Bonus %", float),
    "rate":    ("Send the <b>exchange rate</b> — local currency per 1 USD (e.g. <code>120</code>).", "Exchange Rate", float),
    "curr":    ("Send the <b>rate currency code</b> (e.g. <code>BDT</code>).", "Rate Currency", str),
    "order":   ("Send the <b>display order</b> number (lower = higher up).", "Display Order", int),
    "notes":   ("Send your <b>admin notes</b> (never shown to customers).", "Admin Notes", str),
}

_FIELD_COLUMN = {
    "name": "name", "display": "display_name", "emoji": "emoji",
    "wallet": "wallet_number", "holder": "account_holder",
    "instr": "instructions", "min": "min_deposit", "max": "max_deposit",
    "bonus": "bonus_percent", "rate": "exchange_rate", "curr": "rate_currency",
    "order": "display_order", "notes": "admin_notes",
}

_TOGGLE_COLUMN = {
    "enabled": "is_enabled",
    "visible": "is_visible",
    "maint": "maintenance_mode",
    "autorate": "auto_rate",
}


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _allowed(update: Update) -> bool:
    return has_permission(update.effective_user.id, "manage_payments")


async def _render(query, text, keyboard):
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def _ask(update, text, keyboard=None):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


def _money(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


# ══════════════════════════════════════════════════════════════════════════
# Screen 1 — provider list (fully generated from the database)
# ══════════════════════════════════════════════════════════════════════════

def build_menu_keyboard(providers):
    rows = []
    for p in providers:
        label = f"{p.status_icon} {p.emoji or '💳'} {p.display_name}"
        if p.badge:
            label += f" {p.badge}"
        rows.append([InlineKeyboardButton(label, callback_data=f"alp_view_{p.id}")])
    rows.append([InlineKeyboardButton("➕ ADD LOCAL PAYMENT", callback_data="alp_add")])
    rows.append([InlineKeyboardButton("👁 LIVE PREVIEW (CUSTOMER VIEW)",
                                      callback_data="alp_preview")])
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data="admin_gateways")])
    return InlineKeyboardMarkup(rows)


async def admin_local_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    lp.ensure_table()
    providers = lp.list_providers()

    if not providers:
        text = (
            "🇧🇩 <b>LOCAL PAYMENT PROVIDERS</b>\n\n"
            "No local payment providers configured yet.\n\n"
            "Tap <b>➕ ADD LOCAL PAYMENT</b> to create your first one "
            "(bKash, Nagad, Rocket, Upay, SureCash, Tap, CellFin — or any "
            "provider you like). Anything you add here appears automatically "
            "in the customer payment menu."
        )
    else:
        live = sum(1 for p in providers if p.live)
        text = (
            "🇧🇩 <b>LOCAL PAYMENT PROVIDERS</b>\n\n"
            f"Total: <b>{len(providers)}</b>   •   Live: <b>{live}</b>\n\n"
            "✅ live  •  🚫 disabled  •  🙈 hidden  •  🛠 maintenance  •  ⭐ default\n\n"
            "Tap a provider to configure it."
        )
    await _render(query, text, build_menu_keyboard(providers))


async def admin_local_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


@safe_conversation(cleanup_keys=(_DRAFT, "_alp_edit"))
async def admin_local_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(_DRAFT, None)
    context.user_data.pop("_alp_edit", None)
    await admin_local_menu(update, context)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# Screen 2 — single provider detail
# ══════════════════════════════════════════════════════════════════════════

def build_detail_keyboard(p):
    def tgl(flag, on, label):
        return InlineKeyboardButton(f"{'✅' if on else '🚫'} {label}",
                                    callback_data=f"alp_tgl_{flag}_{p.id}")

    return InlineKeyboardMarkup([
        [tgl("enabled", p.is_enabled, "ENABLED"),
         tgl("visible", p.is_visible, "SHOW")],
        [tgl("maint", p.maintenance_mode, "MAINTENANCE 🛠"),
         tgl("autorate", p.auto_rate, "AUTO RATE")],
        [InlineKeyboardButton(
            "⭐ DEFAULT PROVIDER" if p.is_default else "⭐ SET AS DEFAULT",
            callback_data=f"alp_default_{p.id}")],
        [InlineKeyboardButton("✏️ NAME", callback_data=f"alp_edit_name_{p.id}"),
         InlineKeyboardButton("✏️ DISPLAY", callback_data=f"alp_edit_display_{p.id}")],
        [InlineKeyboardButton("✏️ EMOJI", callback_data=f"alp_edit_emoji_{p.id}"),
         InlineKeyboardButton("✏️ WALLET NUMBER", callback_data=f"alp_edit_wallet_{p.id}")],
        [InlineKeyboardButton("🏷 ACCOUNT TYPE", callback_data=f"alp_type_{p.id}"),
         InlineKeyboardButton("✏️ HOLDER NAME", callback_data=f"alp_edit_holder_{p.id}")],
        [InlineKeyboardButton("✏️ MIN", callback_data=f"alp_edit_min_{p.id}"),
         InlineKeyboardButton("✏️ MAX", callback_data=f"alp_edit_max_{p.id}")],
        [InlineKeyboardButton("✏️ BONUS %", callback_data=f"alp_edit_bonus_{p.id}"),
         InlineKeyboardButton("✏️ RATE", callback_data=f"alp_edit_rate_{p.id}"),
         InlineKeyboardButton("✏️ CURRENCY", callback_data=f"alp_edit_curr_{p.id}")],
        [InlineKeyboardButton("✏️ INSTRUCTIONS", callback_data=f"alp_edit_instr_{p.id}")],
        [InlineKeyboardButton("🔼 MOVE UP", callback_data=f"alp_up_{p.id}"),
         InlineKeyboardButton("🔽 MOVE DOWN", callback_data=f"alp_dn_{p.id}"),
         InlineKeyboardButton("✏️ ORDER", callback_data=f"alp_edit_order_{p.id}")],
        [InlineKeyboardButton("📊 STATISTICS", callback_data=f"alp_stats_{p.id}"),
         InlineKeyboardButton("🧪 SELF-CHECK", callback_data=f"alp_test_{p.id}")],
        [InlineKeyboardButton("👁 LIVE PREVIEW (CUSTOMER VIEW)",
                              callback_data="alp_preview")],
        [InlineKeyboardButton("📝 ADMIN NOTES", callback_data=f"alp_edit_notes_{p.id}")],
        [InlineKeyboardButton("🗑 DELETE PROVIDER", callback_data=f"alp_del_{p.id}")],
        [InlineKeyboardButton("🔙 BACK", callback_data="alp_menu")],
    ])


def render_detail(p) -> str:
    route = (f"pay_{p.gateway_key}" if p.gateway_key
             else (f"pay_pm_{p.manual_method_id}" if p.manual_method_id else "—"))
    max_line = _money(p.max_deposit) if p.max_deposit else "no limit"
    rate = p.effective_rate
    rate_line = (f"{float(rate):.2f} {p.rate_currency or 'BDT'} / USD"
                 if rate else "—")
    if p.auto_rate:
        rate_line += "  <i>(auto)</i>"
    return (
        f"{p.emoji or '💳'} <b>{p.display_name}</b>\n"
        f"<i>Local Payment</i>\n\n"
        f"Status: {p.status_icon} "
        f"{'Live' if p.live else ('Maintenance' if p.maintenance_mode else 'Offline')}\n"
        f"Default: {'⭐ Yes' if p.is_default else 'No'}\n"
        f"Order: <b>{p.display_order}</b>\n\n"
        f"🏷 Name: {p.name}\n"
        f"📱 Wallet Number: <code>{p.wallet_number or '—'}</code>\n"
        f"🧾 Account Type: {p.account_type_label}\n"
        f"🙍 Account Holder: {p.account_holder or '—'}\n\n"
        f"💵 Min: {_money(p.min_deposit)}   •   Max: {max_line}\n"
        f"🎁 Bonus: {float(p.bonus_percent or 0):.2f}%\n"
        f"💱 Exchange Rate: {rate_line}\n\n"
        f"↪️ Routes through: <code>{route}</code>\n\n"
        f"📜 Instructions:\n{(p.instructions or '—')[:400]}\n\n"
        f"📝 Notes (admin only): {(p.admin_notes or '—')[:200]}"
    )


async def admin_local_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    override = context.user_data.pop("_alp_id", None)
    pid = int(override) if override else int(query.data.split("_")[-1])
    p = lp.get_provider(pid)
    if not p:
        await _render(query, "❌ Provider not found.", build_menu_keyboard(lp.list_providers()))
        return
    await _render(query, render_detail(p), build_detail_keyboard(p))


async def _back_to_detail(update, context, pid):
    context.user_data["_alp_id"] = str(pid)
    await admin_local_view(update, context)


# ══════════════════════════════════════════════════════════════════════════
# Toggles / default / ordering / account type / delete
# ══════════════════════════════════════════════════════════════════════════

async def admin_local_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")          # alp_tgl_<flag>_<id>
    flag, pid = parts[2], int(parts[3])
    lp.toggle_field(pid, _TOGGLE_COLUMN[flag])
    await _back_to_detail(update, context, pid)


async def admin_local_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pid = int(query.data.split("_")[-1])
    lp.set_default(pid)
    await _back_to_detail(update, context, pid)


async def admin_local_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")          # alp_up_<id> / alp_dn_<id>
    pid = int(parts[2])
    lp.move(pid, -1 if parts[1] == "up" else 1)
    await _back_to_detail(update, context, pid)


async def admin_local_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Personal / Agent / Merchant picker for an existing provider."""
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")
    if parts[1] == "typeset":              # alp_typeset_<idx>_<id>
        pid = int(parts[3])
        lp.update_fields(pid, account_type=lp.ACCOUNT_TYPES[int(parts[2])])
        await _back_to_detail(update, context, pid)
        return
    pid = int(parts[2])                    # alp_type_<id>
    rows = [[InlineKeyboardButton(lp.ACCOUNT_TYPE_LABEL[t],
                                  callback_data=f"alp_typeset_{i}_{pid}")]
            for i, t in enumerate(lp.ACCOUNT_TYPES)]
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data=f"alp_view_{pid}")])
    await _render(query, "🏷 <b>ACCOUNT TYPE</b>\n\nWhat kind of wallet is this?",
                  InlineKeyboardMarkup(rows))


async def admin_local_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pid = int(query.data.split("_")[-1])
    p = lp.get_provider(pid)
    if not p:
        return
    s = lp.stats(pid)
    text = (
        f"📊 <b>STATISTICS — {p.display_name}</b>\n\n"
        f"📥 Total Deposits: <b>{s['total']}</b>\n"
        f"💰 Total Volume: <b>{_money(s['volume'])}</b>\n"
        f"✅ Total Successful: <b>{s['success']}</b>\n"
        f"❌ Total Failed: <b>{s['failed']}</b>"
    )
    await _render(query, text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 BACK", callback_data=f"alp_view_{pid}")]]))


async def admin_local_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configuration self-check — shows exactly what a customer would see.
    It never creates a payment and never touches a wallet."""
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pid = int(query.data.split("_")[-1])
    p = lp.get_provider(pid)
    if not p:
        return

    issues = []
    if not p.wallet_number and not p.gateway_key:
        issues.append("• No wallet number set")
    if not p.instructions and not p.gateway_key:
        issues.append("• No instructions set")
    if not p.callback_key:
        issues.append("• Not routed to any existing payment flow")
    if not p.live:
        issues.append("• Not live (disabled / hidden / maintenance)")

    rate = p.effective_rate
    text = (
        f"🧪 <b>PREVIEW — {p.display_name}</b>\n\n"
        f"Customer button: <b>{p.emoji or '💳'} {p.display_name}</b>\n"
        f"Callback: <code>pay_{p.callback_key or '—'}</code>\n\n"
        f"📱 {p.account_type_label} number: <code>{p.wallet_number or '—'}</code>\n"
        f"🙍 Holder: {p.account_holder or '—'}\n"
        f"💵 Min {_money(p.min_deposit)} • Max "
        f"{_money(p.max_deposit) if p.max_deposit else 'no limit'}\n"
        f"🎁 Bonus {float(p.bonus_percent or 0):.2f}%\n"
        f"💱 Rate: {f'{float(rate):.2f} ' + (p.rate_currency or 'BDT') if rate else '—'}\n\n"
        + ("✅ Configuration looks good." if not issues
           else "⚠️ <b>Issues found:</b>\n" + "\n".join(issues))
    )
    await _render(query, text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 BACK", callback_data=f"alp_view_{pid}")]]))


async def admin_local_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pid = int(query.data.split("_")[-1])
    p = lp.get_provider(pid)
    if not p:
        return
    await _render(
        query,
        f"🗑 <b>DELETE {p.display_name}?</b>\n\n"
        "The provider disappears from the customer menu immediately.\n"
        "Existing deposits and payment history are never touched.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 YES, DELETE", callback_data=f"alp_delgo_{pid}")],
            [InlineKeyboardButton("🔙 CANCEL", callback_data=f"alp_view_{pid}")],
        ]))


async def admin_local_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pid = int(query.data.split("_")[-1])
    lp.delete_provider(pid)
    await query.answer("🗑 Provider deleted.", show_alert=False)
    await admin_local_menu(update, context)


# ══════════════════════════════════════════════════════════════════════════
# Single-field edit conversation
# ══════════════════════════════════════════════════════════════════════════

@safe_conversation(cleanup_keys=("_alp_edit",))
async def admin_local_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return ConversationHandler.END
    parts = query.data.split("_")          # alp_edit_<field>_<id>
    field, pid = parts[2], int(parts[3])
    context.user_data["_alp_edit"] = (field, pid)
    prompt, label, _ = EDITABLE[field]
    await _render(query, f"✏️ <b>{label}</b>\n\n{prompt}", InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 CANCEL", callback_data=f"alp_view_{pid}")]]))
    return ALP_EDIT_VALUE


@safe_conversation(cleanup_keys=("_alp_edit",))
async def admin_local_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field, pid = context.user_data.pop("_alp_edit", (None, None))
    if not field:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    _, label, parser = EDITABLE[field]
    try:
        value = parser(raw) if parser is not str else raw
    except ValueError:
        await update.message.reply_text(f"❌ Invalid value for {label}. Try again.")
        context.user_data["_alp_edit"] = (field, pid)
        return ALP_EDIT_VALUE

    if parser is str and raw == "-":
        value = None
    if field == "max" and not value:
        value = None
    if field == "curr" and value:
        value = str(value).upper()
    if field == "display" and value:
        value = str(value).upper()

    lp.update_fields(pid, **{_FIELD_COLUMN[field]: value})
    p = lp.get_provider(pid)
    await update.message.reply_text(
        f"✅ {label} updated.\n\n" + render_detail(p),
        reply_markup=build_detail_keyboard(p), parse_mode="HTML")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# ➕ ADD LOCAL PAYMENT wizard
# ══════════════════════════════════════════════════════════════════════════

def _skip_kb(cb: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ SKIP", callback_data=cb)],
        [InlineKeyboardButton("🔙 CANCEL", callback_data="alp_menu")],
    ])


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return ConversationHandler.END
    lp.ensure_table()
    context.user_data[_DRAFT] = {}
    rows = [[InlineKeyboardButton(f"{emoji} {display}",
                                  callback_data=f"alp_preset_{i}")]
            for i, (_key, _name, display, emoji) in enumerate(lp.PRESETS)]
    rows.append([InlineKeyboardButton("🔙 CANCEL", callback_data="alp_menu")])
    await _ask(update,
               "➕ <b>ADD LOCAL PAYMENT</b>  <i>(1/6)</i>\n\n"
               "Pick a provider preset, or just send the "
               "<b>provider name</b> to create any other one.\n"
               "Example: <code>bKash</code>",
               InlineKeyboardMarkup(rows))
    return ALP_ADD_NAME


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.callback_query and update.callback_query.data.startswith("alp_preset_"):
        idx = int(update.callback_query.data.split("_")[-1])
        key, name, display, emoji = lp.PRESETS[idx]
        d.update({"name": name, "display_name": display, "emoji": emoji,
                  "provider_key": lp.unique_key(key)})
    else:
        name = update.message.text.strip()
        d.update({"name": name, "provider_key": lp.unique_key(name)})

    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(2/6)</i>\n\n"
                       "Send the <b>display name</b> customers will see.\n"
                       "Example: <code>BKASH</code>",
               _skip_kb("alp_skip_display"))
    return ALP_ADD_DISPLAY


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_display(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.message:
        d["display_name"] = update.message.text.strip().upper()
    else:
        d.setdefault("display_name", d.get("name", "Provider").upper())
    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(3/6)</i>\n\n"
                       "Send an <b>emoji</b> for this provider.\n"
                       "Example: <code>🩷</code>",
               _skip_kb("alp_skip_emoji"))
    return ALP_ADD_EMOJI


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.message:
        d["emoji"] = update.message.text.strip()[:8]
    else:
        d.setdefault("emoji", "💳")
    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(4/6)</i>\n\n"
                       "Send the <b>wallet number</b> customers send money to.\n"
                       "Example: <code>01700000000</code>",
               _skip_kb("alp_skip_wallet"))
    return ALP_ADD_WALLET


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.message:
        d["wallet_number"] = update.message.text.strip()
    rows = [[InlineKeyboardButton(lp.ACCOUNT_TYPE_LABEL[t],
                                  callback_data=f"alp_newtype_{i}")]
            for i, t in enumerate(lp.ACCOUNT_TYPES)]
    rows.append([InlineKeyboardButton("🔙 CANCEL", callback_data="alp_menu")])
    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(5/6)</i>\n\n"
                       "Choose the <b>account type</b>.",
               InlineKeyboardMarkup(rows))
    return ALP_ADD_TYPE


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.callback_query:
        idx = int(update.callback_query.data.split("_")[-1])
        d["account_type"] = lp.ACCOUNT_TYPES[idx]
    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(6/6)</i>\n\n"
                       "Send the <b>account holder name</b>.",
               _skip_kb("alp_skip_holder"))
    return ALP_ADD_HOLDER


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_holder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.message:
        d["account_holder"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD LOCAL PAYMENT</b>  <i>(final)</i>\n\n"
                       "Send the <b>instructions</b> customers should follow.",
               _skip_kb("alp_skip_instr"))
    return ALP_ADD_INSTR


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_local_add_instr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.setdefault(_DRAFT, {})
    if update.message:
        d["instructions"] = update.message.text.strip()

    d.setdefault("display_order", len(lp.list_providers()))
    d.setdefault("is_enabled", True)
    d.setdefault("is_visible", True)
    pid = lp.create_provider(d)
    context.user_data.pop(_DRAFT, None)

    if not pid:
        await _ask(update, "❌ Could not create the provider. Please try again.",
                   InlineKeyboardMarkup([[InlineKeyboardButton(
                       "🔙 BACK", callback_data="alp_menu")]]))
        return ConversationHandler.END

    p = lp.get_provider(pid)
    text = ("✅ <b>Provider created.</b> It is already live in the customer "
            "🇧🇩 LOCAL PAYMENT menu.\n\n" + render_detail(p))
    if update.callback_query:
        await _render(update.callback_query, text, build_detail_keyboard(p))
    else:
        await update.message.reply_text(text, reply_markup=build_detail_keyboard(p),
                                        parse_mode="HTML")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# 👁 LIVE PREVIEW — the real customer 🇧🇩 LOCAL PAYMENT screen
# ══════════════════════════════════════════════════════════════════════════
#
# This screen renders the EXACT text + keyboard produced by the customer
# builder (services/payment_selection_ui.build_mobile_money_screen) using the
# current provider settings, so an admin sees precisely what a customer sees.
# It is read-only: every customer button is re-mapped to a preview-safe
# callback, so tapping one can never start a deposit, create a payment or
# touch a wallet. Nothing in the payment/verification/wallet flow is called.

_PV_MAP = "_alp_pv_map"


def _customer_local_screen():
    """Ask the customer-facing code for its own screen (text, keyboard)."""
    try:
        from handlers.payment_handlers import _build_mobile_money_screen
        return _build_mobile_money_screen()
    except Exception:  # noqa: BLE001
        from services import payment_selection_ui as psel
        return psel.build_mobile_money_screen(None)


def _preview_keyboard(keyboard, context) -> InlineKeyboardMarkup:
    """Clone the customer keyboard, keeping labels/layout byte-identical but
    swapping every callback for a preview-safe one."""
    mapping = {}
    rows = []
    for i, row in enumerate(keyboard.inline_keyboard if keyboard else []):
        new_row = []
        for j, btn in enumerate(row):
            data = getattr(btn, "callback_data", "") or ""
            if data == "topup_menu_back":
                continue  # customer BACK is replaced by the admin controls
            idx = f"{i}_{j}"
            mapping[idx] = {"label": btn.text, "callback": data}
            new_row.append(InlineKeyboardButton(btn.text, callback_data=f"alp_pvb_{idx}"))
        if new_row:
            rows.append(new_row)
    context.user_data[_PV_MAP] = mapping
    if not rows:
        rows.append([InlineKeyboardButton("— no provider visible to customers —",
                                          callback_data="alp_noop")])
    rows.append([InlineKeyboardButton("🔄 REFRESH", callback_data="alp_preview"),
                 InlineKeyboardButton("🔙 BACK", callback_data="alp_menu")])
    return InlineKeyboardMarkup(rows)


def _preview_footer(providers) -> str:
    hidden = [p for p in providers if not p.live]
    lines = [
        "",
        "───────────────",
        "👁 <i>Live preview — read-only. Buttons are disabled here; "
        "customers see exactly the screen above.</i>",
        f"<i>Visible to customers: <b>{len(providers) - len(hidden)}</b> "
        f"of {len(providers)}</i>",
    ]
    if hidden:
        detail = ", ".join(
            f"{p.display_name} ({p.status_icon})" for p in hidden[:8])
        lines.append(f"<i>Not shown: {detail}</i>")
    return "\n".join(lines)


async def admin_local_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        from database import run_db
        text, keyboard = await run_db(_customer_local_screen)
    except Exception:  # noqa: BLE001
        text, keyboard = _customer_local_screen()

    providers = lp.list_providers()
    await _render(query, text + _preview_footer(providers),
                  _preview_keyboard(keyboard, context))


async def admin_local_preview_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tapping a previewed customer button — shows what it would do, and
    opens the deposit-screen preview for that provider. Never runs it."""
    query = update.callback_query
    if not _allowed(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    idx = query.data[len("alp_pvb_"):]
    info = (context.user_data.get(_PV_MAP) or {}).get(idx)
    if not info:
        await query.answer("Preview expired — tap 🔄 REFRESH.", show_alert=True)
        return

    cb = info["callback"]
    key = cb[4:] if cb.startswith("pay_") else cb
    match = next((p for p in lp.list_providers()
                  if p.callback_key and p.callback_key == key), None)
    if match:
        await query.answer()
        await _render(query, _render_deposit_preview(match),
                      InlineKeyboardMarkup([
                          [InlineKeyboardButton("🔙 BACK TO PREVIEW",
                                                callback_data="alp_preview")],
                          [InlineKeyboardButton("⚙️ CONFIGURE",
                                                callback_data=f"alp_view_{match.id}")],
                      ]))
        return

    await query.answer(f"{info['label']}\nRoutes to: {cb}\n\n"
                       "Preview only — nothing was charged.", show_alert=True)


def _render_deposit_preview(p) -> str:
    """The next screen a customer reaches after tapping this provider,
    rendered from the current settings (read-only, no payment created)."""
    rate = p.effective_rate
    max_line = _money(p.max_deposit) if p.max_deposit else "no limit"
    bonus = float(p.bonus_percent or 0)
    lines = [
        f"{p.emoji or '💳'} <b>{p.display_name}</b>",
        "",
        f"📱 {p.account_type_label} Number: <code>{p.wallet_number or '—'}</code>",
    ]
    if p.account_holder:
        lines.append(f"🙍 Account Name: {p.account_holder}")
    lines += [
        "",
        f"💵 Min: {_money(p.min_deposit)}   •   Max: {max_line}",
    ]
    if bonus > 0:
        lines.append(f"🎁 Bonus: +{bonus:.2f}% on every deposit")
    if rate:
        lines.append(f"💱 Rate: 1 USD = {float(rate):.2f} {p.rate_currency or 'BDT'}"
                     + ("  (auto)" if p.auto_rate else ""))
    if p.instructions:
        lines += ["", "📜 <b>How to pay</b>", p.instructions[:600]]
    lines += [
        "",
        "───────────────",
        "👁 <i>Live preview of the customer deposit screen — read-only.</i>",
        f"<i>Real callback: <code>pay_{p.callback_key or '—'}</code></i>",
    ]
    return "\n".join(lines)
