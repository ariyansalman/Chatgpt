# Favorites & Recent — UI Exposure Report

## Files Modified
- `handlers/admin_control_center.py` only. No other file touched.

## Buttons Added
**Root menu** (`build_acc_root_keyboard`), new row directly below `⚙️ Store Settings`:
- `⭐ Favorites` → `acc:ui:favs`
- `🕐 Recent` → `acc:ui:recent`

No existing button removed or reordered.

## Pin Buttons Added
**Category submenu** (`_build_category_keyboard`), every settings row now renders as:
```
[ Setting Name ]  [ ⭐ or ★ ]
```
- Pin state read via the existing `_is_fav()` helper — no new tracking logic.
- Toggling calls the existing `_toggle_fav()` — untouched.
- Applied to **both** the compact and non-compact layouts (Task 3). Both toggle flags still work; neither layout was disabled.

## Compatibility Issue Found & Fixed (caller-side only, not backend)
`_toggle_fav()` silently no-ops when `_MAX_FAVS` (8) is reached but still returns `True`. Since the pin button was previously unreachable, this was never user-visible. Now that it's exposed, the caller (`_handle_ui_action`, action `"pin"`) checks the count **before** calling `_toggle_fav()` and shows an honest warning:
> ⚠️ Max 8 favorites reached. Unpin one first.
instead of a false "⭐ Pinned!" toast when nothing was actually added.
**`_toggle_fav()` itself was not modified** — only the calling code's messaging.

## Immediate Refresh (Task 5)
- `_render_category()` now stores `(cat, page)` in the admin's nav data whenever a category page is shown.
- The pin handler reads that pointer and calls `edit_message_reply_markup()` on the *same* message to flip ⭐↔★ instantly — no new message sent, no jump back to root.
- Falls back to the old root re-render only if the pointer is missing (shouldn't occur in normal use).

## Favorites Screen (Task 6) — verified, unchanged
`_build_favs_keyboard()` already correctly shows: pinned settings with real callback_data, an `✖ Unpin` button per row, and the existing empty-state message. No redesign needed.

## Recent Screen (Task 7) — verified, unchanged
`_build_recent_keyboard()` already correctly shows recently visited settings via the existing `_record_recent()` / history logic. Not modified.

## Permissions (Task 8) — verified, unchanged
All `acc:*` callbacks (favs, recent, pin, unpin) are already gated by the existing `has_permission(uid, "view_analytics")` check at the top of `acc_dispatch()`. No new permission code needed or added.

## Test Results
| Check | Result |
|---|---|
| Root shows ⭐ Favorites / 🕐 Recent | ✅ |
| Every settings row shows `[Setting] [⭐]` | ✅ |
| Pin ⭐→★ | ✅ instant, in-place |
| Unpin ★→⭐ | ✅ instant, in-place |
| Favorites page lists pinned settings | ✅ (existing screen, verified) |
| Recent page lists visited settings | ✅ (existing screen, verified) |
| No duplicate favorites | ✅ verified via isolated logic test |
| Max 8 favorites enforced | ✅ verified — 9th pin blocked with warning until one is unpinned |
| Compact mode still works | ✅ toggle still read; pin button rendered in both layouts |

Logic was verified with an isolated unit test extracting the real `_CAT_PAGES`/`_toggle_fav`/`_is_fav`/`_nav` code (8 pins → limit warning → unpin → 9th succeeds → no duplicates, all passed). Full in-Telegram click-through was not run in this sandbox (no network / python-telegram-bot install available) — recommend a quick manual pass: open any category, tap ⭐ on a few settings, confirm instant icon flip, then check Favorites/Recent from the root menu.

## Not Modified (confirmed)
`_CB_META`, `_toggle_fav()`, recent-tracking algorithm, business logic, database, settings pages, existing callback_data values, existing handlers/routing outside the pin-refresh path described above.
