# Admin Order Notification Redesign — 2026-07-27

UI/UX-only redesign of admin-facing order/delivery notifications. No order,
payment, wallet, referral, coupon, inventory, product, delivery, database,
API, route, callback, auth, or permission logic was changed.

## What changed

### `utils/notify_format.py`
Added `render_order_notification()` and `dhaka_time_str()`, a dedicated
layout builder for order/delivery notifications only. The existing generic
`render()` / `utc_now_str()` used by every other admin notification
(deposits, disputes, SLA alerts, new-user, etc.) is untouched.

`render_order_notification()` produces the premium layout: bold titles,
`<code>` only for IDs, three divided sections (identity / order details /
coupon+referral), a single Asia/Dhaka timestamp (`BST`, never UTC), and
automatic omission of any field with no value — so callers never have to
special-case "no coupon" or "no referral" themselves.

### `handlers/payment_handlers.py`
- **"Order Completed" notification** (instant wallet-purchase delivery):
  rebuilt on `render_order_notification()`. Now also shows Unit Price,
  Payment Method, and — when a coupon was used — the coupon code and
  discount. Two small snapshot variables (`_notif_unit_price`,
  `_notif_coupon_code`, `_notif_coupon_label`) were added at the point the
  product/coupon are already loaded, purely so the notification has stable
  values to read later; no pricing/coupon computation changed.
- **Failed-delivery admin alert**: rebuilt on `render_order_notification()`
  with `status="failed"`. This also fixes a pre-existing issue where the
  old plain-text message exposed the raw internal `product_id` — it now
  shows the product name and the public `ORD-...` order id instead, per
  the "never expose internal database IDs" rule.

### `services/order_lifecycle.py`
The fallback "Order Completed" notification (fires for cart-checkout
completions, where the richer context above isn't available) previously
only showed Order ID + Amount. It's now rebuilt on
`render_order_notification()` and additionally queries the order's
line items and customer so it can show Product, Quantity, and Unit Price
too — read-only queries against data already scoped to this function, no
change to what triggers the notification or when.

## Not changed (no live code path exists yet)

The "New Order" (pre-delivery), "Order Pending", and "Manual Delivery"
notification *events* are registered in `services/notifications.py`'s
catalog as `live=False` — the codebase itself notes they're "not yet wired
to a live event." `render_order_notification()` already supports their
statuses (`new_order`, `pending`, `manual`) so whenever those code paths
are added, they can call the same builder and automatically match this
layout — no separate redesign will be needed.

## Referral Commission field

`render_order_notification()` supports a `referral_commission` field, but
the actual commission is computed asynchronously by
`handlers/referral_handlers.process_referral_reward()` *after* the order
notification is already sent (fire-and-forget, by design, so it never
blocks the purchase flow). That value isn't available synchronously at
notification-build time, so it's correctly omitted today — this matches
prior behavior and required no logic changes. Wiring the commission amount
into this notification would require restructuring the referral flow,
which is out of scope for a UI/UX-only change.
