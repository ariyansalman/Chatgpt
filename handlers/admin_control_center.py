"""Premium Admin Control Center — categorized navigation (v46).

Root shows the 12 enterprise sections (Dashboard, Products, Orders,
Payments, Customers, Marketing, Notifications, UI & Menu, Store Settings,
Security, System, Tools) + search + quick-access + maintenance + exit.
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
# Category definitions — Enterprise structure (v46)
#
# Exactly 12 top-level sections, matching the approved information
# architecture. Every existing feature keeps its original callback_data —
# only *which category dict it's listed under* changed, so no routing,
# handler, or business logic was touched anywhere in the bot.
#
# Each category is a list of pages; each page is a list of (label, callback_data)
# Maximum 8 items per page so that page + Back/pagination ≤ 10 buttons.
# ═══════════════════════════════════════════════════════════════════════════════

_CAT_PAGES: dict[str, list[list[tuple[str, str]]]] = {

    # ── 📊 Dashboard — reporting & KPIs (unchanged from v45) ────────────────
    "dashboard": [[
        ("📊 Dashboard",           "acc:sec:dashboard"),
        ("📈 Analytics",           "admin_analytics"),
        ("🔬 Advanced Analytics",  "aana:menu"),
        ("💼 Business Insights",   "abiz:menu"),
        ("📉 Sales Forecast",      "asf:menu"),
        ("💹 Profit",              "acc:sec:profit"),
        ("📜 Activity Logs",       "gat:menu"),
        ("📈 Growth & LTV",        "admin_analytics_cohort"),
    ]],

    # ── 📦 Products — catalog, discovery, inventory & suppliers all live
    # under one roof (former "inventory" and "suppliers" top-level categories
    # folded in as extra pages — they're all product-management, not
    # separate concerns).
    "products": [
        [
            ("📦 Products",            "admin_products"),
            ("🗂 Categories",          "admin_manage_categories"),
            ("🎀 Bundles",             "abn:menu"),
            ("🎟 Gift Cards",          "agc:menu"),
            ("🗂 Template Manager",    "apt:menu"),
            ("📄 Clone Products",      "pct:menu"),
        ],
        [
            ("❓ Product FAQ",          "acc:sec:pfaq"),
            ("⚖️ Product Compare",      "acc:sec:pcmp"),
            ("❤️ Favorites",            "acc:sec:favs"),
            ("🕒 Recently Viewed",      "acc:sec:rvw"),
        ],
        [
            ("📥 Inventory",           "admin_restock_keys"),
            ("📂 Batches",             "acc:sec:batches"),
            ("🏷 Price History",       "acc:sec:ph"),
            ("⏳ Reservation",         "acc:sec:irs"),
            ("⚡ Bulk Products",       "bpim:menu"),
            ("📉 Low Stock",           "admin_low_stock"),
        ],
        [
            ("🏭 Suppliers",           "acc:sec:suppliers"),
            ("🗝 File & Keys",         "flm:menu"),
            ("🚚 Delivery Manager",    "dms:menu"),
        ],
    ],

    # ── 🛒 Orders — order lifecycle, incl. disputes (moved in from the old
    # "loyalty" grab-bag — a dispute is an order-level issue, not a loyalty
    # one).
    "orders": [[
        ("🧾 Orders",              "admin_orders"),
        ("🔎 Search Order",        "aos:menu"),
        ("🤖 Auto Assign",         "acc:sec:sas"),
        ("⏱ Order Timeline",      "acc:sec:ots"),
        ("📬 Delivery Queue",      "acc:sec:delivery"),
        ("💰 Refunds",             "aref:menu"),
        ("🛍 Gift Purchase",       "agp:menu"),
        ("⚠️ Disputes",            "admin_view_disputes"),
    ]],

    # ── 💳 Payments — unchanged from v45 ──────────────────────────────────
    "payments": [[
        ("💳 Payment Settings",    "admin_gateways"),
        ("🏦 Manual Payments",     "admin_payment_methods"),
        ("🧾 Pending Deposits",    "pd:list:0:desc"),
        ("👛 Wallets",             "acc:sec:wallets"),
        ("🌍 Multi-Currency",      "amcw:menu"),
        ("🔄 Exchange Rates",      "aerm:menu"),
        ("🔌 Webhook Monitor",     "awm:menu"),
    ]],

    # ── 👥 Customers — accounts & customer support (Support moved in from
    # the old "loyalty" grab-bag — it's a customer-facing concern, not a
    # loyalty-program one).
    "customers": [[
        ("👥 Users",               "admin_users"),
        ("📝 Customer CRM",        "crm:home"),
        ("📋 Bulk Users",          "bum:menu"),
        ("⭐ Reviews",             "arv:menu"),
        ("⏳ Pending Reviews",     "arv:list:pending:0"),
        ("🎧 Support",             "admin_tickets"),
    ]],

    # ── 📣 Marketing — everything that reaches out to or rewards customers:
    # broadcast, promotions/coupons, and the loyalty/VIP/referral programs
    # (former "broadcast", "promotions", and most of "loyalty" merged here).
    "marketing": [
        [
            ("📢 Broadcast",           "acc:sec:broadcast"),
            ("📨 Scheduled Broadcast", "asb:menu"),
            ("📣 Announcements",       "ann:menu"),
            ("🎁 Loyalty Points",      "admin_loyalty"),
            ("🏆 VIP Manager",         "vip:menu"),
            ("🎯 Referral Program",    "rd:admin"),
            ("🎉 Promotions",          "acc:sec:promotions"),
            ("⚡ Flash Sales",         "fsm:menu"),
        ],
        [
            ("✂️ Coupons",             "admin_coupons"),
            ("🏷 Coupons (Advanced)",  "acpn:menu"),
            ("⏰ Sub Reminders",       "acc:sec:subrem"),
        ],
    ],

    # ── 🔔 Notifications — new top-level home for every notification
    # surface. "Notification Center" moved in from "broadcast"; "Notification
    # Settings" promoted from a root-panel quick-access shortcut so it now
    # has a proper category home; "Restock Notifications" previously had no
    # menu entry point anywhere in the panel — added here.
    "notifications": [[
        ("🔔 Notification Center",   "anc:menu"),
        ("⚙️ Notification Settings", "nsm:menu"),
        ("📥 Restock Notifications", "rsn:menu"),
    ]],

    # ── 🎨 UI & Menu — everything about how the bot's menus look, split out
    # from general settings so navigation/presentation is its own clear
    # section. "Menu Manager" moved from "system"; "Panel Settings" promoted
    # from a root-panel quick-access shortcut; "Store Preview" moved from
    # "system" (it's a UI preview, not a store config value).
    "ui_menu": [[
        ("🧩 Menu Builder",        "mm:menu"),
        ("😀 Emoji Manager",       "mm:emoji_help"),
        ("🎨 Button Color Manager", "acc:sec:colors"),
        ("🎭 Theme Manager",       "acc:sec:theme"),
        ("🔧 Panel Settings",      "acc:ui:settings"),
        ("👁 Store Preview",       "admin_preview"),
    ]],

    # ── 🏪 Store Settings — customer-facing store configuration, split out
    # of the old catch-all "system" category. "Display Currency" previously
    # had no menu entry point anywhere in the panel — added here.
    "store_settings": [[
        ("🏪 Store Settings",       "admin_settings"),
        ("💱 Display Currency",     "admin_currency"),
        ("🌐 Languages",            "alng:menu"),
        ("🔧 Storefront Features",  "af:menu"),
        ("📱 Account Features",     "aaf:menu"),
    ]],

    # ── 🛡 Security — unchanged from v45 ("API & Integrations" renamed to
    # "Integrations Health" to make clear it's the read-only check; "API
    # Keys" is the actual key-management screen).
    "security": [[
        ("🔍 Fraud Detection",     "fds:home"),
        ("🛡 Anti-Spam",           "aasm:menu"),
        ("📝 Audit Logs",          "acc:sec:audit"),
        ("🔐 Login Activity",      "lam:home"),
        ("🔌 Integrations Health", "acc:sec:integrations"),
        ("🔑 API Keys",            "aim:menu"),
        ("📡 API Status",          "aim:check_all"),
        ("👤 Admin Roles",         "acc:sec:roles"),
    ]],

    # ── ⚙ System — technical/infrastructure configuration and health, split
    # out of the old catch-all "system" category (business/branding settings
    # moved to Store Settings; menu presentation moved to UI & Menu).
    "system": [
        [
            ("⚙️ Bot Settings",        "admin_bot_config"),
            ("🛠 System Tools",        "acc:sec:system"),
            ("📈 System Health",       "acc:sys:health"),
            ("🗄 Database Status",     "acc:sys:db"),
            ("💾 Backup & Restore",    "acc:sec:backups"),
            ("⚡ Performance Monitor", "pcm:menu"),
            ("🧹 Cache Manager",       "pcm:cache"),
            ("🩺 Diagnostics",         "acc:diag:menu"),
        ],
        [
            ("🧩 Modules & Plugins",   "pmm:menu"),
            ("📤 Data Export",         "dec:menu"),
        ],
    ],

    # ── 🧰 Tools — day-to-day admin/dev utilities (Global Search moved in
    # from the old "performance" page; the rest is what's left of the old
    # "tools" category after System took the infrastructure items above).
    "tools": [[
        ("🔩 Maintenance+",        "maint:menu"),
        ("🧪 Quality Control",     "acc:sec:quality"),
        ("🔬 Integrity Scan",      "acc:sec:integrity"),
        ("🤝 Resellers",           "acc:sec:resellers"),
        ("🔍 Global Search",       "gse:menu"),
        ("⚙️ Search Settings",     "gse:settings"),
    ]],
}

_CAT_META: dict[str, tuple[str, str]] = {
    "dashboard":      ("📊", "Dashboard"),
    "products":       ("📦", "Products"),
    "orders":         ("🛒", "Orders"),
    "payments":       ("💳", "Payments"),
    "customers":      ("👥", "Users"),
    "marketing":      ("📣", "Marketing"),
    "notifications":  ("🔔", "Notifications"),
    "ui_menu":        ("🎨", "UI & Menu"),
    "store_settings": ("🏪", "Store"),
    "security":       ("🔒", "Security"),
    "system":         ("⚙️", "System"),
    "tools":          ("🧰", "Tools"),
}

# One-line tagline shown under the breadcrumb on each category's submenu,
# so admins know at a glance what kind of tools live in this section.
_CAT_DESC: dict[str, str] = {
    "dashboard":      "Live stats, revenue &amp; growth metrics at a glance.",
    "products":       "Catalog, discovery, inventory &amp; suppliers.",
    "orders":         "Order queue, search, delivery tracking, refunds &amp; disputes.",
    "payments":       "Gateways, deposits, wallets, FX rates &amp; webhooks.",
    "customers":      "User accounts, CRM, bulk tools, reviews &amp; support.",
    "marketing":      "Broadcasts, promotions, coupons, loyalty, VIP &amp; referrals.",
    "notifications":  "Notification center, delivery settings &amp; restock alerts.",
    "ui_menu":        "Main-menu layout, admin panel appearance &amp; store preview.",
    "store_settings": "Branding, currency, languages &amp; storefront/account features.",
    "security":       "Fraud detection, anti-spam, audit logs, access &amp; API keys.",
    "system":         "Bot config, infrastructure health, backups &amp; performance.",
    "tools":          "Maintenance, quality control, integrity checks &amp; search.",
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



# Root panel groups — Primary (daily operations), Management (marketing &
# presentation), System (infrastructure & configuration). Purely a visual
# grouping of the existing 12 categories; callback_data is untouched.
_ROOT_GROUPS: list[tuple[str, list[str]]] = [
    ("Primary",    ["dashboard", "products", "orders", "payments", "customers"]),
    ("Management", ["marketing", "notifications", "ui_menu"]),
    ("System",     ["store_settings", "security", "system", "tools"]),
]


def build_acc_root_keyboard(maintenance_on: bool,
                            stats: "dict | None" = None) -> IKM:
    """Compact premium root panel — 2-column grid with dynamic status badges.

    Button order and callback_data are fully preserved; only the visual
    presentation (layout, labels, badges) has changed.
    """
    if stats is None:
        stats = {}

    # ── Dynamic badge counts ──────────────────────────────────────────────────
    low_stock         = stats.get("low_stock", 0)
    pending_orders    = stats.get("pending_orders", 0)
    pending_payments  = stats.get("pending_payments", 0)
    open_tickets      = stats.get("open_tickets", 0)

    def _badge(base: str, count: int, suffix: str = "") -> str:
        """Append a parenthetical badge when count > 0."""
        if count:
            tag = f"{count} {suffix}".strip() if suffix else str(count)
            return f"{base} ({tag})"
        return base

    # ── Fixed 2-column grid, exact order from spec ────────────────────────────
    _GRID: list[tuple[str, str, int, str]] = [
        ("dashboard",      "📊 Dashboard",      0,               ""),
        ("products",       "📦 Products",       low_stock,       "Low Stock"),
        ("orders",         "🛒 Orders",         pending_orders,  "Pending"),
        ("payments",       "💳 Payments",       pending_payments,"Pending"),
        ("customers",      "👥 Users",          0,               ""),
        ("marketing",      "📣 Marketing",      0,               ""),
        ("notifications",  "🔔 Notifications",  open_tickets,    ""),
        ("ui_menu",        "🎨 UI & Menu",      0,               ""),
        ("store_settings", "🏪 Store",          0,               ""),
        ("security",       "🔒 Security",       0,               ""),
        ("system",         "⚙️ System",         0,               ""),
        ("tools",          "🧰 Tools",          0,               ""),
    ]

    kb: list[list[IKB]] = []
    row: list[IKB] = []
    for cat, base_label, count, suffix in _GRID:
        label = _badge(base_label, count, suffix)
        row.append(IKB(label, callback_data=f"acc:cat:{cat}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    # ── Search ────────────────────────────────────────────────────────────────
    kb.append([IKB("🔍 Search", callback_data="acc:ui:search")])

    # ── Maintenance toggle ────────────────────────────────────────────────────
    maint_label = (
        "🔴 Maintenance (ON)" if maintenance_on else "🟢 Maintenance (OFF)"
    )
    kb.append([IKB(maint_label, callback_data="admin_maintenance_toggle")])

    # ── Exit ──────────────────────────────────────────────────────────────────
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
        pag.append(IKB("« Prev", callback_data=f"acc:cat:{cat}:{page - 1}"))
    if page < total:
        pag.append(IKB(f"Next »",
                        callback_data=f"acc:cat:{cat}:{page + 1}"))
    if pag:
        kb.append(pag)

    # Back to root
    kb.append([IKB("🏠  Back to Admin Panel", callback_data="acc:root")])
    return IKM(kb)


def _build_favs_keyboard(context: ContextTypes.DEFAULT_TYPE, uid: int) -> IKM:
    nd   = _nav(context, uid)
    favs = nd["favs"]
    kb: list[list[IKB]] = []
    if favs:
        for label, cb in favs:
            kb.append([
                IKB(label, callback_data=cb),
                IKB("✖ Unpin", callback_data=f"acc:ui:unpin:{cb}"),
            ])
    else:
        kb.append([IKB("📭  No favourites pinned yet", callback_data="acc:root")])
    kb.append([IKB("🏠  Back to Admin Panel", callback_data="acc:root")])
    return IKM(kb)


def _build_recent_keyboard(context: ContextTypes.DEFAULT_TYPE, uid: int) -> IKM:
    nd     = _nav(context, uid)
    recent = nd["recent"]
    kb: list[list[IKB]] = []
    if recent:
        for label, cb in recent:
            kb.append([IKB(label, callback_data=cb)])
        kb.append([IKB("🗑  Clear History", callback_data="acc:ui:clear_recent")])
    else:
        kb.append([IKB("📭  No recent menus yet", callback_data="acc:root")])
    kb.append([IKB("🏠  Back to Admin Panel", callback_data="acc:root")])
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
        [IKB(f"{s_icon}  Panel Status: {status.upper()}  →  {s_next_icon} {s_next.upper()}",
             callback_data="acc:ui:set:status")],
        [IKB(f"{_tog_icon('admin_panel_categories', True)}  Category Grid",
             callback_data="acc:ui:tog:admin_panel_categories"),
         IKB(f"{_tog_icon('admin_panel_search', True)}  Search Bar",
             callback_data="acc:ui:tog:admin_panel_search")],
        [IKB(f"{_tog_icon('admin_panel_compact', False)}  Compact Mode",
             callback_data="acc:ui:tog:admin_panel_compact"),
         IKB(f"{_tog_icon('admin_panel_icons', True)}  Icons",
             callback_data="acc:ui:tog:admin_panel_icons")],
        [IKB(f"{_tog_icon('admin_panel_breadcrumb', True)}  Breadcrumb Navigation",
             callback_data="acc:ui:tog:admin_panel_breadcrumb")],
        [IKB("🏠  Back to Admin Panel", callback_data="acc:root")],
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
    kb    = build_acc_root_keyboard(cfg.get_bool("maintenance_mode", False), stats=stats)

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
        breadcrumb = f"🏠 Admin  ›  {cat_icon} <b>{cat_name}</b>  ·  {page} / {total}"
    elif use_bc:
        breadcrumb = f"🏠 Admin  ›  {cat_icon} <b>{cat_name}</b>"
    else:
        breadcrumb = f"{cat_icon} <b>{cat_name}</b>"

    tagline = _CAT_DESC.get(cat, "")
    lines   = [breadcrumb]
    if tagline:
        lines.append(f"<i>{tagline}</i>")
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
    if section == "theme":
        await _render_theme_manager(update, context); return
    if section == "colors":
        await _render_button_color_manager(update, context); return
    if section == "roles":
        await _render_admin_roles(update, context); return
    # Unknown → root
    await render_control_center(update, context)


# ═══════════════════════════════════════════════════════════════════════════════
# New management screens (v47) — Theme Manager, Button Color Manager, Admin
# Roles. Each is a thin presentation layer: every toggle/button below calls
# an existing, unchanged handler (menu_colors.* / permissions.list_admins).
# No new business logic is introduced anywhere in this section.
# ═══════════════════════════════════════════════════════════════════════════════

async def _render_theme_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🎭 Theme Manager — single hub for every visual/appearance control in
    the bot. Pure navigation: each button below opens an existing, already-
    working screen (colors, emoji, panel appearance, live preview)."""
    query = update.callback_query
    from utils.bot_config import cfg
    all_colors_on = cfg.get_bool("global_button_colors_enabled", True)
    icons_on = cfg.get_bool("admin_panel_icons", True)
    text_ = (
        "🎭 <b>Theme Manager</b>\n\n"
        "Central hub for everything that controls how the bot looks.\n\n"
        f"🌈 Bot-wide button colors: <b>{'ON' if all_colors_on else 'OFF'}</b>\n"
        f"🔤 Admin panel icons: <b>{'ON' if icons_on else 'OFF'}</b>\n\n"
        "Pick an area to customize:"
    )
    kb = [
        [IKB("🎨 Button Color Manager", callback_data="acc:sec:colors")],
        [IKB("😀 Emoji Manager",        callback_data="mm:emoji_help")],
        [IKB("🧩 Menu Builder",         callback_data="mm:menu")],
        [IKB("🔧 Panel Settings",       callback_data="acc:ui:settings")],
        [IKB("👁 Store Preview",        callback_data="admin_preview")],
        [IKB("🔙 Back",                 callback_data="acc:cat:ui_menu")],
    ]
    await _safe_edit(query, text_, IKM(kb))


async def _render_button_color_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🎨 Button Color Manager — dedicated screen for the color controls
    that already exist in Menu Manager (handlers/menu_colors.py). Every
    button below uses the exact same callback_data as before, so it routes
    to the same unchanged handler functions."""
    query = update.callback_query
    from utils.bot_config import cfg
    from handlers.menu_state import active_audience
    from utils.menu_registry import get_menu_layout, MENU_AUDIENCE_LABELS

    uid = update.effective_user.id
    audience = active_audience(context)
    colors_on = bool(get_menu_layout(audience).get("colors_enabled", True))
    all_colors_on = cfg.get_bool("global_button_colors_enabled", True)

    text_ = (
        "🎨 <b>Button Color Manager</b>\n\n"
        f"👥 Profile: <b>{MENU_AUDIENCE_LABELS[audience]}</b>\n"
        f"🎨 This profile's menu colors: <b>{'ON' if colors_on else 'OFF'}</b>\n"
        f"🌈 All bot buttons (every keyboard): <b>{'ON' if all_colors_on else 'OFF'}</b>\n\n"
        "Toggle colors on/off instantly, or reset back to defaults. "
        "Per-item colors are still edited from Menu Builder → tap an item."
    )
    kb = [
        [IKB(f"🎨 This Profile: {'✅ ON' if colors_on else '🚫 OFF'}",
             callback_data="mm:colors_toggle"),
         IKB(f"🌈 All Buttons: {'✅ ON' if all_colors_on else '🚫 OFF'}",
             callback_data="mm:all_colors_toggle")],
        [IKB("🔁 Reset This Profile's Colors", callback_data="mm:colors_reset")],
        [IKB("♻ Reset All Colors Everywhere",  callback_data="mm:all_colors_reset")],
        [IKB("🧩 Open Menu Builder",            callback_data="mm:menu")],
        [IKB("🔙 Back",                         callback_data="acc:sec:theme")],
    ]
    await _safe_edit(query, text_, IKM(kb))


async def _render_admin_roles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """👤 Admin Roles — read-only roster view built on top of the existing,
    unchanged utils.permissions.list_admins(). Role changes still go through
    the existing /admin_add, /admin_role, /admin_remove commands (unchanged
    business logic) — this screen only adds visibility inside the panel."""
    query = update.callback_query
    from utils.permissions import list_admins, is_super_admin

    uid = update.effective_user.id
    admins = list_admins(include_inactive=is_super_admin(uid))
    role_icon = {"super_admin": "👑", "moderator": "🛡", "support_staff": "🎧"}

    if not admins:
        lines = ["<i>No admins registered yet (besides the bootstrap owner).</i>"]
    else:
        lines = []
        for a in admins:
            icon = role_icon.get(a["role"], "•")
            status = "" if a["is_active"] else " (inactive)"
            uname = f"@{a['username']}" if a["username"] else str(a["telegram_id"])
            lines.append(f"{icon} <code>{a['telegram_id']}</code> {uname} — <b>{a['role']}</b>{status}")

    text_ = (
        "👤 <b>Admin Roles</b>\n\n"
        + "\n".join(lines)
        + "\n\n"
        "To add, re-role, or remove an admin use:\n"
        "<code>/admin_add &lt;telegram_id&gt; &lt;role&gt;</code>\n"
        "<code>/admin_role &lt;telegram_id&gt; &lt;role&gt;</code>\n"
        "<code>/admin_remove &lt;telegram_id&gt;</code>\n"
        "(super_admin, moderator, support_staff)"
    )
    kb = [[IKB("🔙 Back", callback_data="acc:root")]]
    await _safe_edit(query, text_, IKM(kb))


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
                [IKB("🔙 Back", callback_data="acc:root"),
                 IKB("🏠 Admin", callback_data="acc:root")],
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
