# Manual Payment Review — Audit & Synchronization Fix Report

**Scope respected:** No changes were made to payment logic, wallet logic,
business/verification logic, database schema, APIs, routes, callback_data,
permissions, or security. Every change below is either (a) a query that
*reads* existing state more consistently, or (b) a message/keyboard
*rendering* change that routes through the project's own existing
centralized template module, `services/payment_ui.py`.

---

## 1. Root cause of "Pending Deposits count doesn't match"

Three separate places in the codebase independently defined *"which
transactions count as a deposit awaiting manual review"*, and they had
drifted apart:

| Location | Filter used |
|---|---|
| `handlers/admin_pending_deposits.py` (`pd:*`, the dedicated review queue) | `MANUAL, BKASH, NAGAD` + `PENDING/AWAITING_CONFIRMATION` |
| `handlers/admin_handlers.py` (`admin_confirm_order_menu`, the Payments menu badge) | same tuple, hand-copied a second time |
| `handlers/admin_manual_payments.py` (legacy `mp:*` panel) | `MANUAL` **only** — bKash/Nagad manual-mode deposits were invisible here |
| `handlers/admin_dashboard.py` (`_collect_dashboard_stats`, the top-level "💳 Payments (N)" badge) | **every** pending Transaction on **every** gateway, including Binance Pay / Bybit Pay / ZiniPay / Cryptomus / NOWPayments / Heleket / Stars — none of which need a human, they're just waiting on their own webhook |

Because the dashboard badge counted gateway-pending transactions that
have nothing to do with manual review, and the legacy panel undercounted
by missing bKash/Nagad, no two of these four screens reliably agreed.

**Fix:** added one function, `services.payment_ui.count_pending_deposits(session)`,
as the single source of truth. It returns:
```python
{"deposits": <reviewable Transaction rows pending>,
 "gateway_verifications": <PendingManualVerification rows pending>,
 "total": <sum>}
```
`admin_pending_deposits.py`, `admin_handlers.py`, and `admin_dashboard.py`
now all call this one function instead of hand-copied filters. The legacy
`mp:*` panel's own history view was left with its original (broader,
all-status) scope — see §4 — since narrowing it would have removed an
existing history-browsing feature rather than fixed a bug.

---

## 2. Root cause of "Review screen shows a deposit while the list shows 0"

This one is real and has nothing to do with a race condition — it's a
second, entirely separate manual-review queue that the "Pending Deposits"
screen never counted:

- When a Binance Pay / Bybit Pay / ZiniPay payment's **automatic**
  verification fails (API error, amount mismatch, TXID not found, etc.),
  a row is written to `PendingManualVerification` and the admin gets a
  live Approve/Reject notification (`handlers/payment_handlers.py`,
  `admin_binance.py`, `admin_bybit.py`).
- That row is a genuine "waiting for a human" state — but the
  `pd:list` counter and header only ever queried the `Transaction` table
  (by design — see that module's own docstring on why gateways are
  excluded from it), so it could show **(0)** while one of these was
  sitting in an admin's chat waiting for a tap.

**Fix:** `count_pending_deposits()` (see §1) also counts pending
`PendingManualVerification` rows. Rather than silently merging two
differently-shaped queues into one list (which would have meant inventing
new callback routes — out of scope), the **Pending Deposits list header**
and the **Payments menu header** now both transparently show this second
number alongside the deposit count, e.g.:

> 🧾 Pending Deposits (2)
> Deposits waiting for manual review.
> ⚠️ 1 gateway verification(s) also awaiting review (Binance/Bybit/ZiniPay panels).

So it is no longer possible for any of these three screens to say "0
waiting" while a review is genuinely pending somewhere in the bot.

---

## 3. "Gateway" vs "Payment Method" — traced to the shared template itself

The mixing wasn't just in individual handlers — the *centralized* renderer
had the same inconsistency baked in:

- `services/payment_ui.py :: admin_review_card()` → used **"Gateway"**
- `handlers/admin_pending_deposits.py :: _deposit_detail_msg()` → used **"Payment Method"** (built its own card, didn't call the shared one)
- `handlers/admin_manual_payments.py :: _payment_msg()` → used **"Gateway"** (also built its own card)

**Fix:** standardized on **"Payment Method"** everywhere in
`services/payment_ui.py` (`admin_review_card`, `user_payment_card`,
`PaymentMethodView.render`), and rewired both handler modules to stop
hand-building cards — they now call `pui.admin_review_card()` /
`pui.build_card()` and simply supply values.

---

## 4. "Multiple templates / old layouts / different handlers build different layouts"

Confirmed three independent card builders for what is conceptually one
screen (an admin reviewing one payment):

1. `services/payment_ui.py :: admin_review_card()` — the intended shared one, but already used by the Binance/Bybit/ZiniPay PendingManualVerification notifications, so those were already consistent with each other.
2. `handlers/admin_pending_deposits.py :: _deposit_detail_msg()` / `_deposit_kb()` — hand-rolled, different field set (no Network, no Verification Result, no View User button), different button emoji (🟢/🔴 instead of ✅/❌).
3. `handlers/admin_manual_payments.py :: _payment_msg()` — hand-rolled, "Gateway" label, badge stuffed into a free-text `note` instead of the shared `status_key` styling.

**Fix:**
- `admin_pending_deposits.py`'s detail screen now calls
  `pui.admin_review_card(...)` / `pui.admin_review_keyboard(...)` directly
  — it supplies `network` and `verification_result` (new explicit fields,
  see §5) and gets the identical layout the gateway-verification cards
  already used.
- `admin_manual_payments.py`'s card now calls `pui.build_card(...)` with
  `status_key=` instead of hand-building a badge string.
- No handler in this flow builds its own HTML/text layout by hand anymore
  for the primary review screen — every one calls into `payment_ui.py`.

One screen was **left untouched by design**: `admin_pending_deposits.py`'s
"📜 View Details" (`pd:info:{tx_id}`) is a distinct, secondary screen (raw
proof/admin-note dump), not the primary review card the spec describes —
changing it wasn't necessary to satisfy the "one template" requirement and
risked scope creep into a feature that already works correctly.

Also **left untouched by design** (flagged, not fixed, to avoid
overreach): `admin_binance.py :: admin_binance_pending()` and the Bybit
equivalent render their *list* of pending gateway verifications with their
own plain-text format rather than `payment_ui`. This is a list view, not
the single-item review card the task's field spec describes, and each row
already links to the standardized `admin_review_card` detail notification.
Recommended follow-up if you want 100% template unity: migrate these two
list renderers onto `pui.build_card` per-row as well.

---

## 5. Admin Review Screen — brought in line with the requested field set

`payment_ui.admin_review_card()` now takes explicit `network` and
`verification_result` parameters (previously these could only be smuggled
in via a generic `extra` list, which is why some cards had them and others
didn't). Field order is now fixed everywhere:

> 💳 Payment Method → 💰 Amount → 🧾 Deposit ID → 🔗 Transaction ID →
> 👤 Customer → 🆔 Telegram ID → 🌐 Network → ⚠ Verification Result → status

`admin_pending_deposits.py`'s card populates `verification_result` with
*"Not auto-verifiable — human review required"* (accurate for
Manual/bKash/Nagad submissions, which never had an API to check against)
and `network` with `"bKash (BDT)"` / `"Nagad (BDT)"` where applicable, or
omits it cleanly (no blank row) otherwise.

`payment_ui.admin_review_keyboard()` gained an optional `back_cb` param so
**⬅ Back** is available in the same fixed button order as everything else:
🔄 Verify Again → ✅ Approve → ❌ Reject → 👤 View User → ⬅ Back. The
"View User" button reuses the **existing** `admin_view_user_pmv_<id>`
callback (already registered in `bot.py` for the gateway-verification
cards) — no new routes were created.

---

## 6. User Review Screen — verified, not changed

`services/payment_ui.py :: pending_review_card()` (the screen a user sees
after submitting proof/TXID) was already correctly scoped to exactly:

> 💳 Payment Method → 💰 Amount → 🧾 Deposit ID → (🔗 Transaction ID, only
> if it differs from the Deposit ID) → 🔍 Status

I checked every call site in `handlers/payment_handlers.py` (11 call
sites) individually — none of them pass Telegram ID, username, or network
into it. This part of the system was already compliant with the spec; no
change was needed or made.

---

## 7. Dashed separator lines

Found and removed the one place they were structurally baked in:
`services/payment_ui.py`'s `DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"`, used by
`build_card()` between every section and by `admin_resolution_suffix()`.
Both now use a plain blank line. Since every payment/deposit screen in
scope renders through this one function (or through
`utils/notify_format.py`, which never had dashes), this single fix removes
the dashed lines from every Manual Review screen at once.

I also swept the rest of the repository for the same characters
(━ ─ ┈ ▬ ═) — the only other user-facing hits were in unrelated features
(Products list maintenance banner in `user_handlers.py`, some
inventory/analytics banners). Those are outside the Manual Review system
this audit covers and were left alone to avoid scope creep; flagging them
here in case you'd like a follow-up pass.

---

## 8. Files changed

| File | What changed |
|---|---|
| `services/payment_ui.py` | Removed dashed `DIVIDER`; renamed "Gateway"→"Payment Method" and "User ID"→"Telegram ID"; added `network`/`verification_result` params to `admin_review_card`; added `back_cb` to `admin_review_keyboard`; added `reviewable_methods()`, `pending_tx_statuses()`, `count_pending_deposits()` as the single shared source of truth. |
| `handlers/admin_pending_deposits.py` | Sources its reviewable-methods/status tuples from `payment_ui` instead of a local copy; detail screen rebuilt on `pui.admin_review_card`/`admin_review_keyboard` (adds Network, Verification Result, View User, Back); list header now also surfaces the gateway-verification count. |
| `handlers/admin_handlers.py` | Payments menu badge now uses `pui.count_pending_deposits()` instead of a hand-copied query; header also surfaces the gateway-verification count. |
| `handlers/admin_dashboard.py` | Dashboard "💳 Payments" badge now uses `pui.count_pending_deposits()["total"]` instead of counting every pending transaction on every gateway. |
| `handlers/admin_manual_payments.py` | Card rendering switched from a hand-built "Gateway" + free-text badge layout to `pui.build_card(status_key=...)` with "Payment Method"/"Telegram ID" labels. |

## 9. Verified unchanged

- Every callback_data string, route pattern, and handler registration in `bot.py` — untouched.
- Approve/reject/idempotency logic, wallet crediting, BDT→USD conversion — untouched.
- Database schema (`database/models.py`) — untouched.
- Permission checks (`has_permission(...)` calls) — untouched.
- All edited files pass `py_compile` with no syntax errors.
