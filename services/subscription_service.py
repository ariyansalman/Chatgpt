"""Recurring subscription billing service (V13).

Two jobs, meant to be run periodically from the bot's JobQueue
(see ``bot.py``):

  * :func:`send_renewal_reminders` — nudges users a few days before their
    next auto-charge so a low wallet balance doesn't come as a surprise.
  * :func:`run_billing_cycle` — charges the wallet for every subscription
    whose ``next_billing_date`` has arrived, via ``services/wallet.py``
    (the single choke-point for balance mutations, so every charge is
    ledgered). On success the cycle rolls forward; on insufficient funds
    the subscription is marked ``past_due`` and, after too many consecutive
    failures, auto-cancelled.

Also exposes plain query/action helpers used by the admin panel
(``handlers/admin_subscriptions.py``): listing, counts, and force-cancel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from database import get_db_session, User, Product, Subscription
from services import wallet as wallet_svc
from services.wallet import WalletError
from services.notifications import notify_admins
from utils.bot_config import cfg
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

ACTIVE = "active"
PAST_DUE = "past_due"
CANCELLED = "cancelled"
EXPIRED = "expired"


# ─────────────────────────────────────────────────────────────────────────
# Config (admin-tunable via BotConfig; sane defaults if unset)
# ─────────────────────────────────────────────────────────────────────────
def _reminder_days_before() -> int:
    return max(0, cfg.get_int("subscription_reminder_days_before", 3))


def _max_failed_attempts() -> int:
    return max(1, cfg.get_int("subscription_max_failed_attempts", 3))


# ─────────────────────────────────────────────────────────────────────────
# Renewal reminders
# ─────────────────────────────────────────────────────────────────────────
async def send_renewal_reminders(bot: Bot) -> int:
    """Message users whose next charge lands within the reminder window.

    Sends at most one reminder per subscription per cycle (tracked via
    ``last_reminder_at``). Returns the number of reminders sent.
    """
    days_before = _reminder_days_before()
    if days_before <= 0:
        return 0
    now = datetime.utcnow()
    horizon = now + timedelta(days=days_before)
    sent = 0
    with get_db_session() as s:
        subs = (s.query(Subscription)
                .filter(Subscription.status == ACTIVE,
                        Subscription.auto_renew == True,  # noqa: E712
                        Subscription.next_billing_date.isnot(None),
                        Subscription.next_billing_date <= horizon,
                        Subscription.next_billing_date > now)
                .all())
        due = []
        for sub in subs:
            # Skip if we already reminded for this cycle.
            if sub.last_reminder_at and sub.last_reminder_at >= (sub.next_billing_date - timedelta(days=days_before)):
                continue
            user = s.query(User).filter(User.id == sub.user_id).first()
            product = s.query(Product).filter(Product.id == sub.product_id).first()
            if not user or not user.telegram_id:
                continue
            due.append((sub.id, user.telegram_id, product.name if product else "Subscription",
                        sub.next_billing_date, float(sub.billing_amount or 0.0)))
            sub.last_reminder_at = now
        s.commit()

    for sub_id, tg_id, pname, next_date, amount in due:
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=(f"🔔 <b>Upcoming renewal</b>\n\n"
                      f"Your subscription to <b>{pname}</b> renews on "
                      f"<b>{next_date:%Y-%m-%d}</b> for <b>${amount:.2f}</b>, "
                      f"auto-deducted from your wallet.\n\n"
                      f"Make sure your wallet balance is topped up to avoid "
                      f"interruption."),
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            logger.exception("Failed to send renewal reminder for subscription %s", sub_id)
    return sent


# ─────────────────────────────────────────────────────────────────────────
# Auto-billing
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class BillingOutcome:
    charged: int = 0
    failed: int = 0
    auto_cancelled: int = 0


async def run_billing_cycle(bot: Bot) -> BillingOutcome:
    """Charge every subscription whose next_billing_date has arrived."""
    now = datetime.utcnow()
    outcome = BillingOutcome()
    max_attempts = _max_failed_attempts()

    with get_db_session() as s:
        due = (s.query(Subscription)
               .filter(Subscription.status.in_([ACTIVE, PAST_DUE]),
                       Subscription.auto_renew == True,  # noqa: E712
                       Subscription.next_billing_date.isnot(None),
                       Subscription.next_billing_date <= now)
               .all())
        due_ids = [d.id for d in due]

    for sub_id in due_ids:
        await _bill_one(bot, sub_id, now, max_attempts, outcome)
    return outcome


async def _bill_one(bot: Bot, sub_id: int, now: datetime, max_attempts: int,
                     outcome: BillingOutcome) -> None:
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
        if not sub or sub.status not in (ACTIVE, PAST_DUE):
            return

        # Concurrency guard — run_billing_cycle selects "due" subscriptions
        # in one query, then bills each one sequentially with network I/O
        # (bot.send_message) per subscription. On a store with many active
        # subscriptions a single run can outlast the job interval, so a
        # second scheduled tick can start selecting the SAME still-due
        # subscription before the first tick advances next_billing_date /
        # failed_attempts — a double charge. This claims the exact
        # (subscription, cycle, attempt) tuple via the existing payment
        # idempotency table before any money moves; only the caller that
        # wins proceeds, and a legitimate retry on the next tick (after
        # failed_attempts increments) still gets its own fresh claim.
        from services.idempotency import claim_locked as _idem_claim_locked
        claim_ref = f"sub:{sub_id}:cycle:{sub.next_billing_date.isoformat()}:attempt:{sub.failed_attempts or 0}"
        try:
            if not _idem_claim_locked(s, "subscription_bill", claim_ref):
                return  # Already being handled / handled by another run
        except Exception:
            logger.exception(
                "idempotency.claim_locked raised for subscription %s — "
                "skipping this tick (fail closed)", sub_id,
            )
            return
        s.commit()

        user = s.query(User).filter(User.id == sub.user_id).first()
        product = s.query(Product).filter(Product.id == sub.product_id).first()
        if not user:
            return
        amount = float(sub.billing_amount or (product.price if product else 0.0) or 0.0)
        cycle_days = int(sub.billing_cycle_days or 30)
        pname = product.name if product else "Subscription"
        tg_id = user.telegram_id

    if amount <= 0:
        # Nothing to charge — just roll the cycle forward.
        with get_db_session() as s:
            sub2 = s.query(Subscription).filter(Subscription.id == sub_id).first()
            if sub2:
                sub2.next_billing_date = now + timedelta(days=cycle_days)
                sub2.expires_at = max(sub2.expires_at, sub2.next_billing_date)
        return

    try:
        wallet_svc.debit(
            sub.user_id, amount,
            reason=f"Subscription renewal — {pname}",
            actor_type="system", actor_id=None,
            ref_type="subscription", ref_id=str(sub_id),
        )
    except WalletError:
        with get_db_session() as s:
            sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
            if not sub:
                return
            sub.failed_attempts = (sub.failed_attempts or 0) + 1
            sub.status = PAST_DUE
            auto_cancel = sub.failed_attempts >= max_attempts
            if auto_cancel:
                sub.status = CANCELLED
                sub.auto_renew = False
                sub.cancelled_at = now
                sub.cancel_reason = "auto_cancel_insufficient_funds"
            attempts_left = max_attempts - sub.failed_attempts
        outcome.failed += 1
        try:
            if auto_cancel:
                await bot.send_message(
                    chat_id=tg_id,
                    text=(f"❌ <b>Subscription cancelled</b>\n\n"
                          f"We couldn't renew <b>{pname}</b> (${amount:.2f}) after "
                          f"{max_attempts} attempts due to insufficient wallet balance. "
                          f"Your subscription has been cancelled. Top up your wallet "
                          f"and re-subscribe any time."),
                    parse_mode=ParseMode.HTML,
                )
                outcome.auto_cancelled += 1
            else:
                await bot.send_message(
                    chat_id=tg_id,
                    text=(f"⚠️ <b>Renewal failed</b>\n\n"
                          f"We tried to renew <b>{pname}</b> for ${amount:.2f} but your "
                          f"wallet balance is too low. Please top up — "
                          f"{attempts_left} attempt(s) remaining before cancellation."),
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            logger.exception("Failed to notify user %s of renewal failure", tg_id)
        try:
            from utils.notify_format import render as _render, utc_now_str as _ts
            await notify_admins(
                bot, "subscription",
                _render("⚠️", "Subscription Renewal Failed", [
                    ("Subscription", f"#{sub_id}"),
                    ("User", f"tg:{tg_id}"),
                    ("Plan", pname),
                    ("Amount", f"${amount:.2f}"),
                    ("Outcome", "Auto-cancelled" if auto_cancel else f"{attempts_left} attempt(s) left"),
                ], _ts()),
            )
        except Exception:
            logger.exception("notify_admins failed for subscription %s", sub_id)
        return

    # Success: roll the billing cycle forward.
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
        if not sub:
            return
        sub.failed_attempts = 0
        sub.status = ACTIVE
        sub.last_billed_at = now
        sub.next_billing_date = now + timedelta(days=cycle_days)
        sub.expires_at = max(sub.expires_at, sub.next_billing_date)
    outcome.charged += 1
    try:
        await bot.send_message(
            chat_id=tg_id,
            text=(f"✅ <b>Subscription renewed</b>\n\n"
                  f"<b>{pname}</b> — ${amount:.2f} was auto-deducted from your wallet.\n"
                  f"Next renewal: {(now + timedelta(days=cycle_days)):%Y-%m-%d}"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to notify user %s of successful renewal", tg_id)


# ─────────────────────────────────────────────────────────────────────────
# Admin query / action helpers
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class SubRow:
    id: int
    user_id: int
    telegram_id: Optional[int]
    username: str
    product_name: str
    status: str
    next_billing_date: Optional[datetime]
    billing_amount: float
    auto_renew: bool
    failed_attempts: int


def counts_by_status() -> dict:
    with get_db_session() as s:
        out = {}
        for st in (ACTIVE, PAST_DUE, CANCELLED, EXPIRED):
            out[st] = s.query(Subscription).filter(Subscription.status == st).count()
        return out


def list_subscriptions(status: Optional[str] = None, page: int = 0,
                        page_size: int = 8) -> tuple[list, int, int]:
    """Return (rows, page, total_pages) for the admin list view."""
    with get_db_session() as s:
        q = s.query(Subscription)
        if status:
            q = q.filter(Subscription.status == status)
        q = q.order_by(Subscription.id.desc())
        total = q.count()
        page_size = max(1, page_size)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, pages - 1))
        rows_db = q.offset(page * page_size).limit(page_size).all()

        rows = []
        for sub in rows_db:
            user = s.query(User).filter(User.id == sub.user_id).first()
            product = s.query(Product).filter(Product.id == sub.product_id).first()
            rows.append(SubRow(
                id=sub.id,
                user_id=sub.user_id,
                telegram_id=(user.telegram_id if user else None),
                username=(user.username or "—") if user else "—",
                product_name=(product.name if product else f"Product #{sub.product_id}"),
                status=sub.status,
                next_billing_date=sub.next_billing_date,
                billing_amount=float(sub.billing_amount or 0.0),
                auto_renew=bool(sub.auto_renew),
                failed_attempts=int(sub.failed_attempts or 0),
            ))
        return rows, page, pages


def get_detail(sub_id: int) -> Optional[dict]:
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
        if not sub:
            return None
        user = s.query(User).filter(User.id == sub.user_id).first()
        product = s.query(Product).filter(Product.id == sub.product_id).first()
        return {
            "id": sub.id,
            "user_id": sub.user_id,
            "telegram_id": user.telegram_id if user else None,
            "username": (user.username or "—") if user else "—",
            "product_name": product.name if product else f"Product #{sub.product_id}",
            "status": sub.status,
            "starts_at": sub.starts_at,
            "expires_at": sub.expires_at,
            "next_billing_date": sub.next_billing_date,
            "billing_cycle_days": sub.billing_cycle_days,
            "billing_amount": float(sub.billing_amount or 0.0),
            "auto_renew": bool(sub.auto_renew),
            "failed_attempts": int(sub.failed_attempts or 0),
            "last_billed_at": sub.last_billed_at,
            "cancelled_at": sub.cancelled_at,
            "cancel_reason": sub.cancel_reason,
        }


def force_cancel(sub_id: int, admin_telegram_id: int,
                  reason: str = "admin_force_cancel") -> bool:
    """Admin action: immediately cancel a subscription and stop auto-renew."""
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == sub_id).first()
        if not sub or sub.status == CANCELLED:
            return False
        sub.status = CANCELLED
        sub.auto_renew = False
        sub.cancelled_at = datetime.utcnow()
        sub.cancelled_by = admin_telegram_id
        sub.cancel_reason = reason[:255]
    try:
        log_admin_action(admin_telegram_id, "subscription.force_cancel",
                         target_type="subscription", target_id=sub_id,
                         details=reason)
    except Exception:
        logger.exception("Failed to log admin action for subscription %s", sub_id)
    return True


# ─────────────────────────────────────────────────────────────────────────
# JobQueue entry points
# ─────────────────────────────────────────────────────────────────────────
async def reminder_job(context) -> None:
    try:
        n = await send_renewal_reminders(context.bot)
        if n:
            logger.info("Sent %d subscription renewal reminders", n)
    except Exception:
        logger.exception("subscription reminder_job failed")


async def billing_job(context) -> None:
    try:
        outcome = await run_billing_cycle(context.bot)
        if outcome.charged or outcome.failed:
            logger.info("Subscription billing cycle: charged=%d failed=%d cancelled=%d",
                       outcome.charged, outcome.failed, outcome.auto_cancelled)
    except Exception:
        logger.exception("subscription billing_job failed")
