"""services/order_lifecycle.py — transition mapping, idempotency, timeline.

Complements the DELIVERED -> COMPLETED coverage already in
tests/test_inventory_and_idempotency.py with the remaining lifecycle
states, invoice-dispatch idempotency, and render_timeline() formatting.
"""
import os
import unittest
from unittest import mock


def _setup_inmemory():
    os.environ.setdefault("BOT_TOKEN", "test:test")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"


class _BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _setup_inmemory()
        import importlib
        from database import db as db_mod
        importlib.reload(db_mod)
        from database.models import Base
        Base.metadata.create_all(db_mod.engine)

        import services.order_lifecycle as lc_mod
        importlib.reload(lc_mod)
        cls.lc = lc_mod

    def setUp(self):
        from database import get_db_session, User
        with get_db_session() as s:
            user = User(telegram_id=5000 + id(self) % 100000, username="buyer",
                       wallet_balance=0.0)
            s.add(user)
            s.commit()
            self.user_id = user.id

    def _make_order(self, total=19.99):
        from database import get_db_session, Order, OrderStatus
        with get_db_session() as s:
            o = Order(user_id=self.user_id, total_amount=total,
                      status=OrderStatus.PROCESSING)
            s.add(o)
            s.commit()
            return o.id


class LegacyStatusMappingTest(_BaseCase):
    """Every OrderLifecycleStatus must sync to the correct legacy OrderStatus."""

    def _assert_maps_to(self, lifecycle_status, expected_legacy):
        from database import OrderStatus, get_db_session, Order
        order_id = self._make_order()
        ok = self.lc.transition(order_id, lifecycle_status, send_invoice=False)
        self.assertTrue(ok)
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            self.assertEqual(order.lifecycle_status, lifecycle_status)
            self.assertEqual(order.status, expected_legacy)

    def test_pending_maps_to_processing(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.PENDING, OrderStatus.PROCESSING)

    def test_awaiting_payment_maps_to_processing(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.AWAITING_PAYMENT, OrderStatus.PROCESSING)

    def test_paid_maps_to_processing(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.PAID, OrderStatus.PROCESSING)

    def test_cancelled_maps_to_cancelled(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.CANCELLED, OrderStatus.CANCELLED)

    def test_failed_maps_to_cancelled(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.FAILED, OrderStatus.CANCELLED)

    def test_refunded_maps_to_cancelled(self):
        from database import OrderLifecycleStatus, OrderStatus
        self._assert_maps_to(OrderLifecycleStatus.REFUNDED, OrderStatus.CANCELLED)


class TransitionEdgeCasesTest(_BaseCase):
    def test_unknown_order_id_returns_false(self):
        from database import OrderLifecycleStatus
        ok = self.lc.transition(999_999, OrderLifecycleStatus.PAID, send_invoice=False)
        self.assertFalse(ok)

    def test_idempotent_same_status_transition_still_logs_history(self):
        """Transitioning to the SAME status twice is a documented no-op for
        the status itself, but each call still appends a history row (so
        admin notes attached to a repeat call aren't silently dropped)."""
        from database import OrderLifecycleStatus, get_db_session
        from database.models import OrderStatusHistory

        order_id = self._make_order()
        self.lc.transition(order_id, OrderLifecycleStatus.PROCESSING,
                          reason="first note", send_invoice=False)
        self.lc.transition(order_id, OrderLifecycleStatus.PROCESSING,
                          reason="second note", send_invoice=False)

        with get_db_session() as s:
            rows = (s.query(OrderStatusHistory)
                    .filter_by(order_id=order_id)
                    .order_by(OrderStatusHistory.id.asc()).all())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].reason, "first note")
        self.assertEqual(rows[1].reason, "second note")
        # Both rows recorded the same from/to since the status didn't change
        # on the second call... except the from_status reflects the PRIOR
        # value at the time of that specific call.
        self.assertEqual(rows[1].to_status, "PROCESSING")

    def test_completed_at_only_set_once_on_repeat_completion(self):
        """Repeating a COMPLETED transition must not reset completed_at or
        re-fire the invoice dispatch (idempotent completion)."""
        from database import OrderLifecycleStatus, get_db_session, Order

        order_id = self._make_order()
        with mock.patch.object(self.lc, "_dispatch_invoice_send") as mocked_dispatch:
            self.lc.transition(order_id, OrderLifecycleStatus.COMPLETED)
            with get_db_session() as s:
                first_completed_at = s.query(Order).filter_by(id=order_id).first().completed_at
            self.assertIsNotNone(first_completed_at)
            self.assertEqual(mocked_dispatch.call_count, 1)

            # Repeat the same transition — completed_at must be unchanged and
            # the invoice must NOT be dispatched a second time.
            self.lc.transition(order_id, OrderLifecycleStatus.COMPLETED)
            with get_db_session() as s:
                second_completed_at = s.query(Order).filter_by(id=order_id).first().completed_at
            self.assertEqual(first_completed_at, second_completed_at)
            self.assertEqual(mocked_dispatch.call_count, 1)

    def test_send_invoice_false_skips_dispatch(self):
        from database import OrderLifecycleStatus
        order_id = self._make_order()
        with mock.patch.object(self.lc, "_dispatch_invoice_send") as mocked_dispatch:
            self.lc.transition(order_id, OrderLifecycleStatus.COMPLETED, send_invoice=False)
            mocked_dispatch.assert_not_called()

    def test_sync_legacy_false_leaves_legacy_status_untouched(self):
        from database import OrderLifecycleStatus, OrderStatus, get_db_session, Order
        order_id = self._make_order()
        ok = self.lc.transition(order_id, OrderLifecycleStatus.COMPLETED,
                               sync_legacy=False, send_invoice=False)
        self.assertTrue(ok)
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            self.assertEqual(order.lifecycle_status, OrderLifecycleStatus.COMPLETED)
            # Legacy status stays at its original PROCESSING value.
            self.assertEqual(order.status, OrderStatus.PROCESSING)
            self.assertIsNone(order.completed_at)


class RenderTimelineTest(_BaseCase):
    def test_no_history_yet(self):
        order_id = self._make_order()
        text = self.lc.render_timeline(order_id)
        self.assertEqual(text, "— no history yet —")

    def test_timeline_includes_actor_and_reason(self):
        from database import OrderLifecycleStatus
        order_id = self._make_order()
        self.lc.transition(order_id, OrderLifecycleStatus.PAID,
                          actor_type="admin", admin_id=1,
                          reason="manual payment confirmed", send_invoice=False)
        text = self.lc.render_timeline(order_id)
        self.assertIn("[admin]", text)
        self.assertIn("manual payment confirmed", text)
        self.assertIn("→ PAID", text)

    def test_timeline_limit_caps_rows(self):
        from database import OrderLifecycleStatus
        order_id = self._make_order()
        # PROCESSING -> PROCESSING no-op transitions still append rows.
        for i in range(5):
            self.lc.transition(order_id, OrderLifecycleStatus.PROCESSING,
                              reason=f"note {i}", send_invoice=False)
        text = self.lc.render_timeline(order_id, limit=2)
        self.assertEqual(len(text.strip().split("\n")), 2)


if __name__ == "__main__":
    unittest.main()
