"""Enterprise Product Template System — V46.

Standalone reusable templates for all digital product types.
Admin can create, edit, duplicate, archive, delete, preview,
import / export, mark as default, and view usage statistics.

Callback namespace: ``apt:*``

Callbacks handled
─────────────────
apt:menu                     — Dashboard / main menu
apt:list                     — Active template list (page 0)
apt:list:<page>              — Paginated list
apt:arch_list                — Archived list (page 0)
apt:arch_list:<page>         — Paginated archived list
apt:new                      — Start creation wizard (conv entry)
apt:new:type:<TYPE>          — Pick product type during creation (conv)
apt:view:<id>                — Template detail view
apt:edit:<id>                — Edit menu
apt:edit:<id>:f:<field>      — Edit single field (conv entry)
apt:dup:<id>                 — Duplicate
apt:del_ask:<id>             — Delete confirmation
apt:del_ok:<id>              — Execute delete
apt:archive:<id>             — Archive
apt:restore:<id>             — Restore from archive
apt:set_default:<id>         — Mark as default for its type
apt:unset_default:<id>       — Unmark default
apt:preview:<id>             — Preview rendered product card
apt:stats                    — Usage statistics dashboard
apt:export                   — Export all templates as JSON
apt:import                   — Import from JSON file (conv entry)
apt:filter:<type|all>        — Filter list by product type
apt:sort:<field>             — Sort list (name|price|used|created)
apt:search                   — Free-text search (conv entry)
apt:search_res:<query>:<pg>  — Paginated search results
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import (
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    InputFile,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import get_db_session
from database.models import ProductTemplate, ProductType
from utils.audit import log_admin_action
from utils.bot_config import cfg
from utils.helpers import is_admin
from utils.permissions import has_permission
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

PAGE_SIZE = 8

# ── Conversation states ────────────────────────────────────────────────────────
APT_CREATE_NAME = 0
APT_CREATE_DESC = 1
APT_CREATE_PRICE = 2
APT_EDIT_FIELD  = 3
APT_WAIT_IMPORT = 4
APT_SEARCH      = 5


# ── Custom delivery field definitions per product type ────────────────────────

_CUSTOM_FIELDS: Dict[str, List[tuple]] = {
    "KEY": [
        ("license_key",   "License Key"),
        ("download_link", "Download Link"),
        ("version",       "Version"),
        ("platform",      "Platform"),
    ],
    "REDEEM_LINK": [
        ("activation_link", "Activation Link"),
        ("instructions",    "Instructions (optional)"),
    ],
    "ACCOUNT_LOGIN": [
        ("email",          "Email"),
        ("password",       "Password"),
        ("recovery_email", "Recovery Email"),
        ("notes",          "Notes"),
    ],
    "DOWNLOADABLE_FILE": [
        ("file",         "File / file_id"),
        ("download_url", "Download URL"),
    ],
    "SUBSCRIPTION": [
        ("plan_name",   "Plan Name"),
        ("start_date",  "Start Date"),
        ("expiry_date", "Expiry Date"),
    ],
    "VOUCHER": [
        ("voucher_code",   "Voucher Code"),
        ("redemption_url", "Redemption URL"),
    ],
    "MANUAL_DELIVERY": [
        ("custom_text",  "Custom Text"),
        ("attachments",  "Attachments Notes"),
    ],
    "SERVICE": [
        ("service_name", "Service Name"),
        ("notes",        "Notes"),
    ],
    "BUNDLE": [
        ("contents",     "Bundle Contents Description"),
    ],
    "AUTO_GENERATED": [
        ("format_hint",  "Format Hint (e.g. UUID)"),
    ],
    "PREORDER": [
        ("eta",          "Estimated Delivery Date"),
        ("notes",        "Pre-Order Notes"),
    ],
    "EXTERNAL_DELIVERY": [
        ("endpoint_url", "Webhook / API URL"),
        ("notes",        "Notes"),
    ],
    "FILE": [
        ("download_link", "Download Link"),
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _guard(uid: int) -> bool:
    return is_admin(uid) or has_permission(uid, "manage_products")


def _is_active() -> bool:
    return cfg.get("apt_status", "enabled") in ("enabled", "maintenance")


async def _safe_edit(query, text: str, kb=None, parse_mode: str = "HTML") -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


def _back(data: str = "apt:menu") -> IKM:
    return IKM([[IKB("⬅️ Back", callback_data=data)]])


def _home_back(back: str = "apt:menu") -> List[List[IKB]]:
    return [[IKB("⬅️ Back", callback_data=back),
             IKB("🏠 Templates", callback_data="apt:menu")]]


def _type_emoji(template_type: Optional[str]) -> str:
    _MAP = {
        "KEY":               "🔑",
        "REDEEM_LINK":       "🔗",
        "ACCOUNT_LOGIN":     "👤",
        "DOWNLOADABLE_FILE": "📁",
        "FILE":              "📁",
        "AUTO_GENERATED":    "🤖",
        "MANUAL_DELIVERY":   "👨",
        "PREORDER":          "⏳",
        "SUBSCRIPTION":      "🔄",
        "BUNDLE":            "📦",
        "SERVICE":           "🛠",
        "VOUCHER":           "🎟",
        "EXTERNAL_DELIVERY": "🌐",
    }
    return _MAP.get(template_type or "", "📋")


def _type_label(template_type: Optional[str]) -> str:
    _MAP = {
        "KEY":               "Software Key",
        "REDEEM_LINK":       "Redeem Link",
        "ACCOUNT_LOGIN":     "Account / Login",
        "DOWNLOADABLE_FILE": "Downloadable File",
        "FILE":              "Downloadable File",
        "AUTO_GENERATED":    "Auto Generated",
        "MANUAL_DELIVERY":   "Manual Delivery",
        "PREORDER":          "Pre-Order",
        "SUBSCRIPTION":      "Subscription",
        "BUNDLE":            "Bundle",
        "SERVICE":           "Service Product",
        "VOUCHER":           "Voucher / Gift Code",
        "EXTERNAL_DELIVERY": "External Delivery",
    }
    return _MAP.get(template_type or "", "Unknown")


def _default_template_data(template_type: str) -> Dict[str, Any]:
    """Build a minimal template_data snapshot compatible with _create_from_snapshot."""
    return {
        "name": "",
        "description": "",
        "product_type": template_type,
        "price": 0.0,
        "sale_price": None,
        "currency": "USD",
        "category_id": None,
        "subcategory_id": None,
        "is_active": True,
        "is_featured": False,
        "product_emoji": None,
        "delivery_note": None,
        "warranty_info": None,
        "min_quantity": 1,
        "max_quantity": 10,
        "bulk_purchase_enabled": False,
        "stock_count": 0,
        "type_config": None,
        "image_url": None,
        "image_file_id": None,
        "auto_restock": False,
        "restock_threshold": 0,
        "variants": [],
        "faq": [],
        "coupons": [],
    }


def _default_custom_fields(template_type: str) -> Dict[str, str]:
    fields = _CUSTOM_FIELDS.get(template_type, [])
    return {key: "" for key, _ in fields}


def _tpl_summary(t: ProductTemplate) -> str:
    emoji = _type_emoji(t.template_type)
    label = _type_label(t.template_type)
    default_tag = " ⭐" if t.is_default else ""
    price_str = (f"${t.default_price:.2f}" if t.default_price is not None else "—")
    tags = json.loads(t.tags_json or "[]")
    tags_str = ", ".join(tags) if tags else "—"
    return (
        f"{emoji} <b>{t.name}</b>{default_tag}\n"
        f"   Type: {label}  |  Price: {price_str}\n"
        f"   Used: {t.use_count}×  |  Created: {t.products_created} products\n"
        f"   Tags: {tags_str}"
    )


# ── Main menu ─────────────────────────────────────────────────────────────────

async def apt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    if query:
        await query.answer()
    if not _guard(uid):
        if query:
            await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as s:
        total      = s.query(ProductTemplate).filter_by(is_archived=False).count()
        archived   = s.query(ProductTemplate).filter_by(is_archived=True).count()
        defaults   = s.query(ProductTemplate).filter_by(is_default=True, is_archived=False).count()
        total_used = s.query(ProductTemplate).filter_by(is_archived=False).with_entities(
            ProductTemplate.use_count
        ).all()
        used_sum = sum(r[0] or 0 for r in total_used)

    text = (
        "📋 <b>PRODUCT TEMPLATE SYSTEM</b>\n\n"
        f"📊 <b>Overview:</b>\n"
        f"  📋 Active Templates: <b>{total}</b>\n"
        f"  🗄 Archived:         <b>{archived}</b>\n"
        f"  ⭐ Defaults:         <b>{defaults}</b>\n"
        f"  🔢 Total Uses:       <b>{used_sum}</b>\n"
    )

    kb = IKM([
        [IKB("📋 All Templates",   callback_data="apt:list"),
         IKB("➕ New Template",    callback_data="apt:new")],
        [IKB("🗄 Archived",        callback_data="apt:arch_list"),
         IKB("📊 Statistics",      callback_data="apt:stats")],
        [IKB("🔍 Search",          callback_data="apt:search"),
         IKB("⚙️ Filter by Type",  callback_data="apt:filter:all")],
        [IKB("📤 Export JSON",     callback_data="apt:export"),
         IKB("📥 Import JSON",     callback_data="apt:import")],
        [IKB("⬅️ Back", callback_data="acc:cat:products")],
    ])
    if query:
        await _safe_edit(query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ── Template list ──────────────────────────────────────────────────────────────

async def apt_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data  # apt:list or apt:list:<page>
    parts = data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0

    # Honour active filter/sort in user_data
    ftype = context.user_data.get("apt_filter", "all")
    sort  = context.user_data.get("apt_sort", "name")

    with get_db_session() as s:
        q = s.query(ProductTemplate).filter_by(is_archived=False)
        if ftype != "all":
            q = q.filter(ProductTemplate.template_type == ftype)
        if sort == "used":
            q = q.order_by(ProductTemplate.use_count.desc())
        elif sort == "price":
            q = q.order_by(ProductTemplate.default_price.asc().nullslast())
        elif sort == "created":
            q = q.order_by(ProductTemplate.created_at.desc())
        else:
            q = q.order_by(ProductTemplate.name.asc())
        total = q.count()
        templates = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

    if not templates and page == 0:
        await _safe_edit(query,
            "📋 <b>Product Templates</b>\n\nNo templates yet. Create the first one!",
            IKM([
                [IKB("➕ New Template", callback_data="apt:new")],
                _home_back("apt:menu")[0],
            ]))
        return

    rows: List[List[IKB]] = []
    for t in templates:
        emoji = _type_emoji(t.template_type)
        star  = "⭐ " if t.is_default else ""
        label = f"{star}{emoji} {t.name}"
        rows.append([IKB(label, callback_data=f"apt:view:{t.id}")])

    # Pagination
    nav: List[IKB] = []
    if page > 0:
        nav.append(IKB("⬅️ Prev", callback_data=f"apt:list:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(IKB("➡️ Next", callback_data=f"apt:list:{page + 1}"))
    if nav:
        rows.append(nav)

    filter_label = (f"[{_type_label(ftype)}]" if ftype != "all" else "[All types]")
    sort_label   = {"name": "A-Z", "used": "Most used", "price": "Price", "created": "Newest"}.get(sort, sort)

    rows.append([
        IKB(f"⚙️ Filter {filter_label}", callback_data="apt:filter:all"),
        IKB(f"↕️ Sort: {sort_label}",    callback_data="apt:sort:next"),
    ])
    rows.append([
        IKB("➕ New Template", callback_data="apt:new"),
        IKB("🏠 Menu",         callback_data="apt:menu"),
    ])

    text = (
        f"📋 <b>Active Templates</b>  ({total} total)\n"
        f"Filter: {filter_label}  •  Sort: {sort_label}"
    )
    await _safe_edit(query, text, IKM(rows))


async def apt_arch_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data
    parts = data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0

    with get_db_session() as s:
        q = s.query(ProductTemplate).filter_by(is_archived=True).order_by(
            ProductTemplate.updated_at.desc()
        )
        total = q.count()
        templates = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

    if not templates and page == 0:
        await _safe_edit(query,
            "🗄 <b>Archived Templates</b>\n\nNo archived templates.",
            _back("apt:menu"))
        return

    rows: List[List[IKB]] = []
    for t in templates:
        emoji = _type_emoji(t.template_type)
        rows.append([IKB(f"🗄 {emoji} {t.name}", callback_data=f"apt:view:{t.id}")])

    nav: List[IKB] = []
    if page > 0:
        nav.append(IKB("⬅️ Prev", callback_data=f"apt:arch_list:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(IKB("➡️ Next", callback_data=f"apt:arch_list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows += _home_back("apt:menu")

    await _safe_edit(query, f"🗄 <b>Archived Templates</b>  ({total} total)", IKM(rows))


# ── Template creation (conversation) ──────────────────────────────────────────

async def apt_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    if not _is_active():
        await query.answer("⛔ Template system is disabled.", show_alert=True)
        return ConversationHandler.END

    context.user_data["_apt_create"] = {}
    await _safe_edit(query,
        "📋 <b>New Product Template</b>\n\n"
        "Step 1 of 3 — Enter a template name:\n"
        "(e.g. <i>Windows 11 Key</i>)",
        _back("apt:menu"))
    return APT_CREATE_NAME


async def apt_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()[:120]
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Please enter a name:")
        return APT_CREATE_NAME

    context.user_data["_apt_create"]["name"] = name

    # Build type picker
    catalog = ProductType.catalog()
    rows: List[List[IKB]] = []
    for pt, emoji, label in catalog:
        rows.append([IKB(f"{emoji} {label}", callback_data=f"apt:new:type:{pt.name}")])
    rows.append([IKB("❌ Cancel", callback_data="apt:menu")])

    await update.message.reply_text(
        "📋 <b>Step 2 of 3 — Choose product type:</b>",
        reply_markup=IKM(rows),
        parse_mode="HTML",
    )
    return APT_CREATE_DESC


async def apt_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        return ConversationHandler.END

    data = query.data  # apt:new:type:<TYPE_NAME>
    type_name = data.split(":")[-1]
    try:
        ProductType[type_name]  # validate
    except KeyError:
        await query.answer("❌ Invalid type.", show_alert=True)
        return APT_CREATE_DESC

    context.user_data.setdefault("_apt_create", {})["template_type"] = type_name

    emoji = _type_emoji(type_name)
    await _safe_edit(query,
        f"📋 <b>New Template — {emoji} {_type_label(type_name)}</b>\n\n"
        "Step 3 of 3 — Enter a short description (optional).\n"
        "Send <code>/skip</code> to leave blank.",
        _back("apt:menu"))
    return APT_CREATE_PRICE


async def apt_receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text.lower() != "/skip":
        context.user_data.setdefault("_apt_create", {})["description"] = text[:512]
    await update.message.reply_text(
        "💰 Enter a default price (e.g. <code>9.99</code>).\n"
        "Send <code>/skip</code> to leave unset.",
        parse_mode="HTML",
    )
    return APT_CREATE_PRICE


async def apt_receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    create_data = context.user_data.get("_apt_create", {})

    price: Optional[float] = None
    if text.lower() != "/skip":
        try:
            price = float(text)
            if price < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid price. Enter a number (e.g. <code>9.99</code>) or /skip:",
                parse_mode="HTML",
            )
            return APT_CREATE_PRICE

    name        = create_data.get("name", "Unnamed Template")
    description = create_data.get("description")
    ttype       = create_data.get("template_type", "KEY")

    template_data = _default_template_data(ttype)
    template_data["name"]        = name
    template_data["description"] = description or ""
    if price is not None:
        template_data["price"] = price

    custom_fields = _default_custom_fields(ttype)

    with get_db_session() as s:
        tpl = ProductTemplate(
            name              = name,
            description       = description,
            template_data     = json.dumps(template_data),
            template_type     = ttype,
            default_price     = price,
            currency_code     = "USD",
            visibility        = "public",
            auto_delivery     = True,
            manual_review     = False,
            is_default        = False,
            is_archived       = False,
            products_created  = 0,
            use_count         = 0,
            custom_fields_json = json.dumps(custom_fields),
            created_by        = uid,
        )
        s.add(tpl)
        s.commit()
        tpl_id = tpl.id

    log_admin_action(uid, "apt_create", target_type="product_template",
                     target_id=tpl_id, details=f"Template '{name}' type={ttype}")
    context.user_data.pop("_apt_create", None)

    await update.message.reply_text(
        f"✅ <b>Template created!</b>\n\n"
        f"📋 <b>{name}</b>  ({_type_emoji(ttype)} {_type_label(ttype)})\n\n"
        "Use the buttons below to configure additional fields.",
        parse_mode="HTML",
        reply_markup=IKM([
            [IKB("✏️ Edit Template",   callback_data=f"apt:edit:{tpl_id}")],
            [IKB("👁 Preview",         callback_data=f"apt:preview:{tpl_id}")],
            [IKB("📋 All Templates",   callback_data="apt:list"),
             IKB("🏠 Menu",            callback_data="apt:menu")],
        ]),
    )
    return ConversationHandler.END


# ── Template view ──────────────────────────────────────────────────────────────

async def apt_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Template not found.", show_alert=True)
            return

        # Capture ALL needed values inside the session to avoid DetachedInstanceError
        tpl_name        = tpl.name
        is_archived     = bool(tpl.is_archived)
        is_default      = bool(tpl.is_default)
        emoji           = _type_emoji(tpl.template_type)
        label           = _type_label(tpl.template_type)
        price_str       = f"${tpl.default_price:.2f}" if tpl.default_price is not None else "Not set"
        default_tag     = " ⭐ Default" if is_default else ""
        arch_tag        = " 🗄 Archived" if is_archived else ""
        currency_code   = tpl.currency_code or "USD"
        visibility      = tpl.visibility or "public"
        auto_delivery   = bool(tpl.auto_delivery)
        manual_review   = bool(tpl.manual_review)
        warranty_info   = tpl.warranty_info or "—"
        description     = tpl.description or "—"
        use_count       = tpl.use_count or 0
        products_created = tpl.products_created or 0
        tags            = json.loads(tpl.tags_json or "[]")
        tags_str        = ", ".join(tags) if tags else "None"
        last_used       = tpl.last_used_at.strftime("%Y-%m-%d") if tpl.last_used_at else "Never"
        created_at      = tpl.created_at.strftime("%Y-%m-%d") if tpl.created_at else "—"
        cfields         = json.loads(tpl.custom_fields_json or "{}")
        cf_lines        = "\n".join(
            f"  • {k}: <code>{v or '—'}</code>"
            for k, v in cfields.items()
        ) if cfields else "  None"

    text = (
        f"{emoji} <b>{tpl_name}</b>{default_tag}{arch_tag}\n"
        f"{'─' * 28}\n"
        f"📦 Type:       {label}\n"
        f"💰 Price:      {price_str}  ({currency_code})\n"
        f"👁 Visibility: {visibility}\n"
        f"🚚 Auto Del:   {'✅' if auto_delivery else '❌'}\n"
        f"🔍 Manual Rev: {'✅' if manual_review else '❌'}\n"
        f"🏷 Tags:       {tags_str}\n"
        f"🛡 Warranty:   {warranty_info}\n"
        f"📝 Description:\n{description}\n\n"
        f"🔑 <b>Custom Fields:</b>\n{cf_lines}\n\n"
        f"📊 Used: {use_count}×  •  Products: {products_created}  •  Last: {last_used}\n"
        f"📅 Created: {created_at}"
    )

    back_cb = "apt:arch_list" if is_archived else "apt:list"
    rows: List[List[IKB]] = [
        [IKB("✏️ Edit",         callback_data=f"apt:edit:{tpl_id}"),
         IKB("👁 Preview",      callback_data=f"apt:preview:{tpl_id}")],
        [IKB("📋 Duplicate",    callback_data=f"apt:dup:{tpl_id}")],
    ]
    if not is_archived:
        def_btn = (IKB("⭐ Unset Default", callback_data=f"apt:unset_default:{tpl_id}")
                   if is_default else
                   IKB("⭐ Set Default",   callback_data=f"apt:set_default:{tpl_id}"))
        rows.append([def_btn, IKB("🗄 Archive", callback_data=f"apt:archive:{tpl_id}")])
    else:
        rows.append([IKB("♻️ Restore", callback_data=f"apt:restore:{tpl_id}")])
    rows.append([IKB("🗑 Delete", callback_data=f"apt:del_ask:{tpl_id}")])
    rows += _home_back(back_cb)
    await _safe_edit(query, text, IKM(rows))


# ── Template edit menu ─────────────────────────────────────────────────────────

_EDIT_FIELDS = [
    ("name",               "📛 Name"),
    ("description",        "📝 Description"),
    ("default_price",      "💰 Default Price"),
    ("currency_code",      "💱 Currency"),
    ("visibility",         "👁 Visibility"),
    ("tags_json",          "🏷 Tags"),
    ("warranty_info",      "🛡 Warranty"),
    ("delivery_method",    "🚚 Delivery Method"),
    ("refund_policy",      "↩️ Refund Policy"),
    ("replacement_policy", "🔁 Replacement Policy"),
    ("auto_delivery",      "⚡ Auto Delivery (on/off)"),
    ("manual_review",      "🔍 Manual Review (on/off)"),
]


async def apt_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Template not found.", show_alert=True)
            return
        tpl_name = tpl.name
        ttype    = tpl.template_type or "KEY"

    cfield_defs = _CUSTOM_FIELDS.get(ttype, [])
    rows: List[List[IKB]] = []
    for field, label in _EDIT_FIELDS:
        rows.append([IKB(label, callback_data=f"apt:edit:{tpl_id}:f:{field}")])
    # Custom delivery fields
    for key, cf_label in cfield_defs:
        rows.append([IKB(f"🔑 {cf_label}", callback_data=f"apt:edit:{tpl_id}:f:cf_{key}")])

    rows += _home_back(f"apt:view:{tpl_id}")
    await _safe_edit(query,
        f"✏️ <b>Edit Template: {tpl_name}</b>\n\nSelect a field to edit:",
        IKM(rows))


async def apt_edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        return ConversationHandler.END

    # apt:edit:<id>:f:<field>
    parts  = query.data.split(":")
    tpl_id = int(parts[2])
    field  = parts[4]

    context.user_data["_apt_edit"] = {"tpl_id": tpl_id, "field": field}

    field_label = field.replace("cf_", "").replace("_", " ").title()

    # Current value hint
    hint = ""
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Not found.", show_alert=True)
            return ConversationHandler.END
        if field.startswith("cf_"):
            cfields = json.loads(tpl.custom_fields_json or "{}")
            hint = str(cfields.get(field[3:], ""))
        else:
            hint = str(getattr(tpl, field, "") or "")

    booleans = {"auto_delivery", "manual_review"}
    if field in booleans:
        current_val = (hint.lower() in ("true", "1", "yes"))
        new_val = not current_val
        # Toggle immediately, no text step needed
        with get_db_session() as s:
            tpl = s.get(ProductTemplate, tpl_id)
            if tpl:
                setattr(tpl, field, new_val)
                s.commit()
        log_admin_action(uid, "apt_edit", target_type="product_template",
                         target_id=tpl_id, details=f"field={field} val={new_val}")
        context.user_data.pop("_apt_edit", None)
        await _safe_edit(query,
            f"✅ <b>{field_label}</b> set to <code>{'on' if new_val else 'off'}</code>.",
            IKM([[IKB("⬅️ Back to Edit", callback_data=f"apt:edit:{tpl_id}")]]))
        return ConversationHandler.END

    hint_str = f"\nCurrent: <code>{hint or '—'}</code>" if hint else ""
    await _safe_edit(query,
        f"✏️ <b>Edit {field_label}</b>{hint_str}\n\nSend the new value.\n"
        f"Send <code>/skip</code> to clear/cancel.",
        _back(f"apt:edit:{tpl_id}"))
    return APT_EDIT_FIELD


async def apt_edit_field_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()
    edit = context.user_data.get("_apt_edit", {})
    tpl_id = edit.get("tpl_id")
    field  = edit.get("field")

    if not tpl_id or not field:
        return ConversationHandler.END

    if text.lower() == "/skip":
        context.user_data.pop("_apt_edit", None)
        await update.message.reply_text(
            "↩️ Edit cancelled.",
            reply_markup=IKM([[IKB("⬅️ Back", callback_data=f"apt:edit:{tpl_id}")]]))
        return ConversationHandler.END

    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await update.message.reply_text("❌ Template not found.")
            return ConversationHandler.END

        if field.startswith("cf_"):
            cf_key = field[3:]
            cfields = json.loads(tpl.custom_fields_json or "{}")
            cfields[cf_key] = text[:512]
            tpl.custom_fields_json = json.dumps(cfields)
        elif field == "default_price":
            try:
                tpl.default_price = float(text)
            except ValueError:
                await update.message.reply_text("❌ Invalid price. Send a number:")
                return APT_EDIT_FIELD
        elif field == "tags_json":
            tag_list = [t.strip() for t in text.replace(",", " ").split() if t.strip()]
            tpl.tags_json = json.dumps(tag_list[:20])
        else:
            # String fields — truncate to safe length
            max_len = 512 if field == "description" else 256
            setattr(tpl, field, text[:max_len])

        # Sync name into template_data blob too
        if field == "name":
            try:
                td = json.loads(tpl.template_data or "{}")
                td["name"] = text[:120]
                tpl.template_data = json.dumps(td)
            except Exception:
                pass
        if field == "description":
            try:
                td = json.loads(tpl.template_data or "{}")
                td["description"] = text[:512]
                tpl.template_data = json.dumps(td)
            except Exception:
                pass
        if field == "default_price":
            try:
                td = json.loads(tpl.template_data or "{}")
                td["price"] = tpl.default_price
                tpl.template_data = json.dumps(td)
            except Exception:
                pass
        if field == "warranty_info":
            try:
                td = json.loads(tpl.template_data or "{}")
                td["warranty_info"] = text
                tpl.template_data = json.dumps(td)
            except Exception:
                pass

        tpl.updated_at = datetime.utcnow()
        s.commit()

    field_label = field.replace("cf_", "").replace("_", " ").title()
    log_admin_action(uid, "apt_edit", target_type="product_template",
                     target_id=tpl_id, details=f"field={field}")
    context.user_data.pop("_apt_edit", None)
    await update.message.reply_text(
        f"✅ <b>{field_label}</b> updated.",
        parse_mode="HTML",
        reply_markup=IKM([
            [IKB("✏️ Continue Editing", callback_data=f"apt:edit:{tpl_id}")],
            [IKB("👁 View Template",    callback_data=f"apt:view:{tpl_id}")],
        ]))
    return ConversationHandler.END


# ── Template actions ───────────────────────────────────────────────────────────

async def apt_duplicate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        src = s.get(ProductTemplate, tpl_id)
        if not src:
            await query.answer("❌ Template not found.", show_alert=True)
            return
        # Capture src.name inside session before it becomes detached
        src_name = src.name
        new_tpl = ProductTemplate(
            name              = f"Copy of {src_name}"[:120],
            description       = src.description,
            template_data     = src.template_data,
            template_type     = src.template_type,
            delivery_method   = src.delivery_method,
            is_default        = False,
            is_archived       = False,
            tags_json         = src.tags_json,
            default_price     = src.default_price,
            currency_code     = src.currency_code,
            visibility        = src.visibility,
            auto_delivery     = src.auto_delivery,
            manual_review     = src.manual_review,
            refund_policy     = src.refund_policy,
            replacement_policy = src.replacement_policy,
            warranty_info     = src.warranty_info,
            product_image     = src.product_image,
            custom_fields_json = src.custom_fields_json,
            use_count         = 0,
            products_created  = 0,
            created_by        = uid,
        )
        s.add(new_tpl)
        s.commit()
        new_id = new_tpl.id

    log_admin_action(uid, "apt_duplicate", target_type="product_template",
                     target_id=tpl_id, details=f"new_id={new_id}")
    await _safe_edit(query,
        f"✅ <b>Template duplicated!</b>\n\nNew template: <b>Copy of {src_name}</b>",
        IKM([
            [IKB("✏️ Edit Copy",  callback_data=f"apt:edit:{new_id}")],
            [IKB("👁 View Copy",  callback_data=f"apt:view:{new_id}")],
            [IKB("📋 All Templates", callback_data="apt:list")],
        ]))


async def apt_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        name = tpl.name if tpl else "?"

    await _safe_edit(query,
        f"🗑 <b>Delete Template?</b>\n\n"
        f"<b>{name}</b>\n\n"
        "⚠️ This cannot be undone.",
        IKM([
            [IKB("✅ Yes, Delete", callback_data=f"apt:del_ok:{tpl_id}"),
             IKB("❌ Cancel",      callback_data=f"apt:view:{tpl_id}")],
        ]))


async def apt_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Template not found.", show_alert=True)
            return
        name = tpl.name
        s.delete(tpl)
        s.commit()

    log_admin_action(uid, "apt_delete", target_type="product_template",
                     target_id=tpl_id, details=f"name={name}")
    await _safe_edit(query,
        f"✅ <b>Template deleted:</b> {name}",
        IKM([[IKB("📋 All Templates", callback_data="apt:list"),
              IKB("🏠 Menu",          callback_data="apt:menu")]]))


async def apt_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Not found.", show_alert=True)
            return
        tpl.is_archived = True
        tpl.is_default  = False
        tpl.updated_at  = datetime.utcnow()
        s.commit()
        name = tpl.name

    log_admin_action(uid, "apt_archive", target_type="product_template", target_id=tpl_id)
    await _safe_edit(query,
        f"🗄 <b>Template archived:</b> {name}",
        IKM([[IKB("♻️ Restore",       callback_data=f"apt:restore:{tpl_id}")],
             [IKB("📋 Active List",   callback_data="apt:list"),
              IKB("🏠 Menu",          callback_data="apt:menu")]]))


async def apt_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Not found.", show_alert=True)
            return
        tpl.is_archived = False
        tpl.updated_at  = datetime.utcnow()
        s.commit()
        name = tpl.name

    log_admin_action(uid, "apt_restore", target_type="product_template", target_id=tpl_id)
    await _safe_edit(query,
        f"✅ <b>Template restored:</b> {name}",
        IKM([[IKB("👁 View",           callback_data=f"apt:view:{tpl_id}")],
             [IKB("📋 Active List",    callback_data="apt:list"),
              IKB("🏠 Menu",           callback_data="apt:menu")]]))


async def apt_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts  = query.data.split(":")
    tpl_id = int(parts[2])
    unset  = (parts[1] == "unset_default")

    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Not found.", show_alert=True)
            return
        if not unset:
            # Clear any existing default for same type
            (s.query(ProductTemplate)
             .filter(ProductTemplate.template_type == tpl.template_type,
                     ProductTemplate.is_default == True,
                     ProductTemplate.id != tpl_id)
             .update({"is_default": False}))
        tpl.is_default = not unset
        s.commit()
        name  = tpl.name
        ttype = tpl.template_type

    action = "unset_default" if unset else "set_default"
    log_admin_action(uid, f"apt_{action}", target_type="product_template", target_id=tpl_id)
    label = "removed as default" if unset else "set as default ⭐"
    await _safe_edit(query,
        f"✅ <b>{name}</b> {label}\n\nType: {_type_label(ttype)}",
        IKM([[IKB("👁 View", callback_data=f"apt:view:{tpl_id}")],
             [IKB("📋 List", callback_data="apt:list"),
              IKB("🏠 Menu", callback_data="apt:menu")]]))


async def apt_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    tpl_id = int(query.data.split(":")[2])
    with get_db_session() as s:
        tpl = s.get(ProductTemplate, tpl_id)
        if not tpl:
            await query.answer("❌ Not found.", show_alert=True)
            return

        emoji = _type_emoji(tpl.template_type)
        label = _type_label(tpl.template_type)
        price = f"${tpl.default_price:.2f}" if tpl.default_price is not None else "TBD"
        desc  = tpl.description or "No description provided."
        warranty = f"\n🛡 <b>Warranty:</b> {tpl.warranty_info}" if tpl.warranty_info else ""

        preview_text = (
            f"{emoji} <b>{tpl.name}</b>\n"
            f"{'─' * 30}\n"
            f"{desc}\n\n"
            f"💰 <b>Price:</b> {price}\n"
            f"📦 <b>Type:</b> {label}\n"
            f"🚚 <b>Delivery:</b> {'Automatic' if tpl.auto_delivery else 'Manual'}"
            f"{warranty}\n\n"
            f"<i>── Template Preview ──</i>"
        )

    await _safe_edit(query, preview_text,
        IKM([[IKB("✏️ Edit", callback_data=f"apt:edit:{tpl_id}"),
              IKB("⬅️ Back", callback_data=f"apt:view:{tpl_id}")]]))


# ── Statistics ─────────────────────────────────────────────────────────────────

async def apt_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as s:
        active    = s.query(ProductTemplate).filter_by(is_archived=False).all()
        archived  = s.query(ProductTemplate).filter_by(is_archived=True).count()
        defaults  = sum(1 for t in active if t.is_default)
        total_use = sum(t.use_count or 0 for t in active)
        total_prd = sum(t.products_created or 0 for t in active)

        # Per-type breakdown
        type_counts: Dict[str, int] = {}
        for t in active:
            tt = t.template_type or "Unknown"
            type_counts[tt] = type_counts.get(tt, 0) + 1

        # Top by usage
        top = sorted(active, key=lambda x: x.use_count or 0, reverse=True)[:5]

    type_lines = "\n".join(
        f"  {_type_emoji(tt)} {_type_label(tt)}: {cnt}"
        for tt, cnt in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    ) or "  None"

    top_lines = "\n".join(
        f"  {i+1}. {t.name} — {t.use_count}×"
        for i, t in enumerate(top)
    ) or "  None yet"

    text = (
        "📊 <b>TEMPLATE STATISTICS</b>\n\n"
        f"📋 Active Templates:    <b>{len(active)}</b>\n"
        f"🗄 Archived:            <b>{archived}</b>\n"
        f"⭐ Default Templates:   <b>{defaults}</b>\n"
        f"🔢 Total Uses:          <b>{total_use}</b>\n"
        f"📦 Products Created:    <b>{total_prd}</b>\n\n"
        f"📊 <b>By Type:</b>\n{type_lines}\n\n"
        f"🏆 <b>Most Used:</b>\n{top_lines}"
    )
    await _safe_edit(query, text, _back("apt:menu"))


# ── Export ─────────────────────────────────────────────────────────────────────

async def apt_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as s:
        templates = s.query(ProductTemplate).filter_by(is_archived=False).all()
        export_list = []
        for t in templates:
            export_list.append({
                "name":              t.name,
                "description":       t.description,
                "template_type":     t.template_type,
                "delivery_method":   t.delivery_method,
                "is_default":        t.is_default,
                "tags":              json.loads(t.tags_json or "[]"),
                "default_price":     t.default_price,
                "currency_code":     t.currency_code,
                "visibility":        t.visibility,
                "auto_delivery":     t.auto_delivery,
                "manual_review":     t.manual_review,
                "refund_policy":     t.refund_policy,
                "replacement_policy": t.replacement_policy,
                "warranty_info":     t.warranty_info,
                "custom_fields":     json.loads(t.custom_fields_json or "{}"),
                "template_data":     json.loads(t.template_data or "{}"),
                "_exported_at":      datetime.utcnow().isoformat(),
                "_version":          1,
            })

    payload = json.dumps({"templates": export_list, "count": len(export_list)},
                         indent=2, ensure_ascii=False)
    filename = f"product_templates_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"

    log_admin_action(uid, "apt_export", details=f"count={len(export_list)}")
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(payload.encode("utf-8"), filename=filename),
            caption=f"📤 <b>Template Export</b>\n{len(export_list)} templates",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("apt_export failed")
        await query.answer("❌ Export failed.", show_alert=True)
        return

    await _safe_edit(query,
        f"✅ <b>Export complete</b> — {len(export_list)} template(s) sent as JSON file.",
        _back("apt:menu"))


# ── Import (conversation) ──────────────────────────────────────────────────────

async def apt_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await _safe_edit(query,
        "📥 <b>Import Templates</b>\n\n"
        "Upload a JSON file previously exported from this system.\n\n"
        "Send the file now, or /cancel to abort.",
        _back("apt:menu"))
    return APT_WAIT_IMPORT


async def apt_import_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Please send a JSON file:")
        return APT_WAIT_IMPORT

    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ Only .json files are accepted:")
        return APT_WAIT_IMPORT

    try:
        file_obj = await context.bot.get_file(doc.file_id)
        raw = await file_obj.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("apt_import parse failed: %s", exc)
        await update.message.reply_text("❌ Failed to parse JSON. Please check the file and try again.")
        return APT_WAIT_IMPORT

    templates_raw = data if isinstance(data, list) else data.get("templates", [])
    if not isinstance(templates_raw, list):
        await update.message.reply_text("❌ Invalid format. Expected a list of templates.")
        return APT_WAIT_IMPORT

    imported = 0
    skipped  = 0
    errors   = 0

    with get_db_session() as s:
        for entry in templates_raw[:200]:
            try:
                ttype = entry.get("template_type") or "KEY"
                # Validate template type
                try:
                    ProductType[ttype]
                except KeyError:
                    ttype = "KEY"

                td = entry.get("template_data") or _default_template_data(ttype)
                if isinstance(td, dict):
                    td["name"] = entry.get("name", "Imported Template")
                    td_str = json.dumps(td)
                else:
                    td_str = json.dumps(_default_template_data(ttype))

                cf_raw = entry.get("custom_fields") or {}
                tags   = entry.get("tags") or []

                tpl = ProductTemplate(
                    name              = str(entry.get("name", "Imported Template"))[:120],
                    description       = str(entry.get("description") or "")[:512] or None,
                    template_data     = td_str,
                    template_type     = ttype,
                    delivery_method   = entry.get("delivery_method"),
                    is_default        = False,   # never import as default
                    is_archived       = False,
                    tags_json         = json.dumps(tags[:20]),
                    default_price     = entry.get("default_price"),
                    currency_code     = str(entry.get("currency_code") or "USD")[:10],
                    visibility        = str(entry.get("visibility") or "public")[:16],
                    auto_delivery     = bool(entry.get("auto_delivery", True)),
                    manual_review     = bool(entry.get("manual_review", False)),
                    refund_policy     = entry.get("refund_policy"),
                    replacement_policy = entry.get("replacement_policy"),
                    warranty_info     = entry.get("warranty_info"),
                    custom_fields_json = json.dumps(cf_raw),
                    use_count         = 0,
                    products_created  = 0,
                    created_by        = uid,
                )
                s.add(tpl)
                imported += 1
            except Exception as exc:
                logger.warning("apt_import row failed: %s", exc)
                errors += 1
        s.commit()

    log_admin_action(uid, "apt_import",
                     details=f"imported={imported} errors={errors}")
    await update.message.reply_text(
        f"📥 <b>Import complete</b>\n\n"
        f"✅ Imported: <b>{imported}</b>\n"
        f"❌ Errors:   <b>{errors}</b>",
        parse_mode="HTML",
        reply_markup=IKM([
            [IKB("📋 View Templates", callback_data="apt:list"),
             IKB("🏠 Menu",           callback_data="apt:menu")],
        ]))
    return ConversationHandler.END


# ── Filter / Sort / Search ────────────────────────────────────────────────────

async def apt_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # apt:filter:<type|all>
    selected = query.data.split(":")[-1]

    if selected == "all" or not selected:
        # Show type picker
        catalog = ProductType.catalog()
        rows: List[List[IKB]] = [[IKB("🔄 All Types", callback_data="apt:filter:ALL")]]
        for pt, emoji, label in catalog:
            rows.append([IKB(f"{emoji} {label}", callback_data=f"apt:filter:{pt.name}")])
        rows += _home_back("apt:menu")
        await _safe_edit(query, "⚙️ <b>Filter by Product Type:</b>", IKM(rows))
        return

    if selected == "ALL":
        context.user_data["apt_filter"] = "all"
    else:
        context.user_data["apt_filter"] = selected

    # Redirect to list page 0
    await apt_list(with_data(update, "apt:list"), context)


async def apt_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        return

    # Cycle through sort options
    _SORTS = ["name", "used", "price", "created"]
    current = context.user_data.get("apt_sort", "name")
    idx     = _SORTS.index(current) if current in _SORTS else 0
    context.user_data["apt_sort"] = _SORTS[(idx + 1) % len(_SORTS)]

    await apt_list(with_data(update, "apt:list"), context)


async def apt_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not _guard(uid):
        return ConversationHandler.END

    await _safe_edit(query,
        "🔍 <b>Search Templates</b>\n\nSend your search query:",
        _back("apt:list"))
    return APT_SEARCH


async def apt_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q_text = (update.message.text or "").strip().lower()
    if not q_text:
        await update.message.reply_text("❌ Query cannot be empty. Try again:")
        return APT_SEARCH

    with get_db_session() as s:
        results = (
            s.query(ProductTemplate)
            .filter(
                ProductTemplate.is_archived == False,
                ProductTemplate.name.ilike(f"%{q_text}%"),
            )
            .order_by(ProductTemplate.name)
            .limit(20)
            .all()
        )

    if not results:
        await update.message.reply_text(
            f"🔍 No templates matching <i>{q_text}</i>.",
            parse_mode="HTML",
            reply_markup=IKM([[IKB("📋 All Templates", callback_data="apt:list")]]))
        return ConversationHandler.END

    rows: List[List[IKB]] = []
    for t in results:
        emoji = _type_emoji(t.template_type)
        rows.append([IKB(f"{emoji} {t.name}", callback_data=f"apt:view:{t.id}")])
    rows.append([IKB("📋 All Templates", callback_data="apt:list"),
                 IKB("🏠 Menu",          callback_data="apt:menu")])

    await update.message.reply_text(
        f"🔍 <b>Results for:</b> <i>{q_text}</i>  ({len(results)} found)",
        parse_mode="HTML",
        reply_markup=IKM(rows))
    return ConversationHandler.END


# ── Cancel ─────────────────────────────────────────────────────────────────────

async def apt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("_apt_create", None)
    context.user_data.pop("_apt_edit", None)
    q = update.callback_query
    if q:
        await q.answer()
        await _safe_edit(q, "↩️ Operation cancelled.", _back("apt:menu"))
    else:
        await update.message.reply_text(
            "↩️ Cancelled.",
            reply_markup=IKM([[IKB("📋 Templates", callback_data="apt:menu")]])),
    return ConversationHandler.END


# ── Dispatcher ─────────────────────────────────────────────────────────────────

async def apt_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid   = update.effective_user.id

    if not query:
        return
    # Do NOT answer here — each individual handler answers its own query
    # to avoid double-answering when functions are also called internally.

    if not _guard(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data  # apt:...

    if data == "apt:menu":
        return await apt_menu(update, context)
    if data in ("apt:list", ) or data.startswith("apt:list:"):
        return await apt_list(update, context)
    if data in ("apt:arch_list", ) or data.startswith("apt:arch_list:"):
        return await apt_arch_list(update, context)
    if data.startswith("apt:view:"):
        return await apt_view(update, context)
    if data.startswith("apt:edit:") and ":f:" not in data and len(data.split(":")) == 3:
        return await apt_edit_menu(update, context)
    if data.startswith("apt:dup:"):
        return await apt_duplicate(update, context)
    if data.startswith("apt:del_ask:"):
        return await apt_delete_ask(update, context)
    if data.startswith("apt:del_ok:"):
        return await apt_delete_ok(update, context)
    if data.startswith("apt:archive:"):
        return await apt_archive(update, context)
    if data.startswith("apt:restore:"):
        return await apt_restore(update, context)
    if data.startswith("apt:set_default:") or data.startswith("apt:unset_default:"):
        return await apt_set_default(update, context)
    if data.startswith("apt:preview:"):
        return await apt_preview(update, context)
    if data == "apt:stats":
        return await apt_stats(update, context)
    if data == "apt:export":
        return await apt_export(update, context)
    if data.startswith("apt:filter:"):
        return await apt_filter(update, context)
    if data.startswith("apt:sort:"):
        return await apt_sort(update, context)

    await query.answer("❓ Unknown command.", show_alert=True)


# ── Conversation builders ──────────────────────────────────────────────────────

def build_apt_create_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(apt_new_start,   pattern=r"^apt:new$"),
        ],
        states={
            APT_CREATE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apt_receive_name),
            ],
            APT_CREATE_DESC: [
                CallbackQueryHandler(apt_type_selected, pattern=r"^apt:new:type:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apt_receive_desc),
            ],
            APT_CREATE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apt_receive_price),
                CommandHandler("skip", apt_receive_price),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(apt_cancel, pattern=r"^apt:menu$"),
            CommandHandler("cancel", apt_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_apt_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(apt_edit_field_start, pattern=r"^apt:edit:\d+:f:.+$"),
        ],
        states={
            APT_EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apt_edit_field_receive),
                CommandHandler("skip", apt_edit_field_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", apt_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_apt_import_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(apt_import_start, pattern=r"^apt:import$"),
        ],
        states={
            APT_WAIT_IMPORT: [
                MessageHandler(filters.Document.ALL, apt_import_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(apt_cancel, pattern=r"^apt:menu$"),
            CommandHandler("cancel", apt_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_apt_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(apt_search_start, pattern=r"^apt:search$"),
        ],
        states={
            APT_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apt_search_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(apt_cancel, pattern=r"^apt:menu$"),
            CommandHandler("cancel", apt_cancel),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


# ── Handler registration ───────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all apt:* handlers into the PTB Application."""
    from telegram.ext import CallbackQueryHandler as CQH

    # Conversation handlers (must be registered before the catch-all dispatcher)
    application.add_handler(build_apt_create_conv())
    application.add_handler(build_apt_edit_conv())
    application.add_handler(build_apt_import_conv())
    application.add_handler(build_apt_search_conv())

    # Catch-all dispatcher for non-conversation apt:* callbacks
    application.add_handler(CQH(apt_dispatch, pattern=r"^apt:.+$"))
