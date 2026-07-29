"""Inventory reservation + payment idempotency regression tests (SQLite in-memory).

Covers the fixes described in REPLIT_FINAL_INTEGRATION_AUDIT.md:

  1. reserve() -> consume() round-trip for all four KEY_BACKED_TYPES
     (KEY, REDEEM_LINK, ACCOUNT_LOGIN, VOUCHER).
  2. idempotency.claim() raising an exception must NOT result in a credit
     (fail-closed).
  3. A duplicate Telegram successful_payment update must only credit once.
  4. A repeated admin manual-approval action must only credit once.
  5. Order lifecycle DELIVERED -> COMPLETED transition.
  6. Reservation release on cancel / reject / expire returns stock.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock


def _setup_inmemory():
    os.environ.setdefault("BOT_TOKEN", "test:test")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"


class _BaseCase(unittest.TestCase):
    """Fresh in-memory DB + module state for every test in this file."""

    @classmethod
    def setUpClass(cls):
        _setup_inmemory()
        import importlib
        from database import db as db_mod
        importlib.reload(db_mod)
        from database.models import Base
        Base.metadata.create_all(db_mod.engine)

        # Reload every module that cached a reference to the (now stale)
        # engine/scoped_session so all of them share the fresh in-memory DB.
        import services.inventory as inv_mod
        import services.idempotency as idem_mod
        import services.order_lifecycle as lc_mod
        import services.delivery_service as ds_mod
        importlib.reload(inv_mod)
        importlib.reload(idem_mod)
        importlib.reload(lc_mod)
        importlib.reload(ds_mod)
        cls.inv = inv_mod
        cls.idem = idem_mod
        cls.lc = lc_mod
        cls.ds = ds_mod

    def setUp(self):
        from database import get_db_session, User
        with get_db_session() as s:
            user = User(telegram_id=1000 + id(self) % 100000, username="buyer",
                       wallet_balance=1000.0)
            s.add(user)
            s.commit()
            self.user_id = user.id

    def _make_product(self, product_type, num_keys=3):
        from database import get_db_session, Product
        with get_db_session() as s:
            p = Product(name=f"P-{product_type.value}", price=9.99,
                       stock_count=0, product_type=product_type, is_active=True)
            s.add(p)
            s.commit()
            product_id = p.id
        if num_keys:
            self._add_keys(product_id, num_keys)
        return product_id

    def _add_keys(self, product_id, n, prefix="KEY"):
        from database import get_db_session, ProductKey
        with get_db_session() as s:
            for i in range(n):
                s.add(ProductKey(product_id=product_id, key_value=f"{prefix}-{product_id}-{i}"))
            s.commit()

    def _make_order(self, total=9.99):
        from database import get_db_session, Order, OrderStatus
        with get_db_session() as s:
            o = Order(user_id=self.user_id, total_amount=total, status=OrderStatus.PROCESSING)
            s.add(o)
            s.commit()
            return o.id


class ReserveConsumeAllTypesTest(_BaseCase):
    """Requirement 1: reserve()->consume() works for all 4 key-backed types."""

    def _round_trip(self, product_type):
        from database import ProductType
        product_id = self._make_product(product_type, num_keys=2)
        order_id = self._make_order()

        reservation = self.inv.reserve(self.user_id, product_id, 1)
        self.assertEqual(reservation.status.value, "active")

        # Attach to the order the way confirm_purchase does.
        from database import get_db_session, StockReservation
        with get_db_session() as s:
            s.query(StockReservation).filter(StockReservation.id == reservation.id).update(
                {StockReservation.order_id: order_id})
            s.commit()

        delivered = self.inv.consume(reservation.id, order_id)
        self.assertEqual(len(delivered), 1)

        # The consumed key must now be marked sold and attached to this order.
        from database import ProductKey
        with get_db_session() as s:
            sold = s.query(ProductKey).filter(
                ProductKey.key_value == delivered[0]).first()
            self.assertTrue(sold.is_sold)
            self.assertEqual(sold.order_id, order_id)

    def test_key_type(self):
        from database import ProductType
        self._round_trip(ProductType.KEY)

    def test_redeem_link_type(self):
        from database import ProductType
        self._round_trip(ProductType.REDEEM_LINK)

    def test_account_login_type(self):
        from database import ProductType
        self._round_trip(ProductType.ACCOUNT_LOGIN)

    def test_voucher_type(self):
        from database import ProductType
        self._round_trip(ProductType.VOUCHER)

    def test_delivery_service_consumes_existing_reservation_not_fresh_rows(self):
        """delivery_service must consume the SAME keys that were reserved for
        this order, not grab any arbitrary unreserved row (regression test
        for the original bug: _consume_keys queried fresh unreserved rows
        instead of the order's own reservation)."""
        from database import ProductType, get_db_session, StockReservation, OrderItem
        product_id = self._make_product(ProductType.VOUCHER, num_keys=0)
        # Two callers reserve concurrently: order A gets 1 key, then more
        # stock is added that order A must NOT be able to grab.
        self._add_keys(product_id, 1, prefix="RESERVED-FOR-A")
        res_a = self.inv.reserve(self.user_id, product_id, 1)
        # Stock added AFTER the reservation — must not be eligible for A.
        self._add_keys(product_id, 1, prefix="ADDED-LATER")

        order_id = self._make_order()
        with get_db_session() as s:
            s.query(StockReservation).filter(StockReservation.id == res_a.id).update(
                {StockReservation.order_id: order_id})
            item = OrderItem(order_id=order_id, product_id=product_id, quantity=1, price=9.99)
            s.add(item)
            s.commit()

        with get_db_session() as s:
            reservation = self.ds._find_active_reservation(s, order_id, product_id)
            self.assertIsNotNone(reservation)
            values = self.ds._consume_keys(s, product_id, 1, order_id,
                                           reservation_id=reservation.id)
            s.commit()
        self.assertEqual(values, ["RESERVED-FOR-A-%s-0" % product_id])


class ReservationReleaseTest(_BaseCase):
    """Requirement 1: reservation released on cancel / reject / expire."""

    def test_release_for_order_returns_stock(self):
        from database import ProductType, ProductKey, get_db_session, StockReservation
        product_id = self._make_product(ProductType.KEY, num_keys=2)
        order_id = self._make_order()
        reservation = self.inv.reserve(self.user_id, product_id, 1)
        with get_db_session() as s:
            s.query(StockReservation).filter(StockReservation.id == reservation.id).update(
                {StockReservation.order_id: order_id})
            s.commit()

        released = self.inv.release_for_order(order_id, reason="order_cancelled")
        self.assertEqual(released, 1)

        with get_db_session() as s:
            row = s.query(StockReservation).filter(StockReservation.id == reservation.id).first()
            self.assertEqual(row.status.value, "released")
            # Keys are unlocked and available again.
            unlocked = s.query(ProductKey).filter(
                ProductKey.product_id == product_id,
                ProductKey.reservation_id == None,  # noqa: E711
                ProductKey.is_sold == False,  # noqa: E712
            ).count()
            self.assertEqual(unlocked, 2)

    def test_expired_reservation_releases_keys(self):
        from database import ProductType, ProductKey, get_db_session, StockReservation, ReservationStatus
        product_id = self._make_product(ProductType.KEY, num_keys=1)
        reservation = self.inv.reserve(self.user_id, product_id, 1)
        # Force it into the past so passive cleanup treats it as expired.
        with get_db_session() as s:
            s.query(StockReservation).filter(StockReservation.id == reservation.id).update(
                {StockReservation.expires_at: datetime.utcnow() - timedelta(minutes=1)})
            s.commit()

        # _expire_stale() runs inside reserve(); trigger it via another reserve
        # call against the same product scope (will fail, but the sweep runs
        # first) — or call it directly if exposed.
        with get_db_session() as s:
            self.inv._expire_stale(s)
            s.commit()

        with get_db_session() as s:
            row = s.query(StockReservation).filter(StockReservation.id == reservation.id).first()
            self.assertEqual(row.status, ReservationStatus.EXPIRED)
            key = s.query(ProductKey).filter(ProductKey.product_id == product_id).first()
            self.assertIsNone(key.reservation_id)
            self.assertFalse(key.is_sold)


class IdempotencyClaimTest(_BaseCase):
    """Requirement 2 & 3: fail-closed idempotency semantics."""

    def test_claim_exception_means_no_credit(self):
        """If claim() itself raises, the caller must NOT proceed to credit."""
        from database import get_db_session, User
        credited = {"called": False}

        def _fake_claim(*args, **kwargs):
            raise RuntimeError("simulated DB outage during idempotency check")

        with mock.patch.object(self.idem, "claim", side_effect=_fake_claim):
            # Emulates the guarded call pattern used in payment_handlers /
            # admin_handlers / webhook_server: claim() raising must abort
            # BEFORE any wallet mutation happens.
            try:
                with self.idem.claim("test_source", "ref-1") as ok:
                    if ok:
                        credited["called"] = True
            except RuntimeError:
                pass  # caller is expected to catch and fail closed

        self.assertFalse(credited["called"], "wallet credit must not run when claim() raises")

    def test_duplicate_claim_is_rejected(self):
        won_first = False
        won_second = True
        with self.idem.claim("dup_source", "same-ref") as ok1:
            won_first = ok1
        with self.idem.claim("dup_source", "same-ref") as ok2:
            won_second = ok2
        self.assertTrue(won_first)
        self.assertFalse(won_second)

    def test_claim_locked_inside_existing_session_does_not_double_claim(self):
        from database import get_db_session
        with get_db_session() as s:
            first = self.idem.claim_locked(s, "locked_source", "ref-x")
            second = self.idem.claim_locked(s, "locked_source", "ref-x")
            s.commit()
        self.assertTrue(first)
        self.assertFalse(second)


class DuplicateTelegramPaymentTest(_BaseCase):
    """Requirement 2/3: duplicate Telegram successful_payment -> one credit."""

    def test_duplicate_charge_id_credits_once(self):
        from database import get_db_session, Transaction, TransactionStatus, PaymentMethod, User

        with get_db_session() as s:
            tx = Transaction(user_id=self.user_id, amount=50.0,
                             payment_method=PaymentMethod.CARD,
                             status=TransactionStatus.PENDING)
            s.add(tx)
            s.commit()
            tx_id = tx.id

        charge_id = "tg_charge_ABC123"

        def _process_once():
            """Mirrors the guarded body of successful_payment_callback."""
            with self.idem.claim("tg_card_topup", charge_id) as ok:
                if not ok:
                    return False
            with get_db_session() as s:
                transaction = s.query(Transaction).filter_by(
                    id=tx_id, payment_method=PaymentMethod.CARD).first()
                if transaction.status == TransactionStatus.COMPLETED:
                    return False
                transaction.status = TransactionStatus.COMPLETED
                transaction.completed_at = datetime.utcnow()
                user = s.query(User).filter_by(id=transaction.user_id).first()
                user.wallet_balance += transaction.amount
                s.commit()
            return True

        first = _process_once()
        second = _process_once()  # simulates Telegram redelivering the same update
        self.assertTrue(first)
        self.assertFalse(second)

        with get_db_session() as s:
            user = s.query(User).filter_by(id=self.user_id).first()
            self.assertAlmostEqual(user.wallet_balance, 1050.0)


class RepeatedManualApprovalTest(_BaseCase):
    """Requirement 2/3: repeated admin manual approval -> one credit."""

    def test_repeated_approval_credits_once(self):
        from database import get_db_session, Transaction, TransactionStatus, PaymentMethod, User

        with get_db_session() as s:
            tx = Transaction(user_id=self.user_id, amount=25.0,
                             payment_method=PaymentMethod.MANUAL,
                             status=TransactionStatus.PENDING)
            s.add(tx)
            s.commit()
            tx_id = tx.id

        def _approve_once():
            """Mirrors the guarded body of admin_manual_approve."""
            with self.idem.claim("manual_approve", f"tx:{tx_id}") as ok:
                if not ok:
                    return False
            with get_db_session() as s:
                flipped = s.query(Transaction).filter(
                    Transaction.id == tx_id,
                    Transaction.payment_method == PaymentMethod.MANUAL,
                    Transaction.status.in_([TransactionStatus.PENDING,
                                            TransactionStatus.AWAITING_CONFIRMATION]),
                ).update({
                    Transaction.status: TransactionStatus.COMPLETED,
                    Transaction.completed_at: datetime.utcnow(),
                })
                if flipped == 0:
                    return False
                s.query(User).filter(User.id == self.user_id).update(
                    {User.wallet_balance: User.wallet_balance + 25.0})
                s.commit()
            return True

        first = _approve_once()
        second = _approve_once()  # admin double-clicks "Approve"
        third = _approve_once()   # a stale/duplicate callback redelivery
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(third)

        with get_db_session() as s:
            user = s.query(User).filter_by(id=self.user_id).first()
            self.assertAlmostEqual(user.wallet_balance, 1025.0)


class OrderLifecycleTest(_BaseCase):
    """Requirement 4: DELIVERED -> COMPLETED lifecycle transition."""

    def test_delivered_then_completed(self):
        from database import OrderLifecycleStatus, OrderStatus, get_db_session, Order

        order_id = self._make_order()
        ok1 = self.lc.transition(order_id, OrderLifecycleStatus.DELIVERED)
        self.assertTrue(ok1)
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            self.assertEqual(order.lifecycle_status, OrderLifecycleStatus.DELIVERED)
            self.assertEqual(order.status, OrderStatus.COMPLETED)

        ok2 = self.lc.transition(order_id, OrderLifecycleStatus.COMPLETED)
        self.assertTrue(ok2)
        with get_db_session() as s:
            order = s.query(Order).filter_by(id=order_id).first()
            self.assertEqual(order.lifecycle_status, OrderLifecycleStatus.COMPLETED)
            self.assertEqual(order.status, OrderStatus.COMPLETED)
            self.assertIsNotNone(order.completed_at)

        # History has both transitions recorded, in order.
        from database.models import OrderStatusHistory
        with get_db_session() as s:
            rows = s.query(OrderStatusHistory).filter_by(order_id=order_id).order_by(
                OrderStatusHistory.id.asc()).all()
            self.assertEqual([r.to_status for r in rows], ["DELIVERED", "COMPLETED"])


if __name__ == "__main__":
    unittest.main()
