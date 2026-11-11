import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import KycStatus
from app.modules.kyc.models import KycEvent
from app.modules.users.models import User


class KycRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_user(self, user_id: uuid.UUID) -> User | None:
        """The user row, locked until the transaction ends, so two requests can't
        change the KYC status at the same time."""
        result = await self._session.scalars(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def set_status(
        self, user_id: uuid.UUID, *, current: KycStatus, target: KycStatus
    ) -> bool:
        """Changes the status only if it is still `current`. False if something else
        changed it first (a second guard besides the row lock)."""
        result = await self._session.execute(
            update(User)
            .where(User.id == user_id, User.kyc_status == current)
            .values(kyc_status=target)
            .execution_options(synchronize_session="fetch")
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def add_event(self, event: KycEvent) -> None:
        self._session.add(event)

    async def events(self, user_id: uuid.UUID) -> list[KycEvent]:
        result = await self._session.scalars(
            select(KycEvent).where(KycEvent.user_id == user_id).order_by(KycEvent.created_at)
        )
        return list(result)

    async def flush(self) -> None:
        await self._session.flush()
