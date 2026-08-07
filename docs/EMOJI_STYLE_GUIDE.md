# Global Emoji Style Guide

One feature = one emoji, everywhere (menus, buttons, titles, messages,
notifications, popups, confirmations, errors, success — user and admin).
Source of truth: `utils/emoji_guide.py`.

| Feature | Emoji |
|---|---|
| Products | 🛒 |
| Wallet | 💳 |
| Orders | 📦 |
| Profile | 👤 |
| Support | 🎧 |
| Invite | 👥 |
| Language | 🌐 |
| Add Funds | 💰 |
| Payment History | 📜 |
| Purchase | 🛍 |
| Coupon | 🎟 |
| Notifications | 🔔 |
| Settings | ⚙️ |
| Admin Panel | 🛠 |
| Pixel Verification | 🇬 |

Shared: ⬅️ Back · 🏠 Main Menu · ❌ Cancel/Error · ✅ Confirm/Success · ⚠️ Warning · ⏳ Pending · ℹ️ Info · 🔄 Refresh

Notes
- Presentation only: no callback_data, handler, DB, payment or wallet logic changed.
- `database/`, `alembic/`, `migrations/` were intentionally left untouched (stored/seeded values).
