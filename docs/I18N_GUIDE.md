# i18n — What's built & how to extend it

**9 languages supported:** 🇬🇧 English · 🇧🇩 বাংলা · 🇻🇳 Tiếng Việt ·
🇷🇺 Русский · 🇨🇳 中文 · 🇫🇷 Français · 🇩🇪 Deutsch · 🇸🇦 العربية ·
🇮🇩 Bahasa Indonesia.

## Infrastructure (done, production-ready)

- `i18n/__init__.py` — `t(key, lang, **kwargs)` translation lookup with
  automatic fallback (missing translation → en → the raw key, never
  crashes), `get_user_language(telegram_id)` / `set_user_language(telegram_id,
  lang)` (reads/writes `User.language`, the column already added by
  `migrations/v2_add_referral_support_i18n.py`), and
  `resolve_initial_language(telegram_user)` which defaults brand-new users
  to their Telegram client's language if it's one of `SUPPORTED_LANGUAGES`,
  else English. `SUPPORTED_LANGUAGES` / `LANGUAGE_NAMES` / `LANGUAGE_FLAGS`
  are the single source of truth for which languages exist — add a language
  by adding an entry to these three plus a `locales/<code>.json` file; no
  other code needs to change. `RTL_LANGUAGES = ("ar",)` is exposed for any
  future code that needs to know direction (Telegram itself renders Arabic
  RTL automatically; nothing in this codebase hardcodes LTR).
- `i18n/locales/*.json` (en, bn, vi, ru, zh, fr, de, ar, id) — nested-namespace
  string tables (`wallet.title`, `loyalty.redeemed`, etc), all 105 keys kept
  in lockstep across every file.
- `migrations/v11_i18n_full.py` — idempotent, extends v2: ensures
  `users.language` exists and backfills any NULL/blank/unsupported value to
  `'en'`. Run once: `python -m migrations.v11_i18n_full`. (Its own
  `SUPPORTED_LANGUAGES` tuple is frozen to `en`/`bn` on purpose — it's a
  point-in-time backfill script for pre-i18n rows, not the live language
  list; the live list lives in `i18n/__init__.py`.)
- `utils/keyboards.py` — `create_main_menu_keyboard(lang, user_id)` renders
  every button in the user's language and has the **🌐 Language** button;
  `create_language_keyboard(lang)` is the picker and loops over
  `SUPPORTED_LANGUAGES`, so it automatically grew from 2 buttons to 9 with
  no code change; `create_refer_keyboard(lang, ...)` localized too.
- `/language` command **and** the 🌐 Language button (`handlers/user_handlers.py:
  language_command`, `language_menu_callback`, `set_language_callback`) —
  saves the choice to `User.language` immediately.

## Fully converted handlers (real, tested examples of the pattern)

- `handlers/user_handlers.py` — `start_command`, `main_menu_callback`,
  `currency_toggle_callback`, language picker/switcher.
- `handlers/referral_handlers.py` — `refer_callback` + the referral-reward
  DM sent to the referrer.
- `handlers/wallet_handlers.py` — `wallet_menu`, `wallet_history`,
  `wallet_currency_toggle`.
- `handlers/loyalty_handlers.py` — `loyalty_menu`, `loyalty_redeem_start`,
  `loyalty_redeem_amount` (admin-only loyalty config screens were left in
  English on purpose — see scope note below).
- `handlers/cart_handlers.py` — fully converted: `cart_view`, `cart_add`,
  `cart_inc`/`cart_dec`, `_revalidate_cart` (stock/qty error messages),
  `cart_checkout`, `cart_confirm` (idempotency guard, stock-reservation
  failures, wallet debit, every delivery-type receipt line for KEY/FILE/V11
  dispatcher products, refund-on-failure message, final success receipt,
  bulk `.txt` key-file caption). This was the largest and most complex file
  in the customer journey — checkout/payment/receipt — so it's a good
  reference for converting deeply nested logic.

## Scope note: admin panels are intentionally still English-only

"User-facing" here means customer-facing: everything a shopper sees.
The `admin_*.py` handlers (dashboard, broadcast center, product/order
management, etc.) are operator tooling, not customer-facing, so they were
left as-is. If you *do* want the admin panel bilingual too, the same
pattern applies — just also key admin strings under a `bn`/`en` namespace
like `admin.*`.

## What's not converted yet

Given the size of this codebase (30+ handler files, ~270KB of customer-facing
code alone), the remaining flows still have hardcoded English strings and
need the same treatment:

- `handlers/payment_handlers.py` (top-up, payment methods, invoices)
- `handlers/coupon_handlers.py`
- `handlers/review_handlers.py`
- `handlers/dispute_handlers.py`
- `handlers/support_handlers.py`
- `handlers/variant_handlers.py`
- `handlers/search_handlers.py`
- product/category browsing in the rest of `user_handlers.py`
  (`products_callback`, `product_detail_callback`, `order_history_callback`, etc.)

## The pattern to apply to each remaining file

1. Add the import:
   ```python
   from i18n import t, get_user_language
   ```
2. At the top of each handler, resolve the user's language once:
   ```python
   lang = get_user_language(update.effective_user.id)
   ```
3. Add the new strings to **both** `i18n/locales/en.json` and
   `i18n/locales/bn.json` under a namespace named after the file, e.g.
   `"cart": { "title": "...", "empty": "...", ... }`.
4. Replace hardcoded text with `t("cart.title", lang)`, and use
   `t("cart.item_line", lang, name=..., price=...)` for strings with
   placeholders (uses `str.format`, so use `{name}` / `{price}` in the JSON).
5. Pass `lang` into any keyboard-building helper you touch, mirroring
   `create_main_menu_keyboard(lang, user_id)`.

Happy to continue converting the remaining files in follow-up turns —
just say which one to do next (cart + checkout is usually the highest-value
one since it's the most-used flow after the main menu).
