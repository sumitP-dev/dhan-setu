import hashlib
import hmac

from app.modules.payments.gateway.signatures import (
    hmac_sha256_hex,
    payment_signature,
    signature_matches,
)

SECRET = "test-key-secret"


def test_payment_signature_is_hmac_of_order_and_payment_ids() -> None:
    expected = hmac.new(SECRET.encode(), b"order_ABC|pay_XYZ", hashlib.sha256).hexdigest()

    assert payment_signature(SECRET, "order_ABC", "pay_XYZ") == expected


def test_signature_matches_only_the_exact_signature() -> None:
    good = hmac_sha256_hex(SECRET, b"body")

    assert signature_matches(good, good) is True
    assert signature_matches(good, hmac_sha256_hex("other-secret", b"body")) is False
    assert signature_matches(good, hmac_sha256_hex(SECRET, b"body2")) is False


def test_malformed_signatures_are_rejected_without_errors() -> None:
    good = hmac_sha256_hex(SECRET, b"body")

    for bad in [None, "", good.upper(), good[:-1], good + "0", "é" * 64, " " + good[1:]]:
        assert signature_matches(good, bad) is False
