"""Wallet service: credit / debit / adjust / ledger round-trip on SQLite in-memory."""
import os
import unittest


def _setup_inmemory():
    os.environ.setdefault("BOT_TOKEN", "test:test")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"


class WalletServiceTest(unittest.TestCase):
    # NOTE: this used to be a `setUpClass` fixture shared across every test
    # method in the class. Because the tests assert *absolute* wallet
    # balances (not deltas), sharing one in-memory DB/user across the whole
    # class made results depend on unittest's (alphabetical) method
    # execution order — e.g. `test_credit_and_debit` would see whatever
    # balance earlier-running tests had already left behind, rather than a
    # clean $0 starting point. Switched to `setUp` so every test method
    # gets its own fresh in-memory database and a brand-new user.
    def setUp(self):
        _setup_inmemory()
        # Fresh module state
        import importlib
        from database import db as db_mod
        importlib.reload(db_mod)
        from database.models import Base
        Base.metadata.create_all(db_mod.engine)
        from database import get_db_session, User
        with get_db_session() as s:
            s.add(User(telegram_id=42, username="u", wallet_balance=0.0))
            s.commit()
            uid = s.query(User).filter_by(telegram_id=42).first().id
        self.uid = uid

    def test_credit_and_debit(self):
        from services import wallet
        new_bal = wallet.credit(self.uid, 25.0, reason="test topup")
        self.assertAlmostEqual(new_bal, 25.0)
        new_bal = wallet.debit(self.uid, 10.0, reason="test purchase")
        self.assertAlmostEqual(new_bal, 15.0)
        entries = wallet.ledger(self.uid, limit=10)
        self.assertEqual(len(entries), 2)
        self.assertGreater(entries[0]["created_at"].timestamp(), 0)

    def test_debit_insufficient(self):
        from services import wallet
        with self.assertRaises(wallet.WalletError):
            wallet.debit(self.uid, 10_000.0, reason="overdraw")

    def test_adjust_positive_and_negative(self):
        """Admin `adjust()` can move the balance either direction in one call."""
        from services import wallet
        start = wallet.credit(self.uid, 40.0, reason="seed for adjust test")
        up = wallet.adjust(self.uid, 10.0, reason="bonus", actor_id=999)
        self.assertAlmostEqual(up, start + 10.0)
        down = wallet.adjust(self.uid, -25.0, reason="correction", actor_id=999)
        self.assertAlmostEqual(down, up - 25.0)

        entries = wallet.ledger(self.uid, limit=10)
        # Newest first — the last two ops should be the -25 then +10 adjustments.
        self.assertAlmostEqual(entries[0]["delta"], -25.0)
        self.assertEqual(entries[0]["actor_type"], "admin")
        self.assertEqual(entries[0]["actor_id"], 999)
        self.assertAlmostEqual(entries[1]["delta"], 10.0)

    def test_credit_rejects_zero_or_negative_amount(self):
        from services import wallet
        with self.assertRaises(wallet.WalletError):
            wallet.credit(self.uid, 0, reason="zero credit")
        with self.assertRaises(wallet.WalletError):
            wallet.credit(self.uid, -5.0, reason="negative credit")

    def test_debit_rejects_zero_or_negative_amount(self):
        from services import wallet
        with self.assertRaises(wallet.WalletError):
            wallet.debit(self.uid, 0, reason="zero debit")
        with self.assertRaises(wallet.WalletError):
            wallet.debit(self.uid, -5.0, reason="negative debit")

    def test_adjust_rejects_zero_delta(self):
        from services import wallet
        with self.assertRaises(wallet.WalletError):
            wallet.adjust(self.uid, 0, reason="no-op adjust")

    def test_unknown_user_raises(self):
        from services import wallet
        with self.assertRaises(wallet.WalletError):
            wallet.credit(999_999, 10.0, reason="ghost user")

    def test_balance_never_goes_negative(self):
        """Debiting exactly the full balance is fine; one cent more must fail
        and must NOT partially apply (balance stays unchanged on failure)."""
        from services import wallet
        from database import get_db_session, User
        wallet.credit(self.uid, 5.0, reason="exact balance test")
        with get_db_session() as s:
            before = s.query(User).filter_by(id=self.uid).first().wallet_balance
        with self.assertRaises(wallet.WalletError):
            wallet.debit(self.uid, before + 0.01, reason="one cent over")
        with get_db_session() as s:
            after = s.query(User).filter_by(id=self.uid).first().wallet_balance
        self.assertAlmostEqual(before, after)

    def test_ledger_reason_and_ref_fields_round_trip(self):
        from services import wallet
        wallet.credit(self.uid, 12.5, reason="order refund", actor_type="admin",
                     actor_id=7, ref_type="order", ref_id="12345")
        entries = wallet.ledger(self.uid, limit=1)
        self.assertEqual(entries[0]["reason"], "order refund")
        self.assertEqual(entries[0]["actor_type"], "admin")
        self.assertEqual(entries[0]["actor_id"], 7)


if __name__ == "__main__":
    unittest.main()
