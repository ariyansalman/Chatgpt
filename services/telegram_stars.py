"""Telegram Stars (XTR) native payment service.

Telegram Stars is Telegram's own in-app currency for digital goods and
services. Unlike bKash / Nagad / Card, a Stars payment never touches a
third-party gateway — the bot invoices the user directly through the Bot
API using the special currency code ``"XTR"`` and an EMPTY
``provider_token``, and Telegram settles the Stars with the bot itself.

This service is the single place that:
  1. Reads/writes the admin-configurable Stars→USD conversion rate,
     stored in ``PaymentGatewayConfig`` (gateway="telegram_stars") —
     see database/models.py.
  2. Converts between USD (what the wallet is credited in) and Stars
     (what Telegram actually charges the user).

It deliberately mirrors the shape of ``services/bkash_payment.py`` /
``services/crypto_bot.py`` (lazy DB access, no work at import time) so it
plugs into the same handlers/payment_handlers.py conventions.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from database import get_db_session
from database.models import PaymentGatewayConfig

logger = logging.getLogger(__name__)

GATEWAY_KEY = "telegram_stars"

# Telegram doesn't publish one fixed USD/Star peg — the effective value you
# get back when cashing Stars out varies by time/region and Telegram's own
# terms. This is only a sane out-of-the-box default; the admin should
# confirm/update it from the admin panel (Admin → Payment Gateways →
# ⭐ Telegram Stars) to match current Telegram terms.
DEFAULT_RATE_USD_PER_STAR = 0.013
DEFAULT_MIN_STARS = 1
DEFAULT_MAX_STARS = 10000  # Telegram also enforces its own server-side ceiling


def _get_or_create_config(session) -> PaymentGatewayConfig:
    row = session.query(PaymentGatewayConfig).filter_by(gateway=GATEWAY_KEY).first()
    if not row:
        row = PaymentGatewayConfig(
            gateway=GATEWAY_KEY,
            is_enabled=False,
            rate_usd_per_star=DEFAULT_RATE_USD_PER_STAR,
            min_stars=DEFAULT_MIN_STARS,
            max_stars=DEFAULT_MAX_STARS,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


class TelegramStarsService:
    """Stateless helper — every call reads fresh config from the DB, so an
    admin changing the rate takes effect immediately without a bot restart.
    """

    def get_config(self) -> dict:
        with get_db_session() as session:
            row = _get_or_create_config(session)
            return {
                "enabled": bool(row.is_enabled),
                "rate": float(row.rate_usd_per_star or DEFAULT_RATE_USD_PER_STAR),
                "min_stars": int(row.min_stars or DEFAULT_MIN_STARS),
                "max_stars": int(row.max_stars or DEFAULT_MAX_STARS),
            }

    def is_enabled(self) -> bool:
        return self.get_config()["enabled"]

    def set_enabled(self, enabled: bool) -> None:
        with get_db_session() as session:
            row = _get_or_create_config(session)
            row.is_enabled = bool(enabled)
            session.commit()

    def get_rate(self) -> float:
        return self.get_config()["rate"]

    def set_rate(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("Rate must be > 0")
        with get_db_session() as session:
            row = _get_or_create_config(session)
            row.rate_usd_per_star = float(rate)
            session.commit()

    def set_star_limits(self, min_stars: Optional[int] = None, max_stars: Optional[int] = None) -> None:
        with get_db_session() as session:
            row = _get_or_create_config(session)
            if min_stars is not None:
                row.min_stars = int(min_stars)
            if max_stars is not None:
                row.max_stars = int(max_stars)
            session.commit()

    def stars_for_usd(self, usd_amount: float) -> int:
        """How many ⭐ Stars the user must pay to credit ``usd_amount`` USD.

        Rounds UP so the store never under-charges because of rounding.
        """
        rate = self.get_rate()
        if rate <= 0:
            rate = DEFAULT_RATE_USD_PER_STAR
        stars = math.ceil(float(usd_amount) / rate)
        return max(stars, 1)

    def usd_for_stars(self, stars: int) -> float:
        """USD value credited to the wallet for a given number of Stars."""
        return round(float(stars) * self.get_rate(), 2)


telegram_stars_service = TelegramStarsService()
