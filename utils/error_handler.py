"""Global error handler for python-telegram-bot with optional Sentry."""

import html
import logging
import os
import traceback

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import settings as app_settings

logger = logging.getLogger(__name__)

# Telegram invalidates a callback_query id once it's "too old" (roughly
# ~15-30s after the button tap, e.g. because of bot restarts, webhook
# backlog, or a slow prior handler) or if it was already answered. Every
# callback handler in this project calls ``query.answer()``, so this is an
# expected, unpreventable race condition rather than an application bug —
# there's nothing to "fix" in the update, retrying won't help, and it
# should not page the admin or confuse the user with a fake "something
# went wrong" message.
_BENIGN_CALLBACK_QUERY_ERRORS = (
    "query is too old",
    "query id is invalid",
    "query id invalid",
)


def _is_benign_stale_callback_query(err: BaseException | None) -> bool:
    if not isinstance(err, BadRequest):
        return False
    msg = str(err).lower()
    return any(needle in msg for needle in _BENIGN_CALLBACK_QUERY_ERRORS)

# Optional Sentry integration — only initialized if SENTRY_DSN is set.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
_sentry_enabled = False

if SENTRY_DSN:
    try:
        import sentry_sdk  # type: ignore

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            send_default_pii=False,
        )
        _sentry_enabled = True
        logger.info("Sentry initialized")
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to initialize Sentry: %s", e)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error, notify the admin, and never let the user see a raw crash."""
    err = context.error

    if _is_benign_stale_callback_query(err):
        # Expected Telegram behavior (stale/expired/already-answered
        # callback query) — log quietly and skip the user/admin
        # notifications entirely; there is nothing actionable here.
        logger.debug("Ignoring stale/expired callback query: %s", err)
        return

    logger.error("Unhandled exception while processing update", exc_info=err)

    # Send to Sentry if configured
    if _sentry_enabled:
        try:
            import sentry_sdk  # type: ignore
            sentry_sdk.capture_exception(err)
        except Exception:  # noqa: BLE001
            pass

    # Try to inform the user gently (never leak the traceback)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ Oops! Something went wrong on our side.\n"
                    "Our team has been notified. Please try again in a moment."
                ),
            )
    except Exception:  # noqa: BLE001
        pass

    # Notify admin with a redacted traceback
    try:
        admin_id = app_settings.ADMIN_TELEGRAM_ID
        if not admin_id:
            return
        tb_lines = traceback.format_exception(type(err), err, err.__traceback__ if err else None)
        # Telegram hard limit is 4096 chars; keep well under it after HTML escape.
        update_repr = ""
        if isinstance(update, Update):
            try:
                update_repr = html.escape(str(update.to_dict())[:400])
            except Exception:  # noqa: BLE001
                update_repr = "<unavailable>"
        err_type_name = type(err).__name__ if err else "Unknown"
        is_db_error = "sqlalchemy" in type(err).__module__.lower() if err else False
        from utils.notify_format import render as _render_notif, utc_now_str as _ts
        header = _render_notif(
            "🗄" if is_db_error else "🚨",
            "Database Error" if is_db_error else "Critical Error",
            [
                ("Type", f"<code>{html.escape(err_type_name)}</code>"),
                ("Message", f"<code>{html.escape(str(err)[:300])}</code>"),
                ("Update", f"<code>{update_repr}</code>" if update_repr else None),
            ],
            _ts(),
        )
        try:
            await context.bot.send_message(chat_id=admin_id, text=header, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            # Fallback: strip HTML on parse/length errors
            await context.bot.send_message(chat_id=admin_id, text=header[:3500])

        # Send traceback in chunks as plain text (no HTML) to avoid entity issues.
        tb_text = "".join(tb_lines)[-6000:]
        chunk_size = 3500
        for i in range(0, len(tb_text), chunk_size):
            piece = tb_text[i:i + chunk_size]
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"Traceback:\n{piece}")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.exception("Failed to notify admin of error")
