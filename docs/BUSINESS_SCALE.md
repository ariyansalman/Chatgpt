# Business Scale & Operations (V10)

Additive layer on top of the existing bot. Non-destructive: no drops, no data
resets. All new columns are nullable; all new tables use `IF NOT EXISTS` guards.

## What was added

### New tables (migration `20260707_bs`)
- `suppliers`
- `inventory_batches`
- `inventory_issues`
- `reseller_tiers`
- `user_reseller`
- `delivery_jobs` (Postgres partial unique index: one active job per order)
- `backup_records`
- `integrity_scans` / `integrity_scan_results`

### New nullable columns on existing tables
- `product_keys.batch_id`, `product_keys.cost_per_unit_snapshot`
- `order_items.base_price`, `order_items.unit_cost_snapshot`,
  `order_items.total_cost_snapshot`, `order_items.reseller_tier_id`,
  `order_items.pricing_meta`

### Enum strategy
Every V10 enum-like field is a **VARCHAR validated in Python** (e.g. `DeliveryJob.status`,
`InventoryIssue.issue_type`, `BackupRecord.status`, `IntegrityScanResult.severity`).
This deliberately avoids the PostgreSQL native-enum migration issues previously
seen with `OrderStatus`.

### New services
- `services/pricing.py` — single source of truth for price quotes. Order:
  base → sale → bulk → reseller. Coupon is applied at order level (unchanged).
- `services/delivery_queue.py` — enqueue, assign inventory idempotently, mark
  delivered / failed with backoff (`[1,5,15,60,240]` minutes). Uses
  `SELECT ... FOR UPDATE SKIP LOCKED` on Postgres.
- `services/backup.py` — pg_dump wrapper with retention pruning that never
  deletes the newest successful backup.
- `services/integrity.py` — 8 read-only checks; results are persisted for
  admin review.

### New handlers (all under the `acc:` namespace)
- `admin_suppliers.py` — list/view/toggle/add
- `admin_batches.py` — list/view/add
- `admin_profit.py` — 24h / 7d / 30d revenue • COGS • profit • top 5 products
- `admin_quality.py` — issue counts + recent list
- `admin_resellers.py` — tier CRUD + user assignment
- `admin_delivery_queue.py` — status tabs, view, manual retry, cancel
- `admin_backups.py` — manual run / retention prune / toggle scheduled
- `admin_integrity.py` — run scan / view latest results

### New BotConfig keys
`reseller_system_enabled`, `delivery_max_attempts`, `backup_enabled`,
`backup_interval_hours`, `backup_retention_count`,
`integrity_scan_interval_hours`.

## Pricing rules (documented)

```
base       = variant.price if variant else product.price
after_sale = min(base, product.discount_price) if product.discount_price > 0 else base
after_bulk = product.bulk_price if quantity >= product.bulk_price_qty else after_sale
effective  = after_bulk * (1 - reseller_tier.discount_pct/100)
subtotal   = effective * quantity
```
Coupon math continues to run at the order/subtotal level in the existing
coupon handler — no double discount.

## Profit rules (documented)

Revenue counts **only** `OrderItem` rows whose `Order.lifecycle_status` is
`DELIVERED` or `COMPLETED`. Wallet top-ups, rejected/cancelled orders, and
in-flight orders are excluded. COGS uses `OrderItem.total_cost_snapshot`
when present (recorded at delivery time), else falls back to summing
`ProductKey.cost_per_unit_snapshot` for keys assigned to that order.

## Delivery idempotency

1. `enqueue(order_id)` — refuses to create a second active job for the same
   order (Postgres partial unique index guarantees this; the code re-uses the
   existing active job if the race is lost).
2. `assign_inventory(job_id)` — if `ProductKey.order_id == job.order_id`
   already exists, reuse those keys and return `(False, existing_ids)`;
   otherwise allocate under `FOR UPDATE SKIP LOCKED`.
3. Telegram delivery happens **outside** the DB transaction.
4. Failures classify into RETRYABLE / permanent. Retries use fixed backoff
   `[1, 5, 15, 60, 240]` minutes, capped by `max_attempts`.

The background sweep in `bot.py` promotes due `RETRY_SCHEDULED` jobs back to
`PENDING` so a worker (or a follow-up wiring pass into `payment_handlers`)
can execute them.

## Backups

Local pg_dump only. Set `BACKUP_DIR` (default `/var/backups/telegram-store`)
and ensure `pg_dump` is on `PATH`. Cloud upload is intentionally not
implemented — arrange offsite copies out-of-band. Restore is a manual VPS
operation:

```
gunzip -c $BACKUP_DIR/pgdump_YYYYMMDD_HHMMSS.sql.gz | psql "$DATABASE_URL"
```

## Integrity checks shipped

- paid_not_delivered (WARNING)
- duplicate_key_values_assigned (CRITICAL)
- sold_without_order (WARNING)
- expired_active_reservations (WARNING)
- duplicate_transaction_txids (CRITICAL)
- orphan_delivery_jobs (WARNING)
- delivery_jobs_processing (INFO)
- batch_quantity_drift (WARNING)

Scans are read-only. Repairs remain manual.

## What is NOT wired into the live checkout in this drop

- `services/pricing.quote()` is available and safe to call, but the existing
  checkout continues to compute prices via its current path. Swapping over
  is a follow-up mechanical change and is documented above so it can be done
  incrementally without breaking coupon/loyalty logic.
- The delivery worker that actually calls Telegram lives in
  `handlers/payment_handlers.py`. `services/delivery_queue.py` provides the
  transactional inventory-assignment primitive it should use — a follow-up
  turn should call `enqueue → assign_inventory → mark_delivered/mark_failed`
  around the existing delivery path.

## Commands

```bash
python -m compileall -q .
alembic upgrade head
python bot.py            # polling
```
