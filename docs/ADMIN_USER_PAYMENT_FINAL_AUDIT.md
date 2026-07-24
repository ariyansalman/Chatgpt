# Admin User & Payment Management — Final Audit
**Date:** 2026-07-04  
**Scope:** Full Users panel + Manual Payments admin flow implementation

---

## 1. Users Menu Callback Map

| Element | Value |
|---------|-------|
| Trigger callback | `admin_users` |
| Handler | `admin_handlers.admin_users_callback` → delegates to `admin_users.users_menu` |
| Button: Users List | `usr:list:0:desc` → `admin_users.users_list` |
| Button: User Search | `usr:search` → `admin_users.build_user_search_conv()` entry |
| Button: Manual Payments | `mp:list:0:desc` → `admin_manual_payments.payments_list` |
| Button: Return | `admin_menu` → `admin_handlers.admin_menu_callback` |

---

## 2. Users List — Query & Pagination Map

| Property | Implementation |
|---|---|
| Handler | `admin_users.users_list` |
| Callback pattern | `^usr:list:\d+:(asc|desc)$` |
| DB query | `session.query(User).order_by(col).offset(page * 8).limit(8)` |
| Total count | `session.query(func.count(User.id)).scalar()` |
| Memory safety | DB-level LIMIT/OFFSET — **never loads full table into memory** |
| Sort: Latest | `User.created_at.desc()` (default) |
| Sort: Oldest | `User.created_at.asc()` |
| Sort toggle | `usr:list:{page}:{next_sort}` — alternates `asc` ↔ `desc` |
| Button format | `@username \| {telegram_id}` or `User {tg_id} \| {tg_id}` — never "None" |
| Items per page | 8 |
| Previous button | `usr:list:{page-1}:{sort}` (shown when `page > 0`) |
| Next button | `usr:list:{page+1}:{sort}` (shown when more pages) |

---

## 3. User Search Flow

| Step | Detail |
|---|---|
| Entry | `usr:search` callback → ConversationHandler |
| Handler | `admin_users.build_user_search_conv()` |
| Conversation state | `WAITING_USR_SEARCH = 10` |
| Search by Telegram ID | `User.telegram_id == int(raw)` |
| Search by `@username` | `User.username.ilike(raw.lstrip("@"))` (case-insensitive) |
| Search by username (no @) | `User.username.ilike(raw)` |
| Not found | Re-prompt in same message; stays in `WAITING_USR_SEARCH` |
| Cancel | `/cancel` command → `user_search_cancel` → `ConversationHandler.END` |
| Cleanup | `_clr_search(ctx)` removes `_search_msg_id`, `_search_chat_id` |

---

## 4. User Information Renderer

| Field | Source |
|---|---|
| 🆔 ID | `user.telegram_id` |
| 👤 Name | `@{user.username}` if set; `User {telegram_id}` if None |
| 💰 Balance | `format_price(float(user.wallet_balance or 0.0))` |
| 📅 Reg Date | `user.created_at.strftime("%Y-%m-%d %H:%M")` |
| HTML safety | `html.escape()` applied to all user-supplied text |
| "None" safeguard | `_name()` / `_name_esc()` helpers never emit the string "None" |

Keyboard buttons:
- `💰 Change Balance` → `usr:bal:{user.id}`
- `🔴 Ban User` or `🟢 Unban User` (conditional on `user.is_banned`)
- `🧾 User Purchase History` → `usr:ord:{user.id}:0`
- `👔 Position` → `usr:pos:{user.id}`
- `↩️ Return` → `usr:list:0:desc`

---

## 5. Balance Mutation Service Call Sites

| Action | Delta | Ledger Entry | Row Lock |
|---|---|---|---|
| Set Balance | `new_amount - current_balance` | ✅ WalletLedger | ✅ `with_for_update()` (PostgreSQL) |
| Add Balance | `+amount` | ✅ WalletLedger | ✅ `with_for_update()` |
| Deduct Balance | `-amount` | ✅ WalletLedger | ✅ `with_for_update()` |

WalletLedger row fields recorded:
- `user_id`, `delta`, `balance_after`, `reason` (`admin SET/ADD/DED`)
- `actor_type="admin"`, `actor_id={admin_telegram_id}`
- `ref_type="admin_adjust"`, `ref_id={user_id}`

AdminAuditLog entry: `log_admin_action(tg_id, "wallet.{action}", target_type="user", ...)`

---

## 6. Balance Idempotency / Replay Protection

| Mechanism | Detail |
|---|---|
| Confirmation token | UUID4 16-char token generated when confirmation screen shown |
| Token storage | `context.user_data["_bal_idem"]` |
| Callback includes token | `usr:bal:cfm:{user_id}:{token}` |
| Replay guard | Token compared in `balance_confirm`; mismatch → show_alert, no mutation |
| Token consumed | `context.user_data["_bal_idem"] = None` before any DB write |
| Double-click | Second press sees stored token already None → "Already processed" |
| Row locking | `User.with_for_update()` prevents concurrent race conditions |
| Fail closed | Exceptions in DB block are caught; explicit error message; no silent failures |

---

## 7. Ban/Unban Enforcement Map

| Step | Handler | Behaviour |
|---|---|---|
| Click 🔴 Ban | `usr:ban:{user_id}` → `ban_screen` | Shows confirmation with user name + ID |
| Confirm ban | `usr:ban:cfm:{user_id}` → `ban_execute` | Sets `user.is_banned = True`, commits, calls `clear_ban_cache(tg_id)` |
| Effect | `is_banned_check` in `utils/helpers.py` | Cache cleared; next access check queries DB fresh |
| Click 🟢 Unban | `usr:ubn:{user_id}` → `unban_screen` | Shows confirmation |
| Confirm unban | `usr:ubn:cfm:{user_id}` → `unban_execute` | Sets `user.is_banned = False`, commits, clears cache |
| Audit log | `log_admin_action(tg_id, "user.ban"/"user.unban", ...)` | Recorded in AdminAuditLog |
| Access enforcement | `is_banned()` in `utils/helpers.py` | 30-second cache; DB fallback on miss |

---

## 8. Purchase History → Existing Order Detail Integration

| Step | Implementation |
|---|---|
| Handler | `admin_users.purchase_history` |
| Callback | `usr:ord:{user_id}:{page}` |
| Query | `session.query(Order).filter_by(user_id=user.id).order_by(created_at.desc()).offset(page*8).limit(8)` |
| No new table | Uses existing `Order` model |
| Button format | `{icon} #{order_id} \| {product_name} \| {status}` |
| Order detail link | `callback_data=f"view_order_{order_id}"` → routes to **existing** `admin_order_detail_callback` |
| No duplication | Zero new order-detail rendering code |

---

## 9. Position Role Architecture

| Field | Detail |
|---|---|
| Handler | `admin_users.position_view` |
| Callback | `usr:pos:{user_id}` |
| Role source | `user.telegram_id == settings.ADMIN_TELEGRAM_ID` |
| Roles | `ADMIN` (matches ADMIN_TELEGRAM_ID) or `USER` |
| Promotion path | Not supported in current codebase (single admin via env var); UI explains this |
| No invented roles | Only roles that exist in the repository architecture are shown |

---

## 10. Manual Payments Query & Pagination Map

| Property | Implementation |
|---|---|
| Handler | `admin_manual_payments.payments_list` |
| Callback | `^mp:list:\d+:(asc|desc)$` |
| Filter | `Transaction.payment_method == PaymentMethod.MANUAL` |
| Sort: Freshest | `Transaction.created_at.desc()` (default) |
| Sort: Oldest | `Transaction.created_at.asc()` |
| Count query | `session.query(func.count(Transaction.id)).filter(...)` |
| Pagination | DB LIMIT/OFFSET, 8 per page |
| Button format | `{status_icon} @{username} \| {amount:.2f}` |
| Status icons | `⏳` PENDING/AWAITING, `✅` COMPLETED, `❌` REJECTED |
| Fallback | If `amount is None` → shows "Pending" (literal never used when valid amount exists) |

---

## 11. Payment Approval Transaction Map

```
mp:cfm_ok:{tx_id}
  │
  ├─ 1. is_admin() check — fail closed on denial
  ├─ 2. idempotency.claim("manual_approve", f"tx:{tx_id}") [PaymentIdempotency table]
  │       └─ if already claimed → show "already processed" — no further action
  │       └─ if claim raises   → show error — no credit (fail closed)
  ├─ 3. session.query(Transaction).filter(status IN [PENDING, AWAITING]).update(
  │       status=COMPLETED, completed_at=now(), admin_note=f"approved by {admin_id}"
  │       ) — atomic conditional update (flipped == 0 → rollback, show error)
  ├─ 4. User.with_for_update() — row-level lock prevents concurrent race
  ├─ 5. user.wallet_balance += tx.amount
  ├─ 6. WalletLedger row inserted in SAME session/transaction
  ├─ 7. session.commit() — atomic
  ├─ 8. log_admin_action("manual_payment.approve", ...)
  ├─ 9. Notify user (non-transactional; log failure only — never re-credit on failure)
  └─ 10. Refresh payment detail view
```

---

## 12. Payment Rejection Map

```
mp:rej_ok:{tx_id}
  │
  ├─ 1. is_admin() check
  ├─ 2. session.query(Transaction).filter(status IN [PENDING, AWAITING]).update(
  │       status=REJECTED, admin_note=f"rejected by {admin_id}"
  │       ) — atomic conditional (flipped == 0 → "already processed")
  ├─ 3. session.commit()
  ├─ 4. Confirmed payments are NEVER rejectable (status filter rejects them)
  ├─ 5. log_admin_action("manual_payment.reject", ...)
  └─ 6. Notify user (non-transactional; log failure only)
```

---

## 13. PaymentIdempotency Call Sites

| Scenario | Source | Idempotency Key |
|---|---|---|
| Manual payment approval (new panel) | `admin_manual_payments.payment_confirm_execute` | `("manual_approve", f"tx:{tx_id}")` |
| Manual payment approval (legacy inline) | `payment_handlers.admin_manual_approve` | `("manual_approve", f"tx:{tx_id}")` |
| Successful payment (Telegram Pay) | `payment_handlers.successful_payment_callback` | (existing implementation) |

Both the new panel and the legacy handler use the **same** idempotency key format, so a payment approved via one path is automatically blocked by the other.

---

## 14. Edit Debitable State Map

```
ConversationHandler: build_edit_debitable_conv()
  Entry point: ^mp:edit:\d+$  →  edit_debitable_start
    │  State: WAITING_EDIT_AMOUNT = 20
    │
    ├─ Check: tx.status in PENDING_STATUSES (fail if not)
    ├─ Store: _edit_tx_id, _edit_prev in context.user_data
    ├─ Ask: "Enter new amount for this payment:"
    │
    └─ MessageHandler(TEXT & ~COMMAND) → edit_debitable_receive
         ├─ Validate: Decimal parse, > 0
         ├─ Generate: idem_tok = uuid4()[:16]
         ├─ Store: _edit_new, _edit_idem in context.user_data
         ├─ Show: confirmation screen with mp:edit_cfm:{tx_id}:{token}
         └─ return ConversationHandler.END  ← conversation exits here

Confirmation callback (NOT inside conversation):
  mp:edit_cfm:{tx_id}:{token}  →  edit_debitable_confirm
    ├─ Verify token matches stored (double-click guard)
    ├─ Consume token (set to None)
    ├─ Re-validate tx.status in PENDING_STATUSES
    ├─ tx.amount = float(new_amount)
    ├─ session.commit()
    ├─ log_admin_action("manual_payment.edit_amount", ...)
    ├─ _clr_edit(context)
    └─ Refresh payment detail view
```

Field edited: `Transaction.amount` — the amount credited on approval.

---

## 15. ConversationHandler Cleanup Map

| Handler | State Key(s) Cleared | When |
|---|---|---|
| `balance_action_start` | `_bal_action, _bal_user_id, _bal_amount, _bal_curr, _bal_idem` | On entry (via `_clr_bal`) |
| `balance_amount_receive` | (sets keys) | After `/cancel` via `balance_cancel` |
| `balance_confirm` | All `_bal_*` | After success or error |
| `balance_cancel` | All `_bal_*` | On `/cancel` |
| `user_search_start` | `_search_msg_id, _search_chat_id` | On entry (via `_clr_search`) |
| `user_search_receive` | `_search_msg_id, _search_chat_id` | After successful search |
| `user_search_cancel` | `_search_msg_id, _search_chat_id` | On `/cancel` |
| `edit_debitable_start` | `_edit_tx_id, _edit_prev, _edit_new, _edit_idem` | On entry (via `_clr_edit`) |
| `edit_debitable_confirm` | All `_edit_*` | After success or error |
| `edit_debitable_cancel` | All `_edit_*` | On `/cancel` |

---

## 16. Stale-State Bug Root Cause

**Root cause (original code):** The legacy `create_product_conv` fallbacks used a broad  
`CallbackQueryHandler(cancel_product_creation)` **without a pattern**, which acted as a catch-all.  
Any unrelated callback query during the product-creation conversation (e.g., navigating to Users  
or Payments) triggered `cancel_product_creation`, resetting state and producing a stale  
"Enter new amount for this payment:" prompt on the next open.

**Fix applied (previous session):** Replaced the broad fallback with:  
`CallbackQueryHandler(cancel_product_creation, pattern="^cancel_product$")`

**Additional safeguard (this session):**  
- All new ConversationHandlers (`build_user_search_conv`, `build_balance_conv`,  
  `build_edit_debitable_conv`) use only **explicit-pattern entry points** and **command-only  
  fallbacks** (`/cancel`). No broad fallback CallbackQueryHandler.  
- Each handler clears its `user_data` keys via `_clr_*()` helpers before setting new values  
  (`allow_reentry=True` + explicit clear = no leaked state between sessions).  
- `ConversationHandler.END` is returned immediately on authorization failure, rather than  
  silently doing nothing (prevents the state machine from stalling in an invisible state).

---

## 17. compileall Result

```
python -m compileall . (all files in /tmp/bot_src)
→ No errors. Exit 0.
```

Files verified individually:
- `handlers/admin_users.py` ✅
- `handlers/admin_manual_payments.py` ✅
- `handlers/admin_handlers.py` ✅ (admin_users_callback delegated)
- `utils/keyboards.py` ✅
- `bot.py` ✅

---

## 18. pytest Result

`pytest` module not available in this environment (no sqlalchemy in test env Python path).  
Unit logic tests were executed inline:

| Test | Result |
|---|---|
| `_name()` never emits literal "None" | ✅ PASS |
| All 22 callback patterns ≤ 64 bytes | ✅ PASS |
| Balance delta calculation (set/add/ded) | ✅ PASS |
| Negative balance guard | ✅ PASS |
| DB pagination offset formula | ✅ PASS |
| Idempotency token consumed after first use | ✅ PASS |
| Sort toggle (asc ↔ desc) | ✅ PASS |

---

## 19. Alembic Result

> Alembic commands require a live DATABASE_URL (PostgreSQL connection) and a running DB,  
> which is unavailable in this build/export environment.  
>
> **No new database migrations are required** for this implementation:
> - All models used (`User`, `Transaction`, `Order`, `WalletLedger`, `PaymentIdempotency`,
>   `AdminAuditLog`) already exist in the schema.
> - No new columns, tables, or indexes were added.
>
> Run `alembic current` / `alembic upgrade head` on deployment to confirm schema is current.

---

## 20. Telegram Runtime Tests — Test Plan

| # | Test | Expected Behaviour |
|---|---|---|
| A | Admin → Users | Shows: 📋 Users List / 🔍 User Search / 📝 Manual Payments / ↩️ Return |
| B | Users List | Paginated list, format "@username \| {tg_id}" or "User {tg_id} \| {tg_id}" |
| C | Latest/Oldest sort | Query reorders in DB; icon toggles between 🕒 Latest and 🕰 Oldest |
| D | Pagination | ⬅️ Previous / ➡️ Next navigate pages without loading full table |
| E | Select user | Shows 👤 User Information with all fields, no literal "None" |
| F | User Search by Telegram ID | Finds user by integer ID; shows detail |
| G | User Search by @username | Case-insensitive; finds user; shows detail |
| H | Set Balance → cancel | Shows confirmation; ❌ Cancel returns to detail; no DB write |
| I | Add Balance → confirm | Wallet credited; WalletLedger row created; AdminAuditLog entry |
| J | Replay same confirmation | Token mismatch → "Already processed" alert; no double-credit |
| K | Deduct Balance | Wallet debited; negative guard prevents balance going < 0 |
| L | Ban User | Confirmation shown; after confirm, ban cache cleared; UI shows 🟢 Unban |
| M | Verify ban enforced | Banned user's next command blocked by `is_banned_check` |
| N | Unban User | Confirmation shown; after confirm, ban cleared; UI shows 🔴 Ban User |
| O | User Purchase History | Paginated real Orders; each button links to existing order detail |
| P | Open order from history | Routes to existing `admin_order_detail_callback` (no duplication) |
| Q | Position screen | Shows ADMIN or USER; explains settings-controlled architecture |
| R | Manual Payments | Paginated Transaction list (PaymentMethod.MANUAL) with status icons |
| S | Freshest/Oldest sort | Query reorders; mode label changes |
| T | Select pending payment | Detail shows amount, date, method, status + all valid action buttons |
| U | Get Proof | Sends photo (proof_file_id) or text (proof); "unavailable" if neither |
| V | Confirm → cancel | ❌ No, Cancel returns to detail; payment status unchanged |
| W | Confirm payment | Idempotency claimed; status → COMPLETED; wallet credited; user notified |
| X | Replay approval callback | PaymentIdempotency blocks; "already processed" shown; no double credit |
| Y | Reject pending payment | Confirmation; status → REJECTED; user notified |
| Z | Edit Debitable → cancel | ❌ No, Cancel returns to detail; amount unchanged |
| AA | Edit Debitable → confirm | tx.amount updated; detail refreshed with new amount |
| AB | Navigate to Users List after amount edit | No stale "Enter new amount" prompt |
| AC | Send unrelated text after leaving edit | Text not consumed by stale conversation |

**Status: Test plan defined. Live execution requires deployed bot with valid BOT_TOKEN and database.**

---

## 21. Remaining Risks

1. **Live Telegram test required** — compileall and inline logic tests pass; end-to-end flows need live bot validation.
2. **Edit Debitable on confirmed payments** — guarded with status re-validation (`tx.status not in _PENDING_STATUSES`), but guard runs only on conversation entry. If admin races to confirm between edit start and confirm callback, the `edit_debitable_confirm` re-validates and rejects. Safe.
3. **Payment method label** — `tx.manual_method.name` may raise `AttributeError` if `manual_method` relationship is not eagerly loaded. The code calls `_ = tx.manual_method` before session close to trigger load. If the session closes before this, `lazy='select'` would raise. Mitigation: always access `tx.manual_method` inside the session context (verified in code).
4. **Single-admin architecture** — Position panel correctly displays "settings-controlled" explanation. Role change UI is intentionally not implemented because the current codebase has no `roles` table or field; promoting users would require a schema migration.
5. **WalletLedger vs. wallet_svc.adjust** — The new balance confirm handler writes `WalletLedger` directly (same pattern as `wallet_svc._apply`) rather than calling `wallet_svc.adjust`, because `adjust` uses its own session and would close the open row-lock session. This is architecturally equivalent to `wallet_svc._apply`. If `wallet_svc` ever wraps additional logic, the balance handler should be updated to call it.
6. **`noop` handler placement** — Registered after all specific pattern handlers. If future handlers use a broad pattern that overlaps with `noop`, re-order as needed.
