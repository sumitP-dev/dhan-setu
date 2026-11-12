import uuid

from app.common.enums import KycStatus
from app.common.exceptions import NotFoundError
from app.modules.kyc.models import KycEvent
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.state import InvalidKycTransitionError, ensure_transition
from app.modules.users.models import User


class KycService:
    def __init__(self, repository: KycRepository) -> None:
        self._repository = repository

    async def transition(
        self,
        user_id: uuid.UUID,
        target: KycStatus,
        *,
        reason: str,
        actor_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> User:
        """The only way a KYC status changes: under a row lock, checked against the
        allowed changes, and recorded in kyc_events."""
        user = await self._repository.lock_user(user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        current = user.kyc_status
        ensure_transition(current, target)
        if not await self._repository.set_status(user.id, current=current, target=target):
            raise InvalidKycTransitionError
        self._repository.add_event(
            KycEvent(
                user_id=user.id,
                from_status=current,
                to_status=target,
                reason=reason,
                actor_id=actor_id,
                note=note,
            )
        )
        await self._repository.flush()
        return user
