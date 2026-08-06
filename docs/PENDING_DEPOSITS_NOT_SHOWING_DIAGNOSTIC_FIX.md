# Pending Deposits — "Not Showing" Diagnostic Fix

## What was reported
After deploying the Back-navigation fix, the Payments menu and the Pending
Deposits list still showed **"No deposits are currently waiting for
review."** even though a deposit was believed to exist in the database.

## Why this can happen even with correct code

The Pending Deposits queue is, **by design**, scoped to only 3 payment
methods:

- `Manual` (admin-created ManualPaymentMethod rows)
- `bKash` (Manual mode)
- `Nagad` (Manual mode)

These are the only methods where a **human** has to check a submitted
TXID/screenshot. Every other payment method in the project — Crypto Wallet,
Binance Pay, Bybit Pay, Cryptomus, ZiniPay, NOWPayments, Heleket, Telegram
Stars — is confirmed automatically by its own API/webhook, and was
deliberately excluded from this queue (see the module docstring in
`handlers/admin_pending_deposits.py`).

So "No deposits are currently waiting for review" is the **correct**
message whenever:
- there genuinely are zero pending rows, **or**
- the deposit the admin has in mind was made through one of the
  auto-confirmed gateways above, and is simply out of this queue's scope —
  which looks identical to "not showing" from the admin's point of view.

There was no way to tell these two cases apart from the screen itself,
which is what made this look like a bug even when the query logic was
correct.

## Fix applied (rendering only — no business/payment/wallet logic touched)

Both places that can show "No deposits are currently waiting for review" —
the **Payments menu** header (`handlers/admin_handlers.py`,
`admin_confirm_order_menu`) and the **Pending Deposits list** empty state
(`handlers/admin_pending_deposits.py`, `_render_pending_deposits_list`) —
now run one extra **read-only** diagnostic query before choosing the empty
message:

```python
other_pending = (
    session.query(Transaction.payment_method, func.count(Transaction.id))
    .filter(
        Transaction.status.in_(pending_tx_statuses()),
        ~Transaction.payment_method.in_(reviewable_methods()),
    )
    .group_by(Transaction.payment_method)
    .all()
)
```

- If this comes back empty → the original message is shown unchanged:
  *"No deposits are currently waiting for review."*
- If it finds rows → the screen now says, e.g.:

  > No deposits are currently waiting for review under Manual / bKash / Nagad.
  >
  > ℹ️ Found pending activity outside this queue's scope:
  > • Binance Pay: 1 awaiting its own auto-confirmation

This turns a silent scope mismatch into a message that tells the admin
exactly what's actually pending and why it isn't in this particular queue —
without changing which transactions can be approved/rejected, without
touching the DB schema, APIs, routes, callback data, permissions, or any
approval/rejection logic. It is purely an extra `SELECT ... GROUP BY`
used only to pick which text to display.

## What to check next

Open **Payments → Pending Deposits** again after deploying this update:

- If it now shows the **ℹ️ scope note** with a gateway name and count, that
  confirms the deposit exists but was made through an auto-confirmed
  gateway (not Manual/bKash/Nagad) — this is expected behavior, not a bug,
  and that deposit should already have auto-confirmed via its own
  webhook/API (or shows up under Binance/Bybit's own review screen, or
  Webhook Monitor, if its auto-verification failed).
- If it still shows the plain **"No deposits..."** message with no scope
  note, then there are genuinely zero pending rows of any kind in the
  `transactions` table right now — worth re-confirming with the admin who
  reported the deposit which payment method and rough time it was made.
