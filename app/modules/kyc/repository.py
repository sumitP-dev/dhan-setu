import uuid
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import KycStatus
from app.modules.kyc.models import (
    KycAccessLog,
    KycDigiLockerSession,
    KycEvent,
    KycProfile,
    KycSelfieSession,
)
from app.modules.kyc.provider.base import DigiLockerStatus, SelfieStatus
from app.modules.users.models import User


class KycRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- Status

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

    async def latest_event(self, user_id: uuid.UUID, to_status: KycStatus) -> KycEvent | None:
        return await self._session.scalar(
            select(KycEvent)
            .where(KycEvent.user_id == user_id, KycEvent.to_status == to_status)
            .order_by(KycEvent.created_at.desc(), KycEvent.id.desc())
            .limit(1)
        )

    # ---- Admin review

    def _submitted_at(self) -> Select[uuid.UUID, datetime]:
        """When each user last submitted for review."""
        return (
            select(KycEvent.user_id, func.max(KycEvent.created_at).label("submitted_at"))
            .where(KycEvent.to_status == KycStatus.PENDING_REVIEW)
            .group_by(KycEvent.user_id)
        )

    async def list_for_review(
        self, status: KycStatus, *, limit: int, offset: int
    ) -> list[tuple[User, KycProfile | None, KycSelfieSession | None, datetime | None]]:
        """Users in this KYC status, the longest-waiting submission first."""
        submitted = self._submitted_at().subquery()
        result = await self._session.execute(
            select(User, KycProfile, KycSelfieSession, submitted.c.submitted_at)
            .outerjoin(KycProfile, KycProfile.user_id == User.id)
            .outerjoin(KycSelfieSession, KycSelfieSession.id == KycProfile.selfie_session_id)
            .outerjoin(submitted, submitted.c.user_id == User.id)
            .where(User.kyc_status == status)
            .order_by(submitted.c.submitted_at.asc().nulls_last(), User.created_at, User.id)
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], row[1], row[2], row[3]) for row in result.all()]

    async def count_by_status(self, status: KycStatus) -> int:
        count = await self._session.scalar(
            select(func.count()).select_from(User).where(User.kyc_status == status)
        )
        return count or 0

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def submitted_at(self, user_id: uuid.UUID) -> datetime | None:
        event = await self.latest_event(user_id, KycStatus.PENDING_REVIEW)
        return event.created_at if event else None

    def add_access_log(self, log: KycAccessLog) -> None:
        self._session.add(log)

    # ---- DigiLocker sessions

    def add_session(self, session: KycDigiLockerSession) -> None:
        self._session.add(session)

    async def get_session(
        self, session_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
    ) -> KycDigiLockerSession | None:
        query = select(KycDigiLockerSession).where(
            KycDigiLockerSession.id == session_id, KycDigiLockerSession.user_id == user_id
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self._session.scalar(query)

    async def get_session_by_state(self, state_hash: str) -> KycDigiLockerSession | None:
        return await self._session.scalar(
            select(KycDigiLockerSession)
            .where(KycDigiLockerSession.state_hash == state_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def open_session(self, user_id: uuid.UUID, now: datetime) -> KycDigiLockerSession | None:
        return await self._session.scalar(
            select(KycDigiLockerSession)
            .where(
                KycDigiLockerSession.user_id == user_id,
                KycDigiLockerSession.status == DigiLockerStatus.PENDING,
                KycDigiLockerSession.expires_at > now,
            )
            .order_by(KycDigiLockerSession.created_at.desc())
            .limit(1)
        )

    async def count_sessions_since(self, user_id: uuid.UUID, since: datetime) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(KycDigiLockerSession)
            .where(
                KycDigiLockerSession.user_id == user_id,
                KycDigiLockerSession.created_at >= since,
            )
        )
        return count or 0

    # ---- Selfie sessions

    async def get_selfie_by_id(self, selfie_id: uuid.UUID) -> KycSelfieSession | None:
        return await self._session.get(KycSelfieSession, selfie_id)

    def add_selfie(self, selfie: KycSelfieSession) -> None:
        self._session.add(selfie)

    async def get_selfie(
        self, selfie_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
    ) -> KycSelfieSession | None:
        query = select(KycSelfieSession).where(
            KycSelfieSession.id == selfie_id, KycSelfieSession.user_id == user_id
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self._session.scalar(query)

    async def open_selfie(
        self, user_id: uuid.UUID, digilocker_session_id: uuid.UUID, now: datetime
    ) -> KycSelfieSession | None:
        return await self._session.scalar(
            select(KycSelfieSession)
            .where(
                KycSelfieSession.user_id == user_id,
                KycSelfieSession.digilocker_session_id == digilocker_session_id,
                KycSelfieSession.status == SelfieStatus.PENDING,
                KycSelfieSession.expires_at > now,
            )
            .order_by(KycSelfieSession.created_at.desc())
            .limit(1)
        )

    async def count_selfies_since(self, user_id: uuid.UUID, since: datetime) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(KycSelfieSession)
            .where(KycSelfieSession.user_id == user_id, KycSelfieSession.created_at >= since)
        )
        return count or 0

    # ---- Profiles

    async def get_profile(
        self, user_id: uuid.UUID, *, for_update: bool = False
    ) -> KycProfile | None:
        query = select(KycProfile).where(KycProfile.user_id == user_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self._session.scalar(query)

    def add_profile(self, profile: KycProfile) -> None:
        self._session.add(profile)

    async def pan_used_by_another_user(self, pan_hash: str, user_id: uuid.UUID) -> bool:
        owner = await self._session.scalar(
            select(KycProfile.user_id).where(KycProfile.pan_hash == pan_hash)
        )
        return owner is not None and owner != user_id

    async def flush(self) -> None:
        await self._session.flush()
