# Callback, Navigation & Message-Editing Reliability Fix — 2026-07-29

## Scope

This pass touched **only** callback reliability, navigation stability, and
Telegram message-edit handling. No business logic, payment logic, or
database schema was modified. No file was deleted or rewritten wholesale —
fixes were injected centrally so they apply project-wide without editing
each of the ~570 callback registrations or ~670 `edit_message_text` call
sites individually.

## What was already in place

The project already had solid building blocks for this: `utils/safe_edit.py`,
`utils/callback_safety.py` (`guarded_callback` / `safe_answer`),
`utils/error_handler.py` (global error handler with benign-stale-callback
filtering), and `utils/nav_state.py` (per-user back-stack tracking). An
audit of `bot.py`'s ~570 `CallbackQueryHandler` registrations found **no
genuine top-level pattern conflicts** — the repeated patterns (e.g.
`^cancel_product$` appearing 10 times) are intentional, scoped to different
`ConversationHandler`s' own `fallbacks`, not competing global routes.

## Gaps found and fixed

1. **Most `edit_message_text` calls only handled "message is not modified".**
   669 call sites across the handlers, and only 28 used the shared
   `safe_edit_message_text` helper. Every other case Telegram can raise on
   edit — message deleted, message can't be edited, chat gone, stale
   query — bubbled up as an unhandled exception, leaving the screen stuck.

   **Fix:** `utils/safe_edit.py` now recognizes every known unrecoverable
   edit failure and automatically sends a brand-new message with the same
   text/keyboard instead of raising. `utils/global_callback_reliability.py`
   installs the equivalent behavior directly on
   `telegram.CallbackQuery.edit_message_text` at the class level (the same
   monkeypatch pattern already used by `utils/global_button_colors.py` for
   button styling), so **every** existing call site — old or new, wrapped
   or not — gets this fallback for free, with zero per-file edits.

2. **No guarantee every tap was answered immediately.** Handlers answered
   the callback query themselves, at whatever point in their own code they
   got to it (or not at all, if they raised first).

   **Fix:** a new `CallbackQueryHandler` registered in handler group `-3`
   (`global_callback_reliability.register_immediate_ack`, wired in
   `bot.py`) answers every callback query the instant it arrives, before
   any other handler group runs — including the existing maintenance-mode
   gate. `CallbackQuery.answer()` itself is also patched to never raise
   (benign "already answered" / "query too old" errors are logged quietly).

3. **No project-wide duplicate-tap guard.** `guarded_callback` already
   solved this per-handler, but only ~46 of the ~570 registrations used it.

   **Fix:** the same group `-3` handler drops an identical repeat tap
   (same chat + same `callback_data`) that arrives within 0.8s of the
   first, via `ApplicationHandlerStop` — the tap is still answered
   (spinner clears) but no handler runs a second time for it.

4. **No safety net for stale/unroutable `callback_data`.** If a button's
   `callback_data` didn't match any `ConversationHandler` state/fallback or
   any standalone `CallbackQueryHandler` (renamed callback data, a button
   left over from before a bot restart, a state a conversation didn't
   anticipate), Telegram received no answer at all and the button stayed
   in its loading state forever — exactly the "nothing happens" symptom
   described in the report.

   **Fix:** `global_callback_reliability.register_catchall` adds one final,
   pattern-less `CallbackQueryHandler` as the **last** handler in the
   default group. PTB only runs the first matching handler per group, so
   this only ever fires when nothing more specific claimed the update. It
   answers the tap, resets the user's nav-stack, and redraws the main
   menu — so an expired/invalid button can never leave the user stuck.
   **This registration must stay last** — see the comment at its call
   site in `bot.py` and in `global_callback_reliability.py`.

## Files touched

- `utils/safe_edit.py` — expanded fallback coverage (edited, not rewritten).
- `utils/global_callback_reliability.py` — **new**, all three dispatcher-
  level fixes above.
- `bot.py` — three lines added: one import, one early registration call,
  one final registration call. No existing line was removed or reordered.

## Explicitly not touched

Business logic, payment/gateway logic, database models and queries,
existing `callback_data` strings, keyboard layouts, i18n strings, and every
individual handler file. `guarded_callback`, `safe_answer`, and
`nav_state` continue to work exactly as before — these fixes are additive
safety nets underneath them, not replacements.
