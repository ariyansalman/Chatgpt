# V8 — Premium Core Upgrade

Adds four connected systems on top of the existing store:

1. **Product Variants** (`product_variants` table + `variants` relationship)
2. **Persistent Cart** (existing `cart` table extended with `variant_id`, `updated_at`)
3. **Stock Reservations** (`stock_reservations` table + `services/inventory.py`)
4. **Advanced Order Lifecycle** (`order_status_history` + `services/order_lifecycle.py`
   + `orders.lifecycle_status/payment_status/delivery_status`)

All changes are **additive and non-destructive**. Existing rows, code paths,
and admin flows keep working unchanged.

## Migration

```bash
alembic upgrade head        # applies 20260705_pc (premium_core)
```

Postgres and SQLite are both supported (all changes go through
`op.batch_alter_table`). The migration is idempotent — safe to re-run.

## New bot_config keys

| Key                                    | Type | Default | Category  |
|----------------------------------------|------|---------|-----------|
| `inventory_reservation_ttl_minutes`    | int  | 15      | inventory |

Edit from Admin Panel → Bot Config → 📊 Inventory.

## Admin Panel additions

`Admin → Products → 🎛️ Manage Variants`

- Pick product → variant list (per-product)
- Add / edit (name, price, sale price, stock) / toggle / delete
- Deletion is refused while stock or keys still reference the variant.

## User flow additions

- Every product detail can offer **🛒 Add to cart** (`cart_add_<pid>`).
  Products with active variants show a variant picker first.
- Main menu can offer **🛒 Cart** (`cart` callback) — free to wire in
  wherever the project displays user shortcuts.

## Programmatic API

```python
from services import inventory

r = inventory.reserve(user_id=42, product_id=7, quantity=2, variant_id=None)
# ... user pays ...
delivered_keys = inventory.consume(r.id, order_id=99)
# or, on cancel:
inventory.release(r.id)

from services import order_lifecycle
from database import OrderLifecycleStatus
order_lifecycle.transition(order_id, OrderLifecycleStatus.PAID,
                           actor_type="user", reason="Wallet debited")
timeline = order_lifecycle.render_timeline(order_id)
```

## Integration hook points (for the project owner)

The scaffolding above is complete and correct. Wiring these calls into the
existing checkout/payment paths is the last mile — kept for manual review so
we do not accidentally rewrite the wallet/coupon/loyalty flows:

1. **Right before "wallet debit / open payment"** in `payment_handlers.py`,
   call `inventory.reserve(...)` and store the returned `reservation.id`
   on the pending order.
2. **On successful payment** (both manual approve + Telegram Card success),
   call `inventory.consume(reservation_id, order.id)` and use the returned
   keys as the delivered asset.
3. **On payment cancel/rejection/expiry**, call `inventory.release(reservation_id)`.
4. Anywhere `Order.status` is set, also call
   `order_lifecycle.transition(order.id, OrderLifecycleStatus.<X>, ...)`
   with the matching new status so the timeline is populated.

A background job (`inventory.expire_reservations_job`, wired in `bot.py`
every 60 s) already frees any reservation not consumed within the TTL.

## Multi-item cart checkout

`handlers/cart_handlers.py::cart_checkout` currently shows a friendly
"ready to check out" screen. To finish it, iterate cart rows and either:

- call `inventory.reserve` per row and open the existing payment
  method chooser, then `inventory.consume` per row on success, **or**
- create a single "cart order" whose `OrderItem` rows carry `variant_id`.

Both approaches reuse the existing wallet + coupon + loyalty modules.

## Compatibility guarantees

- Legacy `OrderStatus` enum is **unchanged**. Extended states live on the
  new `Order.lifecycle_status` column.
- Products with no variants behave exactly as before (`product.variants`
  is an empty list, so all branches fall back to product-level price/stock).
- Existing `ProductKey`, `Cart`, `OrderItem` rows all have `NULL` for the
  new columns; every query treats `NULL` as "no variant".
- `services/inventory.py` uses `SELECT … FOR UPDATE SKIP LOCKED` on
  PostgreSQL for concurrency-safe key selection; SQLite falls back to
  normal SELECT (single-writer semantics).