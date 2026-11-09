import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import GatewayCustomer, Payment, PaymentEvent


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


class PaymentRepository:
    """User-facing lookups are scoped to the user; by-order lookups are for the gateway
    (webhooks), which has no user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_key(self, user_id: uuid.UUID, key: str) -> Payment | None:
        return await self._session.scalar(
            select(Payment).where(Payment.user_id == user_id, Payment.idempotency_key == key)
        )

    async def get(
        self, user_id: uuid.UUID, payment_id: uuid.UUID, *, for_update: bool = False
    ) -> Payment | None:
        query = select(Payment).where(Payment.user_id == user_id, Payment.id == payment_id)
        if for_update:
            # Verify, polling and webhooks for one payment run one at a time.
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_by_order_id(self, order_id: str, *, for_update: bool = False) -> Payment | None:
        query = select(Payment).where(Payment.gateway_order_id == order_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_by_gateway_payment_id(
        self, gateway_payment_id: str, *, for_update: bool = False
    ) -> Payment | None:
        query = select(Payment).where(Payment.gateway_payment_id == gateway_payment_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def add_if_absent(self, payment: Payment) -> bool:
        try:
            async with self._session.begin_nested():
                self._session.add(payment)
        except IntegrityError:
            return False
        return True

    def add_event(self, event: PaymentEvent) -> None:
        self._session.add(event)

    async def events(self, payment_id: uuid.UUID) -> list[PaymentEvent]:
        result = await self._session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at)
        )
        return list(result)

    async def flush(self) -> None:
        await self._session.flush()
