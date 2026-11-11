import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.common.enums import KycStatus
from app.common.schemas import RequestModel, ResponseModel
from app.modules.kyc.provider.base import DigiLockerStatus, Gender, SelfieStatus

# What the app should show next.
KycNextStep = Literal["digilocker", "pan", "selfie", "submit"]


class KycDetails(ResponseModel):
    """What DigiLocker returned, for the user to check. Aadhaar and PAN are masked."""

    name: str
    date_of_birth: date | None
    birth_year: int
    gender: Gender
    address: str | None
    aadhaar: str  # XXXX XXXX 1234
    pan: str | None  # ABCXX1234F
    pan_verified: bool
    pan_issue: str | None
    pan_issue_message: str | None
    # A live selfie matched the Aadhaar photo.
    selfie_verified: bool
    fetched_at: datetime


class KycRead(ResponseModel):
    status: KycStatus
    # True when the user can start or continue KYC (not while it's under review).
    can_start: bool
    next_step: KycNextStep | None
    details: KycDetails | None


class DigiLockerSessionRead(ResponseModel):
    id: uuid.UUID
    status: DigiLockerStatus
    # Open this in a browser; the user logs in to DigiLocker and allows sharing.
    consent_url: str
    expires_at: datetime
    failure_reason: str | None
    failure_message: str | None


class PanSubmit(RequestModel):
    """For users whose PAN isn't in DigiLocker."""

    pan: str = Field(min_length=10, max_length=12, examples=["ABCPV1234F"])


class SelfieSessionRead(ResponseModel):
    id: uuid.UUID
    status: SelfieStatus
    failure_reason: str | None
    failure_message: str | None
    expires_at: datetime


class SelfieSessionStart(SelfieSessionRead):
    # Starts the provider's selfie SDK in the app. Short-lived; only returned here.
    sdk_token: str
