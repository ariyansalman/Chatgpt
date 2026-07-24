"""Customer Notes & CRM System — Admin Panel (V33).

Callback namespace: crm:*
ConversationHandler states: 9601–9605

Route map
---------
crm:home                          — Dashboard
crm:list[:<pg>]                   — All CRM profiles (paginated)
crm:user:<db_uid>                 — Full CRM card for one user
crm:notes:arch:<db_uid>           — Archived notes for user
crm:note:add:<db_uid>             — ConvHandler: prompt for note text
crm:note:edit:<note_id>:<db_uid>  — ConvHandler: prompt for updated text
crm:note:pin:<note_id>:<db_uid>   — Toggle pin on note
crm:note:arch:<note_id>:<db_uid>  — Toggle archive on note
crm:note:del:<note_id>:<db_uid>   — Ask for confirmation
crm:note:delok:<note_id>:<db_uid> — Execute deletion
crm:tags                          — Tag management list
crm:tag:add                       — ConvHandler: prompt for tag name
crm:tag:del:<tag_id>              — Delete a global tag
crm:tag:u:<db_uid>                — Manage tags for a user
crm:tag:a:<tag_id>:<db_uid>       — Assign tag to user
crm:tag:r:<tag_id>:<db_uid>       — Remove tag from user
crm:sbytag:<tag_id>               — Users carrying a tag
crm:prio:<db_uid>                 — Priority picker
crm:prio:set:<db_uid>:<level>     — Set priority
crm:stat:<db_uid>                 — Status picker
crm:stat:set:<db_uid>:<value>     — Set status
crm:rem:add:<db_uid>              — ConvHandler: prompt for reminder
crm:rem:done:<rem_id>:<db_uid>    — Mark reminder complete
crm:rem:del:<rem_id>:<db_uid>     — Delete reminder
crm:timeline:<db_uid>             — Customer event timeline
crm:export:<db_uid>               — Send notes as text message
crm:search                        — ConvHandler: prompt for search term
crm:settings                      — Settings panel
crm:setstatus:<v>                 — Set feature status
crm:toggle:<key>                  — Toggle bool config key
crm:setval:<key>:<val>            — Set int config value
crm:noop                          — No-op
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from utils.permissions import has_permission
from utils.bot_config import cfg
from utils.update_proxy import with_data
from services.customer_crm import (
    add_note, edit_note, delete_note, pin_note, archive_note,
    get_notes, search_notes,
    create_tag, delete_tag, assign_tag, remove_tag,
    get_tags, get_user_tags, search_by_tag,
    get_or_create_profile, set_priority, set_crm_status,
    add_reminder, complete_reminder, delete_reminder, get_reminders,
    get_crm_stats, get_customer_timeline, export_customer_notes_text,
)

logger = logging.getLogger(__name__)

# ── ConversationHandler state constants ──────────────────────────────────────
CRM_ADD_NOTE    = 9601
CRM_EDIT_NOTE   = 9602
CRM_ADD_TAG     = 9603
CRM_ADD_REMINDER = 9604
CRM_SEARCH       = 9605

_PAGE = 8


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _status() -> str:
    return str(cfg.get("crm_status", "enabled") or "enabled")

def _semoji(s: str) -> str:
    return {"enabled": "🟢", "maintenance": "🟡", "disabled": "🔴"}.get(s, "⚪")

def _is_active() -> bool:
    return _status() in ("enabled", "maintenance")

def _prio_emoji(p: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(p, "⚪")

def _status_label(s: str, custom: str | None = None) -> str:
    labels = {
        "new_customer": "New Customer",  "returning": "Returning Customer",
        "vip": "VIP Customer",           "reseller": "Reseller",
        "wholesale": "Wholesale",        "blocked": "Blocked",
        "suspended": "Suspended",        "verified": "Verified",
        "custom": custom or "Custom",
    }
    return labels.get(s, s)

def _fmt(dt: datetime | None) -> str:
    if not dt:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M")

def _uname(tg_id: int | None, username: str | None) -> str:
    return f"@{username}" if username else f"TG#{tg_id}"

def _check(tg_id: int) -> bool:
    return has_permission(tg_id, "manage_users")

def _back_home() -> list:
    return [[InlineKeyboardButton("🔙 CRM Dashboard", callback_data="crm:home")]]

def _back_user(db_uid: int) -> list:
    return [[InlineKeyboardButton("🔙 CRM Profile", callback_data=f"crm:user:{db_uid}")]]

async def _safe_edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _parse_reminder_dt(raw: str) -> datetime | None:
    """Parse admin-supplied date string into a UTC datetime.

    Accepted formats:
      YYYY-MM-DD HH:MM     — absolute
      +N days              — relative (e.g. "+3 days")
      tomorrow             — next calendar day at 09:00 UTC
    """
    raw = raw.strip().lower()
    try:
        if raw == "tomorrow":
            return (datetime.utcnow() + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
        if raw.startswith("+"):
            parts = raw.lstrip("+").split()
            days = int(parts[0])
            return datetime.utcnow() + timedelta(days=days)
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except Exception:
        return None


# ─── Dashboard ────────────────────────────────────────────────────────────────

def _home_text(stats: dict) -> str:
    st = _status()
    se = _semoji(st)
    return (
        "📝 <b>Customer CRM Dashboard</b>\n"
        f"Status: {se} {st.title()}\n\n"
        f"👥 CRM Profiles:         <b>{stats.get('total_profiles', 0)}</b>\n"
        f"⭐ VIP Customers:        <b>{stats.get('vip_count', 0)}</b>\n"
        f"🏭 Wholesale Customers:  <b>{stats.get('wholesale_count', 0)}</b>\n"
        f"📝 Active Notes:         <b>{stats.get('total_notes', 0)}</b>\n"
        f"🏷 Total Tags:           <b>{stats.get('total_tags', 0)}</b>\n\n"
        "📅 <b>Reminders</b>\n"
        f"  ⏳ Pending:   <b>{stats.get('pending_reminders', 0)}</b>\n"
        f"  ✅ Completed: <b>{stats.get('completed_reminders', 0)}</b>\n"
        f"  👤 Users with pending: <b>{stats.get('with_reminders', 0)}</b>\n"
    )

def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 CRM Profiles",    callback_data="crm:list:0"),
         InlineKeyboardButton("🏷 Manage Tags",     callback_data="crm:tags")],
        [InlineKeyboardButton("🔍 Search Notes",   callback_data="crm:search"),
         InlineKeyboardButton("⚙️ Settings",        callback_data="crm:settings")],
        [InlineKeyboardButton("🔙 Admin Panel",     callback_data="acc:root")],
    ])

async def crm_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    if not _is_active():
        await query.answer("🔴 Customer CRM is disabled.", show_alert=True); return
    stats = get_crm_stats()
    await _safe_edit(query, _home_text(stats), _home_kb())


# ─── CRM profiles list ────────────────────────────────────────────────────────

async def crm_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:list:<pg> — all users who have a CRM profile."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0

    from database import get_db_session
    from database.models import CustomerProfile, User
    from sqlalchemy import text

    rows_data: list[dict] = []
    total = 0
    try:
        with get_db_session() as s:
            total = s.query(CustomerProfile).count()
            profiles = (
                s.query(CustomerProfile, User)
                .join(User, User.id == CustomerProfile.user_id)
                .order_by(CustomerProfile.updated_at.desc())
                .offset(page * _PAGE).limit(_PAGE)
                .all()
            )
            for cp, u in profiles:
                rows_data.append({
                    "uid": u.id, "tg_id": u.telegram_id, "username": u.username,
                    "priority": cp.priority, "crm_status": cp.crm_status,
                    "notes_count": cp.notes_count,
                })
    except Exception:
        logger.debug("crm_list DB query failed", exc_info=True)

    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
    text = f"👥 <b>CRM Profiles</b>  <i>({total} total, page {page + 1}/{total_pages})</i>\n\n"

    kb_rows: list[list] = []
    for r in rows_data:
        pe   = _prio_emoji(r["priority"])
        uname = _uname(r["tg_id"], r["username"])
        ns   = r["notes_count"]
        text += f"{pe} <b>{html.escape(uname)}</b>  notes:{ns}  [{r['crm_status']}]\n"
        kb_rows.append([InlineKeyboardButton(
            f"{pe} {uname} — {r['crm_status']}",
            callback_data=f"crm:user:{r['uid']}",
        )])

    if not rows_data:
        text += "<i>No CRM profiles yet.</i>\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"crm:list:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="crm:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"crm:list:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)
    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Full CRM profile card ────────────────────────────────────────────────────

async def crm_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:user:<db_uid> — full CRM card with notes, tags, reminders."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return

    # Fetch user info
    from database import get_db_session
    from database.models import User as DbUser
    tg_id = None
    username = None
    try:
        with get_db_session() as s:
            u = s.query(DbUser).filter_by(id=db_uid).first()
            if not u:
                await query.answer("User not found.", show_alert=True); return
            tg_id    = u.telegram_id
            username = u.username
    except Exception:
        pass

    profile = get_or_create_profile(db_uid, update.effective_user.id)
    notes   = get_notes(db_uid, include_archived=False)
    tags    = get_user_tags(db_uid)
    rems    = get_reminders(db_uid, pending_only=True)

    uname_esc = html.escape(_uname(tg_id, username))
    pe  = _prio_emoji(profile.get("priority", "low"))
    st  = _status_label(profile.get("crm_status", "new_customer"),
                        profile.get("custom_status"))

    text = (
        f"📝 <b>CRM Profile</b>  —  {uname_esc}\n"
        f"🆔 DB#{db_uid}  TG#{tg_id}\n"
        f"{'━' * 24}\n"
        f"{pe} Priority:  <b>{profile.get('priority', 'low').title()}</b>\n"
        f"🏷 Status:    <b>{html.escape(st)}</b>\n"
        f"🏷 Tags:      {', '.join(html.escape(t['name']) for t in tags) or '<i>none</i>'}\n"
        f"{'━' * 24}\n"
        f"📝 <b>Notes ({len(notes)} active)</b>\n"
    )

    for n in notes[:5]:
        pin  = "📌 " if n["is_pinned"] else ""
        when = _fmt(n.get("created_at"))
        snip = html.escape((n.get("content") or "")[:60])
        text += f"  {pin}[{when}] <i>{snip}</i>\n"
    if len(notes) > 5:
        text += f"  <i>… and {len(notes) - 5} more</i>\n"
    if not notes:
        text += "  <i>No notes yet.</i>\n"

    text += f"{'━' * 24}\n📅 <b>Pending Reminders ({len(rems)})</b>\n"
    for r in rems[:3]:
        text += f"  ⏰ {_fmt(r.get('remind_at'))}  {html.escape((r.get('reason') or '')[:40])}\n"
    if not rems:
        text += "  <i>No pending reminders.</i>\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Note",      callback_data=f"crm:note:add:{db_uid}"),
         InlineKeyboardButton("🗄 View All Notes", callback_data=f"crm:notes:all:{db_uid}")],
        [InlineKeyboardButton("📌 Archived Notes",callback_data=f"crm:notes:arch:{db_uid}"),
         InlineKeyboardButton("🏷 Tags",          callback_data=f"crm:tag:u:{db_uid}")],
        [InlineKeyboardButton(f"{pe} Priority",   callback_data=f"crm:prio:{db_uid}"),
         InlineKeyboardButton("🔖 Status",        callback_data=f"crm:stat:{db_uid}")],
        [InlineKeyboardButton("📅 Add Reminder",  callback_data=f"crm:rem:add:{db_uid}"),
         InlineKeyboardButton("📜 Timeline",      callback_data=f"crm:timeline:{db_uid}")],
        [InlineKeyboardButton("📤 Export Notes",  callback_data=f"crm:export:{db_uid}"),
         InlineKeyboardButton("🔍 Customer 360°", callback_data=f"c360:view:{db_uid}")],
        *_back_home(),
    ])
    await _safe_edit(query, text, kb)


# ─── All notes for user ───────────────────────────────────────────────────────

async def crm_notes_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:notes:all:<db_uid> — full active note list with per-note actions."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    notes = get_notes(db_uid, include_archived=False)
    text  = f"📝 <b>Active Notes</b>  (DB#{db_uid})\n{'━' * 24}\n\n"
    kb_rows: list[list] = []

    for n in notes:
        pin  = "📌" if n["is_pinned"] else "📝"
        when = _fmt(n.get("created_at"))
        adm  = html.escape(n.get("admin_name") or "Admin")
        snip = html.escape((n.get("content") or "")[:120])
        upd  = _fmt(n.get("updated_at")) if n.get("updated_at") != n.get("created_at") else None
        text += f"{pin} <b>[{when}]</b> by {adm}"
        if upd:
            text += f"  <i>(edited {upd})</i>"
        text += f"\n{snip}\n\n"

        note_id = n["id"]
        kb_rows.append([
            InlineKeyboardButton("📝 Edit",    callback_data=f"crm:note:edit:{note_id}:{db_uid}"),
            InlineKeyboardButton("📌 Pin",     callback_data=f"crm:note:pin:{note_id}:{db_uid}"),
            InlineKeyboardButton("🗄 Archive", callback_data=f"crm:note:arch:{note_id}:{db_uid}"),
            InlineKeyboardButton("🗑 Delete",  callback_data=f"crm:note:del:{note_id}:{db_uid}"),
        ])

    if not notes:
        text += "<i>No active notes.</i>"

    kb_rows.append([InlineKeyboardButton("➕ Add Note", callback_data=f"crm:note:add:{db_uid}")])
    kb_rows.extend(_back_user(db_uid))
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


async def crm_notes_archived(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:notes:arch:<db_uid> — archived notes."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    all_notes = get_notes(db_uid, include_archived=True)
    archived  = [n for n in all_notes if n.get("is_archived")]
    text      = f"🗄 <b>Archived Notes</b>  (DB#{db_uid})\n{'━' * 24}\n\n"
    kb_rows: list[list] = []

    for n in archived:
        when = _fmt(n.get("created_at"))
        adm  = html.escape(n.get("admin_name") or "Admin")
        snip = html.escape((n.get("content") or "")[:100])
        text += f"🗄 <b>[{when}]</b> by {adm}\n{snip}\n\n"
        note_id = n["id"]
        kb_rows.append([
            InlineKeyboardButton("↩️ Unarchive", callback_data=f"crm:note:arch:{note_id}:{db_uid}"),
            InlineKeyboardButton("🗑 Delete",    callback_data=f"crm:note:del:{note_id}:{db_uid}"),
        ])

    if not archived:
        text += "<i>No archived notes.</i>"

    kb_rows.extend(_back_user(db_uid))
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Note actions (pin / archive / delete) ────────────────────────────────────

async def crm_note_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        note_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    pin_note(note_id, db_uid)
    await query.answer("📌 Pin toggled.", show_alert=False)
    # Refresh notes list
    await crm_notes_all(with_data(update, f"crm:notes:all:{db_uid}"), context)


async def crm_note_arch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        note_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    archive_note(note_id, db_uid, update.effective_user.id)
    await query.answer("🗄 Archive toggled.", show_alert=False)
    await crm_notes_all(with_data(update, f"crm:notes:all:{db_uid}"), context)


async def crm_note_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:note:del:<note_id>:<db_uid> — ask for confirmation."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        note_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        return
    await _safe_edit(
        query,
        "🗑 <b>Delete this note?</b>\n\nThis action cannot be undone.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete",
                                  callback_data=f"crm:note:delok:{note_id}:{db_uid}"),
             InlineKeyboardButton("❌ Cancel",
                                  callback_data=f"crm:notes:all:{db_uid}")],
        ]),
    )


async def crm_note_del_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:note:delok:<note_id>:<db_uid> — execute deletion."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        note_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        return
    delete_note(note_id, db_uid, update.effective_user.id)
    await query.answer("🗑 Note deleted.", show_alert=False)
    await crm_notes_all(with_data(update, f"crm:notes:all:{db_uid}"), context)


# ─── Add Note — ConversationHandler ──────────────────────────────────────────

async def crm_note_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """crm:note:add:<db_uid> — entry point, prompt admin for note text."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["crm_note_uid"] = db_uid
    await query.message.reply_text(
        "📝 <b>Add Customer Note</b>\n\nSend the note text now. "
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return CRM_ADD_NOTE


async def crm_note_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive note text and save."""
    if not _check(update.effective_user.id):
        return ConversationHandler.END

    db_uid = context.user_data.get("crm_note_uid")
    if not db_uid:
        return ConversationHandler.END

    content    = (update.message.text or "").strip()
    admin      = update.effective_user
    admin_name = admin.username or admin.first_name or str(admin.id)

    result = add_note(db_uid, admin.id, admin_name, content)
    if result is None:
        max_n = cfg.get_int("crm_max_notes", 0)
        if max_n:
            await update.message.reply_text(
                f"⚠️ Note limit reached ({max_n} max). Archive or delete an existing note first."
            )
        else:
            await update.message.reply_text("⚠️ Could not save note. Please try again.")
    else:
        await update.message.reply_text(
            f"✅ Note saved (ID #{result}).\n\n"
            f"Use /admin or the keyboard to return to the CRM profile.",
        )

    context.user_data.pop("crm_note_uid", None)
    return ConversationHandler.END


# ─── Edit Note — ConversationHandler ─────────────────────────────────────────

async def crm_note_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """crm:note:edit:<note_id>:<db_uid> — prompt admin for updated text."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    parts = query.data.split(":")
    try:
        note_id = int(parts[3])
        db_uid  = int(parts[4])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["crm_edit_note_id"]  = note_id
    context.user_data["crm_edit_note_uid"] = db_uid
    await query.message.reply_text(
        "✏️ <b>Edit Note</b>\n\nSend the new text. Send /cancel to abort.",
        parse_mode="HTML",
    )
    return CRM_EDIT_NOTE


async def crm_note_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new text and update note."""
    if not _check(update.effective_user.id):
        return ConversationHandler.END

    note_id = context.user_data.get("crm_edit_note_id")
    db_uid  = context.user_data.get("crm_edit_note_uid")
    if not note_id or not db_uid:
        return ConversationHandler.END

    new_text = (update.message.text or "").strip()
    if edit_note(note_id, update.effective_user.id, new_text):
        await update.message.reply_text("✅ Note updated successfully.")
    else:
        await update.message.reply_text("⚠️ Could not update note.")

    context.user_data.pop("crm_edit_note_id", None)
    context.user_data.pop("crm_edit_note_uid", None)
    return ConversationHandler.END


# ─── Tags ─────────────────────────────────────────────────────────────────────

async def crm_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:tags — global tag management list."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    tags = get_tags()
    text = f"🏷 <b>Customer Tags</b>  ({len(tags)} total)\n{'━' * 24}\n\n"
    kb_rows: list[list] = []

    for t in tags:
        text += f"🏷 <b>{html.escape(t['name'])}</b>\n"
        kb_rows.append([
            InlineKeyboardButton(f"🏷 {t['name']}", callback_data=f"crm:sbytag:{t['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"crm:tag:del:{t['id']}"),
        ])

    if not tags:
        text += "<i>No tags defined.</i>\n"

    kb_rows.append([InlineKeyboardButton("➕ Create Tag", callback_data="crm:tag:add")])
    kb_rows.extend(_back_home())
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


async def crm_tag_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """crm:tag:add — prompt admin for new tag name."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.message.reply_text(
        "🏷 <b>Create New Tag</b>\n\n"
        "Send the tag name (e.g. <i>High Value</i>). "
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return CRM_ADD_TAG


async def crm_tag_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _check(update.effective_user.id):
        return ConversationHandler.END

    name = (update.message.text or "").strip()[:64]
    if not name:
        await update.message.reply_text("⚠️ Tag name cannot be empty.")
        return ConversationHandler.END

    tag_id = create_tag(name, None, update.effective_user.id)
    if tag_id:
        await update.message.reply_text(f"✅ Tag <b>{html.escape(name)}</b> created (ID #{tag_id}).",
                                         parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Could not create tag (may already exist).")
    return ConversationHandler.END


async def crm_tag_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    try:
        tag_id = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        await query.answer(); return
    delete_tag(tag_id)
    await query.answer("🗑 Tag deleted.", show_alert=False)
    await crm_tags(with_data(update, "crm:tags"), context)


async def crm_tag_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:tag:u:<db_uid> — manage tags for a specific user."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    all_tags  = get_tags()
    user_tags = {t["id"] for t in get_user_tags(db_uid)}

    text = f"🏷 <b>Tags for DB#{db_uid}</b>\n\nTap to assign or remove:\n"
    kb_rows: list[list] = []

    for t in all_tags:
        assigned = t["id"] in user_tags
        em  = "✅" if assigned else "➕"
        action = f"crm:tag:r:{t['id']}:{db_uid}" if assigned else f"crm:tag:a:{t['id']}:{db_uid}"
        kb_rows.append([InlineKeyboardButton(f"{em} {t['name']}", callback_data=action)])

    if not all_tags:
        text += "<i>No tags exist yet. Create tags first.</i>"

    kb_rows.extend(_back_user(db_uid))
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


async def crm_tag_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        tag_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    assign_tag(db_uid, tag_id, update.effective_user.id)
    await query.answer("✅ Tag assigned.", show_alert=False)
    await crm_tag_user(with_data(update, f"crm:tag:u:{db_uid}"), context)


async def crm_tag_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        tag_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    remove_tag(db_uid, tag_id)
    await query.answer("🗑 Tag removed.", show_alert=False)
    await crm_tag_user(with_data(update, f"crm:tag:u:{db_uid}"), context)


async def crm_search_by_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:sbytag:<tag_id> — list users carrying a tag."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        tag_id = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    # Get tag name
    all_tags = get_tags()
    tag_name = next((t["name"] for t in all_tags if t["id"] == tag_id), f"#{tag_id}")

    users = search_by_tag(tag_id, limit=30)
    text  = f"🏷 <b>Tag: {html.escape(tag_name)}</b>  ({len(users)} users)\n\n"
    kb_rows: list[list] = []

    for u in users:
        uname = _uname(u["telegram_id"], u["username"])
        text += f"• {html.escape(uname)}\n"
        kb_rows.append([InlineKeyboardButton(
            uname, callback_data=f"crm:user:{u['user_id']}",
        )])

    if not users:
        text += "<i>No users carry this tag.</i>"

    kb_rows.append([InlineKeyboardButton("🔙 Tags", callback_data="crm:tags")])
    await _safe_edit(query, text, InlineKeyboardMarkup(kb_rows))


# ─── Priority ────────────────────────────────────────────────────────────────

_PRIORITIES = [
    ("low",      "🟢 Low"),
    ("medium",   "🟡 Medium"),
    ("high",     "🟠 High"),
    ("critical", "🔴 Critical"),
]

async def crm_prio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    profile = get_or_create_profile(db_uid)
    cur = profile.get("priority", "low")
    text = f"🎯 <b>Set Priority</b>  (DB#{db_uid})\nCurrent: {_prio_emoji(cur)} {cur.title()}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅ ' if p == cur else ''}{label}",
            callback_data=f"crm:prio:set:{db_uid}:{p}",
        ) for p, label in _PRIORITIES],
        *_back_user(db_uid),
    ])
    await _safe_edit(query, text, kb)


async def crm_prio_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        db_uid = int(parts[3])
        level  = parts[4]
    except (IndexError, ValueError):
        await query.answer(); return
    set_priority(db_uid, level, update.effective_user.id)
    await query.answer(f"✅ Priority set to {level}.", show_alert=False)
    await crm_user(with_data(update, f"crm:user:{db_uid}"), context)


# ─── Status ──────────────────────────────────────────────────────────────────

_CRM_STATUSES = [
    ("new_customer", "New Customer"),
    ("returning",    "Returning Customer"),
    ("vip",          "⭐ VIP Customer"),
    ("reseller",     "🤝 Reseller"),
    ("wholesale",    "🏭 Wholesale"),
    ("blocked",      "🚫 Blocked"),
    ("suspended",    "⏸ Suspended"),
    ("verified",     "✅ Verified"),
    ("custom",       "✏️ Custom"),
]

async def crm_stat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    profile = get_or_create_profile(db_uid)
    cur  = profile.get("crm_status", "new_customer")
    text = f"🔖 <b>Set CRM Status</b>  (DB#{db_uid})\nCurrent: <b>{_status_label(cur)}</b>"

    rows = []
    for val, label in _CRM_STATUSES:
        marker = "✅ " if val == cur else ""
        rows.append([InlineKeyboardButton(
            f"{marker}{label}",
            callback_data=f"crm:stat:set:{db_uid}:{val}",
        )])
    rows.extend(_back_user(db_uid))
    await _safe_edit(query, text, InlineKeyboardMarkup(rows))


async def crm_stat_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        db_uid = int(parts[3])
        value  = parts[4]
    except (IndexError, ValueError):
        return
    set_crm_status(db_uid, value, update.effective_user.id)
    await query.answer(f"✅ Status set.", show_alert=False)
    await crm_user(with_data(update, f"crm:user:{db_uid}"), context)


# ─── Reminders ───────────────────────────────────────────────────────────────

async def crm_rem_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """crm:rem:add:<db_uid> — ConvHandler entry."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return ConversationHandler.END

    context.user_data["crm_reminder_uid"] = db_uid
    await query.message.reply_text(
        "📅 <b>Add Follow-up Reminder</b>\n\n"
        "Send message in this format:\n"
        "<code>Reason text\nYYYY-MM-DD HH:MM</code>\n\n"
        "Shortcuts:\n"
        "  <code>tomorrow</code> — next day at 09:00 UTC\n"
        "  <code>+N days</code>  — N days from now\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return CRM_ADD_REMINDER


async def crm_rem_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _check(update.effective_user.id):
        return ConversationHandler.END

    db_uid = context.user_data.get("crm_reminder_uid")
    if not db_uid:
        return ConversationHandler.END

    raw   = (update.message.text or "").strip()
    lines = raw.split("\n", 1)

    if len(lines) < 2:
        await update.message.reply_text(
            "⚠️ Please send both reason and date on separate lines.\n"
            "Example:\n<code>Contact about refund\n2026-09-01 14:00</code>",
            parse_mode="HTML",
        )
        return CRM_ADD_REMINDER   # stay in state, let admin try again

    reason   = lines[0].strip()
    date_raw = lines[1].strip()
    remind_at = _parse_reminder_dt(date_raw)

    if not remind_at:
        await update.message.reply_text(
            "⚠️ Could not parse the date. Use <code>YYYY-MM-DD HH:MM</code>, "
            "<code>tomorrow</code>, or <code>+N days</code>.",
            parse_mode="HTML",
        )
        return CRM_ADD_REMINDER

    rem_id = add_reminder(db_uid, update.effective_user.id, reason, remind_at)
    if rem_id:
        await update.message.reply_text(
            f"✅ Reminder set for <code>{_fmt(remind_at)}</code> UTC  (ID #{rem_id}).",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⚠️ Could not save reminder.")

    context.user_data.pop("crm_reminder_uid", None)
    return ConversationHandler.END


async def crm_rem_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        rem_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    complete_reminder(rem_id, update.effective_user.id)
    await query.answer("✅ Reminder marked complete.", show_alert=False)
    await crm_user(with_data(update, f"crm:user:{db_uid}"), context)


async def crm_rem_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return
    parts = query.data.split(":")
    try:
        rem_id, db_uid = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await query.answer(); return
    delete_reminder(rem_id, update.effective_user.id)
    await query.answer("🗑 Reminder deleted.", show_alert=False)
    await crm_user(with_data(update, f"crm:user:{db_uid}"), context)


# ─── Timeline ─────────────────────────────────────────────────────────────────

_TL_ICONS = {
    "login": "🔑", "logout": "🚪", "purchase": "🛒", "deposit": "💰",
    "refund": "↩️", "withdrawal": "💸", "coupon": "🎟", "referral": "👥",
    "profile_changed": "✏️", "note": "📝", "reminder_done": "✅",
    "activity": "📋",
}

async def crm_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    events = get_customer_timeline(db_uid, limit=25)
    text   = f"📜 <b>Customer Timeline</b>  (DB#{db_uid})\n{'━' * 24}\n\n"

    for e in events:
        icon   = _TL_ICONS.get(e.get("action", ""), _TL_ICONS.get(e.get("type", ""), "📋"))
        when   = _fmt(e.get("when"))
        action = html.escape(str(e.get("action") or ""))
        detail = html.escape(str(e.get("detail") or ""))
        text += f"{icon} <code>{when}</code>  {action}\n"
        if detail:
            text += f"    <i>{detail}</i>\n"

    if not events:
        text += "<i>No events recorded yet.</i>"

    await _safe_edit(query, text, InlineKeyboardMarkup(_back_user(db_uid)))


# ─── Export ──────────────────────────────────────────────────────────────────

async def crm_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crm:export:<db_uid> — send notes export as a text message."""
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True); return

    try:
        db_uid = int(query.data.split(":")[-1])
    except (IndexError, ValueError):
        return

    from database import get_db_session
    from database.models import User as DbUser
    username = None
    try:
        with get_db_session() as s:
            u = s.query(DbUser).filter_by(id=db_uid).first()
            if u:
                username = u.username
    except Exception:
        pass

    export_text = export_customer_notes_text(db_uid, username)

    # Send as a separate message (may be longer than edit limit)
    try:
        chunks = [export_text[i:i+4000] for i in range(0, len(export_text), 4000)]
        for chunk in chunks:
            await query.message.reply_text(f"<pre>{html.escape(chunk)}</pre>",
                                            parse_mode="HTML")
    except Exception:
        await query.message.reply_text("⚠️ Export failed.")


# ─── Search notes — ConversationHandler ──────────────────────────────────────

async def crm_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _check(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.message.reply_text(
        "🔍 <b>Search Notes</b>\n\nSend a keyword to search across note content, "
        "usernames, and admin names. Send /cancel to abort.",
        parse_mode="HTML",
    )
    return CRM_SEARCH


async def crm_search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _check(update.effective_user.id):
        return ConversationHandler.END

    q      = (update.message.text or "").strip()
    results = search_notes(q, limit=20)
    text   = f"🔍 <b>Search: \"{html.escape(q)}\"</b>  ({len(results)} results)\n{'━' * 24}\n\n"

    kb_rows: list[list] = []
    for r in results:
        uname = _uname(r.get("telegram_id"), r.get("username"))
        when  = _fmt(r.get("created_at"))
        snip  = html.escape((r.get("content") or "")[:60])
        text += f"📝 {html.escape(uname)}  [{when}]\n<i>{snip}</i>\n\n"
        kb_rows.append([InlineKeyboardButton(
            f"→ {uname}", callback_data=f"crm:user:{r['user_id']}",
        )])

    if not results:
        text += "<i>No notes matched your search.</i>"

    kb_rows.extend(_back_home())
    try:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML",
        )
    except Exception:
        await update.message.reply_text(text, parse_mode="HTML")
    return ConversationHandler.END


# ─── Settings ─────────────────────────────────────────────────────────────────

_CRM_BOOL_KEYS = [
    ("crm_allow_multiple_notes",  "Allow Multiple Notes"),
    ("crm_allow_tags",            "Allow Tags"),
    ("crm_allow_priority",        "Allow Priority Levels"),
    ("crm_allow_reminders",       "Allow Follow-up Reminders"),
    ("crm_allow_internal_status", "Allow Internal Status"),
]
_CRM_INT_KEYS = [
    ("crm_max_notes", "Max Notes per User", [
        ("0", "Unlimited"), ("1", "1"), ("3", "3"),
        ("5", "5"), ("10", "10"), ("20", "20"),
    ]),
]


def _settings_text() -> str:
    st = _status()
    se = _semoji(st)
    lines = [f"⚙️ <b>Customer CRM Settings</b>\n\nStatus: {se} {st.title()}\n\n"
             "<b>Feature Toggles</b>\n"]
    for key, label in _CRM_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        em  = "🟢 ON" if val else "🔴 OFF"
        lines.append(f"  {label}: {em}\n")
    lines.append("\n<b>Limits</b>\n")
    for key, label, opts in _CRM_INT_KEYS:
        cur = str(cfg.get(key, "0") or "0")
        cur_label = next((lbl for v, lbl in opts if v == cur), cur)
        lines.append(f"  {label}: <b>{cur_label}</b>\n")
    return "".join(lines)


def _settings_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🟢 Enable",      callback_data="crm:setstatus:enabled"),
         InlineKeyboardButton("🟡 Maintenance", callback_data="crm:setstatus:maintenance"),
         InlineKeyboardButton("🔴 Disable",     callback_data="crm:setstatus:disabled")],
    ]
    for key, label in _CRM_BOOL_KEYS:
        val = cfg.get_bool(key, True)
        em  = "🟢 ON" if val else "🔴 OFF"
        rows.append([InlineKeyboardButton(
            f"Toggle {label} [{em}]", callback_data=f"crm:toggle:{key}",
        )])
    for key, label, opts in _CRM_INT_KEYS:
        cur = str(cfg.get(key, "0") or "0")
        sub = [InlineKeyboardButton(
            f"{'✅' if v == cur else ''}{lbl}",
            callback_data=f"crm:setval:{key}:{v}",
        ) for v, lbl in opts]
        for i in range(0, len(sub), 3):
            rows.append(sub[i:i + 3])
    rows.extend(_back_home())
    return InlineKeyboardMarkup(rows)


async def crm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not has_permission(update.effective_user.id, "manage_settings"):
        await query.answer("⛔ Permission denied.", show_alert=True); return
    await _safe_edit(query, _settings_text(), _settings_kb())


# ─── Cancel helper (shared fallback) ─────────────────────────────────────────

async def _crm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("crm_note_uid", None)
    context.user_data.pop("crm_edit_note_id", None)
    context.user_data.pop("crm_edit_note_uid", None)
    context.user_data.pop("crm_reminder_uid", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── Dispatcher ──────────────────────────────────────────────────────────────

async def crm_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all crm:* callbacks that are not handled by ConversationHandlers."""
    query = update.callback_query
    data  = query.data or ""
    parts = data.split(":")

    action = parts[1] if len(parts) >= 2 else ""
    sub    = parts[2] if len(parts) >= 3 else ""

    if action == "home":
        await crm_home(update, context)
    elif action == "list":
        await crm_list(update, context)
    elif action == "user":
        await crm_user(update, context)
    elif action == "notes":
        if sub == "arch":
            await crm_notes_archived(update, context)
        elif sub == "all":
            await crm_notes_all(update, context)
        else:
            await crm_notes_all(update, context)
    elif action == "note":
        if sub == "pin":
            await crm_note_pin(update, context)
        elif sub == "arch":
            await crm_note_arch(update, context)
        elif sub == "del":
            await crm_note_del_confirm(update, context)
        elif sub == "delok":
            await crm_note_del_execute(update, context)
        else:
            await query.answer()
    elif action == "tags":
        await crm_tags(update, context)
    elif action == "tag":
        if sub == "del":
            await crm_tag_del(update, context)
        elif sub == "u":
            await crm_tag_user(update, context)
        elif sub == "a":
            await crm_tag_assign(update, context)
        elif sub == "r":
            await crm_tag_remove(update, context)
        else:
            await query.answer()
    elif action == "sbytag":
        await crm_search_by_tag(update, context)
    elif action == "prio":
        if sub == "set":
            await crm_prio_set(update, context)
        else:
            await crm_prio(update, context)
    elif action == "stat":
        if sub == "set":
            await crm_stat_set(update, context)
        else:
            await crm_stat(update, context)
    elif action == "rem":
        if sub == "done":
            await crm_rem_done(update, context)
        elif sub == "del":
            await crm_rem_del(update, context)
        else:
            await query.answer()
    elif action == "timeline":
        await crm_timeline(update, context)
    elif action == "export":
        await crm_export(update, context)
    elif action == "settings":
        await crm_settings(update, context)
    elif action == "setstatus":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True); return
        await query.answer()
        val = sub
        if val in ("enabled", "maintenance", "disabled"):
            cfg.set("crm_status", val)
        await _safe_edit(query, _settings_text(), _settings_kb())
    elif action == "toggle":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True); return
        await query.answer()
        key = sub
        valid = {k for k, _ in _CRM_BOOL_KEYS}
        if key in valid:
            cfg.set(key, not cfg.get_bool(key, True))
        await _safe_edit(query, _settings_text(), _settings_kb())
    elif action == "setval":
        if not has_permission(update.effective_user.id, "manage_settings"):
            await query.answer("⛔ Permission denied.", show_alert=True); return
        await query.answer()
        key = sub
        val = parts[3] if len(parts) >= 4 else "0"
        valid = {k for k, _, _ in _CRM_INT_KEYS}
        if key in valid:
            try:
                cfg.set(key, int(val))
            except ValueError:
                pass
        await _safe_edit(query, _settings_text(), _settings_kb())
    elif action == "noop":
        await query.answer()
    else:
        await query.answer()


# ─── Registration ─────────────────────────────────────────────────────────────

def build_crm_convs() -> list:
    """Build all ConversationHandlers for the CRM system."""
    _cancel_fallbacks = [CommandHandler("cancel", _crm_cancel)]

    add_note_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(crm_note_add_start, pattern=r"^crm:note:add:\d+$")],
        states={CRM_ADD_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, crm_note_add_receive)]},
        fallbacks=_cancel_fallbacks,
        per_user=True, per_chat=True, allow_reentry=True,
    )
    edit_note_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(crm_note_edit_start, pattern=r"^crm:note:edit:\d+:\d+$")],
        states={CRM_EDIT_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, crm_note_edit_receive)]},
        fallbacks=_cancel_fallbacks,
        per_user=True, per_chat=True, allow_reentry=True,
    )
    add_tag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(crm_tag_add_start, pattern=r"^crm:tag:add$")],
        states={CRM_ADD_TAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, crm_tag_add_receive)]},
        fallbacks=_cancel_fallbacks,
        per_user=True, per_chat=True, allow_reentry=True,
    )
    add_rem_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(crm_rem_add_start, pattern=r"^crm:rem:add:\d+$")],
        states={CRM_ADD_REMINDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, crm_rem_add_receive)]},
        fallbacks=_cancel_fallbacks,
        per_user=True, per_chat=True, allow_reentry=True,
    )
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(crm_search_start, pattern=r"^crm:search$")],
        states={CRM_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, crm_search_receive)]},
        fallbacks=_cancel_fallbacks,
        per_user=True, per_chat=True, allow_reentry=True,
    )
    return [add_note_conv, edit_note_conv, add_tag_conv, add_rem_conv, search_conv]


def register_handlers(application) -> None:
    # ConversationHandlers must be added BEFORE the catch-all dispatcher
    for conv in build_crm_convs():
        application.add_handler(conv)
    # Catch-all for all other crm:* callbacks
    application.add_handler(
        CallbackQueryHandler(crm_dispatch, pattern=r"^crm:")
    )
