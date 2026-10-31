import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import OtpPurpose
from app.modules.auth.models import OtpCode, RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )

    async def consume(self, token_hash: str, *, now: datetime) -> RefreshToken | None:
        """Atomically revoke a valid (unrevoked, unexpired) token and return it.

        A single conditional UPDATE, so two concurrent requests with the same token
        can't both succeed. Returns None if the token is unknown, expired or already used.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .values(revoked_at=now)
            .returning(RefreshToken)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )


class OtpRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, otp: OtpCode) -> OtpCode:
        self._session.add(otp)
        await self._session.flush()
        return otp

    async def get_active(self, phone: str, purpose: OtpPurpose, *, now: datetime) -> OtpCode | None:
        """The newest unused, unexpired code for this phone and purpose."""
        return await self._session.scalar(
            select(OtpCode)
            .where(
                OtpCode.phone == phone,
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
                OtpCode.expires_at > now,
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )

    async def invalidate_active(self, phone: str, purpose: OtpPurpose, *, now: datetime) -> None:
        """Retire older codes when a new one is sent, so only the latest works."""
        await self._session.execute(
            update(OtpCode)
            .where(
                OtpCode.phone == phone,
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )

    async def record_attempt(self, otp_id: uuid.UUID, *, max_attempts: int) -> bool:
        """Count one guess. A single conditional UPDATE, so parallel guesses can't exceed
        the limit. Returns False once the limit is used up."""
        result = await self._session.execute(
            update(OtpCode)
            .where(OtpCode.id == otp_id, OtpCode.attempts < max_attempts)
            .values(attempts=OtpCode.attempts + 1)
            .returning(OtpCode.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def consume(self, otp_id: uuid.UUID, *, now: datetime) -> bool:
        """Mark the code used. Returns False if a parallel request already used it."""
        result = await self._session.execute(
            update(OtpCode)
            .where(OtpCode.id == otp_id, OtpCode.consumed_at.is_(None))
            .values(consumed_at=now)
            .returning(OtpCode.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None
