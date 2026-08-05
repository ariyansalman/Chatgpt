"""
Central Payment Gateway Registry.
══════════════════════════════════

Single source of truth for "what payment gateways exist and what they can
do". Every part of the payment stack (creation, auto-verification, the
pending-review queue, admin approve/reject, dashboard counters, payment
history, notifications) reads gateway *capabilities* from here instead of
hardcoding gateway names or PaymentMethod enum members.

This module does NOT touch:
  • wallet crediting logic       (services/wallet.py, WalletLedger)
  • order/product logic
  • the database schema          (no new tables/columns — registry state
                                   lives in memory, populated at import
                                   time by services/payment_gateway_bootstrap.py)
  • any gateway's own API client (services/<gateway>_payment.py etc. are
                                   registered *as-is*, unmodified)
  • callback_data formats, permissions, or existing security checks

Adding a brand-new gateway in the future is exactly one call:

    from services.payment_gateway_registry import registry, GatewayDescriptor

    registry.register(GatewayDescriptor(
        gateway_id="stripe",
        display_name="Stripe",
        payment_type="card",
        verification_mode="auto",
        supports_webhook=True,
        supports_manual_review=True,   # fallback when auto-verify fails
        supports_auto_verification=True,
        currency="USD",
        service_cls=StripeService,     # exposes .create_payment(amount, tx_id)
    ))

From that point on, services/payment_workflow.py drives that gateway
through the full Created → Waiting → Auto-Verify → Approved/Wallet-Credit
(or → Pending Manual Review → Admin Approve/Reject → Wallet-Credit)
lifecycle automatically — no new workflow code required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class GatewayDescriptor:
    """Everything the universal payment workflow needs to know about one
    gateway. Only ``gateway_id`` and ``display_name`` are required —
    everything else has a safe, conservative default so a minimal
    registration still works."""

    # ── Identity ────────────────────────────────────────────────────────
    gateway_id: str                      # stable key, e.g. "cryptomus", "bkash"
    display_name: str                    # e.g. "Cryptomus", "bKash"

    # ── Classification ──────────────────────────────────────────────────
    payment_type: str = "gateway"        # "gateway" | "crypto" | "manual" | "card" | "wallet"
    verification_mode: str = "auto"      # "auto" | "manual" | "hybrid"

    # ── Capabilities ─────────────────────────────────────────────────────
    supports_webhook: bool = False
    supports_manual_review: bool = True  # can this gateway fall back to admin review?
    supports_auto_verification: bool = False

    # ── Money ────────────────────────────────────────────────────────────
    currency: str = "USD"                # the currency the GATEWAY quotes/settles in
    network: Optional[str] = None        # e.g. "TRC20" for a fixed-network gateway

    # ── Wiring ───────────────────────────────────────────────────────────
    # Optional adapter class exposing `.create_payment(usd_amount, tx_id)`
    # and (by convention) a `.last_error` attribute — the same shape every
    # existing gateway service class in services/*_payment.py already has.
    service_cls: Optional[type] = None

    # Optional callable(amount) -> amount_in_usd, only needed when
    # `currency` isn't already USD (e.g. bKash/Nagad settle in BDT).
    # Defaults to identity (no conversion) when the gateway is USD-quoted.
    to_usd: Optional[Callable[[float], float]] = None

    # Whether this gateway can be toggled between its automated API flow
    # and an admin-managed manual flow at runtime (mirrors
    # services/gateway_manual_mode.py). False for gateways that are always
    # fully automated or always fully manual.
    supports_manual_toggle: bool = False

    # ── Status ───────────────────────────────────────────────────────────
    # Can be a bool or a zero-arg callable for gateways whose enabled state
    # is read live from bot_config / PaymentGatewayConfig (most of them).
    enabled: "bool | Callable[[], bool]" = True

    # Free-form bag for anything a specific adapter needs to stash without
    # requiring a registry schema change.
    metadata: dict = field(default_factory=dict)

    def is_enabled(self) -> bool:
        return self.enabled() if callable(self.enabled) else bool(self.enabled)

    def convert_to_usd(self, amount: float) -> float:
        if self.to_usd:
            return self.to_usd(amount)
        return float(amount or 0.0)


class PaymentGatewayRegistry:
    """In-memory registry. Populated once at process start by
    services/payment_gateway_bootstrap.py; safe to re-register (last write
    wins) so tests / hot-reload can re-run bootstrap freely."""

    def __init__(self) -> None:
        self._gateways: Dict[str, GatewayDescriptor] = {}

    def register(self, descriptor: GatewayDescriptor) -> None:
        self._gateways[descriptor.gateway_id] = descriptor

    def unregister(self, gateway_id: str) -> None:
        self._gateways.pop(gateway_id, None)

    def get(self, gateway_id: Optional[str]) -> Optional[GatewayDescriptor]:
        if not gateway_id:
            return None
        return self._gateways.get(gateway_id)

    def all(self) -> List[GatewayDescriptor]:
        return list(self._gateways.values())

    def enabled(self) -> List[GatewayDescriptor]:
        return [g for g in self._gateways.values() if g.is_enabled()]

    def ids(self) -> List[str]:
        return list(self._gateways.keys())

    # ── Capability queries used by services/payment_workflow.py ─────────
    def supports_manual_review(self, gateway_id: str) -> bool:
        g = self.get(gateway_id)
        return bool(g and g.supports_manual_review)

    def supports_auto_verification(self, gateway_id: str) -> bool:
        g = self.get(gateway_id)
        return bool(g and g.supports_auto_verification)

    def supports_manual_toggle(self, gateway_id: str) -> bool:
        g = self.get(gateway_id)
        return bool(g and g.supports_manual_toggle)

    def currency_of(self, gateway_id: str) -> str:
        g = self.get(gateway_id)
        return g.currency if g else "USD"

    def network_of(self, gateway_id: str) -> Optional[str]:
        g = self.get(gateway_id)
        return g.network if g else None

    def reviewable_gateway_ids(self) -> List[str]:
        """Gateway ids whose PENDING/AWAITING_CONFIRMATION rows represent a
        deposit genuinely waiting on a human — i.e. gateways that are NOT
        purely webhook/API auto-verified end-to-end."""
        return [
            g.gateway_id for g in self._gateways.values()
            if g.supports_manual_review and g.verification_mode in ("manual", "hybrid")
        ]


# Module-level singleton — import this everywhere.
registry = PaymentGatewayRegistry()
