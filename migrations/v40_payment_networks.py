"""
V40 — Dynamic Payment Networks.
═══════════════════════════════

Creates the additive ``payment_networks`` table used by the admin-managed,
database-driven Payment Network Configuration panel
(handlers/admin_payment_networks.py + services/payment_networks.py).

Purely additive and idempotent:
  • no existing table, column or row is modified
  • no payment logic, verification flow, wallet crediting or callback_data
    is touched — networks route through the EXISTING pay_<gateway_key> and
    pay_pm_<manual_method_id> handlers
  • the app runs unchanged if this migration has not been executed: the
    service layer degrades to "no networks configured" and every payment
    screen behaves exactly as before.

Run:  python -m migrations.v40_payment_networks
"""
from __future__ import annotations


def upgrade() -> None:
    from database.db import engine
    from database.models import PaymentNetwork

    print("• Creating table 'payment_networks' (if missing) …")
    PaymentNetwork.__table__.create(bind=engine, checkfirst=True)
    print("✔ payment_networks ready.")


if __name__ == "__main__":
    upgrade()
