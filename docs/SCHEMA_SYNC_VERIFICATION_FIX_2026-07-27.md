# Production fix: `transactions.verification_in_progress` schema drift

## What broke

```
psycopg2.errors.UndefinedColumn: column transactions.verification_in_progress does not exist
```

`database/models.py`'s `Transaction` model has three columns —
`verification_in_progress`, `verification_locked_at`, `auto_verify_attempts`
(the auto-verification retry lock backing
`services/payment_workflow.run_auto_verification_with_retries`) — that the
live PostgreSQL database never received.

## Root cause

Two separate mechanisms are supposed to keep the live schema in sync with
`database/models.py`, and both missed this change:

1. **Alembic** — a correct, idempotent migration
   (`alembic/versions/20260921_auto_verify_retry_lock.py`) already exists
   for these exact columns and is the current head. It was simply never run
   against the production database (`alembic upgrade head`).
2. **`bot.py`'s `_apply_pending_migrations()`** — a hand-maintained list of
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements that runs on every
   bot startup as a safety net for exactly this situation. It was updated
   for the *previous* migration (`expiry_notified` / `review_notified`) but
   never updated for this one, so the safety net had a hole in it.

Neither gap was fatal by itself — but together they meant the bot booted
successfully and only crashed the first time a query touched the missing
columns, instead of failing fast at startup with a clear cause.

There was also a **latent bug that would have undone a real fix**: the
`alembic_version` "cleanup" step at the end of `_apply_pending_migrations()`
deleted any row `NOT IN` a hardcoded snapshot of revision ids that stopped
at `20260920_paynotify` — one revision *before* the head that adds these
columns. If Alembic were ever run and correctly stamped
`20260921_autoverifylock`, this step would have immediately deleted that
stamp again.

## What was fixed

1. **`bot.py` / `_apply_pending_migrations()`** — added the three missing
   `ADD COLUMN IF NOT EXISTS` statements for `verification_in_progress`,
   `verification_locked_at`, `auto_verify_attempts`, matching the existing
   pattern used for `expiry_notified` / `review_notified`. This is the
   direct, minimal fix for the crash.

2. **`bot.py` / `alembic_version` cleanup** — replaced the hardcoded,
   already-stale revision whitelist with a dynamic lookup of the real
   revision ids from `alembic/versions/` (via
   `alembic.script.ScriptDirectory`), so it only ever removes truly
   orphaned rows and can't delete a legitimate current head again just
   because a snapshot list wasn't updated.

3. **`database/schema_check.py` (new)** — a generic, permanent guard
   against this entire class of bug:
   - `compute_schema_diff(engine)` walks **every** table/column declared in
     `database/models.py` (not a hand-maintained list) and reports anything
     missing from the live database.
   - `ensure_schema_synced(engine)` auto-heals drift it finds: first via
     `alembic upgrade head` (keeps `alembic_version` accurate), then via
     safe `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` for
     anything Alembic didn't end up covering. Never drops a column, never
     recreates an existing table, never touches existing data.
   - If drift remains after both attempts, it raises
     `SchemaOutOfSyncError` with a complete, actionable report instead of
     letting the app continue toward a crash.
   - Also runnable standalone: `python -m database.schema_check` (report
     only) or `python -m database.schema_check --fix` (report + heal).

4. **`bot.py` / `main()`** — wired `ensure_schema_synced(engine)` into
   startup, right after the existing auto-migration step and before
   `initialize_database()`. If the schema still can't be reconciled, the
   bot now prints a clear `DATABASE SCHEMA OUT OF SYNC` message listing the
   exact missing tables/columns and refuses to start, instead of booting
   and crashing on the first affected query.

## What was intentionally left alone

- No business logic in `services/payment_workflow.py` or any handler was
  touched.
- No column was dropped, no table was recreated, no data was modified.
- The existing SQLite-only dev auto-fix in `database/db.py`
  (`_autofix_missing_columns`) is untouched — it's a separate, narrower
  mechanism for local dev and doesn't apply to PostgreSQL by default.
