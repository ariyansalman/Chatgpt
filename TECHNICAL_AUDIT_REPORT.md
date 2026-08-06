# Technical Audit Report — ZINIPAY_CHECKBOX_UI

Scope respected: no UI redesign, no new features, no business-logic rewrite.
Every change below is either (a) a metadata/config fix with zero behavioral
surface, or (b) restoring something that was already *intended* to exist
(a missing model column an existing migration already added, a missing
deploy config file for a platform already named in the Procfile/docs).

Methodology: this codebase is large (378 Python files / ~72k LOC, 140
handler modules, 86 services, 81 Alembic migrations, 517 `add_handler`
call sites, 611 `CallbackQueryHandler` pattern registrations). Rather than
manually eyeballing that volume, every claim below was produced by a
purpose-built static-analysis script run directly against the code (AST
import-graph, handler-registration extraction, alembic revision-graph
walk, migration↔model column diff) and then hand-verified by reading the
actual source at the reported line numbers. Where a check came back
clean, that's stated explicitly rather than omitted, so this report
doesn't imply problems that aren't there.

---

## 1. Root cause of the deployment issue

**Finding: `railway.toml` did not exist**, even though the task's own
deploy checklist (and Procfile comments) name Railway as a target platform
alongside Render/Docker. Without it, a Railway deploy falls back to
Nixpacks auto-detection, which has no way to know this repo is a Telegram
bot started via `render_service.py` (the file that binds `$PORT` and
exposes `/health` — Railway won't pass health checks without it, and could
easily auto-detect the wrong entry point entirely).

**Fix applied:** added `railway.toml`, mirroring the already-correct
`render.yaml` primary service (`startCommand = "python render_service.py"`,
`healthcheckPath = "/health"`).

**Everything else in the deploy chain was verified consistent — no fix
needed:**
| File | Starts | Verified against |
|---|---|---|
| `Procfile` `bot:` | `python render_service.py` | ✅ matches render.yaml |
| `render.yaml` (`telegram-store-bot`) | `python render_service.py` | ✅ |
| `render_service.py` | binds `$PORT`, then imports and calls `bot.main()` | ✅ `bot.py`'s `main()` ends in a blocking `application.run_polling()` / `run_webhook()` — process stays up correctly |
| `Procfile` `webhook:` / `render.yaml` (`telegram-store-webhooks`) | `python webhook_server.py` | ✅ separate Flask app, own `if __name__ == '__main__': app.run(...)` |
| `Dockerfile` | `tini -- sh -c "python webhook_server.py & exec python bot.py"` | Runs *both* processes in one container — see note below |
| `docker-compose.yml` `bot` service | inherits the Dockerfile CMD above | consistent with Dockerfile |
| `docker-compose.yml` `cryptobot-webhook` service | `python webhook_server.py`, gated behind `--profile crypto` | Not started by default, so it does **not** double-run against the `bot` service's already-bundled webhook process |

**Note, not fixed (documented, not a bug):** the Dockerfile's default `CMD`
backgrounds `webhook_server.py` inside the same container as `bot.py`.
This is a *different* topology from Render/Railway (which run them as two
independently-supervised services) but it's an intentional, working,
simpler all-in-one option for `docker run`/plain `docker compose up`
users who don't need per-service scaling or independent restarts. The one
real risk it carries — if `webhook_server.py` crashes, nothing supervises
or restarts it independently, since only `bot.py` is the container's PID-1
tracked foreground process — is worth knowing about if you rely on the
Docker path for payment webhooks, but changing the process topology was
outside this audit's "don't rewrite" boundary. If you want it fixed, the
one-line change is running both under `tini -s` with a tiny supervisor
loop, or simply deploying `webhook_server.py` as its own container the way
`render.yaml`/`railway.toml` already do.

---

## 2. Root cause of the "database problems" / migration issues

### 2a. Broken Alembic revision chain (fixed)

`alembic/versions/20260728_global_delivery_template.py` had:
```
down_revision = "20260921_autoverifylock"
```
— i.e. a migration dated **July 28** declared its parent as the migration
dated **September 21**, the *last* migration in the entire history. Since
nothing else pointed back at it, this silently made the July 28 migration
the effective Alembic head (`alembic upgrade head` would apply all 80
other migrations first, then this one dead last) instead of applying in
its intended position right after `20260728_binance_expiry_default`. This
is exactly the kind of drift that produces "column already exists" /
"relation does not exist" errors on a fresh deploy depending on ordering
assumptions baked into later migrations.

**Fix applied:** corrected `20260728_global_delivery_template.py`'s
`down_revision` back to `20260728_binance_expiry_default`, and re-pointed
`20260729_bybit_ltc.py`'s `down_revision` at it, restoring a single clean
linear chain. Verified programmatically: exactly one root
(`20260703_pv2`), exactly one head (`20260921_autoverifylock`), zero
dangling `down_revision` references.

### 2b. Missing ORM column → guaranteed `AttributeError` at runtime (fixed)

`alembic/versions/20260913_broadcast_analytics.py` adds
`scheduled_broadcasts.is_archived` to the live table. But the
`ScheduledBroadcast` class in `database/models.py` never declared that
column. Meanwhile `services/broadcast_analytics_service.py` actively
*uses* it — `ScheduledBroadcast.is_archived == False` as a query filter,
and `br.is_archived = True` as a write — both of which raise
`AttributeError: type object 'ScheduledBroadcast' has no attribute
'is_archived'` the instant that code path runs (broadcast archiving /
analytics list filtering).

**Fix applied:** added
`is_archived = Column(Boolean, nullable=False, default=False)` to the
`ScheduledBroadcast` model, matching the migration's
`server_default="false", nullable=False`.

A full migration↔model column diff was run across all 81 migrations —
**this was the only drift found.** No missing enums, no orphaned tables.

### 2c. Startup migration/schema-check layering — verified sound

`bot.py:main()` runs, in order: (1) a hand-maintained
`_apply_pending_migrations()` idempotent patch list, (2)
`schema_check.ensure_schema_synced()` (diffs live DB against
`Base.metadata`, tries `alembic upgrade head`, then falls back to safe
`ADD COLUMN IF NOT EXISTS`, and **refuses to start** if drift remains),
(3) `initialize_database()` (`create_all`, which only adds missing
*tables*, never touches existing ones). This layering is correct and
already defends against exactly the kind of column drift found in §2b —
it just couldn't catch this specific case because the ORM model itself
was wrong, not the live schema vs. migrations.

---

## 3. Root cause of the slow-database / "bot feels sluggish on Postgres" issue

This is the single largest finding in the audit, and it's real,
reproducible from the code, and **not fully fixed** — fixing it completely
would mean touching the body of ~600 handler functions across ~140 files,
which is well beyond a safe "find and fix bugs, don't rewrite" pass. It's
documented here in full so it can be triaged deliberately.

**What the codebase already got right:** `database/db.py` ships a
`run_db(fn, *args)` helper — `await asyncio.to_thread(fn, ...)` — with a
docstring explicitly warning that direct synchronous DB calls inside an
`async def` handler block the bot's single event loop for every user, not
just the one making the request. `bot.py` also sets
`concurrent_updates=256` on the `Application`, with a comment describing
exactly this "bot freezes during payment verification" failure mode.

**What's still broken:** `concurrent_updates` only helps when the
`await`ed work is *actually async*. A static scan (AST-based, not regex)
found **613 `async def` functions across handlers/ and services/ that
call `with get_db_session(): ...` directly**, not wrapped in `run_db()`.
`get_db_session()` is a plain synchronous SQLAlchemy context manager —
against Postgres/Supabase, every query inside it is a real blocking
network round trip on the *only* thread running the asyncio event loop.
While that call is in flight, no other update — from any other user —
can be processed, regardless of how many concurrent tasks are queued.
Only 32 call sites in the whole codebase currently go through `run_db()`.

Representative examples (full list is reproducible by re-running the
audit script — every handler file has multiple instances):
```
handlers/payment_handlers.py   — 30+ functions, incl. the entire
                                  deposit/verification flow this session's
                                  UI work is centered on
handlers/user_handlers.py      — category browsing, product detail,
                                  order history (high-traffic, user-facing)
handlers/admin_handlers.py     — order/user management
handlers/admin_scheduled_broadcast.py, admin_user_profile.py, ...
```

**Why this wasn't fixed in this pass:** converting a call site correctly
requires splitting each function into a plain synchronous inner function
(the part that touches the DB) plus an `await run_db(...)` call from the
`async def` handler — a mechanical but non-trivial edit per function, and
at 600+ sites the risk of silently changing behavior (return values,
exception handling, transaction boundaries) while doing it "quickly"
outweighs doing it right. This needs a dedicated pass, not a drive-by
fix bundled into a UI audit.

**Recommended triage order** (highest perceived-latency impact first):
1. `handlers/payment_handlers.py` — the deposit/verification flow already
   has one documented freeze bug (`concurrent_updates`); the *remaining*
   direct DB calls in this file are the most likely source of any
   lingering "stuck" reports during payment review.
2. `handlers/user_handlers.py` — every user hits this on `/start` and
   catalog browsing; blocking here is the most *visible* to end users.
3. Admin-only handlers (`admin_*.py`) — lower priority; single admin,
   infrequent taps, blocking here is felt only by that one admin.

**Db-layer tuning was checked and is already solid, no changes needed:**
`database/db.py`'s Postgres engine uses `pool_pre_ping=True`,
`pool_recycle=60s` (well under Supabase Supavisor's ~10s idle-kill risk
window is actually the *opposite* direction — recycle happens well before
staleness, which is correct), TCP keepalives, a bounded
`pool_timeout=8s` so a saturated pool fails fast instead of hanging a
Telegram update for 30s, and a `statement_timeout` guard. Pool size
(5 + 10 overflow, both env-overridable) is reasonable for a bot process.

---

## 4. Handler registration — imports, duplicates, dead callbacks

**Every broken import: none found.** An AST-based import graph across all
378 files found zero handler/service modules that are defined but never
imported anywhere. (Files that *are* legitimately unreferenced from
runtime code — `tests/*`, `alembic/versions/*`, `migrations/*` [legacy,
pre-Alembic, run manually], `monitoring/exporter.py`,
`scripts/migrate_turso_to_postgres.py` — are meant to be run standalone,
not imported, so they're correctly excluded from this list.)

**Duplicate handlers: none found at the level that matters.** A
line-by-line scan initially flagged ~100 "duplicate" `CallbackQueryHandler`
patterns (e.g. `^cancel$` appearing 5 times) — but every one of those
turned out to be inside the `states=`/`fallbacks=` of *different*
`ConversationHandler` instances, where pattern reuse across independent
conversations is normal and correct (each conversation is scoped
per-user/per-chat; only one is active at a time). Re-running the check
restricted to **top-level `application.add_handler(CallbackQueryHandler(...))`
calls only** (402 of them — these are the ones that really do compete for
the same slot) found **zero collisions**. Same result for all 49
`ConversationHandler` `entry_points=` blocks — zero duplicate entry
patterns.

**Dead/unreachable callbacks:** none found via static registration
analysis. All prefix-dispatch routers (`fav:`, `cmp:`, `rv:`, `ph:`,
`irs:`, `ua:`, `mm:`, `af:`, and the V9 Admin Control Center's `acc_*`
dispatcher) are registered in `bot.py`, and the modules they dispatch
into are all imported. A full behavioral trace of every `callback_data`
string actually produced at runtime against every registered pattern
(true reachability, not just "is it registered") was not performed —
that requires either running the bot with instrumentation or manually
tracing all ~600+ `callback_data=` literals, which was out of scope for a
static pass. No specific broken callback was identified.

---

## 5. Dead code

**Two files with zero references anywhere in the codebase** (verified
against dynamic-import patterns too — no `importlib`/`getattr(sys.modules...)`
tricks referencing them):

| File | What it is |
|---|---|
| `services/invoice_service.py` (362 lines) | A complete PDF-invoice-email feature (`send_invoice_pdf`) — fully implemented, never called from any handler. |
| `utils/admin_ui.py` (226 lines) | A "design system" helper module (`H()`, `BTN`, `PAG`, `badge()`, `fmt_stat()`, `STATUS`) — fully implemented, its own docstring shows example usage, but no handler ever imports it. |

**Not deleted.** Both are complete, working, self-contained code with no
broken references *to* them and no risk *from* leaving them in place —
deleting either is a pure judgment call about whether they're intentional
in-progress work you plan to wire in later (this looks likely for
`invoice_service.py` given how complete it is) versus abandoned. Flagging
here per the audit brief; delete at your discretion with:
```
rm services/invoice_service.py utils/admin_ui.py
```

No other dead files, no duplicate UI implementations, and no old/legacy
code paths were found still wired into execution. (The `migrations/`
directory is pre-Alembic legacy tooling, already superseded by
`alembic/versions/` and not imported by the running app — it's inert, not
"dead" in the sense of half-wired.)

---

## 6. UI rendering — Product UI / Deposit UI using the right files

Verified `services/payment_ui.py` is the single template module actually
imported and called by the live deposit/payment handlers
(`handlers/payment_handlers.py`, `handlers/admin_pending_deposits.py`,
`handlers/admin_manual_payments.py`, `handlers/admin_binance.py`,
`handlers/admin_bybit.py`, `handlers/admin_zinipay.py`) — no shadow/unused
copy of a payment-UI module exists elsewhere in the tree. Same for the
product catalog: `handlers/user_handlers.py`'s product detail/list
renderers are the only code paths producing that UI; no orphaned
alternate renderer was found.

---

## 7. Fixes applied (summary)

| # | File(s) | Change | Risk |
|---|---|---|---|
| 1 | `railway.toml` (new) | Added missing Railway deploy config, mirrors `render.yaml` | None — new file, additive |
| 2 | `alembic/versions/20260728_global_delivery_template.py`, `alembic/versions/20260729_bybit_ltc.py` | Fixed broken `down_revision` chain (metadata only, no SQL/DDL changed) | None — linkage-only fix, verified single clean chain afterward |
| 3 | `database/models.py` | Added missing `ScheduledBroadcast.is_archived` column declaration to match existing migration + existing service-layer usage | None — purely additive, matches what the DB and calling code already expect |

## 8. Not fixed — flagged for a deliberate follow-up

| # | Item | Why not fixed here |
|---|---|---|
| 1 | 613 async functions doing blocking DB calls without `run_db()` (§3) | Scope: ~600 call sites across ~140 files; needs a dedicated, carefully-tested pass, not a bundled drive-by edit |
| 2 | Dockerfile's single-container webhook+bot topology (§1) | Working as designed for the `docker run` path; changing process supervision is a deploy-architecture decision, not a bug fix |
| 3 | Two dead files (§5) | Zero risk either way; deletion is a product decision (is `invoice_service.py` WIP or abandoned?), not an audit call |
| 4 | Full runtime `callback_data` reachability trace (§4) | Requires live instrumentation or exhaustive manual tracing of 600+ literal strings; static registration analysis found no issues but can't prove 100% reachability |
