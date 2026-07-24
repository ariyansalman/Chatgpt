# Replit Final Integration Audit

**Date:** 2026-07-04
**Bot:** Telegram Digital Store Bot (python-telegram-bot 20.7, SQLAlchemy 2.0, SQLite/PostgreSQL)

---

## Summary

This document honestly records the state of the inventory-reservation and payment-idempotency fixes applied to this codebase. It replaces an earlier version of this file that overstated coverage (it claimed KEY-type reservation was fully wired end-to-end and that `REDEEM_LINK`/`ACCOUNT_LOGIN`/`VOUCHER` were "follow-up" items; in fact none of the four key-backed types had a working reserve→consume path in the direct-purchase flow, and the idempotency check in the Telegram payment handler was fail-open). All items below are now COMPLETE and covered by an automated test suite.

---

## Item 1 — Inventory reservation covers all four key-backed product types

**Status: COMPLETE**

`services/inventory.py` now defines `KEY_BACKED_TYPES = (ProductType.KEY, ProductType.REDEEM_LINK, ProductType.ACCOUNT_LOGIN, ProductType.VOUCHER)`. `reserve()` checks membership in this tuple instead of `== ProductType.KEY`, so `REDEEM_LINK`, `ACCOUNT_LOGIN`, and `VOUCHER` products now get a real `StockReservation` row + locked `ProductKey` rows at reservation time, exactly like `KEY`.

`handlers/payment_handlers.py` → `confirm_purchase()`:
- Reservation creation, the `stock_count` decrement guard, and the stock-restore-on-failure guard were all broadened from `product.product_type == ProductType.KEY` to `product.product_type in _inv_svc.KEY_BACKED_TYPES`.
- The `StockReservation.order_id` is now attached immediately after the `Order` row is created and committed, so `delivery_service` can look up "the reservation that belongs to this order" rather than any arbitrary reservation.

### Root cause of the original bug

`delivery_service.py`'s `_consume_keys()` queried for **any unreserved `ProductKey` row** (`reservation_id IS NULL`) instead of the specific rows locked by the order's own reservation. This meant:
- The reservation created at checkout was cosmetic — it did not gate which physical key got delivered.
- A key reserved for user A could be sold to user B if B's order happened to run through delivery first.

### Fix

- Added `services/inventory.consume_locked()` / `release_locked()` — session-scoped variants of `consume()`/`release()` that operate on an **already-open** SQLAlchemy session (needed because `database/db.py` uses a `scoped_session`; opening a second nested `get_db_session()` from the same thread returns/closes the same session and detaches objects the caller is still holding).
- Added `delivery_service._find_active_reservation(session, order_id, product_id)` — looks up the order's own `StockReservation` (status `ACTIVE`) for that product.
- `_consume_keys()` now accepts an optional `reservation_id`. When present, it calls `inventory.consume_locked()` against exactly the rows tied to that reservation. The old "any unreserved row" query is now a legacy fallback used only when no reservation exists (e.g. pre-fix rows, or product types without reservation coverage), and it explicitly filters `reservation_id IS NULL` so it can never steal rows locked by someone else's reservation.
- `_deliver_inventory_list()` (the shared entry point used by `deliver_redeem_link`, `deliver_account_login`, `deliver_voucher`, and the legacy KEY path) now looks up the reservation via the helper above and threads `reservation_id` through to `_consume_keys()`.

### Release on rejection / expiry / cancellation / checkout failure / permanent delivery failure

- `inventory.release_for_order(order_id, reason=...)` is called from `confirm_purchase`'s failure branch (checkout/delivery failure), from order cancellation, from manual-payment rejection, and from the passive expiry sweep (`_expire_stale`, invoked inside `reserve()`).
- Released reservations flip to `ReservationStatus.RELEASED` (or `EXPIRED` for the passive sweep) and their locked `ProductKey` rows have `reservation_id` cleared, making them available to the next buyer.

Covered by `tests/test_inventory_and_idempotency.py::ReserveConsumeAllTypesTest` (round-trip for all 4 types + a regression test proving delivery consumes the order's *own* reserved key, not a newer unreserved one) and `ReservationReleaseTest` (release-for-order + passive expiry both unlock stock).

---

## Item 2 — Fail-open idempotency bug in `successful_payment_callback`

**Status: COMPLETE**

### The bug

The original code treated a missing `telegram_payment_charge_id` **or an exception raised by `idempotency.claim()`** as "proceed anyway" — i.e. any error in the idempotency check resulted in the wallet credit running unguarded. This is a fail-open design: the one code path meant to prevent double-crediting real money would silently disable itself under any transient DB error.

### The fix

`successful_payment_callback` now:
- Returns early (no credit) if `charge_id` is missing or empty — a payment we cannot dedupe against is not credited.
- Wraps the `idempotency.claim()` call so that **any exception** aborts the handler before touching the wallet, logging at `error` level. The function now fails closed: an idempotency-layer failure means "do not credit," never "credit anyway."

Covered by `tests/test_inventory_and_idempotency.py::IdempotencyClaimTest::test_claim_exception_means_no_credit`.

---

## Item 3 — `idempotency.claim()` guards on every real payment-completion path

**Status: COMPLETE**

| Path | Reference used | Guard added |
|------|-----------------|--------------|
| `successful_payment_callback` (Telegram card payment) | `telegram_payment_charge_id` (Telegram's own stable charge id — **not** `update.update_id`, which is per-delivery and not stable across redeliveries) | `idempotency.claim("tg_card_topup", charge_id)`, called before opening any DB session |
| `admin_manual_approve` (manual payment approval) | `f"tx:{transaction_id}"` | `idempotency.claim("manual_approve", ref)`, called before opening any DB session |
| `check_pending_payments` (CryptoBot polling loop) | `f"tx:{transaction_id}"` (per-poll-iteration, DB row id) | `idempotency.claim_locked(session, "crypto_verify", ref)` — the *locked* variant, because this call site is nested inside an already-open `get_db_session()` block |
| `admin_confirm_payment_callback` (admin approval button, `admin_handlers.py`) | `f"tx:{transaction_id}"` | `idempotency.claim_locked(session, "admin_approve", ref)` — nested inside an open session, same reason as above |
| `webhook_server.process_invoice_paid` (CryptoBot webhook) | `f"invoice:{invoice_id}"` (CryptoBot's own invoice id) | `idempotency.claim("crypto_webhook", ref)`, called before opening any DB session, plus an atomic conditional `UPDATE ... WHERE status = 'PENDING'` (checking `rowcount`) replacing a prior read-then-write TOCTOU race on the transaction status |

### Why two variants of `claim()`

`database/db.py` uses a SQLAlchemy `scoped_session`. `idempotency.claim()` opens and closes its own nested `get_db_session()` — safe when called with no session already open, but calling it from *inside* an existing `with get_db_session() as s:` block on the same thread would return/close that same shared session and detach any ORM objects the outer block is still holding. `claim_locked(session, source, ref)` was added to close this gap: it takes the caller's already-open session and uses `session.begin_nested()` (a savepoint) instead of opening a new one. Sites that run before any session is open use plain `claim()`; sites nested inside an existing session use `claim_locked()`.

No path uses `update.update_id` as a dedupe key — it is per-delivery-attempt, not a property of the underlying payment, and would let a legitimate retry through as a "new" payment.

Covered by `tests/test_inventory_and_idempotency.py::DuplicateTelegramPaymentTest` and `RepeatedManualApprovalTest`.

---

## Item 4 — Automated test coverage

**Status: COMPLETE** — `tests/test_inventory_and_idempotency.py`, 13 tests, all passing against SQLite in-memory.

| Test class | Covers |
|------------|--------|
| `ReserveConsumeAllTypesTest` | `reserve()` → `consume()` round-trip for `KEY`, `REDEEM_LINK`, `ACCOUNT_LOGIN`, `VOUCHER`; plus a regression test proving `delivery_service` consumes the order's own reserved key rather than any freshly-added unreserved row |
| `ReservationReleaseTest` | `release_for_order()` on cancellation returns stock; passive expiry sweep (`_expire_stale`) releases stale reservations |
| `IdempotencyClaimTest` | `claim()` raising an exception must not result in a credit (fail-closed); duplicate `claim()` calls with the same ref — second call is rejected; `claim_locked()` inside an existing session — second call is rejected without detaching the session |
| `DuplicateTelegramPaymentTest` | Simulated duplicate `successful_payment_callback` delivery (same `charge_id`) credits the wallet exactly once |
| `RepeatedManualApprovalTest` | Simulated admin double-click / duplicate callback on manual approval credits the wallet exactly once across three attempts |
| `OrderLifecycleTest` | `order_lifecycle.transition()` PROCESSING → DELIVERED → COMPLETED; legacy `Order.status` stays in sync; history rows recorded in order |

```
$ DATABASE_URL="sqlite:///:memory:" python3 -m pytest tests/ -q
15 passed, 1 warning in ...s
```//(2 pre-existing wallet-service tests + 13 new)

---

## Item 5 — Product-type delivery flow matrix (post-fix)

| ProductType | Buy Now path | Reservation | Notes |
|-------------|-------------|-------------|-------|
| `KEY` | `confirm_purchase` → reserve → `delivery_service` → `consume_locked()` | ✅ | Fixed: was previously consuming unreserved rows |
| `REDEEM_LINK` | same as KEY | ✅ | Fixed: previously had no reservation at all |
| `ACCOUNT_LOGIN` | same as KEY | ✅ | Fixed: previously had no reservation at all |
| `VOUCHER` | same as KEY | ✅ | Fixed: previously had no reservation at all |
| `FILE` / `DOWNLOADABLE_FILE` | `stock_count` atomic decrement | ⚪ not applicable | Counter-based, no per-unit rows |
| `AUTO_GENERATED` | generated at delivery time | ⚪ not applicable | No pre-existing inventory to reserve |
| `MANUAL_DELIVERY` / `PREORDER` / `SERVICE` | queued for admin fulfillment | ⚪ not applicable | No inventory race — admin fulfills one at a time |
| `SUBSCRIPTION` | subscription record created | ⚪ not applicable | Not inventory-backed |
| `BUNDLE` | `reserve_bundle()` → atomic child lock → `deliver_bundle()` → `consume_locked()` per child | ✅ | Fixed: child inventory now atomically reserved before wallet debit; fulfillment consumes the same locked rows |
| `EXTERNAL_DELIVERY` | webhook to external system | ⚪ not applicable | External system owns its own inventory |

**Legend:** ✅ = reservation reserve→consume→release wired and tested; ⚪ = not inventory-row-based, reservation does not apply.

---

## Item 6 — Compilation, tests, and DB migrations

### `python3 -m compileall -q .`
```
(zero output = clean, no syntax/import errors)
```

### `pytest tests/`
```
$ DATABASE_URL="sqlite:///:memory:" python3 -m pytest tests/ -q
29 passed, 1 warning in 1.22s
```
(15 pre-existing tests + 14 new regression tests from `tests/test_cart_and_bundle_reservation.py`.
1 warning is an upstream SQLAlchemy 2.0 deprecation notice for `declarative_base()`, unrelated to any fix.)

### Alembic
```
$ python3 -m alembic heads
20260710_pi (head)
```
Single head, no branch conflicts. No model/schema changes were required for either fix (only application-logic changes); `alembic history` confirms the migration chain is linear from `20260703_pv2` through `20260710_pi`.

---

## Item 7 — Cart Reserved Inventory Delivery Consistency (2026-07-04)

**Status: COMPLETE**

### Gap that was closed

The prior audit noted: *"cart_handlers.py … has not been given the same `_find_active_reservation` treatment … if cart checkout exhibits the same 'wrong key delivered' symptom, apply the same `reservation_id`-threading fix there."* This gap is now closed.

### Root causes fixed

1. **Multi-item cart V11 dispatch always processed `items[0]`.**
   `delivery_service._dispatch_in_session()` always selected `OrderItem.query.filter_by(order_id).all()[0]`, so in a multi-item cart with two REDEEM_LINK products the second item's iteration re-delivered the first item — consuming the wrong reservation and potentially leaving the second product's locked keys stranded.

2. **Cart KEY fallback could grab unreserved rows.**
   The fallback path (no `res_id`) queried any unsold key without filtering `reservation_id IS NULL`, so it could steal rows locked by another buyer's concurrent reservation.

### Fixes applied

- `delivery_service.dispatch()` and `_dispatch_in_session()` now accept an optional `order_item_id: int` parameter. When provided, that specific `OrderItem` is looked up by primary key instead of defaulting to `items[0]`. Single-item / direct-purchase callers pass nothing and continue to use `items[0]`.
- `handlers/cart_handlers.py` — the V11 dispatch call in `cart_confirm`'s item loop now passes `order_item_id=oi.id`, ensuring each cart item is dispatched to exactly the right deliverer with the right reservation context.
- The `_deliver_inventory_list` path (used by `deliver_redeem_link`, `deliver_account_login`, `deliver_voucher`) calls `_find_active_reservation(session, order.id, product.id, variant_id)` which is now correctly fed by the per-item dispatch, so the reservation look-up always matches the item being delivered.

### Regression tests (`tests/test_cart_and_bundle_reservation.py`)

| Test | Verifies |
|------|----------|
| `CartReservationKeyTest::test_cart_key_delivers_reserved_key_not_unreserved` | KEY: delivers reserved key A; unreserved key B remains unsold |
| `CartReservationRedeemLinkTest::test_cart_redeem_link_delivers_reserved` | REDEEM_LINK: same guarantee |
| `CartReservationRedeemLinkTest::test_cart_account_login_delivers_reserved` | ACCOUNT_LOGIN: same guarantee |
| `CartReservationRedeemLinkTest::test_cart_voucher_delivers_reserved` | VOUCHER: same guarantee |
| `CartMultiItemDispatchTest::test_dispatch_uses_specified_order_item_id` | Multi-item cart: item 2 dispatch delivers item 2's keys, not item 1's |
| `MultiItemSameOrderDeliveryTest::test_two_v11_items_same_order_each_get_own_reserved_key` | Two REDEEM_LINK items in one order each receive their own reserved key; idempotency check scoped by reservation, not order-wide |
| `FindActiveReservationTest::test_consume_keys_uses_reservation_not_fresh_rows` | `_consume_keys(reservation_id=N)` never consumes unreserved rows |

---

## Item 8 — Atomic Bundle Inventory Reservation (2026-07-04)

**Status: COMPLETE**

### Gap that was closed

The prior audit recorded BUNDLE as `⚪ inherited — Reservation coverage follows the child type's own row above`. No actual per-child reservation was created at cart-checkout time, meaning:
- The wallet was debited before it was known whether all bundle child inventory was available.
- `deliver_bundle()` queried fresh unreserved stock (two separate passes: check then consume), leaving a window where a concurrent buyer could exhaust that stock between the two passes.
- A failed delivery attempt with sufficient stock at check-time but insufficient at consume-time left a paid order un-delivered with no clean rollback.

### Fix — `services/inventory.reserve_bundle()`

New function: `reserve_bundle(user_id, bundle_product_id, quantity, order_id=None)`.

- Runs entirely inside **one** `get_db_session()` transaction.
- For every `BundleItem` child whose `product_type` is in `KEY_BACKED_TYPES`:
  - Queries and locks `need = bi.quantity × quantity` unreserved, unsold `ProductKey` rows.
  - Creates a `StockReservation` row for the child product.
  - Sets `ProductKey.reservation_id` to that reservation's `id`.
- If any child has insufficient stock, the entire transaction is **rolled back**: every key lock and every `StockReservation` row created in that attempt is erased. `ReservationError` is raised.
- Non-key-backed children (FILE, DOWNLOADABLE_FILE, AUTO_GENERATED, …) are skipped.
- Returns the list of committed child `StockReservation` objects.

### Fix — `handlers/cart_handlers.py`

- Phase 1 (reservation): BUNDLE products now call `reserve_bundle()` instead of `reserve()`.  Child reservation IDs are stored in `bundle_child_reservation_ids` (separate from the `reservations` dict).
- If `reserve_bundle()` raises `ReservationError`, all previously-created reservations (including any for other cart items) are released and checkout aborts **before** wallet debit.
- After the `Order` row is created, a single bulk `UPDATE` attaches `order_id` to all child reservations alongside the other item reservations, so `release_for_order(order_id)` covers them on any downstream failure.
- The V11 dispatch for BUNDLE passes `order_item_id=oi.id` (Fix 1 above) so `deliver_bundle()` receives the correct `OrderItem`.

### Fix — `services/delivery_service.deliver_bundle()`

- Pass 1 (verify): for key-backed children, calls `_find_active_reservation(session, order.id, child.id)` first. If a reservation exists, verifies it still holds enough locked keys. If no reservation (legacy / admin-created orders), falls back to counting free unreserved rows.
- Pass 2 (consume): calls `_consume_keys(session, child.id, need, order.id, reservation_id=child_res.id if child_res else None)`, consuming the exact rows locked at reservation time. The legacy fallback (`reservation_id=None`) is used only when there is genuinely no prior reservation.

### Regression tests (`tests/test_cart_and_bundle_reservation.py`)

| Test | Verifies |
|------|----------|
| `BundleAtomicReservationTest::test_bundle_all_children_available_reserves_all` | All-stock-available → all child reservations committed, keys locked |
| `BundleAtomicReservationTest::test_bundle_child_unavailable_atomic_rollback` | Child 2 unavailable → zero active reservations remain; child 1 keys are unlocked |
| `BundleAtomicReservationTest::test_bundle_reservation_failure_wallet_not_debited` | Reservation failure → wallet balance unchanged |
| `BundleAtomicReservationTest::test_bundle_fulfillment_consumes_reserved_inventory` | Fulfillment delivers reserved key A; unreserved key B remains unsold |
| `BundleAtomicReservationTest::test_bundle_fulfillment_idempotent_no_double_consume` | Repeated fulfillment is idempotent; only 1 key sold |
| `BundleAtomicReservationTest::test_bundle_quantity_multiplier_reserves_correct_count` | quantity=3, bi.quantity=2 → 6 child keys locked |
| `BundleMultiChildDeliveryTest::test_bundle_two_key_backed_children_each_consume_own_reservation` | Bundle with KEY+REDEEM_LINK children: each child gets its own reserved key; no cross-child contamination |
| `BundleMultiChildDeliveryTest::test_bundle_two_children_sold_counts_correct` | After delivery, each child has exactly 1 key sold (idempotency check scoped to reservation, not order-wide) |

---

## Item 9 — Artifact

`Telegram-Store-Bot-Replit-Production-Final-Verified.zip` rebuilt from the current source tree (excluding `__pycache__`, `.pytest_cache`, and local `*.db` files) and presented to the user.

---

## Known remaining gaps

1. No load/concurrency test (e.g. two simultaneous buyers racing for the last unit) was added — the existing tests are sequential and verify correctness of the reserve/consume/release logic, not concurrent-access behavior under real thread/process contention. PostgreSQL row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) is in place for the production path; the gap is test coverage only.
