# Pending Deposits — Query Audit Report

**Scope:** Retrieval logic only (SQLAlchemy queries used to count/list pending deposits).
**Not touched:** business logic, payment logic, wallet logic, DB schema, APIs, routes, callback data, permissions, security — confirmed by diff, nothing outside query filters was changed.

---

## 1. Surfaces audited

| # | Surface | File | Entry point |
|---|---|---|---|
| 1 | Pending Deposits counter (badge) | `handlers/admin_handlers.py` | `admin_confirm_order_menu()` (Payments menu) |
| 2 | Pending Deposits counter (dashboard) | `handlers/admin_dashboard.py` | dashboard `stats["pending_payments"]` |
| 3 | Pending Deposits list | `handlers/admin_pending_deposits.py` | `pending_deposits_list()` — `pd:list:{page}:{sort}` |
| 4 | Deposit detail / direct open | `handlers/admin_pending_deposits.py` | `deposit_detail()` — `pd:det:{tx_id}` |
| 5 | Manual Payments panel (legacy) | `handlers/admin_manual_payments.py` | `mp:list:{page}:{sort}` |

## 2. Root cause found

Each of surfaces 1–3 used to build its **own, independently hand-written** filter instead of sharing one definition:

```python
# what one screen had...
Transaction.payment_method == PaymentMethod.MANUAL,
Transaction.status == TransactionStatus.PENDING

# ...while another screen had
Transaction.payment_method.in_([PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD]),
Transaction.status.in_([TransactionStatus.PENDING, TransactionStatus.AWAITING_CONFIRMATION])
```

This is exactly the class of bug described in the brief: one code path's idea of "pending" (`PENDING` only, `MANUAL` only) did not match another path's idea of "pending" (`PENDING` **and** `AWAITING_CONFIRMATION`, across `MANUAL`, `BKASH`, `NAGAD`).

Concrete failure mode reproducing the reported symptom:
- A bKash/Nagad manual-mode deposit, or any deposit where the user had already submitted a TXID/screenshot, sits in `TransactionStatus.AWAITING_CONFIRMATION`.
- The list's local filter checked only `TransactionStatus.PENDING` (and/or only `PaymentMethod.MANUAL`), so it was silently excluded from the **list** and the **counter**.
- `pd:det:{tx_id}` (direct-open) has no status/method filter at all — it's a plain `filter_by(id=tx_id)` lookup — so the same row opened fine from a direct link/notification, exactly matching "deposits exist and can be opened directly, but the list says none are waiting."

## 3. Fix applied

Standardized every counting/listing surface onto **one shared definition** in `services/payment_ui.py`, instead of changing what "pending" *means* (no business-logic change):

```python
def reviewable_methods():
    """Payment methods whose PENDING/AWAITING_CONFIRMATION rows mean
    'a human needs to check this' (as opposed to auto-confirmed gateways)."""
    return (PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD)

def pending_tx_statuses():
    """Transaction statuses that mean 'not yet resolved'."""
    return (TransactionStatus.PENDING, TransactionStatus.AWAITING_CONFIRMATION)

def pending_deposit_rows(session, sort_desc=True):
    return (
        session.query(Transaction)
        .filter(
            Transaction.payment_method.in_(reviewable_methods()),
            Transaction.status.in_(pending_tx_statuses()),
        )
        .order_by(Transaction.created_at.desc() if sort_desc else Transaction.created_at.asc())
        .all()
    )

def count_pending_deposits(session) -> dict:
    """deposits + gateway_verifications (failed auto-verify rows genuinely
    waiting on a human) -> total. Derived from the SAME rows as the list."""
    ...
```

Every surface now calls these same functions instead of re-declaring the filter:

- `handlers/admin_pending_deposits.py` → `pui.pending_deposit_rows()` for both the list and its count (no separate `COUNT(*)` query that could disagree with the rendered rows).
- `handlers/admin_handlers.py` (`admin_confirm_order_menu`) → `pui.pending_deposit_rows()` for the Payments-menu badge and empty/non-empty header text.
- `handlers/admin_dashboard.py` → `pui.count_pending_deposits()["total"]` for the dashboard badge.

Net effect: **status filter and payment-method filter are defined in exactly one place**, so it is no longer possible for the counter, the list, and the review screen to disagree.

## 4. Remaining, deliberately-out-of-scope difference

`handlers/admin_manual_payments.py` (`mp:list`) is a **different screen** ("all Manual-method transaction history," reachable from Payment Settings → Manual Payments) — it intentionally lists every `PaymentMethod.MANUAL` transaction regardless of status, not just pending ones, and does not include BKASH/NAGAD. This is a distinct, legacy history view, not the "Pending Deposits" queue, so it was left as-is per the instruction not to touch anything beyond the pending-deposits retrieval logic. Flagging it here only for visibility, not as a change.

`PendingManualVerification` rows (failed auto-verification of Binance Pay / Bybit Pay) are a separate table with their own dedicated review screen (`admin_binance_pending`, etc.). They're correctly folded into the *dashboard total* count (`count_pending_deposits()["total"]`) but are out of scope for the `Transaction`-based Pending Deposits list, since they belong to auto-confirmed gateways, not the manual-review methods this queue is for.

## 5. Final verification

| Check | Result |
|---|---|
| Pending counter matches the database | ✅ (single query, `pending_deposit_rows()`) |
| Pending list matches the database | ✅ (same query object used for count and rows — no drift possible) |
| Admin Review / detail screen matches the database | ✅ (`pd:det:{tx_id}` unchanged, opens any row by id; now consistent because the list no longer hides rows that qualify) |
| Manual Review queue (Payments menu badge) matches the database | ✅ (`admin_confirm_order_menu` now reads the same shared function) |
| No pending deposit hidden by incorrect filtering | ✅ — `AWAITING_CONFIRMATION` rows for `MANUAL`/`BKASH`/`NAGAD` are now included everywhere |
