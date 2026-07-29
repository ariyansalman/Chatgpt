"""
Universal Payment Workflow.
═══════════════════════════

The one reusable engine every gateway's lifecycle runs through:

    Created → Waiting for Payment → Auto Verification (if supported)
        ├─ success → Approved → Wallet Credited
        └─ ANY failure (API error, HTTP error, timeout, webhook delay,
                        invalid response, txn not found, network error,
                        unknown exception)
                 → Pending Manual Review → Admin Approve/Reject
                        ├─ Approve → Wallet Credited
                        └─ Reject  → user notified, no credit

This module reads gateway *capabilities* from
services/payment_gateway_registry.py — it never hardcodes a gateway name,
and it never duplicates wallet-crediting or order logic: it computes the
inputs those existing code paths need (how much to credit, in what
currency, whether a review is warranted) and leaves the actual DB
writes/wallet ledger entries to the existing call sites
(handlers/admin_pending_deposits.py, handlers/payment_handlers.py), which
already implement those atomically and idempotently. This keeps Wallet
Logic, Order Logic, the DB schema, and existing security/permission checks
completely untouched, while removing the per-gateway `if`/`elif` chains
that used to stand in for a real registry.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple

from services.payment_gateway_registry import registry

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Reviewable methods / pending-queue membership
# ─────────────────────────────────────────────────────────────────────────

def is_reviewable_payment_method(payment_method) -> bool:
    """Replaces hardcoded tuples like
    ``(PaymentMethod.MANUAL, PaymentMethod.BKASH, PaymentMethod.NAGAD)``.

    True when this PaymentMethod's PENDING/AWAITING_CONFIRMATION Transaction
    rows represent a deposit genuinely waiting on a human, per the gateway's
    registry entry — works for any gateway, present or future, with no code
    change here when a new one is added.
    """
    if payment_method is None:
        return False
    gateway_id = getattr(payment_method, "value", payment_method)
    return gateway_id in registry.reviewable_gateway_ids()


def reviewable_payment_methods(payment_method_enum) -> tuple:
    """Return the actual PaymentMethod enum members that are reviewable,
    for callers that need to build a SQLAlchemy ``.in_(...)`` filter.
    ``payment_method_enum`` is the ``PaymentMethod`` enum class itself
    (passed in rather than imported here, so this module has zero
    dependency on database.models and stays reusable outside a DB context).
    """
    reviewable_ids = set(registry.reviewable_gateway_ids())
    return tuple(m for m in payment_method_enum if m.value in reviewable_ids)


# ─────────────────────────────────────────────────────────────────────────
# Currency conversion (registry-driven — replaces hardcoded
# "gateway == BKASH or gateway == NAGAD" branches)
# ─────────────────────────────────────────────────────────────────────────

def gateway_key_of(payment_method) -> Optional[str]:
    if payment_method is None:
        return None
    return getattr(payment_method, "value", payment_method)


def is_foreign_currency_gateway(payment_method) -> bool:
    """True when the gateway settles in a currency other than the wallet's
    USD base currency (e.g. bKash/Nagad settle in BDT) and therefore needs
    conversion before crediting the wallet."""
    gid = gateway_key_of(payment_method)
    g = registry.get(gid)
    return bool(g and g.currency.upper() != "USD")


def credited_usd_amount(payment_method, raw_amount: float) -> float:
    """Convert a gateway-native amount to the USD amount that should be
    credited to the wallet — using the registry entry's ``to_usd``
    converter for whichever gateway this transaction used. Gateways that
    are already USD-quoted (or unregistered) pass the amount through
    unchanged. Never touches the wallet itself — callers still do the
    actual ``user.wallet_balance += ...`` and ``WalletLedger`` write.
    """
    gid = gateway_key_of(payment_method)
    g = registry.get(gid)
    if g:
        return g.convert_to_usd(raw_amount)
    return float(raw_amount or 0.0)


def native_currency_label(payment_method) -> Optional[str]:
    """e.g. 'BDT' for bKash/Nagad, None for USD-quoted gateways — used to
    decide whether to render an amount as "৳X BDT → $Y" vs plain "$Y"."""
    gid = gateway_key_of(payment_method)
    g = registry.get(gid)
    if g and g.currency.upper() != "USD":
        return g.currency.upper()
    return None


def network_hint(payment_method, fallback_network: Optional[str] = None) -> Optional[str]:
    """Registry-driven replacement for hardcoded
    ``if tx.payment_method == PaymentMethod.BKASH: return "bKash (BDT)"``
    chains. Falls back to a caller-supplied value (e.g. a crypto tx's own
    ``crypto_network`` column) when the registry has nothing gateway-level
    to show.
    """
    gid = gateway_key_of(payment_method)
    g = registry.get(gid)
    if g and g.network:
        return g.network
    return fallback_network


def supports_manual_toggle(payment_method_or_key) -> bool:
    """Registry-driven replacement for
    ``gateway_key in ("bkash", "nagad")`` when deciding whether a gateway
    can be flipped between its automated API flow and an admin-managed
    manual flow (services/gateway_manual_mode.py)."""
    gid = gateway_key_of(payment_method_or_key)
    return registry.supports_manual_toggle(gid)


# ─────────────────────────────────────────────────────────────────────────
# Universal auto-verification wrapper
# ─────────────────────────────────────────────────────────────────────────

class VerificationFailed(Exception):
    """Raised by a gateway adapter's verify callable to explicitly signal
    "could not confirm this payment" (as opposed to a clean success). A
    plain unexpected exception is caught just as safely — this is only for
    adapters that want to attach a specific reason/outcome code."""

    def __init__(self, reason: str, outcome: str = "not_confirmed"):
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


def run_auto_verification(
    gateway_id: str,
    verify_fn: Callable[[], Any],
    *,
    on_success: Callable[[Any], None],
    on_pending_review: Callable[[str, str], None],
) -> None:
    """Drive ONE gateway-agnostic auto-verification attempt.

    ``verify_fn`` — zero-arg callable that performs the gateway's own API
    call / webhook-payload check and returns a truthy confirmation value
    on success, or raises (any exception — ``VerificationFailed`` for a
    clean "not confirmed", or literally anything else: an API/HTTP error,
    timeout, malformed response, etc.) on failure.

    ``on_success(result)`` — called with the verify_fn's return value when
    verification succeeds. Caller does the actual "Approved → Wallet
    Credited" transition (unchanged existing code).

    ``on_pending_review(outcome, detail)`` — called on ANY failure, however
    it occurred. Caller does the actual "move to Pending Manual Review"
    transition (unchanged existing code / the shared
    ``enqueue_pending_review`` helper below).

    No payment is ever silently lost: every code path below ends in either
    on_success or on_pending_review.
    """
    if not registry.supports_auto_verification(gateway_id):
        on_pending_review("unsupported", f"{gateway_id} has no auto-verification")
        return

    try:
        result = verify_fn()
    except VerificationFailed as e:
        logger.info("Auto-verification not confirmed for %s: %s", gateway_id, e.reason)
        on_pending_review(e.outcome, e.reason)
        return
    except Exception as e:  # API Error / HTTP Error / Timeout / Webhook Delay /
        # Invalid Response / Transaction Not Found / Network Error / Unknown Exception
        # — every one of these is a reason to fail SAFE into manual review,
        # never to drop the payment.
        logger.warning(
            "Auto-verification raised for gateway=%s — routing to manual review",
            gateway_id, exc_info=True,
        )
        on_pending_review("exception", f"{type(e).__name__}: {e}")
        return

    if not result:
        on_pending_review("not_confirmed", "gateway returned no confirmation")
        return

    on_success(result)


# ─────────────────────────────────────────────────────────────────────────
# Universal Pending Review queue — generalizes the previously
# ZiniPay-specific "create a PendingManualVerification row" snippet in
# handlers/payment_handlers.py so any gateway can reach the same queue
# through one call instead of copy-pasting the insert + dedupe logic.
# ─────────────────────────────────────────────────────────────────────────

def enqueue_pending_review(
    session,
    *,
    gateway_id: str,
    telegram_user_id: int,
    internal_order_id: int,
    submitted_txid: str,
    amount: float,
    currency: Optional[str] = None,
    payment_type: Optional[str] = None,
    network: Optional[str] = None,
    auto_outcome: Optional[str] = None,
    auto_detail: Optional[str] = None,
):
    """Insert (or return the existing) PendingManualVerification row for a
    failed auto-verification, for ANY registered gateway. Relies on the
    table's existing UNIQUE(gateway, internal_order_id, submitted_txid)
    constraint (database/models.py — unchanged) so the same failed
    submission is never queued twice, exactly like the current ZiniPay
    behaviour it generalizes.
    """
    from database.models import PendingManualVerification

    existing = session.query(PendingManualVerification).filter_by(
        gateway=gateway_id,
        internal_order_id=internal_order_id,
        submitted_txid=submitted_txid,
    ).first()
    if existing:
        return existing

    g = registry.get(gateway_id)
    pmv = PendingManualVerification(
        gateway=gateway_id,
        telegram_user_id=telegram_user_id,
        internal_order_id=internal_order_id,
        submitted_txid=submitted_txid,
        amount=amount,
        currency=currency or (g.currency if g else "USD"),
        payment_type=payment_type,
        network=network or (g.network if g else None),
        auto_outcome=auto_outcome,
        auto_detail=auto_detail,
    )
    session.add(pmv)
    return pmv


# ─────────────────────────────────────────────────────────────────────────
# Retry-before-manual-review engine
# ─────────────────────────────────────────────────────────────────────────
#
# Required flow for every gateway:
#
#   Deposit Created
#         |
#   Auto Verification Starts
#         |
#   Gateway/API Check --retry if needed-->  (repeat, with backoff)
#         |
#       Success?
#        ├── YES -> caller's existing "Approve -> Credit Wallet -> Notify"
#        |          code runs, completely unchanged.
#        └── NO  -> only AFTER retries are exhausted -> caller's existing
#                   "Pending Manual Review -> Notify Admin" code runs.
#
# This module never approves a deposit, credits a wallet, or writes an
# order status itself — it only decides *when* the caller's existing
# success/failure branches (already implemented per-gateway in
# handlers/payment_handlers.py) are allowed to run. Wallet logic, order
# logic, the DB schema (aside from the lock/attempt-count columns added
# purely to make this engine race-safe), and every gateway integration are
# untouched.

VERIFY_SUCCESS = "success"     # gateway confirmed the payment -> caller approves
VERIFY_TERMINAL = "terminal"   # gateway gave a definitive "no" that will not
                                # change on retry (e.g. amount mismatch) ->
                                # caller sends straight to manual review
VERIFY_RETRYABLE = "retryable"  # transient failure (API/HTTP error, timeout,
                                 # "not found yet") -> worth retrying
VERIFY_EXHAUSTED = "exhausted"  # retryable on every attempt, but ran out of
                                 # attempts -> caller sends to manual review

DEFAULT_MAX_ATTEMPTS = 4
# Delay (seconds) before attempt 2, 3, 4... — gives the gateway/blockchain
# time to catch up between checks. Last value repeats if max_attempts grows.
DEFAULT_RETRY_DELAYS = (3, 8, 20)

_DEFAULT_STALE_LOCK_SECONDS = 180  # a crashed worker's lock is reclaimable after this


class VerificationLockBusy(Exception):
    """Raised when a verification job is already running for this order
    (another user resubmission, a background retry, or an admin's
    "Verify Again" tap). Prevents duplicate concurrent verification jobs
    and the race conditions / duplicate wallet credits or admin
    notifications they could cause."""


def acquire_verification_lock(session, tx_id: int, stale_after_seconds: int = _DEFAULT_STALE_LOCK_SECONDS) -> bool:
    """Atomically claim the per-order verification lock.

    Succeeds if the lock is free, or if it was left behind by a job that
    started more than ``stale_after_seconds`` ago (a crashed/killed worker) —
    otherwise fails so the caller can tell the user/admin verification is
    already in progress. Mirrors the existing ``review_notified`` /
    ``expiry_notified`` atomic-UPDATE dedup pattern already used elsewhere
    in this codebase.
    """
    from sqlalchemy import or_
    from database.models import Transaction

    stale_cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    claimed = session.query(Transaction).filter(
        Transaction.id == tx_id,
        or_(
            Transaction.verification_in_progress.is_(False),
            Transaction.verification_locked_at.is_(None),
            Transaction.verification_locked_at < stale_cutoff,
        ),
    ).update(
        {
            Transaction.verification_in_progress: True,
            Transaction.verification_locked_at: datetime.utcnow(),
        },
        synchronize_session=False,
    )
    return claimed == 1


def release_verification_lock(tx_id: int) -> None:
    """Release the lock in its own short-lived session — always called
    from a ``finally`` block so a lock is never left held after the job
    ends, whatever the outcome."""
    from database.models import Transaction
    try:
        from database import get_db_session
        with get_db_session() as session:
            session.query(Transaction).filter(Transaction.id == tx_id).update(
                {Transaction.verification_in_progress: False}, synchronize_session=False,
            )
            session.commit()
    except Exception:
        logger.exception("Failed to release verification lock for tx %s", tx_id)


def log_verification_attempt(
    gateway_id: str,
    tx_id: int,
    telegram_user_id: int = 0,
    submitted_txid: str = "",
    outcome: str = "",
    detail: str = "",
) -> None:
    """Durably log ONE verification attempt for a gateway that checks
    payment status with a simple boolean (Cryptomus / NOWPayments /
    CryptoBot's background poll and expiry-time checks) rather than the
    outcome-classified ``run_auto_verification_with_retries`` engine used
    by Binance Pay / Bybit Pay / ZiniPay.

    Writes to the SAME ``VerificationAttemptLog`` table that engine uses,
    so "every verification attempt is logged" holds for every gateway, not
    just the ones with a rich classifier. Best-effort — a logging failure
    must never block the actual payment check/credit it's describing.
    """
    from database import get_db_session
    from database.models import VerificationAttemptLog
    try:
        with get_db_session() as _sess:
            _sess.add(VerificationAttemptLog(
                gateway=gateway_id,
                telegram_user_id=telegram_user_id or 0,
                internal_order_id=tx_id,
                submitted_txid=submitted_txid or "",
                outcome=outcome[:120] if outcome else "",
                detail=(detail or "")[:500],
            ))
            _sess.commit()
    except Exception:
        logger.exception(
            "Failed to persist verification attempt log (gateway=%s tx=%s)",
            gateway_id, tx_id,
        )


async def run_auto_verification_with_retries(
    *,
    gateway_id: str,
    tx_id: int,
    attempt_fn: Callable[[], Any],
    classify: Callable[[Any], Tuple[str, str]],
    telegram_user_id: int,
    submitted_txid: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delays: Optional[Tuple[int, ...]] = None,
) -> Tuple[Any, str, str]:
    """Run up to ``max_attempts`` auto-verification attempts for ONE
    deposit before conceding to manual review, for any gateway.

    ``attempt_fn`` — zero-arg, synchronous callable that performs the
    gateway's own API/blockchain check (run via ``asyncio.to_thread`` so it
    never blocks the event loop). Returns whatever raw result object that
    gateway's existing code already knows how to interpret.

    ``classify(raw_result) -> (kind, detail)`` — gateway-specific mapping
    from that raw result to one of VERIFY_SUCCESS / VERIFY_TERMINAL /
    VERIFY_RETRYABLE, plus a short human-readable detail string for the
    log. An uncaught exception from ``attempt_fn`` (network error, timeout,
    malformed response, or any other unknown exception) is always treated
    as VERIFY_RETRYABLE — never a reason to drop the payment.

    Returns ``(last_raw_result, final_kind, final_detail)`` where
    ``final_kind`` is VERIFY_SUCCESS, VERIFY_TERMINAL, or VERIFY_EXHAUSTED.
    The caller's existing success/failure code (approve+credit, or
    enqueue_pending_review+notify) is unchanged — it just runs on this
    function's *final* result instead of a single attempt's result.

    Raises ``VerificationLockBusy`` if another verification job is already
    running for this order (caller should just tell the user/admin to wait
    — this is not a failure, and no attempt is logged).
    """
    from database import get_db_session
    from database.models import Transaction, VerificationAttemptLog

    with get_db_session() as _sess:
        got_lock = acquire_verification_lock(_sess, tx_id)
        _sess.commit()
    if not got_lock:
        raise VerificationLockBusy(f"verification already in progress for transaction {tx_id}")

    delays = retry_delays or DEFAULT_RETRY_DELAYS
    last_raw: Any = None
    last_detail = ""

    # Hard ceiling on a single attempt, independent of whatever timeout (if
    # any) the gateway adapter itself sets on its HTTP calls. Guarantees
    # this engine can never hang indefinitely on one attempt.
    MAX_ATTEMPT_SECONDS = 30

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(attempt_fn), timeout=MAX_ATTEMPT_SECONDS,
                )
                kind, detail = classify(raw)
            except asyncio.TimeoutError:
                # Gateway call exceeded the hard ceiling — never block the
                # bot waiting on it. The background thread is left to finish
                # on its own; we simply stop waiting for it and retry.
                raw = None
                kind, detail = VERIFY_RETRYABLE, (
                    f"timed out after {MAX_ATTEMPT_SECONDS}s"
                )
                logger.warning(
                    "Auto-verification attempt %s/%s timed out (>%ss) for gateway=%s tx=%s",
                    attempt, max_attempts, MAX_ATTEMPT_SECONDS, gateway_id, tx_id,
                )
            except Exception as e:  # API error / HTTP error / timeout / webhook
                # delay / invalid response / unknown exception — always safe
                # to retry, never a reason to lose the deposit.
                raw = None
                kind, detail = VERIFY_RETRYABLE, f"{type(e).__name__}: {e}"
                logger.warning(
                    "Auto-verification attempt %s/%s raised for gateway=%s tx=%s",
                    attempt, max_attempts, gateway_id, tx_id, exc_info=True,
                )

            last_raw, last_detail = raw, detail

            try:
                with get_db_session() as _sess:
                    _sess.add(VerificationAttemptLog(
                        gateway=gateway_id,
                        telegram_user_id=telegram_user_id,
                        internal_order_id=tx_id,
                        submitted_txid=submitted_txid or "",
                        outcome=f"AUTO_ATTEMPT_{attempt}_{kind.upper()}",
                        detail=(detail or "")[:500],
                    ))
                    _sess.query(Transaction).filter_by(id=tx_id).update(
                        {Transaction.auto_verify_attempts: Transaction.auto_verify_attempts + 1},
                        synchronize_session=False,
                    )
                    _sess.commit()
            except Exception:
                logger.exception(
                    "Failed to persist verification attempt log (gateway=%s tx=%s attempt=%s)",
                    gateway_id, tx_id, attempt,
                )

            if kind == VERIFY_SUCCESS:
                logger.info("Auto-verification succeeded for gateway=%s tx=%s on attempt %s", gateway_id, tx_id, attempt)
                return raw, VERIFY_SUCCESS, detail

            if kind == VERIFY_TERMINAL:
                logger.info("Auto-verification terminally failed for gateway=%s tx=%s: %s", gateway_id, tx_id, detail)
                return raw, VERIFY_TERMINAL, detail

            # VERIFY_RETRYABLE — wait (unless this was the last attempt) and try again.
            if attempt < max_attempts:
                delay = delays[min(attempt - 1, len(delays) - 1)]
                await asyncio.sleep(delay)

        logger.info(
            "Auto-verification exhausted %s attempts for gateway=%s tx=%s — routing to manual review",
            max_attempts, gateway_id, tx_id,
        )
        return last_raw, VERIFY_EXHAUSTED, last_detail
    finally:
        release_verification_lock(tx_id)
