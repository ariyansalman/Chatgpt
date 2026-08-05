import base64, hashlib
from services.heleket_payment import HeleketPaymentService, SUPPORTED_ASSETS

def test_heleket_sign_matches_documented_algorithm():
    svc=HeleketPaymentService(); svc.api_key="secret"
    payload={"currency":"USDT","network":"tron","order_id":"1","url_callback":"https://example.com/callback"}
    raw=svc._json_bytes(payload)
    assert svc._sign(payload)==hashlib.md5(base64.b64encode(raw)+b"secret").hexdigest()

def test_webhook_signature_rejects_bad_signature():
    svc=HeleketPaymentService(); svc.api_key="secret"
    assert not svc.verify_webhook_signature({"uuid":"x","sign":"bad"},"bad")

def test_supported_pairs_are_distinct():
    assert SUPPORTED_ASSETS["usdt_tron"][:2] == ("USDT","tron")
    assert SUPPORTED_ASSETS["usdt_bsc"][:2] == ("USDT","bsc")
