# Cancel → Back Navigation Audit (2026-07-27)

## Scope of this pass

No code has been changed yet — this is the audit deliverable you asked
to review first. Companion file: `CANCEL_TO_BACK_AUDIT_2026-07-27.csv`
(all 267 rows, one per "❌ Cancel" button found in the codebase).

**Method:** every `❌ Cancel` button in `handlers/` and `services/` was
located, then classified by scanning the 15 lines above it (the
enclosing conversation state / prompt text) for terminal-flow signals:
payment waiting, TXID/OTP/2FA input, ticket creation, withdrawals,
deposits, broadcast sending, confirm-delete, or other irreversible
steps — matching your Requirement 6 list exactly. Everything without
one of those signals is flagged `CONVERT_TO_BACK`.

**This is a first-pass heuristic, not a final verdict.** It will have
some false positives/negatives — e.g. a Cancel button 20 lines below
its "enter TXID" prompt instead of 15, or a `confirm_delete` button
whose text doesn't literally say "delete". Every `CONVERT_TO_BACK` row
should still get a quick human/code read before the button is actually
swapped, especially in files with 0 "keep" rows (below) — a 0 there
usually means "correctly all navigational," but occasionally means the
terminal-flow language just didn't match my keyword list.

## Results

| | Count |
|---|---|
| Total Cancel buttons found | 267 |
| Classified **KEEP_CANCEL** (matches your Requirement 6 list) | 81 |
| Classified **CONVERT_TO_BACK** (candidates) | 186 |
| Files touched | 79 |

## Files with the most conversion candidates

| File | Convert | Keep |
|---|---:|---:|
| handlers/admin_conversations.py | 25 | 0 |
| handlers/admin_broadcast_campaign_manager.py | 19 | 0 |
| handlers/admin_scheduled_broadcast.py | 10 | 1 |
| handlers/admin_handlers.py | 7 | 2 |
| handlers/admin_backups.py | 6 | 0 |
| handlers/admin_activity_feed.py | 5 | 0 |
| handlers/admin_user_profile.py | 5 | 0 |
| handlers/review_handlers.py | 5 | 0 |
| handlers/admin_api_manager.py | 5 | 0 |
| handlers/admin_bulk_products.py | 4 | 0 |
| handlers/admin_vip_manager.py | 4 | 0 |
| handlers/admin_announcements.py | 4 | 0 |
| handlers/admin_bulk_users.py | 4 | 0 |
| handlers/payment_handlers.py | 3 | 8 |
| handlers/menu_actions.py | 3 | 0 |
| handlers/admin_file_license_manager.py | 3 | 0 |
| handlers/admin_users.py | 3 | 0 |
| handlers/gift_purchase_handlers.py | 3 | 0 |
| handlers/favorites_handlers.py | 3 | 0 |
| handlers/admin_maintenance.py | 3 | 0 |
| handlers/coupon_handlers.py | 3 | 0 |
| ...54 more files with 1–2 each | | |

`payment_handlers.py` is the one file where the mix looks right at a
glance — 8 correctly-kept Cancels (TXID submission, manual payment
review, gateway flows) against only 3 flagged for conversion, which
lines up with Requirement 6's examples almost exactly.

## What "convert" will actually involve per button

Swapping the label isn't the hard part. For each `CONVERT_TO_BACK` row
I still need to:
1. Confirm the correct **immediate previous screen** (not just "the
   parent menu") — some of these are mid-conversation states where the
   previous screen is another step of the same flow, not a top menu.
2. Confirm the handler uses `edit_message_text`/`edit_message_reply_markup`
   already, or needs to be switched from a `send`/`reply` call per
   Requirement 5.
3. Confirm state (`context.user_data`, conversation stage) is preserved
   rather than reset — `admin_conversations.py`'s 25 buttons are the
   highest-risk cluster here since that file's fallback handler is
   already known (from the prior `NAVIGATION_AUDIT_REPORT.md`) to be
   shared across 8 unrelated conversations.
4. Where a screen currently has both a redundant Cancel *and* would
   also get a Back, collapse to one button per Requirement 7.

## Suggested execution order

Given the risk profile, I'd tackle these in batches, verifying
`py_compile` + a manual read after each file, not as one bulk
find-and-replace:

1. **admin_conversations.py** (25) — highest count and already flagged
   as the one file with a known cross-conversation routing bug, so it
   needs the most care.
2. **admin_broadcast_campaign_manager.py** (19)
3. **admin_scheduled_broadcast.py, admin_handlers.py, admin_backups.py**
4. Remaining ~65 files with 1–5 each — mechanical once the pattern
   from batches 1–3 is established.

## Files/rows for your review

Open `CANCEL_TO_BACK_AUDIT_2026-07-27.csv` — columns are `file`,
`line`, `function`, `button_text`, `callback_data`, `classification`,
`matched_keywords`. Sort/filter by `classification` to see the full
`CONVERT_TO_BACK` list before I touch any code.
