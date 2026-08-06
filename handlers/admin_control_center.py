"""Premium Admin Control Center — categorized navigation (v47).

Root shows 24 enterprise categories (Dashboard, Products, Orders, Payments,
Wallet, Customers, Coupons, Referrals, Marketing, Support, Appearance,
Store, Localization, Notifications, Security, Admins, System, Tools,
Backup, Analytics, Templates, Automation, Logs, API Manager) + Favorites /
Recent + Global Search / Settings Search + Maintenance toggle + Exit Admin.
Each category opens a paginated submenu (≤8 items + Back/Home per page),
so navigation never exceeds Admin Panel → Category → Feature (3 levels).

Callback namespace
──────────────────
  acc:root                — root panel (categories)
  acc:cat:<name>          — category submenu page 1
  acc:cat:<name>:<page>   — category submenu page N
  acc:ui:search           — admin quick search → existing gse:menu
  acc:ui:ssearch          — Settings Search (searches _CAT_PAGES only, new)
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
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

from utils.permissions import has_permission
from utils.perf import perf_track
from utils import nav_state

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Category definitions — Enterprise structure (v46)
#
# 24 top-level sections, matching the approved enterprise information
# architecture. Every existing feature keeps its original callback_data —
# only *which category dict it's listed under* changed, so no routing,
# handler, or business logic was touched anywhere in the bot.
#
# Each category is a list of pages; each page is a list of (label, callback_data)
# Maximum 8 items per page so that page + Back/pagination ≤ 10 buttons.
# ═══════════════════════════════════════════════════════════════════════════════

_CAT_PAGES: dict[str, list[list[tuple[str, str]]]] = {

    # ── ⚡ Dashboard — live KPIs + 1-tap quick actions. Analytics/forecast
    # screens moved out to their own 📈 Analytics category (below) so this
    # page stays pure "what needs my attention right now". Every quick
    # action reuses its existing, unchanged callback_data.
    "dashboard": [[
        ("📊 Live Dashboard",      "acc:sec:dashboard"),
        ("📉 Low Stock Alert",     "admin_low_stock"),
        ("🛒 Pending Orders",      "admin_orders"),
        ("🧾 Pending Deposits",    "pd:list:0:desc"),
        ("🎧 Open Tickets",        "admin_tickets"),
    ]],

    # ── 📈 Analytics — split out of the old "dashboard" category so KPIs
    # (above) and deep-dive reporting (here) aren't mixed on one page.
    # Sales Graph, Top Products, Top Customers, Conversion Rate and Payment
    # Statistics are sub-views inside Analytics Overview / Advanced
    # Analytics rather than separate top-level buttons.
    "analytics": [[
        ("📈 Analytics Overview",  "admin_analytics"),
        ("🔬 Advanced Analytics",  "aana:menu"),
        ("💼 Business Insights",   "abiz:menu"),
        ("📉 Sales Forecast",      "asf:menu"),
        ("💹 Profit",              "acc:sec:profit"),
        ("📈 Growth & LTV",        "admin_analytics_cohort"),
    ]],

    # ── 📦 Products — catalog, discovery, inventory & suppliers all live
    # under one roof. Unchanged from v46 other than a "Badges & Featured"
    # entry, which reuses the existing per-product toggle (there's no
    # separate badges list screen in the codebase).
    # NOTE: intentionally a single page (all 21 items together) per explicit
    # request — overrides the "≤8 items/page" guideline for this category only.
    "products": [
        [
            ("📦 Product List",        "admin_products"),
            ("🗂 Categories",          "admin_manage_categories"),
            ("🎀 Bundles",             "abn:menu"),
            ("🎟 Gift Cards",          "agc:menu"),
            ("🗂 Template Manager",    "apt:menu"),
            ("📄 Clone Products",      "pct:menu"),
            ("⭐ Badges & Featured",    "admin_products"),
            ("❓ Product FAQ",          "acc:sec:pfaq"),
            ("⚖️ Product Compare",      "acc:sec:pcmp"),
            ("❤️ Favorites",            "acc:sec:favs"),
            ("🕒 Recently Viewed",      "acc:sec:rvw"),
            ("📥 Inventory",           "admin_restock_keys"),
            ("📂 Batches",             "acc:sec:batches"),
            ("🏷 Price History",       "acc:sec:ph"),
            ("⏳ Reservation",         "acc:sec:irs"),
            ("⚡ Bulk Import / Export", "bpim:menu"),
            ("📉 Stock Alerts",        "admin_low_stock"),
            ("🏭 Suppliers",           "acc:sec:suppliers"),
            ("📦 Digital Delivery",    "flm:menu"),
            ("🚚 Delivery Manager",    "dms:menu"),
            ("📖 Product Info Builder", "pib:admin:products:0"),
        ],
    ],

    # ── 🛒 Orders — order lifecycle, incl. disputes. Resend Delivery and
    # Manual Complete are per-order actions inside Orders / Delivery Queue,
    # not separate top-level buttons.
    "orders": [[
        ("🧾 Orders (All / Pending / Completed)", "admin_orders"),
        ("🔎 Search Orders",       "aos:menu"),
        ("📬 Delivery Queue",      "acc:sec:delivery"),
        ("💰 Refunds / Cancellations", "aref:menu"),
        ("🛍 Gift Purchase",       "agp:menu"),
        ("⚠️ Disputes",            "admin_view_disputes"),
    ]],

    # ── 💳 Payments — gateways & deposits. Wallet balances/manual credit and
    # multi-currency/FX now live in their own 💰 Wallet category (below);
    # log-style screens (webhooks/payment logs) moved to 📜 Logs so this
    # category stays focused on "how customers pay", not history/records.
    "payments": [[
        ("💳 Payment Gateways",    "admin_gateways"),
        ("🏦 Manual Payment Methods", "admin_payment_methods"),
        ("🧾 Deposit Requests",    "pd:list:0:desc"),
    ]],

    # ── 💰 Wallet — customer wallet balances, manual credit/debit,
    # multi-currency wallets and FX rates. Split out of Payments so wallet
    # administration has its own front door, matching the customer-facing
    # Wallet feature.
    "wallet": [[
        ("👛 Wallets / Manual Credit", "acc:sec:wallets"),
        ("🌍 Multi-Currency Wallets",  "amcw:menu"),
        ("🔄 Exchange Rates",          "aerm:menu"),
    ]],

    # ── 👥 Customers — accounts, CRM & bulk tools. Support moved out to its
    # own 🎧 Support category (below); Ban/Unban, User Purchases, User Notes
    # and User Groups live inside User List / Customer CRM detail screens.
    "users": [[
        ("👥 User List / Search",  "admin_users"),
        ("📝 Customer CRM",        "crm:home"),
        ("📋 Bulk Users / Export", "bum:menu"),
        ("⭐ Reviews",             "arv:menu"),
        ("⏳ Pending Reviews",     "arv:list:pending:0"),
    ]],

    # ── 🎟 Coupons — split out of the old "marketing" grab-bag into its own
    # top-level home. Active/Expired/Usage History are views inside these
    # two screens.
    "coupons": [[
        ("✂️ Coupons",             "admin_coupons"),
        ("🏷 Advanced / Auto Coupons", "acpn:menu"),
    ]],

    # ── 🎁 Referral — split out of "marketing" into its own top-level home.
    # Commission, Rewards, Statistics and History are sections inside the
    # Referral Program screen.
    "referral": [[
        ("🎯 Referral Program",    "rd:admin"),
    ]],

    # ── 📣 Marketing — broadcast, promotions, loyalty, VIP & flash sales
    # (Coupons and Referral now live in their own categories above; recurring
    # / scheduled jobs now live in 🤖 Automation, below).
    # No standalone Banner Manager or Push Notifications screen exists yet —
    # banner images are set per-campaign inside Flash Sales, and outbound
    # pushes go through Broadcast / Notification Center.
    "marketing": [[
        ("📢 Broadcast",           "acc:sec:broadcast"),
        ("📣 Popup Announcements", "ann:menu"),
        ("🎁 Loyalty Points",      "admin_loyalty"),
        ("🏆 VIP Manager",         "vip:menu"),
        ("🎉 Promotions",          "acc:sec:promotions"),
        ("⚡ Flash Sales",         "fsm:menu"),
    ]],

    # ── 🤖 Automation — recurring/scheduled jobs that run without a human
    # tapping a button each time. Auto Assign moved from Orders, Scheduled
    # Broadcast moved from Marketing, Subscription Reminders moved from
    # Marketing; Marketing Automation (cart/win-back reminders) is an
    # existing screen that previously had no front door in this panel at
    # all (it was only reachable by drilling into Broadcast) — surfaced
    # here directly, same unchanged callback. Auto Backup and Auto Reports
    # don't have dedicated admin screens in the codebase yet — flagged as
    # gaps, nothing fabricated.
    "automation": [[
        ("📨 Scheduled Broadcast", "asb:menu"),
        ("🛒 Marketing Automation (Cart/Win-back)", "acc:bc:mkt:menu"),
        ("🤖 Auto Assign Orders",  "acc:sec:sas"),
        ("⏰ Subscription Reminders", "acc:sec:subrem"),
    ]],

    # ── 🎨 Appearance — everything about how the bot looks. Store Logo and
    # Welcome Message live inside the 🏪 Store category (below) — they're
    # fields on the same Store Settings screen, so listing them here too
    # would give that one screen two parents. No dedicated Font Style
    # control exists in the codebase — flagged as a gap.
    "appearance": [[
        ("🧩 Main Menu Builder",   "mm:menu"),
        ("🔘 Button Manager",      "mm:menu"),
        ("😀 Emoji Manager",       "mm:emoji_help"),
        ("🎨 Color Manager",       "acc:sec:colors"),
        ("🎭 Theme",               "acc:sec:theme"),
        ("👁 Live Preview",        "admin_preview"),
        ("🔧 Panel Settings",      "acc:ui:settings"),
    ]],

    # ── 🏪 Store — customer-facing store configuration. This is the single,
    # canonical home for the Store Settings screen (Store Logo, Welcome
    # Message, Support Username, Channel, Referral Reward/Toggle, Delivery
    # Message Builder, Account Delivery Settings all live inside it) —
    # nothing else in the panel links to "admin_settings" anymore, so this
    # screen now has exactly one parent.
    "store": [[
        ("🏪 Store Settings (Name, Logo, Welcome Msg)", "admin_settings"),
        ("🔧 Storefront Features", "af:menu"),
        ("📱 Account Features",    "aaf:menu"),
    ]],

    # ── 🌍 Localization — split out of Store so language/currency admin has
    # its own home. No dedicated Timezone or Date Format screen exists in
    # the codebase yet (dates are rendered server-side) — flagged as a gap.
    "localization": [[
        ("🌐 Languages",           "alng:menu"),
        ("💱 Currency",            "admin_currency"),
    ]],

    # ── 🔔 Notifications — unchanged from v46. Order/Payment/Deposit/Ticket/
    # Admin alert toggles and Log Channel live inside Notification Settings.
    "notifications": [[
        ("🔔 Notification Center", "anc:menu"),
        ("⚙️ Notification Settings", "nsm:menu"),
        ("📥 Restock Notifications", "rsn:menu"),
    ]],

    # ── 🎧 Support — split out of "users" into its own top-level home.
    # Product FAQ stays single-parented under 📦 Products (it's product-
    # specific, not a general store FAQ). Support Categories exist in the
    # ticket flow (category picker) but aren't yet admin-editable; a
    # general Store FAQ, Auto Reply and Canned Replies don't exist in the
    # codebase yet — flagged as gaps, nothing fabricated here.
    "support": [[
        ("🎫 Ticket System",       "admin_tickets"),
    ]],

    # ── 🔐 Security — threat detection & access control. Admin roster moved
    # to its own 👨‍💼 Admins category, API keys/status moved to 🌐 API
    # Manager, and Audit Logs moved to 📜 Logs, so Security stays focused
    # on fraud/spam/session protection. Session Manager lives inside Login
    # Activity (it already lists/terminates active sessions).
    "security": [[
        ("🔍 Fraud Detection",     "fds:home"),
        ("🛡 Anti-Spam",           "aasm:menu"),
        ("🔐 Login Activity / Sessions", "lam:home"),
        ("🔌 Integrations Health", "acc:sec:integrations"),
    ]],

    # ── 👨‍💼 Admins — admin roster & permissions. Add/re-role/remove still
    # go through the existing /admin_add, /admin_role, /admin_remove
    # commands (unchanged) — this screen (moved from Security) is the
    # existing read-only roster + permissions view.
    "admins": [[
        ("👤 Admin Roles & Permissions", "acc:sec:roles"),
    ]],

    # ── 🌐 API Manager — split out of Security into its own home.
    "api": [[
        ("🔑 API Keys",            "aim:menu"),
        ("📡 API Status",          "aim:check_all"),
    ]],

    # ── 📜 Logs — read-only history/audit trails, pulled together from
    # across the panel (Audit Logs from Security, Recent Activity from
    # Dashboard, Order Timeline from Orders, Webhooks & Payment Logs from
    # Payments) so there's one place to review "what happened", separate
    # from the day-to-day action screens. No separate System/Error log
    # viewer exists yet beyond these — flagged as a gap.
    "logs": [[
        ("📝 Audit Logs",          "acc:sec:audit"),
        ("📜 Recent Activity / Global Timeline", "gat:menu"),
        ("⏱ Order Timeline",      "acc:sec:ots"),
        ("🔌 Webhooks & Payment Logs", "awm:menu"),
    ]],

    # ── 📝 Templates — message templates that are actually editable from
    # the panel today. Order/Deposit/Ticket/Coupon/Referral notifications
    # use fixed formatting (utils/notify_format.py) rather than admin-
    # editable templates, so only Delivery Message Builder is listed here
    # — flagged as a gap rather than fabricated.
    "templates": [[
        ("📦 Delivery Message Builder", "dmb:menu"),
    ]],

    # ── ⚙ System — infrastructure config & health (Backup split out to its
    # own category below).
    "system": [[
        ("⚙️ Bot Settings / Environment", "admin_bot_config"),
        ("🛠 System Tools",        "acc:sec:system"),
        ("📈 System Health / Version", "acc:sys:health"),
        ("🗄 Database Status",     "acc:sys:db"),
        ("⚡ Performance / Scheduler", "pcm:menu"),
        ("🧹 Cache Manager",       "pcm:cache"),
        ("🩺 Diagnostics / Health Check", "acc:diag:menu"),
        ("🧩 Modules & Plugins",   "pmm:menu"),
    ]],

    # ── 📂 Backup — split out of "system" into its own top-level home.
    # DB Dump, Settings Backup (with Import/Export) all live inside this
    # one screen already.
    "backup": [[
        ("💾 Backup & Restore",    "acc:sec:backups"),
        ("📤 Data Export",         "dec:menu"),
    ]],

    # ── 🧰 Tools — day-to-day admin/dev utilities. Global Search itself is
    # now a root-panel icon (below); Search Settings stays here.
    "tools": [[
        ("🔩 Maintenance+",        "maint:menu"),
        ("🧪 Quality Control",     "acc:sec:quality"),
        ("🔬 Integrity / Diagnostics Scan", "acc:sec:integrity"),
        ("🤝 Resellers",           "acc:sec:resellers"),
        ("⚙️ Search Settings",     "gse:settings"),
    ]],
}

_CAT_META: dict[str, tuple[str, str]] = {
    "dashboard":      ("⚡", "Dashboard"),
    "analytics":      ("📈", "Analytics"),
    "products":       ("📦", "Products"),
    "orders":         ("🛒", "Orders"),
    "payments":       ("💳", "Payments"),
    "wallet":         ("💰", "Wallet"),
    "users":          ("👥", "Customers"),
    "coupons":        ("🎟", "Coupons"),
    "referral":       ("🎁", "Referrals"),
    "marketing":      ("📢", "Marketing"),
    "automation":     ("🤖", "Automation"),
    "support":        ("🎫", "Support"),
    "appearance":     ("🎨", "Appearance"),
    "store":          ("🏪", "Store"),
    "localization":   ("🌍", "Localization"),
    "notifications":  ("🔔", "Notifications"),
    "security":       ("🔐", "Security"),
    "admins":         ("👨‍💼", "Admins"),
    "system":         ("⚙️", "System"),
    "tools":          ("🧰", "Tools"),
    "backup":         ("📂", "Backup"),
    "templates":      ("📝", "Templates"),
    "logs":           ("📜", "Logs"),
    "api":            ("🌐", "API Manager"),
}

# One-line tagline shown under the breadcrumb on each category's submenu,
# so admins know at a glance what kind of tools live in this section.
_CAT_DESC: dict[str, str] = {
    "dashboard":      "Live KPIs &amp; 1-tap quick actions for daily ops.",
    "analytics":      "Deep-dive reporting, forecasts &amp; growth metrics.",
    "products":       "Catalog, discovery, inventory &amp; suppliers.",
    "orders":         "Order queue, search, delivery tracking, refunds &amp; disputes.",
    "payments":       "Gateways, manual methods &amp; deposit requests.",
    "wallet":         "Customer wallet balances, manual credit/debit, multi-currency &amp; FX rates.",
    "users":          "Customer accounts, CRM, bulk tools &amp; reviews.",
    "coupons":        "Create, track &amp; retire discount codes.",
    "referral":       "Referral program, commissions &amp; rewards.",
    "marketing":      "Broadcasts, promotions, loyalty, VIP &amp; flash sales.",
    "automation":     "Scheduled/recurring jobs: broadcasts, cart &amp; win-back reminders, auto-assign, subscription reminders.",
    "appearance":     "Branding, menus, buttons, colors, theme &amp; preview.",
    "store":          "Store identity &amp; storefront/account features. (Language &amp; currency live in Localization.)",
    "localization":   "Languages &amp; currency.",
    "notifications":  "Notification center, delivery settings &amp; restock alerts.",
    "support":        "Ticket system. (Product FAQ lives in Products.)",
    "security":       "Fraud detection, anti-spam &amp; session/login protection.",
    "admins":         "Admin roster &amp; permissions.",
    "system":         "Bot config, infrastructure health &amp; performance.",
    "backup":         "Database &amp; settings backup, restore, import/export.",
    "tools":          "Maintenance, quality control, integrity checks &amp; search config.",
    "templates":      "Editable message templates.",
    "logs":           "Audit logs, activity timeline, order timeline &amp; webhook/payment logs.",
    "api":            "API keys &amp; live API status.",
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

# ═══════════════════════════════════════════════════════════════════════════════
# Settings Search — independent from Global Search (gse:*), searches only
# the settings/menu items in _CAT_PAGES. Built once at import time.
# ═══════════════════════════════════════════════════════════════════════════════

_SSEARCH_INDEX: list[tuple[str, str, str, str]] = []  # (category_name, emoji, label, cb)
for _cat, _pages in _CAT_PAGES.items():
    _icon, _name = _CAT_META.get(_cat, ("📋", _cat.title()))
    for _page in _pages:
        for _label, _cb in _page:
            _SSEARCH_INDEX.append((_name, _icon, _label, _cb))

SSEARCH_QUERY = 950  # ConversationHandler state for Settings Search


def _ssearch(query: str, limit: int = 15) -> list[tuple[str, str, str, str]]:
    """Rank-search _SSEARCH_INDEX. Never touches _CAT_PAGES."""
    q = query.strip().lower()
    if not q:
        return []
    words = q.split()

    exact, starts, label_hit, cat_hit, other = [], [], [], [], []
    for name, icon, label, cb in _SSEARCH_INDEX:
        label_l, cat_l = label.lower(), name.lower()
        if label_l == q:
            exact.append((name, icon, label, cb))
        elif label_l.startswith(q):
            starts.append((name, icon, label, cb))
        elif q in label_l:
            label_hit.append((name, icon, label, cb))
        elif q in cat_l:
            cat_hit.append((name, icon, label, cb))
        elif all(w in label_l or w in cat_l for w in words):
            other.append((name, icon, label, cb))

    seen: set[str] = set()
    results: list[tuple[str, str, str, str]] = []
    for item in exact + starts + label_hit + cat_hit + other:
        if item[3] in seen:
            continue
        seen.add(item[3])
        results.append(item)
        if len(results) >= limit:
            break
    return results


async def ssearch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for acc:ui:ssearch — prompts admin for a keyword."""
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "view_analytics"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await _safe_edit(
        query,
        "🔍 <b>Search Settings</b>\n\n"
        "Type a keyword to search settings.\n\n"
        "Examples:\n"
        "<code>delivery</code>  <code>payment</code>  <code>theme</code>\n"
        "<code>wallet</code>  <code>language</code>",
        IKM([[IKB("❌ Cancel", callback_data="acc:root")]]),
    )
    return SSEARCH_QUERY


async def ssearch_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the typed keyword, shows ranked results, ends the conversation."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("🔍 Type a keyword to search settings.")
        return SSEARCH_QUERY

    results = _ssearch(text)

    if not results:
        await update.message.reply_text(
            "🔍 <b>Search Settings</b>\n\n"
            "❌ কিছু পাওয়া যায়নি।\n\n"
            "বানান পরীক্ষা করুন অথবা অন্য একটি শব্দ ব্যবহার করুন।",
            parse_mode="HTML",
            reply_markup=IKM([
                [IKB("🔄 Search Again", callback_data="acc:ui:ssearch")],
                [IKB("⬅ Back", callback_data="acc:root")],
            ]),
        )
        return ConversationHandler.END

    kb = [[IKB(f"{icon} {label}", callback_data=cb)] for _, icon, label, cb in results]
    kb.append([IKB("🔄 Search Again", callback_data="acc:ui:ssearch")])
    kb.append([IKB("⬅ Back", callback_data="acc:root")])

    await update.message.reply_text(
        f"🔍 <b>Search Results</b>\n\nFound: {len(results)} result(s)",
        parse_mode="HTML",
        reply_markup=IKM(kb),
    )
    return ConversationHandler.END


async def ssearch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel/Back handler while the search conversation is active."""
    if update.callback_query:
        await update.callback_query.answer()
    await render_control_center(update, context)
    return ConversationHandler.END


def build_ssearch_conversation() -> ConversationHandler:
    """Settings Search conversation — registered separately in bot.py,
    independent from the existing Global Search (gse:*) conversation."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ssearch_start, pattern=r"^acc:ui:ssearch$")],
        states={
            SSEARCH_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ssearch_recv),
                CallbackQueryHandler(ssearch_cancel, pattern=r"^acc:root$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(ssearch_cancel, pattern=r"^acc:root$"),
            CommandHandler("cancel", ssearch_cancel),
        ],
        per_message=False,
        allow_reentry=True,
        name="acc_ssearch",
    )


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



# Root panel groups — Primary (daily operations), Growth (marketing &
# presentation), System (infrastructure & configuration). Purely a visual
# grouping of the 24 categories below; callback_data is untouched.
_ROOT_GROUPS: list[tuple[str, list[str]]] = [
    ("Primary", ["dashboard", "analytics", "products", "orders", "payments", "users"]),
    ("Growth",  ["coupons", "referral", "marketing", "appearance", "store", "notifications", "support"]),
    ("System",  ["security", "system", "backup", "tools"]),
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

    # ── Fixed 2-column grid, 24 categories — Enterprise Marketplace Control
    # Center layout. Every callback_data is an existing acc:cat:<name>
    # route; only which bucket a feature is grouped under changed.
    _GRID: list[tuple[str, str, int, str]] = [
        ("dashboard",      "⚡ Dashboard",      0,               ""),
        ("products",       "📦 Products",       low_stock,       "Low Stock"),
        ("orders",         "🛒 Orders",         pending_orders,  "Pending"),
        ("payments",       "💳 Payments",       pending_payments,"Pending"),
        ("users",          "👥 Customers",      0,               ""),
        ("wallet",         "💰 Wallet",         0,               ""),
        ("coupons",        "🎟 Coupons",        0,               ""),
        ("referral",       "🎁 Referrals",      0,               ""),
        ("marketing",      "📢 Marketing",      0,               ""),
        ("support",        "🎫 Support",        open_tickets,    "Open"),
        ("appearance",     "🎨 Appearance",     0,               ""),
        ("store",          "🏪 Store",          0,               ""),
        ("localization",   "🌍 Localization",   0,               ""),
        ("notifications",  "🔔 Notifications",  0,               ""),
        ("security",       "🔐 Security",       0,               ""),
        ("admins",         "👨‍💼 Admins",        0,               ""),
        ("system",         "⚙️ System",         0,               ""),
        ("tools",          "🧰 Tools",          0,               ""),
        ("backup",         "📂 Backup",         0,               ""),
        ("analytics",      "📈 Analytics",      0,               ""),
        ("templates",      "📝 Templates",      0,               ""),
        ("automation",     "🤖 Automation",     0,               ""),
        ("logs",           "📜 Logs",           0,               ""),
        ("api",            "🌐 API Manager",    0,               ""),
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

    # ── Utility row 1 — Favorites doubles as Pinned Settings (acc:ui:favs
    # already pins/unpins arbitrary callbacks); Recent shows last-visited
    # screens. Both reuse existing, unchanged backend logic.
    kb.append([
        IKB("⭐ Favorites / 📌 Pinned", callback_data="acc:ui:favs"),
        IKB("🕐 Recent", callback_data="acc:ui:recent"),
    ])

    # ── Utility row 2 — Search ─────────────────────────────────────────────────
    # "Global Search" = data search (orders/users/products) via existing
    # Global Search module. "Search Settings" = searches only _CAT_PAGES
    # (admin settings/menus).
    kb.append([
        IKB("🔍 Global Search", callback_data="acc:ui:search"),
        IKB("⚙️ Search Settings", callback_data="acc:ui:ssearch"),
    ])

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
        # Setting + pin toggle per row (compact layout keeps its own
        # toggle/flag working; pin exposure is identical in shape to
        # non-compact so every setting gets a reachable pin button).
        for label, cb in items:
            pin_icon = "★" if _is_fav(context, uid, cb) else "⭐"
            kb.append([
                IKB(label, callback_data=cb),
                IKB(pin_icon, callback_data=f"acc:ui:pin:{cb}"),
            ])
    else:
        # One setting + pin toggle per row (cleaner on mobile)
        for label, cb in items:
            pin_icon = "★" if _is_fav(context, uid, cb) else "⭐"
            kb.append([
                IKB(label, callback_data=cb),
                IKB(pin_icon, callback_data=f"acc:ui:pin:{cb}"),
            ])

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

    # Root of the breadcrumb trail — every deeper screen's Back chain
    # (including external ones like Bot Configuration) resolves up to
    # this frame instead of a hardcoded destination.
    nav_state.enter_screen(context, "acc:root")

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

    # Remember where the admin currently is, so pin/unpin (Task 5) can
    # refresh this exact message in place instead of jumping to root.
    _nav(context, uid)["cur"] = (cat, page)

    # Push this category page onto the breadcrumb stack. Screens that are
    # reached *from* here (e.g. Bot Configuration under "system", Search
    # Settings under "tools") are rendered by other handler modules, but
    # they share this same per-user stack, so their Back button can look
    # up "whatever was open immediately before me" instead of guessing.
    nav_state.enter_screen(context, f"acc:cat:{cat}")

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

        nd         = _nav(context, uid)
        was_pinned = _is_fav(context, uid, cb)
        if not was_pinned and len(nd["favs"]) >= _MAX_FAVS:
            # _toggle_fav() itself no-ops silently at the cap — surface an
            # honest warning here rather than a misleading "Pinned!" toast.
            await query.answer(
                f"⚠️ Max {_MAX_FAVS} favorites reached. Unpin one first.",
                show_alert=True,
            )
        else:
            _toggle_fav(context, uid, label, cb)
            now_pinned = _is_fav(context, uid, cb)
            await query.answer("⭐ Pinned!" if now_pinned else "✅ Unpinned", show_alert=False)

        # Immediate in-place refresh (⭐ ↔ ★) — no new message, no jump to
        # root. Falls back to the root panel only if we don't know where
        # the admin currently is (should not normally happen, since the
        # pin button only appears inside a category page).
        cur = _nav(context, uid).get("cur")
        if cur:
            cat, page = cur
            new_kb = _build_category_keyboard(cat, page, uid, context)
            try:
                await query.edit_message_reply_markup(reply_markup=new_kb)
            except BadRequest:
                pass
        else:
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
    from utils.button_colors import global_colors_enabled
    all_colors_on = global_colors_enabled()
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
    from utils.button_colors import global_colors_enabled
    from handlers.menu_state import active_audience
    from utils.menu_registry import get_menu_layout, MENU_AUDIENCE_LABELS

    uid = update.effective_user.id
    audience = active_audience(context)
    colors_on = bool(get_menu_layout(audience).get("colors_enabled", True))
    all_colors_on = global_colors_enabled()

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
