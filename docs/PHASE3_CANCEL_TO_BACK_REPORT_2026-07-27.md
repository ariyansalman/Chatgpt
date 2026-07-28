# Phase 3 — Store & Shopping Navigation Cleanup — Report (2026-07-27)

## Guide used
`CANCEL_TO_BACK_AUDIT_v2_FINAL.csv` (134 rows, user-supplied), cross-checked
against a fresh `grep` of every literal `❌ Cancel` in the codebase (155
matches) and against the actual handler code around each shopping-flow hit.

## Result: 0 conversions performed

Every `❌ Cancel` button that falls inside the Phase 3 scope (Products,
Product Details, Categories, Product Browser, Search Products, Filters,
Quantity Selection, Custom Quantity, Purchase Summary, Apply Coupon, My
Orders, Purchased Keys, Order Details) is classified `KEEP_CANCEL` in the
supplied audit, and independent code review confirms each one:

| File | Line | Function | Reason kept as Cancel |
|---|---:|---|---|
| `services/quantity_presets.py` | 157 | `build_keyboard` | Screen already has a separate `⬅ Back to Product` button in the same row; `❌ Cancel` is wired to `cancel_purchase`, an abort action, not a second Back control. |
| `utils/keyboards.py` | 511 | `create_quantity_keyboard` | Same pattern as above — paired with its own `⬅ Back to Product` button; Cancel aborts via `cancel_purchase`. |
| `utils/keyboards.py` | 519 | `create_cancel_keyboard` | Generic shared helper; its ~40 call sites are almost entirely `payment_handlers.py` (deposit/TXID/gateway flows), `admin_conversations.py`, and `dispute_handlers.py` — out of shopping scope. Not safe to relabel globally. |
| `handlers/coupon_handlers.py` | 119 | `apply_coupon_start` | Coupon-code text-entry prompt; `❌ Cancel` → `cancel_purchase` ends the conversation (`ConversationHandler.END`). No intermediate "previous screen" exists to go back to inside this state. |
| `handlers/coupon_handlers.py` | 144 | `apply_coupon_input` | Same flow, shown again on invalid coupon input; same `cancel_purchase` abort semantics. |
| `handlers/gift_purchase_handlers.py` | 89, 152, 220 | `gift_start`, `gift_recipient_input`, `_show_gift_confirmation` | Linear text-entry conversation (recipient ID → message → confirm); `gp:cancel` aborts the whole gift flow rather than stepping back one field. No previous-screen destination to return to. |

## Why none were converted despite being CONVERT_TO_BACK candidates in the
## *first* (Phase-2-zip) audit report

The Phase-2 ZIP's `docs/CANCEL_TO_BACK_AUDIT_REPORT_2026-07-27.md` flagged
`payment_handlers.py` buttons for review but only worked from a heuristic
text scan and explicitly called itself "a first-pass heuristic, not a final
verdict." The `v2_FINAL` CSV you supplied appears to be the result of that
promised per-button follow-up: every shopping-flow Cancel button's actual
`callback_data` was checked, and all of them route to abort/terminal
callbacks (`cancel_purchase`, `gp:cancel`, `cancel`) rather than to a
previous-screen destination.

Relabeling any of these to `⬅️ Back` without also rewiring their
`callback_data` to a real previous-screen handler would produce a button
that *says* Back but *behaves* like Cancel (it would still abort the
purchase/flow instead of returning to it with state intact) — a direct
violation of the Back Button Rules. Rewiring `callback_data` is explicitly
out of scope for Phase 3 ("Do NOT touch: ... Callback names").

## My Orders / Purchased Keys / Order Details
No `❌ Cancel` buttons exist in `handlers/user_order_timeline.py` or the
order-history sections of `handlers/user_handlers.py`. These screens
already use `⬅️ Back` (confirmed via the Phase-2 back-button inventory,
destination `order_history`) — no navigation regression, nothing to
convert.

## Testing
- `python3 -m py_compile` run on all in-scope shopping files
  (`coupon_handlers.py`, `gift_purchase_handlers.py`,
  `quantity_presets.py`, `keyboards.py`, `user_handlers.py`,
  `user_order_timeline.py`, `search_handlers.py`, `cart_handlers.py`,
  `variant_handlers.py`) — all pass, no syntax errors.
- No files were modified, so no regression risk to callback routing,
  quantity/product/coupon state preservation, search, filters, or
  pagination.

## Deliverable summary
- Files modified: **0**
- Cancel → Back conversions: **0**
- Buttons reviewed and intentionally kept as Cancel: **8** (table above)
- Navigation issues found: **none**
- `py_compile`: **passed**
