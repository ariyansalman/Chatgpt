"""Advanced Customer Analytics — cohort retention, LTV, churn rate.

Pure read-only query layer over the existing ``users`` / ``orders`` tables —
no new tables, same convention as ``services/customer_segmentation.py``.
Every number here is computed live from ``Order.status == OrderStatus.COMPLETED``
rows, so it always reflects the current state of the store.

Exposed metrics:

  * **Cohort retention** — group customers by the calendar month they signed
    up in (the "cohort"), then for each following month measure what
    percentage of that cohort placed at least one completed order. Classic
    month-0 / month-1 / month-2 ... retention table used in SaaS/e-commerce
    growth reporting.

  * **Customer Lifetime Value (LTV)** — total completed-order spend per
    customer, averaged overall and broken down per signup cohort, plus the
    current top spenders.

  * **Churn rate** — of customers who have ever completed a purchase, the
    percentage who have NOT completed an order within the configurable
    "inactivity window" (default 30 days), measured against their most
    recent completed order (or signup date if they only ever bought once
    a long time ago).

All functions take an open SQLAlchemy session (same pattern as
``customer_segmentation.py``) so callers control the ``with get_db_session()``
scope and can combine several of these calls in one transaction when
rendering a single dashboard screen.
"""
from __future__ import annotations

from calendar import month_name
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


from database import Order, OrderStatus, User
from utils.bot_config import cfg

MONTH_FMT = "%Y-%m"


def _month_key(dt: datetime) -> str:
    return dt.strftime(MONTH_FMT)


def _month_label(key: str) -> str:
    year, mon = key.split("-")
    return f"{month_name[int(mon)][:3]} {year}"


def _add_months(dt: datetime, n: int) -> datetime:
    year = dt.year + (dt.month - 1 + n) // 12
    month = (dt.month - 1 + n) % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _months_between(a: datetime, b: datetime) -> int:
    """Whole calendar months between two dates (b - a), floor-truncated to month."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def churn_inactive_days() -> int:
    """Admin-tunable inactivity window used to define "churned" (default 30d).

    Shares the same underlying config key as ``customer_segmentation``'s
    "inactive" audience so the two stay consistent with each other.
    """
    return cfg.get_int("seg_inactive_days", 30)


# ─────────────────────────────────────────────────────────────────────────
# Shared raw data loaders
# ─────────────────────────────────────────────────────────────────────────

def _load_user_signups(session) -> Dict[int, datetime]:
    rows = session.query(User.id, User.created_at).all()
    return {uid: created for uid, created in rows if created is not None}


def _load_completed_orders(session) -> List[Tuple[int, datetime, float]]:
    rows = (
        session.query(Order.user_id, Order.created_at, Order.total_amount)
        .filter(Order.status == OrderStatus.COMPLETED)
        .all()
    )
    return [(uid, created, float(amount or 0.0)) for uid, created, amount in rows if created is not None]


# ─────────────────────────────────────────────────────────────────────────
# Cohort retention
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class CohortRow:
    cohort_key: str
    cohort_label: str
    size: int
    # index i = retention % for month i after signup (0 = signup month itself)
    retention_pct: List[float] = field(default_factory=list)


def compute_cohort_retention(session, num_cohorts: int = 6, num_periods: int = 6) -> List[CohortRow]:
    """Monthly signup-cohort retention table.

    Returns the most recent ``num_cohorts`` signup-month cohorts (oldest
    first), each with a ``retention_pct`` list of length ``num_periods``:
    the percentage of that cohort's users who placed >=1 completed order
    in signup-month + i, for i in [0, num_periods).
    """
    signups = _load_user_signups(session)
    if not signups:
        return []
    orders = _load_completed_orders(session)

    # Group user_ids by cohort month key.
    cohort_users: Dict[str, List[int]] = {}
    for uid, created in signups.items():
        key = _month_key(created)
        cohort_users.setdefault(key, []).append(uid)

    now = datetime.utcnow()
    _month_key(now)
    all_cohort_keys = sorted(cohort_users.keys())
    # Only cohorts old enough to exist won't be filtered — just take the most recent N.
    cohort_keys = all_cohort_keys[-num_cohorts:]

    # For each user, the set of cohort-relative month offsets in which they ordered.
    # user_id -> set(months since signup with >=1 completed order)
    user_signup_dt = signups
    active_offsets: Dict[int, set] = {}
    for uid, created, _amount in orders:
        signup_dt = user_signup_dt.get(uid)
        if signup_dt is None:
            continue
        offset = _months_between(signup_dt.replace(day=1), created.replace(day=1))
        if offset < 0:
            continue  # order predates recorded signup month somehow — ignore
        active_offsets.setdefault(uid, set()).add(offset)

    rows: List[CohortRow] = []
    for key in cohort_keys:
        user_ids = cohort_users[key]
        size = len(user_ids)
        cohort_month_offset_from_now = _months_between(
            datetime.strptime(key, MONTH_FMT), now.replace(day=1)
        )
        retention: List[float] = []
        for period in range(num_periods):
            # Don't claim retention data for months that haven't happened yet.
            if period > cohort_month_offset_from_now:
                retention.append(None)  # type: ignore[arg-type]
                continue
            active = sum(1 for uid in user_ids if period in active_offsets.get(uid, ()))
            retention.append(round(100.0 * active / size, 1) if size else 0.0)
        rows.append(CohortRow(
            cohort_key=key,
            cohort_label=_month_label(key),
            size=size,
            retention_pct=retention,
        ))
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Customer Lifetime Value (LTV)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class LTVSummary:
    overall_avg_ltv: float
    overall_median_ltv: float
    paying_customers: int
    total_ltv: float
    per_cohort_avg_ltv: List[Tuple[str, float, int]]  # (cohort_label, avg_ltv, cohort_size)
    top_customers: List[Tuple[int, float]]  # (user_id, total_spend)


def compute_ltv(session, num_cohorts: int = 6, top_n: int = 5) -> LTVSummary:
    """Customer Lifetime Value — overall average/median, per-cohort average,
    and the current top spenders (by total completed-order spend to date).
    """
    signups = _load_user_signups(session)
    orders = _load_completed_orders(session)

    spend: Dict[int, float] = {}
    for uid, _created, amount in orders:
        spend[uid] = spend.get(uid, 0.0) + amount

    paying_values = list(spend.values())
    paying_customers = len(paying_values)
    total_ltv = sum(paying_values)
    overall_avg = (total_ltv / paying_customers) if paying_customers else 0.0

    sorted_values = sorted(paying_values)
    if sorted_values:
        mid = len(sorted_values) // 2
        median = (sorted_values[mid] if len(sorted_values) % 2
                  else (sorted_values[mid - 1] + sorted_values[mid]) / 2)
    else:
        median = 0.0

    # Per-cohort average LTV (all users in cohort, including non-buyers, so this
    # reflects true average revenue per acquired user — not just per buyer).
    cohort_users: Dict[str, List[int]] = {}
    for uid, created in signups.items():
        cohort_users.setdefault(_month_key(created), []).append(uid)
    cohort_keys = sorted(cohort_users.keys())[-num_cohorts:]

    per_cohort: List[Tuple[str, float, int]] = []
    for key in cohort_keys:
        uids = cohort_users[key]
        size = len(uids)
        cohort_total = sum(spend.get(uid, 0.0) for uid in uids)
        avg = (cohort_total / size) if size else 0.0
        per_cohort.append((_month_label(key), round(avg, 2), size))

    top_customers = sorted(spend.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    return LTVSummary(
        overall_avg_ltv=round(overall_avg, 2),
        overall_median_ltv=round(median, 2),
        paying_customers=paying_customers,
        total_ltv=round(total_ltv, 2),
        per_cohort_avg_ltv=per_cohort,
        top_customers=top_customers,
    )


# ─────────────────────────────────────────────────────────────────────────
# Churn rate
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ChurnSummary:
    inactive_days: int
    paying_customers: int
    churned_customers: int
    churn_rate_pct: float
    retained_rate_pct: float


def compute_churn_rate(session, inactive_days: Optional[int] = None) -> ChurnSummary:
    """Percentage of past buyers who haven't completed an order within the
    inactivity window, measured against each customer's own most recent
    completed order.
    """
    if inactive_days is None:
        inactive_days = churn_inactive_days()
    orders = _load_completed_orders(session)

    last_order: Dict[int, datetime] = {}
    for uid, created, _amount in orders:
        if uid not in last_order or created > last_order[uid]:
            last_order[uid] = created

    paying_customers = len(last_order)
    if paying_customers == 0:
        return ChurnSummary(inactive_days, 0, 0, 0.0, 0.0)

    cutoff = datetime.utcnow() - timedelta(days=inactive_days)
    churned = sum(1 for last_dt in last_order.values() if last_dt <= cutoff)
    churn_pct = round(100.0 * churned / paying_customers, 1)
    return ChurnSummary(
        inactive_days=inactive_days,
        paying_customers=paying_customers,
        churned_customers=churned,
        churn_rate_pct=churn_pct,
        retained_rate_pct=round(100.0 - churn_pct, 1),
    )
