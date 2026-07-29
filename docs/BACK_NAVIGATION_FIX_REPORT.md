# Pending Deposits — Back Navigation Audit & Fix

**Scope:** Back-button navigation and Pending Deposits rendering only.
**Not touched:** business logic, payment logic, wallet logic, DB schema, APIs, routes, callback data, permissions, security — every callback string, query filter, and status transition is byte-for-byte identical to before.

---

## 1. Flows audited

| Flow | Callback | File |
|---|---|---|
| Pending Deposit Details | `pd:det:{tx_id}` | `handlers/admin_pending_deposits.py` |
| Manual Review (raw info / proof) | `pd:info:{tx_id}` | same |
| Payment Review (list) | `pd:list:{page}:{sort}` | same |
| Approve Flow | `pd:appr_ask` / `pd:appr_ok` | same |
| Reject Flow | `pd:rej_ask` / `pd:rej_ok` | same |
| Verify Again Flow | n/a for this module — MANUAL/BKASH/NAGAD deposits have no automated re-verification API, by design (see module docstring); the "Verify Again" button in `services/payment_ui.admin_review_keyboard` is simply omitted here. Not a navigation gap. |

## 2. What was wrong

Every one of those flows renders a "⬅ Back" button, and **every one of them was pointed at the identical literal string** `"pd:list:0:desc"`, copy-pasted independently at six separate call sites:

- the pending-status review keyboard (`_deposit_kb`)
- the already-approved/already-rejected keyboard (`_deposit_kb`, else-branch)
- the duplicate-approval guard fallback
- the approve-error fallback
- the approve-success "refresh detail view" fallback
- the reject-success "refresh detail view" fallback

Each copy happened to agree, so the button *did* still route to the live `pending_deposits_list` handler. But this is precisely the failure mode the previous audit (`PENDING_DEPOSITS_QUERY_AUDIT.md`) already flagged for the counting/listing queries: **when the same value is hand-duplicated across several call sites, one of them drifting on a future edit is enough to break navigation** — e.g. a future tweak to one Back button (a typo, a different page number, a different target screen) would silently detach that one screen's Back button from the live-querying handler while every other screen kept working, reproducing exactly the "Back opens a stale/hard‑coded empty screen while deposits still exist" symptom described in this task.

There was no working code path where Back rendered a literally hard-coded empty-state message; the risk was structural (duplication), not an active bug in this snapshot — but it is the direct root cause class the task describes, and left unfixed it reappears the next time any one of those six call sites is touched without the others.

## 3. Fix applied

1. **Single source of truth for the destination.** Added one module-level constant:
   ```python
   _BACK_TO_LIST_CB = "pd:list:0:desc"
   ```
   All six call sites now reference `_BACK_TO_LIST_CB` instead of a re-typed literal. The callback_data string itself is unchanged, so the existing `CallbackQueryHandler` registration (`^pd:list:\d+:(asc|desc)$` in `bot.py`) still matches — full backward compatibility, no route/pattern changes.

2. **Single implementation of the rendering rule.** Extracted the DB-query + decision logic out of `pending_deposits_list()` into a dedicated function:
   ```python
   async def _render_pending_deposits_list(query, page: int, sort: str):
       ...
       pending_rows = pui.pending_deposit_rows(session, sort_desc=sort == "desc")
       total = len(pending_rows)
       if total == 0:
           # render empty state
       else:
           # render the list
   ```
   `pending_deposits_list()` (the registered callback handler) now only parses `page`/`sort` from `query.data` and delegates to this function. Because **every** Back button ultimately re-enters through this one function, the rendering rule is enforced in exactly one place:
   - `pending_count > 0` → Pending Deposits list, built from the same live query used for the count (no separate COUNT query that could disagree with the rows).
   - `pending_count == 0` → empty-state screen.

   No caching, no stored/reused screen state — every press re-runs `pui.pending_deposit_rows()` against the database at that instant.

## 4. Final verification

| Check | Result |
|---|---|
| Back reloads live data from the database | ✅ every Back button → `_BACK_TO_LIST_CB` → `pending_deposits_list` → `_render_pending_deposits_list` → fresh `pui.pending_deposit_rows()` query |
| Pending deposits are displayed immediately after returning | ✅ list is built from the same query used for the count, in the same request |
| Empty-state appears only when the database contains zero pending deposits | ✅ single `if total == 0` branch, no other code path renders that message |
| No stale cached screens | ✅ no session/state is reused across requests; `get_db_session()` opens/closes a fresh session per call |
| Existing callbacks remain compatible | ✅ callback_data strings, handler registrations in `bot.py`, and all business/payment/wallet logic are unchanged |
