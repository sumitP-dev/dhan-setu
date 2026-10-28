import pytest
from pydantic import TypeAdapter, ValidationError

from app.common.validators import IndianMobileNumber, NewPassword, NormalizedEmail

mobile = TypeAdapter(IndianMobileNumber)
password = TypeAdapter(NewPassword)
email = TypeAdapter(NormalizedEmail)


@pytest.mark.parametrize(
    "raw",
    [
        "9876543210",
        "+919876543210",
        "+91 98765 43210",
        "919876543210",
        "09876543210",
        "98765-43210",
    ],
)
def test_mobile_numbers_normalize_to_e164(raw: str) -> None:
    assert mobile.validate_python(raw) == "+919876543210"


@pytest.mark.parametrize(
    "raw", ["5876543210", "987654321", "98765432100", "+19876543210", "abcdefghij", ""]
)
def test_invalid_mobile_numbers(raw: str) -> None:
    with pytest.raises(ValidationError, match="valid 10-digit Indian mobile number"):
        mobile.validate_python(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("Ab1", "at least 8 characters"),
        ("abcdefgh", "one letter and one number"),
        ("12345678", "one letter and one number"),
        ("a1" + "é" * 36, "at most 72 bytes"),  # 74 bytes, only 38 characters
    ],
)
def test_weak_passwords(raw: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        password.validate_python(raw)


def test_valid_password() -> None:
    assert password.validate_python("Secret123") == "Secret123"


def test_email_is_lower_cased() -> None:
    assert email.validate_python("Asha.Verma@Example.COM") == "asha.verma@example.com"
