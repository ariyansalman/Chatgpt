"""
Admin Panel — Dynamic Payment Network Configuration.
════════════════════════════════════════════════════

Every payment network / coin shown on this screen is generated from the
``payment_networks`` table (services/payment_networks.py). Nothing here is
hardcoded, so a network added from the Admin Panel appears instantly in the
Payment Settings screen, the user payment menu and the deposit flow without
any code change.

Strictly configuration + UI. This module never verifies a deposit, never
credits a wallet, never calls a gateway API and never introduces a new
user-facing callback: a network routes through the EXISTING
``pay_<gateway_key>`` handler when it is bound to a code gateway, or the
EXISTING ``pay_pm_<manual_method_id>`` manual flow when the admin created it
from scratch.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from services import payment_networks as pn
from utils.permissions import has_permission
from utils.safe_conversation import safe_conversation

# Conversation states
(
    APN_ADD_NAME,
    APN_ADD_SYMBOL,
    APN_ADD_DISPLAY,
    APN_ADD_CATEGORY,
    APN_ADD_EMOJI,
    APN_ADD_ADDRESS,
    APN_ADD_MEMO,
    APN_ADD_PROVIDER,
    APN_ADD_VERIFICATION,
    APN_ADD_MIN,
    APN_ADD_MAX,
    APN_ADD_BONUS,
    APN_ADD_CONFIRMATIONS,
    APN_ADD_ORDER,
    APN_ADD_ENABLED,
    APN_EDIT_VALUE,
) = range(16)

_DRAFT = "apn_draft"

# field key → (prompt, label, parser)
EDITABLE = {
    "name":    ("Send the new <b>network name</b>.", "Network Name", str),
    "display": ("Send the new <b>display name</b> (shown to users).", "Display Name", str),
    "symbol":  ("Send the new <b>symbol</b> (e.g. USDT, BTC).", "Symbol", str),
    "emoji":   ("Send the new <b>emoji / icon</b>.", "Emoji", str),
    "address": ("Send the new <b>deposit address</b>.", "Deposit Address", str),
    "memo":    ("Send the <b>memo / tag / destination tag</b> (or <code>-</code> to clear).", "Memo / Tag", str),
    "instr":   ("Send the <b>payment instructions</b> shown to users.", "Instructions", str),
    "min":     ("Send the <b>minimum deposit</b> in USD.", "Min Deposit", float),
    "max":     ("Send the <b>maximum deposit</b> in USD (<code>0</code> = no limit).", "Max Deposit", float),
    "bonus":   ("Send the <b>bonus %</b> for this network (e.g. <code>5</code>).", "Bonus %", float),
    "conf":    ("Send the required <b>confirmation count</b>.", "Confirmations", int),
    "order":   ("Send the <b>display order</b> number (lower = higher up).", "Display Order", int),
    "notes":   ("Send your <b>admin notes</b> (never shown to users).", "Admin Notes", str),
    "provider": ("Send the <b>API provider</b> name (or <code>-</code> to clear).", "API Provider", str),
}

_FIELD_COLUMN = {
    "name": "name", "display": "display_name", "symbol": "symbol",
    "emoji": "emoji", "address": "address", "memo": "memo",
    "instr": "instructions", "min": "min_deposit", "max": "max_deposit",
    "bonus": "bonus_percent", "conf": "confirmations", "order": "display_order",
    "notes": "admin_notes", "provider": "api_provider",
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


def _money(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


# ══════════════════════════════════════════════════════════════════════════
# Screen 1 — network list (fully generated from the database)
# ══════════════════════════════════════════════════════════════════════════

def build_menu_keyboard(networks):
    rows = []
    current_category = None
    for n in networks:
        if n.category != current_category:
            current_category = n.category
            rows.append([InlineKeyboardButton(
                f"— {pn.CATEGORY_EMOJI.get(n.category, '💳')} {n.category} —",
                callback_data="apn_noop",
            )])
        label = f"{n.status_icon} {n.emoji or '💳'} {n.display_name}"
        if n.badge:
            label += f" {n.badge}"
        rows.append([InlineKeyboardButton(label, callback_data=f"apn_view_{n.id}")])
    rows.append([InlineKeyboardButton("➕ ADD PAYMENT NETWORK", callback_data="apn_add")])
    rows.append([InlineKeyboardButton("🔄 IMPORT CODE GATEWAYS", callback_data="apn_import")])
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data="admin_gateways")])
    return InlineKeyboardMarkup(rows)


async def admin_networks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    pn.ensure_table()
    networks = pn.list_networks()

    if not networks:
        text = (
            "💳 <b>PAYMENT NETWORKS</b>\n\n"
            "No payment networks configured yet.\n\n"
            "Tap <b>➕ ADD PAYMENT NETWORK</b> to create one, or "
            "<b>🔄 IMPORT CODE GATEWAYS</b> to pull every gateway that already "
            "exists in the bot into this panel so you can manage it here."
        )
    else:
        live = sum(1 for n in networks if n.live)
        text = (
            "💳 <b>PAYMENT NETWORKS</b>\n\n"
            f"Total: <b>{len(networks)}</b>   •   Live: <b>{live}</b>\n\n"
            "✅ live  •  🚫 disabled  •  🙈 hidden  •  🛠 maintenance\n"
            "⭐ featured  •  👍 recommended\n\n"
            "Tap a network to configure it."
        )
    await _render(query, text, build_menu_keyboard(networks))


async def admin_networks_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ══════════════════════════════════════════════════════════════════════════
# Screen 2 — single network detail
# ══════════════════════════════════════════════════════════════════════════

def build_detail_keyboard(n):
    def tgl(flag, on, label):
        return InlineKeyboardButton(f"{'✅' if on else '🚫'} {label}",
                                    callback_data=f"apn_tgl_{flag}_{n.id}")

    return InlineKeyboardMarkup([
        [tgl("enabled", n.is_enabled, "ENABLED"),
         tgl("visible", n.is_visible, "VISIBLE")],
        [tgl("featured", n.is_featured, "FEATURED ⭐"),
         tgl("recommended", n.is_recommended, "RECOMMENDED 👍")],
        [tgl("maint", n.maintenance_mode, "MAINTENANCE 🛠")],
        [tgl("api", n.api_verification, "API VERIFY"),
         tgl("manual", n.manual_verification, "MANUAL VERIFY")],
        [InlineKeyboardButton("✏️ NAME", callback_data=f"apn_edit_name_{n.id}"),
         InlineKeyboardButton("✏️ DISPLAY", callback_data=f"apn_edit_display_{n.id}")],
        [InlineKeyboardButton("✏️ SYMBOL", callback_data=f"apn_edit_symbol_{n.id}"),
         InlineKeyboardButton("✏️ EMOJI", callback_data=f"apn_edit_emoji_{n.id}")],
        [InlineKeyboardButton("✏️ ADDRESS", callback_data=f"apn_edit_address_{n.id}"),
         InlineKeyboardButton("✏️ MEMO / TAG", callback_data=f"apn_edit_memo_{n.id}")],
        [InlineKeyboardButton("✏️ MIN", callback_data=f"apn_edit_min_{n.id}"),
         InlineKeyboardButton("✏️ MAX", callback_data=f"apn_edit_max_{n.id}")],
        [InlineKeyboardButton("✏️ BONUS %", callback_data=f"apn_edit_bonus_{n.id}"),
         InlineKeyboardButton("✏️ CONFIRMATIONS", callback_data=f"apn_edit_conf_{n.id}")],
        [InlineKeyboardButton("✏️ INSTRUCTIONS", callback_data=f"apn_edit_instr_{n.id}")],
        [InlineKeyboardButton("✏️ API PROVIDER", callback_data=f"apn_edit_provider_{n.id}"),
         InlineKeyboardButton("📁 CATEGORY", callback_data=f"apn_cat_{n.id}")],
        [InlineKeyboardButton("🔼 MOVE UP", callback_data=f"apn_up_{n.id}"),
         InlineKeyboardButton("🔽 MOVE DOWN", callback_data=f"apn_dn_{n.id}"),
         InlineKeyboardButton("✏️ ORDER", callback_data=f"apn_edit_order_{n.id}")],
        [InlineKeyboardButton("📊 STATISTICS", callback_data=f"apn_stats_{n.id}"),
         InlineKeyboardButton("🧪 TEST PAYMENT", callback_data=f"apn_test_{n.id}")],
        [InlineKeyboardButton("📝 ADMIN NOTES", callback_data=f"apn_edit_notes_{n.id}")],
        [InlineKeyboardButton("🗑 REMOVE NETWORK", callback_data=f"apn_del_{n.id}")],
        [InlineKeyboardButton("🔙 BACK", callback_data="apn_menu")],
    ])


def render_detail(n) -> str:
    route = (f"pay_{n.gateway_key}" if n.gateway_key
             else (f"pay_pm_{n.manual_method_id}" if n.manual_method_id else "—"))
    max_line = _money(n.max_deposit) if n.max_deposit else "no limit"
    badges = n.badge or "—"
    return (
        f"{n.emoji or '💳'} <b>{n.display_name}</b>\n"
        f"<i>{n.category}</i>\n\n"
        f"Status: {n.status_icon} "
        f"{'Live' if n.live else ('Maintenance' if n.maintenance_mode else 'Offline')}\n"
        f"Badges: {badges}\n"
        f"Order: <b>{n.display_order}</b>\n\n"
        f"🏷 Name: {n.name}\n"
        f"💠 Symbol: {n.symbol or '—'}\n"
        f"📥 Address: <code>{n.address or '—'}</code>\n"
        f"🏷 Memo / Tag: <code>{n.memo or '—'}</code>\n\n"
        f"💵 Min: {_money(n.min_deposit)}   •   Max: {max_line}\n"
        f"🎁 Bonus: {float(n.bonus_percent or 0):.2f}%\n"
        f"🔗 Confirmations: {n.confirmations}\n\n"
        f"🔌 API Provider: {n.api_provider or '—'}\n"
        f"⚙️ Verification: "
        f"{'API ✅' if n.api_verification else 'API 🚫'} / "
        f"{'Manual ✅' if n.manual_verification else 'Manual 🚫'}\n"
        f"↪️ Routes through: <code>{route}</code>\n\n"
        f"📜 Instructions:\n{(n.instructions or '—')[:400]}\n\n"
        f"📝 Notes (admin only): {(n.admin_notes or '—')[:200]}"
    )


async def admin_network_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    override = context.user_data.pop("_apn_id", None)
    nid = int(override) if override else int(query.data.split("_")[-1])
    n = pn.get_network(nid)
    if not n:
        await _render(query, "❌ Network not found.", build_menu_keyboard(pn.list_networks()))
        return
    await _render(query, render_detail(n), build_detail_keyboard(n))


async def _back_to_detail(update, context, nid):
    context.user_data["_apn_id"] = str(nid)
    await admin_network_view(update, context)


# ══════════════════════════════════════════════════════════════════════════
# Toggles / ordering / delete
# ══════════════════════════════════════════════════════════════════════════

_TOGGLE_COLUMN = {
    "enabled": "is_enabled",
    "visible": "is_visible",
    "featured": "is_featured",
    "recommended": "is_recommended",
    "maint": "maintenance_mode",
    "api": "api_verification",
    "manual": "manual_verification",
}


async def admin_network_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")          # apn_tgl_<flag>_<id>
    flag, nid = parts[2], int(parts[3])
    pn.toggle_field(nid, _TOGGLE_COLUMN[flag])
    await _back_to_detail(update, context, nid)


async def admin_network_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")          # apn_up_<id> / apn_dn_<id>
    nid = int(parts[2])
    pn.move(nid, -1 if parts[1] == "up" else 1)
    await _back_to_detail(update, context, nid)


async def admin_network_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a category for an existing network."""
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    parts = query.data.split("_")
    if parts[1] == "catset":              # apn_catset_<idx>_<id>
        nid = int(parts[3])
        pn.update_fields(nid, category=pn.CATEGORIES[int(parts[2])])
        await _back_to_detail(update, context, nid)
        return
    nid = int(parts[2])                   # apn_cat_<id>
    rows = [[InlineKeyboardButton(f"{pn.CATEGORY_EMOJI[c]} {c}",
                                  callback_data=f"apn_catset_{i}_{nid}")]
            for i, c in enumerate(pn.CATEGORIES)]
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data=f"apn_view_{nid}")])
    await _render(query, "📁 <b>SELECT CATEGORY</b>\n\nWhere should this network appear?",
                  InlineKeyboardMarkup(rows))


async def admin_network_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    nid = int(query.data.split("_")[-1])
    n = pn.get_network(nid)
    if not n:
        return
    s = pn.stats(nid)
    text = (
        f"📊 <b>STATISTICS — {n.display_name}</b>\n\n"
        f"📥 Total Deposits: <b>{s['total']}</b>\n"
        f"💰 Total Volume: <b>{_money(s['volume'])}</b>\n"
        f"✅ Total Successful: <b>{s['success']}</b>\n"
        f"❌ Total Failed: <b>{s['failed']}</b>"
    )
    await _render(query, text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 BACK", callback_data=f"apn_view_{nid}")]]))


async def admin_network_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configuration self-check — renders exactly what a user would see.
    It never creates a payment or touches a wallet."""
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    nid = int(query.data.split("_")[-1])
    n = pn.get_network(nid)
    if not n:
        return

    issues = []
    if not n.address and not n.gateway_key:
        issues.append("• No deposit address set")
    if not n.instructions and not n.gateway_key:
        issues.append("• No payment instructions set")
    if not n.callback_key:
        issues.append("• Not routed to any payment handler")
    if n.max_deposit and n.min_deposit and n.max_deposit < n.min_deposit:
        issues.append("• Max deposit is lower than min deposit")
    if n.api_verification and not n.api_provider:
        issues.append("• API verification is ON but no provider is set")
    if not n.api_verification and not n.manual_verification:
        issues.append("• Both API and manual verification are OFF")

    verdict = "✅ <b>Ready</b> — this network is correctly configured." if not issues \
        else "⚠️ <b>Issues found</b>\n" + "\n".join(issues)

    preview = (
        f"{n.emoji or '💳'} <b>{n.display_name}</b>\n"
        f"Address: <code>{n.address or '—'}</code>\n"
        f"Memo / Tag: <code>{n.memo or '—'}</code>\n"
        f"Min {_money(n.min_deposit)} • Max "
        f"{_money(n.max_deposit) if n.max_deposit else 'no limit'} • "
        f"{n.confirmations} confirmations"
    )
    text = (f"🧪 <b>TEST PAYMENT — {n.display_name}</b>\n\n{verdict}\n\n"
            f"<b>User preview</b>\n{preview}")
    await _render(query, text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 BACK", callback_data=f"apn_view_{nid}")]]))


async def admin_network_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    nid = int(query.data.split("_")[-1])
    n = pn.get_network(nid)
    if not n:
        return
    await _render(
        query,
        f"🗑 <b>REMOVE NETWORK</b>\n\nRemove <b>{n.display_name}</b> from the "
        f"payment menu?\n\nExisting deposits and payment history are kept.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES, REMOVE", callback_data=f"apn_delgo_{nid}")],
            [InlineKeyboardButton("🔙 CANCEL", callback_data=f"apn_view_{nid}")],
        ]),
    )


async def admin_network_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pn.delete_network(int(query.data.split("_")[-1]))
    await admin_networks_menu(update, context)


async def admin_networks_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a managed row for every gateway that already exists in code so
    the panel lists them too. Idempotent, and it changes no gateway logic."""
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    pn.ensure_table()

    existing = {n.gateway_key for n in pn.list_networks() if n.gateway_key}
    created = 0
    try:
        from services.payment_gateway_registry import registry
        descriptors = registry.all()
    except Exception:  # noqa: BLE001
        descriptors = []

    order = len(pn.list_networks())
    for d in descriptors:
        if d.gateway_id in existing:
            continue
        name = d.display_name
        upper = f"{d.gateway_id} {name}".lower()
        if "binance" in upper:
            category = "BINANCE PAY"
        elif "bybit" in upper:
            category = "BYBIT PAY"
        elif any(h in upper for h in ("bkash", "nagad", "rocket", "upay", "zinipay")):
            category = "LOCAL PAYMENT"
        elif any(h in upper for h in ("usdt", "trc20", "bep20", "erc20")):
            category = "USDT NETWORKS"
        else:
            category = "OTHER COINS"
        if pn.create_network({
            "network_key": pn.unique_key(d.gateway_id),
            "name": name,
            "display_name": name.upper(),
            "category": category,
            "emoji": pn.CATEGORY_EMOJI.get(category, "💳"),
            "gateway_key": d.gateway_id,
            "verification_type": "api" if d.supports_auto_verification else "manual",
            "display_order": order,
            "is_enabled": True,
        }):
            created += 1
            order += 1

    await query.answer(f"✅ Imported {created} gateway(s).", show_alert=True)
    await admin_networks_menu(update, context)


# ══════════════════════════════════════════════════════════════════════════
# Single-field edit conversation
# ══════════════════════════════════════════════════════════════════════════

@safe_conversation(cleanup_keys=("_apn_edit",))
async def admin_network_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return ConversationHandler.END
    parts = query.data.split("_")          # apn_edit_<field>_<id>
    field, nid = parts[2], int(parts[3])
    context.user_data["_apn_edit"] = (field, nid)
    prompt, label, _ = EDITABLE[field]
    await _render(query, f"✏️ <b>{label}</b>\n\n{prompt}", InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 CANCEL", callback_data=f"apn_view_{nid}")]]))
    return APN_EDIT_VALUE


@safe_conversation(cleanup_keys=("_apn_edit",))
async def admin_network_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field, nid = context.user_data.pop("_apn_edit", (None, None))
    if not field:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    _, label, parser = EDITABLE[field]
    try:
        value = parser(raw) if parser is not str else raw
    except ValueError:
        await update.message.reply_text(f"❌ Invalid value for {label}. Try again.")
        context.user_data["_apn_edit"] = (field, nid)
        return APN_EDIT_VALUE

    if parser is str and raw == "-":
        value = None
    if field == "max" and not value:
        value = None

    pn.update_fields(nid, **{_FIELD_COLUMN[field]: value})
    n = pn.get_network(nid)
    await update.message.reply_text(
        f"✅ {label} updated.\n\n" + render_detail(n),
        reply_markup=build_detail_keyboard(n), parse_mode="HTML")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# ➕ ADD PAYMENT NETWORK wizard
# ══════════════════════════════════════════════════════════════════════════

def _skip_kb(cb: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ SKIP", callback_data=cb)],
        [InlineKeyboardButton("🔙 CANCEL", callback_data="apn_menu")],
    ])


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


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return ConversationHandler.END
    pn.ensure_table()
    context.user_data[_DRAFT] = {}
    await _ask(update,
               "➕ <b>ADD PAYMENT NETWORK</b>  <i>(1/15)</i>\n\n"
               "Send the <b>network name</b>.\nExample: <code>Tron</code>",
               InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CANCEL", callback_data="apn_menu")]]))
    return APN_ADD_NAME


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["name"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(2/15)</i>\n\n"
                       "Send the <b>symbol</b>.\nExample: <code>USDT</code>",
               _skip_kb("apn_skip_symbol"))
    return APN_ADD_SYMBOL


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        context.user_data[_DRAFT]["symbol"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(3/15)</i>\n\n"
                       "Send the <b>display name</b> shown to users.\n"
                       "Example: <code>USDT (TRC20)</code>",
               _skip_kb("apn_skip_display"))
    return APN_ADD_DISPLAY


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_display(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data[_DRAFT]
    if update.message:
        d["display_name"] = update.message.text.strip()
    else:
        d["display_name"] = d.get("name", "Network").upper()
    rows = [[InlineKeyboardButton(f"{pn.CATEGORY_EMOJI[c]} {c}",
                                  callback_data=f"apn_addcat_{i}")]
            for i, c in enumerate(pn.CATEGORIES)]
    rows.append([InlineKeyboardButton("🔙 CANCEL", callback_data="apn_menu")])
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(4/15)</i>\n\n"
                       "Choose the <b>category</b>.", InlineKeyboardMarkup(rows))
    return APN_ADD_CATEGORY


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = int(update.callback_query.data.split("_")[-1])
    category = pn.CATEGORIES[idx]
    context.user_data[_DRAFT]["category"] = category
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(5/15)</i>\n\n"
                       "Send the <b>emoji / icon</b> for this network.",
               _skip_kb("apn_skip_emoji"))
    return APN_ADD_EMOJI


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data[_DRAFT]
    if update.message:
        d["emoji"] = update.message.text.strip()[:8]
    else:
        d["emoji"] = pn.CATEGORY_EMOJI.get(d.get("category", ""), "💳")
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(6/15)</i>\n\n"
                       "Send the <b>deposit address</b>.", _skip_kb("apn_skip_address"))
    return APN_ADD_ADDRESS


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        context.user_data[_DRAFT]["address"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(7/15)</i>\n\n"
                       "Send the <b>memo / tag / destination tag</b> (optional).",
               _skip_kb("apn_skip_memo"))
    return APN_ADD_MEMO


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        context.user_data[_DRAFT]["memo"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(8/15)</i>\n\n"
                       "Send the <b>API provider</b> name (optional).",
               _skip_kb("apn_skip_provider"))
    return APN_ADD_PROVIDER


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        context.user_data[_DRAFT]["api_provider"] = update.message.text.strip()
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(9/15)</i>\n\n"
                       "Choose the <b>verification type</b>.",
               InlineKeyboardMarkup([
                   [InlineKeyboardButton("🔌 API", callback_data="apn_addver_api"),
                    InlineKeyboardButton("🙋 MANUAL", callback_data="apn_addver_manual")],
                   [InlineKeyboardButton("🔙 CANCEL", callback_data="apn_menu")],
               ]))
    return APN_ADD_VERIFICATION


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["verification_type"] = update.callback_query.data.split("_")[-1]
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(10/15)</i>\n\n"
                       "Send the <b>minimum deposit</b> in USD.", _skip_kb("apn_skip_min"))
    return APN_ADD_MIN


def _num(update, default=None, cast=float):
    if not update.message:
        return default
    try:
        return cast(update.message.text.strip())
    except ValueError:
        return default


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["min_deposit"] = _num(update, 1.0)
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(11/15)</i>\n\n"
                       "Send the <b>maximum deposit</b> in USD "
                       "(<code>0</code> = no limit).", _skip_kb("apn_skip_max"))
    return APN_ADD_MAX


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = _num(update, 0.0)
    context.user_data[_DRAFT]["max_deposit"] = value or None
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(12/15)</i>\n\n"
                       "Send the <b>bonus %</b> for deposits on this network.",
               _skip_kb("apn_skip_bonus"))
    return APN_ADD_BONUS


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["bonus_percent"] = _num(update, 0.0)
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(13/15)</i>\n\n"
                       "Send the required <b>confirmation count</b>.",
               _skip_kb("apn_skip_conf"))
    return APN_ADD_CONFIRMATIONS


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_confirmations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["confirmations"] = _num(update, 1, int) or 1
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(14/15)</i>\n\n"
                       "Send the <b>display order</b> (lower = higher in the list).",
               _skip_kb("apn_skip_order"))
    return APN_ADD_ORDER


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_DRAFT]["display_order"] = _num(update, len(pn.list_networks()), int) or 0
    await _ask(update, "➕ <b>ADD PAYMENT NETWORK</b>  <i>(15/15)</i>\n\n"
                       "Should this network be <b>enabled</b> right away?",
               InlineKeyboardMarkup([
                   [InlineKeyboardButton("✅ ENABLED", callback_data="apn_adden_1"),
                    InlineKeyboardButton("🚫 DISABLED", callback_data="apn_adden_0")],
                   [InlineKeyboardButton("🔙 CANCEL", callback_data="apn_menu")],
               ]))
    return APN_ADD_ENABLED


@safe_conversation(cleanup_keys=(_DRAFT,))
async def admin_network_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = context.user_data.pop(_DRAFT, {})
    draft["is_enabled"] = query.data.endswith("1")
    draft["network_key"] = pn.unique_key(draft.get("name", "network"))
    nid = pn.create_network(draft)
    if not nid:
        await _render(query, "❌ Could not create the network. Please try again.",
                      build_menu_keyboard(pn.list_networks()))
        return ConversationHandler.END
    n = pn.get_network(nid)
    await _render(query, "✅ <b>NETWORK CREATED</b>\n\n" + render_detail(n),
                  build_detail_keyboard(n))
    return ConversationHandler.END


@safe_conversation(cleanup_keys=(_DRAFT, "_apn_edit"))
async def admin_network_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(_DRAFT, None)
    context.user_data.pop("_apn_edit", None)
    await admin_networks_menu(update, context)
    return ConversationHandler.END
