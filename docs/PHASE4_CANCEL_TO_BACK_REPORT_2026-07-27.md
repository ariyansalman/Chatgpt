# Phase 4 — Payment & Deposit Navigation Cleanup — Report (2026-07-27)

## Guide used
`CANCEL_TO_BACK_AUDIT_2026-07-27.csv` (267 rows — the raw first-pass
heuristic audit, matching the counts described in the Phase-2 zip's own
report: 186 `CONVERT_TO_BACK`, 81 `KEEP_CANCEL`). That report explicitly
labels itself "a first-pass heuristic, not a final verdict" that needs a
human/code read on every `CONVERT_TO_BACK` row before swapping a button —
so each candidate touching a payment/deposit file was checked against the
actual code rather than applied automatically.

## Result: 0 conversions performed

Filtering the CSV to files that implement the Phase 4 scope (Add Funds,
Amount Selection, Payment Method Selection, Crypto Network Selection,
Deposit Details/Instructions, Pending/Manual-Review Deposit, Gateway
Selection) gives two groups:

**Already `KEEP_CANCEL` in the CSV** — `services/payment_ui.py` (488, 554,
701, 1471), `services/payment_selection_ui.py` (177, 252),
`handlers/payment_handlers.py` (703, 2971, 3609, 3619). Code review
confirms these are correctly kept: they're TXID-submission prompts,
"Cancel Deposit" on the Pending Deposit screen (an active-session cancel),
or invoice/method screens that already carry their own separate `🔙 Back`
(`topup_menu_back`) button alongside a distinct `❌ Cancel` (`cancel`)
abort button — converting Cancel here would either duplicate Back or
relabel an abort action as if it were reversible navigation.

**Flagged `CONVERT_TO_BACK` but not legitimate payment/deposit buttons —
verified false positives / out of scope:**

| File | Line | What it actually is |
|---|---:|---|
| `handlers/payment_handlers.py` | 6092 | A comment (`# Product screen keeps only quantity presets + ❌ Cancel...`), not a button. Keyword-matched by the heuristic. |
| `handlers/payment_handlers.py` | 6130 | Product **quantity** custom-entry prompt — has its own `⬅ Back to Product` button; `❌ Cancel` → `cancel_purchase` (abort). This is Shopping/Quantity-Selection scope (Phase 3), not Payment/Deposit. |
| `handlers/payment_handlers.py` | 6255/6262 | Purchase Summary (wallet-balance) screen — already has `◀ Back` button; `❌ Cancel` → `cancel_purchase` (abort). Also Phase 3 scope, not Phase 4. |
| `handlers/wallet_multicurrency_handlers.py` | 321 | Plain message text, `"❌ No target currencies available. Transfer cancelled."` — not a button at all. Also a currency-to-currency **transfer** flow, not a deposit flow. |
| `utils/keyboards.py` | 511, 519 | Same generic quantity/cancel keyboard helpers reviewed in Phase 3 and correctly kept there (511 pairs with its own Back button; 519 is a shared helper used almost entirely by non-shopping, non-this-phase-scope terminal flows). |

None of the `CONVERT_TO_BACK` rows that land in an actual payment or
deposit screen survive review — every real Payment/Deposit-scope Cancel
button in the codebase sits directly next to one of this phase's own
explicit KEEP conditions (TXID submission, active payment-session cancel,
manual review, waiting-for-confirmation) or already has a distinct,
correctly-wired Back button beside it.

## Payment flow verification
Since no files were modified, the documented flows (Wallet → Add Funds →
Amount → Payment Method → Payment Instructions → Back chain; Crypto
Network back-navigation; Pending Deposit → Continue → Back) are unchanged
from their current, working behavior — nothing to regress. Spot-checked
`payment_selection_ui.py` and `payment_ui.py` directly: Amount, Payment
Method, and Crypto Network screens already carry working `⬅️ Back`
buttons distinct from their `❌ Cancel` buttons, and Pending Deposit
already offers Continue / Cancel Deposit / Back as three separate,
correctly-scoped actions.

## Testing
- `python3 -m py_compile` run on all in-scope payment/deposit files
  (`payment_handlers.py`, `payment_ui.py`, `payment_selection_ui.py`,
  `amount_selection_ui.py`, `wallet_handlers.py`,
  `wallet_multicurrency_handlers.py`, `admin_pending_deposits.py`,
  `keyboards.py`) — all pass, no syntax errors.
- No files modified — no risk to callback routing, amount/method/network
  state, pending-deposit recovery, or duplicate-deposit creation.

## Deliverable summary
- Files modified: **0**
- Cancel → Back conversions: **0**
- Buttons reviewed and intentionally kept as Cancel: **10** in-scope
  buttons (table above notwithstanding — the 5 CSV-flagged rows were not
  genuine in-scope Cancel buttons at all)
- Payment/navigation issues found: **none**
- `py_compile`: **passed**

STOP after Phase 4 — Phase 5 not started.
