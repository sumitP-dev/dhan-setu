import base64
import secrets

import pytest
from pydantic import SecretStr, ValidationError

from app.core.encryption import DecryptionError, FieldCipher, decode_master_key
from tests.helpers import make_settings


def new_cipher() -> FieldCipher:
    return FieldCipher(secrets.token_bytes(32))


def test_round_trip() -> None:
    cipher = new_cipher()
    token = cipher.encrypt("Asha Verma", context="kyc:u1:name")

    assert token.startswith("v1:")
    assert "Asha" not in token
    assert cipher.decrypt(token, context="kyc:u1:name") == "Asha Verma"


def test_same_value_encrypts_differently_each_time() -> None:
    cipher = new_cipher()
    assert cipher.encrypt("x", context="c") != cipher.encrypt("x", context="c")


@pytest.mark.parametrize("context", ["kyc:u2:name", "kyc:u1:pan", ""])
def test_value_moved_to_another_row_or_field_fails(context: str) -> None:
    cipher = new_cipher()
    token = cipher.encrypt("ABCPV1234F", context="kyc:u1:name")

    with pytest.raises(DecryptionError):
        cipher.decrypt(token, context=context)


def test_tampering_is_detected() -> None:
    cipher = new_cipher()
    token = cipher.encrypt("Asha Verma", context="c")
    raw = bytearray(base64.urlsafe_b64decode(token[3:]))
    raw[-1] ^= 1
    tampered = "v1:" + base64.urlsafe_b64encode(bytes(raw)).decode()

    with pytest.raises(DecryptionError):
        cipher.decrypt(tampered, context="c")


@pytest.mark.parametrize("token", ["", "v1:", "v2:abcd", "plain text", "v1:!!!notbase64"])
def test_garbage_is_refused(token: str) -> None:
    with pytest.raises(DecryptionError):
        new_cipher().decrypt(token, context="c")


def test_another_key_cannot_decrypt() -> None:
    token = new_cipher().encrypt("secret", context="c")
    with pytest.raises(DecryptionError):
        new_cipher().decrypt(token, context="c")


def test_lookup_hash_is_stable_keyed_and_per_purpose() -> None:
    key = secrets.token_bytes(32)
    cipher, same_key, other_key = FieldCipher(key), FieldCipher(key), new_cipher()

    value = cipher.lookup_hash("ABCPV1234F", purpose="pan")
    assert value == same_key.lookup_hash("ABCPV1234F", purpose="pan")
    assert value != other_key.lookup_hash("ABCPV1234F", purpose="pan")
    assert value != cipher.lookup_hash("ABCPV1234F", purpose="other")
    assert "ABCPV1234F" not in value


def test_keys_never_show_in_repr() -> None:
    assert "hidden" in repr(new_cipher())


@pytest.mark.parametrize(
    "value",
    ["", "short", base64.urlsafe_b64encode(b"x" * 16).decode(), "not base64 at all!!"],
)
def test_master_key_must_be_32_bytes_of_base64(value: str) -> None:
    with pytest.raises(ValueError, match="must"):
        decode_master_key(value)


def test_settings_require_a_valid_key_unless_kyc_is_off() -> None:
    with pytest.raises(ValidationError, match="KYC_ENCRYPTION_KEY is required"):
        make_settings(kyc_encryption_key=None)
    with pytest.raises(ValidationError, match="KYC_ENCRYPTION_KEY must"):
        make_settings(kyc_encryption_key=SecretStr("too-short"))
    assert make_settings(kyc_provider="none", kyc_encryption_key=None).kyc_encryption_key is None


def test_key_is_masked_in_settings() -> None:
    settings = make_settings()
    assert settings.kyc_encryption_key is not None
    assert settings.kyc_encryption_key.get_secret_value() not in repr(settings)


def test_production_needs_https_public_url() -> None:
    production = {
        "environment": "production",
        "payment_gateway": "razorpay",
        "razorpay_key_id": "rzp_live_abc123",
        "razorpay_key_secret": "live-key-secret",
        "razorpay_webhook_secret": "live-webhook-secret",
        "kyc_provider": "none",
    }
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL must be https"):
        make_settings(**production, public_base_url="http://api.dhansetu.example")
