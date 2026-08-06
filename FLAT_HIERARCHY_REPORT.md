# Admin Panel — Flat Hierarchy Pass

**Scope:** navigation only, across `handlers/admin_control_center.py`,
`utils/menu_builder.py`, `utils/keyboards.py`, and the Back-button target in
3 module files. No business logic, database schema, payment logic, or
handler behavior was changed — only which screen a button/Back-press points
to.

## 1. Every module's *home* screen is now 3 taps from the Admin Dashboard

⚡ Home (1) → category page (2) → module home screen (3), for all 96 unique
leaf screens across the 17 categories. This was already true after the
previous pass and still holds — verified again by walking the full,
evaluated `_CAT_PAGES` structure, not just spot-checked.

**Note on "3 taps" for record-level actions:** editing one specific field on
one specific record (e.g. one exchange-rate pair's refresh interval, one
payment method's name) will always take more than 3 taps in any admin panel,
Telegram or otherwise — you first have to pick *which* record. That's normal
SaaS drill-down (list → detail → edit), not a flat-hierarchy violation. The
3-tap rule was applied to *reaching every feature's home screen*, which is
where it matters.

## 2. "Settings inside Settings" — found and fixed

`admin_settings` (Store Settings) rendered a *second*, nested settings
screen (`admin_settings_menu`, 14 rows) containing 4 items that already had
their own canonical home elsewhere in the panel:

| Removed from the nested screen | Already lives at |
|---|---|
| 🎟 Coupons / Promo Codes | Coupons category → ✂️ Coupons |
| 💱 Display Currency | Store category → 💱 Currency |
| 🎁 Loyalty Program | Marketing category → 🎁 Loyalty Points |
| 🛠 Bot Configuration | System category → ⚙️ Bot Settings |

Removed all 4 from `utils/menu_builder.py`'s `admin_settings_menu`
registration — nothing was deleted, each item's *other* entry point (the
canonical one) is untouched and still fully functional.

## 3. Single-parent rule — 2 real violations found and fixed

- **`acc:sec:wallets`** (Wallet Manager) was listed under both Payments and
  Users. Now lives only under **Payments** — removed from Users (noted in
  its tagline).
- **`acc:sec:pfaq`** (Product FAQ) was listed under both Products and
  Support. Now lives only under **Products** — removed from Support (noted
  in its tagline; a *general* store FAQ, as opposed to per-product FAQ,
  doesn't exist in the codebase — flagged as a gap, not fabricated).
- **`admin_settings`** itself had 3 labels pointing to it split across two
  categories (Appearance: Store Logo / Welcome Message; Store: Store Name).
  It's one screen, so it now has one parent — **Store** — with all three
  fields described in that single entry's label.

**Not counted as violations:** Dashboard's Quick Actions (Low Stock,
Pending Orders, Pending Deposits, Open Tickets) intentionally duplicate a
canonical entry that also exists in its real category. That's the same
1-tap-shortcut pattern approved in the original navigation audit — it's
additive, the long-form path still works, and it's clearly labeled as a
shortcut rather than a second silent parent.

Ran an automated check over the full evaluated category structure after
these fixes: **0 remaining multi-parent callbacks** (excluding the 4
intentional Dashboard shortcuts).

## 4. Back button correctness — found and fixed

- `admin_settings_menu`'s own Back button pointed to `admin_menu` — the
  **user-facing bot main menu**, not the admin panel. Fixed to
  `acc:cat:store`, its actual immediate parent.
- `create_admin_payment_methods_menu_keyboard`'s Back button pointed to
  `acc:root` (the very top of the panel), skipping past its Payments
  category page. Fixed to `acc:cat:payments`.
- `rd_admin_menu` (Referral Program)'s Back button also skipped straight to
  `acc:root`. Fixed to `acc:cat:referral`.
- `flm_menu` (Digital Delivery), `fsm_menu` (Flash Sales), and `anc_menu`
  (Notification Center) had the same "skip to acc:root" pattern on their
  own top-level screen. Fixed to `acc:cat:products`, `acc:cat:marketing`,
  and `acc:cat:notifications` respectively.

### Scope note — this pattern is wider than these 6 fixes

A grep for `callback_data="acc:root"` across `handlers/` and `utils/`
turns up **76 occurrences in 54 files**. The 6 above are the ones I could
verify by tracing code to confirm they sit on a module's *top-level* entry
screen (the one directly opened from a category page) — those are fixed
with certainty and no guesswork.

The remaining ~70 occurrences sit on screens *nested inside* a module (a
record list, a detail view, a confirmation screen). Blindly repointing all
of those to the category page would often be **wrong in the other
direction** — a detail screen's immediate previous menu is usually the
module's own list screen, not the category page two levels further up — so
fixing them correctly requires tracing each module's actual internal screen
sequence individually rather than a bulk find-and-replace. That's a real
follow-up piece of work I didn't want to guess through. The codebase
already has a per-user navigation-stack utility built for exactly this
(`utils/nav_state.py`, `parent_of()`) that isn't yet wired into most of
these screens — that's the natural mechanism to finish this with rather
than hardcoding each one.

## What's included

`handlers/admin_control_center.py`, `utils/menu_builder.py`,
`utils/keyboards.py`, `handlers/admin_file_license_manager.py`,
`handlers/admin_flash_sale_manager.py`,
`handlers/admin_notification_center.py`, `handlers/referral_dashboard.py`.
