"""Admin panel: unified Bot Configuration menu — organised, sectioned, paginated, searchable.

Navigation tree:
  admin_bot_config
    → cfg_search                  (search entry — conversation)
    → cfg_sec_<section>           (section root — categories list)
    → cfg_cat_<cat>__p<page>      (paginated settings list)
    → cfg_view_<key>              (single-setting detail)
    → cfg_toggle_<key>            (bool flip)
    → cfg_reset_<key>             (reset to default)
    → cfg_edit_<key>              (start text-edit conversation)
    → cfg_srp__p<page>            (search results page — query in user_data)

No existing callback_data keys outside this file are touched.
"""

from __future__ import annotations

import logging
import math
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from utils.bot_config import cfg, CATEGORIES, get_meta, list_by_category, DEFAULTS
from utils.safe_conversation import safe_conversation
from utils.permissions import has_permission

logger = logging.getLogger(__name__)

# ── Conversation states ─────────────────────────────────────────────────────
EDIT_VALUE   = 1
SEARCH_QUERY = 2

# ── Items per page ──────────────────────────────────────────────────────────
_PAGE_SIZE        = 8
_SEARCH_PAGE_SIZE = 8

# ── Section definitions ─────────────────────────────────────────────────────
_SECTIONS: list[tuple[str, str, str, list[str]]] = [
    ("payments",   "💳", "Payments & Billing",   [
        "payments", "gateways", "wallets", "exchange_rates", "invoicing",
    ]),
    ("products",   "📦", "Products & Inventory", [
        "products", "inventory", "catalog",
    ]),
    ("operations", "🔧", "Operations",           [
        "ops", "monitoring", "notifications", "home_dashboard",
    ]),
    ("customers",  "👥", "Customers & Loyalty",  [
        "crm", "vip", "referral_advanced", "subscriptions",
    ]),
    ("marketing",  "📢", "Marketing",            [
        "broadcast", "marketing", "promotions",
    ]),
    ("security",   "🛡", "Security",             [
        "security", "antispam", "api_manager",
    ]),
    ("features",   "⚙️", "Features",             [
        "features", "account_features",
    ]),
    ("system",     "🛠", "System",               [
        "system", "backups", "diagnostics", "admin", "admin_ui",
    ]),
]

# ── Fast-lookup helpers ─────────────────────────────────────────────────────
_SEC_BY_ID  = {s[0]: s for s in _SECTIONS}
_CAT_TO_SEC: dict[str, str] = {}
for _s in _SECTIONS:
    for _c in _s[3]:
        _CAT_TO_SEC[_c] = _s[0]

_CAT_LABELS: dict[str, str] = dict(CATEGORIES)

_CAT_COUNT: dict[str, int] = {}
for _d in DEFAULTS:
    _CAT_COUNT[_d[3]] = _CAT_COUNT.get(_d[3], 0) + 1

# Pre-build searchable index: list of (key, vtype, cat, label, desc)
_SEARCH_INDEX: list[tuple[str, str, str, str, str]] = [
    (d[0], d[1], d[3], d[4], d[5]) for d in DEFAULTS
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_value(vtype: str, raw: str) -> str:
    if vtype == "bool":
        return "✅ ON" if str(raw).lower() in ("1", "true", "yes", "on") else "🔴 OFF"
    if not raw:
        return "—"
    if len(raw) > 35:
        return raw[:35].replace("\n", " ") + "…"
    return raw


def _setting_breadcrumb(cat: str) -> str:
    sid = _CAT_TO_SEC.get(cat, "")
    sec = _SEC_BY_ID.get(sid)
    cat_label = _CAT_LABELS.get(cat, cat)
    return f"{sec[1]} {sec[2]}  ›  {cat_label}" if sec else cat_label


async def _safe_edit(query, text: str, markup: InlineKeyboardMarkup, **kw):
    try:
        await query.edit_message_text(text, reply_markup=markup, **kw)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ═══════════════════════════════════════════════════════════════════════════
# 1.  ROOT MENU  —  🔍 Search on top, then 8 section tiles
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    # ── Search bar at the top ──
    kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "🔍  Search settings…",
            callback_data="cfg_search",
        )],
    ]

    # ── 2-column section grid ──
    row: list[InlineKeyboardButton] = []
    for sid, emoji, label, cats in _SECTIONS:
        total = sum(_CAT_COUNT.get(c, 0) for c in cats)
        btn = InlineKeyboardButton(
            f"{emoji} {label}  ({total})",
            callback_data=f"cfg_sec_{sid}",
        )
        row.append(btn)
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_settings")])

    total_all = sum(_CAT_COUNT.values())
    text = (
        "🛠 <b>Bot Configuration</b>\n\n"
        f"<b>{total_all} settings</b>  ·  "
        f"<b>{len(_CAT_LABELS)} categories</b>  ·  "
        f"<b>{len(_SECTIONS)} sections</b>\n\n"
        "🔍 Use Search to find any setting instantly,\n"
        "or browse by section below."
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 2.  SEARCH  —  conversation (entry → query text → results page)
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: show search prompt."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    # Clear any stale search state
    context.user_data.pop("cfg_search_results", None)
    context.user_data.pop("cfg_search_term",    None)

    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_bot_config")]]
    await _safe_edit(
        query,
        "🔍 <b>Search Settings</b>\n\n"
        "Type a keyword — e.g. <code>payment</code>, <code>broadcast</code>, "
        "<code>timeout</code>, <code>vip</code>.\n\n"
        "Searches setting names, keys, and descriptions.",
        InlineKeyboardMarkup(kb),
        parse_mode="HTML",
    )
    return SEARCH_QUERY


async def admin_config_search_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the user's search query and show page 1 of results."""
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    term = (update.message.text or "").strip().lower()
    if not term:
        await update.message.reply_text("❌ Empty query. Try again or tap Cancel.")
        return SEARCH_QUERY

    # Search across key, label, description (case-insensitive)
    hits: list[tuple[str, str, str, str, str]] = []
    for key, vtype, cat, label, desc in _SEARCH_INDEX:
        if (term in key.lower()
                or term in label.lower()
                or term in desc.lower()):
            hits.append((key, vtype, cat, label, desc))

    # Store results and term for pagination
    context.user_data["cfg_search_results"] = hits
    context.user_data["cfg_search_term"]    = term

    await _send_search_results(update, context, hits, term, page=1, via_message=True)
    return ConversationHandler.END


async def admin_config_search_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paginate stored search results (callback cfg_srp__p<N>)."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    page = int(query.data.split("__p")[1])
    hits = context.user_data.get("cfg_search_results", [])
    term = context.user_data.get("cfg_search_term", "")
    await _send_search_results(update, context, hits, term, page=page, via_message=False)


async def _send_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    hits: list,
    term: str,
    page: int,
    via_message: bool,
):
    total       = len(hits)
    total_pages = max(1, math.ceil(total / _SEARCH_PAGE_SIZE))
    page        = max(1, min(page, total_pages))
    page_hits   = hits[(page - 1) * _SEARCH_PAGE_SIZE : page * _SEARCH_PAGE_SIZE]

    kb: list[list[InlineKeyboardButton]] = []

    if not hits:
        kb.append([InlineKeyboardButton("🔍 Search again", callback_data="cfg_search")])
        kb.append([InlineKeyboardButton("🏠 Sections",     callback_data="admin_bot_config")])
        text = f"🔍 No results for <b>{term}</b>.\n\nTry a shorter or different keyword."
    else:
        for key, vtype, cat, label, desc in page_hits:
            bc      = _setting_breadcrumb(cat)
            preview = _fmt_value(vtype, cfg.get_str(key, ""))
            kb.append([InlineKeyboardButton(
                f"{label}  —  {preview}",
                callback_data=f"cfg_view_{key}",
            )])

        # Pagination nav
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"cfg_srp__p{page-1}"))
        if total_pages > 1:
            nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data=f"cfg_srp__p1"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"cfg_srp__p{page+1}"))
        if nav:
            kb.append(nav)

        kb.append([
            InlineKeyboardButton("🔍 New search", callback_data="cfg_search"),
            InlineKeyboardButton("🏠 Sections",   callback_data="admin_bot_config"),
        ])

        pg   = f"  ·  Page {page}/{total_pages}" if total_pages > 1 else ""
        text = (
            f"🔍 <b>Results for «{term}»</b>{pg}\n\n"
            f"<b>{total} setting{'s' if total != 1 else ''} found</b> — "
            "tap any to view or edit."
        )

    if via_message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
    else:
        await _safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 3.  SECTION ROOT  —  category list
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    sid = query.data.split("cfg_sec_", 1)[1]
    sec = _SEC_BY_ID.get(sid)
    if not sec:
        return
    _, emoji, label, cats = sec

    kb: list[list[InlineKeyboardButton]] = []
    total_sec = 0
    for cat in cats:
        count = _CAT_COUNT.get(cat, 0)
        if not count:
            continue
        total_sec += count
        cat_label = _CAT_LABELS.get(cat, cat)
        kb.append([InlineKeyboardButton(
            f"{cat_label}  ·  {count} settings",
            callback_data=f"cfg_cat_{cat}__p1",
        )])

    kb.append([InlineKeyboardButton("🔙 Back to Sections", callback_data="admin_bot_config")])

    active_cats = len([c for c in cats if _CAT_COUNT.get(c, 0)])
    text = (
        f"{emoji} <b>{label}</b>\n\n"
        f"<b>{total_sec} settings</b> across {active_cats} categories.\n\n"
        "Select a category to view and edit."
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 4.  CATEGORY VIEW  —  paginated settings list
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_category(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                _override: str | None = None, _page: int = 1):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    if _override:
        cat, page = _override, _page
    else:
        raw = query.data.split("cfg_cat_", 1)[1]
        if "__p" in raw:
            cat, pstr = raw.rsplit("__p", 1)
            page = int(pstr) if pstr.isdigit() else 1
        else:
            cat, page = raw, 1

    items       = list_by_category(cat)
    cat_label   = _CAT_LABELS.get(cat, cat)
    total       = len(items)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page        = max(1, min(page, total_pages))
    page_items  = items[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]

    sid = _CAT_TO_SEC.get(cat, "")
    sec = _SEC_BY_ID.get(sid)
    bc  = f"{sec[1]} {sec[2]}  ›  " if sec else ""

    kb: list[list[InlineKeyboardButton]] = []
    for key, vtype, _default, _cat, setting_label, _desc in page_items:
        preview = _fmt_value(vtype, cfg.get_str(key, ""))
        kb.append([InlineKeyboardButton(
            f"{setting_label}  —  {preview}",
            callback_data=f"cfg_view_{key}",
        )])

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"cfg_cat_{cat}__p{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data=f"cfg_cat_{cat}__p1"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"cfg_cat_{cat}__p{page+1}"))
    if nav:
        kb.append(nav)

    back_cb = f"cfg_sec_{sid}" if sid else "admin_bot_config"
    kb.append([
        InlineKeyboardButton("🔙 Back",    callback_data=back_cb),
        InlineKeyboardButton("🏠 Sections", callback_data="admin_bot_config"),
    ])

    pg   = f"  ·  Page {page}/{total_pages}" if total_pages > 1 else ""
    text = (
        f"<b>{bc}{cat_label}</b>{pg}\n\n"
        f"<b>{total} settings</b> — tap any to view or edit."
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 5.  SINGLE-SETTING DETAIL
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_view(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            _override_key: str | None = None):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    key     = _override_key or query.data.split("cfg_view_", 1)[1]
    vtype, category, label, description = get_meta(key)
    current = cfg.get_str(key, "")

    bc = _setting_breadcrumb(category) + "  ›  "
    text = (
        f"⚙️ <b>{bc}{label}</b>\n"
        f"<code>{key}</code>  <i>({vtype})</i>\n\n"
        f"<i>{description}</i>\n\n"
        f"<b>Current value:</b>\n<code>{(current or '—')[:400]}</code>"
    )

    back_cb = f"cfg_cat_{category}__p1"
    if vtype == "bool":
        tgl = "🔴 Turn OFF" if cfg.get_bool(key) else "✅ Turn ON"
        kb = [
            [InlineKeyboardButton(tgl,                    callback_data=f"cfg_toggle_{key}")],
            [InlineKeyboardButton("↩️ Reset to default",  callback_data=f"cfg_reset_{key}")],
            [InlineKeyboardButton("🔙 Back",              callback_data=back_cb),
             InlineKeyboardButton("🏠 Sections",          callback_data="admin_bot_config")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("✏️ Edit",              callback_data=f"cfg_edit_{key}")],
            [InlineKeyboardButton("↩️ Reset to default",  callback_data=f"cfg_reset_{key}")],
            [InlineKeyboardButton("🔙 Back",              callback_data=back_cb),
             InlineKeyboardButton("🏠 Sections",          callback_data="admin_bot_config")],
        ]

    await _safe_edit(query, text, InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 6.  TOGGLE (bool)
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    key     = query.data.split("cfg_toggle_", 1)[1]
    new_val = not cfg.get_bool(key)
    cfg.set(key, new_val)
    await query.answer("✅ ON" if new_val else "🔴 OFF", show_alert=False)
    await admin_config_view(update, context, _override_key=key)


# ═══════════════════════════════════════════════════════════════════════════
# 7.  RESET TO DEFAULT
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return

    key = query.data.split("cfg_reset_", 1)[1]
    cfg.reset(key)
    await query.answer("↩️ Reset to default.", show_alert=False)
    await admin_config_view(update, context, _override_key=key)


# ═══════════════════════════════════════════════════════════════════════════
# 8.  EDIT  (conversation)
# ═══════════════════════════════════════════════════════════════════════════

async def admin_config_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        return ConversationHandler.END

    key   = query.data.split("cfg_edit_", 1)[1]
    vtype, _cat, label, description = get_meta(key)
    context.user_data["cfg_editing_key"] = key

    hint = {
        "int":   "Send a whole number  (e.g. <code>10</code>).",
        "float": "Send a decimal number  (e.g. <code>0.50</code>).",
        "bool":  "Send <code>true</code> or <code>false</code>.",
        "text":  "Send the full new text (multi-line is OK).",
        "str":   "Send the new value.",
    }.get(vtype, "Send the new value.")

    cat_label = _CAT_LABELS.get(_cat, _cat)
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cfg_view_{key}")]]
    try:
        await query.edit_message_text(
            f"✏️ <b>Edit: {label}</b>\n"
            f"<i>{cat_label}</i>  ·  <code>{key}</code>\n\n"
            f"<i>{description}</i>\n\n"
            f"{hint}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return EDIT_VALUE


@safe_conversation(cleanup_keys=("cfg_editing_key",))
async def admin_config_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.pop("cfg_editing_key", None)
    if not key:
        return ConversationHandler.END

    vtype, category, label, _desc = get_meta(key)
    raw = (update.message.text or "").strip()

    if vtype == "int":
        try:
            cfg.set(key, int(raw))
        except ValueError:
            await update.message.reply_text("❌ Not a valid integer. Send again, or /cancel.")
            context.user_data["cfg_editing_key"] = key
            return EDIT_VALUE
    elif vtype == "float":
        try:
            cfg.set(key, float(raw))
        except ValueError:
            await update.message.reply_text("❌ Not a valid number. Send again, or /cancel.")
            context.user_data["cfg_editing_key"] = key
            return EDIT_VALUE
    elif vtype == "bool":
        cfg.set(key, raw.lower() in ("1", "true", "yes", "on", "y", "t"))
    else:
        cfg.set(key, raw)

    cat_label = _CAT_LABELS.get(category, category)
    kb = [
        [InlineKeyboardButton("🔙 Back to setting", callback_data=f"cfg_view_{key}")],
        [InlineKeyboardButton(f"📂 {cat_label}",    callback_data=f"cfg_cat_{category}__p1")],
        [InlineKeyboardButton("🏠 Sections",         callback_data="admin_bot_config")],
    ]
    await update.message.reply_text(
        f"✅ <b>{label}</b> updated.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def admin_config_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("cfg_editing_key", None)
    if update.callback_query:
        await update.callback_query.answer()
        await admin_config_view(update, context)
    return ConversationHandler.END
