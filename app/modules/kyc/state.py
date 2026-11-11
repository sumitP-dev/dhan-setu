"""KYC status rules.

    not_submitted -> in_progress -> pending_review -> verified
                          ^               |
                          +-- rejected <--+

A user starts (DigiLocker) and submits; an admin approves or rejects. After a rejection
the user can try again. "verified" is final.
"""

from app.common.enums import KycStatus
from app.common.exceptions import ConflictError

ALLOWED_TRANSITIONS: dict[KycStatus, frozenset[KycStatus]] = {
    KycStatus.NOT_SUBMITTED: frozenset({KycStatus.IN_PROGRESS}),
    KycStatus.IN_PROGRESS: frozenset({KycStatus.PENDING_REVIEW}),
    KycStatus.PENDING_REVIEW: frozenset({KycStatus.VERIFIED, KycStatus.REJECTED}),
    KycStatus.REJECTED: frozenset({KycStatus.IN_PROGRESS}),
    KycStatus.VERIFIED: frozenset(),
}

# Statuses where the user can start or continue KYC.
CAN_START: frozenset[KycStatus] = frozenset(
    {KycStatus.NOT_SUBMITTED, KycStatus.IN_PROGRESS, KycStatus.REJECTED}
)


class InvalidKycTransitionError(ConflictError):
    code = "invalid_kyc_transition"
    message = "Your KYC can't change to that status"


def can_transition(current: KycStatus, target: KycStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: KycStatus, target: KycStatus) -> None:
    if not can_transition(current, target):
        raise InvalidKycTransitionError
