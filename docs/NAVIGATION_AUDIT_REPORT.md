# Admin Panel Navigation Audit — Report

**Scope respected:** no changes to business logic, payment logic, wallet
logic, order logic, database schema, APIs, routes, permissions, or
security. Every change below is either (a) a new, additive utility
module, or (b) a fix to *where a screen sends the admin when they tap
Back/Cancel* — never *what that screen does*.

---

## 1. What was actually broken (verified, not guessed)

The admin panel has **556 callback routes** across **~150 handler
files**. Before auditing that whole surface by hand, I checked which
Back/Cancel destinations are computed *dynamically per-screen* vs.
*shared by one generic function across multiple, unrelated screens* —
because only the second pattern can produce "random menu switching":
a single hardcoded destination can't be wrong for the screen that
owns it, but it silently becomes wrong the moment a second, different
screen starts sharing that same handler.

Scanning `bot.py`'s handler registrations for exactly that shape found
one real instance:

**`handlers/admin_conversations.py :: cancel_conversation`** was
registered as the Cancel/fallback handler for **eight unrelated
conversations** — creating/editing a product, creating/editing a
category, creating/editing a subcategory, and four config-value
flows (support username, channel username, welcome message, store
logo) — and it **always** hardcoded the same destination:
`create_admin_category_menu_keyboard()`.

Concretely, before the fix:
- Cancelling out of **Edit Product** → landed on the **Category
  menu**, not Products.
- An unrelated stray tap while inside the Edit Product conversation
  (its fallback has no pattern filter, so it catches *any* callback)
  → same wrong redirect.
- The four config-value flows would do the same, though those four
  entry points aren't currently wired to any visible button (see §4)
  so this half was latent rather than user-visible today.

This is precisely the "no random menu switching" / "don't reuse
generic back callbacks across unrelated menus" failure mode described
in the audit brief, and it was reproducible by reading the code, not
inferred.

---

## 2. Fix: dynamic destination tracking (new, additive)

Added `utils/nav_state.py` — a small per-user navigation-state helper
built on `context.user_data` (the same store PTB already uses for
conversation state). It does **not** replace any existing
callback_data, route, or menu-renderer. It adds:

- `set_conversation_home(context, screen_cb)` / `pop_conversation_home(...)`
  — lets an entry point record *its own* correct return screen.
- `enter_screen` / `parent_of` / `go_back` / `reset` — a general
  breadcrumb stack, available for any handler that wants to stop
  hardcoding a Back destination and track it dynamically instead, per
  the audit brief. (Infrastructure is in place; see §5 for the
  honest scope of what's wired up to it today vs. left as follow-up.)
- `render(...)` — a drop-in replacement for
  `query.edit_message_text(...)` that always edits the existing
  message when one exists (never opens a second message thread) and
  only sends a new one when there is genuinely nothing to edit.

**`cancel_conversation` was rewritten** to read back whichever home
screen the conversation's entry point recorded, and redirect there by
calling that screen's own real handler (`admin_products_callback`,
`admin_manage_categories_callback`, `admin_settings_callback`,
`admin_menu_callback`) via the project's existing `with_data()` proxy
(`utils/update_proxy.py`, unchanged) — so the destination is rendered
by the same code path a normal button press would use (correct badge
counts, live data, etc.), not a hand-rolled guess. If no home was
recorded (e.g. an entry point nobody tagged yet), it falls back to the
exact previous behavior, so nothing regresses.

**10 entry points were tagged** with their correct home in one line
each (`nav_state.set_conversation_home(context, "...")`), pure
navigation metadata, zero logic change:

| Entry point | Correct home |
|---|---|
| `create_product_start`, `edit_product_start` | `admin_products` |
| `create_category_start`, `create_subcategory_start`, `edit_category_start`, `edit_subcategory_start` | `admin_manage_categories` |
| `config_support_username`, `config_channel_username`, `config_welcome_message`, `config_store_logo` | `admin_settings` |

All touched files pass `py_compile` cleanly.

---

## 3. Systemic check: is this pattern hiding anywhere else?

Scanned every `CallbackQueryHandler(handler, pattern=...)` registration
in `bot.py` for any other single handler function bound across many
*unrelated*-looking patterns (the same shape as the bug in §1). One
other hit: `admin_conversations.product_type`, bound to
`cancel_product`, `ptype:`, `ptype_page:`, `type_` — but those are all
states of the *same* product-creation conversation (a normal
multi-pattern state handler, not a shared cross-menu fallback). No
second instance of the actual bug class was found.

---

## 4. Also flagged (not touched — out of scope / no live impact)

- **Dead entry points:** `admin_support_username`, `admin_channel_username`,
  `admin_welcome_msg`, `admin_store_logo` are registered as conversation
  entry points in `bot.py` but no menu in the current codebase renders
  a button pointing at them (superseded by the newer
  `admin_config_handlers.py` system). They're unreachable today, so
  the fix in §2 covers them defensively but there's no user-facing bug
  to verify. Recommend a follow-up pass to either wire them into the
  Settings menu or remove the orphaned conversation registrations.

---

## 5. Full-panel back-button inventory (honest scope statement)

The panel has **480 hand-written "Back"-style buttons across 89
files**, each with its own literal `callback_data` string. I read
every one of them well enough to extract its destination(s) —
`docs/NAV_AUDIT_BACK_BUTTON_INVENTORY.csv` lists, per file: how many
Back buttons it has, how many *distinct* destinations they point to,
a sample of those destinations, and rough edit/send counts as a
duplicate-message risk signal.

**What that inventory shows:** the overwhelming majority of these 480
buttons already point to a specific, correct, per-screen parent (e.g.
`bcm:menu`, `flm:menu`, `pct:templates:0`) — they're hardcoded, but
not *wrong*, because each one is only ever used by the one screen that
defines it. The failure mode this audit was asked to find — a
destination that's wrong because it's shared across unrelated screens
— was isolated to the one case fixed in §1‑2.

Converting all 480 of these to route through the new dynamic
`nav_state` stack instead of a literal string is real, valuable
follow-up work (it would make the panel resilient to future menu
reshuffles the way this bug shows static strings aren't), but doing
it across 89 files in one pass isn't something I can respond "done and
verified" to without meaningfully raising regression risk on a
production admin panel — most of the 3,063 `callback_data` sites in
this codebase are extremely intertwined with each screen's own
handler code and would each need to be individually confirmed after
the change, not just pattern-replaced. I've deliberately scoped this
pass to the fix I could verify was both real and safe, and left the
inventory as a ready-made, prioritized worklist (already sorted by
Back-button count, worst files first) for whoever picks up the next
pass — happy to continue through that list with you file-by-file if
you'd like to keep going now instead.

---

## 6. Verified unchanged

- Every `callback_data` string, route pattern, and handler
  registration in `bot.py` — untouched except the (also-unchanged)
  routing of `cancel_conversation`'s *internal* redirect logic.
- All business/payment/wallet/order logic, `database/models.py`, and
  every `has_permission(...)` check — untouched.
- All edited/added files pass `py_compile` with no syntax errors.
