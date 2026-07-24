"""Customer Segmentation (V16) — audience segments for targeted broadcasts.

Pure read-only query layer over the existing ``users`` / ``orders`` tables —
no new tables, no parallel data store. Every segment is computed live from
``Order.status == OrderStatus.COMPLETED`` rows, so it always reflects the
current state of the store.

Segments:
  * ``all``      — every non-banned user (unchanged existing behaviour).
  * ``vip``      — total completed spend at/above the VIP threshold.
  * ``hf``       — completed-order count at/above the frequency threshold.
  * ``inactive`` — has purchased before, but not within the inactivity
    window (based on their most recent completed order).
  * ``never``    — signed up but has never completed an order.

Thresholds are admin-tunable via the existing ``bot_config`` system (see
``utils/bot_config.py`` — category "segmentation") so no code change is
needed to retune them.

Integrates with ``handlers/admin_broadcast_center.py``, which lets the admin
pick one of these segments as the audience for a Product or Custom
broadcast instead of always messaging every user.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from sqlalchemy import func

from database import get_db_session, User, Order, OrderStatus
from utils.bot_config import cfg

# ── Segment keys (also used as callback-data tokens — keep them short) ────
SEG_ALL = "all"
SEG_VIP = "vip"
SEG_HIGH_FREQ = "hf"
SEG_INACTIVE = "inactive"
SEG_NEVER_PURCHASED = "never"

# (key, label, description) — order here is the display order everywhere.
SEGMENT_DEFS: List[Tuple[str, str, str]] = [
    (SEG_ALL, "👥 All Users", "Every non-banned user."),
    (SEG_VIP, "💎 VIP (High Spenders)", "Total completed spend at or above the VIP threshold."),
    (SEG_HIGH_FREQ, "🔁 High-Frequency Buyers", "Completed orders at or above the frequency threshold."),
    (SEG_INACTIVE, "😴 Inactive (Lapsed)", "Has purchased before, but not within the inactivity window."),
    (SEG_NEVER_PURCHASED, "🆕 Never Purchased", "Signed up but has never completed an order."),
]
_SEGMENT_KEYS = {key for key, _label, _desc in SEGMENT_DEFS}


def _vip_threshold() -> float:
    return cfg.get_float("seg_vip_spend_threshold", 100.0)


def _high_freq_threshold() -> int:
    return cfg.get_int("seg_high_freq_order_count", 3)


def _inactive_days() -> int:
    return cfg.get_int("seg_inactive_days", 30)


def segment_label(segment_key: str) -> str:
    for key, label, _desc in SEGMENT_DEFS:
        if key == segment_key:
            return label
    return dict(((k, l) for k, l, _d in SEGMENT_DEFS))[SEG_ALL]


def _completed_order_stats(session) -> Tuple[Dict[int, float], Dict[int, int], Dict[int, datetime]]:
    """Per-user (total completed spend, completed order count, last completed order date)."""
    rows = (
        session.query(
            Order.user_id,
            func.sum(Order.total_amount),
            func.count(Order.id),
            func.max(Order.created_at),
        )
        .filter(Order.status == OrderStatus.COMPLETED)
        .group_by(Order.user_id)
        .all()
    )
    spend: Dict[int, float] = {}
    count: Dict[int, int] = {}
    last_order: Dict[int, datetime] = {}
    for user_id, total, cnt, last_dt in rows:
        spend[user_id] = float(total or 0.0)
        count[user_id] = int(cnt or 0)
        last_order[user_id] = last_dt
    return spend, count, last_order


def _matches_segment(segment_key: str, user_id: int, spend: Dict[int, float],
                      count: Dict[int, int], last_order: Dict[int, datetime],
                      inactive_cutoff: datetime) -> bool:
    if segment_key == SEG_ALL:
        return True
    if segment_key == SEG_VIP:
        return spend.get(user_id, 0.0) >= _vip_threshold()
    if segment_key == SEG_HIGH_FREQ:
        return count.get(user_id, 0) >= _high_freq_threshold()
    if segment_key == SEG_INACTIVE:
        last_dt = last_order.get(user_id)
        return last_dt is not None and last_dt <= inactive_cutoff
    if segment_key == SEG_NEVER_PURCHASED:
        return count.get(user_id, 0) == 0
    return True  # unknown segment key — fail open to "all" semantics


def _segment_telegram_ids_sync(segment_key: str) -> List[int]:
    """Runs in a worker thread — same convention as ``_eligible_user_ids_sync``."""
    if segment_key not in _SEGMENT_KEYS:
        segment_key = SEG_ALL
    with get_db_session() as s:
        users = s.query(User.id, User.telegram_id).filter_by(is_banned=False).all()
        if segment_key == SEG_ALL:
            return [tid for _uid, tid in users]

        spend, count, last_order = _completed_order_stats(s)
        cutoff = datetime.utcnow() - timedelta(days=_inactive_days())
        return [
            tid for uid, tid in users
            if _matches_segment(segment_key, uid, spend, count, last_order, cutoff)
        ]


def _segment_counts_sync() -> Dict[str, int]:
    """Counts for every segment in one pass — used to render the picker."""
    with get_db_session() as s:
        user_ids = [uid for (uid,) in s.query(User.id).filter_by(is_banned=False).all()]
        spend, count, last_order = _completed_order_stats(s)
        cutoff = datetime.utcnow() - timedelta(days=_inactive_days())
        counts: Dict[str, int] = {}
        for key, _label, _desc in SEGMENT_DEFS:
            counts[key] = sum(
                1 for uid in user_ids
                if _matches_segment(key, uid, spend, count, last_order, cutoff)
            )
        return counts


# ── Public, sync API (callers wrap in asyncio.to_thread from handlers) ────

def get_segment_telegram_ids(segment_key: str) -> List[int]:
    """Telegram IDs of non-banned users belonging to ``segment_key``."""
    return _segment_telegram_ids_sync(segment_key)


def get_segment_counts() -> Dict[str, int]:
    """``{segment_key: user_count}`` for every known segment."""
    return _segment_counts_sync()


def get_segment_count(segment_key: str) -> int:
    return len(get_segment_telegram_ids(segment_key))
