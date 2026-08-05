# Home Welcome Message Redesign

## What changed

`handlers/user_handlers.py:_build_home_message()` is the single function
that builds the welcome text everywhere the Home screen appears —
`/start`, Main Menu, Home, Return to Home, and the post-language-change
refresh all already called this one function (confirmed by checking every
call site before editing), so this was a one-function change, not a
find-and-replace across the bot.

It now returns exactly:

```
🛍 Welcome to {shop_name}!

Premium digital products with secure payments and instant automated delivery.

💰 Wallet Balance: {balance}
```

Replacing the old admin-configurable "dashboard card" layout (title /
time-of-day greeting / balance / order count / footer, wrapped in divider
lines).

## Dynamic variables

- **`{shop_name}`** — new Bot Configuration key `shop_name` (Operations →
  Home section in the admin panel's existing generic settings editor, no
  new admin UI code needed since that screen renders whatever's in the
  catalogue). Read fresh from config on every single call — an admin
  changing it takes effect on the very next `/start` or menu tap, with no
  restart or caching lag, exactly as required. Falls back to **"Digital
  Store"** if never set.
- **`{balance}`** — unchanged from before: each call site already fetches
  the user's live `wallet_balance` from the DB and formats it in their
  configured currency (`format_amount_in` / `format_price_for_user`) before
  passing it in — none of that logic was touched. Falls back to **"$0.00"**
  if a caller ever passes an empty value. The existing "hide balance"
  privacy preference still masks it (`••••••`) exactly as it did before.

## Preserved exactly as-is

Wallet logic, user creation/lookup, order counting, currency conversion,
all `callback_data`, and the database schema were not touched — only the
text this one function returns changed, and one new key was added to the
existing generic `bot_config` key/value table (no migration; that table
already exists for admin-tunable values like the old `home_title`).

## Cleanup

The old `home_title` / `home_subtitle` / `home_wallet_label` / `home_footer`
config entries and the greeting/dashboard i18n strings they used are gone —
confirmed (by grep) that nothing else in the codebase referenced them, so
removing them avoids leaving dead, confusing settings behind in the admin
panel now that the layout they controlled no longer exists.

## Scope note

`database/models.py`'s `Settings.welcome_message` column (a separate, older
field) is intentionally untouched — it's used elsewhere purely for
receipt/invoice header branding (`utils/receipt.py`,
`services/invoice_service.py`, `handlers/account_features.py`), not for the
Home welcome message, so it's a different concern from what this brief
asked for. Happy to unify those onto the same `shop_name` value on request.
