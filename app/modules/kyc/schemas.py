import uuid
from datetime import date, datetime
from typing import Literal, get_args

from pydantic import Field, model_validator

from app.common.enums import KycStatus
from app.common.schemas import RequestModel, ResponseModel
from app.modules.kyc.provider.base import DigiLockerStatus, Gender, SelfieStatus

# Why an admin rejected a KYC submission; the user sees the message, not the admin's note.
KycRejectionReason = Literal["name_mismatch", "photo_mismatch", "document_issue", "other"]
REJECTION_MESSAGES: dict[str, str] = {
    "name_mismatch": "The name on your documents doesn't match your account. Start KYC again.",
    "photo_mismatch": (
        "Your selfie didn't clearly match your Aadhaar photo. Start KYC again and take a "
        "clear selfie in good light."
    ),
    "document_issue": "There was a problem with your documents. Start KYC again.",
    "other": "Your KYC couldn't be approved. Start again, or contact support.",
}


def rejection_reason(value: str | None) -> KycRejectionReason:
    """A stored reason as a known code; anything else reads as "other"."""
    for reason in get_args(KycRejectionReason):
        if value == reason:
            return reason  # type: ignore[no-any-return]
    return "other"


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
    # Set while the status is "rejected".
    rejection_reason: KycRejectionReason | None = None
    rejection_message: str | None = None


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


# ---- Admin review


class KycReviewItem(ResponseModel):
    """One row of the review queue. No decrypted personal data here."""

    user_id: uuid.UUID
    full_name: str  # the account's name, not the Aadhaar name
    phone: str
    kyc_status: KycStatus
    submitted_at: datetime | None
    aadhaar: str | None  # XXXX XXXX 1234
    pan: str | None  # ABCXX1234F
    selfie_match_score: float | None


class KycReviewPage(ResponseModel):
    items: list[KycReviewItem]
    total: int
    limit: int
    offset: int


class KycAccount(ResponseModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str
    created_at: datetime


class KycSelfieSummary(ResponseModel):
    is_live: bool | None
    match_score: float | None
    min_match_score: int
    completed_at: datetime | None


class KycEventRead(ResponseModel):
    from_status: KycStatus
    to_status: KycStatus
    reason: str
    note: str | None
    actor_id: uuid.UUID | None
    created_at: datetime


class KycReviewDetail(ResponseModel):
    """Everything an admin needs to decide. Opening it is recorded in kyc_access_logs."""

    account: KycAccount
    kyc_status: KycStatus
    submitted_at: datetime | None
    details: KycDetails | None
    # Whether the account name matches the Aadhaar name (same rules as the PAN check).
    account_name_matches: bool | None
    pan_source: str | None
    selfie: KycSelfieSummary | None
    can_decide: bool
    events: list[KycEventRead]


class KycReject(RequestModel):
    reason: KycRejectionReason
    # Internal note for other admins; the user never sees it. Required for "other".
    note: str | None = Field(default=None, min_length=3, max_length=500)

    @model_validator(mode="after")
    def _other_needs_a_note(self) -> "KycReject":
        if self.reason == "other" and not (self.note and self.note.strip()):
            msg = 'Add a note when the reason is "other"'
            raise ValueError(msg)
        return self
