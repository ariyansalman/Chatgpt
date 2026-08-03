# Database performance fix

This package addresses the common slowdown that appears after switching from
local SQLite to PostgreSQL/Supabase.

## What changed

- PostgreSQL connection and statement timeouts are bounded.
- The SQLAlchemy pool waits for a limited time instead of blocking for 30
  seconds by default.
- Global activity tracking runs in a worker thread and is throttled to once per
  user per minute.
- Anti-spam moderation reads and violation writes use `run_db()` so they do not
  block the Telegram event loop.
- User language reads are cached for five minutes.
- Bot configuration refreshes use stale-while-revalidate after startup.
- The configuration cache is warmed before Telegram polling starts.

## Important deployment note

This fixes the highest-impact global paths. Feature handlers that perform their
own synchronous database work should also wrap their service call with
`await run_db(...)` as they are touched. Do not pass SQLAlchemy ORM objects
between the worker thread and the async handler; return plain dictionaries,
lists, or scalar values instead.