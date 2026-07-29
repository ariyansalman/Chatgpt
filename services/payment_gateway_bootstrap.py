"""
Payment Gateway Bootstrap.
══════════════════════════

Registers every *currently existing* payment gateway into the central
Payment Gateway Registry (services/payment_gateway_registry.py).

This file is the ONLY place that lists gateways by name. It changes only
when a gateway is added or removed — never when workflow behaviour
changes. It does not implement any payment logic itself; it just declares
metadata about gateways whose real implementation already lives in
services/<gateway>_payment.py (unmodified).

Call ``bootstrap_gateways()`` once at process start (see bot.py). It is
idempotent — safe to call again (e.g. in tests) since registration is
last-write-wins.
"""
from __future__ import annotations

from services.payment_gateway_registry import registry, GatewayDescriptor


def bootstrap_gateways() -> None:
    from services.pricing import convert_currency

    # ── Native crypto / API gateways (fully automated, webhook or polling
    #    verified, fall back to the universal Pending Manual Review queue
    #    on any verification error) ────────────────────────────────────
    from services.binance_pay import BinancePayService
    from services.bybit_pay import BybitPayService
    from services.cryptomus_payment import CryptomusPaymentService
    from services.nowpayments_payment import NowPaymentsService
    from services.heleket_payment import HeleketPaymentService
    from services.telegram_stars import TelegramStarsService
    from services.crypto_bot import CryptoBotService

    registry.register(GatewayDescriptor(
        gateway_id="binance_pay", display_name="Binance Pay",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=BinancePayService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="bybit_pay", display_name="Bybit Pay",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=BybitPayService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="cryptomus", display_name="Cryptomus",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=CryptomusPaymentService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="nowpayments", display_name="NOWPayments",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=NowPaymentsService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="heleket", display_name="Heleket",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=HeleketPaymentService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="stars", display_name="Telegram Stars",
        payment_type="wallet", verification_mode="auto",
        supports_webhook=True, supports_manual_review=False,
        supports_auto_verification=True, currency="XTR",
        service_cls=TelegramStarsService,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="cryptobot", display_name="CryptoBot",
        payment_type="crypto", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=CryptoBotService,
    ))

    # ── ZiniPay-brokered mobile wallets (bKash / Nagad / Rocket) — hybrid:
    #    automated API checkout by default, but each can be individually
    #    switched to manual-review mode by an admin (services/gateway_manual_mode.py),
    #    and Rocket is manual-review only today ─────────────────────────
    from services.zinipay_payment import ZiniPayService

    registry.register(GatewayDescriptor(
        gateway_id="zinipay", display_name="bKash • Nagad • Rocket",
        payment_type="mobile_wallet", verification_mode="auto",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="USD",
        service_cls=ZiniPayService,
    ))

    from services.bkash_payment import BkashPaymentService
    from services.nagad_payment import NagadPaymentService

    registry.register(GatewayDescriptor(
        gateway_id="bkash", display_name="bKash",
        payment_type="mobile_wallet", verification_mode="hybrid",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="BDT",
        network="bKash (BDT)",
        service_cls=BkashPaymentService,
        to_usd=lambda amount: convert_currency(amount, "BDT", "USD"),
        supports_manual_toggle=True,
    ))
    registry.register(GatewayDescriptor(
        gateway_id="nagad", display_name="Nagad",
        payment_type="mobile_wallet", verification_mode="hybrid",
        supports_webhook=True, supports_manual_review=True,
        supports_auto_verification=True, currency="BDT",
        network="Nagad (BDT)",
        service_cls=NagadPaymentService,
        to_usd=lambda amount: convert_currency(amount, "BDT", "USD"),
        supports_manual_toggle=True,
    ))

    # ── Pure manual gateways (admin-managed merchant number + instructions,
    #    TrxID/screenshot reviewed by hand — no API, no auto-verification) ─
    registry.register(GatewayDescriptor(
        gateway_id="manual", display_name="Manual Payment",
        payment_type="manual", verification_mode="manual",
        supports_webhook=False, supports_manual_review=True,
        supports_auto_verification=False, currency="USD",
    ))


_bootstrapped = False


def ensure_bootstrapped() -> None:
    """Idempotent guard so importing this module from multiple entry
    points (bot process, webhook process, tests) never double-registers
    or races — call this instead of bootstrap_gateways() directly from
    anywhere except bot.py's single startup call."""
    global _bootstrapped
    if _bootstrapped:
        return
    bootstrap_gateways()
    _bootstrapped = True
