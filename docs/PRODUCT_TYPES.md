# V11 — Product Types 360

This upgrade adds **10 new product types** to the existing Telegram Store
Bot, taking the total to **12**. Everything is additive; existing KEY /
FILE products and orders keep working with zero changes.

## Types

| # | Enum name           | Emoji | Delivery style                         |
|---|---------------------|-------|----------------------------------------|
| 1 | `KEY`               | 🔑    | Legacy — assign_product_keys()         |
| 2 | `REDEEM_LINK`       | 🔗    | Auto — shared `product_keys` inventory |
| 3 | `ACCOUNT_LOGIN`     | 📧    | Auto — shared `product_keys` inventory |
| 4 | `DOWNLOADABLE_FILE` | 📁    | Auto — Telegram `file_id` / URL        |
| 5 | `AUTO_GENERATED`    | 🤖    | Auto — cryptographic `secrets` module  |
| 6 | `MANUAL_DELIVERY`   | 👤    | Queued — `manual_delivery_tasks` table |
| 7 | `PREORDER`          | ⏳    | Queued — `preorders` table             |
| 8 | `SUBSCRIPTION`      | ♻️    | Auto — activates row in `subscriptions`|
| 9 | `BUNDLE`            | 📦    | Auto — atomic multi-child reservation  |
|10 | `SERVICE`           | 🛠️    | Queued — `service_orders` table        |
|11 | `VOUCHER`           | 🎟️    | Auto — shared `product_keys` inventory |
|12 | `EXTERNAL_DELIVERY` | 🌐    | Auto — HTTP call + idempotent log      |
|   | `FILE` (legacy)     | 📁    | Legacy — `download_link` field         |

## Architecture

```
handlers/payment_handlers.py
    ↓  (payment confirmed, order row exists)
services/delivery_service.dispatch(order_id)
    ├─ handled=False  → legacy KEY / FILE branch runs unchanged
    └─ handled=True   → per-type deliverer already wrote assets
```

`DeliveryResult` has:

- `handled`  — did the dispatcher take over?
- `success`  — real assets delivered to user?
- `queued`   — awaits admin fulfilment
- `user_message`, `admin_notice`, `assets`, `error`
- `idempotent_replay` — a second call for the same order

### Idempotency

Every deliverer checks for prior delivery before doing work:

| Type              | Idempotency check                                                    |
|-------------------|----------------------------------------------------------------------|
| KEY-like          | `OrderItem.delivered_asset` populated → return same values           |
| DOWNLOADABLE_FILE | `delivered_asset` populated (unless product.reusable=True)           |
| AUTO_GENERATED    | `delivered_asset` populated                                          |
| MANUAL_DELIVERY   | `ManualDeliveryTask` row exists for order                            |
| PREORDER          | `Preorder` row exists                                                |
| SUBSCRIPTION      | `Subscription` row exists                                            |
| BUNDLE            | `delivered_asset` populated                                          |
| SERVICE           | `ServiceOrder` row exists                                            |
| EXTERNAL_DELIVERY | `ExternalDeliveryLog` row with idempotency_key `order:<id>` exists   |

### Concurrency

- KEY-like consumption uses `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL.
- Bundle reservation is two-pass: first a check on every child's available
  count, then atomic consumption — either the whole bundle succeeds or nothing
  is consumed.
- AUTO_GENERATED uses `secrets` module (cryptographic) with UNIQUE constraint
  in `generated_values.value` — retries on collision up to 5 times.

## Database changes (migration `20260708_pt360`)

**Product columns added** (all nullable, safe on production):

- `type_config` (TEXT, JSON blob)
- `delivery_note` (TEXT)
- `warranty_info` (TEXT)
- `min_quantity`, `max_quantity` (INT)
- `bulk_purchase_enabled` (BOOL, default true)
- `telegram_file_id`, `telegram_file_type` (VARCHAR)
- `reusable` (BOOL, default false)

**Enum extension** — `ALTER TYPE producttype ADD VALUE IF NOT EXISTS ...`
for 10 new members. On SQLite no-op (enum type doesn't exist there).

**8 new tables**: `subscription_plans`, `subscriptions`, `bundle_items`,
`preorders`, `service_orders`, `manual_delivery_tasks`,
`external_integrations`, `generated_values`, `external_delivery_logs`.

## Admin UI

Product creation flow (`handlers/admin_conversations.py`) now shows a
paginated 12-option picker:

```
Page 1: 🔑 Software Key · 🔗 Redeem Link · 📧 Account · 📁 File · 🤖 Auto · 👤 Manual
Page 2: ⏳ Pre-Order · ♻️ Subscription · 📦 Bundle · 🛠️ Service · 🎟️ Voucher · 🌐 External
Nav:    ⬅️ Previous  ➡️ Next  ❌ Cancel
```

Callback data format: `ptype:<ENUM_NAME>` (stable identifier — never uses
emoji or visible text). Legacy `type_key` / `type_file` callbacks still
accepted so mid-flight conversations from older sessions don't break.

After type selection the flow:

- **KEY / REDEEM_LINK / ACCOUNT_LOGIN / VOUCHER** — reuses the existing
  paste-or-upload-txt inventory step (with duplicate rejection).
- **FILE / DOWNLOADABLE_FILE** — asks for download link.
- Everything else — creates the product and prompts the admin to finish
  per-type configuration from the admin control center.

## Security

- `EXTERNAL_DELIVERY` never stores raw credentials in `products` or
  `external_integrations` — only the *name* of the env var holding the
  secret (`credential_env_name`).
- Loopback / `.local` hosts blocked before the outbound HTTP request.
- Response body capped at 64 KB.
- Idempotency key prevents double-charge on retry.

## What is fully wired vs. queued for future work

**Fully auto-delivering:** REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER,
DOWNLOADABLE_FILE, AUTO_GENERATED, SUBSCRIPTION (single default plan),
BUNDLE, EXTERNAL_DELIVERY.

**Fully queued to admin:** MANUAL_DELIVERY, PREORDER, SERVICE.

**Follow-up work not shipped in this turn:**

- Dedicated admin sub-menus to edit `SubscriptionPlan` rows,
  `BundleItem` composition, `ExternalIntegration` records, and per-type
  `type_config` (currently editable via SQL / Restock flow).
- Manual-delivery / pre-order / service admin action buttons
  (send-text / send-file / mark-complete) — the queue rows exist and
  admins are notified, but a purpose-built inline UI to complete them is
  the next iteration.
- Retry worker for failed `EXTERNAL_DELIVERY` jobs — current
  implementation is single-attempt with a persisted failure log so an
  admin can inspect and retry manually.

## Files changed

**New:**

- `services/delivery_service.py` — dispatcher + 11 per-type deliverers
- `handlers/admin_product_types.py` — paginated 12-option picker
- `alembic/versions/20260708_product_types.py` — migration

**Modified:**

- `database/models.py` — `ProductType` extended, 9 new Product columns,
  9 new tables appended
- `database/__init__.py` — export new models
- `handlers/admin_conversations.py` — 12-option picker + per-type routing
  + duplicate rejection on inventory upload
- `handlers/payment_handlers.py` — invokes `delivery_service.dispatch()`
  before legacy KEY/FILE branch
- `bot.py` — register `^ptype:` and `^ptype_page:` callback patterns

## Validation

- `python -m compileall` on every changed file — clean.
- SQLite in-memory smoke test executed the full flow for
  `AUTO_GENERATED`, `REDEEM_LINK`, `MANUAL_DELIVERY`, `PREORDER`,
  `SUBSCRIPTION`, and `BUNDLE` including idempotent replay and atomic
  bundle failure — all pass.
