import uuid

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        return await self._session.scalar(select(User).where(User.email == email))

    async def get_by_phone(self, phone: str) -> User | None:
        return await self._session.scalar(select(User).where(User.phone == phone))

    async def email_exists(self, email: str) -> bool:
        return await self._exists(User.email == email)

    async def phone_exists(self, phone: str) -> bool:
        return await self._exists(User.phone == phone)

    async def _exists(self, condition: ColumnElement[bool]) -> bool:
        found = await self._session.scalar(select(User.id).where(condition).limit(1))
        return found is not None

    async def add(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def save(self, user: User) -> User:
        await self._session.flush()
        return user
