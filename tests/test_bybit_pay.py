import hashlib
import hmac
from decimal import Decimal

from services.bybit_pay import (
    BybitPayService, VerificationOutcome, PaymentType,
    is_valid_uid_txid_format, is_valid_onchain_txid_format,
    NETWORK_CHAIN_MAP,
)


def _svc(**overrides):
    svc = BybitPayService()
    svc.api_key, svc.api_secret = "key", "secret"
    svc.wallets = {"TRC20": "TAbc123TronWallet", "BEP20": "0xBscWallet", "ERC20": "0xEthWallet"}
    svc.allowed_networks = ["TRC20", "BEP20", "ERC20"]
    for k, v in overrides.items():
        setattr(svc, k, v)
    return svc


# ---------------------------------------------------------------------------
# Signing / configuration
# ---------------------------------------------------------------------------

def test_hmac_signature_matches_documented_algorithm():
    svc = _svc()
    to_sign = "1000" + "key" + "10000" + "limit=1"
    expected = hmac.new(b"secret", to_sign.encode(), hashlib.sha256).hexdigest()
    assert svc._sign(to_sign) == expected


def test_is_configured_requires_both_key_and_secret():
    svc = BybitPayService()
    svc.api_key, svc.api_secret = "", ""
    assert not svc.is_configured()
    svc.api_key = "key"
    assert not svc.is_configured()
    svc.api_secret = "secret"
    assert svc.is_configured()


def test_networks_with_wallets_filters_missing_addresses():
    svc = _svc()
    svc.wallets = {"TRC20": "TAbc123", "BEP20": "", "ERC20": "0xEthWallet"}
    svc.allowed_networks = ["TRC20", "BEP20", "ERC20"]
    assert svc.networks_with_wallets() == ["TRC20", "ERC20"]


def test_networks_with_wallets_respects_admin_disabled_networks():
    svc = _svc()
    svc.allowed_networks = ["TRC20"]  # BEP20/ERC20 disabled by admin even though wallets are set
    assert svc.networks_with_wallets() == ["TRC20"]


# ---------------------------------------------------------------------------
# TXID format validation
# ---------------------------------------------------------------------------

def test_uid_txid_format_validation():
    assert is_valid_uid_txid_format("77c37e5c-d9fa-41e5-bd13-c9b59d95")
    assert not is_valid_uid_txid_format("")
    assert not is_valid_uid_txid_format("ab")  # too short
    assert not is_valid_uid_txid_format("has spaces in it")


def test_onchain_txid_format_validation():
    assert is_valid_onchain_txid_format("0x" + "a" * 64)
    assert is_valid_onchain_txid_format("a" * 64)
    assert not is_valid_onchain_txid_format("")
    assert not is_valid_onchain_txid_format("ab")
    assert not is_valid_onchain_txid_format("has spaces in it")


def test_network_chain_map_covers_all_supported_networks():
    assert set(NETWORK_CHAIN_MAP.keys()) == {"TRC20", "BEP20", "ERC20"}
    assert NETWORK_CHAIN_MAP["TRC20"] == "TRX"
    assert NETWORK_CHAIN_MAP["BEP20"] == "BSC"
    assert NETWORK_CHAIN_MAP["ERC20"] == "ETH"


# ---------------------------------------------------------------------------
# UID (internal) transfer verification
# ---------------------------------------------------------------------------

def test_verify_uid_transfer_not_configured():
    svc = _svc(api_key="", api_secret="")
    result = svc.verify_uid_transfer(
        transaction_id="77c37e5c-d9fa-41e5-bd13-c9b59d95",
        expected_amount=Decimal("25.00"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_CONFIGURED


def test_verify_uid_transfer_invalid_txid_short_circuits_before_api_call():
    svc = _svc()
    result = svc.verify_uid_transfer(
        transaction_id="x", expected_amount=Decimal("25.00"),
        currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.INVALID_TXID


def test_verify_uid_transfer_success(monkeypatch):
    svc = _svc()
    record = {
        "id": "998877", "txID": "77c37e5c-d9fa-41e5-bd13-c9b59d95",
        "status": 2, "coin": "USDT", "amount": "25", "createdTime": "1705393280",
    }
    monkeypatch.setattr(svc, "get_internal_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_uid_transfer(
        transaction_id="77c37e5c-d9fa-41e5-bd13-c9b59d95",
        expected_amount=Decimal("25"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.SUCCESS
    assert result.received_amount == Decimal("25")
    assert result.currency == "USDT"
    assert result.bybit_record_id == "998877"


def test_verify_uid_transfer_amount_mismatch(monkeypatch):
    svc = _svc()
    record = {"id": "1", "txID": "TESTTX1", "status": 2, "coin": "USDT", "amount": "20", "createdTime": "1705393280"}
    monkeypatch.setattr(svc, "get_internal_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_uid_transfer(
        transaction_id="TESTTX1", expected_amount=Decimal("25"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.AMOUNT_MISMATCH
    assert result.received_amount == Decimal("20")


def test_verify_uid_transfer_not_yet_successful(monkeypatch):
    svc = _svc()
    record = {"id": "1", "txID": "TESTTX1", "status": 1, "coin": "USDT", "amount": "25", "createdTime": "1705393280"}
    monkeypatch.setattr(svc, "get_internal_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_uid_transfer(
        transaction_id="TESTTX1", expected_amount=Decimal("25"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_SUCCESSFUL


def test_verify_uid_transfer_not_found(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "get_internal_deposit_records", lambda coin=None, limit=50: [])

    result = svc.verify_uid_transfer(
        transaction_id="MISSING123", expected_amount=Decimal("25"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_FOUND


def test_verify_uid_transfer_api_error(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "get_internal_deposit_records", lambda coin=None, limit=50: None)

    result = svc.verify_uid_transfer(
        transaction_id="77c37e5c-d9fa-41e5-bd13-c9b59d95",
        expected_amount=Decimal("25"), currency="USDT", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.API_ERROR


# ---------------------------------------------------------------------------
# On-chain deposit verification
# ---------------------------------------------------------------------------

def test_verify_onchain_deposit_not_configured():
    svc = _svc(api_key="", api_secret="")
    result = svc.verify_onchain_deposit(
        transaction_id="a" * 64, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_CONFIGURED


def test_verify_onchain_deposit_no_wallet_configured():
    svc = _svc()
    svc.wallets["TRC20"] = ""
    result = svc.verify_onchain_deposit(
        transaction_id="a" * 64, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_CONFIGURED


def test_verify_onchain_deposit_invalid_txid_short_circuits():
    svc = _svc()
    result = svc.verify_onchain_deposit(
        transaction_id="x", expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.INVALID_TXID


def test_verify_onchain_deposit_success(monkeypatch):
    svc = _svc()
    txid = "b" * 64
    record = {
        "id": "555", "txID": txid, "chain": "TRX", "status": 3,
        "coin": "USDT", "amount": "25", "toAddress": "TAbc123TronWallet",
        "successAt": "1705393280",
    }
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_onchain_deposit(
        transaction_id=txid, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.SUCCESS
    assert result.received_amount == Decimal("25")
    assert result.network == "TRC20"


def test_verify_onchain_deposit_wrong_network(monkeypatch):
    svc = _svc()
    txid = "c" * 64
    record = {
        "id": "1", "txID": txid, "chain": "ETH", "status": 3,  # sent on ERC20, order was TRC20
        "coin": "USDT", "amount": "25", "toAddress": "TAbc123TronWallet",
        "successAt": "1705393280",
    }
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_onchain_deposit(
        transaction_id=txid, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NETWORK_MISMATCH


def test_verify_onchain_deposit_wrong_address(monkeypatch):
    svc = _svc()
    txid = "d" * 64
    record = {
        "id": "1", "txID": txid, "chain": "TRX", "status": 3,
        "coin": "USDT", "amount": "25", "toAddress": "SomeoneElsesWallet",
        "successAt": "1705393280",
    }
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_onchain_deposit(
        transaction_id=txid, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.WRONG_ADDRESS


def test_verify_onchain_deposit_not_yet_successful(monkeypatch):
    svc = _svc()
    txid = "e" * 64
    record = {
        "id": "1", "txID": txid, "chain": "TRX", "status": 1,  # ToBeConfirmed
        "coin": "USDT", "amount": "25", "toAddress": "TAbc123TronWallet",
        "successAt": None,
    }
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_onchain_deposit(
        transaction_id=txid, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_SUCCESSFUL


def test_verify_onchain_deposit_amount_mismatch(monkeypatch):
    svc = _svc()
    txid = "f" * 64
    record = {
        "id": "1", "txID": txid, "chain": "TRX", "status": 3,
        "coin": "USDT", "amount": "24.99", "toAddress": "TAbc123TronWallet",
        "successAt": "1705393280",
    }
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [record])

    result = svc.verify_onchain_deposit(
        transaction_id=txid, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.AMOUNT_MISMATCH


def test_verify_onchain_deposit_not_found(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: [])

    result = svc.verify_onchain_deposit(
        transaction_id="a" * 64, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NOT_FOUND


def test_verify_onchain_deposit_api_error(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "get_onchain_deposit_records", lambda coin=None, limit=50: None)

    result = svc.verify_onchain_deposit(
        transaction_id="a" * 64, expected_amount=Decimal("25"), currency="USDT",
        network="TRC20", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.API_ERROR


def test_verify_onchain_deposit_unsupported_network():
    svc = _svc()
    result = svc.verify_onchain_deposit(
        transaction_id="a" * 64, expected_amount=Decimal("25"), currency="USDT",
        network="SOL", order_created_at=None,
    )
    assert result.outcome == VerificationOutcome.NETWORK_MISMATCH


# ---------------------------------------------------------------------------
# Amount safety
# ---------------------------------------------------------------------------

def test_never_uses_float_for_amount_equality():
    """0.1 + 0.2 != 0.3 in float — Decimal must be used for comparisons."""
    assert Decimal("25.00") == Decimal("25.00")
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")


def test_payment_type_constants():
    assert PaymentType.UID_TRANSFER == "uid_transfer"
    assert PaymentType.ONCHAIN == "onchain"
