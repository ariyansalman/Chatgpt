"""Admin authentication (OTP-based 2FA) and admin-roster management.

/admin_login   -> bot DMs the admin a 6-digit code, they type it back
/admin_logout  -> ends the current verified session early
/admin_list    -> (any admin) view the roster
/admin_add     -> (super_admin, manage_admins) add or re-invite an admin
/admin_role    -> (super_admin, manage_admins) change someone's tier
/admin_remove  -> (super_admin, manage_admins) deactivate an admin
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from database import AdminRoleType
from utils.permissions import (
    is_admin, has_permission, is_super_admin, generate_and_store_otp,
    verify_otp, otp_cooldown_remaining, invalidate_session,
    has_valid_session, list_admins, upsert_admin, deactivate_admin,
    require_role, OTP_TTL_MINUTES, SESSION_TTL_HOURS,
)
from utils.audit import log_admin_action
from utils.safe_conversation import safe_conversation

logger = logging.getLogger(__name__)

WAITING_FOR_OTP = 100

_ROLE_ALIASES = {
    "super_admin": AdminRoleType.SUPER_ADMIN, "super": AdminRoleType.SUPER_ADMIN,
    "owner": AdminRoleType.SUPER_ADMIN,
    "moderator": AdminRoleType.MODERATOR, "mod": AdminRoleType.MODERATOR,
    "support_staff": AdminRoleType.SUPPORT_STAFF, "support": AdminRoleType.SUPPORT_STAFF,
    "staff": AdminRoleType.SUPPORT_STAFF,
}


def _parse_role(text: str):
    return _ROLE_ALIASES.get(text.strip().lower())


# ─────────────────────────────── /admin_login ───────────────────────────────

async def admin_login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ You don't have permission to access this command.")
        return ConversationHandler.END

    if has_valid_session(user_id):
        await update.message.reply_text(
            f"✅ You're already verified (session active for up to {SESSION_TTL_HOURS}h).\n"
            "Use /admin_logout if you want to force a fresh code."
        )
        return ConversationHandler.END

    cooldown = otp_cooldown_remaining(user_id)
    if cooldown > 0:
        await update.message.reply_text(f"⏳ Please wait {cooldown}s before requesting another code.")
        return ConversationHandler.END

    try:
        code = generate_and_store_otp(user_id)
    except PermissionError:
        await update.message.reply_text("⛔ You don't have permission to access this command.")
        return ConversationHandler.END

    # Sent by the bot itself, in the admin's own chat — no SMS/e-mail involved.
    await update.message.reply_text(
        f"🔐 Your admin verification code is:\n\n`{code}`\n\n"
        f"It expires in {OTP_TTL_MINUTES} minutes. Reply with the 6 digits to verify.\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_OTP


@safe_conversation()
async def admin_login_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    submitted = (update.message.text or "").strip()

    if not submitted.isdigit() or len(submitted) != 6:
        await update.message.reply_text("Please send the 6-digit code, or /cancel.")
        return WAITING_FOR_OTP

    ok, message = verify_otp(user_id, submitted)
    await update.message.reply_text(message)
    if ok:
        log_admin_action(user_id, "admin.login_2fa_success")
        return ConversationHandler.END
    return WAITING_FOR_OTP


async def admin_login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Login cancelled.")
    return ConversationHandler.END


def build_admin_login_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("admin_login", admin_login_start)],
        states={
            WAITING_FOR_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_login_verify),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_login_cancel)],
    )


async def admin_logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ You don't have permission to access this command.")
        return
    invalidate_session(user_id)
    log_admin_action(user_id, "admin.logout")
    await update.message.reply_text("👋 Signed out of the admin panel. Send /admin_login to verify again.")


# ─────────────────────────────── Roster management ───────────────────────────────

async def admin_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ You don't have permission to access this command.")
        return

    admins = list_admins(include_inactive=is_super_admin(user_id))
    if not admins:
        await update.message.reply_text("No admins registered yet (besides the bootstrap owner).")
        return

    lines = ["👥 *Admin roster*\n"]
    role_icon = {"super_admin": "👑", "moderator": "🛡", "support_staff": "🎧"}
    for a in admins:
        icon = role_icon.get(a["role"], "•")
        status = "" if a["is_active"] else " (inactive)"
        uname = f"@{a['username']}" if a["username"] else str(a["telegram_id"])
        lines.append(f"{icon} `{a['telegram_id']}` {uname} — *{a['role']}*{status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_role(AdminRoleType.SUPER_ADMIN)
async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_add <telegram_id> <role>"""
    user_id = update.effective_user.id
    if not has_permission(user_id, "manage_admins"):
        await update.message.reply_text("⛔ You don't have permission to manage admins.")
        return

    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage: `/admin_add <telegram_id> <super_admin|moderator|support_staff>`",
            parse_mode="Markdown",
        )
        return

    target_id = int(args[0])
    role = _parse_role(args[1])
    if not role:
        await update.message.reply_text("Unknown role. Use: super_admin, moderator, or support_staff.")
        return

    upsert_admin(target_id, role, added_by=user_id)
    log_admin_action(user_id, "admin.add", target_type="admin", target_id=target_id,
                      details=f"role={role.value}")
    await update.message.reply_text(f"✅ `{target_id}` is now a *{role.value}*.", parse_mode="Markdown")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(f"🎉 You've been added as a *{role.value}* admin.\n"
                  "Send /admin_login to verify your identity and access the admin panel."),
            parse_mode="Markdown",
        )
    except Exception:  # noqa: BLE001
        logger.info("could not DM new admin %s (they may not have started the bot)", target_id)


@require_role(AdminRoleType.SUPER_ADMIN)
async def admin_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_role <telegram_id> <role> — alias of /admin_add for changing an existing admin's tier."""
    await admin_add_command(update, context)


@require_role(AdminRoleType.SUPER_ADMIN)
async def admin_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_remove <telegram_id>"""
    user_id = update.effective_user.id
    if not has_permission(user_id, "manage_admins"):
        await update.message.reply_text("⛔ You don't have permission to manage admins.")
        return

    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: `/admin_remove <telegram_id>`", parse_mode="Markdown")
        return

    target_id = int(args[0])
    if not deactivate_admin(target_id):
        await update.message.reply_text(
            "Couldn't remove that admin (they may be the bootstrap owner, or don't exist)."
        )
        return
    log_admin_action(user_id, "admin.remove", target_type="admin", target_id=target_id)
    await update.message.reply_text(f"🗑 `{target_id}` has been removed from the admin roster.", parse_mode="Markdown")
