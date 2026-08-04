# Admin Panel Redesign — Premium Marketplace Control Center

**Scope of this pass:** `handlers/admin_control_center.py` only — the single
source of truth for admin navigation (same role `menu_registry.py` plays for
the user Main Menu). No handler, service, model, migration, or callback was
touched anywhere else in the codebase. Every `callback_data` value that
existed before still exists, unchanged — only *which category it's grouped
under* changed. Verified: all 97 original entries are still reachable
(`gse:menu` moved from a category page onto the root panel's 🔍 Global
Search button instead of being deleted).

## What changed

- **12 categories → 17**, matching your spec: split Analytics out of
  Dashboard, split Coupons and Referral out of Marketing, split Appearance
  out of "UI & Menu", split Support out of Users, split Backup out of
  System.
- **Dashboard is now KPIs + Quick Actions only** — Live Dashboard (revenue,
  orders today, active/online users, wallet balance, total products,
  failed payments all render inside that one screen via the existing
  `dashboard_widgets` service), plus 1-tap shortcuts to Low Stock, Pending
  Orders, Pending Deposits, and Open Tickets, plus Recent Activity.
- **Root panel** rebuilt as a 2-column, 17-category grid + a utility row
  (Favorites/Pinned, Recent, Global Search, Search Settings, Maintenance
  toggle, Exit Admin) — all of which already existed as working features.

## Gaps found (flagged, not invented)

Nothing below was faked with a dead button. Where a requested item has no
backing feature, it's simply not listed:

- **Font Style** — no control exists anywhere in the codebase.
- **Auto Reply / Canned Replies** (Support) — not implemented.
- **Support Categories** — a category picker exists in the ticket flow, but
  it isn't admin-editable yet.
- **Banner Manager / Push Notifications** (Marketing) — no standalone
  screens; banners are set per-flash-sale, and outbound messages go through
  Broadcast / Notification Center.
- **Product Badges** — no dedicated list screen; "Featured" is a per-product
  toggle reached from the Product List, so that's where the Appearance/
  Products entry for it points.

## Full new → old mapping

See `handlers/admin_control_center.py` — every category's comment block
explains exactly what moved and why.
