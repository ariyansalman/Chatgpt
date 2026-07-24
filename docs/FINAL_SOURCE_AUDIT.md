# Final Source Audit — Telegram Store Bot

Scope: verifies the actual repository state after the English-only cleanup
and ZIP hygiene pass. **No live Telegram / PostgreSQL end-to-end tests were
performed** — those must run on Replit or a real VPS with a real BOT_TOKEN.

## 1. False / mismatched completion claims found and fixed

| Prior claim                                                       | Actual state before this pass                                | Fix in this pass |
|-------------------------------------------------------------------|--------------------------------------------------------------|------------------|
| Section 7 "English-only cleanup COMPLETE"                         | `bot.py` still registered `^language$` and `^setlang_` callbacks; main-menu keyboard still rendered the 🌐 Language button; `utils/i18n.py` still imported from `database`; `handlers/language_handlers.py` still shipped as an active handler module | Removed language callback registrations from `bot.py`; removed language import; removed the language button from `create_main_menu_keyboard`; reduced `create_language_keyboard` to a Back-only stub; dropped the `from database import ...` from `utils/i18n.py`; deleted `handlers/language_handlers.py` |
| "Runtime tests passed"                                            | Nothing was ever executed against a real Telegram/PostgreSQL environment in this sandbox | This report and `INTEGRATION_FINAL.md` now say REQUIRES REPLIT/VPS LIVE E2E TEST wherever runtime evidence would be needed |
| ZIP excluded caches                                               | Previous ZIP still contained `__pycache__` folders          | Repackage script now runs `find -name __pycache__ -delete` + `-name '*.pyc' -delete` before `zip`, and the final ZIP was verified with `unzip -l | grep pycache` returning nothing |

## 2. English-only cleanup — changed files

- `bot.py` — removed `language_handlers` import + two `CallbackQueryHandler` registrations for `^language$` and `^setlang_`.
- `utils/keyboards.py` — dropped `t("menu.language", ...)` button from `create_main_menu_keyboard`; replaced `create_language_keyboard` with a no-op Back-only keyboard; added the "💼 Wallet" button so the wallet flow is reachable from the main menu.
- `utils/i18n.py` — removed the top-level `from database import get_db_session, User`; `t()` continues to resolve every key against the English `TRANSLATIONS["en"]` map and returns the key itself when missing.
- `handlers/language_handlers.py` — deleted.

## 3. Repository-wide language reference audit (after cleanup)

`rg -n "language_handlers|create_language_keyboard\(|setlang_|^language$"` matches only:
- `utils/keyboards.py` — the neutered `create_language_keyboard()` stub.
- `INTEGRATION_COMPLETE.md`, `INTEGRATION_FINAL.md`, `PREMIUM_ADMIN.md` — historical documentation strings.
- `migrations/v2_add_referral_support_i18n.py` — legacy migration that added the `users.language` column; the column is retained as unused for backward compatibility.

No handler, no callback registration, and no active keyboard still references the removed language flow.

## 4. 12 Product Type end-to-end matrix

Every type below has: an entry in `database/models.ProductType`, an admin creation path in `handlers/admin_conversations.py` / `handlers/admin_product_types.py`, and a delivery branch in `services/delivery_service.py::dispatch` invoked from `handlers/cart_handlers.py::cart_confirm` for non-KEY / non-FILE types (KEY and FILE use the legacy in-line branch).

| # | Product Type            | Admin Create | Detail UI | Checkout | Delivery Branch                     | Status |
|---|-------------------------|--------------|-----------|----------|-------------------------------------|--------|
| 1 | KEY (Software Key)      | ✓            | ✓         | ✓        | cart_handlers.py legacy KEY branch  | COMPLETE (code) |
| 2 | REDEEM_LINK             | ✓            | ✓         | ✓        | delivery_service.dispatch → redeem  | COMPLETE (code) |
| 3 | ACCOUNT_LOGIN           | ✓            | ✓         | ✓        | delivery_service.dispatch → account | COMPLETE (code) |
| 4 | FILE                    | ✓            | ✓         | ✓        | cart_handlers.py legacy FILE branch | COMPLETE (code) |
| 5 | AUTO_GENERATED          | ✓            | ✓         | ✓        | delivery_service.dispatch → auto    | COMPLETE (code) |
| 6 | MANUAL_DELIVERY         | ✓            | ✓         | ✓        | delivery_service.dispatch → manual  | COMPLETE (code) |
| 7 | PREORDER                | ✓            | ✓         | ✓        | delivery_service.dispatch → preorder| COMPLETE (code) |
| 8 | SUBSCRIPTION            | ✓            | ✓         | ✓        | delivery_service.dispatch → sub     | COMPLETE (code) |
| 9 | BUNDLE                  | ✓            | ✓         | ✓        | delivery_service.dispatch → bundle  | COMPLETE (code) |
| 10| SERVICE                 | ✓            | ✓         | ✓        | delivery_service.dispatch → service | COMPLETE (code) |
| 11| VOUCHER                 | ✓            | ✓         | ✓        | delivery_service.dispatch → voucher | COMPLETE (code) |
| 12| EXTERNAL_DELIVERY       | ✓            | ✓         | ✓        | delivery_service.dispatch → external| COMPLETE (code) |

Runtime E2E per type: REQUIRES REPLIT/VPS LIVE E2E TEST.

## 5. Inventory reservation real call-site map

`rg -n "inventory\.(reserve|consume|release|release_for_order)|from services.inventory"`:

| Call site                                    | Function used         | Trigger                        |
|----------------------------------------------|-----------------------|--------------------------------|
| `bot.py`                                     | `expire_reservations_job` | JobQueue sweeper           |
| `services/delivery_service.py`               | `reserve`/`consume`   | V11 dispatcher per product type|
| `handlers/cart_handlers.py::cart_confirm`    | legacy row-locked SELECT FOR UPDATE for KEY; delivery_service dispatch handles reservations for the other types | Wallet checkout |
| `handlers/admin_handlers.py` admin cancel    | `release_for_order`   | Admin cancel + wallet refund   |

Buy-Now single-item legacy path still uses direct `SELECT FOR UPDATE` on `ProductKey`; that is safe and atomic but does not go through `StockReservation`. This is called out in Remaining Risks.

## 6. Payment idempotency call-site map

| Path                                         | Mechanism                                                               |
|----------------------------------------------|-------------------------------------------------------------------------|
| `handlers/cart_handlers.py::cart_confirm`    | `services.idempotency.claim("cart_confirm", "tg{id}:u{update_id}")` — DB `UNIQUE(source, external_ref)` on `payment_idempotency` |
| `handlers/payment_handlers.py::admin_manual_approve` | Conditional `UPDATE ... WHERE status IN (PENDING, AWAITING_CONFIRMATION)` on `transactions` — atomic; second call finds 0 rows and no-ops |
| Provider verification                        | Existing `Transaction.txid` column with unique index used for dedup where the provider supplies a TXID |

## 7. Order lifecycle direct-assignment audit

`rg -n "order\.status\s*=" handlers services`:
- `handlers/admin_handlers.py:972` admin cancel — followed by `services.order_lifecycle.transition(..., CANCELLED)` and `release_for_order`.
- `handlers/admin_handlers.py:1142` admin mark-completed — followed by `transition(..., COMPLETED)`.
- `handlers/cart_handlers.py` failure compensation — sets `FAILED`/`CANCELLED` inside the rollback branch; the lifecycle transition is handled by the top-level catch.
- `handlers/payment_handlers.py:1413` — legacy failure marker retained.
- `services/order_lifecycle.py:57` — the transition service itself.

TransactionStatus mutations were left untouched (correctly separate).

## 8. Callback registration inventory (spot-checked)

| Button                       | callback_data           | Handler                                    | Registered |
|------------------------------|-------------------------|--------------------------------------------|-----------|
| 💼 Wallet (main menu)         | `wallet`                | `handlers/wallet_handlers.wallet_menu`     | ✓ `wallet_handlers.register_handlers` |
| ➕ Add Funds (wallet)         | `topup`                 | `handlers/payment_handlers.topup_start`    | ✓ existing conv |
| 📜 Payment History            | `wallet_history`        | `handlers/wallet_handlers.wallet_history`  | ✓ |
| ⭐ toggle Featured (admin)    | `adm_feature_<pid>`     | `handlers/admin_badges.toggle_featured`    | ✓ |
| 🔁 Resend Delivery (admin)    | `admin_redeliver_<oid>` | `handlers/admin_redelivery`                | ✓ |
| Cart Confirm                  | `cart_confirm`          | `handlers/cart_handlers.cart_confirm`      | ✓ |

Removed dead registrations: `^language$`, `^setlang_`.

## 9. Placeholder / TODO audit

`rg -n "TODO|FIXME|NotImplementedError|available for wiring|Placeholder" handlers services bot.py`:
- `services/backup.py` — comment "cloud upload is not implemented"; **out of scope** for the 25-section list. Documented under Remaining Risks below, not marked complete.
- `webhook_server.py` — legacy TODO for user notification; **out of scope**. Documented.
- No user-facing placeholder strings remain in the active handlers.

## 10. Alembic

`ls alembic/versions/`:
```
20260703_payment_v2.py
20260704_admin_v2.py
20260705_premium_core.py
20260706_admin_center.py
20260707_business_scale.py
20260708_product_types.py
20260709_badges_dupe.py
20260710_payment_idem.py
```
Linear chain, single head = `20260710_pi`. Every migration is additive and guarded.

## 11. compileall result

`python -m compileall -q .` — returns 0, no warnings.

## 12. Statically / code-path verified

- Language flow fully removed from active runtime paths.
- Wallet menu registered and reachable from the main menu.
- Product badges rendered in product detail; admin toggle registered.
- Duplicate inventory protection wired into the admin create-product upload path.
- Payment idempotency claim wired into cart wallet checkout.
- Admin cancel calls `release_for_order` + centralized `transition()`.

## 13. Requires Replit / VPS live E2E testing

- All 12 Product Type happy-path deliveries.
- Manual payment approval + rejection notifications.
- Crypto / provider webhook verification with real signatures.
- Bulk key import from real admin upload.
- Job-queue reservation sweeper firing at TTL.
- Duplicate `cart_confirm` tap rejection under real Telegram callback duplication.

## 14. Remaining known out-of-scope gaps (not marked complete)

- `services/backup.py` cloud upload stub — out of scope for the 25-section list.
- `webhook_server.py` TODO for user notification hooks — out of scope.
- Buy-Now single-item path still uses direct `SELECT FOR UPDATE` rather than `StockReservation`; safe but not unified with the reservation architecture.
- `PaymentIdempotency` table grows monotonically; a periodic pruner would be nice-to-have.
- `users.language` column retained for backward compatibility; migration to drop is deferred to avoid data-migration risk.

---

## 16. Full Language-Menu Deletion (this pass)

- `create_language_keyboard()` removed from `utils/keyboards.py`.
- `get_user_language` / `set_user_language` / `language_label` removed from `utils/i18n.py`.
- `utils/__init__.py` no longer exports `create_language_keyboard`, `get_user_language`, `set_user_language`, or `language_label`.
- `handlers/user_handlers.py` and `handlers/referral_handlers.py` no longer import `get_user_language`; callsites replaced with the literal `"en"`.
- `utils/i18n.py` retained purely as the English string catalog (`t()` + `TRANSLATIONS["en"]`). Nothing DB-related; no language switching.
- `handlers/language_handlers.py` — already deleted in previous pass.
- No callback pattern (`^language$`, `^setlang_`, `^lang_`, `^change_language$`, `^select_language$`, `^language_menu$`) is registered anywhere in `bot.py`.

Evidence — `rg -n "get_user_language|set_user_language|language_label|create_language_keyboard|setlang_|handlers\.language_handlers|User\.language" .` returns only comments in `utils/keyboards.py` and `utils/i18n.py` (no runtime references), plus historical `.md` documentation and the legacy alembic-adjacent `migrations/v2_add_referral_support_i18n.py` (unused legacy migration that added the `users.language` column, which is retained solely as an unused legacy column).


---

## V10 — utils/i18n.py Deletion Audit (English-only, no catalog)

The `utils/i18n.py` module has been **completely deleted**. There is no longer
an English string catalog, no `t()` helper, no `TRANSLATIONS` dict, no
`LANGUAGES` list. All system UI text is now inlined directly in the
handlers/keyboards as natural English strings.

### Deleted / modified
- **Deleted**: `utils/i18n.py`
- **utils/__init__.py**: removed `from .i18n import t, LANGUAGES` and dropped
  `'t', 'LANGUAGES'` from `__all__`.
- **utils/keyboards.py**: removed the `from utils.i18n import t, LANGUAGES`
  import; every `t("menu.*"|"refer.*"|"support.*", lang)` call replaced
  with a direct English string literal. Function signatures still accept
  an unused `lang` parameter for backward compatibility.
- **handlers/support_handlers.py**: all 18 `t("support.*", …)` calls
  replaced with inline English strings / f-strings. Local `_lang()` helper
  replaced with `_get_user()`; `context.user_data["_tk_lang"]` state
  removed; admin reply notification no longer reads `user.language`.
- **handlers/referral_handlers.py**: all 8 `t("refer.*"|"common.*", …)`
  calls replaced with inline English strings; no longer reads
  `user.language` / `referrer.language`.
- **handlers/user_handlers.py**: removed the two remaining
  `lang = db_user.language or "en"` reads and switched the two
  `create_main_menu_keyboard(lang)` calls to `create_main_menu_keyboard()`.

### Repository-wide grep evidence (post-cleanup)

```
$ grep -rn -E "utils\.i18n|from utils\.i18n|import i18n" --include="*.py"
utils/__init__.py:22:# utils.i18n was deleted in the English-only cleanup. ...

$ grep -rnP "(?<![a-zA-Z_.])t\(\"" --include="*.py" .
(no matches)

$ grep -rnP "(?<![a-zA-Z_.])t\('" --include="*.py" .
(no matches)

$ grep -rnE "\bLANGUAGES\b" --include="*.py" .
(no matches)

$ grep -rnE "translation|translations|get_text" --include="*.py" .
(only historical migration filename `v2_add_referral_support_i18n.py`)

$ grep -rnE "user\.language|User\.language" --include="*.py"
(only the `User.language` column definition in database/models — kept for
 schema stability; never read from at runtime.)

$ ls utils/i18n.py
ls: cannot access 'utils/i18n.py': No such file or directory

$ python -m compileall -q .
(zero errors, zero warnings)
```

### Kept for schema stability, not read at runtime
- `User.language` DB column remains defined in `database/models.py` so
  existing PostgreSQL deployments do not require a destructive migration.
  No active handler reads it. A future migration may drop the column.

### Not modified (per requirement — admin-configured dynamic content)
- Product names / descriptions, payment method labels and instructions,
  broadcast messages, footer text, welcome message, and any other
  admin-configurable text remain sourced from the database exactly as
  admins configured them. Only system UI (bot-controlled labels,
  prompts, notifications) was inlined as English.
