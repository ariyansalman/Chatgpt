"""V42 — Admin Plugin & Module Manager.

Callback namespace: ``pmm:*``

Callbacks:
  pmm:menu                  — Main dashboard
  pmm:list:PAGE             — Paginated module list
  pmm:view:SLUG             — Module detail
  pmm:set:SLUG:STATUS       — Change module status
  pmm:deps:SLUG             — Dependency check
  pmm:search                — Enter search mode (ConversationHandler)
  pmm:stats                 — Statistics panel
  pmm:settings              — Settings panel
  pmm:settings:toggle:KEY   — Toggle a boolean setting
"""
from __future__ import annotations

import json
import logging
from math import ceil
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from telegram.error import BadRequest

from services.module_manager import (
    get_all_modules, get_module, set_module_status, check_dependencies,
    get_module_stats, STATUS_EMOJI, STATUS_LABEL, VALID_STATUSES,
)
from services.global_timeline import record_module_change
from utils.audit import log_admin_action
from utils.permissions import has_permission
from utils.bot_config import cfg
from utils.update_proxy import with_data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8
_SEARCH_STATE = 9100   # ConversationHandler state


# ─── Keyboard builders ────────────────────────────────────────────────────────

def _back_kb(callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=callback)
    ]])


def _main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📋 All Modules",   callback_data="pmm:list:1"),
         InlineKeyboardButton("📊 Statistics",     callback_data="pmm:stats")],
        [InlineKeyboardButton("🔍 Search Module",  callback_data="pmm:search"),
         InlineKeyboardButton("⚙️ Settings",       callback_data="pmm:settings")],
        [InlineKeyboardButton("🔙 Admin Panel",    callback_data="acc:root")],
    ]
    return InlineKeyboardMarkup(kb)


def _module_list_kb(modules, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for m in modules:
        emoji = STATUS_EMOJI.get(m.status, "❓")
        rows.append([InlineKeyboardButton(
            f"{emoji} {m.name}",
            callback_data=f"pmm:view:{m.slug}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pmm:list:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"pmm:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="pmm:menu")])
    return InlineKeyboardMarkup(rows)


def _module_detail_kb(slug: str, current_status: str, is_core: bool) -> InlineKeyboardMarkup:
    rows = []
    # Status change buttons — only show buttons for OTHER statuses
    status_row = []
    for s, emoji in [("enabled", "🟢"), ("maintenance", "🟡"), ("disabled", "🔴")]:
        if s != current_status:
            label = f"{emoji} Set {STATUS_LABEL[s]}"
            if is_core and s == "disabled":
                label = "🔒 Core (cannot disable)"
                status_row.append(InlineKeyboardButton(label, callback_data="pmm:noop"))
            else:
                status_row.append(InlineKeyboardButton(label, callback_data=f"pmm:set:{slug}:{s}"))
    if status_row:
        # Split into two rows if needed
        rows.append(status_row[:2])
        if len(status_row) > 2:
            rows.append(status_row[2:])
    rows.append([
        InlineKeyboardButton("🔗 Check Deps", callback_data=f"pmm:deps:{slug}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Module List", callback_data="pmm:list:1")])
    return InlineKeyboardMarkup(rows)


def _settings_kb() -> InlineKeyboardMarkup:
    dep_check  = cfg.get_bool("pmm_dependency_check", True)
    safe_mode  = cfg.get_bool("pmm_safe_mode", True)
    mod_logs   = cfg.get_bool("pmm_module_logs", True)
    auto_health= cfg.get_bool("pmm_auto_health_check", False)

    def _toggle_btn(label: str, key: str, val: bool) -> InlineKeyboardButton:
        icon = "✅" if val else "☑️"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"pmm:settings:toggle:{key}")

    kb = [
        [_toggle_btn("Dependency Check", "pmm_dependency_check", dep_check)],
        [_toggle_btn("Safe Mode",         "pmm_safe_mode",         safe_mode)],
        [_toggle_btn("Module Logs",        "pmm_module_logs",       mod_logs)],
        [_toggle_btn("Auto Health Check",  "pmm_auto_health_check", auto_health)],
        [InlineKeyboardButton("🔙 Back", callback_data="pmm:menu")],
    ]
    return InlineKeyboardMarkup(kb)


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def pmm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    stats = get_module_stats()
    text = (
        "🧩 <b>Plugin & Module Manager</b>\n\n"
        f"Total Modules:  <b>{stats['total']}</b>\n"
        f"🟢 Enabled:      <b>{stats.get('enabled', 0)}</b>\n"
        f"🟡 Maintenance:  <b>{stats.get('maintenance', 0)}</b>\n"
        f"🔴 Disabled:     <b>{stats.get('disabled', 0)}</b>\n"
        f"🔒 Core (locked): <b>{stats.get('core', 0)}</b>\n"
    )
    try:
        await query.edit_message_text(text, reply_markup=_main_menu_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = query.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    all_mods = get_all_modules()
    total_pages = max(1, ceil(len(all_mods) / _PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * _PAGE_SIZE
    modules = all_mods[start: start + _PAGE_SIZE]

    text = (
        f"🧩 <b>All Modules</b>  (page {page}/{total_pages})\n\n"
        "Tap a module to view details and manage its status."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_module_list_kb(modules, page, total_pages),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    slug = query.data.split(":", 2)[2]
    mod = get_module(slug)
    if mod is None:
        await query.answer("Module not found.", show_alert=True)
        return

    try:
        deps = json.loads(mod.dependencies or "[]")
    except Exception:
        deps = []

    status_emoji = STATUS_EMOJI.get(mod.status, "❓")
    deps_str = ", ".join(deps) if deps else "None"
    core_str  = "🔒 Yes (cannot disable)" if mod.is_core else "No"
    updated   = mod.last_updated_at.strftime("%Y-%m-%d %H:%M") if mod.last_updated_at else "—"

    text = (
        f"🧩 <b>{mod.name}</b>\n\n"
        f"<b>Slug:</b>        {mod.slug}\n"
        f"<b>Version:</b>     {mod.version or '—'}\n"
        f"<b>Status:</b>      {status_emoji} {STATUS_LABEL.get(mod.status, mod.status)}\n"
        f"<b>Category:</b>    {mod.category or '—'}\n"
        f"<b>Core Module:</b> {core_str}\n"
        f"<b>Author:</b>      {mod.author or '—'}\n"
        f"<b>Dependencies:</b> {deps_str}\n"
        f"<b>Last Updated:</b> {updated}\n\n"
        f"<b>Description:</b>\n{mod.description or '—'}"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_module_detail_kb(slug, mod.status, mod.is_core),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    parts = query.data.split(":")   # pmm:set:SLUG:STATUS
    slug   = parts[2]
    status = parts[3]

    # Get old status for timeline
    mod = get_module(slug)
    old_status = mod.status if mod else "unknown"

    # Optional dependency check before enabling
    if status == "enabled" and cfg.get_bool("pmm_dependency_check", True):
        dep_result = check_dependencies(slug)
        if not dep_result["ok"]:
            issues = []
            if dep_result["missing"]:
                issues.append("Missing: " + ", ".join(dep_result["missing"]))
            if dep_result["disabled"]:
                issues.append("Disabled deps: " + ", ".join(dep_result["disabled"]))
            if dep_result["circular"]:
                issues.append("Circular dependency detected.")
            await query.answer(
                "⚠️ Dependency issue:\n" + "\n".join(issues),
                show_alert=True,
            )
            return

    ok, msg = set_module_status(slug, status)
    if not ok:
        await query.answer(f"❌ {msg}", show_alert=True)
        return

    # Audit + timeline
    admin_id = update.effective_user.id
    log_admin_action(admin_id, f"module.set_status", "module", slug,
                     details=f"{old_status} → {status}", module="plugin_module_manager")
    record_module_change(admin_id, slug, old_status, status)

    await query.answer(f"✅ {msg}")
    # Refresh the detail view
    # Rebuild data callback to re-show view
    await pmm_view(with_data(update, f"pmm:view:{slug}"), context)


async def pmm_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-op callback for locked core-module disable buttons."""
    query = update.callback_query
    await query.answer("🔒 Core modules cannot be disabled.", show_alert=True)


async def pmm_deps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    slug = query.data.split(":", 2)[2]
    mod  = get_module(slug)
    if mod is None:
        await query.answer("Module not found.", show_alert=True)
        return

    result = check_dependencies(slug)
    if result["ok"]:
        status_line = "✅ All dependencies satisfied."
    else:
        lines = []
        if result["missing"]:
            lines.append("❌ Missing: " + ", ".join(result["missing"]))
        if result["disabled"]:
            lines.append("🔴 Disabled deps: " + ", ".join(result["disabled"]))
        if result["circular"]:
            lines.append("🔄 Circular dependency detected.")
        status_line = "\n".join(lines)

    try:
        deps = json.loads(mod.dependencies or "[]")
    except Exception:
        deps = []

    text = (
        f"🔗 <b>Dependency Check — {mod.name}</b>\n\n"
        f"<b>Declared deps:</b> {', '.join(deps) or 'None'}\n\n"
        f"{status_line}"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_back_kb(f"pmm:view:{slug}"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    mods = get_all_modules()
    by_cat: dict[str, list] = {}
    for m in mods:
        cat = m.category or "misc"
        by_cat.setdefault(cat, []).append(m)

    cat_lines = []
    for cat, ms in sorted(by_cat.items()):
        en = sum(1 for m in ms if m.status == "enabled")
        mn = sum(1 for m in ms if m.status == "maintenance")
        di = sum(1 for m in ms if m.status == "disabled")
        cat_lines.append(f"  <b>{cat}</b>: {en}🟢 {mn}🟡 {di}🔴")

    stats = get_module_stats()
    text = (
        "📊 <b>Module Statistics</b>\n\n"
        f"Total:       <b>{stats['total']}</b>\n"
        f"🟢 Enabled:   <b>{stats.get('enabled', 0)}</b>\n"
        f"🟡 Maintenance:<b>{stats.get('maintenance', 0)}</b>\n"
        f"🔴 Disabled:  <b>{stats.get('disabled', 0)}</b>\n"
        f"🔒 Core:      <b>{stats.get('core', 0)}</b>\n\n"
        "<b>By Category:</b>\n" + "\n".join(cat_lines)
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=_back_kb("pmm:menu"),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    text = (
        "⚙️ <b>Module Manager Settings</b>\n\n"
        "Configure how the Plugin & Module Manager behaves."
    )
    try:
        await query.edit_message_text(text, reply_markup=_settings_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def pmm_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    key = query.data.split(":", 3)[3]   # pmm:settings:toggle:KEY
    allowed_keys = {
        "pmm_dependency_check", "pmm_safe_mode",
        "pmm_module_logs", "pmm_auto_health_check",
    }
    if key not in allowed_keys:
        await query.answer("Unknown key.", show_alert=True)
        return

    old = cfg.get_bool(key, True)
    new_val = "false" if old else "true"
    try:
        with __import__("database").get_db_session() as s:
            from database.models import BotConfig
            row = s.query(BotConfig).filter_by(key=key).first()
            if row:
                row.value = new_val
            else:
                s.add(BotConfig(key=key, value=new_val, value_type="bool",
                                category="plugin_module_manager", label=key))
            s.commit()
    except Exception:
        logger.exception("pmm_settings_toggle: failed to save key=%s", key)
        await query.answer("❌ Failed to save setting.", show_alert=True)
        return

    log_admin_action(update.effective_user.id, "module_settings.toggle", "config", key,
                     details=f"{old} → {not old}", module="plugin_module_manager")
    await pmm_settings(with_data(update, "pmm:settings"), context)


# ─── Search conversation ──────────────────────────────────────────────────────

async def pmm_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "admin"):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        "🔍 <b>Search Modules</b>\n\nSend a module name or keyword to search:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="pmm:menu")
        ]]),
        parse_mode="HTML",
    )
    return _SEARCH_STATE


async def pmm_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    await update.message.delete()

    all_mods = get_all_modules()
    results = [
        m for m in all_mods
        if text in m.name.lower() or text in (m.slug or "").lower()
        or text in (m.description or "").lower()
        or text in (m.category or "").lower()
    ]

    if not results:
        msg_text = f"🔍 No modules found for <b>{text}</b>."
        kb = _back_kb("pmm:menu")
    else:
        msg_text = f"🔍 <b>Search Results</b> for <i>{text}</i>:\n"
        kb_rows = []
        for m in results[:20]:
            emoji = STATUS_EMOJI.get(m.status, "❓")
            kb_rows.append([InlineKeyboardButton(
                f"{emoji} {m.name}", callback_data=f"pmm:view:{m.slug}"
            )])
        kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="pmm:menu")])
        kb = InlineKeyboardMarkup(kb_rows)

    await update.effective_chat.send_message(msg_text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


# ─── Registration ─────────────────────────────────────────────────────────────

def register_handlers(application) -> None:
    """Register all pmm:* handlers."""
    application.add_handler(CallbackQueryHandler(pmm_menu,    pattern=r"^pmm:menu$"))
    application.add_handler(CallbackQueryHandler(pmm_list,    pattern=r"^pmm:list:\d+$"))
    application.add_handler(CallbackQueryHandler(pmm_view,    pattern=r"^pmm:view:.+$"))
    application.add_handler(CallbackQueryHandler(pmm_set,     pattern=r"^pmm:set:.+:.+$"))
    application.add_handler(CallbackQueryHandler(pmm_noop,    pattern=r"^pmm:noop$"))
    application.add_handler(CallbackQueryHandler(pmm_deps,    pattern=r"^pmm:deps:.+$"))
    application.add_handler(CallbackQueryHandler(pmm_stats,   pattern=r"^pmm:stats$"))
    application.add_handler(CallbackQueryHandler(pmm_settings, pattern=r"^pmm:settings$"))
    application.add_handler(CallbackQueryHandler(
        pmm_settings_toggle, pattern=r"^pmm:settings:toggle:.+$"
    ))

    # Search conversation
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(pmm_search_start, pattern=r"^pmm:search$")],
        states={
            _SEARCH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pmm_search_query)],
        },
        fallbacks=[CallbackQueryHandler(pmm_menu, pattern=r"^pmm:menu$")],
        per_message=False,
    )
    application.add_handler(search_conv)

    logger.info("admin_module_manager: handlers registered (pmm:*)")
