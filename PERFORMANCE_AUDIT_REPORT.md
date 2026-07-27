# Telegram Bot — Performance Audit Report
**Scope:** responsiveness/latency only. No business, payment, wallet, order, referral, or UI logic was changed.
**Codebase:** 370 Python files (138 handler files, ~1.7M of services, 224K models.py).

---

## 1. Root-cause summary

The bot runs on a **single asyncio event loop** (python-telegram-bot `Application`). Every
Telegram update (including every button tap) is processed on that one loop. The production
database is **PostgreSQL/Supabase accessed through synchronous SQLAlchemy** (`database/db.py`
builds a plain blocking `Engine`, no async driver). That combination has one hard rule: **any
synchronous, blocking call made directly inside an `async def` handler freezes the bot for
every user, not just the one who tapped**, for the duration of that call (a real network round
trip to Postgres or to an external payment API).

The "2–3 taps before responding" symptom is exactly what you'd expect from this: tap 1 starts a
handler that opens a blocking DB session or calls a payment gateway's HTTP API; while that's in
flight, the event loop cannot service *any* other update — including the `answer()` for that
same tap, or the next tap the impatient user sends. The button visually "hangs," the user taps
again, and the second/third tap is what finally gets processed once the first blocking call
returns.

The codebase already contains the correct fix pattern in one place — `database/db.py`'s
`run_db()` helper (`await asyncio.to_thread(...)`) — and one handler (`main_menu_callback` in
`handlers/user_handlers.py`) uses it correctly. It was just never propagated to the rest of the
codebase.

## 2. Findings, by severity

### 🔴 CRITICAL — Synchronous DB session calls made directly inside async handlers
- `with get_db_session()` appears **1,213 times** across `handlers/` and `services/`.
- **97 of 138 handler files** (and most service files) call it **directly**, not through
  `run_db()`/`asyncio.to_thread`.
- Only **4 files in the entire project** (`database/db.py` itself, `handlers/payment_handlers.py`,
  `handlers/user_handlers.py`, `services/anti_spam.py`) route any DB work through `run_db()`.
- Every one of the other ~1,100+ call sites blocks the shared event loop for a full Postgres
  round trip on every tap/message that reaches it.
- Reference (correct) implementation already in the codebase, for comparison:
  `handlers/user_handlers.py::main_menu_callback` — answers the tap immediately, then does
  `await run_db(_load_home, ...)`.

**This is the dominant cause of the multi-tap symptom.** Fixing it properly means, file by file:
wrap each handler's synchronous DB logic in a small function and call it via `run_db()`,
returning only plain data (not live ORM objects) across the thread boundary — the same pattern
`run_db()`'s own docstring documents. This is a mechanical but *not* automatable-safely change
(each call site needs a human to check what ORM attributes are used after the session would
close), so full remediation across all 97 files is a larger follow-up effort — see §4 for the
prioritized order to tackle it in.

### 🔴 CRITICAL — `query.answer()` is not reliably called immediately
- `utils/callback_safety.py::guarded_callback` is the only mechanism in the codebase designed to
  *guarantee* an immediate, crash-safe answer — but it was adopted in only **1 of 138** handler
  files, despite `bot.py` registering **568** `CallbackQueryHandler`s.
- Even where it *was* used, its old implementation only answered the query from the `except`
  branch (i.e., only guaranteed an eventual answer *if the handler crashed*), not proactively
  before the handler's own work ran — so a slow-but-non-crashing handler still left the button
  spinning. **Fixed** (see §3).
- Elsewhere, handlers call `await query.answer()` manually at inconsistent points — sometimes
  first, sometimes after a permission check, sometimes not until after a DB read.

### 🟠 HIGH — Blocking external HTTP calls to payment gateways inside the event loop
- 12 service files (`services/*_payment.py`, `crypto_bot.py`, `exchange_rate_service.py`,
  `ltc_rate.py`, `pricing.py`) use the synchronous `requests` library for gateway/API calls
  (typically 1–5 seconds, longer under network issues).
- Confirmed hot path: **every** gateway (Cryptomus, bKash, Nagad, NOWPayments, Binance Pay,
  Bybit Pay, Heleket, ZiniPay, …) funnels through one dispatch point,
  `handlers/payment_handlers.py::_finish_gateway_automated_payment`, which called
  `service.create_payment(...)` **directly**, un-awaited-via-thread, blocking the whole bot for
  as long as the gateway's API took to respond. A second call site (orphaned-transaction
  recovery, same function) had the identical problem — and additionally held a DB connection
  from the pool open for the duration of the HTTP call. **Fixed** (see §3).
- `services/exchange_rate_service.py` has the same blocking-`requests`-on-cache-miss pattern,
  mitigated somewhat by a 60s cache, but not yet thread-offloaded. Flagged for follow-up.

### 🟡 MEDIUM — Lazy per-user cache misses make a blocking DB call inline
- `i18n/__init__.py::get_user_language` (5 min TTL) and `utils/helpers.py::check_user_banned`
  (30s TTL) are called near the top of nearly every handler. Both are well-designed caches, and
  both are pre-warmed by the throttled activity middleware (`bot.py::_track_activity`, which
  itself correctly runs off-loop via `asyncio.to_thread`) — but on a genuine cache miss (new
  user, first message after restart, cache just expired) they still make a **synchronous**
  `with get_db_session()` call inline. Low frequency relative to §2/§3 issues, but worth folding
  into the same remediation pass since the pattern is identical.

### 🟢 LOW / Already good — no action needed
- **Config reads** (`utils/bot_config.py`): a well-built 30s TTL cache with stale-while-revalidate
  (background thread refresh, never blocks a request) and startup preload. No repeated/uncached
  config reads found.
- **SQL indexes** (`database/models.py`): 434 indexed columns; `telegram_id` and other hot lookup
  columns are indexed on every table checked (`users`, admin tables, sessions, orders, etc.). No
  missing-index pattern found on the primary lookup paths.
- **Global middleware** (`bot.py::_track_activity`, `_maintenance_gate`): already correctly
  offloads its DB work via `asyncio.to_thread` and reads config through the cached accessor.
- **No blocking `time.sleep()`** found anywhere in production handler/service code.
- **Webhook server** (`webhook_server.py`) is a separate synchronous Flask process handling
  payment-gateway callbacks — it does **not** share the bot's asyncio event loop, so it is not a
  factor in inline-button responsiveness. (It has its own, separate blocking-DB-call profile if
  webhook latency ever becomes a concern, but that's out of scope here.)
- **JobQueue**: ~30 `run_repeating`/`run_daily` jobs (broadcasts, health checks, reminders,
  scheduler ticks). These run on the *same* event loop as button handlers, so any job that does
  synchronous DB work directly will periodically stall buttons too. Spot-checked
  `health_monitor.py::health_check_job` — already lock-guarded and reasonably careful. The
  broadcast/reminder jobs live in the same handler/service files flagged in §2 and should be
  covered by the same `run_db()` remediation pass rather than treated separately.

## 3. Fixes applied in this pass

Both changes are mechanical (execution-context only) — **no business, payment, wallet, order, or
UI logic changed.**

1. **`utils/callback_safety.py` — `guarded_callback`**
   Now calls `safe_answer(query)` immediately, before the wrapped handler runs, instead of only
   guaranteeing an answer from the exception path. `safe_answer()` is idempotent and swallows
   Telegram's "already answered" error, so handlers that also call `query.answer()` themselves
   later (e.g. to show a validation alert) are unaffected.

2. **`handlers/payment_handlers.py` — gateway payment creation**
   Both call sites that invoke a gateway service's `create_payment(...)` (the primary
   "start a new payment" path and the orphaned-transaction recovery path inside
   `_finish_gateway_automated_payment`) now run that blocking `requests` call via
   `await asyncio.to_thread(...)` instead of calling it directly on the event loop. Since every
   gateway funnels through this one dispatcher, this single change protects the bot from
   freezing on **any** payment gateway's slow/unresponsive API — for every gateway, in one place.

## 4. Phase 2 — cart / wallet / search handlers (highest-traffic, done)

Following the roadmap below, the three highest-traffic storefront handler files have now been
converted to the `run_db()` pattern. **No business/order/wallet logic changed** — every DB
query, filter, mutation, and return value is identical; only *where* the synchronous DB code
executes changed (worker thread instead of the event loop).

- **`handlers/search_handlers.py`** — product search query + stock lookup now run via `run_db()`.
  Also fixed a related N+1: `get_user_currency()` was being called once **per search result
  row** (each call its own blocking DB round trip); it's now fetched once per search.
- **`handlers/wallet_handlers.py`** — wallet balance/totals/history query and the currency-toggle
  write now run via `run_db()`.
- **`handlers/cart_handlers.py`** — `cart_view`, `cart_add`, `cart_add_variant`, `cart_inc`,
  `cart_dec`, `cart_remove`, `cart_clear`, and the checkout **preview** screen (`cart_checkout`)
  now run their DB work via `run_db()`. Where a handler used to call `query.edit_message_text()`
  or `query.answer()` *while a DB session was still open* (not safe to run on a worker thread),
  it was restructured to: do the DB work in a sync function that returns a plain-data status,
  then perform the Telegram call afterwards based on that status — same user-visible behavior,
  cleaner separation of I/O.

  **Deliberately left untouched: `cart_confirm`** (the actual wallet-debit / inventory-reservation
  / order-creation checkout execution). This function interleaves the atomic wallet debit,
  `inventory.reserve()`/`release()` calls, and Telegram messages in a specific sequence that
  gives its correctness guarantees (no overselling, no double-charging on a failed step). Given
  the explicit instruction not to touch payment/wallet/order logic, and the real risk of
  introducing a money-handling bug by restructuring that sequence, this was left exactly as-is.
  It still makes several blocking DB calls directly on the event loop and remains the top item
  for a *dedicated, carefully-tested* follow-up pass — not a mechanical one.

## 5. Recommended remediation roadmap for the remaining files

Full remediation of §2's CRITICAL finding requires touching ~97 handler files individually
(convert each synchronous DB block to a small function + `await run_db(fn, ...)`, returning
plain data). That is too large and too risky to do as a blanket automated change in the same
pass as this audit — each site needs a human to confirm which ORM attributes are read after the
session would otherwise close. Suggested order, highest user-facing traffic first:

1. ~~Core navigation & shopping: `cart_handlers.py`, `search_handlers.py`, `wallet_handlers.py`~~
   — **done, see §4.** Still open: `variant_handlers.py`, `reservation_handlers.py`,
   `coupon_handlers.py`, and `cart_handlers.py::cart_confirm` specifically (see §4 note).
2. Account/engagement surfaces: `account_features.py`, `referral_handlers.py`, `loyalty_handlers.py`,
   `review_handlers.py`, `support_handlers.py`, `gift_purchase_handlers.py`, `vip_handlers.py`.
3. Admin handlers (`admin_*.py`, ~70 files) — lower tap volume than the storefront, but several
   (`admin_dashboard.py`, `admin_broadcast_center.py`, `admin_pending_deposits.py`,
   `admin_manual_payments.py`) are used constantly during business hours and are worth prioritizing.
4. `services/exchange_rate_service.py`, `services/ltc_rate.py`, `services/pricing.py` — thread-offload
   the remaining blocking `requests` calls the same way §3's payment fix did.

A useful interim signal while this rolls out: `utils/perf.py` already provides `@perf_track()` and
`perf_step()` and logs anything over 1s as `SLOW DB`. Turning that on for the files above during
rollout will make regressions and remaining hot spots visible in production logs.
