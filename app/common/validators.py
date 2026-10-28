"""Reusable input types. Errors use PydanticCustomError so messages reach the client
without Pydantic's "Value error, " prefix."""

import re
from typing import Annotated

from pydantic import AfterValidator, EmailStr, StringConstraints
from pydantic_core import PydanticCustomError

BCRYPT_MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8

_MOBILE_SEPARATORS = re.compile(r"[\s\-()]")
_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")


def normalize_indian_mobile(value: str) -> str:
    """Accepts 9876543210, +91 98765 43210, 919876543210 or 09876543210.
    Returns E.164: +919876543210."""
    digits = _MOBILE_SEPARATORS.sub("", value)
    if digits.startswith("+91"):
        digits = digits[3:]
    elif digits.startswith("91") and len(digits) == 12:  # noqa: PLR2004
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:  # noqa: PLR2004
        digits = digits[1:]
    if not _INDIAN_MOBILE.fullmatch(digits):
        raise PydanticCustomError("mobile_invalid", "Enter a valid 10-digit Indian mobile number")
    return f"+91{digits}"


def validate_new_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise PydanticCustomError(
            "password_too_short", f"Must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    # bcrypt only uses the first 72 bytes; reject rather than silently truncate.
    if len(value.encode()) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PydanticCustomError("password_too_long", "Must be at most 72 bytes")
    if not (any(ch.isalpha() for ch in value) and any(ch.isdigit() for ch in value)):
        raise PydanticCustomError(
            "password_too_weak", "Must contain at least one letter and one number"
        )
    return value


# Lower-cased so "A@x.com" and "a@x.com" are the same account.
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
IndianMobileNumber = Annotated[str, AfterValidator(normalize_indian_mobile)]
NewPassword = Annotated[str, AfterValidator(validate_new_password)]
# For checking an existing password: no policy, just a sane size limit.
ExistingPassword = Annotated[str, StringConstraints(min_length=1, max_length=128)]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
