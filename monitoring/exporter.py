"""Prometheus metrics exporter for the Telegram bot (Phase 4+).

Exposes /metrics on port 9100 with counters/gauges built from the DB:
  - tgbot_users_total
  - tgbot_users_banned
  - tgbot_orders_total{status="..."}
  - tgbot_revenue_usd_total
  - tgbot_wallet_balance_usd_total
  - tgbot_pending_transactions
  - tgbot_products_active
  - tgbot_low_stock_products (stock <= 5)
  - tgbot_churn_rate_percent
  - tgbot_churned_customers
  - tgbot_paying_customers
  - tgbot_avg_ltv_usd
  - tgbot_median_ltv_usd
  - tgbot_total_ltv_usd
  - tgbot_cohort_retention_percent{cohort="YYYY-MM", period="M<n>"}

Run alongside the bot:
    docker compose --profile monitoring up -d
"""

import time
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

from sqlalchemy import func
from database.db import get_db_session
from database.models import (
    User, Order, OrderStatus, Transaction, TransactionStatus, Product,
)
from services.customer_analytics import (
    compute_churn_rate, compute_ltv, compute_cohort_retention,
)
from utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger("metrics")

PORT = 9100


def _fmt(name: str, value, help_text: str, mtype: str = "gauge", labels: dict | None = None) -> str:
    lbl = ""
    if labels:
        lbl = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
    return (
        f"# HELP {name} {help_text}\n"
        f"# TYPE {name} {mtype}\n"
        f"{name}{lbl} {value}\n"
    )


def collect_metrics() -> str:
    out = []
    with get_db_session() as s:
        users_total = s.query(func.count(User.id)).scalar() or 0
        users_banned = s.query(func.count(User.id)).filter(User.is_banned == True).scalar() or 0  # noqa: E712
        wallet_sum = float(s.query(func.coalesce(func.sum(User.wallet_balance), 0)).scalar() or 0)

        out.append(_fmt("tgbot_users_total", users_total, "Total registered users"))
        out.append(_fmt("tgbot_users_banned", users_banned, "Currently banned users"))
        out.append(_fmt("tgbot_wallet_balance_usd_total", f"{wallet_sum:.2f}", "Sum of all wallet balances (USD)"))

        for st in OrderStatus:
            c = s.query(func.count(Order.id)).filter(Order.status == st).scalar() or 0
            out.append(_fmt("tgbot_orders_total", c, "Orders by status", "gauge", {"status": st.value}))

        revenue = float(
            s.query(func.coalesce(func.sum(Order.total_amount), 0))
            .filter(Order.status == OrderStatus.COMPLETED).scalar() or 0
        )
        out.append(_fmt("tgbot_revenue_usd_total", f"{revenue:.2f}", "Completed-order revenue (USD)", "counter"))

        pending_tx = s.query(func.count(Transaction.id)).filter(
            Transaction.status == TransactionStatus.PENDING
        ).scalar() or 0
        out.append(_fmt("tgbot_pending_transactions", pending_tx, "Pending top-up transactions"))

        products_active = s.query(func.count(Product.id)).filter(Product.is_active == True).scalar() or 0  # noqa: E712
        out.append(_fmt("tgbot_products_active", products_active, "Active products in catalog"))

        # Low stock alert — sum of stock <=5. Uses relationship to product_keys, so
        # we compute using a subquery of key counts.
        from database.models import ProductKey
        low = (
            s.query(func.count(Product.id))
            .outerjoin(ProductKey, (ProductKey.product_id == Product.id) & (ProductKey.is_sold == False))  # noqa: E712
            .filter(Product.is_active == True)  # noqa: E712
            .group_by(Product.id)
            .having(func.count(ProductKey.id) <= 5)
            .count()
        )
        out.append(_fmt("tgbot_low_stock_products", low, "Active products with ≤5 keys in stock"))

        # ── Churn rate ──────────────────────────────────────────────────
        churn = compute_churn_rate(s)
        out.append(_fmt("tgbot_churn_rate_percent", churn.churn_rate_pct,
                         "Percent of past buyers inactive beyond the churn window"))
        out.append(_fmt("tgbot_churned_customers", churn.churned_customers,
                         "Count of past buyers inactive beyond the churn window"))
        out.append(_fmt("tgbot_paying_customers", churn.paying_customers,
                         "Count of customers with at least one completed order"))

        # ── Customer Lifetime Value ─────────────────────────────────────
        ltv = compute_ltv(s)
        out.append(_fmt("tgbot_avg_ltv_usd", f"{ltv.overall_avg_ltv:.2f}",
                         "Average lifetime spend per paying customer (USD)"))
        out.append(_fmt("tgbot_median_ltv_usd", f"{ltv.overall_median_ltv:.2f}",
                         "Median lifetime spend per paying customer (USD)"))
        out.append(_fmt("tgbot_total_ltv_usd", f"{ltv.total_ltv:.2f}",
                         "Total completed-order revenue across all customers (USD)", "counter"))

        # ── Cohort retention (most recent cohorts only, keeps series count sane) ─
        cohorts = compute_cohort_retention(s, num_cohorts=3, num_periods=4)
        for row in cohorts:
            for period, pct in enumerate(row.retention_pct):
                if pct is None:
                    continue
                out.append(_fmt(
                    "tgbot_cohort_retention_percent", pct,
                    "Percent of a signup cohort with >=1 completed order in month M<n> after signup",
                    "gauge", {"cohort": row.cohort_key, "period": f"M{period}"},
                ))

    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/metrics":
            self.send_response(404); self.end_headers(); return
        try:
            body = collect_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            logger.exception("metrics collection failed")
            self.send_response(500); self.end_headers()

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    logger.info(f"Metrics exporter listening on :{PORT}/metrics")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception:
            logger.exception("exporter crashed, restarting in 5s")
            time.sleep(5)
