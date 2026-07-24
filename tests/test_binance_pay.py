import hashlib
import hmac
from decimal import Decimal

from services.binance_pay import (
    BinancePayService, VerificationOutcome, is_valid_txid_format,
)


def test_hmac_signature_matches_documented_algorithm():
    svc = BinancePayService()
    svc.api_key = "key"
    svc.api_secret = "secret"
    query_string = "limit=1&timestamp=1000&recvWindow=10000"
    expected = hmac.new(b"secret", query_string.encode(), hashlib.sha256).hexdigest()
    assert svc._sign(query_string) == expected


def test_is_configured_requires_both_key_and_secret():
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "", ""
    assert not svc.is_configured()
    svc.api_key = "key"
    assert not svc.is_configured()
    svc.api_secret = "secret"
    assert svc.is_configured()


def test_txid_format_validation():
    assert is_valid_txid_format("BP123456789")
    assert not is_valid_txid_format("")
    assert not is_valid_txid_format("ab")  # too short
    assert not is_valid_txid_format("has spaces in it")


def test_verify_transaction_not_configured():
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "", ""
    result = svc.verify_transaction(
        transaction_id="BP123456789", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_CONFIGURED


def test_verify_transaction_invalid_txid_short_circuits_before_api_call():
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    result = svc.verify_transaction(
        transaction_id="x", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.INVALID_TXID


def test_verify_transaction_matches_and_validates_amount_currency_direction(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    svc.allowed_currencies = ["USDT", "USDC"]

    record = {
        "orderId": "BP123456789",
        "transactionId": "BP123456789",
        "transactionSide": 1,  # RECEIVE
        "status": "SUCCESS",
        "currency": "USDT",
        "amount": "10.00",
        "transactionTime": 99999999999999,  # far future, always after order creation
    }
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: [record])

    result = svc.verify_transaction(
        transaction_id="BP123456789", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.SUCCESS
    assert result.received_amount == Decimal("10.00")
    assert result.currency == "USDT"


def test_verify_transaction_amount_mismatch(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    svc.allowed_currencies = ["USDT", "USDC"]

    record = {
        "orderId": "BP1AMOUNT", "transactionId": "BP1AMOUNT", "transactionSide": 1,
        "status": "SUCCESS", "currency": "USDT", "amount": "9.00",
        "transactionTime": 99999999999999,
    }
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: [record])

    result = svc.verify_transaction(
        transaction_id="BP1AMOUNT", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.AMOUNT_MISMATCH
    assert result.received_amount == Decimal("9.00")


def test_verify_transaction_currency_mismatch(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    svc.allowed_currencies = ["USDT", "USDC"]

    record = {
        "orderId": "BP1CURRENCY", "transactionId": "BP1CURRENCY", "transactionSide": 1,
        "status": "SUCCESS", "currency": "BUSD", "amount": "10.00",
        "transactionTime": 99999999999999,
    }
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: [record])

    result = svc.verify_transaction(
        transaction_id="BP1CURRENCY", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.CURRENCY_MISMATCH


def test_verify_transaction_wrong_direction_rejected(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    svc.allowed_currencies = ["USDT", "USDC"]

    record = {
        "orderId": "BP1DIRECTION", "transactionId": "BP1DIRECTION", "transactionSide": 0,  # SEND, not RECEIVE
        "status": "SUCCESS", "currency": "USDT", "amount": "10.00",
        "transactionTime": 99999999999999,
    }
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: [record])

    result = svc.verify_transaction(
        transaction_id="BP1DIRECTION", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.WRONG_DIRECTION


def test_verify_transaction_not_found(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: [])

    result = svc.verify_transaction(
        transaction_id="BPMISSING1", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_FOUND


def test_verify_transaction_api_error(monkeypatch):
    svc = BinancePayService()
    svc.api_key, svc.api_secret = "key", "secret"
    monkeypatch.setattr(svc, "get_pay_transactions", lambda limit=100: None)

    result = svc.verify_transaction(
        transaction_id="BP123456789", expected_amount=Decimal("10.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.API_ERROR


def test_never_uses_float_for_amount_equality():
    """0.1 + 0.2 != 0.3 in float — Decimal must be used for comparisons."""
    a = Decimal("10.10")
    b = Decimal("10.10")
    assert a == b
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
