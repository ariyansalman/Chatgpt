# Notification Event Management — Audit & Redesign

Scope: Notification Event Management inside Notification Settings
(`handlers/admin_notification_settings.py`, `services/notifications.py`).
No other admin modules' business logic was changed.

## 1. What was built

**Notification Settings → 📋 Notification Categories** now exposes the
7 requested categories, each opening its own event list with individual
☑️/⬜ toggles:

🛒 Orders · 💳 Payments · 👤 Users · 🎟 Coupons · 📦 Inventory · 💬 Support · ⚙️ System

The full catalog lives in `services/notifications.NOTIFICATION_CATALOG` —
one place both the settings UI and the delivery code read from, so the
UI can never drift from what actually gets sent.

Toggling an event writes to the *same* store `notify_admins()` reads
(`AdminNotificationPref` for per-admin events, a `BotConfig` key for
global-only events) — there is exactly one source of truth per event,
not a separate "settings" copy.

No minor UI action (menu opened, wallet viewed, profile viewed, FAQ,
language change, product browsing) triggers an admin notification —
verified by search; none call `notify_admins`.

## 2. Bugs found and fixed

1. **Delivery mode was cosmetic.** `notify_admins()` — the function every
   real event goes through — never read `notif_settings_mode` or the log
   channel. "Log Channel Only" and "Admin + Log Channel" appeared to save
   correctly but every real notification still only DM'd the admin.
   **Fixed:** `notify_admins()` now resolves the configured mode/channel
   and delivers accordingly, per-event-toggle still applies to the
   channel copy too. Falls back to "admin only" if the channel isn't
   configured/verified, so a half-finished setup can't blackhole alerts.

2. **Multi-admin preferences were dead.** `AdminNotificationPref` is a
   per-admin table, but the sender only ever messaged the single
   `settings.ADMIN_TELEGRAM_ID`. A second admin could configure their own
   preferences and never receive anything. **Fixed:** the recipient list
   now includes everyone from `utils.permissions.list_admins()`.

3. **`fraud_alert` notifications were silently dropped.**
   `services/fraud_detection.py` fired `notify_admins(..., event="fraud_alert", ...)`,
   but `fraud_alert` was never registered in the event catalog, so the
   opt-in check failed for every admin before `bot.send_message` was ever
   called — a real, high-severity alert type that never actually sent.
   **Fixed:** registered under the new ⚙️ System category.

4. **`services/health_monitor.py` bypassed the entire notification
   system.** API/webhook health alerts were sent straight to
   `settings.ADMIN_TELEGRAM_ID`, ignoring preferences and the mode/channel
   setting entirely. **Fixed:** routed through `notify_admins()` under a
   new `system_alert` event, so it now respects the same settings as
   everything else.

5. **Duplicate Notification Center records for one delivered order.**
   `services/order_lifecycle.transition()` fired an `order_delivered`
   notify on *every* call where `new_status == DELIVERED`, including
   idempotent re-transitions from `delivery_queue.py`/`redelivery.py`
   re-syncing state — each re-call created another Notification Center
   entry for the same order. **Fixed:** gated on `just_completed` (only
   the transition that actually first completes the order) and on
   `bot is not None` (state-sync callers that pass `bot=None` no longer
   create phantom records).

6. **Inconsistent message design.** `notify_format.render()` is the
   documented single layout for every admin notification, but
   `subscription_service.py`, `fraud_detection.py`, the SLA
   warning/breach messages in `notifications.py`, and the settings
   module's own test notification built raw, differently-formatted HTML
   strings instead. **Fixed:** all now render through `notify_format.render()`.

## 3. Duplicate/overlapping UI — consolidated

The admin root menu had **three** separate "🔔 Notification…" entries:
a flat 14-event per-admin toggle panel (`acc:sec:notifs` →
`handlers/admin_notifications.py`), the Notification Center log viewer
(`anc:menu`), and the mode/channel screen (`nsm:menu`). The flat panel
duplicated — with a different, non-categorized list — exactly what
Notification Settings now owns, risking two screens governing the same
underlying prefs. **The flat panel's entry point was removed** from the
root menu; event management now lives in one place (Notification
Settings → Categories). The old module file itself was left intact
(not deleted) in case anything else references it, but it is no longer
reachable from the admin panel.

## 4. Known gaps (honestly labeled, not silently faked)

Several events required by the category spec have **no live trigger
anywhere in this codebase** — toggling them is real (it's stored and
would gate delivery), but nothing currently calls `notify_admins()` for
them:

- **Coupons** — no coupon-notification call site exists at all
  (Created/Used/Expired).
- **Orders** — `New Order`, `Order Failed`, `Manual Delivery`,
  `Delivery Failed` are not emitted anywhere (only `Order Completed` /
  `order_delivered` is live).
- **Support** — `Ticket Reply` and `New Dispute` are not emitted
  (only the SLA warning/breach events are live).

These are marked with a ⚠️ in the event list and explained in the
category screen's header text, rather than hidden or faked as working.
Wiring real triggers for these would mean touching order/coupon/support
business logic, which is outside Notification Event Management's scope
per the brief ("existing business logic remains unchanged").

## 5. Verification checklist (per the request)

- [x] No duplicate notifications — fixed the `order_lifecycle` double-fire;
      confirmed no other event has more than one call site for the same
      business action.
- [x] No callback errors — `nsm:cat:menu` / `nsm:cat:view:*` /
      `nsm:cat:tgl:*` are routed in `nsm_dispatch`; existing `nsm:*`
      ConversationHandler entry point still takes priority for
      `nsm:channel:set` and is unaffected.
- [x] One consistent premium design — every admin notification now
      renders through `utils/notify_format.render()`.
- [x] Notification Settings works — mode, channel, and the new
      categorized event toggles all read/write the same stores the
      delivery code actually uses.
- [x] Admin / Channel / Admin+Channel modes work — previously cosmetic,
      now actually control delivery (see §2.1).
- [x] Existing business logic unchanged — all edits were additive
      (new events, new UI, new fan-out logic) or narrow bug fixes to the
      notification layer itself; no order/payment/coupon/support
      business logic was touched.
