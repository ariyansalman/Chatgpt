"""Regression tests for two inventory-consistency fixes.

Fix 1 — Cart Reserved Inventory Delivery Consistency
=====================================================
For cart checkouts containing KEY / REDEEM_LINK / ACCOUNT_LOGIN / VOUCHER
products the delivery flow must consume the SAME ``ProductKey`` rows that
were locked by ``inventory.reserve()`` at checkout time, not arbitrary
unreserved rows.

For each test:
  • A is reserved (locked to the reservation).
  • B is a second, *unreserved* key added after the reservation is created.
  • Fulfillment runs.
  • A must be the delivered key.
  • B must remain unsold and unassigned.

Fix 2 — Atomic Bundle Inventory Reservation
============================================
``inventory.reserve_bundle()`` must atomically reserve all key-backed child
inventory inside a single database transaction. Partial failure must roll back
every lock created in that attempt.

Additionally ``delivery_service.deliver_bundle()`` must consume the child
reservations created by ``reserve_bundle()`` rather than querying fresh
unreserved rows.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest import mock


def _setup_inmemory():
    os.environ.setdefault("BOT_TOKEN", "test:test")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"


class _BaseCase(unittest.TestCase):
    """Fresh in-memory DB + reloaded module state for each test class."""

    @classmethod
    def setUpClass(cls):
        _setup_inmemory()
        import importlib
        from database import db as db_mod

        importlib.reload(db_mod)
        from database.models import Base

        Base.metadata.create_all(db_mod.engine)

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
            user = User(
                telegram_id=2000 + id(self) % 100000,
                username="buyer",
                wallet_balance=1000.0,
            )
            s.add(user)
            s.commit()
            self.user_id = user.id

    # ── helpers ──────────────────────────────────────────────────────

    def _make_product(self, product_type, num_keys: int = 0):
        from database import get_db_session, Product

        with get_db_session() as s:
            p = Product(
                name=f"P-{product_type.value}-{id(self)}",
                price=9.99,
                stock_count=0,
                product_type=product_type,
                is_active=True,
            )
            s.add(p)
            s.commit()
            product_id = p.id

        if num_keys:
            self._add_keys(product_id, num_keys)
        return product_id

    def _add_keys(self, product_id: int, n: int, prefix: str = "KEY"):
        from database import get_db_session, ProductKey

        with get_db_session() as s:
            for i in range(n):
                s.add(
                    ProductKey(
                        product_id=product_id,
                        key_value=f"{prefix}-{product_id}-{id(self)}-{i}",
                    )
                )
            s.commit()

    def _get_key_values(self, product_id: int, *, sold: bool | None = None):
        """Return all key_value strings for a product, optionally filtered by sold status."""
        from database import get_db_session, ProductKey

        with get_db_session() as s:
            q = s.query(ProductKey).filter(ProductKey.product_id == product_id)
            if sold is True:
                q = q.filter(ProductKey.is_sold == True)  # noqa: E712
            elif sold is False:
                q = q.filter(ProductKey.is_sold == False)  # noqa: E712
            return [k.key_value for k in q.all()]

    def _make_order(self, total: float = 9.99):
        from database import get_db_session, Order, OrderStatus

        with get_db_session() as s:
            o = Order(
                user_id=self.user_id,
                total_amount=total,
                status=OrderStatus.PROCESSING,
            )
            s.add(o)
            s.commit()
            return o.id

    def _attach_reservation(self, reservation_id: int, order_id: int):
        from database import get_db_session, StockReservation

        with get_db_session() as s:
            s.query(StockReservation).filter_by(id=reservation_id).update(
                {"order_id": order_id}
            )
            s.commit()

    def _attach_reservations(self, reservation_ids: list[int], order_id: int):
        from database import get_db_session, StockReservation

        if not reservation_ids:
            return
        with get_db_session() as s:
            s.query(StockReservation).filter(
                StockReservation.id.in_(reservation_ids)
            ).update({"order_id": order_id})
            s.commit()

    def _first_key_value(self, product_id: int) -> str:
        """Return the key_value of the first ProductKey row for this product."""
        from database import get_db_session, ProductKey

        with get_db_session() as s:
            k = (
                s.query(ProductKey)
                .filter(ProductKey.product_id == product_id)
                .order_by(ProductKey.id.asc())
                .first()
            )
            return k.key_value if k else ""

    def _second_key_value(self, product_id: int) -> str:
        from database import get_db_session, ProductKey

        with get_db_session() as s:
            keys = (
                s.query(ProductKey)
                .filter(ProductKey.product_id == product_id)
                .order_by(ProductKey.id.asc())
                .all()
            )
            return keys[1].key_value if len(keys) >= 2 else ""

    def _get_reservation_id(self, product_id: int, user_id: int | None = None) -> int | None:
        from database import get_db_session, StockReservation, ReservationStatus

        uid = user_id if user_id is not None else self.user_id
        with get_db_session() as s:
            r = (
                s.query(StockReservation)
                .filter(
                    StockReservation.product_id == product_id,
                    StockReservation.user_id == uid,
                    StockReservation.status == ReservationStatus.ACTIVE,
                )
                .order_by(StockReservation.id.desc())
                .first()
            )
            return r.id if r else None


# ─────────────────────────────────────────────────────────────────────
# Fix 1 — Cart reservation delivery consistency for KEY_BACKED_TYPES
# ─────────────────────────────────────────────────────────────────────


class CartReservationKeyTest(_BaseCase):
    """KEY type: fulfillment delivers the RESERVED key, not an arbitrary one."""

    def test_cart_key_delivers_reserved_key_not_unreserved(self):
        from database import get_db_session, ProductKey, ProductType

        product_id = self._make_product(ProductType.KEY, num_keys=1)
        # A is the first (reserved) key
        key_A = self._first_key_value(product_id)

        reservation = self.inv.reserve(self.user_id, product_id, 1)
        order_id = self._make_order()
        self._attach_reservation(reservation.id, order_id)

        # B is added AFTER the reservation is created — must NOT be delivered
        self._add_keys(product_id, 1, prefix="UNRESERVED")
        key_B = self._second_key_value(product_id)
        self.assertNotEqual(key_A, key_B, "test data: A and B must differ")

        # Consume via consume_locked (same path used by _consume_keys with reservation_id)
        from database import get_db_session as _gs

        with _gs() as s:
            delivered = self.inv.consume_locked(s, reservation.id, order_id)
            s.commit()

        self.assertEqual(delivered, [key_A], "must deliver the reserved key A")

        # B must remain unsold
        unsold = self._get_key_values(product_id, sold=False)
        self.assertIn(key_B, unsold, "unreserved key B must remain unsold")
        sold = self._get_key_values(product_id, sold=True)
        self.assertNotIn(key_B, sold, "B must not be sold")


class CartReservationRedeemLinkTest(_BaseCase):
    """REDEEM_LINK type: delivery_service uses the reserved row, not a fresh one."""

    def _run_delivery_for_type(self, product_type):
        from database import get_db_session, OrderItem, ProductKey

        product_id = self._make_product(product_type, num_keys=1)
        key_A = self._first_key_value(product_id)

        reservation = self.inv.reserve(self.user_id, product_id, 1)
        order_id = self._make_order()
        self._attach_reservation(reservation.id, order_id)

        # B added after reservation — must NOT be delivered
        self._add_keys(product_id, 1, prefix="UNRESERVED")
        key_B = self._second_key_value(product_id)

        with get_db_session() as s:
            from database import Order

            order = s.query(Order).filter_by(id=order_id).first()
            oi = OrderItem(
                order_id=order_id,
                product_id=product_id,
                quantity=1,
                price=9.99,
            )
            s.add(oi)
            s.flush()

            from database import Product

            product = s.query(Product).filter_by(id=product_id).first()
            result = self.ds._deliver_inventory_list(
                s, order, oi, product,
                f"Test {product_type.value}"
            )
            s.commit()

        self.assertTrue(result.success, f"delivery failed: {result.error}")
        # Must have delivered A
        self.assertIn(key_A, result.assets, "reserved key A must be in delivered assets")
        # B must be untouched
        unsold = self._get_key_values(product_id, sold=False)
        self.assertIn(key_B, unsold, "unreserved key B must remain unsold")
        sold = self._get_key_values(product_id, sold=True)
        self.assertNotIn(key_B, sold, "B must not be sold")

    def test_cart_redeem_link_delivers_reserved(self):
        from database import ProductType
        self._run_delivery_for_type(ProductType.REDEEM_LINK)

    def test_cart_account_login_delivers_reserved(self):
        from database import ProductType
        self._run_delivery_for_type(ProductType.ACCOUNT_LOGIN)

    def test_cart_voucher_delivers_reserved(self):
        from database import ProductType
        self._run_delivery_for_type(ProductType.VOUCHER)


class CartMultiItemDispatchTest(_BaseCase):
    """dispatch(order_item_id=N) must deliver the named item, not always items[0]."""

    def test_dispatch_uses_specified_order_item_id(self):
        from database import (
            get_db_session, Order, OrderItem, ProductType, ProductKey, Product,
        )

        # Two REDEEM_LINK products, each with one key
        pid1 = self._make_product(ProductType.REDEEM_LINK, num_keys=1)
        pid2 = self._make_product(ProductType.REDEEM_LINK, num_keys=1)
        key1 = self._first_key_value(pid1)
        key2 = self._first_key_value(pid2)

        res1 = self.inv.reserve(self.user_id, pid1, 1)
        res2 = self.inv.reserve(self.user_id, pid2, 1)

        order_id = self._make_order()
        self._attach_reservations([res1.id, res2.id], order_id)

        with get_db_session() as s:
            from database import Order as _Order

            order = s.query(_Order).filter_by(id=order_id).first()

            oi1 = OrderItem(order_id=order_id, product_id=pid1, quantity=1, price=9.99)
            oi2 = OrderItem(order_id=order_id, product_id=pid2, quantity=1, price=9.99)
            s.add(oi1)
            s.add(oi2)
            s.flush()
            oi1_id = oi1.id
            oi2_id = oi2.id

            # Dispatch for oi2 specifically — must deliver product 2's key, not product 1's
            result = self.ds.dispatch(order_id, session=s, order_item_id=oi2_id)
            s.commit()

        self.assertTrue(result.handled, "dispatch must handle REDEEM_LINK")
        self.assertTrue(result.success, f"delivery failed: {result.error}")
        self.assertIn(key2, result.assets, "item 2's key must be delivered")
        self.assertNotIn(key1, result.assets, "item 1's key must NOT be delivered for item 2")


# ─────────────────────────────────────────────────────────────────────
# Fix 2 — Atomic Bundle Inventory Reservation
# ─────────────────────────────────────────────────────────────────────


class BundleAtomicReservationTest(_BaseCase):
    """reserve_bundle() atomically locks child inventory."""

    def _make_bundle(self, child_types_and_keys: list[tuple]) -> tuple[int, list[int]]:
        """Create a BUNDLE parent and child products.

        ``child_types_and_keys`` is a list of (ProductType, num_keys) tuples.
        Returns (bundle_product_id, [child_product_id, ...]).
        """
        from database import get_db_session, Product, BundleItem, ProductType

        bundle_id = self._make_product(ProductType.BUNDLE, num_keys=0)
        child_ids = []

        with get_db_session() as s:
            for ptype, nkeys in child_types_and_keys:
                child_id = self._make_product(ptype, num_keys=nkeys)
                child_ids.append(child_id)
                s.add(
                    BundleItem(
                        parent_product_id=bundle_id,
                        child_product_id=child_id,
                        quantity=1,
                    )
                )
            s.commit()

        return bundle_id, child_ids

    # ── test 1: all children available → all reservations succeed ──────

    def test_bundle_all_children_available_reserves_all(self):
        from database import ProductType, StockReservation, get_db_session, ReservationStatus

        bundle_id, child_ids = self._make_bundle(
            [
                (ProductType.KEY, 2),
                (ProductType.REDEEM_LINK, 2),
            ]
        )

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, 1)

        self.assertEqual(len(child_reservations), 2, "both children must be reserved")
        for r in child_reservations:
            self.assertEqual(r.status.value, "active")

        # Verify keys are locked
        for r in child_reservations:
            with get_db_session() as s:
                from database import ProductKey
                locked = (
                    s.query(ProductKey)
                    .filter_by(reservation_id=r.id, is_sold=False)
                    .count()
                )
                self.assertGreater(locked, 0, "child reservation must hold locked keys")

    # ── test 2: child 2 unavailable → zero active reservations remain ──

    def test_bundle_child_unavailable_atomic_rollback(self):
        from database import ProductType, StockReservation, get_db_session, ReservationStatus

        # Child 1 has stock; child 2 has NONE
        bundle_id, child_ids = self._make_bundle(
            [
                (ProductType.KEY, 2),        # child 1 — has stock
                (ProductType.REDEEM_LINK, 0), # child 2 — NO stock
            ]
        )

        with self.assertRaises(self.inv.ReservationError):
            self.inv.reserve_bundle(self.user_id, bundle_id, 1)

        # Verify zero active reservations remain for child 1 (rollback must have freed keys)
        with get_db_session() as s:
            active = (
                s.query(StockReservation)
                .filter(
                    StockReservation.product_id.in_(child_ids),
                    StockReservation.status == ReservationStatus.ACTIVE,
                )
                .count()
            )
            self.assertEqual(
                active, 0,
                "partial bundle reservation must be fully rolled back — no active reservations"
            )

            # Keys for child 1 must be free (reservation_id = NULL)
            from database import ProductKey
            locked = (
                s.query(ProductKey)
                .filter(
                    ProductKey.product_id == child_ids[0],
                    ProductKey.reservation_id != None,  # noqa: E711
                )
                .count()
            )
            self.assertEqual(
                locked, 0,
                "child 1 keys must be unlocked after rollback"
            )

    # ── test 3: bundle reservation failure → wallet is not debited ──────

    def test_bundle_reservation_failure_wallet_not_debited(self):
        from database import ProductType, get_db_session, User

        # Verify initial wallet balance
        with get_db_session() as s:
            user = s.query(User).filter_by(id=self.user_id).first()
            initial_balance = user.wallet_balance

        # Bundle with unavailable child 2
        bundle_id, _ = self._make_bundle(
            [
                (ProductType.KEY, 2),
                (ProductType.VOUCHER, 0),  # no stock
            ]
        )

        with self.assertRaises(self.inv.ReservationError):
            self.inv.reserve_bundle(self.user_id, bundle_id, 1)

        # Phase 1 raises → cart_confirm never reaches wallet debit
        # Verify wallet unchanged
        with get_db_session() as s:
            user = s.query(User).filter_by(id=self.user_id).first()
            self.assertAlmostEqual(
                user.wallet_balance,
                initial_balance,
                msg="wallet must not be debited when bundle reservation fails",
            )

    # ── test 4: fulfillment consumes the exact reserved child inventory ─

    def test_bundle_fulfillment_consumes_reserved_inventory(self):
        from database import (
            get_db_session, Order, OrderItem, ProductKey, ProductType,
        )

        bundle_id, child_ids = self._make_bundle(
            [(ProductType.KEY, 2)]
        )
        child_id = child_ids[0]

        # Key A: reserved; Key B: added AFTER the reservation
        key_A = self._first_key_value(child_id)

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, 1)

        # Add Key B *after* reservation — must NOT be delivered
        self._add_keys(child_id, 1, prefix="UNRESERVED-BUNDLE")
        key_B = self._second_key_value(child_id)
        self.assertNotEqual(key_A, key_B)

        order_id = self._make_order()
        self._attach_reservations([r.id for r in child_reservations], order_id)

        with get_db_session() as s:
            from database import Order as _Order, Product as _Product

            order = s.query(_Order).filter_by(id=order_id).first()
            bundle_product = s.query(_Product).filter_by(id=bundle_id).first()

            oi = OrderItem(
                order_id=order_id,
                product_id=bundle_id,
                quantity=1,
                price=9.99,
            )
            s.add(oi)
            s.flush()

            result = self.ds.deliver_bundle(s, order, oi, bundle_product)
            s.commit()

        self.assertTrue(result.success, f"bundle delivery failed: {result.error}")
        self.assertIn(key_A, result.user_message, "reserved key A must be in the delivery")
        self.assertNotIn(key_B, result.user_message, "unreserved key B must NOT be delivered")

        # B must remain unsold
        with get_db_session() as s:
            kb = s.query(ProductKey).filter_by(key_value=key_B).first()
            self.assertFalse(kb.is_sold, "B must remain unsold")
            self.assertIsNone(kb.order_id, "B must not be assigned to any order")

    # ── test 5: repeated fulfillment does not consume inventory twice ───

    def test_bundle_fulfillment_idempotent_no_double_consume(self):
        from database import (
            get_db_session, Order, OrderItem, ProductKey, ProductType,
        )

        bundle_id, child_ids = self._make_bundle([(ProductType.KEY, 3)])
        child_id = child_ids[0]

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, 1)
        order_id = self._make_order()
        self._attach_reservations([r.id for r in child_reservations], order_id)

        def _run_delivery(oi_id: int | None = None) -> "DeliveryResult":
            with get_db_session() as s:
                from database import Order as _Order, Product as _Product

                order = s.query(_Order).filter_by(id=order_id).first()
                bundle_product = s.query(_Product).filter_by(id=bundle_id).first()
                if oi_id:
                    oi = s.query(OrderItem).filter_by(id=oi_id).first()
                else:
                    oi = OrderItem(
                        order_id=order_id, product_id=bundle_id,
                        quantity=1, price=9.99,
                    )
                    s.add(oi)
                    s.flush()
                    oi_id = oi.id
                    s.commit()
                    # Re-fetch so it stays valid
                    oi = s.query(OrderItem).filter_by(id=oi_id).first()
                result = self.ds.deliver_bundle(s, order, oi, bundle_product)
                s.commit()
                return result, oi_id

        result1, oi_id = _run_delivery()
        self.assertTrue(result1.success, f"first delivery failed: {result1.error}")

        result2, _ = _run_delivery(oi_id)
        self.assertTrue(result2.idempotent_replay, "second delivery must be idempotent replay")

        # Only 1 key must be sold (the reserved one)
        sold_keys = self._get_key_values(child_id, sold=True)
        self.assertEqual(len(sold_keys), 1, "exactly one key must be sold, never twice")

    # ── test 6: reserve_bundle with quantity > 1 reserves correct count ─

    def test_bundle_quantity_multiplier_reserves_correct_count(self):
        from database import ProductType, get_db_session, ProductKey

        # 1 BundleItem with quantity=2 (2 child units per bundle unit)
        # Buying 3 bundles → need 6 child keys
        bundle_id = self._make_product(ProductType.BUNDLE, num_keys=0)
        child_id = self._make_product(ProductType.KEY, num_keys=8)

        from database import get_db_session, BundleItem

        with get_db_session() as s:
            s.add(BundleItem(parent_product_id=bundle_id, child_product_id=child_id, quantity=2))
            s.commit()

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, quantity=3)
        self.assertEqual(len(child_reservations), 1)
        self.assertEqual(child_reservations[0].quantity, 6, "need 2 × 3 = 6 child units")

        with get_db_session() as s:
            locked = s.query(ProductKey).filter_by(
                reservation_id=child_reservations[0].id, is_sold=False
            ).count()
            self.assertEqual(locked, 6)


# ─────────────────────────────────────────────────────────────────────
# Existing _find_active_reservation / _consume_keys integration
# ─────────────────────────────────────────────────────────────────────


class MultiItemSameOrderDeliveryTest(_BaseCase):
    """Two key-backed V11 items in the same order must each receive their OWN reserved keys."""

    def test_two_v11_items_same_order_each_get_own_reserved_key(self):
        """REDEEM_LINK A and REDEEM_LINK B in one order: dispatch with order_item_id delivers
        each item's own reserved key, never the other item's key."""
        from database import (
            get_db_session, Order, OrderItem, ProductType, ProductKey,
        )

        pid1 = self._make_product(ProductType.REDEEM_LINK, num_keys=1)
        pid2 = self._make_product(ProductType.REDEEM_LINK, num_keys=1)
        key1 = self._first_key_value(pid1)
        key2 = self._first_key_value(pid2)

        # Reserve both items
        res1 = self.inv.reserve(self.user_id, pid1, 1)
        res2 = self.inv.reserve(self.user_id, pid2, 1)

        order_id = self._make_order()
        self._attach_reservations([res1.id, res2.id], order_id)

        with get_db_session() as s:
            from database import Order as _Order

            order = s.query(_Order).filter_by(id=order_id).first()

            # Create both order items as the cart loop would
            oi1 = OrderItem(order_id=order_id, product_id=pid1, quantity=1, price=9.99)
            oi2 = OrderItem(order_id=order_id, product_id=pid2, quantity=1, price=9.99)
            s.add(oi1); s.add(oi2)
            s.flush()
            oi1_id, oi2_id = oi1.id, oi2.id

            # Deliver item 1 first
            result1 = self.ds.dispatch(order_id, session=s, order_item_id=oi1_id)
            # Deliver item 2 second — must NOT reuse item 1's sold keys
            result2 = self.ds.dispatch(order_id, session=s, order_item_id=oi2_id)
            s.commit()

        self.assertTrue(result1.success, f"item 1 delivery failed: {result1.error}")
        self.assertTrue(result2.success, f"item 2 delivery failed: {result2.error}")

        self.assertIn(key1, result1.assets, "item 1 must receive its own reserved key")
        self.assertNotIn(key2, result1.assets, "item 1 must NOT receive item 2's key")

        self.assertIn(key2, result2.assets, "item 2 must receive its own reserved key")
        self.assertNotIn(key1, result2.assets, "item 2 must NOT receive item 1's key")

        # Both keys must be sold, each to the correct order
        with get_db_session() as s:
            from database import ProductKey as PK

            k1 = s.query(PK).filter_by(key_value=key1).first()
            k2 = s.query(PK).filter_by(key_value=key2).first()
            self.assertTrue(k1.is_sold)
            self.assertTrue(k2.is_sold)
            self.assertEqual(k1.order_id, order_id)
            self.assertEqual(k2.order_id, order_id)


class BundleMultiChildDeliveryTest(_BaseCase):
    """Bundle with 2+ key-backed children must consume each child's OWN reservation."""

    def _make_bundle_multi_child(self):
        """Bundle → [KEY child1 (2 keys), REDEEM_LINK child2 (2 keys)]."""
        from database import get_db_session, BundleItem, ProductType

        bundle_id = self._make_product(ProductType.BUNDLE, num_keys=0)
        child1_id = self._make_product(ProductType.KEY, num_keys=2)
        child2_id = self._make_product(ProductType.REDEEM_LINK, num_keys=2)

        with get_db_session() as s:
            s.add(BundleItem(parent_product_id=bundle_id,
                             child_product_id=child1_id, quantity=1))
            s.add(BundleItem(parent_product_id=bundle_id,
                             child_product_id=child2_id, quantity=1))
            s.commit()

        return bundle_id, child1_id, child2_id

    def test_bundle_two_key_backed_children_each_consume_own_reservation(self):
        """Each child gets its own reserved key; the other child's key is untouched."""
        from database import (
            get_db_session, Order, OrderItem, ProductType, ProductKey,
        )

        bundle_id, child1_id, child2_id = self._make_bundle_multi_child()
        key_c1 = self._first_key_value(child1_id)
        key_c2 = self._first_key_value(child2_id)

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, 1)
        self.assertEqual(len(child_reservations), 2)

        order_id = self._make_order()
        self._attach_reservations([r.id for r in child_reservations], order_id)

        with get_db_session() as s:
            from database import Order as _Order, Product as _Product

            order = s.query(_Order).filter_by(id=order_id).first()
            bundle_product = s.query(_Product).filter_by(id=bundle_id).first()

            oi = OrderItem(order_id=order_id, product_id=bundle_id,
                           quantity=1, price=19.99)
            s.add(oi)
            s.flush()

            result = self.ds.deliver_bundle(s, order, oi, bundle_product)
            s.commit()

        self.assertTrue(result.success, f"bundle delivery failed: {result.error}")
        self.assertIn(key_c1, result.user_message,
                      "child 1 reserved key must be in delivery output")
        self.assertIn(key_c2, result.user_message,
                      "child 2 reserved key must be in delivery output")

        # Both child keys must be sold; the second keys of each child must stay unsold
        c1_sold = self._get_key_values(child1_id, sold=True)
        c2_sold = self._get_key_values(child2_id, sold=True)
        self.assertIn(key_c1, c1_sold)
        self.assertIn(key_c2, c2_sold)

        # The second (unreserved) key of each child must remain unsold
        c1_unsold = self._get_key_values(child1_id, sold=False)
        c2_unsold = self._get_key_values(child2_id, sold=False)
        self.assertEqual(len(c1_unsold), 1,
                         "exactly 1 of child1's 2 keys must remain unsold")
        self.assertEqual(len(c2_unsold), 1,
                         "exactly 1 of child2's 2 keys must remain unsold")

    def test_bundle_two_children_sold_counts_correct(self):
        """After delivery, each child has exactly 1 key sold (not 0, not 2)."""
        from database import get_db_session, Order, OrderItem, ProductType, ProductKey

        bundle_id, child1_id, child2_id = self._make_bundle_multi_child()

        child_reservations = self.inv.reserve_bundle(self.user_id, bundle_id, 1)
        order_id = self._make_order()
        self._attach_reservations([r.id for r in child_reservations], order_id)

        with get_db_session() as s:
            from database import Order as _Order, Product as _Product

            order = s.query(_Order).filter_by(id=order_id).first()
            product = s.query(_Product).filter_by(id=bundle_id).first()
            oi = OrderItem(order_id=order_id, product_id=bundle_id,
                           quantity=1, price=19.99)
            s.add(oi)
            s.flush()
            self.ds.deliver_bundle(s, order, oi, product)
            s.commit()

        c1_sold_count = len(self._get_key_values(child1_id, sold=True))
        c2_sold_count = len(self._get_key_values(child2_id, sold=True))
        self.assertEqual(c1_sold_count, 1,
                         "child1 must have exactly 1 sold key")
        self.assertEqual(c2_sold_count, 1,
                         "child2 must have exactly 1 sold key — not 0 (missing) or 2 (double)")


class FindActiveReservationTest(_BaseCase):
    """_find_active_reservation correctly locates the reservation by order+product."""

    def test_finds_reservation_after_order_id_attached(self):
        from database import ProductType, get_db_session, StockReservation

        product_id = self._make_product(ProductType.REDEEM_LINK, num_keys=2)
        res = self.inv.reserve(self.user_id, product_id, 1)
        order_id = self._make_order()
        self._attach_reservation(res.id, order_id)

        with get_db_session() as s:
            found = self.ds._find_active_reservation(s, order_id, product_id)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, res.id)

    def test_returns_none_when_no_reservation(self):
        from database import ProductType, get_db_session

        product_id = self._make_product(ProductType.KEY, num_keys=2)
        order_id = self._make_order()

        with get_db_session() as s:
            found = self.ds._find_active_reservation(s, order_id, product_id)
            self.assertIsNone(found)

    def test_consume_keys_uses_reservation_not_fresh_rows(self):
        """_consume_keys(reservation_id=N) delivers only the locked rows."""
        from database import ProductType, get_db_session, ProductKey

        product_id = self._make_product(ProductType.KEY, num_keys=1)
        key_A = self._first_key_value(product_id)

        res = self.inv.reserve(self.user_id, product_id, 1)
        order_id = self._make_order()
        self._attach_reservation(res.id, order_id)

        # Add a second key AFTER the reservation
        self._add_keys(product_id, 1, prefix="EXTRA")
        key_B = self._second_key_value(product_id)

        with get_db_session() as s:
            values = self.ds._consume_keys(s, product_id, 1, order_id, reservation_id=res.id)
            s.commit()

        self.assertEqual(values, [key_A])

        with get_db_session() as s:
            kb = s.query(ProductKey).filter_by(key_value=key_B).first()
            self.assertFalse(kb.is_sold)
            self.assertIsNone(kb.order_id)


if __name__ == "__main__":
    unittest.main()
