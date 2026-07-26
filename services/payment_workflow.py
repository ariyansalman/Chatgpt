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

import logging
from typing import Any, Callable, Optional

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
