"""Tests for services/customer_segmentation.py (V16).

Covers:
  * VIP segment (total completed spend threshold)
  * High-frequency segment (completed order count threshold)
  * Inactive segment (last completed order older than the window)
  * Never-purchased segment
  * Banned users are excluded from every segment
  * "all" segment matches the existing eligible-user behaviour
  * get_segment_counts() totals match get_segment_telegram_ids() lengths
"""
import os
import unittest
from datetime import datetime, timedelta


def _setup_inmemory():
    os.environ.setdefault("BOT_TOKEN", "test:test")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"


class CustomerSegmentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _setup_inmemory()
        import importlib
        from database import db as db_mod
        importlib.reload(db_mod)
        from database.models import Base
        Base.metadata.create_all(db_mod.engine)

        from utils.bot_config import seed_defaults, cfg
        seed_defaults()
        # Pin thresholds so the tests don't depend on the shipped defaults.
        cfg.set("seg_vip_spend_threshold", 100.0)
        cfg.set("seg_high_freq_order_count", 3)
        cfg.set("seg_inactive_days", 30)

        cls.db_mod = db_mod

    def _make_user(self, telegram_id, username, is_banned=False):
        from database import get_db_session, User
        with get_db_session() as s:
            u = User(telegram_id=telegram_id, username=username, is_banned=is_banned)
            s.add(u)
            s.commit()
            return u.id

    def _make_order(self, user_id, total_amount, status, created_at):
        from database import get_db_session, Order
        from database.models import OrderStatus
        with get_db_session() as s:
            o = Order(user_id=user_id, total_amount=total_amount,
                       status=status, created_at=created_at)
            s.add(o)
            s.commit()
            return o.id

    def test_vip_segment_matches_spend_threshold(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid_vip = self._make_user(4001, "vip_user")
        uid_regular = self._make_user(4002, "regular_user")

        self._make_order(uid_vip, 60.0, OrderStatus.COMPLETED, datetime.utcnow())
        self._make_order(uid_vip, 50.0, OrderStatus.COMPLETED, datetime.utcnow())  # total 110 >= 100
        self._make_order(uid_regular, 20.0, OrderStatus.COMPLETED, datetime.utcnow())  # total 20 < 100

        ids = set(seg.get_segment_telegram_ids(seg.SEG_VIP))
        self.assertIn(4001, ids)
        self.assertNotIn(4002, ids)

    def test_vip_segment_ignores_non_completed_orders(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid = self._make_user(4003, "cancelled_spender")
        self._make_order(uid, 500.0, OrderStatus.CANCELLED, datetime.utcnow())

        ids = set(seg.get_segment_telegram_ids(seg.SEG_VIP))
        self.assertNotIn(4003, ids)

    def test_high_frequency_segment_matches_order_count_threshold(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid_frequent = self._make_user(4004, "frequent_buyer")
        uid_occasional = self._make_user(4005, "occasional_buyer")

        for _ in range(3):
            self._make_order(uid_frequent, 5.0, OrderStatus.COMPLETED, datetime.utcnow())
        self._make_order(uid_occasional, 5.0, OrderStatus.COMPLETED, datetime.utcnow())

        ids = set(seg.get_segment_telegram_ids(seg.SEG_HIGH_FREQ))
        self.assertIn(4004, ids)
        self.assertNotIn(4005, ids)

    def test_inactive_segment_matches_stale_last_order(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid_stale = self._make_user(4006, "lapsed_buyer")
        uid_fresh = self._make_user(4007, "recent_buyer")

        self._make_order(uid_stale, 10.0, OrderStatus.COMPLETED,
                          datetime.utcnow() - timedelta(days=45))
        self._make_order(uid_fresh, 10.0, OrderStatus.COMPLETED,
                          datetime.utcnow() - timedelta(days=2))

        ids = set(seg.get_segment_telegram_ids(seg.SEG_INACTIVE))
        self.assertIn(4006, ids)
        self.assertNotIn(4007, ids)

    def test_never_purchased_segment_excludes_any_buyer(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid_never = self._make_user(4008, "browser_only")
        uid_bought = self._make_user(4009, "one_time_buyer")
        self._make_order(uid_bought, 10.0, OrderStatus.COMPLETED, datetime.utcnow())

        ids = set(seg.get_segment_telegram_ids(seg.SEG_NEVER_PURCHASED))
        self.assertIn(4008, ids)
        self.assertNotIn(4009, ids)

    def test_banned_users_excluded_from_every_segment(self):
        from database.models import OrderStatus
        from services import customer_segmentation as seg

        uid_banned_vip = self._make_user(4010, "banned_vip", is_banned=True)
        self._make_order(uid_banned_vip, 999.0, OrderStatus.COMPLETED, datetime.utcnow())

        for key, _label, _desc in seg.SEGMENT_DEFS:
            ids = set(seg.get_segment_telegram_ids(key))
            self.assertNotIn(4010, ids, f"banned user leaked into segment {key!r}")

    def test_all_segment_includes_every_non_banned_user(self):
        from services import customer_segmentation as seg
        from handlers.admin_broadcast_center import _eligible_user_ids_sync

        self.assertEqual(
            set(seg.get_segment_telegram_ids(seg.SEG_ALL)),
            set(_eligible_user_ids_sync()),
        )

    def test_segment_counts_match_id_list_lengths(self):
        from services import customer_segmentation as seg

        counts = seg.get_segment_counts()
        for key, _label, _desc in seg.SEGMENT_DEFS:
            self.assertEqual(counts[key], len(seg.get_segment_telegram_ids(key)))

    def test_unknown_segment_key_falls_back_to_all(self):
        from services import customer_segmentation as seg

        self.assertEqual(
            set(seg.get_segment_telegram_ids("not_a_real_segment")),
            set(seg.get_segment_telegram_ids(seg.SEG_ALL)),
        )


if __name__ == "__main__":
    unittest.main()
