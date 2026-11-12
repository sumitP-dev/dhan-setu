import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin_auth.models import AdminSession


class AdminSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, admin_session: AdminSession) -> None:
        self._session.add(admin_session)

    async def get(self, session_id: uuid.UUID) -> AdminSession | None:
        return await self._session.get(AdminSession, session_id)

    async def get_by_refresh_hash(self, token_hash: str) -> AdminSession | None:
        """Locked, so two refreshes with the same cookie are handled one after the other."""
        return await self._session.scalar(
            select(AdminSession)
            .where(AdminSession.refresh_token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_by_previous_hash(self, token_hash: str) -> AdminSession | None:
        return await self._session.scalar(
            select(AdminSession)
            .where(AdminSession.previous_refresh_token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def revoke(self, session_id: uuid.UUID, *, now: datetime, reason: str) -> None:
        await self._session.execute(
            update(AdminSession)
            .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason=reason)
            .execution_options(synchronize_session="fetch")
        )

    async def flush(self) -> None:
        await self._session.flush()
