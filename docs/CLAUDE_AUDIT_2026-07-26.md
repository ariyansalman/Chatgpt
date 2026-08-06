# Audit — Payment / Wallet / Security-Critical Paths (2026-07-26)

## Scope and honesty note

This codebase is ~130,000 lines of Python across 368 files (`bot.py` alone
is 3,236 lines; `handlers/` totals ~89,000 lines; `services/` ~34,600
lines). A meaningful, evidence-based audit of a codebase this size cannot
be done exhaustively, file-by-file, in a single pass without either
taking many hours of tool calls or producing shallow, low-confidence
findings dressed up as a "complete" audit. Per your scoping choice, this
pass went **deep on the payment, wallet, and security-critical paths**
rather than broad-and-shallow across all 368 files. Findings below are
each backed by an actual line reference, not a guess.

The `docs/` folder already contains ~15 prior self-audits
(`FINAL_SOURCE_AUDIT.md`, `PAYMENT_GATEWAY_REGISTRY_ARCHITECTURE.md`,
`ADMIN_USER_PAYMENT_FINAL_AUDIT.md`, etc.). Those were read first. Some
are stale relative to the current code (e.g. `FINAL_SOURCE_AUDIT.md`
describes an `i18n` deletion that a later pass reversed — a new
`i18n/` package with 9 locale files now exists and is actively imported
from `bot.py`, `handlers/*.py`, and `utils/keyboards.py`). This is noted
as a process finding, not a runtime bug.

---

## Findings

### CRITICAL — NOWPayments webhook accepted unauthenticated crediting requests when IPN secret unset
**File:** `webhook_server.py`, `nowpayments_webhook()` (~line 719, fixed)

Every other provider webhook in this file (CryptoBot, Heleket, Cryptomus)
verifies a cryptographic signature unconditionally and rejects with 401
on failure. The NOWPayments handler was the one exception: if
`service.ipn_secret` was falsy (not configured), it logged a warning and
**proceeded to credit the wallet anyway**, trusting the raw POST body.

Impact: `order_id` in this flow is `str(Transaction.id)` — a small,
sequential, guessable integer. Anyone able to reach the webhook URL
(it's a public HTTP endpoint by necessity) could POST a forged
`payment_status=finished` for a real pending transaction and have it
credited as paid without any funds moving, if the admin had not set an
IPN secret. This is a direct path to fraudulent wallet credits.

**Fix:** When no IPN secret is configured, the webhook now acknowledges
(200, so NOWPayments doesn't retry-storm) but does **not** credit from
the unsigned payload — it fails closed. This doesn't break functionality:
`handlers/payment_handlers.py::check_pending_payments()` independently
polls `NowPaymentsService().check_payment_status()` — NOWPayments' own
authenticated API — on a repeating job (`bot.py`, registered via
`job_queue.run_repeating`), so pending NOWPayments deposits still get
credited automatically; they just arrive on the poll interval instead of
instantly, until an admin configures the IPN secret for instant webhook
crediting.

### HIGH — Dead-end "Cancel" screens on 3 of 3 crypto/P2P TXID-submission flows
**File:** `handlers/payment_handlers.py` — `zinipay_cancel_submit`,
`binance_cancel_submit`, `bybit_cancel_submit`

This matches the issue already flagged for `bybit_cancel_submit`
("displays a still pending message... may need further attention").
Auditing the other two gateways that share the same
`services/payment_ui.py::submit_txid_prompt()` pattern showed the same
bug in all three, not just Bybit: tapping "❌ Cancel" on the "submit your
Transaction ID" prompt replaced the message with plain text and **no
inline keyboard at all**. The order is intentionally left PENDING (that
part is correct — the user already sent/is sending funds and shouldn't
lose the order), but the user was left with zero buttons: no way back to
their payment page, wallet, support, or the main menu short of typing
`/start`.

**Fix:** Added `services/payment_ui.py::still_pending_keyboard()` and
wired it into all three cancel handlers. It offers a direct "🔄 Submit
TXID Again" button (re-enters the same mini-conversation for that exact
`tx_id`) plus "👛 My Wallet", "📞 Support", and "🏠 Back to Menu" —
matching the existing style of `payment_failed_keyboard()` /
`payment_expired_keyboard()` already used elsewhere in the same file.
No business logic changed: the order is still left PENDING exactly as
before, only navigation was added.

### HIGH — Payment approve/reject/re-verify used the coarse `is_admin()` check instead of the fine-grained RBAC + 2FA the rest of the codebase already uses for the same class of action
**File:** `handlers/payment_handlers.py` — `admin_manual_verify_again`,
`admin_manual_approve`, `admin_manual_reject`, and 6 more gates guarding
Binance/Bybit Pay PMV approve, reject, re-verify, and view-user actions
(previously `is_admin(update.effective_user.id)` at what were lines
1440, 1455, 1654, 6741, 7113, 7366, 7436, 7507, 7651).

The project has a proper RBAC system (`utils/permissions.py`): roles
`support_staff` / `moderator` / `super_admin`, a granular
`manage_payments` permission, and — critically — an enforced 2FA/OTP
session requirement via `has_permission()`. Every *other* admin file
that touches payments already uses it —
`handlers/admin_wallets.py`, `handlers/admin_manual_payments.py`, and
`handlers/admin_payment_methods.py` all gate on
`has_permission(uid, "manage_payments" | "manage_orders")`.
`handlers/payment_handlers.py` was the one file still using the old
blanket `is_admin()` (any active admin of any role, no 2FA check at all)
for approving/rejecting deposits that directly credit user wallets.

Impact: a lower-tier admin without the `manage_payments` permission
(e.g. a `support_staff` account meant only to handle support tickets)
could approve or reject real money deposits. If the store has 2FA
enforced (`is_2fa_enforced()`), those same actions were also bypassing
it entirely on this one file.

**Fix:** all 9 gates now use `has_permission(update.effective_user.id,
"manage_payments")`, matching the rest of the codebase exactly. The
bootstrap owner (`ADMIN_TELEGRAM_ID`) is unaffected — `get_admin()`
already treats them as an implicit, unremovable `super_admin`, and
`AdminInfo.has()` always returns `True` for `super_admin` regardless of
individual permission flags, so the redundant
`update.effective_user.id != app_settings.ADMIN_TELEGRAM_ID` check
that existed on 3 of the 9 gates was correctly dropped as dead logic,
not a behavior change.

**Behavior change to be aware of:** if 2FA is enforced store-wide, an
admin who has `manage_payments` but no currently-verified OTP session
will now be asked to `/admin_login` before these 9 actions succeed,
where before they were never prompted on this file. This is the
intended, already-existing security model applied consistently — not
new functionality — but it will be visible the first time each admin
hits one of these buttons after updating.



- **`bkash_webhook` / `nagad_webhook`** (`webhook_server.py`): never trust
  client-supplied `status`; always re-verify server-side against the
  provider's own API (`execute_payment()` / `verify_payment()`) before
  crediting. Good.
- **`_credit_wallet_once`** (`webhook_server.py`): idempotency claim +
  atomic conditional `UPDATE ... WHERE status = PENDING` + row-locked
  `credit_locked()` in one transaction, fail-closed on any exception.
  No double-credit / race window found.
- **`admin_manual_approve`** (`handlers/payment_handlers.py`): admin
  identity is checked before any mutation; idempotency claim + atomic
  conditional update guard a second concurrent tap.
- **`get_db_session()`** (`database/db.py`): correct
  commit/rollback/close context manager; `run_db()` correctly offloads
  blocking DB calls to a thread so the asyncio event loop isn't frozen.
- **Cryptomus / Heleket webhooks**: signature verification is
  unconditional (no bypass), unlike the NOWPayments handler above.
- **`topup_amount` / `validate_amount`**: custom top-up amount input is
  bounds-checked against admin-configured min/max before any transaction
  is created.

## Noted but not changed (out of scope for this pass / needs a product decision)

- **No global rate limiting** on callback/message handlers
  (`utils/permissions.py`, `bot.py` — only a 5-minute "last seen" write
  throttle exists, not a request rate limiter). Worth adding, but it's a
  cross-cutting change with UX trade-offs (thresholds, per-user vs.
  per-chat) that shouldn't be guessed at without your input.
- **`payment_handlers.py` is 7,679 lines** — a single file handling
  topup, 6+ gateways, admin review, and lifecycle polling. It works, but
  splitting it per-gateway would meaningfully improve maintainability.
  Flagged as a refactor, not attempted here since it's high-risk for
  low immediate safety benefit and you asked not to change working logic.
- Prior audit docs mention `services/backup.py` cloud upload being a
  stub and a TODO in `webhook_server.py` for user notification hooks —
  both were previously marked explicitly out-of-scope and were not
  re-investigated here.

## What this pass did NOT cover

Given the deep-scope choice, the following areas from your original
request were **not** independently re-audited this pass: admin panel
permission matrix beyond the one payment-approval check above, product
catalog / inventory reservation internals, referral/coupon math,
localization completeness across the 9 locale files, and general code
duplication across the ~89k lines of `handlers/`. Prior docs
(`ADMIN_USER_PAYMENT_FINAL_AUDIT.md`, `NAVIGATION_AUDIT_REPORT.md`,
`PENDING_DEPOSITS_QUERY_AUDIT.md`) cover some of that ground — treat
their claims with the same "verify before trusting" approach used here,
since at least one (`FINAL_SOURCE_AUDIT.md`) was found stale.
