"""Payment status rules.

    created -> pending -> success -> refunded
       |          |
       +--------> failed -> pending   (a retry on the same order is being processed)

Every success goes through pending. "failed" isn't final, because a user can retry in
checkout on the same order; if the gateway later confirms money was taken, it must be
recorded. "success" can only become "refunded".
"""

from app.common.enums import PaymentStatus
from app.common.exceptions import ConflictError

ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({PaymentStatus.PENDING, PaymentStatus.FAILED}),
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
    PaymentStatus.FAILED: frozenset({PaymentStatus.PENDING}),
    PaymentStatus.SUCCESS: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
}


class InvalidTransitionError(ConflictError):
    code = "invalid_payment_transition"
    message = "This payment can't change to that status"


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError
