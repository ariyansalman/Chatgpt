# Multi-Admin Roles + OTP 2FA

The bot now supports multiple admins across three tiers, each gated behind
Telegram-native OTP two-factor login. No SMS/e-mail provider needed — the
bot DMs the code to the admin's own chat.

## Roles

| Role | Typical use |
|---|---|
| `super_admin` | Store owner / co-owner. Every permission, always. Can add/remove/re-role other admins. |
| `moderator` | Day-to-day manager: products, orders, users, broadcasts, analytics. Cannot touch payment gateway credentials or bot settings. |
| `support_staff` | Front-line support: orders/tickets only. No product, user, broadcast, or settings access. |

The Telegram ID in `ADMIN_TELEGRAM_ID` (`.env`) is **always** an implicit,
unremovable `super_admin` — even before anything is added to the database —
so the store owner can never be locked out.

## Permission flags

Each admin row (`admin_roles` table) carries independent boolean flags, so a
role's defaults can be fine-tuned per person:

`manage_products` · `manage_orders` · `manage_users` · `manage_broadcasts` ·
`manage_payments` (gateway credentials/manual payment methods) ·
`view_analytics` · `manage_settings` · `manage_admins` (add/remove other admins)

Default grants per role live in `ROLE_DEFAULT_PERMISSIONS` in
`database/models.py`.

## Logging in (2FA)

```
/admin_login   → bot DMs a 6-digit code, expires in 5 minutes
<reply with the 6 digits>
/admin_logout  → ends your session early
```

A verified session lasts **12 hours**, after which any admin action prompts
`/admin_login` again. Codes are stored only as a SHA-256 hash — never in
plaintext — with a 30-second resend cooldown and a 5-attempt lockout.

`/admin` itself is the natural login gate: any registered admin can run the
command, but the dashboard only renders once `/admin_login` has been
completed.

## Managing admins (super_admin only)

```
/admin_add <telegram_id> <super_admin|moderator|support_staff>
/admin_role <telegram_id> <role>      (alias of /admin_add — re-roles)
/admin_remove <telegram_id>           (deactivate, doesn't delete history)
/admin_list                           (any admin can view the roster)
```

Adding someone tries to DM them with a welcome + login instructions; if they
haven't started the bot yet, the assignment still succeeds, they'll just need
to send `/start` first.

## How enforcement works in the codebase

- **`database/models.py`** — `AdminRole` model (role + 8 permission flags + OTP fields). New table, auto-created on next startup (`Base.metadata.create_all`); `migrations/v10_admin_roles.py` / `alembic/versions/20260712_admin_roles.py` exist for explicit/parity rollouts.
- **`utils/permissions.py`** — the whole system: `get_admin()`, `has_permission()`, `is_admin()` (backward-compatible), OTP generate/verify, and decorators `require_permission(...)`, `require_role(...)`, `require_2fa`.
- **`utils/helpers.py`** — the original `is_admin()` now delegates to `utils.permissions.is_admin()`, so all pre-existing `is_admin(...)` call sites and the `@admin_only` decorator keep working unchanged (now role-aware: true for any active tier).
- **Every `handlers/admin_*.py`** — the ~135 existing `if not is_admin(update.effective_user.id):` inline checks were mechanically upgraded to `if not has_permission(update.effective_user.id, "<permission>"):`, mapped per file/function to the closest-fitting permission (e.g. `admin_users.py` → `manage_users`, `admin_payment_methods.py` → `manage_payments`, `admin_broadcast_center.py` → `manage_broadcasts`). **`has_permission()` also requires a verified 2FA session** by default, so this single change enforces both role AND 2FA across the whole admin surface without touching every function individually. `admin_dashboard.py` was intentionally left on the plain `is_admin` check — it's the base menu any logged-in admin tier should see.
- **`handlers/admin_auth.py`** — `/admin_login`, `/admin_logout`, `/admin_list`, `/admin_add`, `/admin_role`, `/admin_remove`.
- **`bot.py`** — registers the login conversation + roster commands.

### Adding a new permission-gated handler
```python
from utils.permissions import require_permission

@require_permission("manage_products")
async def my_new_admin_handler(update, context):
    ...
```
or, inline (matches the existing house style):
```python
from utils.permissions import has_permission

if not has_permission(update.effective_user.id, "manage_products"):
    await query.answer("⛔ Access denied.", show_alert=True)
    return
```

### Auditing
Every login, admin add/remove, and (via the pre-existing `utils/audit.py`)
privileged action continues to be recorded in `admin_audit_logs`, now
tagged with whichever admin performed it — useful for tracing who did what
across a multi-admin team.

## Review recommended

The per-file → per-permission mapping (see `FILE_PERMISSION_MAP` used during
the migration, listed here for reference) is a best-fit based on each file's
purpose. Two mixed-purpose "hub" files — `admin_handlers.py` and
`admin_conversations.py` — were mapped **per function** by keyword instead of
one blanket permission, since they cover several domains (products, users,
orders, settings, broadcasts) in one file. Skim those two if you want to
double check a specific handler landed on the permission you'd expect —
tightening any individual check is a one-line edit (swap the string literal
passed to `has_permission`).
