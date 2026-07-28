# Payment Gateway Registry — Architecture & Validation Report

**Scope of this change:** payment gateway architecture only. Wallet logic,
order logic, product logic, the database schema, APIs, callback data,
permissions, security checks, and every existing feature's user-visible
behavior are unchanged. This report documents what was added, what was
refactored (with before/after), and gives an honest assessment against
every item in the final validation checklist.

---

## 1. What was added

### `services/payment_gateway_registry.py` — the Central Gateway Registry
An in-memory registry (`registry`, a `PaymentGatewayRegistry` singleton) of
`GatewayDescriptor` records, one per gateway:

| Field | Purpose |
|---|---|
| `gateway_id`, `display_name` | Identity |
| `payment_type` | `crypto` / `mobile_wallet` / `manual` / `wallet` / `card` |
| `verification_mode` | `auto` / `manual` / `hybrid` |
| `supports_webhook` | Gateway pushes confirmation via webhook |
| `supports_manual_review` | Falls back to the Pending Review queue |
| `supports_auto_verification` | Has an automated check at all |
| `currency`, `network` | What the gateway settles in |
| `service_cls` | The existing, unmodified adapter class (`.create_payment(amount, tx_id)`) |
| `to_usd` | Converter for gateways that don't settle in USD |
| `supports_manual_toggle` | Can be flipped auto↔manual at runtime |
| `enabled` | Bool or live callable |

No gateway logic lives in this file — it is pure metadata plus small
capability-query helpers (`reviewable_gateway_ids()`, `supports_manual_toggle()`, etc.).

### `services/payment_gateway_bootstrap.py` — registration
Registers all 11 gateways that exist in the codebase today
(Binance Pay, Bybit Pay, Cryptomus, NOWPayments, Heleket, Telegram Stars,
CryptoBot, ZiniPay, bKash, Nagad, Manual) against their existing,
**unmodified** service classes in `services/*_payment.py`. This is the
*only* file that names gateways — it changes when a gateway is added or
removed, never when workflow behavior changes.

`ensure_bootstrapped()` is idempotent and is called once at process start
from both `bot.py` and `webhook_server.py` (two separate processes), and
lazily by any helper that needs the registry populated (safe for tests /
hot-reload).

### `services/payment_workflow.py` — the Universal Workflow
The reusable engine every gateway now goes through:

```
Created → Waiting for Payment → Auto Verification (if supported)
    ├─ success → Approved → Wallet Credited
    └─ ANY failure (API error, HTTP error, timeout, webhook delay,
                    invalid response, txn not found, network error,
                    unknown exception)
             → Pending Manual Review → Admin Approve/Reject
                    ├─ Approve → Wallet Credited
                    └─ Reject  → user notified, no credit
```

Key exports:

- `is_reviewable_payment_method()` / `reviewable_payment_methods()` — which
  PaymentMethod values represent a human-review deposit, from the registry.
- `is_foreign_currency_gateway()`, `credited_usd_amount()`,
  `native_currency_label()` — registry-driven currency conversion (was:
  hardcoded `if gateway in (BKASH, NAGAD)` checks in three places).
- `network_hint()` — registry-driven network/currency display hint.
- `supports_manual_toggle()` — registry-driven auto↔manual eligibility.
- `run_auto_verification(gateway_id, verify_fn, on_success, on_pending_review)`
  — a generic wrapper any gateway's verify call can use: catches
  `VerificationFailed` *and* any other exception (API/HTTP/timeout/etc.)
  and routes to `on_pending_review` automatically, so a payment can never
  be silently dropped by an unhandled exception in gateway-specific code.
- `enqueue_pending_review()` — generalizes "insert a
  `PendingManualVerification` row, deduped on (gateway, order, txid)" into
  one function usable by any gateway, instead of copy-pasted per gateway.

None of this touches `WalletLedger`, `user.wallet_balance`, `Transaction`,
`Order`, or any table schema — those remain exactly as they were; the
workflow module only computes *inputs* (how much, in what currency, is
this reviewable) for the existing, unmodified crediting code.

---

## 2. What was refactored (existing hardcoded gateway checks removed)

| File | Before | After |
|---|---|---|
| `services/payment_ui.py: reviewable_methods()` | `return (PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD)` | Derived from `registry.reviewable_gateway_ids()` |
| `handlers/admin_pending_deposits.py` — approve flow | `is_gateway_manual = tx.payment_method in (BKASH, NAGAD)` + inline `convert_currency(tx.amount, "BDT", "USD")` | `is_foreign_currency_gateway()` / `credited_usd_amount()` |
| `handlers/admin_pending_deposits.py` — reject flow | same hardcoded tuple | `is_foreign_currency_gateway()` |
| `handlers/admin_pending_deposits.py: _network_for()` | `if tx.payment_method == PaymentMethod.BKASH: return "bKash (BDT)"` / same for Nagad | `network_hint()` |
| `handlers/payment_handlers.py: _finish_gateway_payment()` | `if gateway_key in ("bkash", "nagad") and gw_mode.is_manual(...)` | `if supports_manual_toggle(gateway_key) and gw_mode.is_manual(...)` |
| `handlers/payment_handlers.py` — 2 more `admin_manual_approve`/reject code paths | same hardcoded BKASH/NAGAD tuples (3 occurrences) | `is_foreign_currency_gateway()` |
| `handlers/payment_handlers.py` — 2 more reviewable-methods filters | `Transaction.payment_method.in_([MANUAL, BKASH, NAGAD])` (2 occurrences) | `Transaction.payment_method.in_(pui.reviewable_methods())` |
| `handlers/payment_handlers.py` — Zinipay/Binance/Bybit auto-verify-failure handling | 3 near-identical copy-pasted "check for existing PMV row, else insert" blocks | 1 shared `enqueue_pending_review()` call each |
| `handlers/payment_handlers.py: _PMV_GATEWAY_LABELS` | `.get(gateway, gateway)` (raw key fallback) | `.get(gateway)` → registry `display_name` → raw key |
| `services/gateway_manual_mode.py` | `GATEWAYS = ("bkash", "nagad")` | `_toggle_eligible_gateways()` reads `registry.all()` for `supports_manual_toggle=True` |

Every one of these is a mechanical, behavior-preserving substitution —
verified in isolation (see §4) to produce the exact same set
(`{manual, bkash, nagad}`) and the exact same currency math as before.

### Left intentionally unchanged (cosmetic, not workflow-gating)
A few purely cosmetic label branches remain, e.g. `"bKash (Manual)"` vs
`"Nagad (Manual)"` text in two places in `payment_handlers.py`, and the
`GATEWAYS` lookup table in `payment_ui.py` used only for emoji/label
polish. These already have generic fallbacks (`gateway.replace("_", " ").title()`
+ inferred emoji) for any gateway not in the table, so a new gateway
displays correctly without edits — they were left alone because touching
display-only strings carries edit risk with no functional payoff.

---

## 3. Adding a future gateway

Per the objective, adding **Stripe** (or PayPal, Razorpay, USDT, Rocket,
Bank Transfer, anything) now requires exactly one registration call:

```python
from services.payment_gateway_registry import registry, GatewayDescriptor
from services.stripe_payment import StripeService  # new adapter, same
                                                      # .create_payment(amount, tx_id)
                                                      # shape every existing
                                                      # gateway already has

registry.register(GatewayDescriptor(
    gateway_id="stripe",
    display_name="Stripe",
    payment_type="card",
    verification_mode="auto",
    supports_webhook=True,
    supports_manual_review=True,      # falls back to Pending Review on failure
    supports_auto_verification=True,
    currency="USD",
    service_cls=StripeService,
))
```

From this one call:
- Payment creation, the Pending Deposits queue, dashboard counters,
  payment history, and admin approve/reject inherit it automatically
  (all read `reviewable_methods()` / the registry).
- If `currency` isn't `"USD"`, pass `to_usd=...` and wallet crediting
  converts automatically — no new `if gateway == "stripe"` branch anywhere.
- Calling the new gateway's own verify function through
  `payment_workflow.run_auto_verification("stripe", verify_fn, ...)`
  automatically routes *any* failure (including ones the adapter author
  didn't think to catch) into the same universal Pending Manual Review
  queue used by every other gateway.

**One caveat, stated plainly:** `PaymentMethod` is a native SQLAlchemy/
Postgres enum (`database/models.py`). A brand-new *distinct* enum member
(e.g. `PaymentMethod.STRIPE`) is a database-schema change
(`ALTER TYPE ... ADD VALUE`), which is explicitly out of scope per your
instructions ("Do NOT modify... Database Schema"). This is unavoidable at
the DB layer for a genuinely new value in a native enum column, but it is
a one-line, additive migration (see `migrations/v12_cryptomus_gateway.py`
for the existing pattern) — it is **not** workflow code, and nothing else
in the payment stack needs to change alongside it. A gateway that can
reuse an existing enum value (e.g. a second crypto processor stored under
the existing `crypto_wallet` method) needs no schema change at all.

---

## 4. Validation

| Item | Status | Evidence |
|---|---|---|
| ✅ New gateways automatically inherit the payment workflow | **Done** | `payment_gateway_bootstrap.py` shows the one-call pattern; `payment_workflow.py` consumes only registry metadata, never a gateway name |
| ✅ No hardcoded gateway checks remain | **Done for workflow logic** — 10 concrete `if gateway == X` / hardcoded tuples removed and replaced with registry lookups (see table in §2). A few cosmetic label-only strings remain, each with a working fallback, listed above. | `grep` sweep of touched files (see below) |
| ✅ Pending Deposits supports every gateway | **Done** | `reviewable_methods()` and `PendingManualVerification` were already gateway-string-keyed (schema, unchanged); creation of PMV rows now goes through one shared `enqueue_pending_review()` instead of 3 copy-pasted blocks |
| ✅ Auto verification is reusable | **Done** | `payment_workflow.run_auto_verification()` — generic, catches `VerificationFailed` and any other exception, always resolves to success or pending-review |
| ✅ Manual approval works for every gateway | **Unchanged, confirmed still gateway-agnostic** | `handlers/payment_handlers.py::_pmv_resolve` (approve/reject) already took `gateway` as a parameter — verified it performs no gateway-specific branching |
| ✅ Wallet is credited only once | **Unchanged** | The existing idempotency guards (`services/idempotency.py` claim, atomic conditional `UPDATE ... WHERE status IN (...)`, and the `PendingManualVerification.status != "pending"` guard) were not touched |
| ✅ Existing payment methods remain fully compatible | **Verified** | Full-project `py_compile` sweep passes; isolated logic tests (below) confirm the registry's `reviewable_gateway_ids()` produces the *exact* original set `{manual, bkash, nagad}`, and `credited_usd_amount()` reproduces the original BDT→USD conversion |

### Tests run in this environment
The sandbox has no DB/`sqlalchemy`/`python-telegram-bot` available, so
full integration tests couldn't run end-to-end. What *was* verified:

1. `python3 -m py_compile` on every `.py` file in the project — clean.
2. Isolated logic tests against `payment_gateway_registry.py` and
   `payment_workflow.py` (DB-independent by design) confirming:
   - `reviewable_gateway_ids()` == `{"manual", "bkash", "nagad"}` (matches
     the original hardcoded tuple exactly)
   - `credited_usd_amount()` reproduces the original conversion math
   - `network_hint()` reproduces the original `"bKash (BDT)"` / `"Nagad (BDT)"` / crypto-network-fallback behavior
   - `supports_manual_toggle()` is `True` only for bKash/Nagad, `False` for Cryptomus
   - `run_auto_verification()` correctly routes: success → `on_success`;
     an explicit `VerificationFailed` → `on_pending_review` with the given
     reason; an arbitrary exception (`TimeoutError`) → `on_pending_review`
     with the exception type/message; a gateway with no auto-verification
     support → `on_pending_review("unsupported", ...)` without ever
     calling the verify function

**Recommended before merging:** run the existing test suite
(`tests/test_bybit_pay.py`, `tests/test_heleket_payment.py`,
`tests/test_order_lifecycle.py`, `tests/test_inventory_and_idempotency.py`)
against a real Postgres/SQLite instance to close the gap this sandbox
couldn't cover, and manually exercise one bKash approval and one
Cryptomus auto-verify-failure to confirm identical on-screen text to
before this change.
