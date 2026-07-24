"""Premium Admin Control Center — categorized navigation (v45).

Root shows 8 main categories + search + quick-access + maintenance + exit.
Each category opens a paginated submenu (≤8 items + Back/Home per page).

Callback namespace
──────────────────
  acc:root                — root panel (categories)
  acc:cat:<name>          — category submenu page 1
  acc:cat:<name>:<page>   — category submenu page N
  acc:ui:search           — admin quick search → existing gse:menu
  acc:ui:favs             — favorites menu
  acc:ui:recent           — recent menus
  acc:ui:settings         — admin UI settings panel
  acc:ui:tog:<key>        — toggle a bool bot_config key
  acc:ui:set:status       — cycle panel status enabled→maintenance→disabled
  acc:ui:pin:<cb>         — pin callback to favorites
  acc:ui:unpin:<cb>       — remove callback from favorites
  acc:ui:clear_recent     — clear recent menus list

  acc:sec:<section>       — existing leaf-section render (unchanged)
  acc:<sect>:<action>     — existing sub-action route   (unchanged)

All existing callbacks remain fully operational for deep-links /
notification buttons.
"""
from __future__ import annotations

import logging
from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from utils.permissions import has_permission
from utils.perf import perf_track

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Category definitions
# Each category is a list of pages; each page is a list of (label, callback_data)
# Maximum 8 items per page so that page + Back + Home ≤ 10 buttons.
# ═══════════════════════════════════════════════════════════════════════════════

_CAT_PAGES: dict[str, list[list[tuple[str, str]]]] = {
    "dashboard": [[
        ("📊 Dashboard",          "acc:sec:dashboard"),
        ("📈 Analytics",          "admin_analytics"),
        ("📊 Advanced Analytics", "aana:menu"),
        ("💼 Business Insights",  "abiz:menu"),
        ("📊 Sales Forecast",     "asf:menu"),
        ("📈 Profit",             "acc:sec:profit"),
        ("📜 Activity Timeline",  "gat:menu"),
    ]],

    "products": [[
        ("📦 Products",              "admin_products"),
        ("🗂 Categories",            "admin_manage_categories"),
        ("📦 Bundles",               "abn:menu"),
        ("🎟 Gift Cards",            "agc:menu"),
        ("📋 Product Templates",     "apt:menu"),
        ("📄 Clone Products",        "pct:menu"),
    ]],

    "discovery": [[
        ("❓ Product FAQ",           "acc:sec:pfaq"),
        ("⚖️ Product Compare",        "acc:sec:pcmp"),
        ("❤️ Favorites",              "acc:sec:favs"),
        ("🕒 Recently Viewed",       "acc:sec:rvw"),
    ]],

    "inventory": [[
        ("📥 Inventory",              "admin_restock_keys"),
        ("📦 Batches",                "acc:sec:batches"),
        ("📈 Price History",          "acc:sec:ph"),
        ("⏳ Inventory Reservation",  "acc:sec:irs"),
        ("📦 Bulk Products",          "bpim:menu"),
    ]],

    "suppliers": [[
        ("🏭 Suppliers",              "acc:sec:suppliers"),
        ("📂 File & Keys",            "flm:menu"),
        ("🚚 Delivery Manager",       "dms:menu"),
    ]],

    "orders": [[
        ("🧾 Orders",          "admin_orders"),
        ("🔎 Search Order",    "aos:menu"),
        ("🤖 Auto Assign",     "acc:sec:sas"),
        ("📋 Order Timeline",  "acc:sec:ots"),
        ("🚚 Delivery Queue",  "acc:sec:delivery"),
        ("💰 Refunds",         "aref:menu"),
        ("🎁 Gift Purchase",   "agp:menu"),
    ]],

    "payments": [[
        ("💳 Payment Gateways",       "admin_gateways"),
        ("🧾 Manual Payments",        "admin_payment_methods"),
        ("💰 Wallets",                "acc:sec:wallets"),
        ("🌍 Multi-Currency Wallet",  "amcw:menu"),
        ("🔄 Exchange Rates",         "aerm:menu"),
        ("🔌 Webhook Monitor",        "awm:menu"),
    ]],

    "customers": [[
        ("👥 Users",          "admin_users"),
        ("📝 Customer CRM",   "crm:home"),
        ("👥 Bulk Users",     "bum:menu"),
        ("⭐ Reviews",        "arv:menu"),
    ]],

    "loyalty": [[
        ("⭐ Loyalty & VIP",  "admin_loyalty"),
        ("👥 Referrals",      "admin_referral_reward"),
        ("👥 Referral+",      "rd:admin"),
        ("🎧 Support",        "admin_tickets"),
        ("⚠️ Disputes",       "admin_view_disputes"),
    ]],

    "broadcast": [[
        ("📢 Broadcast",            "acc:sec:broadcast"),
        ("📨 Scheduled Broadcast",  "asb:menu"),
        ("📢 Announcements",        "ann:menu"),
        ("🔔 Notification Center",  "anc:menu"),
        ("🔔 Notification Settings", "nsm:menu"),
    ]],

    "promotions": [[
        ("🎁 Promotions",           "acc:sec:promotions"),
        ("⚡ Flash Sales",          "fsm:menu"),
        ("🎟 Coupons",              "admin_coupons"),
        ("🏷 Advanced Coupons",     "acpn:menu"),
        ("🔔 Sub Reminders",        "acc:sec:subrem"),
    ]],

    "security": [[
        ("🔍 Fraud Detection",     "fds:home"),
        ("🛡 Anti-Spam",           "aasm:menu"),
        ("📝 Audit Logs",          "acc:sec:audit"),
        ("🔐 Login Activity",      "lam:home"),
        ("🔌 API & Integrations",  "acc:sec:integrations"),
    ]],

    "system": [[
        ("⚙️ Bot Settings",       "admin_bot_config"),
        ("🎨 Menu Manager",        "mm:menu"),
        ("🌍 Languages",           "alng:menu"),
        ("⚙️ Features",             "af:menu"),
        ("📱 Account Features",    "aaf:menu"),
        ("🧩 Module Manager",      "pmm:menu"),
        ("📤 Data Export Center",  "dec:menu"),
    ]],

    "performance": [[
        ("⚡ Performance Manager", "pcm:menu"),
        ("🧹 Cache Manager",       "pcm:cache"),
        ("🔍 Global Search",       "gse:menu"),
    ]],

    "tools": [[
        ("🩺 Diagnostics",         "acc:diag:menu"),
        ("🔧 Maintenance+",        "maint:menu"),
        ("💾 Backups",             "acc:sec:backups"),
        ("🛠 System Tools",        "acc:sec:system"),
        ("🧪 Quality Control",     "acc:sec:quality"),
        ("🩺 Integrity Scan",      "acc:sec:integrity"),
        ("🤝 Resellers",           "acc:sec:resellers"),
    ]],
}

_CAT_META: dict[str, tuple[str, str]] = {
    "dashboard":   ("📊", "Dashboard"),
    "products":    ("📦", "Products"),
    "discovery":   ("🧭", "Discovery"),
    "inventory":   ("📥", "Inventory"),
    "suppliers":   ("🏭", "Suppliers"),
    "orders":      ("🛒", "Orders"),
    "payments":    ("💳", "Payments"),
    "customers":   ("👥", "Customers"),
    "loyalty":     ("⭐", "Loyalty & Support"),
    "broadcast":   ("📢", "Broadcast"),
    "promotions":  ("🎟", "Promotions"),
    "security":    ("🛡", "Security"),
    "system":      ("⚙️", "Settings"),
    "performance": ("⚡", "Performance"),
    "tools":       ("🛠", "Tools"),
}

# One-line tagline shown under the breadcrumb on each category's submenu,
# so admins know at a glance what kind of tools live in this section.
_CAT_DESC: dict[str, str] = {
    "dashboard":   "Live store stats, growth &amp; profit metrics.",
    "products":    "Catalog, bundles, gift cards &amp; product tools.",
    "discovery":   "FAQ, compare, favorites &amp; recently viewed.",
    "inventory":   "Stock, batches, price history &amp; reservations.",
    "suppliers":   "Suppliers, files/keys &amp; delivery manager.",
    "orders":      "Order queue, search, delivery &amp; refunds.",
    "payments":    "Gateways, manual payments, wallets &amp; FX rates.",
    "customers":   "Users, CRM, bulk users &amp; reviews.",
    "loyalty":     "Loyalty, VIP, referrals, support &amp; disputes.",
    "broadcast":   "Broadcasts, announcements &amp; notifications.",
    "promotions":  "Promotions, flash sales, coupons &amp; reminders.",
    "security":    "Fraud, anti-spam, audit logs &amp; access control.",
    "system":      "Bot config, menu &amp; colors, languages, modules.",
    "performance": "Performance, cache &amp; global search.",
    "tools":       "Diagnostics, backups, system &amp; quality tools.",
}

# Total item count per category (all pages combined) — shown as a badge
# next to the button on the root panel, e.g. "📦 Products · 18".
_CAT_COUNT: dict[str, int] = {
    _cat: sum(len(_page) for _page in _pages)
    for _cat, _pages in _CAT_PAGES.items()
}

# Reverse lookup: callback_data → (category, label) for recent/breadcrumb
_CB_META: dict[str, tuple[str, str]] = {}
for _cat, _pages in _CAT_PAGES.items():
    for _page in _pages:
        for _label, _cb in _page:
            if _cb not in _CB_META:
                _CB_META[_cb] = (_cat, _label)

_MAX_FAVS   = 8
_MAX_RECENT = 10
_NAV_KEY    = "admin_nav_v2"   # key in context.bot_data

# ═══════════════════════════════════════════════════════════════════════════════
# Per-admin nav data (stored in bot_data; survives bot process lifetime)
# ═══════════════════════════════════════════════════════════════════════════════

def _nav(context: ContextTypes.DEFAULT_TYPE, uid: int) -> dict:
    root = context.bot_data.setdefault(_NAV_KEY, {})
    return root.setdefault(str(uid), {"favs": [], "recent": []})


def _record_recent(context: ContextTypes.DEFAULT_TYPE, uid: int,
                   label: str, cb: str) -> None:
    nd = _nav(context, uid)
    nd["recent"] = [e for e in nd["recent"] if e[1] != cb]
    nd["recent"].insert(0, (label, cb))
    nd["recent"] = nd["recent"][:_MAX_RECENT]


def _toggle_fav(context: ContextTypes.DEFAULT_TYPE, uid: int,
                label: str, cb: str) -> bool:
    """Returns True if pinned, False if unpinned."""
    nd = _nav(context, uid)
    if any(e[1] == cb for e in nd["favs"]):
        nd["favs"] = [e for e in nd["favs"] if e[1] != cb]
        return False
    if len(nd["favs"]) < _MAX_FAVS:
        nd["favs"].append((label, cb))
    return True


def _is_fav(context: ContextTypes.DEFAULT_TYPE, uid: int, cb: str) -> bool:
    return any(e[1] == cb for e in _nav(context, uid)["favs"])


# ═══════════════════════════════════════════════════════════════════════════════
# Keyboard builders
# ═══════════════════════════════════════════════════════════════════════════════

def _cfg_bool(key: str, default: bool) -> bool:
    from utils.bot_config import cfg
    return cfg.get_bool(key, default)


def _tog_icon(key: str, default: bool) -> str:
    return "🟢" if _cfg_bool(key, default) else "🔴"


def build_acc_root_keyboard(maintenance_on: bool) -> IKM:
    """New categorized root panel."""
    from utils.bot_config import cfg
    use_icons   = cfg.get_bool("admin_panel_icons",     True)
    show_search = cfg.get_bool("admin_panel_search",    True)
    show_favs   = cfg.get_bool("admin_panel_favorites", True)
    show_recent = cfg.get_bool("admin_panel_recent",    True)

    def lbl(icon: str, text: str, cat: str) -> str:
        badge = f" · {_CAT_COUNT.get(cat, 0)}"
        return f"{icon} {text}{badge}" if use_icons else f"{text}{badge}"

    kb: list[list[IKB]] = []
    row: list[IKB] = []
    for cat, (icon, name) in _CAT_META.items():
        row.append(IKB(lbl(icon, name, cat), callback_data=f"acc:cat:{cat}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([IKB("🔔 Notification Settings", callback_data="nsm:menu")])

    if show_search:
        kb.append([IKB("🔍 Admin Search", callback_data="acc:ui:search")])

    quick: list[IKB] = []
    if show_favs:
        quick.append(IKB("⭐ Favorites",  callback_data="acc:ui:favs"))
    if show_recent:
        quick.append(IKB("🕐 Recent",     callback_data="acc:ui:recent"))
    if quick:
        kb.append(quick)

    maint_label = ("🟢 Maintenance: ON" if maintenance_on else "⚪ Maintenance: OFF")
    kb.append([
        IKB("🔧 UI Settings",  callback_data="acc:ui:settings"),
        IKB(maint_label,        callback_data="admin_maintenance_toggle"),
    ])
    kb.append([IKB("🚪 Exit Admin", callback_data="main_menu")])
    return IKM(kb)


def _build_category_keyboard(cat: str, page: int, uid: int,
                              context: ContextTypes.DEFAULT_TYPE) -> IKM:
    """Submenu for one category page."""
    pages = _CAT_PAGES.get(cat, [[]])
    total = len(pages)
    page  = max(1, min(page, total))
    items = pages[page - 1]

    use_icons  = _cfg_bool("admin_panel_icons",     True)
    show_bc    = _cfg_bool("admin_panel_breadcrumb", True)
    compact    = _cfg_bool("admin_panel_compact",    False)

    cat_icon, cat_name = _CAT_META.get(cat, ("📋", cat.title()))

    kb: list[list[IKB]] = []

    if compact:
        # Two buttons per row
        row: list[IKB] = []
        for label, cb in items:
            row.append(IKB(label, callback_data=cb))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
    else:
        # One button per row (cleaner on mobile)
        for label, cb in items:
            kb.append([IKB(label, callback_data=cb)])

    # Pagination row
    pag: list[IKB] = []
    if page > 1:
        pag.append(IKB("◀ Prev", callback_data=f"acc:cat:{cat}:{page - 1}"))
    if page < total:
        pag.append(IKB(f"▶ More ({page}/{total})",
                        callback_data=f"acc:cat:{cat}:{page + 1}"))
    if pag:
        kb.append(pag)

    # Back + Home
    kb.append([
        IKB("⬅ Back",       callback_data="acc:root"),
        IKB("🏠 Admin Home", callback_data="acc:root"),
    ])
    return IKM(kb)


def _build_favs_keyboard(context: ContextTypes.DEFAULT_TYPE, uid: int) -> IKM:
    nd   = _nav(context, uid)
    favs = nd["favs"]
    kb: list[list[IKB]] = []
    if favs:
        for label, cb in favs:
            kb.append([
                IKB(label, callback_data=cb),
                IKB("❌ Unpin", callback_data=f"acc:ui:unpin:{cb}"),
            ])
    else:
        kb.append([IKB("📭 No favorites pinned yet", callback_data="acc:root")])
    kb.append([IKB("⬅ Back", callback_data="acc:root"),
               IKB("🏠 Admin Home", callback_data="acc:root")])
    return IKM(kb)


def _build_recent_keyboard(context: ContextTypes.DEFAULT_TYPE, uid: int) -> IKM:
    nd     = _nav(context, uid)
    recent = nd["recent"]
    kb: list[list[IKB]] = []
    if recent:
        for label, cb in recent:
            kb.append([IKB(label, callback_data=cb)])
        kb.append([IKB("🗑 Clear History", callback_data="acc:ui:clear_recent")])
    else:
        kb.append([IKB("📭 No recent menus yet", callback_data="acc:root")])
    kb.append([IKB("⬅ Back", callback_data="acc:root"),
               IKB("🏠 Admin Home", callback_data="acc:root")])
    return IKM(kb)


def _build_ui_settings_keyboard() -> IKM:
    from utils.bot_config import cfg
    status = cfg.get("admin_panel_status", "enabled")
    status_icons = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}
    s_icon = status_icons.get(status, "🟢")
    s_next = {"enabled": "maintenance", "maintenance": "disabled",
              "disabled": "enabled"}.get(status, "enabled")
    s_next_icon = status_icons.get(s_next, "🟢")

    kb = [
        [IKB(f"Panel Status: {s_icon} {status.title()}  →  {s_next_icon} {s_next.title()}",
             callback_data="acc:ui:set:status")],
        [IKB(f"{_tog_icon('admin_panel_categories', True)} Categories",
             callback_data="acc:ui:tog:admin_panel_categories")],
        [IKB(f"{_tog_icon('admin_panel_search', True)} Global Search",
             callback_data="acc:ui:tog:admin_panel_search")],
        [IKB(f"{_tog_icon('admin_panel_favorites', True)} Favorites",
             callback_data="acc:ui:tog:admin_panel_favorites")],
        [IKB(f"{_tog_icon('admin_panel_recent', True)} Recent Menus",
             callback_data="acc:ui:tog:admin_panel_recent")],
        [IKB(f"{_tog_icon('admin_panel_compact', False)} Compact Mode",
             callback_data="acc:ui:tog:admin_panel_compact")],
        [IKB(f"{_tog_icon('admin_panel_icons', True)} Icons",
             callback_data="acc:ui:tog:admin_panel_icons")],
        [IKB(f"{_tog_icon('admin_panel_breadcrumb', True)} Breadcrumb Navigation",
             callback_data="acc:ui:tog:admin_panel_breadcrumb")],
        [IKB("⬅ Back",       callback_data="acc:root"),
         IKB("🏠 Admin Home", callback_data="acc:root")],
    ]
    return IKM(kb)


# ═══════════════════════════════════════════════════════════════════════════════
# Root render
# ═══════════════════════════════════════════════════════════════════════════════

async def _safe_edit(query, text: str, kb: IKM) -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            try:
                await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


@perf_track("admin_panel_handler")
async def render_control_center(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the ACC root — categorized panel with live stats header."""
    from handlers.admin_dashboard import _collect_dashboard_stats, _render_dashboard_text
    from utils.bot_config import cfg

    stats = _collect_dashboard_stats()
    text  = _render_dashboard_text(stats)
    kb    = build_acc_root_keyboard(cfg.get_bool("maintenance_mode", False))

    query = getattr(update, "callback_query", None)
    if query is not None:
        await _safe_edit(query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# Central dispatcher for all acc:* callbacks
# ═══════════════════════════════════════════════════════════════════════════════

async def acc_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route every ``acc:*`` callback to the right handler."""
    query = update.callback_query
    if query is None:
        return

    if not has_permission(update.effective_user.id, "view_analytics"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data  = query.data or ""
    parts = data.split(":")
    uid   = update.effective_user.id

    # ── acc:root ─────────────────────────────────────────────────────────────
    if data == "acc:root":
        await query.answer()
        await render_control_center(update, context)
        return

    # ── acc:cat:<name>[:<page>] ───────────────────────────────────────────────
    if len(parts) >= 3 and parts[1] == "cat":
        cat  = parts[2]
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 1
        await query.answer()
        await _render_category(cat, page, uid, update, context)
        return

    # ── acc:ui:* ──────────────────────────────────────────────────────────────
    if len(parts) >= 3 and parts[1] == "ui":
        action = parts[2]
        rest   = parts[3:]
        await query.answer()
        await _handle_ui_action(action, rest, uid, update, context)
        return

    # ── acc:sec:<section> ────────────────────────────────────────────────────
    if len(parts) >= 3 and parts[1] == "sec":
        section = parts[2]
        await query.answer()
        # Record this visit
        label, _ = _CB_META.get(data, ("", data))
        if label:
            _record_recent(context, uid, label, data)
        await _render_section(section, update, context)
        return

    # ── acc:<sect>:<action>[:<rest>] — existing sub-action routing ───────────
    if len(parts) >= 3:
        section = parts[1]
        action  = parts[2]
        rest    = parts[3:]
        await _route_section_action(section, action, rest, update, context)
        return

    await query.answer()
    await render_control_center(update, context)


# ═══════════════════════════════════════════════════════════════════════════════
# Category render
# ═══════════════════════════════════════════════════════════════════════════════

async def _render_category(cat: str, page: int, uid: int,
                           update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if cat not in _CAT_META:
        await render_control_center(update, context)
        return

    cat_icon, cat_name = _CAT_META[cat]
    pages = _CAT_PAGES.get(cat, [[]])
    total = len(pages)
    page  = max(1, min(page, total))

    use_bc = _cfg_bool("admin_panel_breadcrumb", True)
    if use_bc and total > 1:
        breadcrumb = f"🏠 Admin  ›  {cat_icon} <b>{cat_name}</b>  ›  Page {page}/{total}"
    elif use_bc:
        breadcrumb = f"🏠 Admin  ›  {cat_icon} <b>{cat_name}</b>"
    else:
        breadcrumb = f"{cat_icon} <b>{cat_name}</b>"

    tagline  = _CAT_DESC.get(cat, "")
    item_cnt = _CAT_COUNT.get(cat, 0)
    lines = [breadcrumb]
    if tagline:
        lines.append(f"<i>{tagline}</i>")
    lines.append(f"\nSelect a feature ({item_cnt} available):")
    text = "\n".join(lines)
    kb   = _build_category_keyboard(cat, page, uid, context)
    await _safe_edit(query, text, kb)


# ═══════════════════════════════════════════════════════════════════════════════
# UI-action handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_ui_action(action: str, rest: list[str], uid: int,
                             update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    # ── search → existing Global Search Engine ────────────────────────────────
    if action == "search":
        from handlers.admin_global_search import gse_menu
        await gse_menu(update, context)
        return

    # ── favorites ─────────────────────────────────────────────────────────────
    if action == "favs":
        nd   = _nav(context, uid)
        favs = nd["favs"]
        text = (
            "⭐ <b>Favorites</b>\n\n"
            "Your pinned admin menus appear here.\n\n"
            "To pin a menu: open any category, then tap "
            "<code>⭐ Pin this menu</code>."
            if not favs else
            f"⭐ <b>Favorites</b>  ({len(favs)}/{_MAX_FAVS} pinned)"
        )
        await _safe_edit(query, text, _build_favs_keyboard(context, uid))
        return

    # ── pin (from category view) ──────────────────────────────────────────────
    if action == "pin" and rest:
        cb    = ":".join(rest)
        label, _ = _CB_META.get(cb, ("", cb))
        if not label:
            label = cb
        added = _toggle_fav(context, uid, label, cb)
        await query.answer("⭐ Pinned!" if added else "✅ Unpinned", show_alert=False)
        await render_control_center(update, context)
        return

    # ── unpin (from favorites list) ───────────────────────────────────────────
    if action == "unpin" and rest:
        cb = ":".join(rest)
        nd = _nav(context, uid)
        nd["favs"] = [e for e in nd["favs"] if e[1] != cb]
        await query.answer("✅ Unpinned")
        # Re-render favorites
        nd2   = _nav(context, uid)
        favs  = nd2["favs"]
        text  = (
            "⭐ <b>Favorites</b>\n\n"
            "Your pinned menus appear here."
            if not favs else
            f"⭐ <b>Favorites</b>  ({len(favs)}/{_MAX_FAVS} pinned)"
        )
        await _safe_edit(query, text, _build_favs_keyboard(context, uid))
        return

    # ── recent ────────────────────────────────────────────────────────────────
    if action == "recent":
        nd     = _nav(context, uid)
        recent = nd["recent"]
        text   = (
            "🕐 <b>Recent Menus</b>\n\nYour last-visited admin menus appear here."
            if not recent else
            f"🕐 <b>Recent Menus</b>  ({len(recent)} entries)"
        )
        await _safe_edit(query, text, _build_recent_keyboard(context, uid))
        return

    # ── clear recent ──────────────────────────────────────────────────────────
    if action == "clear_recent":
        _nav(context, uid)["recent"] = []
        await query.answer("🗑 History cleared")
        await _safe_edit(query,
                         "🕐 <b>Recent Menus</b>\n\nHistory cleared.",
                         _build_recent_keyboard(context, uid))
        return

    # ── UI settings panel ─────────────────────────────────────────────────────
    if action == "settings":
        from utils.bot_config import cfg
        status = cfg.get("admin_panel_status", "enabled")
        text   = (
            "🔧 <b>Admin UI Settings</b>\n\n"
            "Configure the Admin Panel interface.\n"
            f"Current status: <b>{status.title()}</b>"
        )
        await _safe_edit(query, text, _build_ui_settings_keyboard())
        return

    # ── toggle a bool bot_config key ─────────────────────────────────────────
    if action == "tog" and rest:
        key = rest[0]
        _ALLOWED_TOG = {
            "admin_panel_categories", "admin_panel_search",
            "admin_panel_favorites",  "admin_panel_recent",
            "admin_panel_compact",    "admin_panel_icons",
            "admin_panel_breadcrumb",
        }
        if key not in _ALLOWED_TOG:
            await query.answer("⛔ Not allowed", show_alert=True)
            return
        from utils.bot_config import cfg
        current = cfg.get_bool(key, True)
        cfg.set(key, not current)
        await query.answer(f"{'🟢 Enabled' if not current else '🔴 Disabled'}")
        from utils.bot_config import cfg as cfg2
        status = cfg2.get("admin_panel_status", "enabled")
        text   = (
            "🔧 <b>Admin UI Settings</b>\n\n"
            "Configure the Admin Panel interface.\n"
            f"Current status: <b>{status.title()}</b>"
        )
        await _safe_edit(query, text, _build_ui_settings_keyboard())
        return

    # ── cycle panel status ────────────────────────────────────────────────────
    if action == "set" and rest and rest[0] == "status":
        from utils.bot_config import cfg
        current = cfg.get("admin_panel_status", "enabled")
        nxt = {"enabled": "maintenance", "maintenance": "disabled",
               "disabled": "enabled"}.get(current, "enabled")
        cfg.set("admin_panel_status", nxt)
        icons   = {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}
        await query.answer(f"{icons.get(nxt, '🟢')} Status: {nxt.title()}")
        text = (
            "🔧 <b>Admin UI Settings</b>\n\n"
            "Configure the Admin Panel interface.\n"
            f"Current status: <b>{nxt.title()}</b>"
        )
        await _safe_edit(query, text, _build_ui_settings_keyboard())
        return

    # Unknown UI action → root
    await render_control_center(update, context)


# ═══════════════════════════════════════════════════════════════════════════════
# Existing leaf-section renders (fully preserved)
# ═══════════════════════════════════════════════════════════════════════════════

async def _render_section(section: str, update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a leaf section — identical to original implementation."""
    if section == "dashboard":
        from handlers.admin_dashboard_widgets import show_widget_dashboard
        await show_widget_dashboard(update, context); return
    if section == "wallets":
        from handlers.admin_wallets import wallets_menu
        await wallets_menu(update, context); return
    if section == "promotions":
        from handlers.admin_promotions import promotions_menu
        await promotions_menu(update, context); return
    if section == "notifs":
        from handlers.admin_notifications import notifs_menu
        await notifs_menu(update, context); return
    if section == "broadcast":
        from handlers.admin_broadcast_center import broadcast_menu
        await broadcast_menu(update, context); return
    if section == "audit":
        from handlers.admin_audit_enhanced import audit_menu
        await audit_menu(update, context); return
    if section == "integrations":
        from handlers.admin_integrations import integrations_menu
        await integrations_menu(update, context); return
    if section == "system":
        from handlers.admin_system_tools import system_menu
        await system_menu(update, context); return
    if section == "suppliers":
        from handlers.admin_suppliers import suppliers_menu
        await suppliers_menu(update, context); return
    if section == "batches":
        from handlers.admin_batches import batches_menu
        await batches_menu(update, context); return
    if section == "profit":
        from handlers.admin_profit import profit_menu
        await profit_menu(update, context); return
    if section == "quality":
        from handlers.admin_quality import quality_menu
        await quality_menu(update, context); return
    if section == "resellers":
        from handlers.admin_resellers import resellers_menu
        await resellers_menu(update, context); return
    if section == "delivery":
        from handlers.admin_delivery_queue import delivery_menu
        await delivery_menu(update, context); return
    if section == "backups":
        from handlers.admin_backups import backups_menu
        await backups_menu(update, context); return
    if section == "integrity":
        from handlers.admin_integrity import integrity_menu
        await integrity_menu(update, context); return
    if section == "bulk_products":
        from handlers.admin_bulk_products import bpim_menu
        await bpim_menu(update, context); return
    if section == "bulk_users":
        from handlers.admin_bulk_users import bum_menu
        await bum_menu(update, context); return
    if section == "delivery_manager":
        from handlers.admin_delivery_manager import dms_menu
        await dms_menu(update, context); return
    if section == "notification_center":
        from handlers.admin_notification_center import anc_menu
        await anc_menu(update, context); return
    if section == "file_license_manager":
        from handlers.admin_file_license_manager import flm_menu
        await flm_menu(update, context); return
    if section == "flash_sale_manager":
        from handlers.admin_flash_sale_manager import fsm_menu
        await fsm_menu(update, context); return
    if section == "mcwallet":
        from handlers.admin_multicurrency_wallet import amcw_menu
        await amcw_menu(update, context); return
    if section == "exrate":
        from handlers.admin_exchange_rate import aerm_menu
        await aerm_menu(update, context); return
    if section == "diag":
        from handlers.admin_diagnostics import diag_menu
        await diag_menu(update, context); return
    if section == "subscriptions":
        from handlers.admin_subscriptions import subscriptions_menu
        await subscriptions_menu(update, context); return
    if section == "subrem":
        from handlers.admin_subscription_reminders import subscription_reminders_menu
        await subscription_reminders_menu(update, context); return
    if section == "favs":
        from handlers.admin_favorites import favorites_menu
        await favorites_menu(update, context); return
    if section == "pcmp":
        from handlers.admin_product_compare import product_compare_menu
        await product_compare_menu(update, context); return
    if section == "rvw":
        from handlers.admin_recently_viewed import recently_viewed_admin_menu
        await recently_viewed_admin_menu(update, context); return
    if section == "ph":
        from handlers.admin_price_history import price_history_admin_menu
        await price_history_admin_menu(update, context); return
    if section == "irs":
        from handlers.admin_inventory_reservation import irs_admin_menu
        await irs_admin_menu(update, context); return
    if section == "sas":
        # needs action/rest — fallback to root
        await render_control_center(update, context); return
    if section == "ots":
        from handlers.admin_order_timeline import ots_menu
        await ots_menu(update, context); return
    if section == "pfaq":
        from handlers.admin_product_faq import pfaq_menu
        await pfaq_menu(update, context); return
    if section == "features":
        from handlers.admin_features import features_menu
        await features_menu(update, context); return
    if section == "announcements":
        from handlers.admin_announcements import announcements_menu
        await announcements_menu(update, context); return
    if section == "maint_adv":
        from handlers.admin_maintenance import maintenance_menu
        await maintenance_menu(update, context); return
    if section == "referral_adv":
        from handlers.referral_dashboard import rd_admin_menu
        await rd_admin_menu(update, context); return
    # Unknown → root
    await render_control_center(update, context)


# ═══════════════════════════════════════════════════════════════════════════════
# Existing sub-action routing (fully preserved)
# ═══════════════════════════════════════════════════════════════════════════════

async def _route_section_action(section: str, action: str, rest: list[str],
                                 update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        if section == "wal":
            from handlers import admin_wallets as m
            await m.route(action, rest, update, context); return
        if section == "promo":
            from handlers import admin_promotions as m
            await m.route(action, rest, update, context); return
        if section == "notif":
            from handlers import admin_notifications as m
            await m.route(action, rest, update, context); return
        if section == "bc":
            from handlers import admin_broadcast_center as m
            await m.route(action, rest, update, context); return
        if section == "audit":
            from handlers import admin_audit_enhanced as m
            await m.route(action, rest, update, context); return
        if section == "sys":
            from handlers import admin_system_tools as m
            await m.route(action, rest, update, context); return
        if section == "int":
            from handlers import admin_integrations as m
            await m.route(action, rest, update, context); return
        if section == "sup":
            from handlers import admin_suppliers as m
            await m.route(action, rest, update, context); return
        if section == "bat":
            from handlers import admin_batches as m
            await m.route(action, rest, update, context); return
        if section == "qual":
            from handlers import admin_quality as m
            await m.route(action, rest, update, context); return
        if section == "res":
            from handlers import admin_resellers as m
            await m.route(action, rest, update, context); return
        if section == "dlv":
            from handlers import admin_delivery_queue as m
            await m.route(action, rest, update, context); return
        if section == "bak":
            from handlers import admin_backups as m
            await m.route(action, rest, update, context); return
        if section == "diag":
            from handlers.admin_diagnostics import diag_dispatch
            await diag_dispatch(update, context); return
        if section == "intg":
            from handlers import admin_integrity as m
            await m.route(action, rest, update, context); return
        if section == "subs":
            from handlers import admin_subscriptions as m
            await m.route(action, rest, update, context); return
        if section == "srm":
            from handlers import admin_subscription_reminders as m
            await m.route(action, rest, update, context); return
        if section == "favs":
            from handlers import admin_favorites as m
            await m.route(action, rest, update, context); return
        if section == "pcmp":
            from handlers import admin_product_compare as m
            await m.route(action, rest, update, context); return
        if section == "rvw":
            from handlers import admin_recently_viewed as m
            await m.route(action, rest, update, context); return
        if section == "ph":
            from handlers import admin_price_history as m
            await m.route(action, rest, update, context); return
        if section == "irs":
            from handlers import admin_inventory_reservation as m
            await m.route(action, rest, update, context); return
        if section == "sas":
            from handlers import admin_supplier_auto_assign as m
            await m.route(action, rest, update, context); return
        if section == "ots":
            from handlers import admin_order_timeline as m
            await m.route(action, rest, update, context); return
        if section == "pfaq":
            from handlers import admin_product_faq as m
            await m.route(action, rest, update, context); return
        if section == "bundles":
            from handlers import admin_bundles as m
            await m.route(action, rest, update, context); return
        if section == "reviews":
            from handlers import admin_reviews as m
            await m.route(action, rest, update, context); return
        if section == "gifts":
            if action == "gp":
                from handlers import admin_gift_purchase as m
                await m.route(rest[0] if rest else "menu",
                              rest[1:] if len(rest) > 1 else [],
                              update, context); return
            if action == "gc":
                from handlers import admin_gift_cards as m
                await m.route(rest[0] if rest else "menu",
                              rest[1:] if len(rest) > 1 else [],
                              update, context); return
            # Gift hub
            kb = IKM([
                [IKB("🎁 Gift Purchase Settings", callback_data="agp:menu")],
                [IKB("🎟 Gift Card Manager",       callback_data="agc:menu")],
                [IKB("⬅ Back", callback_data="acc:root"),
                 IKB("🏠 Admin Home", callback_data="acc:root")],
            ])
            try:
                await update.callback_query.edit_message_text(
                    "🎁 <b>Gifts & Gift Cards</b>\n\nChoose a section:",
                    reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return
    except Exception:
        logger.exception("acc sub-action failed: %s:%s", section, action)
    try:
        await query.answer()
    except Exception:
        pass
    await render_control_center(update, context)
