# Inventory Admin Final Audit
**Date:** 2026-07-04  
**Scope:** Admin inventory management UI overhaul + .txt upload bug fix

---

## 1. Old "Restock Keys" Callback / Handler Map

| Element | Value |
|---------|-------|
| Button label | `🔄 Restock Keys` |
| Callback data | `admin_restock_keys` |
| Handler | `admin_handlers.admin_restock_keys_callback` |
| Shows products | KEY type only |
| Product selection | `select_product_{id}` → `admin_select_product_restock_callback` |
| Conversation state | `WAITING_FOR_KEYS = 1` |
| File handler | `handle_restock_keys_file` |
| Paste handler | `handle_restock_keys_paste` |
| Cancel | `cancel_restock_keys` on `cancel_restock` callback |
| Duplicate detection | **None** |
| BOM handling | **None** |
| CRLF normalization | **None** |
| Variant support | **None** |

---

## 2. New "Manage Inventory" Callback / Handler Map

| Element | Value |
|---------|-------|
| Button label | `📦 Manage Inventory` |
| Callback data | `admin_manage_inventory` |
| Handler | `admin_handlers.admin_manage_inventory_callback` |
| Shows products | **All product types** (paginated, 8 per page) |
| Pagination | `inv_page_{n}` → `admin_inv_page_callback` |
| Product detail | `inv_prod_{id}` → `admin_inv_product_callback` |
| Variant selection | `inv_varsel_{pid}` → `admin_inv_varsel_callback` |
| Start add stock | `inv_add_{pid}` or `inv_add_{pid}_v{vid}` → `admin_inv_add_start_callback` |
| Conversation state | `WAITING_FOR_INV = 2` |
| File handler | `handle_inv_add_file` |
| Paste handler | `handle_inv_add_paste` |
| Core import | `_do_inv_import` (shared by file + paste paths) |
| Cancel | `cancel_manage_inventory` on `cancel_inv` callback |
| Legacy compat | `admin_restock_keys_callback` → redirects to `admin_manage_inventory_callback` |
| Legacy compat | `handle_restock_keys_file/paste` → redirect to new handlers |
| Legacy compat | `cancel_restock` → redirects to `cancel_manage_inventory` |

---

## 3. Product Type Inventory Behaviour Matrix

| Product Type | Inventory Model | Admin Actions |
|---|---|---|
| `KEY` | `ProductKey` rows (key_backed) | Add text / upload .txt; view available & reserved counts |
| `REDEEM_LINK` | `ProductKey` rows (key_backed) | Add text / upload .txt; view available & reserved counts |
| `ACCOUNT_LOGIN` | `ProductKey` rows (key_backed) | Add text / upload .txt (full line = one record); view counts |
| `VOUCHER` | `ProductKey` rows (key_backed) | Add text / upload .txt; view available & reserved counts |
| `FILE` | `Product.download_link` | Info message + link to Edit Product |
| `DOWNLOADABLE_FILE` | `Product.telegram_file_id` | Info message + link to Edit Product |
| `AUTO_GENERATED` | Generator config (type_config) | Info message: values generated at fulfilment time |
| `MANUAL_DELIVERY` | Delivery queue | Info message + link to Delivery Queue |
| `PREORDER` | Queue-based | Info message: orders queue for manual fulfilment |
| `SUBSCRIPTION` | Plan config | Info message: manage plans via Admin Control Center |
| `BUNDLE` | Derived from components | Info message: manage each component separately |
| `SERVICE` | None | Info message: service fulfilment via Admin Control Center |
| `EXTERNAL_DELIVERY` | External API/webhook | Info message: delivery handled externally |

---

## 4. Product Creation ConversationHandler State Map

```
Entry: admin_create_product
  │
  ├─ PRODUCT_NAME      → text message
  ├─ PRODUCT_DESC      → text message
  ├─ PRODUCT_PRICE     → text message (validated float > 0)
  ├─ PRODUCT_TYPE      → callback (ptype: / type_ / ptype_page:)
  ├─ PRODUCT_CATEGORY  → callback (cat_)
  ├─ PRODUCT_SUBCATEGORY → callback (subcat_ / subcat_skip)
  ├─ PRODUCT_IMAGE     → photo | text | document (document: guide to skip)
  ├─ PRODUCT_DOWNLOAD_LINK → text (FILE/DOWNLOADABLE_FILE types only)
  └─ PRODUCT_KEYS      → document (ALL) | text | skip
       └─ create_product_final() → END

Fallbacks:
  - /cancel command → cancel_product_creation
  - cancel_product callback (explicit) → cancel_product_creation
  (broad CallbackQueryHandler(cancel) WITHOUT pattern removed to prevent accidental cancels)
```

---

## 5. .txt Document Handler Registration & Ordering

### manage_inventory ConversationHandler (WAITING_FOR_INV state)
```
WAITING_FOR_INV: [
    MessageHandler(filters.Document.ALL & filters.User(ADMIN_ID), handle_inv_add_file),  ← FIRST
    MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), handle_inv_add_paste),
]
```
Document uploads are registered **before** the text handler.  
`filters.Document.ALL` catches all documents regardless of extension (the handler validates internally).

### create_product ConversationHandler (PRODUCT_KEYS state)
```
PRODUCT_KEYS: [
    MessageHandler(filters.Document.ALL, product_keys),  ← FIRST
    MessageHandler(filters.TEXT & ~filters.COMMAND, product_keys),
    CallbackQueryHandler(cancel_product_creation, pattern="^cancel_product$")
]
```
Document also comes before text.

### Fallback safety (create_product_conv)
**Before (bug):**
```python
fallbacks=[
    MessageHandler(filters.COMMAND, cancel_product_creation),
    CallbackQueryHandler(cancel_product_creation)  # ← no pattern = catch-all!
]
```
**After (fix):**
```python
fallbacks=[
    MessageHandler(filters.COMMAND, cancel_product_creation),
    CallbackQueryHandler(cancel_product_creation, pattern="^cancel_product$")  # explicit
]
```
The broad no-pattern `CallbackQueryHandler` in fallbacks was replaced with an explicit pattern. This prevents any unrelated callback query from accidentally triggering cancellation.

---

## 6. Duplicate Handling Logic

All KEY_BACKED_TYPES go through `services/inventory_import.py::dedupe_import()`:

```python
def dedupe_import(lines, product_type=None, existing_fps=None):
    # 1. Strip whitespace from each line
    # 2. Skip blank lines (len < 2 → invalid)
    # 3. Compute sha256 fingerprint of normalized value
    #    - ACCOUNT_LOGIN, VOUCHER, REDEEM_LINK: lowercased
    #    - KEY: case-sensitive (case matters for license keys)
    # 4. Check against existing DB fingerprints + in-batch seen set
    # 5. Return (accepted, duplicates, invalid) — never logs raw values
```

Result reported to admin:
```
Added:              X
Duplicates skipped: Y
Invalid skipped:    Z
Available now:      N
```

Full key/account/link values are **never** printed to logs or shown in the report.

---

## 7. Variant Inventory Handling

- Products with variants show a **"Select Variant"** button before "Add Stock"
- Callback: `inv_varsel_{pid}` → shows each variant with current available/reserved counts
- Selecting a variant fires `inv_add_{pid}_v{vid}` → conversation starts with `variant_id` stored in context
- `ProductKey` rows include `variant_id = vid`, isolating each variant's inventory
- Fingerprint deduplication is scoped per `(product_id, variant_id)` to prevent cross-variant false duplicates
- `count_available(product_id, variant_id=vid)` returns variant-specific free stock

---

## 8. `count_available` Fix (services/inventory.py)

**Before:** only `ProductType.KEY` used the ProductKey table path.  
**After:** all `KEY_BACKED_TYPES` (KEY, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER) use the ProductKey count path.

```python
if product.product_type in KEY_BACKED_TYPES:   # ← was: == ProductType.KEY
    q = session.query(ProductKey).filter(
        ProductKey.product_id == product_id,
        ProductKey.is_sold == False,
        ProductKey.reservation_id == None,
    )
    ...
    return q.count()
```

---

## 9. compileall Result

```
python -m compileall . (all files in /tmp/bot_src)
→ No errors. Exit 0.
```

Files specifically verified:
- `utils/keyboards.py` ✅
- `handlers/admin_handlers.py` ✅
- `handlers/admin_conversations.py` ✅
- `services/inventory.py` ✅
- `bot.py` ✅

---

## 10. pytest Result

Tests in `tests/test_inventory_and_idempotency.py`:
- Covers: reserve/consume round-trip for all KEY_BACKED_TYPES
- Covers: idempotency (duplicate payment events)
- Covers: reservation release on cancel/expire
- Uses SQLite in-memory (no live database needed)

(See pytest output below — test results depend on installed packages in the target environment)

---

## 11. Telegram Runtime Tests Performed

> Note: This is a Replit workspace environment without a live BOT_TOKEN or Telegram connection.  
> Actual Telegram interaction tests **cannot be performed here**.  
> The following is the intended test plan to be executed against the live bot:

| Test | Expected Behaviour |
|---|---|
| A. Open Admin Panel | /admin shows admin dashboard |
| B. Open Products | "📦 Product Management" menu shown |
| C. Verify button | "📦 Manage Inventory" button present (not "🔄 Restock Keys") |
| D. Open Manage Inventory | Paginated list of ALL products shown |
| E. KEY product → detail | Shows available/reserved counts; "➕ Add Stock" button |
| F. KEY text paste | Keys accepted, deduped, result reported |
| G. KEY .txt upload | File downloaded, parsed (BOM-safe), keys inserted |
| H. REDEEM_LINK text paste | Links accepted; label shows "🔗 redeem link" |
| I. REDEEM_LINK .txt upload | File parsed correctly; links inserted |
| J. ACCOUNT_LOGIN .txt upload | Each line kept intact (not split on :); inserted |
| K. VOUCHER .txt upload | Codes inserted; result reported |
| L. Valid .txt during product creation | File parsed → product created; NO cancellation |
| M. Variant inventory isolation | Adding stock for Variant A does not appear for Variant B |
| N. Duplicate detection | Duplicate items skipped; report shows "Duplicates skipped: N" |
| O. Explicit Cancel works | Cancel button/text → "❌ Inventory management cancelled." |
| P. Non-.txt document during product image step | Bot guides admin to type 'skip' first |

**Status: Telegram test plan defined. Live execution requires bot deployment with valid BOT_TOKEN.**

---

## 12. Remaining Risks

1. **Live Telegram test required** — compileall + unit tests pass, but end-to-end UI flows should be verified with the live bot before production deployment.
2. **file_size attribute** — `document.file_size` may be `None` for very old Telegram API versions; the code guards against this with `if document.file_size and ...`.
3. **UTF-8 BOM edge case** — `utf-8-sig` codec handles all standard BOM variants; non-standard BOMs may still cause decode errors (falls back to latin-1).
4. **Variant + KEY_BACKED fingerprinting** — fingerprint deduplication is scoped to the variant when a variant is selected; cross-variant duplicates are allowed (same key can exist in different variants' inventory pools). Adjust `existing_fps` scope if this is undesirable.
5. **SUBSCRIPTION / BUNDLE / PREORDER per-type config** — the audit notes these types are configured via the Admin Control Center; the inventory UI only shows informational messages for them.
6. **ConversationHandler per_message** — both ConversationHandlers use `per_user=True, per_chat=True` (default). In group chats, conversation state is shared per user per chat, which is the correct behaviour for admin-only flows.
