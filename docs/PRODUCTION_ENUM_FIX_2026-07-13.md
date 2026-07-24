# Production fix: missing Postgres enum values (BYBIT_PAY and others)

## What broke

Production error:

```
psycopg2.errors.InvalidTextRepresentation: invalid input value for enum paymentmethod: "BYBIT_PAY"
```

## Root cause

Four Alembic migrations extend native Postgres enum types with new members
using `ALTER TYPE ... ADD VALUE IF NOT EXISTS`:

- `alembic/versions/20260708_product_types.py` (`producttype` enum)
- `alembic/versions/20260711_gateway_payments.py` (`paymentmethod`: BKASH, NAGAD)
- `alembic/versions/20260719_bybit_pay.py` (`paymentmethod`: BYBIT_PAY)
- `alembic/versions/20260720_txn_cancelled.py` (`transactionstatus`: CANCELLED)

All four passed the new value as a SQLAlchemy **bound parameter**
(`.bindparams(val=member)`). PostgreSQL's `ALTER TYPE ... ADD VALUE` DDL
grammar only accepts a literal there — passing it as a `$1` placeholder is
a syntax error. Every one of these migrations then swallowed **all**
exceptions unconditionally (`except Exception: pass`), so the syntax error
was silently discarded, Alembic still recorded the migration as applied,
and the new enum member was never actually added to the live database.

This is why the bug surfaced only in production: it depends on Alembic
having already run against a real PostgreSQL database. SQLite (used for
local dev/tests) has no native enum type, so nothing there ever exercised
this code path.

## What was fixed (in this codebase)

All four migrations now:
1. Interpolate the (hardcoded, non-user-input) enum member name as a
   literal string instead of a bound parameter — this is what actually
   adds the value.
2. Only silently ignore an "already exists" error; anything else is now
   logged as a warning instead of disappearing.

## Action required on your EXISTING production database

Because Alembic already marked these four revisions as applied on your
production database, **redeploying this fixed code will NOT automatically
re-run them** — Alembic only runs migrations it hasn't seen before.

Run this once against your production Postgres database (psql, or any
SQL client) to add the enum values that are still missing:

```sql
ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'BKASH';
ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'NAGAD';
ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'BYBIT_PAY';
ALTER TYPE transactionstatus ADD VALUE IF NOT EXISTS 'CANCELLED';
-- producttype members (only needed if you saw similar errors for products):
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'REDEEM_LINK';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'ACCOUNT_LOGIN';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'DOWNLOADABLE_FILE';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'AUTO_GENERATED';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'MANUAL_DELIVERY';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'PREORDER';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'SUBSCRIPTION';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'BUNDLE';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'SERVICE';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'VOUCHER';
ALTER TYPE producttype ADD VALUE IF NOT EXISTS 'EXTERNAL_DELIVERY';
```

Each statement is idempotent (`IF NOT EXISTS`) — safe to run even for
values that already exist. Run them one at a time if your client wraps
statements in an implicit transaction (`ALTER TYPE ... ADD VALUE` cannot
run inside a transaction block in PostgreSQL).

Any future new payment method / product type / transaction status you add
in code will now correctly reach the database, since the migrations that
create them are fixed.
