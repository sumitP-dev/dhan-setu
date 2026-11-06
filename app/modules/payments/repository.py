import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import GatewayCustomer


class GatewayCustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_user(
        self, user_id: uuid.UUID, *, for_update: bool = False
    ) -> GatewayCustomer | None:
        query = select(GatewayCustomer).where(GatewayCustomer.user_id == user_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def add_if_absent(self, customer: GatewayCustomer) -> bool:
        """False if a row for this user (or this gateway customer) already exists."""
        try:
            async with self._session.begin_nested():
                self._session.add(customer)
        except IntegrityError:
            return False
        return True
