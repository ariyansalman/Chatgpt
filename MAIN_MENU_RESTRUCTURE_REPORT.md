# Main Menu Restructure Report

## Scope of this pass

This bot had already been through a partial version of this exact redesign
(evidenced by version-4 comments in `utils/menu_registry.py`, the existing
`⚙ Settings` sub-menu, and the existing Invite/Support screens). This pass
**audited every screen reachable from the 7 target Main Menu buttons**,
completed the parts of the redesign that were still outstanding, and removed
one confirmed duplicate menu.

**Explicitly out of scope, per your "DO NOT CHANGE" list:** payment gateway
logic, database schema, business logic, wallet/order/product logic, and all
`callback_data` values that already existed. The Admin Panel (`admin_*.py`,
reached via the admin-only 🛠️ Admin Panel button) was also left untouched —
it's a separate control surface from the user-facing Main Menu this brief
describes, and touching ~90 admin screens wasn't part of "redesign the Main
Menu." One exception is noted below where the Admin Panel uses the literal
word "Deposit" in a settings label.

---

## 1. Main Menu — restructured to the exact 7 buttons requested

`utils/menu_registry.py` (`DEFAULT_MENU_ITEMS`) is the **single source of
truth** for the Main Menu — every other file renders whatever this registry
returns, so this was the one place that needed a structural edit:

| Row | Before | After |
|---|---|---|
| 1 | 🛍 Shop (full width) | 🛒 Shop (full width) |
| 2 | 👛 Wallet · 💳 Deposit | 👛 Wallet · 📦 Orders |
| 3 | 📦 Orders · 👥 Invite | 👥 Invite · 🎧 Support |
| 4 | 🎧 Support · ⚙ Settings | 🌐 Language · ⚙️ Settings |
| 5 | 🛠️ Admin Panel (admin-only) | 🛠️ Admin Panel (admin-only, unchanged) |

- **💳 Deposit removed as a standalone button** — folded into 👛 Wallet as
  its "Add Funds" action (same `topup` callback, unchanged).
- **🌐 Language restored as a top-level button** — it had been moved inside
  Settings in a prior pass; it's now back on the Main Menu itself, exactly
  as your spec requires, using the same `language_menu` callback/handler.
- `MENU_DEFAULTS_VERSION` was bumped (4 → 5) so any bot already running in
  production automatically re-syncs to this new layout on next startup,
  instead of an admin's old saved customizations silently overriding it.
- Every `callback_data` value in the registry is unchanged.

---

## 2. 👛 Wallet — now the complete financial center

`handlers/wallet_handlers.py`:
- Added **🎁 Redeem Coupon** button → routes to the existing gift-card/coupon
  redemption flow (`gc:redeem`), which already credits wallet balance and
  already returned to Wallet — it just wasn't surfaced on the Wallet screen
  itself yet.
- Added **💸 Withdraw** button, shown only when withdrawals aren't disabled
  (reads the existing `withdrawal_approval_status` config) → routes to the
  existing withdrawal request flow (`rd:withdraw`).
  - **Known nav caveat:** this flow's own Back/Cancel buttons return to 👥
    Invite (`refer`), not to Wallet, because it's fundamentally a
    referral-earnings withdrawal flow reused here. Rewiring its internal
    back-targets to be context-aware would touch a shared conversation
    handler used from two entry points, which felt like more risk than this
    pass should take on silently — flagging it instead of masking it.
- Renamed **"Total Deposited" → "Total Added"** in the wallet card text.
- No changes to balance/spend calculation logic, only display text and the
  two added buttons.

---

## 3. ⚙️ Settings — cleaned to only real, functional settings

`handlers/settings_handlers.py`: removed the 🌐 Language row (now top-level,
see #1). Settings now shows exactly: Notifications, Currency, Privacy &
Security, Terms of Service, About Store — all five already had real, working
handlers; nothing dead was found here to remove.

---

## 4. 🎧 Support — duplicate menu found and removed

Audit turned up a genuine duplicate: `handlers/user_handlers.py` contained a
second, older Support screen (`support_callback`, "☎️ My Shop is Open 24/7")
backed by `utils/keyboards.py:create_support_keyboard`. It was **never
registered against any `callback_data`** — dead code left over from an
earlier menu version, invisible to users but a maintenance trap. Removed
both, along with the now-unused import.

Its one useful piece — the 📢 Channel link — was merged into the real,
currently-used Support Center (`support_center_callback` /
`create_support_center_keyboard`), which now shows: 🎫 Open Ticket, 📂 My
Tickets, ❓ FAQ, 📢 Channel (new), 📞 Contact Support, 🏠 Main Menu.

---

## 5. 📦 Orders / 👥 Invite — audited, already correctly scoped

- **Orders** (`order_history_callback`): confirmed it contains only
  order/purchase content (order list → per-order detail, which includes
  license keys/downloads/delivered files inline) with no Wallet or Payment
  actions leaking in. No changes needed.
- **Invite** (`referral_handlers.refer_callback` — the file actually wired
  to the `refer` callback; `referral_dashboard.py` is the separate *admin*
  referral console, not user-facing): already shows the referral link,
  live stats, total earnings, and a "📜 Referral History" button. No
  changes needed.

---

## 6. "Deposit" → "Add Funds" terminology pass

Applied across every user-facing string found in `i18n/locales/en.json` and
`services/payment_ui.py` (the shared payment-status message renderer used
across all gateways), plus two hardcoded buttons in `payment_handlers.py`
and `cart_handlers.py`:

| Old | New |
|---|---|
| Deposit (main menu button) | Add Funds |
| Total Deposited | Total Added |
| Deposit Now / Top Up Wallet | Add Funds |
| Deposit History | Payment History |
| Deposit Successful | Payment Successful |
| Pending Deposit | Pending Payment |
| Cancelled Deposit | Payment Cancelled |
| Deposit ID | Payment ID |
| Deposit Request | Payment Request |
| Create New Deposit | Add Funds |
| "please top up and try again" | "please add funds and try again" |

**Only display strings changed** — variable/function names
(`deposit_id`, `_display_deposit_id`), config keys
(`minimum_deposit_enabled`), and every `callback_data` literal (`topup`,
`deposit_cancel`, `cancel_pending_deposit`, etc.) were left exactly as-is,
so nothing about how the bot processes payments changed.

**Known limitation:** the 8 non-English locale files (`bn.json`, `ru.json`,
etc.) still contain the old "Deposit" wording for these same keys —
retranslating them accurately wasn't something I could respons­ibly do
without a translator, so English is fully consistent and the others are
flagged as translation debt rather than silently left half-done.

**Left alone by design:** the Admin Panel's "Minimum Deposit" setting
(`handlers/admin_deposit_settings.py`) still says "Deposit" — that's an
admin-only configuration screen, out of this redesign's scope per the notes
above; happy to rename it too on request.

---

## 7. Navigation

- Every submenu touched in this pass already had (or now has) a working
  Back button that returns to its actual parent, and Main Menu buttons
  return to Home. The one exception is the Withdraw nav caveat in §2, which
  is called out rather than hidden.
- No `callback_data` values were changed anywhere in this pass.

## Files changed

`utils/menu_registry.py`, `handlers/wallet_handlers.py`,
`handlers/settings_handlers.py`, `handlers/support_handlers.py`,
`handlers/user_handlers.py`, `handlers/payment_handlers.py`,
`handlers/cart_handlers.py`, `utils/keyboards.py`, `services/payment_ui.py`,
`i18n/locales/en.json`.

All edited Python files were syntax-checked (`py_compile`) and the edited
JSON locale file was validated before packaging.
