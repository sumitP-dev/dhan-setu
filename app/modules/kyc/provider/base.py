"""The KYC provider interface. A provider (an aggregator with DigiLocker access and face
matching) fetches the user's Aadhaar and PAN from DigiLocker with their consent, and
compares a live selfie with the Aadhaar photo.

What never crosses this boundary: the full Aadhaar number, the Aadhaar XML and any
photo. The Aadhaar Act restricts storing them, so a provider client keeps only the last
4 digits and the provider's own reference ids.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from fastapi import status

from app.common.exceptions import AppError, InvalidInputError


class KycProviderError(AppError):
    """The provider refused the request or answered with something unexpected."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "kyc_provider_error"
    message = "Identity verification could not be completed. Try again."


class KycProviderUnavailableError(KycProviderError):
    """Timeout, network failure or a 5xx from the provider. The outcome is unknown."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "kyc_provider_unavailable"
    message = "Identity verification is not available right now. Try again later."


class KycSessionNotFoundError(KycProviderError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "kyc_session_not_found"
    message = "Verification session not found"


class InvalidKycReferenceError(InvalidInputError):
    code = "invalid_kyc_reference"
    message = "Invalid verification reference"


# Provider ids go into URL paths; checking the shape stops input like "../x" from
# reaching a different endpoint.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
MAX_REFERENCE_LENGTH = 40
PAN_LENGTH = 10
_PAN_HOLDER_TYPE_INDEX = 3

# 5 letters, 4 digits, 1 letter. The 4th letter is the holder type; P = a person.
_PAN_RE = re.compile(r"[A-Z]{3}[ABCFGHJLPT][A-Z][0-9]{4}[A-Z]")
_AADHAAR_LAST4_RE = re.compile(r"[0-9]{4}")


def require_session_id(value: str) -> str:
    if not _SESSION_ID_RE.fullmatch(value):
        raise InvalidKycReferenceError
    return value


def require_reference(value: str) -> str:
    """Our id for the attempt, sent to the provider so retries reuse one session."""
    if not value or len(value) > MAX_REFERENCE_LENGTH or not _SESSION_ID_RE.fullmatch(value):
        msg = f"reference must be 8-{MAX_REFERENCE_LENGTH} letters, digits, _ or -"
        raise ValueError(msg)
    return value


def normalize_pan(value: str) -> str | None:
    """Upper-cased PAN, or None if it isn't a valid PAN."""
    pan = value.strip().upper()
    return pan if _PAN_RE.fullmatch(pan) else None


def is_individual_pan(pan: str) -> bool:
    return len(pan) == PAN_LENGTH and pan[_PAN_HOLDER_TYPE_INDEX] == "P"


def mask_pan(pan: str) -> str:
    """ABCPE1234F -> ABCXX1234F"""
    return f"{pan[:3]}XX{pan[5:]}"


def mask_aadhaar(last4: str) -> str:
    return f"XXXX XXXX {last4}"


class DigiLockerStatus(StrEnum):
    PENDING = "pending"  # user hasn't finished DigiLocker yet
    COMPLETED = "completed"
    DENIED = "denied"  # user refused to share
    EXPIRED = "expired"
    FAILED = "failed"


class SelfieStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class Gender(StrEnum):
    MALE = "M"
    FEMALE = "F"
    TRANSGENDER = "T"


@dataclass(frozen=True, slots=True)
class AadhaarDetails:
    last4: str
    name: str
    date_of_birth: date | None  # some Aadhaar records hold only the year
    birth_year: int
    gender: Gender
    address: str | None
    # The provider checked UIDAI's digital signature on the Aadhaar XML.
    signature_verified: bool

    def __post_init__(self) -> None:
        if not _AADHAAR_LAST4_RE.fullmatch(self.last4):
            msg = "last4 must be exactly 4 digits"
            raise ValueError(msg)
        if self.date_of_birth is not None and self.date_of_birth.year != self.birth_year:
            msg = "birth_year must match date_of_birth"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PanDetails:
    number: str
    name: str
    date_of_birth: date | None

    def __post_init__(self) -> None:
        if normalize_pan(self.number) != self.number:
            msg = "number must be a valid upper-case PAN"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DigiLockerSession:
    id: str
    # Where the app sends the user to log in to DigiLocker and give consent.
    consent_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DigiLockerResult:
    session_id: str
    status: DigiLockerStatus
    aadhaar: AadhaarDetails | None = None
    # None when the user has no PAN in DigiLocker; verify_pan() covers that case.
    pan: PanDetails | None = None
    failure_reason: str | None = None


class PanStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"  # not issued, deleted or deactivated


@dataclass(frozen=True, slots=True)
class PanVerification:
    pan: str
    status: PanStatus
    name_matches: bool
    date_of_birth_matches: bool | None  # None when the provider can't tell
    aadhaar_linked: bool | None


@dataclass(frozen=True, slots=True)
class SelfieSession:
    id: str
    # Short-lived token for the provider's selfie SDK in the app.
    sdk_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SelfieResult:
    session_id: str
    status: SelfieStatus
    is_live: bool | None = None  # a real person in front of the camera, not a photo
    match_score: float | None = None  # 0-100, similarity to the Aadhaar photo
    failure_reason: str | None = None


class KycProvider(Protocol):
    async def create_digilocker_session(
        self, *, reference: str, redirect_url: str
    ) -> DigiLockerSession: ...

    async def fetch_digilocker_result(self, session_id: str) -> DigiLockerResult: ...

    async def verify_pan(
        self, *, pan: str, name: str, date_of_birth: date | None
    ) -> PanVerification:
        """For users whose PAN isn't in DigiLocker."""
        ...

    async def create_selfie_session(
        self, *, digilocker_session_id: str, reference: str
    ) -> SelfieSession:
        """The selfie is compared with the Aadhaar photo from that DigiLocker session."""
        ...

    async def fetch_selfie_result(self, session_id: str) -> SelfieResult: ...

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool: ...

    async def aclose(self) -> None: ...
