"""V25 — Admin Product FAQ Manager.

Callback namespace:  ``acc:pfaq:*``  (routed through admin_control_center)
Section entry:       ``acc:sec:pfaq``

Sub-actions
-----------
acc:pfaq:menu                           Global settings
acc:pfaq:status:<s>                     Set feature status (enabled/maintenance/disabled)
acc:pfaq:toggle:<key>                   Toggle a boolean setting
acc:pfaq:maxset:<value>                 Set max-per-product limit
acc:pfaq:list:<page>                    List products with FAQ counts (paginated)
acc:pfaq:prod:<product_id>:<page>       FAQs for one product (paginated)
acc:pfaq:view:<faq_id>                  View a single FAQ detail
acc:pfaq:del:<faq_id>                   Delete a FAQ (with confirmation)
acc:pfaq:delok:<faq_id>                 Confirmed delete
acc:pfaq:dup:<faq_id>                   Duplicate a FAQ
acc:pfaq:up:<faq_id>                    Move FAQ up
acc:pfaq:down:<faq_id>                  Move FAQ down
acc:pfaq:toggle_active:<faq_id>         Enable/disable a FAQ
acc:pfaq:add:<product_id>               Start add-FAQ conversation
acc:pfaq:edit:<faq_id>                  Start edit-FAQ conversation
acc:pfaq:copy:<faq_id>                  Start copy-to-product conversation
acc:pfaq:search                         Start admin search conversation
"""
from __future__ import annotations

import logging
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

from database import get_db_session
from database.models import Product, ProductFAQ
from services import product_faq as svc
from utils.audit import log_admin_action
from utils.bot_config import cfg
from ._acc_helpers import require_admin, back_root, paginate, send

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────
(
    ADD_Q, ADD_A, ADD_CAT,
    EDIT_FIELD, EDIT_VALUE,
    COPY_TARGET,
    SEARCH_QUERY,
) = range(9500, 9507)

_PAGE = 8   # products/FAQs per page

# ── Config helpers ────────────────────────────────────────────────────────

_STATUS_OPTS = [
    ("enabled",     "🟢 Enable"),
    ("maintenance", "🟡 Maintenance"),
    ("disabled",    "🔴 Disable"),
]

_MAX_OPTS = [
    ("5",   "5"),
    ("10",  "10"),
    ("20",  "20"),
    ("50",  "50"),
    ("0",   "Unlimited"),
]

_BOOL_SETTINGS = [
    ("pfaq_show_counter",  "Show FAQ Counter"),
    ("pfaq_allow_search",  "Allow Search"),
    ("pfaq_expand_first",  "Expand First Question"),
]


def _cur_status() -> str:
    return cfg.get_str("pfaq_status", "enabled")


def _bval(key: str) -> bool:
    return cfg.get_bool(key, True)


def _back_menu() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ FAQ Settings", callback_data="acc:pfaq:menu")


def _back_prod(product_id: int, page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Product FAQs",
                                callback_data=f"acc:pfaq:prod:{product_id}:{page}")


def _back_list(page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Products", callback_data=f"acc:pfaq:list:{page}")


# ─── Settings panel ───────────────────────────────────────────────────────

@require_admin
async def pfaq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = _cur_status()
    status_label = next((lbl for k, lbl in _STATUS_OPTS if k == status), "?")
    mx = svc.max_per_product()
    mx_label = "Unlimited" if mx == 0 else str(mx)

    lines = [
        "❓ <b>PRODUCT FAQ SETTINGS</b>  (V25)",
        "",
        f"<b>Feature Status:</b>  {status_label}",
        f"<b>Max FAQs per Product:</b>  {mx_label}",
        "",
        "<b>Settings:</b>",
    ]
    for key, label in _BOOL_SETTINGS:
        val = _bval(key)
        lines.append(f"  • {label}:  <b>{'✅ ON' if val else '🚫 OFF'}</b>")

    kb: List[List] = [
        [InlineKeyboardButton(lbl, callback_data=f"acc:pfaq:status:{key}")
         for key, lbl in _STATUS_OPTS],
    ]
    # Max per product row
    max_row = [InlineKeyboardButton(lbl, callback_data=f"acc:pfaq:maxset:{key}")
               for key, lbl in _MAX_OPTS]
    kb.append(max_row)
    # Bool toggles
    for key, label in _BOOL_SETTINGS:
        val = _bval(key)
        kb.append([InlineKeyboardButton(
            f"{'✅' if val else '🚫'} {label}",
            callback_data=f"acc:pfaq:toggle:{key}",
        )])
    kb.append([
        InlineKeyboardButton("📋 Browse Products", callback_data="acc:pfaq:list:0"),
        InlineKeyboardButton("🔍 Search FAQs", callback_data="acc:pfaq:search"),
    ])
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─── Products list (with FAQ counts) ─────────────────────────────────────

@require_admin
async def pfaq_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    with get_db_session() as s:
        products = (
            s.query(Product)
            .filter(Product.is_active == True)  # noqa: E712
            .order_by(Product.name.asc())
            .all()
        )
        prod_data = [(p.id, p.name) for p in products]

    items, pages, _ = paginate(prod_data, page, _PAGE)
    lines = ["❓ <b>PRODUCT FAQ MANAGER</b>", "", "Select a product to manage its FAQs:"]
    kb: List[List] = []
    for pid, pname in items:
        cnt = svc.faq_count(pid)
        label = f"📦 {pname[:30]}  [{cnt} FAQ{'s' if cnt != 1 else ''}]"
        kb.append([InlineKeyboardButton(label, callback_data=f"acc:pfaq:prod:{pid}:0")])

    # Navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"acc:pfaq:list:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"acc:pfaq:list:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([_back_menu()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─── FAQs for one product ─────────────────────────────────────────────────

@require_admin
async def pfaq_prod(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    product_id: int, page: int = 0):
    with get_db_session() as s:
        prod = s.query(Product).filter(Product.id == product_id).first()
        pname = prod.name if prod else f"#{product_id}"

    faqs = svc.get_faqs(product_id, active_only=False)
    items, pages, _ = paginate(faqs, page, _PAGE)

    mx = svc.max_per_product()
    total = svc.faq_count(product_id)
    limit_str = f"{total}/{mx}" if mx > 0 else str(total)
    lines = [
        f"❓ <b>FAQs — {pname[:40]}</b>",
        f"Total: {limit_str}",
    ]
    kb: List[List] = []
    for faq in items:
        status_dot = "🟢" if faq["is_active"] else "🔴"
        cat_label = svc.CATEGORIES.get(faq["category"], "")
        label = f"{status_dot} {faq['question'][:40]}"
        if cat_label:
            label += f"  ({cat_label})"
        kb.append([InlineKeyboardButton(label,
                                        callback_data=f"acc:pfaq:view:{faq['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"acc:pfaq:prod:{product_id}:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"acc:pfaq:prod:{product_id}:{page+1}"))
    if nav:
        kb.append(nav)

    # Check limit before showing Add button
    can_add = (mx == 0 or total < mx)
    action_row = []
    if can_add:
        action_row.append(InlineKeyboardButton("➕ Add FAQ",
                                               callback_data=f"acc:pfaq:add:{product_id}"))
    action_row.append(InlineKeyboardButton("🔍 Search",
                                           callback_data=f"acc:pfaq:search"))
    kb.append(action_row)
    kb.append([_back_list(0), back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


# ─── View single FAQ ──────────────────────────────────────────────────────

@require_admin
async def pfaq_view(update: Update, context: ContextTypes.DEFAULT_TYPE, faq_id: int):
    faq = svc.get_faq(faq_id)
    if not faq:
        await send(update, "❌ FAQ not found.",
                   InlineKeyboardMarkup([[_back_menu()]]))
        return

    cat_label = svc.CATEGORIES.get(faq["category"], faq["category"])
    status_str = "🟢 Active" if faq["is_active"] else "🔴 Inactive"
    text = (
        f"❓ <b>FAQ #{faq['id']}</b>\n\n"
        f"<b>Category:</b> {cat_label}\n"
        f"<b>Status:</b> {status_str}\n"
        f"<b>Sort:</b> {faq['sort_order']}\n\n"
        f"<b>Q:</b> {faq['question']}\n\n"
        f"<b>A:</b> {faq['answer']}"
    )
    pid = faq["product_id"]
    kb = [
        [InlineKeyboardButton("✏️ Edit Question", callback_data=f"acc:pfaq:edit:{faq_id}"),
         InlineKeyboardButton("📋 Edit Answer",   callback_data=f"acc:pfaq:edit:{faq_id}")],
        [InlineKeyboardButton("🔄 Duplicate",  callback_data=f"acc:pfaq:dup:{faq_id}"),
         InlineKeyboardButton("📤 Copy To",    callback_data=f"acc:pfaq:copy:{faq_id}")],
        [InlineKeyboardButton("⬆️ Move Up",    callback_data=f"acc:pfaq:up:{faq_id}"),
         InlineKeyboardButton("⬇️ Move Down",  callback_data=f"acc:pfaq:down:{faq_id}")],
        [InlineKeyboardButton(
            "🚫 Deactivate" if faq["is_active"] else "✅ Activate",
            callback_data=f"acc:pfaq:toggle_active:{faq_id}",
        )],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"acc:pfaq:del:{faq_id}")],
        [_back_prod(pid), _back_menu()],
    ]
    await send(update, text, InlineKeyboardMarkup(kb))


# ─── Delete (with confirmation) ───────────────────────────────────────────

async def pfaq_del_confirm(update, context, faq_id: int):
    faq = svc.get_faq(faq_id)
    if not faq:
        await pfaq_menu(update, context)
        return
    text = (
        f"🗑 <b>Delete FAQ #{faq_id}?</b>\n\n"
        f"<b>Q:</b> {faq['question'][:200]}\n\n"
        f"This action cannot be undone."
    )
    kb = [[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"acc:pfaq:delok:{faq_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"acc:pfaq:view:{faq_id}"),
    ]]
    await send(update, text, InlineKeyboardMarkup(kb))


async def pfaq_del_ok(update, context, faq_id: int):
    faq = svc.get_faq(faq_id)
    pid = faq["product_id"] if faq else None
    ok = svc.delete_faq(faq_id)
    msg = "✅ FAQ deleted." if ok else "❌ FAQ not found."
    try:
        log_admin_action(update.effective_user.id, "pfaq_deleted", f"faq_id={faq_id}")
    except Exception:
        pass
    if pid:
        await pfaq_prod(update, context, pid, 0)
    else:
        await pfaq_menu(update, context)


# ─── Quick actions ────────────────────────────────────────────────────────

async def pfaq_dup(update, context, faq_id: int):
    try:
        new_faq = svc.duplicate_faq(faq_id)
        msg = (f"✅ FAQ duplicated as #{new_faq['id']} (inactive — review before enabling)."
               if new_faq else "❌ Original FAQ not found.")
    except Exception as exc:
        msg = f"❌ {exc}"
    q = update.callback_query
    if q:
        await q.answer(msg[:200], show_alert=True)
    faq = svc.get_faq(faq_id)
    if faq:
        await pfaq_prod(update, context, faq["product_id"], 0)
    else:
        await pfaq_menu(update, context)


async def pfaq_move(update, context, faq_id: int, direction: str):
    svc.move_faq(faq_id, direction)
    await pfaq_view(update, context, faq_id)


async def pfaq_toggle_active(update, context, faq_id: int):
    faq = svc.get_faq(faq_id)
    if not faq:
        await pfaq_menu(update, context)
        return
    svc.edit_faq(faq_id, is_active=not faq["is_active"])
    await pfaq_view(update, context, faq_id)


# ─── Add FAQ conversation ─────────────────────────────────────────────────

@require_admin
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    product_id = int(parts[-1])
    context.user_data["pfaq_add_pid"] = product_id
    await q.message.reply_text(
        f"➕ <b>Add FAQ — Step 1/3</b>\n\nEnter the <b>question</b> (max 1000 chars):\n\n"
        f"/cancel to abort",
        parse_mode="HTML",
    )
    return ADD_Q


async def add_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Question cannot be empty. Try again or /cancel.")
        return ADD_Q
    context.user_data["pfaq_add_q"] = text[:1000]
    await update.message.reply_text(
        "📝 <b>Step 2/3</b> — Now enter the <b>answer</b> (max 3000 chars):",
        parse_mode="HTML",
    )
    return ADD_A


async def add_a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Answer cannot be empty. Try again or /cancel.")
        return ADD_A
    context.user_data["pfaq_add_a"] = text[:3000]

    # Build category keyboard
    cats = [(k, v) for k, v in svc.CATEGORIES.items()]
    kb = [[InlineKeyboardButton(lbl, callback_data=f"pfaq_cat:{key}")]
          for key, lbl in cats]
    await update.message.reply_text(
        "📂 <b>Step 3/3</b> — Select a <b>category</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ADD_CAT


async def add_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":")[1]
    pid = context.user_data.pop("pfaq_add_pid", None)
    question = context.user_data.pop("pfaq_add_q", "")
    answer = context.user_data.pop("pfaq_add_a", "")
    if not pid:
        await q.message.reply_text("Session expired. Start over.")
        return ConversationHandler.END
    try:
        faq = svc.add_faq(pid, question, answer, cat)
        try:
            log_admin_action(update.effective_user.id, "pfaq_added",
                             f"product_id={pid} faq_id={faq['id']}")
        except Exception:
            pass
        await q.message.reply_text(
            f"✅ FAQ added as <b>#{faq['id']}</b>.",
            parse_mode="HTML",
        )
    except ValueError as exc:
        await q.message.reply_text(f"❌ {exc}")
    return ConversationHandler.END


# ─── Edit FAQ conversation ────────────────────────────────────────────────

@require_admin
async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    faq_id = int(q.data.split(":")[-1])
    context.user_data["pfaq_edit_id"] = faq_id
    faq = svc.get_faq(faq_id)
    if not faq:
        await q.message.reply_text("FAQ not found.")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Question", callback_data="pfaq_ef:question"),
         InlineKeyboardButton("📋 Answer",   callback_data="pfaq_ef:answer"),
         InlineKeyboardButton("📂 Category", callback_data="pfaq_ef:category")],
    ])
    await q.message.reply_text(
        f"✏️ <b>Edit FAQ #{faq_id}</b>\n\nWhat would you like to edit?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data.split(":")[1]
    context.user_data["pfaq_edit_field"] = field
    faq_id = context.user_data.get("pfaq_edit_id")
    faq = svc.get_faq(faq_id)

    if field == "category":
        cats = [(k, v) for k, v in svc.CATEGORIES.items()]
        kb = [[InlineKeyboardButton(lbl, callback_data=f"pfaq_ev:{key}")]
              for key, lbl in cats]
        await q.message.reply_text(
            "📂 Select new category:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return EDIT_VALUE

    cur = faq.get(field, "") if faq else ""
    await q.message.reply_text(
        f"Current {field}:\n<i>{cur[:500]}</i>\n\n"
        f"Enter new {field} or /cancel:",
        parse_mode="HTML",
    )
    return EDIT_VALUE


async def edit_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    faq_id = context.user_data.pop("pfaq_edit_id", None)
    field = context.user_data.pop("pfaq_edit_field", None)
    if not faq_id or not field:
        await update.message.reply_text("Session expired.")
        return ConversationHandler.END
    try:
        svc.edit_faq(faq_id, **{field: text})
        await update.message.reply_text(f"✅ FAQ #{faq_id} {field} updated.")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
    return ConversationHandler.END


async def edit_value_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    value = q.data.split(":", 1)[1]
    faq_id = context.user_data.pop("pfaq_edit_id", None)
    field = context.user_data.pop("pfaq_edit_field", None)
    if not faq_id or not field:
        await q.message.reply_text("Session expired.")
        return ConversationHandler.END
    try:
        svc.edit_faq(faq_id, **{field: value})
        await q.message.reply_text(f"✅ FAQ #{faq_id} {field} updated.")
    except ValueError as exc:
        await q.message.reply_text(f"❌ {exc}")
    return ConversationHandler.END


# ─── Copy-to-product conversation ─────────────────────────────────────────

@require_admin
async def copy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    faq_id = int(q.data.split(":")[-1])
    context.user_data["pfaq_copy_id"] = faq_id
    await q.message.reply_text(
        f"📤 <b>Copy FAQ #{faq_id}</b>\n\nEnter the <b>target product ID</b> "
        f"to copy this FAQ to:\n\n/cancel to abort",
        parse_mode="HTML",
    )
    return COPY_TARGET


async def copy_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    faq_id = context.user_data.pop("pfaq_copy_id", None)
    if not faq_id:
        await update.message.reply_text("Session expired.")
        return ConversationHandler.END
    try:
        target_id = int(text)
        new_faq = svc.copy_faq_to_product(faq_id, target_id)
        await update.message.reply_text(
            f"✅ FAQ copied to product #{target_id} as <b>#{new_faq['id']}</b> (inactive — review before enabling).",
            parse_mode="HTML",
        )
        try:
            log_admin_action(update.effective_user.id, "pfaq_copied",
                             f"faq_id={faq_id} target_product={target_id}")
        except Exception:
            pass
    except (ValueError, TypeError):
        await update.message.reply_text("❌ Invalid product ID.")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
    return ConversationHandler.END


# ─── Admin search conversation ────────────────────────────────────────────

@require_admin
async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()
        await q.message.reply_text(
            "🔍 <b>FAQ Search</b>\n\n"
            "Enter <code>product_id: search term</code>  (e.g. <code>42: refund</code>)\n\n"
            "/cancel to abort",
            parse_mode="HTML",
        )
    return SEARCH_QUERY


async def search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if ":" not in text:
        await update.message.reply_text(
            "❌ Format: <code>product_id: search term</code>",
            parse_mode="HTML",
        )
        return SEARCH_QUERY
    pid_part, _, term = text.partition(":")
    try:
        pid = int(pid_part.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid product ID.")
        return SEARCH_QUERY
    results = svc.search_faqs(pid, term.strip())
    if not results:
        await update.message.reply_text("No FAQs matched your search.")
        return ConversationHandler.END
    lines = [f"🔍 Found {len(results)} result(s) for product #{pid}:\n"]
    for r in results[:15]:
        cat = svc.CATEGORIES.get(r["category"], "")
        lines.append(
            f"• <b>[#{r['id']}]</b> {cat}  {r['question'][:80]}\n"
            f"  <i>{r['answer'][:120]}…</i>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return ConversationHandler.END


async def _conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("pfaq_add_pid", "pfaq_add_q", "pfaq_add_a",
                "pfaq_edit_id", "pfaq_edit_field",
                "pfaq_copy_id"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ─── Conversation builders ────────────────────────────────────────────────

def build_pfaq_add_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^acc:pfaq:add:\d+$")],
        states={
            ADD_Q:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_q)],
            ADD_A:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_a)],
            ADD_CAT: [CallbackQueryHandler(add_cat, pattern=r"^pfaq_cat:[a-z]+$")],
        },
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_pfaq_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_start, pattern=r"^acc:pfaq:edit:\d+$")],
        states={
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern=r"^pfaq_ef:")],
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_text),
                CallbackQueryHandler(edit_value_cb, pattern=r"^pfaq_ev:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_pfaq_copy_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(copy_start, pattern=r"^acc:pfaq:copy:\d+$")],
        states={
            COPY_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_target)],
        },
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


def build_pfaq_search_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(search_start, pattern=r"^acc:pfaq:search$")],
        states={
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query)],
        },
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_user=True, per_chat=True, allow_reentry=True,
    )


# ─── Route dispatcher ─────────────────────────────────────────────────────

async def route(action: str, rest: list, update, context):
    q = getattr(update, "callback_query", None)
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    if not action or action == "menu":
        await pfaq_menu(update, context)
        return

    if action == "status" and rest:
        if rest[0] in ("enabled", "maintenance", "disabled"):
            cfg.set("pfaq_status", rest[0])
        await pfaq_menu(update, context)
        return

    if action == "toggle" and rest:
        key = rest[0]
        if key in dict(_BOOL_SETTINGS):
            cfg.set(key, not _bval(key))
        await pfaq_menu(update, context)
        return

    if action == "maxset" and rest:
        cfg.set("pfaq_max_per_product", rest[0])
        await pfaq_menu(update, context)
        return

    if action == "list":
        page = int(rest[0]) if rest else 0
        await pfaq_list(update, context, page)
        return

    if action == "prod" and len(rest) >= 2:
        try:
            await pfaq_prod(update, context, int(rest[0]), int(rest[1]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "view" and rest:
        try:
            await pfaq_view(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "del" and rest:
        try:
            await pfaq_del_confirm(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "delok" and rest:
        try:
            await pfaq_del_ok(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "dup" and rest:
        try:
            await pfaq_dup(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "up" and rest:
        try:
            await pfaq_move(update, context, int(rest[0]), "up")
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "down" and rest:
        try:
            await pfaq_move(update, context, int(rest[0]), "down")
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    if action == "toggle_active" and rest:
        try:
            await pfaq_toggle_active(update, context, int(rest[0]))
        except (ValueError, IndexError):
            await pfaq_menu(update, context)
        return

    await pfaq_menu(update, context)
