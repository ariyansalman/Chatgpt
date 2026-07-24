# V9 — Premium Admin Control Center Upgrade

Non-destructive, additive upgrade of the existing admin panel into a
professional control center. Preserves every existing user, product, key,
order, payment, coupon, loyalty, referral, dispute, and configuration row.

## What changed

### New tables (Alembic revision `20260706_admc`)
| Table                       | Purpose                                          |
|-----------------------------|--------------------------------------------------|
| `wallet_ledger`             | Append-only history of every wallet mutation.   |
| `promotions`                | Scheduled wrapper over existing coupons.         |
| `admin_notification_prefs`  | Per-admin event toggles.                         |
| `low_stock_alert_state`     | Edge-trigger state for the low-stock notifier.   |

No existing table, column, enum, or index is renamed or dropped. The
migration re-uses the same `_has_table` guard style as the earlier
`20260705_premium_core` revision and is idempotent.

### New handler modules (`handlers/`)
- `admin_control_center.py` — Root panel and callback dispatcher for the
  new `acc:*` namespace.
- `admin_wallets.py` — Ledger view, credit/debit conversation.
- `admin_notifications.py` — Toggle admin notification events, send a
  test message.
- `admin_promotions.py` — Read-only view of scheduled promotions
  (create/edit uses the existing coupon flow).
- `admin_audit.py` — Paged read-only audit log viewer.
- `admin_integrations.py` — Presence/health of CryptoBot / Telegram
  Payments / Webhook config (never prints secrets).
- `admin_system_tools.py` — DB health, ORM schema-drift report, job list.

### New services
- `services/wallet.py` — Single choke-point for wallet credit / debit /
  adjust. Writes `wallet_ledger` in the same transaction.
- `services/notifications.py` — Fan-out helper that reads per-admin
  preferences and the `notif_*` `BotConfig` fallbacks.

### Bot wiring (`bot.py`)
- Added a `/panel` command that opens the Control Center directly.
- Added a `CallbackQueryHandler` on `^acc:` dispatched by
  `admin_control_center.acc_dispatch` — one dispatcher, no pattern
  collisions.
- Added a `ConversationHandler` for wallet credit/debit adjustments
  (entry points `acc:wal:credit:<uid>` and `acc:wal:debit:<uid>`).
- Added a `run_repeating` low-stock notifier job (interval driven by
  `low_stock_check_interval_minutes`, edge-triggered via
  `low_stock_alert_state`).
- The language switching callbacks (`^language$`, `^setlang_`) are now
  routed to friendly no-op stubs that answer "This bot is English-only"
  so the existing user main menu stays untouched.
- Every previous `admin_*` handler and conversation remains registered
  exactly as before (compat shims).

### English-only cleanup
- `utils/i18n.t()` and `get_user_language()` always return English.
- `set_user_language()` is a no-op.
- `handlers/language_handlers.py` reduced to no-op shims.
- Existing English strings and the `LANGUAGES` list are preserved so any
  keyboard that references them keeps rendering.
- The `users.language` column is preserved to avoid data loss.

### New `BotConfig` keys and categories
Categories `🔔 Notifications`, `💰 Wallets`, `🎁 Promotions`, `🛠 System`
appear automatically the first time `seed_defaults()` runs.

| Key                                  | Type   | Default |
|--------------------------------------|--------|---------|
| `notif_new_order`                    | bool   | true    |
| `notif_manual_payment`               | bool   | true    |
| `notif_dispute`                      | bool   | true    |
| `notif_low_stock`                    | bool   | true    |
| `notif_refund`                       | bool   | true    |
| `notif_ticket_reply`                 | bool   | true    |
| `low_stock_check_interval_minutes`   | int    | 30      |
| `wallet_max_manual_adjust`           | float  | 1000.0  |
| `wallet_require_reason`              | bool   | true    |
| `promotions_enabled`                 | bool   | true    |
| `admin_2step_confirm_destructive`    | bool   | true    |
| `dashboard_default_range_days`       | int    | 7       |
| `audit_retention_days`               | int    | 180     |

## Callback namespace

Short, namespaced, always ≤ 64 bytes.

```
acc:root                              root Control Center panel
acc:sec:<section>                     open a section
    dashboard | wallets | promotions | notifs | audit |
    integrations | system
acc:wal:view:<user_id>                render user's wallet ledger
acc:wal:credit:<user_id>              conversation entry (credit)
acc:wal:debit:<user_id>               conversation entry (debit)
acc:notif:tgl:<event>                 toggle notification preference
acc:notif:test                        send a test admin message
acc:audit:page:<n>                    audit log pagination
acc:sys:health | drift | jobs         system diagnostics
```

Existing `admin_*` callbacks (`admin_products`, `admin_orders`,
`admin_payment_methods`, `admin_coupons`, `admin_loyalty`,
`admin_referral_reward`, `admin_view_disputes`, `admin_tickets`,
`admin_broadcast`, `admin_analytics`, `admin_bot_config`,
`admin_maintenance_toggle`) are unchanged and reachable from the new
grid.

## Permission architecture

The project has a single admin (`settings.ADMIN_TELEGRAM_ID`). Every new
handler enforces this via `utils.helpers.is_admin` before mutating
state. No role table was introduced — matches the existing project
posture. Adding a role table can be done later as a purely additive
follow-up.

## Wallet safety

- All new admin-triggered balance changes go through
  `services/wallet.py`. Each write:
  1. Locks the user row (`FOR UPDATE` on PostgreSQL).
  2. Refuses to go negative.
  3. Appends a `wallet_ledger` row in the same transaction.
- Admin adjust is capped by `wallet_max_manual_adjust` and requires a
  free-text reason when `wallet_require_reason` is on.
- Every admin adjust also writes an `AdminAuditLog` row
  (`action=wallet.adjust`).
- Legacy paths that still write `User.wallet_balance` directly are
  untouched to guarantee zero behavior drift; migrate them one-by-one
  in future turns to route through `services.wallet`.

## Static checks performed

- `python -m compileall -q .` — clean.
- `python -c "import bot"` — clean (with DB env vars set).
- `alembic upgrade head` — clean on an existing populated SQLite
  (populated via `init_db()` then stamped at `20260705_pc`).
- `python -m unittest tests.test_wallet_service` — passes
  (credit/debit/ledger + insufficient-balance guard).

## Replit commands

```bash
# 1. install
pip install -r requirements.txt

# 2. initialize / migrate the DB
python -c "from database import init_db; init_db()"
alembic upgrade head

# 3. static compile check
python -m compileall -q . && python -c "import bot"

# 4. start the bot (polling)
python bot.py
```

Set `BOT_TOKEN` and `ADMIN_TELEGRAM_ID` in the Replit secrets tab. Set
`DATABASE_URL` if not using SQLite. The Replit run command is
`python bot.py`.

## VPS checklist

1. `git pull` (or upload the new archive) then
   `pip install -r requirements.txt`.
2. Back up the database (`deploy/backup.sh`).
3. Run migration:
   `alembic upgrade head` (idempotent).
4. Restart the bot service (`docker compose restart` or `systemctl
   restart your-bot.service`).
5. `/admin` opens the new Control Center. Confirm 📊 Dashboard,
   🎧 Support, 💳 Payments and other tiles work.
6. In Bot Settings → 🔔 Notifications, verify the new toggles render.
7. Open Support → send a manual payment from a test account to exercise
   the notification fan-out.
8. Trigger the schema-drift report in 🛠 System Tools → 📐 Schema drift.
   Expect: "🟢 In sync with ORM metadata".

## Remaining risks (require live Telegram / payment testing)

1. Live CryptoBot webhook — untested from sandbox.
2. Live Telegram Payments card checkout (PreCheckoutQuery /
   SUCCESSFUL_PAYMENT) — untested from sandbox.
3. Wallet ledger integration for the existing top-up and purchase
   paths in `handlers/payment_handlers.py` — new mutations go through
   `services.wallet` but the legacy direct-assignment paths were left
   untouched to guarantee non-destructive behavior. Migrating them is a
   one-file diff in a follow-up turn.
4. Multi-item cart checkout in `handlers/cart_handlers.py::cart_checkout`
   remains a "friendly ready to check out" screen (documented in
   `PREMIUM_CORE.md`). The Control Center does not change that flow.
5. Reservation / consume / release wiring in the single-item purchase
   path (per `PREMIUM_CORE.md`) was left as-is to preserve current
   behavior for production installs; adding it is additive and safe in
   a follow-up.
