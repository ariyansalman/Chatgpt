# Admin Configuration Panel

Every tunable value that used to live in Python code is now a row in the
`bot_config` table and editable from the **Store Settings → 🛠 Bot
Configuration** menu inside Telegram. No more editing files and restarting.

## How it works

- Values are stored as strings and typed on read (`int`, `float`, `bool`, `text`).
- On startup the bot inserts any missing defaults (idempotent — existing
  values are never overwritten).
- Reads are cached in-process for 30 seconds to avoid a DB hit per message.
- Setting a value invalidates the cache immediately.

## The menu

```
Admin Panel
└── ⚙️ Store Settings
    └── 🛠 Bot Configuration
        ├── 💰 Payments
        ├── 📦 Delivery
        ├── 📊 Inventory
        ├── 📝 Templates
        ├── 📄 Static Pages
        └── 🔧 Operations
```

Tap a setting → shows current value + **✏️ Edit** / **↩️ Reset default**.
Toggle-type settings flip inline with a single tap.

## Available keys

| Key | Type | Default | What it controls |
|---|---|---|---|
| `payment_expiry_minutes` | int | 30 | Pending payment lifetime |
| `payment_check_interval_seconds` | int | 30 | Payment poll interval (restart to apply) |
| `auto_refund_enabled` | bool | true | Auto-refund on delivery failure |
| `auto_refund_after_minutes` | int | 5 | Delay before refund |
| `bulk_delivery_threshold` | int | 10 | Orders with **more than** N keys are sent as .txt file |
| `bulk_delivery_caption` | text | ... | Caption for bulk .txt file |
| `low_stock_threshold` | int | 5 | Products at ≤ this stock trigger the low-stock alert |
| `delivery_message_header` | text | ... | Header on successful deliveries |
| `receipt_footer` | text | ... | Footer on PDF receipts |
| `page_terms` | text | ... | Terms of service |
| `page_faq` | text | ... | FAQ page |
| `page_about` | text | ... | About page |
| `maintenance_mode` | bool | false | Blocks all non-admin traffic when ON |
| `maintenance_message` | text | ... | Shown to users during maintenance |

## Escape hatches

- **`/cancel`** — global command; escapes any stuck conversation and clears
  in-progress input.
- **Schema auto-fix** — on every startup the bot compares the live DB to
  the ORM and adds any missing columns. Old databases from earlier phases
  are healed on boot; no manual migration required.
- **`safe_conversation`** decorator — wraps every admin conversation step;
  crashes are logged with a real message and the user is returned to a
  clean state instead of "Oops! Something went wrong".

## Adding a new tunable

1. Add a row to `DEFAULTS` in `utils/bot_config.py`.
2. Read it wherever it's used: `cfg.get_int("your_key", fallback)`.

That's it — no menu code, no handler wiring. It appears in the correct
category automatically on next restart.
