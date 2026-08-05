# Post-Phase-5 Fix — Cancel/Back Label Correction (2026-07-27)

## What triggered this
Cross-checking `CANCEL_TO_BACK_AUDIT_v2_FINAL.csv` (134 rows, all
`KEEP_CANCEL`) against the delivered Phase 5 ZIP found 23 lines where the
button text was already `⬅️ Back` even though the CSV — and the actual
handler behavior — says it should be `❌ Cancel`.

These 23 buttons were **not touched by Phases 3, 4, or 5** (those made 0
code changes). They were already labeled `⬅️ Back` in the Phase 2 output,
i.e. changed during Phase 1 or 2, before this audit process began. All 23
are in the **Admin Panel** (broadcast center, broadcast campaign manager,
bulk products, bulk users, data export, global search, delivery format) —
outside the Store/Shopping and Payment/Deposit scope of Phases 3–4, so
they were never reviewed until now.

## Verification performed
For every one of the 23, traced the button's `callback_data` to its
actual handler function and read the function body. Every single one:

- Pops one or more `context.user_data` keys (clearing in-progress state —
  a draft campaign, an import job, a search filter, a broadcast draft),
- Shows a message that says "Cancelled" (e.g. "❌ Import cancelled.",
  "❌ Export cancelled.", "🔍 Search cancelled.", "❌ Cancelled."),
- Returns `ConversationHandler.END` (terminates the flow rather than
  stepping back one screen).

This is exactly the abort/irreversible behavior this whole project's
Back Button Rules say Cancel must be used for ("preserve
context.user_data", "never restart the workflow") — the buttons were
functioning as Cancel while labeled Back, which is a real label/behavior
mismatch a user would notice (tap "⬅️ Back" expecting to return to where
they were, and instead their in-progress campaign/import/search is wiped).

One additional confirmed inconsistency: `handlers/admin_data_export.py`
had the *same* cancel callback (`dec:cancel_conv`) labeled `❌ Cancel` at
one call site (line 274) and `⬅️ Back` at another (line 360) — same
button, same destination, different label depending on which screen it
was shown from.

## Fix applied
**Label-only change** — reverted `⬅️ Back` → `❌ Cancel` on all 23 lines.
No `callback_data`, no handler function, no business logic, no
conversation-state logic was touched, per this project's standing "Do NOT
touch: callback names / business logic" rule across all phases.

| File | Lines fixed |
|---|---|
| `handlers/admin_broadcast_campaign_manager.py` | 560, 578, 600, 619, 642, 938, 967, 987, 1029, 1195, 1211, 1234 (12) |
| `handlers/admin_bulk_products.py` | 202, 486, 494 (3) |
| `handlers/admin_bulk_users.py` | 184, 499, 584 (3) |
| `handlers/admin_broadcast_center.py` | 327, 502 (2) |
| `handlers/admin_data_export.py` | 360 (1) |
| `handlers/admin_global_search.py` | 805 (1) |
| `handlers/admin_delivery_format.py` | 105 (1) |
| **Total** | **23** |

## Testing
- `python3 -m py_compile` on all 7 modified files — pass.
- `python3 -m py_compile` project-wide — pass, 0 errors.
- Re-ran the CSV cross-check against the fixed code: **0 remaining
  mismatches** across all 134 rows.

## Summary
- Files modified: **7**
- Labels corrected (Back → Cancel): **23**
- Callback/business logic changed: **0**
- py_compile: **pass (file-level and project-wide)**
