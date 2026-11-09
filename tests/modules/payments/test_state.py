import pytest

from app.common.enums import PaymentStatus as S
from app.modules.payments.state import InvalidTransitionError, can_transition, ensure_transition

ALLOWED = {
    (S.CREATED, S.PENDING),
    (S.CREATED, S.FAILED),
    (S.PENDING, S.SUCCESS),
    (S.PENDING, S.FAILED),
    (S.FAILED, S.PENDING),
    (S.SUCCESS, S.REFUNDED),
}


@pytest.mark.parametrize("current", list(S))
@pytest.mark.parametrize("target", list(S))
def test_only_the_listed_transitions_are_allowed(current: S, target: S) -> None:
    assert can_transition(current, target) is ((current, target) in ALLOWED)


def test_success_needs_pending_first_and_refunded_is_final() -> None:
    with pytest.raises(InvalidTransitionError):
        ensure_transition(S.CREATED, S.SUCCESS)
    with pytest.raises(InvalidTransitionError):
        ensure_transition(S.FAILED, S.SUCCESS)
    for target in S:
        assert not can_transition(S.REFUNDED, target)
